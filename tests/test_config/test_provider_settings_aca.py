"""Tests for the ``aca`` structured provider configuration and per-agent
``sandbox:`` block.

Covers issue #284 (E1 — ``aca`` configuration surface): parsing/validation of
``runtime.provider: {name: aca, ...}`` and the per-agent ``sandbox:`` block,
mirroring the existing ``ProviderSettings`` guardrails for copilot/claude/hermes.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from conductor.config.schema import (
    AgentDef,
    ProviderSettings,
    RuntimeConfig,
    SandboxConfig,
)


class TestAcaProviderSettings:
    """``ProviderSettings`` accepts ``name: aca`` with its own field set."""

    def test_minimal_aca_config_round_trips(self) -> None:
        s = ProviderSettings(name="aca", pool_endpoint="https://my-pool.example.com")
        assert s.name == "aca"
        assert s.pool_endpoint == "https://my-pool.example.com"
        # inner_provider/identifier_scope/auth default when left unset in YAML.
        assert s.inner_provider == "copilot"
        assert s.identifier_scope == "agent"
        assert s.auth == "azure_default"
        # api_version/egress/lifecycle have no default; they stay unset.
        assert s.api_version is None
        assert s.egress is None
        assert s.lifecycle is None

    def test_full_aca_config_round_trips(self) -> None:
        s = ProviderSettings(
            name="aca",
            pool_endpoint="https://my-pool.example.com",
            api_version="2025-07-01",
            inner_provider="copilot",
            identifier_scope="workflow",
            egress="disabled",
            lifecycle="timed",
            auth="azure_default",
        )
        assert s.pool_endpoint == "https://my-pool.example.com"
        assert s.api_version == "2025-07-01"
        assert s.inner_provider == "copilot"
        assert s.identifier_scope == "workflow"
        assert s.egress == "disabled"
        assert s.lifecycle == "timed"
        assert s.auth == "azure_default"

    def test_aca_via_runtime_config_object_form(self) -> None:
        rc = RuntimeConfig.model_validate(
            {
                "provider": {
                    "name": "aca",
                    "pool_endpoint": "https://my-pool.example.com",
                }
            }
        )
        assert rc.provider.name == "aca"
        assert rc.provider.pool_endpoint == "https://my-pool.example.com"

    def test_missing_pool_endpoint_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="'pool_endpoint' is required"):
            ProviderSettings(name="aca")

    def test_empty_pool_endpoint_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="'pool_endpoint' is required"):
            ProviderSettings(name="aca", pool_endpoint="")

    def test_whitespace_pool_endpoint_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="'pool_endpoint' is required"):
            ProviderSettings(name="aca", pool_endpoint="   ")

    def test_http_pool_endpoint_rejected(self) -> None:
        """AAD bearer tokens and forwarded provider credentials
        (``inner_provider_settings``) are sent to ``pool_endpoint`` on every
        request — plain HTTP would leak both in transit."""
        with pytest.raises(PydanticValidationError, match="must use https://"):
            ProviderSettings(name="aca", pool_endpoint="http://my-pool.example.com")

    def test_http_pool_endpoint_rejected_case_insensitive_scheme(self) -> None:
        with pytest.raises(PydanticValidationError, match="must use https://"):
            ProviderSettings(name="aca", pool_endpoint="HTTP://my-pool.example.com")

    def test_non_http_scheme_pool_endpoint_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="must use https://"):
            ProviderSettings(name="aca", pool_endpoint="ftp://my-pool.example.com")

    def test_https_pool_endpoint_accepted(self) -> None:
        s = ProviderSettings(name="aca", pool_endpoint="https://my-pool.example.com")
        assert s.pool_endpoint == "https://my-pool.example.com"

    def test_invalid_inner_provider_literal_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            ProviderSettings(
                name="aca",
                pool_endpoint="https://my-pool.example.com",
                inner_provider="claude",  # type: ignore[arg-type]
            )

    def test_invalid_identifier_scope_literal_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            ProviderSettings(
                name="aca",
                pool_endpoint="https://my-pool.example.com",
                identifier_scope="global",  # type: ignore[arg-type]
            )

    def test_invalid_egress_literal_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            ProviderSettings(
                name="aca",
                pool_endpoint="https://my-pool.example.com",
                egress="open",  # type: ignore[arg-type]
            )

    def test_invalid_lifecycle_literal_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            ProviderSettings(
                name="aca",
                pool_endpoint="https://my-pool.example.com",
                lifecycle="always_on",  # type: ignore[arg-type]
            )

    def test_invalid_auth_literal_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            ProviderSettings(
                name="aca",
                pool_endpoint="https://my-pool.example.com",
                auth="managed_identity",  # type: ignore[arg-type]
            )


class TestAcaFieldGating:
    """``aca``-only fields are gated to ``name=='aca'`` and vice-versa."""

    @pytest.mark.parametrize(
        "field,value",
        [
            ("pool_endpoint", "https://my-pool.example.com"),
            ("api_version", "2025-07-01"),
            ("inner_provider", "copilot"),
            ("identifier_scope", "workflow"),
            ("egress", "enabled"),
            ("lifecycle", "timed"),
            ("auth", "azure_default"),
        ],
    )
    def test_aca_field_rejected_for_copilot(self, field: str, value: str) -> None:
        with pytest.raises(PydanticValidationError, match="only supported when name='aca'"):
            ProviderSettings(name="copilot", **{field: value})  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "field,value",
        [
            ("pool_endpoint", "https://my-pool.example.com"),
            ("api_version", "2025-07-01"),
            ("inner_provider", "copilot"),
            ("identifier_scope", "workflow"),
            ("egress", "enabled"),
            ("lifecycle", "timed"),
            ("auth", "azure_default"),
        ],
    )
    def test_aca_field_rejected_for_claude(self, field: str, value: str) -> None:
        with pytest.raises(PydanticValidationError, match="only supported when name='aca'"):
            ProviderSettings(name="claude", **{field: value})  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "field,value",
        [
            ("type", "openai"),
            ("wire_api", "completions"),
            ("bearer_token", "tok"),
            ("headers", {"X-Foo": "1"}),
            ("runtime_url", "localhost:3000"),
        ],
    )
    def test_copilot_only_field_rejected_for_aca(self, field: str, value: object) -> None:
        with pytest.raises(PydanticValidationError, match="only supported when name='copilot'"):
            ProviderSettings(
                name="aca",
                pool_endpoint="https://my-pool.example.com",
                **{field: value},  # type: ignore[arg-type]
            )

    def test_claude_only_field_rejected_for_aca(self) -> None:
        with pytest.raises(PydanticValidationError, match="only supported when name='claude'"):
            ProviderSettings(
                name="aca",
                pool_endpoint="https://my-pool.example.com",
                auth_token="tok",
            )

    def test_hermes_only_field_rejected_for_aca(self) -> None:
        with pytest.raises(PydanticValidationError, match="hermes_home"):
            ProviderSettings(
                name="aca",
                pool_endpoint="https://my-pool.example.com",
                hermes_home="~/.hermes",
            )

    def test_base_url_rejected_for_aca(self) -> None:
        with pytest.raises(PydanticValidationError, match="not yet implemented"):
            ProviderSettings(
                name="aca",
                pool_endpoint="https://my-pool.example.com",
                base_url="http://x/v1",
            )


class TestAcaProviderSettingsSerialization:
    """Round-trip serialization behavior for structured ``aca`` config."""

    def test_aca_never_collapses_to_bare_string(self) -> None:
        """``aca`` always requires ``pool_endpoint``, so it always has
        structured config and must never collapse to a bare string."""
        rc = RuntimeConfig.model_validate(
            {"provider": {"name": "aca", "pool_endpoint": "https://my-pool.example.com"}}
        )
        dumped = rc.model_dump(mode="json", exclude_none=True)
        # inner_provider/identifier_scope/auth are applied as defaults (not
        # left None), so they appear in the dump alongside the explicit fields.
        assert dumped["provider"] == {
            "name": "aca",
            "pool_endpoint": "https://my-pool.example.com",
            "inner_provider": "copilot",
            "identifier_scope": "agent",
            "auth": "azure_default",
        }

    def test_aca_has_structured_config(self) -> None:
        s = ProviderSettings(name="aca", pool_endpoint="https://my-pool.example.com")
        assert s.has_structured_config()


class TestSandboxConfig:
    """``SandboxConfig`` (per-agent ``sandbox:`` block) shape and validation."""

    def test_sandbox_config_defaults(self) -> None:
        sandbox = SandboxConfig()
        assert sandbox.identifier_scope is None
        assert sandbox.working_dir is None

    def test_sandbox_config_full(self) -> None:
        sandbox = SandboxConfig(identifier_scope="item", working_dir="/workspace/repo")
        assert sandbox.identifier_scope == "item"
        assert sandbox.working_dir == "/workspace/repo"

    def test_sandbox_config_invalid_identifier_scope_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            SandboxConfig(identifier_scope="global")  # type: ignore[arg-type]

    def test_sandbox_config_forbids_extra_fields(self) -> None:
        with pytest.raises(PydanticValidationError):
            SandboxConfig(bogus_field="x")  # type: ignore[call-arg]


class TestSandboxOnAgentDef:
    """``sandbox:`` validates only on provider-backed ``agent`` steps."""

    def test_sandbox_allowed_on_regular_agent(self) -> None:
        agent = AgentDef(
            name="llm",
            prompt="hi",
            sandbox=SandboxConfig(identifier_scope="item", working_dir="/workspace/repo"),
        )
        assert agent.sandbox is not None
        assert agent.sandbox.identifier_scope == "item"
        assert agent.sandbox.working_dir == "/workspace/repo"

    def test_sandbox_allowed_on_agent_via_dict(self) -> None:
        agent = AgentDef.model_validate(
            {
                "name": "llm",
                "prompt": "hi",
                "sandbox": {"identifier_scope": "workflow"},
            }
        )
        assert agent.sandbox is not None
        assert agent.sandbox.identifier_scope == "workflow"

    @pytest.mark.parametrize(
        "kwargs,match",
        [
            (
                {"name": "s", "type": "script", "command": "ls"},
                "script agents cannot have 'sandbox'",
            ),
            (
                {
                    "name": "g",
                    "type": "human_gate",
                    "prompt": "Pick",
                    "options": [{"label": "Yes", "value": "yes", "route": "$end"}],
                },
                "human_gate agents cannot have 'sandbox'",
            ),
            (
                {"name": "set", "type": "set", "value": "1"},
                "set agents cannot have 'sandbox'",
            ),
            (
                {"name": "w", "type": "wait", "duration": "1s"},
                "wait agents cannot have 'sandbox'",
            ),
            (
                {"name": "t", "type": "terminate", "status": "success", "reason": "done"},
                "terminate agents cannot have 'sandbox'",
            ),
            (
                {"name": "wf", "type": "workflow", "workflow": "./sub.yaml"},
                "workflow agents cannot have 'sandbox'",
            ),
        ],
        ids=["script", "human_gate", "set", "wait", "terminate", "workflow"],
    )
    def test_sandbox_rejected_on_non_provider_types(self, kwargs: dict, match: str) -> None:
        with pytest.raises(PydanticValidationError, match=match):
            AgentDef.model_validate({**kwargs, "sandbox": {"identifier_scope": "agent"}})
