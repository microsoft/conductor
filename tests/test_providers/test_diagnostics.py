"""Tests for the keyless diagnostics layer behind ``conductor doctor`` (#274).

These tests assert the gather layer never raises, honors the offline
contract (no provider instantiation unless ``check``/``list_models``), and
maps providers/credentials/registries into the report dataclasses correctly.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from conductor.providers import diagnostics as d

# ---------------------------------------------------------------------------
# Environment section
# ---------------------------------------------------------------------------


class TestGatherEnv:
    """`gather_env` reports version/host and update availability."""

    def test_basic_fields(self) -> None:
        env = d.gather_env()
        assert env.conductor_version
        assert env.python_version
        assert env.platform

    def test_update_check_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CONDUCTOR_NO_UPDATE_CHECK", "1")
        env = d.gather_env()
        assert env.update_checked is False
        assert env.update_available is None
        assert env.latest_version is None

    def test_update_available_from_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CONDUCTOR_NO_UPDATE_CHECK", raising=False)
        monkeypatch.setattr(
            "conductor.cli.update.read_cache",
            lambda: {"version": "999.0.0"},
        )
        env = d.gather_env()
        assert env.update_checked is True
        assert env.update_available is True
        assert env.latest_version == "999.0.0"

    def test_update_check_network_failure_is_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CONDUCTOR_NO_UPDATE_CHECK", raising=False)
        monkeypatch.setattr("conductor.cli.update.read_cache", lambda: None)
        monkeypatch.setattr("conductor.cli.update.fetch_latest_version", lambda: None)
        env = d.gather_env()
        assert env.update_checked is True
        assert env.update_available is None

    def test_cold_cache_fetch_and_up_to_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No cache -> fetch, persist, and report "up to date" for an older remote.
        monkeypatch.delenv("CONDUCTOR_NO_UPDATE_CHECK", raising=False)
        monkeypatch.setattr("conductor.cli.update.read_cache", lambda: None)
        monkeypatch.setattr(
            "conductor.cli.update.fetch_latest_version",
            lambda: ("0.0.1", "v0.0.1", "url"),
        )
        wrote: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            "conductor.cli.update.write_cache",
            lambda v, t, u: wrote.append((v, t, u)),
        )
        env = d.gather_env()
        assert env.update_checked is True
        assert env.update_available is False  # remote 0.0.1 is older than current
        assert env.latest_version == "0.0.1"
        assert wrote == [("0.0.1", "v0.0.1", "url")]

    def test_cache_write_failure_does_not_discard_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A non-writable HOME (common in CI) must NOT collapse a successful
        # fetch into a misleading "offline" result.
        monkeypatch.delenv("CONDUCTOR_NO_UPDATE_CHECK", raising=False)
        monkeypatch.setattr("conductor.cli.update.read_cache", lambda: None)
        monkeypatch.setattr(
            "conductor.cli.update.fetch_latest_version",
            lambda: ("999.0.0", "v999.0.0", "url"),
        )

        def _boom(*_args: Any) -> None:
            raise OSError("read-only HOME")

        monkeypatch.setattr("conductor.cli.update.write_cache", _boom)
        env = d.gather_env()
        assert env.update_checked is True
        assert env.update_available is True  # fetch preserved despite write failure
        assert env.latest_version == "999.0.0"


# ---------------------------------------------------------------------------
# Registries section
# ---------------------------------------------------------------------------


class TestGatherRegistries:
    """`gather_registries` reflects the registries config, never raises."""

    def test_reads_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = SimpleNamespace(
            default="team",
            registries={
                "team": SimpleNamespace(type="github", source="org/repo"),
                "local": SimpleNamespace(type="path", source="/tmp/wf"),
            },
        )
        monkeypatch.setattr("conductor.registry.config.load_config", lambda: fake)
        result = d.gather_registries()
        assert result.default == "team"
        assert result.error is None
        assert {r.name for r in result.registries} == {"team", "local"}
        team = next(r for r in result.registries if r.name == "team")
        assert team.is_default is True
        assert team.type == "github"
        local = next(r for r in result.registries if r.name == "local")
        assert local.is_default is False

    def test_load_failure_is_surfaced_not_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A corrupt config must be reported via ``error`` — NOT collapsed into
        # an empty result that reads as "no registries configured".
        def _raise() -> Any:
            raise RuntimeError("malformed TOML at line 3")

        monkeypatch.setattr("conductor.registry.config.load_config", _raise)
        result = d.gather_registries()
        assert result.default is None
        assert result.registries == []
        assert result.error == "malformed TOML at line 3"
        assert result.to_dict()["error"] == "malformed TOML at line 3"


# ---------------------------------------------------------------------------
# Provider section — offline
# ---------------------------------------------------------------------------


class TestGatherProviderOffline:
    """Offline provider probes populate static fields and never connect."""

    async def test_installed_and_tier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("conductor.providers.copilot.COPILOT_SDK_AVAILABLE", True)
        diag = await d.gather_provider("copilot")
        assert diag.installed is True
        assert diag.implemented is True
        assert diag.tier == "stable"
        assert diag.checked is False
        assert diag.connection_ok is None

    async def test_not_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("conductor.providers.hermes.HERMES_SDK_AVAILABLE", False)
        diag = await d.gather_provider("hermes")
        assert diag.installed is False

    async def test_openai_agents_not_implemented(self) -> None:
        diag = await d.gather_provider("openai-agents")
        assert diag.implemented is False
        assert diag.installed is False
        assert diag.note == "not yet implemented"

    async def test_credential_presence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        diag = await d.gather_provider("claude")
        by_name = {c.name: c.present for c in diag.credential_env_vars}
        assert by_name == {"ANTHROPIC_API_KEY": True, "ANTHROPIC_AUTH_TOKEN": False}

    async def test_copilot_runtime_token_presence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COPILOT_PROVIDER_RUNTIME_TOKEN", "runtime-secret")
        diag = await d.gather_provider("copilot")
        by_name = {c.name: c.present for c in diag.credential_env_vars}
        assert by_name["COPILOT_PROVIDER_RUNTIME_TOKEN"] is True

    async def test_copilot_credentials_optional_with_note(self) -> None:
        # copilot authenticates via the GitHub/Copilot CLI login on disk, so
        # its env vars are optional overrides — flagged for a neutral render
        # plus an explanatory note (issue #319).
        diag = await d.gather_provider("copilot")
        assert diag.credentials_optional is True
        assert diag.note is not None
        assert "optional" in diag.note.lower()

    async def test_claude_agent_sdk_credentials_optional_with_note(self) -> None:
        # claude-agent-sdk delegates to the `claude` CLI, which authenticates
        # via `claude login`, so ANTHROPIC_API_KEY is likewise optional.
        diag = await d.gather_provider("claude-agent-sdk")
        assert diag.credentials_optional is True
        assert diag.note is not None
        assert "optional" in diag.note.lower()

    async def test_claude_credentials_required_no_note(self) -> None:
        # The direct Anthropic API has no on-disk login — its key is required.
        diag = await d.gather_provider("claude")
        assert diag.credentials_optional is False
        assert diag.note is None

    async def test_credentials_optional_serialized_in_to_dict(self) -> None:
        diag = await d.gather_provider("copilot")
        assert diag.to_dict()["credentials_optional"] is True

    async def test_credential_values_never_stored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-super-secret")
        diag = await d.gather_provider("claude")
        # Only presence booleans are recorded — never the secret value.
        for cred in diag.credential_env_vars:
            assert isinstance(cred.present, bool)
            assert "sk-super-secret" not in repr(cred)

    async def test_offline_never_instantiates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        create = AsyncMock()
        monkeypatch.setattr("conductor.providers.factory.create_provider", create)
        await d.gather_provider("copilot", check=False)
        create.assert_not_called()

    async def test_tier_failure_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(_name: str) -> Any:
            raise RuntimeError("capabilities boom")

        monkeypatch.setattr("conductor.providers.diagnostics.get_capabilities", _raise)
        diag = await d.gather_provider("copilot")
        assert diag.tier is None  # degrades, does not raise


# ---------------------------------------------------------------------------
# Provider section — with --check / --models
# ---------------------------------------------------------------------------


def _fake_provider(
    *,
    ok: bool = True,
    validate_error: Exception | None = None,
    models: list[str] | None = None,
    models_error: Exception | None = None,
    capabilities: dict[str, Any] | None = None,
) -> Any:
    """Build an AsyncMock provider for check/model probes.

    ``capabilities`` maps model id -> a ``ModelCapabilityInfo``-like object
    (or ``None``) returned by ``get_model_capabilities`` for that id. Ids not
    present in the mapping (or when ``capabilities`` is omitted) resolve to
    ``None``, matching the base-hook default.
    """
    provider = AsyncMock()
    if validate_error is not None:
        provider.validate_connection.side_effect = validate_error
    else:
        provider.validate_connection.return_value = ok
    if models_error is not None:
        provider.list_models.side_effect = models_error
    else:
        provider.list_models.return_value = models
    provider.close.return_value = None
    caps_map = capabilities or {}
    provider.get_model_capabilities.side_effect = lambda model_id: caps_map.get(model_id)
    return provider


class TestGatherProviderCheck:
    """`check` / `list_models` probe connections; failures never raise."""

    async def test_connection_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("conductor.providers.copilot.COPILOT_SDK_AVAILABLE", True)
        provider = _fake_provider(ok=True)
        monkeypatch.setattr(
            "conductor.providers.factory.create_provider",
            AsyncMock(return_value=provider),
        )
        diag = await d.gather_provider("copilot", check=True)
        assert diag.checked is True
        assert diag.connection_ok is True
        provider.close.assert_awaited_once()

    async def test_not_installed_skips_construction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", False)
        create = AsyncMock()
        monkeypatch.setattr("conductor.providers.factory.create_provider", create)
        diag = await d.gather_provider("claude", check=True)
        assert diag.checked is True
        assert diag.connection_ok is False
        assert diag.connection_error == "SDK not installed"
        create.assert_not_called()

    async def test_construction_error_captured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
        monkeypatch.setattr(
            "conductor.providers.factory.create_provider",
            AsyncMock(side_effect=RuntimeError("no api key")),
        )
        diag = await d.gather_provider("claude", check=True)
        assert diag.connection_ok is False
        assert "no api key" in (diag.connection_error or "")

    async def test_validate_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
        provider = _fake_provider(ok=False)
        monkeypatch.setattr(
            "conductor.providers.factory.create_provider",
            AsyncMock(return_value=provider),
        )
        diag = await d.gather_provider("claude", check=True)
        assert diag.connection_ok is False

    async def test_validate_raises_is_caught(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("conductor.providers.copilot.COPILOT_SDK_AVAILABLE", True)
        provider = _fake_provider(validate_error=RuntimeError("kaboom"))
        monkeypatch.setattr(
            "conductor.providers.factory.create_provider",
            AsyncMock(return_value=provider),
        )
        diag = await d.gather_provider("copilot", check=True)
        assert diag.connection_ok is False
        assert "kaboom" in (diag.connection_error or "")
        provider.close.assert_awaited_once()

    async def test_models_implies_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("conductor.providers.copilot.COPILOT_SDK_AVAILABLE", True)
        provider = _fake_provider(ok=True, models=["gpt-5", "gpt-4"])
        monkeypatch.setattr(
            "conductor.providers.factory.create_provider",
            AsyncMock(return_value=provider),
        )
        diag = await d.gather_provider("copilot", list_models=True)
        assert diag.checked is True
        assert diag.models is not None
        assert [m.id for m in diag.models] == ["gpt-5", "gpt-4"]

    async def test_models_none_is_na(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("conductor.providers.copilot.COPILOT_SDK_AVAILABLE", True)
        provider = _fake_provider(ok=True, models=None)
        monkeypatch.setattr(
            "conductor.providers.factory.create_provider",
            AsyncMock(return_value=provider),
        )
        diag = await d.gather_provider("copilot", list_models=True)
        assert diag.models is None

    async def test_models_error_captured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("conductor.providers.copilot.COPILOT_SDK_AVAILABLE", True)
        provider = _fake_provider(ok=True, models_error=RuntimeError("list boom"))
        monkeypatch.setattr(
            "conductor.providers.factory.create_provider",
            AsyncMock(return_value=provider),
        )
        diag = await d.gather_provider("copilot", list_models=True)
        assert diag.models is None
        assert "list boom" in (diag.models_error or "")

    async def test_model_capabilities_populated_per_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each listed model's capabilities are resolved via get_model_capabilities."""
        from conductor.providers.base import ModelCapabilityInfo

        monkeypatch.setattr("conductor.providers.copilot.COPILOT_SDK_AVAILABLE", True)
        provider = _fake_provider(
            ok=True,
            models=["gpt-5", "gpt-4"],
            capabilities={
                "gpt-5": ModelCapabilityInfo(
                    supported_reasoning_efforts=["low", "medium"],
                    default_reasoning_effort="low",
                    max_prompt_tokens=128_000,
                    max_output_tokens=64_000,
                    max_context_window_tokens=192_000,
                ),
                # "gpt-4" intentionally omitted -> None (unknown capabilities).
            },
        )
        monkeypatch.setattr(
            "conductor.providers.factory.create_provider",
            AsyncMock(return_value=provider),
        )
        diag = await d.gather_provider("copilot", list_models=True)
        assert diag.models is not None
        by_id = {m.id: m for m in diag.models}
        assert by_id["gpt-5"].supported_reasoning_efforts == ["low", "medium"]
        assert by_id["gpt-5"].default_reasoning_effort == "low"
        assert by_id["gpt-5"].max_prompt_tokens == 128_000
        assert by_id["gpt-5"].max_output_tokens == 64_000
        assert by_id["gpt-5"].max_context_window_tokens == 192_000
        # Unknown capabilities degrade to id-only, not a dropped entry.
        assert by_id["gpt-4"].supported_reasoning_efforts is None
        assert by_id["gpt-4"].max_prompt_tokens is None

    async def test_model_capabilities_failure_degrades_to_id_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A per-model get_model_capabilities exception must not drop the
        model or fail the whole --models probe."""
        monkeypatch.setattr("conductor.providers.copilot.COPILOT_SDK_AVAILABLE", True)
        provider = _fake_provider(ok=True, models=["gpt-5", "gpt-4"])
        provider.get_model_capabilities.side_effect = RuntimeError("capabilities boom")
        monkeypatch.setattr(
            "conductor.providers.factory.create_provider",
            AsyncMock(return_value=provider),
        )
        diag = await d.gather_provider("copilot", list_models=True)
        assert diag.models is not None
        assert [m.id for m in diag.models] == ["gpt-5", "gpt-4"]
        assert all(m.supported_reasoning_efforts is None for m in diag.models)

    async def test_mixed_success_and_failure_isolates_the_failing_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure on one model (not first/only) must not discard results
        already built for other models in the same list — regression guard
        for a bug where an unguarded attribute read on a malformed
        capabilities object escaped the per-model try/except and wiped the
        whole batch."""
        from conductor.providers.base import ModelCapabilityInfo

        monkeypatch.setattr("conductor.providers.copilot.COPILOT_SDK_AVAILABLE", True)
        provider = _fake_provider(ok=True, models=["gpt-5", "gpt-4", "gpt-3"])

        good_caps = ModelCapabilityInfo(
            supported_reasoning_efforts=["low", "medium"],
            max_prompt_tokens=128_000,
        )

        def _capabilities_for(model_id: str) -> ModelCapabilityInfo | None:
            if model_id == "gpt-4":
                raise RuntimeError("capabilities boom for gpt-4 only")
            return good_caps

        provider.get_model_capabilities.side_effect = _capabilities_for
        monkeypatch.setattr(
            "conductor.providers.factory.create_provider",
            AsyncMock(return_value=provider),
        )
        diag = await d.gather_provider("copilot", list_models=True)
        assert diag.models is not None
        by_id = {m.id: m for m in diag.models}
        # Model before AND after the failing one must retain full data.
        assert by_id["gpt-5"].supported_reasoning_efforts == ["low", "medium"]
        assert by_id["gpt-5"].max_prompt_tokens == 128_000
        assert by_id["gpt-3"].supported_reasoning_efforts == ["low", "medium"]
        assert by_id["gpt-3"].max_prompt_tokens == 128_000
        # The failing model degrades to id-only, isolated from its neighbors.
        assert by_id["gpt-4"].supported_reasoning_efforts is None
        assert by_id["gpt-4"].max_prompt_tokens is None

    async def test_malformed_capabilities_object_degrades_to_id_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A get_model_capabilities call that *succeeds* but returns an
        object missing the expected attributes must degrade that model to
        id-only rather than raising out of the per-model try/except and
        discarding every other model already built in this list."""
        from types import SimpleNamespace

        monkeypatch.setattr("conductor.providers.copilot.COPILOT_SDK_AVAILABLE", True)
        provider = _fake_provider(ok=True, models=["gpt-5", "gpt-4"])
        malformed = SimpleNamespace()  # no capability attributes at all

        def _capabilities_for(model_id: str) -> Any:
            if model_id == "gpt-4":
                return malformed
            return None

        provider.get_model_capabilities.side_effect = _capabilities_for
        monkeypatch.setattr(
            "conductor.providers.factory.create_provider",
            AsyncMock(return_value=provider),
        )
        diag = await d.gather_provider("copilot", list_models=True)
        assert diag.models is not None
        assert [m.id for m in diag.models] == ["gpt-5", "gpt-4"]
        by_id = {m.id: m for m in diag.models}
        assert by_id["gpt-4"].supported_reasoning_efforts is None


# ---------------------------------------------------------------------------
# Top-level gather
# ---------------------------------------------------------------------------


class TestGather:
    """`gather` orchestrates section selection and provider scoping."""

    async def test_all_sections(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CONDUCTOR_NO_UPDATE_CHECK", "1")
        report = await d.gather()
        assert report.env is not None
        assert report.providers is not None
        assert report.registries is not None
        names = {p.name for p in report.providers}
        assert {"copilot", "claude", "openai-agents"} <= names

    async def test_single_section(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CONDUCTOR_NO_UPDATE_CHECK", "1")
        report = await d.gather(sections=("registries",))
        assert report.env is None
        assert report.providers is None
        assert report.registries is not None

    async def test_provider_scoping(self) -> None:
        report = await d.gather(sections=("providers",), provider="claude")
        assert report.providers is not None
        assert [p.name for p in report.providers] == ["claude"]

    async def test_report_to_dict_omits_missing_sections(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONDUCTOR_NO_UPDATE_CHECK", "1")
        report = await d.gather(sections=("env",))
        as_dict = report.to_dict()
        assert set(as_dict) == {"env"}
