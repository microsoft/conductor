"""Tests for ``conductor.engine.secrets`` — the secret resolver, run-scoped
value cache, and per-config delivery index.

Covers the QA matrix:

* Happy path: resolving script-scope and mcp-scope references, idempotent
  re-resolution, per-ref caching, ``allow=None`` passing any consumer class,
  ``allow=["mcp"]`` passing mcp and rejecting script, redactor registration
  (scrubbing an event-like dict yields the marker, never the value), index
  isolation across root/child configs with identical step names, and two
  concurrent child indexes over one shared cache.
* Failure path: unknown references listing available names, unset/empty
  source variables naming the variable and consumer without the value, allow
  violations, the built-in environment rejecting any reference, and
  ``repr(ResolvedSecret)`` containing no value.

Environment isolation uses ``monkeypatch`` exclusively; every test secret
uses a unique ``CONDUCTOR_TEST_TASK4_*`` variable name so an ambient shell
can never satisfy or break an assertion.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, Literal

import pytest

from conductor.config.environment import (
    EnvironmentDocument,
    ProfileDefinition,
    ResolvedEnvironment,
    SecretBinding,
    SecretBindingSource,
    builtin_local_environment,
)
from conductor.config.schema import (
    AgentDef,
    ForEachDef,
    MCPServerDef,
    OutputField,
    RouteDef,
    RuntimeConfig,
    ScriptStepDef,
    SecretDelivery,
    StepExecutionConfig,
    StepSecretRef,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.secrets import (
    IndexedSecretUse,
    ResolvedSecret,
    SecretUseIndex,
    SecretValueCache,
    index_config,
)
from conductor.exceptions import ConfigurationError
from conductor.redaction import REDACTED_MARKER, RunRedactor

_VAR_SCRIPT = "CONDUCTOR_TEST_TASK4_SCRIPT_KEY"
_VAR_MCP = "CONDUCTOR_TEST_TASK4_MCP_TOKEN"
_VAR_ROOT = "CONDUCTOR_TEST_TASK4_ROOT_KEY"
_VAR_CHILD = "CONDUCTOR_TEST_TASK4_CHILD_KEY"
_VAR_SHORT = "CONDUCTOR_TEST_TASK4_SHORT"

_SECRET_SCRIPT = "task4-script-value"
_SECRET_MCP = "task4-mcp-value-0123456789"


def _binding(
    env_var: str, allow: list[Literal["script", "mcp", "agent"]] | None = None
) -> SecretBinding:
    """Build a secret binding resolving from one environment variable."""
    return SecretBinding(source=SecretBindingSource(env=env_var), allow=allow)


def _environment(
    secrets: dict[str, SecretBinding] | None,
    *,
    name: str = "task4-env",
    source: Literal["path", "project", "user"] = "path",
) -> ResolvedEnvironment:
    """Build a resolved authored environment carrying the given bindings."""
    document = EnvironmentDocument(
        default="default",
        profiles={"default": ProfileDefinition(backend="local")},
        secrets=secrets,
    )
    canonical = json.dumps(document.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return ResolvedEnvironment(
        document=document,
        name=name,
        source=source,
        path=None,
        digest=f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}",
    )


def _step_secret(
    ref: str,
    scope: Literal["script", "mcp", "agent"],
    *,
    env: str | None = None,
    header: str | None = None,
) -> StepSecretRef:
    """Build a step-level secret reference with an env or header delivery."""
    delivery = SecretDelivery(env=env) if env is not None else SecretDelivery(header=header)
    return StepSecretRef(ref=ref, scope=scope, delivery=delivery)


def _script_config(
    step_name: str = "run",
    *,
    secrets: list[StepSecretRef] | None = None,
) -> WorkflowConfig:
    """Build a one-script-step workflow config, optionally with step secrets."""
    execution = StepExecutionConfig(secrets=secrets) if secrets else None
    return WorkflowConfig(
        workflow=WorkflowDef(name="task4", entry_point=step_name),
        agents=[
            ScriptStepDef(
                name=step_name,
                command="echo ok",
                execution=execution,
                routes=[RouteDef(to="$end")],
            )
        ],
        output={},
    )


def _mcp_config(
    server_secrets: list[StepSecretRef],
    *,
    step_name: str = "start",
) -> WorkflowConfig:
    """Build a config with one agent and one MCP server carrying secrets."""
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="task4-mcp",
            entry_point=step_name,
            runtime=RuntimeConfig(
                mcp_servers={
                    "docs": MCPServerDef(
                        command="docs-server",
                        secrets=server_secrets,
                    )
                }
            ),
        ),
        agents=[
            AgentDef(
                name=step_name,
                model="gpt-4",
                prompt="test",
                output={"value": OutputField(type="string")},
                routes=[RouteDef(to="$end")],
            )
        ],
        output={},
    )


def _authored_cache(
    secrets: dict[str, SecretBinding],
    redactor: RunRedactor | None = None,
) -> SecretValueCache:
    """Build a cache over an authored environment plus a fresh redactor."""
    return SecretValueCache(_environment(secrets), redactor or RunRedactor())


@pytest.fixture
def script_cache(monkeypatch: pytest.MonkeyPatch) -> SecretValueCache:
    """Cache over an authored environment with the standard script/mcp bindings."""
    monkeypatch.setenv(_VAR_SCRIPT, _SECRET_SCRIPT)
    monkeypatch.setenv(_VAR_MCP, _SECRET_MCP)
    return _authored_cache(
        {
            "script_key": _binding(_VAR_SCRIPT),
            "mcp_token": _binding(_VAR_MCP, allow=["mcp"]),
        }
    )


class TestResolve:
    """Happy-path resolution through ``SecretValueCache.resolve``."""

    def test_resolve_script_ref(self, script_cache: SecretValueCache) -> None:
        # Requirement: a binding with allow=None resolves for a script-scope
        # consumer and exposes the plaintext through the value property only.
        secret = script_cache.resolve("script_key", "script", "step 'run'")

        assert isinstance(secret, ResolvedSecret)
        assert secret.ref == "script_key"
        assert secret.allow is None
        assert secret.value == _SECRET_SCRIPT

    def test_resolve_mcp_ref(self, script_cache: SecretValueCache) -> None:
        # Requirement: a binding restricted via allow=["mcp"] resolves for an
        # mcp-scope consumer.
        secret = script_cache.resolve("mcp_token", "mcp", "MCP server 'docs'")

        assert secret.value == _SECRET_MCP
        assert secret.allow == frozenset({"mcp"})

    def test_resolve_is_idempotent_per_ref(self, script_cache: SecretValueCache) -> None:
        # Requirement: re-resolving the same ref returns the identical object
        # (per-ref cache) — the value is read and registered in the redactor
        # exactly once.
        first = script_cache.resolve("script_key", "script", "step 'run'")
        second = script_cache.resolve("script_key", "script", "step 'run'")

        assert second is first

    def test_allow_none_passes_any_consumer_class(self, script_cache: SecretValueCache) -> None:
        # Requirement: allow=None (the default) permits every consumer class
        # forever — script, mcp, and agent alike.
        for consumer_class in ("script", "mcp", "agent"):
            secret = script_cache.resolve(
                "script_key", consumer_class, f"consumer {consumer_class}"
            )
            assert secret.value == _SECRET_SCRIPT

    def test_allow_mcp_passes_mcp(self, script_cache: SecretValueCache) -> None:
        # Requirement: allow=["mcp"] passes an mcp consumer.
        secret = script_cache.resolve("mcp_token", "mcp", "MCP server 'docs'")
        assert secret.value == _SECRET_MCP

    def test_agent_only_binding_rechecks_other_scopes_on_cache_hit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: agent-only bindings resolve for agents but deny script and MCP, even cached.
        monkeypatch.setenv(_VAR_SCRIPT, _SECRET_SCRIPT)
        cache = _authored_cache({"token": _binding(_VAR_SCRIPT, allow=["agent"])})
        assert cache.resolve("token", "agent", "step 'worker'").value == _SECRET_SCRIPT
        for scope in ("script", "mcp"):
            with pytest.raises(ConfigurationError, match="allows only: agent"):
                cache.resolve("token", scope, f"consumer {scope}")

    def test_allow_checked_on_every_resolve_even_cache_hit(
        self, script_cache: SecretValueCache
    ) -> None:
        # Requirement: the allow policy is enforced on EVERY resolve call,
        # including cache hits — a ref approved for "mcp" cached earlier must
        # not later be served to a "script" consumer without re-checking.
        script_cache.resolve("mcp_token", "mcp", "MCP server 'docs'")

        with pytest.raises(ConfigurationError, match="allows only: mcp"):
            script_cache.resolve("mcp_token", "script", "step 'run'")

    def test_allow_empty_list_is_fail_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Requirement: allow=[] is explicitly fail-closed — no consumer class
        # resolves, and the error says so.
        monkeypatch.setenv(_VAR_SCRIPT, _SECRET_SCRIPT)
        cache = _authored_cache({"locked": _binding(_VAR_SCRIPT, allow=[])})

        with pytest.raises(ConfigurationError, match="allows no consumer classes"):
            cache.resolve("locked", "script", "step 'run'")

    def test_resolved_secret_repr_contains_no_value(self, script_cache: SecretValueCache) -> None:
        # Requirement: repr(ResolvedSecret) names the ref and the allow set
        # but never the plaintext value.
        secret = script_cache.resolve("script_key", "script", "step 'run'")

        rendered = repr(secret)
        assert rendered == "ResolvedSecret(ref='script_key', allow=None)"
        assert _SECRET_SCRIPT not in rendered
        # The value is still reachable through the property path.
        assert secret.value == _SECRET_SCRIPT


class TestRedactorRegistration:
    """Registration of resolved values into the run redactor."""

    def test_registration_scrubs_event_like_dict(self, script_cache: SecretValueCache) -> None:
        # Requirement: every resolved value is registered into the run
        # redactor — scrubbing an event-like dict replaces the value with the
        # marker everywhere (strings, nesting), and the plaintext survives
        # nowhere in the scrubbed structure.
        secret = script_cache.resolve("script_key", "script", "step 'run'")

        event: dict[str, Any] = {
            "message": f"script failed with token {secret.value}",
            "payload": {"nested": [{"token": secret.value}], "count": 3},
        }
        scrubbed = script_cache_redactor(script_cache).scrub_event_data(event)

        assert scrubbed["message"] == f"script failed with token {REDACTED_MARKER}"
        assert scrubbed["payload"]["nested"] == [{"token": REDACTED_MARKER}]
        assert scrubbed["payload"]["count"] == 3
        assert _SECRET_SCRIPT not in json.dumps(scrubbed)

    def test_short_secret_logs_warning_naming_ref_not_value(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Requirement: registering a short value surfaces a logger.warning
        # naming the ref (never the value), flagging over-redaction risk.
        monkeypatch.setenv(_VAR_SHORT, "abc123")
        cache = _authored_cache({"short_one": _binding(_VAR_SHORT)})

        with caplog.at_level(logging.WARNING, logger="conductor.engine.secrets"):
            cache.resolve("short_one", "script", "step 'run'")

        ref_records = [r for r in caplog.records if "short_one" in r.getMessage()]
        assert len(ref_records) == 1
        assert "abc123" not in ref_records[0].getMessage()
        assert "shorter than" in ref_records[0].getMessage()


def script_cache_redactor(cache: SecretValueCache) -> RunRedactor:
    """Reach the redactor a cache was built with (test-only helper)."""
    return cache._redactor


class TestResolveFailures:
    """Failure matrix for ``SecretValueCache.resolve``."""

    def test_unknown_ref_lists_available_names(self, script_cache: SecretValueCache) -> None:
        # Requirement: an unknown reference raises ConfigurationError naming
        # the ref and listing every available binding name.
        with pytest.raises(ConfigurationError) as exc_info:
            script_cache.resolve("nope", "script", "step 'run'")

        message = str(exc_info.value)
        assert "nope" in message
        assert "script_key" in message
        assert "mcp_token" in message

    def test_missing_source_variable_names_variable_and_consumer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: an unset source variable raises ConfigurationError
        # naming the variable and the consumer — never the (nonexistent)
        # value.
        monkeypatch.delenv(_VAR_SCRIPT, raising=False)
        cache = _authored_cache({"script_key": _binding(_VAR_SCRIPT)})

        with pytest.raises(ConfigurationError) as exc_info:
            cache.resolve("script_key", "script", "step 'run'")

        message = str(exc_info.value)
        assert _VAR_SCRIPT in message
        assert "step 'run'" in message
        assert "not set or is empty" in message

    def test_empty_source_variable_is_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Requirement: an empty-string source variable is treated exactly
        # like an unset one (an empty secret is a misconfiguration, not a
        # value).
        monkeypatch.setenv(_VAR_SCRIPT, "")
        cache = _authored_cache({"script_key": _binding(_VAR_SCRIPT)})

        with pytest.raises(ConfigurationError, match="not set or is empty"):
            cache.resolve("script_key", "script", "step 'run'")

    def test_allow_violation_names_allow_list(self, script_cache: SecretValueCache) -> None:
        # Requirement: an allow violation raises ConfigurationError naming
        # the consumer class and the binding's allow list.
        with pytest.raises(ConfigurationError) as exc_info:
            script_cache.resolve("mcp_token", "script", "step 'run'")

        message = str(exc_info.value)
        assert "script" in message
        assert "mcp" in message
        assert "step 'run'" in message
        assert _SECRET_MCP not in message

    def test_builtin_environment_rejects_secret_references(self) -> None:
        # Requirement: the built-in environment declares no secrets, so any
        # reference against it raises ConfigurationError demanding an
        # authored environment document.
        cache = SecretValueCache(builtin_local_environment(), RunRedactor())

        with pytest.raises(ConfigurationError) as exc_info:
            cache.resolve("anything", "script", "step 'run'")

        message = str(exc_info.value)
        assert "authored environment document" in message
        assert "anything" in message

    def test_secret_for_unknown_ref_raises(self, script_cache: SecretValueCache) -> None:
        # Requirement: reading a ref that was never resolved raises
        # ConfigurationError rather than KeyError.
        with pytest.raises(ConfigurationError, match="has not been resolved"):
            script_cache.secret_for("script_key")

    def test_clear_drops_resolved_secrets(
        self, script_cache: SecretValueCache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: clear() empties the per-ref cache; subsequent reads
        # fail until the ref is resolved again.
        script_cache.resolve("script_key", "script", "step 'run'")
        script_cache.clear()

        with pytest.raises(ConfigurationError, match="has not been resolved"):
            script_cache.secret_for("script_key")

        # Re-resolution works after a clear (the env var is still set).
        monkeypatch.setenv(_VAR_SCRIPT, _SECRET_SCRIPT)
        secret = script_cache.resolve("script_key", "script", "step 'run'")
        assert secret.value == _SECRET_SCRIPT


class TestIndexConfig:
    """``index_config`` delivery maps and eager fail-fast resolution."""

    def test_step_env_map_resolves_values_through_cache(
        self, script_cache: SecretValueCache
    ) -> None:
        # Requirement: secret_env_for_step returns the env-name → plaintext
        # dict for one step, resolved through the cache at call time.
        config = _script_config(
            "run",
            secrets=[_step_secret("script_key", "script", env="API_KEY")],
        )
        index = index_config(config, script_cache)

        assert index.secret_env_for_step("run") == {"API_KEY": _SECRET_SCRIPT}
        # A step with no secrets yields an empty env dict, as does an
        # unknown step key.
        assert index.secret_env_for_step("start") == {}

    def test_mcp_server_deliveries_and_value_for(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Requirement: deliveries_for_server returns every use-site record
        # (ref, kind, name) for a server, and value_for is the public
        # read-only plaintext path MCP delivery calls — the index itself
        # stores no plaintext.
        monkeypatch.setenv(_VAR_MCP, _SECRET_MCP)
        cache = _authored_cache({"mcp_token": _binding(_VAR_MCP, allow=["mcp"])})
        config = _mcp_config(
            [
                _step_secret("mcp_token", "mcp", env="DOCS_TOKEN"),
                _step_secret("mcp_token", "mcp", header="X-Api-Key"),
            ]
        )
        index = index_config(config, cache)

        deliveries = index.deliveries_for_server("docs")
        assert deliveries == (
            IndexedSecretUse(ref="mcp_token", delivery_kind="env", delivery_name="DOCS_TOKEN"),
            IndexedSecretUse(ref="mcp_token", delivery_kind="header", delivery_name="X-Api-Key"),
        )
        assert index.deliveries_for_server("unknown") == ()
        assert index.value_for("mcp_token") == _SECRET_MCP

    def test_index_resolves_for_each_inline_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Requirement: inline for-each agents are collected under the
        # for_each.<group>.agent identity key, matching the manifest scheme.
        monkeypatch.setenv(_VAR_SCRIPT, _SECRET_SCRIPT)
        cache = _authored_cache({"script_key": _binding(_VAR_SCRIPT)})
        config = WorkflowConfig(
            workflow=WorkflowDef(name="task4-foreach", entry_point="batch"),
            agents=[],
            for_each=[
                ForEachDef(
                    name="batch",
                    type="for_each",
                    source="workflow.input.items",
                    **{"as": "item"},
                    agent=ScriptStepDef(
                        name="worker",
                        command="echo {{ item }}",
                        execution=StepExecutionConfig(
                            secrets=[_step_secret("script_key", "script", env="WORKER_KEY")]
                        ),
                        routes=[RouteDef(to="$end")],
                    ),
                )
            ],
            output={},
        )

        index = index_config(config, cache)

        assert index.secret_env_for_step("for_each.batch.agent") == {"WORKER_KEY": _SECRET_SCRIPT}

    def test_index_fail_fast_on_unknown_ref(self, script_cache: SecretValueCache) -> None:
        # Requirement: a config referencing an unknown secret fails at index
        # time (eager resolution for THIS config), not mid-execution.
        config = _script_config("run", secrets=[_step_secret("ghost", "script", env="GHOST_KEY")])

        with pytest.raises(ConfigurationError, match="Unknown secret reference"):
            index_config(config, script_cache)

    def test_index_fail_fast_on_allow_violation(self, script_cache: SecretValueCache) -> None:
        # Requirement: step secrets resolve under their declared scope as the
        # consumer class, so a script-scoped use of an mcp-only binding fails
        # at index time.
        config = _script_config("run", secrets=[_step_secret("mcp_token", "script", env="TOKEN")])

        with pytest.raises(ConfigurationError, match="allows only: mcp"):
            index_config(config, script_cache)


class TestIndexIsolation:
    """Root/child index independence over one shared cache (Oracle R1-B6)."""

    def _dual_cache(self, monkeypatch: pytest.MonkeyPatch) -> SecretValueCache:
        monkeypatch.setenv(_VAR_ROOT, "task4-root-value")
        monkeypatch.setenv(_VAR_CHILD, "task4-child-value")
        return _authored_cache(
            {
                "root_key": _binding(_VAR_ROOT),
                "child_key": _binding(_VAR_CHILD),
            }
        )

    def test_child_index_does_not_touch_root_maps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Requirement: identical step names in the root and a child config
        # index independently — building the child index over the shared
        # cache leaves the root's delivery maps untouched, and the root step
        # resolves exactly as before afterwards.
        cache = self._dual_cache(monkeypatch)
        root_config = _script_config(
            "run", secrets=[_step_secret("root_key", "script", env="ROOT_TOKEN")]
        )
        child_config = _script_config(
            "run", secrets=[_step_secret("child_key", "script", env="CHILD_TOKEN")]
        )

        root_index = index_config(root_config, cache)
        assert root_index.secret_env_for_step("run") == {"ROOT_TOKEN": "task4-root-value"}

        child_index = index_config(child_config, cache)
        assert child_index.secret_env_for_step("run") == {"CHILD_TOKEN": "task4-child-value"}

        # Root delivery maps are untouched by the child build, and still
        # resolve after it.
        assert root_index.secret_env_for_step("run") == {"ROOT_TOKEN": "task4-root-value"}
        assert "CHILD_TOKEN" not in root_index.secret_env_for_step("run")

    def test_two_concurrent_child_indexes_over_one_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: two child configs indexed concurrently (threads, as
        # the cache is sync) over one shared cache both resolve correctly
        # and stay independent — the cache is the only shared state and its
        # per-ref entries are idempotent.
        cache = self._dual_cache(monkeypatch)
        first_config = _script_config(
            "run", secrets=[_step_secret("root_key", "script", env="FIRST_TOKEN")]
        )
        second_config = _script_config(
            "run", secrets=[_step_secret("child_key", "script", env="SECOND_TOKEN")]
        )

        async def _build_both() -> tuple[SecretUseIndex, SecretUseIndex]:
            return await asyncio.gather(
                asyncio.to_thread(index_config, first_config, cache),
                asyncio.to_thread(index_config, second_config, cache),
            )

        first_index, second_index = asyncio.run(_build_both())

        assert first_index.secret_env_for_step("run") == {"FIRST_TOKEN": "task4-root-value"}
        assert second_index.secret_env_for_step("run") == {"SECOND_TOKEN": "task4-child-value"}
        # The shared cache served both children; each ref resolved once.
        assert cache.secret_for("root_key").value == "task4-root-value"
        assert cache.secret_for("child_key").value == "task4-child-value"

    def test_index_maps_do_not_store_plaintext(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Requirement: the delivery maps hold refs/kinds/names only — no
        # plaintext value appears anywhere in the index's internal state.
        monkeypatch.setenv(_VAR_ROOT, "task4-root-value")
        cache = _authored_cache({"root_key": _binding(_VAR_ROOT)})
        config = _script_config(
            "run", secrets=[_step_secret("root_key", "script", env="ROOT_TOKEN")]
        )
        index = index_config(config, cache)

        internal = json.dumps(
            {
                "step_uses": {k: [vars(use) for use in v] for k, v in index._step_uses.items()},
                "server_uses": {k: [vars(use) for use in v] for k, v in index._server_uses.items()},
            },
            default=str,
        )
        assert "task4-root-value" not in internal
        assert index.secret_env_for_step("run") == {"ROOT_TOKEN": "task4-root-value"}
