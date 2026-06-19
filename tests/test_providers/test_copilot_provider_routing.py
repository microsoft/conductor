"""Tests for structured ``runtime.provider`` plumbing through ``CopilotProvider``.

Covers issue #136: the resolver (env-var fallbacks, activation gate, secret
unwrap) and the central ``_apply_provider_config`` plumbing into both the
main agent session and dialog turns.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from conductor.config.schema import (
    AzureProviderOptions,
    ProviderSettings,
    RuntimeConfig,
)
from conductor.providers.copilot import CopilotProvider


def _make_provider(**kwargs: Any) -> CopilotProvider:
    return CopilotProvider(**kwargs)


class TestResolveSdkProviderConfig:
    """Unit-tests for ``CopilotProvider._resolve_sdk_provider_config``."""

    def test_no_settings_returns_none(self) -> None:
        provider = _make_provider()
        assert provider._resolve_sdk_provider_config() is None

    def test_name_only_returns_none(self) -> None:
        """Default routing (no custom fields) must not forward a provider
        dict to the SDK — that would silently activate based on ambient
        OpenAI env vars."""
        provider = _make_provider(provider_settings=ProviderSettings(name="copilot"))
        assert provider._resolve_sdk_provider_config() is None

    def test_env_vars_alone_do_not_activate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ambient ``OPENAI_*`` env vars must not divert default Copilot traffic."""
        monkeypatch.setenv("OPENAI_BASE_URL", "http://env-host/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
        provider = _make_provider(provider_settings=ProviderSettings(name="copilot"))
        assert provider._resolve_sdk_provider_config() is None

    def test_full_yaml_config_passes_through(self) -> None:
        s = ProviderSettings.model_validate(
            {
                "name": "copilot",
                "type": "openai",
                "wire_api": "completions",
                "base_url": "http://localhost:11434/v1",
                "api_key": "sk-yaml",
            }
        )
        provider = _make_provider(provider_settings=s, model="ollama/llama3")
        cfg = provider._resolve_sdk_provider_config()
        assert cfg == {
            "type": "openai",
            "wire_api": "completions",
            "base_url": "http://localhost:11434/v1",
            "api_key": "sk-yaml",
        }

    def test_yaml_base_url_then_env_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """YAML opts in; ``COPILOT_PROVIDER_API_KEY`` fills missing
        ``api_key``. Ambient ``OPENAI_API_KEY`` is intentionally NOT
        used as a fallback (credential-leak risk)."""
        monkeypatch.setenv("COPILOT_PROVIDER_API_KEY", "sk-copilot-env")
        # Ambient OPENAI_API_KEY must NOT be consulted.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-should-not-leak")
        s = ProviderSettings(name="copilot", base_url="http://yaml/v1")
        provider = _make_provider(provider_settings=s, model="custom-model")
        cfg = provider._resolve_sdk_provider_config()
        assert cfg == {
            "type": "openai",  # defaulted because base_url is set
            "base_url": "http://yaml/v1",
            "api_key": "sk-copilot-env",
        }

    def test_openai_api_key_is_not_an_ambient_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: ambient ``OPENAI_API_KEY`` must not leak into the
        SDK provider config — only the namespaced
        ``COPILOT_PROVIDER_API_KEY`` is a fallback."""
        monkeypatch.delenv("COPILOT_PROVIDER_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-do-not-leak")
        s = ProviderSettings(name="copilot", base_url="http://ollama/v1")
        provider = _make_provider(provider_settings=s, model="m")
        cfg = provider._resolve_sdk_provider_config()
        assert cfg is not None
        assert "api_key" not in cfg

    def test_copilot_provider_env_var_takes_precedence_over_openai(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``COPILOT_PROVIDER_BASE_URL`` beats ``OPENAI_BASE_URL``."""
        monkeypatch.setenv("OPENAI_BASE_URL", "http://openai/v1")
        monkeypatch.setenv("COPILOT_PROVIDER_BASE_URL", "http://copilot-env/v1")
        monkeypatch.setenv("COPILOT_PROVIDER_API_KEY", "copilot-env-key")
        s = ProviderSettings(name="copilot", api_key="anchor-key", wire_api="completions")
        provider = _make_provider(provider_settings=s, model="m")
        cfg = provider._resolve_sdk_provider_config()
        assert cfg is not None
        assert cfg["base_url"] == "http://copilot-env/v1"
        # api_key in YAML wins over env fallback.
        assert cfg["api_key"] == "anchor-key"

    def test_openai_base_url_fallback_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``OPENAI_BASE_URL`` IS a recognized fallback (URLs are not
        secrets) — but only after YAML opts in via some other field."""
        monkeypatch.delenv("COPILOT_PROVIDER_BASE_URL", raising=False)
        monkeypatch.setenv("OPENAI_BASE_URL", "http://env-only/v1")
        s = ProviderSettings(name="copilot", api_key="anchor-key", type="openai")
        provider = _make_provider(provider_settings=s, model="m")
        cfg = provider._resolve_sdk_provider_config()
        assert cfg is not None
        assert cfg["base_url"] == "http://env-only/v1"

    def test_bearer_token_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COPILOT_PROVIDER_BEARER_TOKEN", "bearer-from-env")
        s = ProviderSettings(name="copilot", base_url="http://x/v1")
        provider = _make_provider(provider_settings=s, model="m")
        cfg = provider._resolve_sdk_provider_config()
        assert cfg is not None
        assert cfg["bearer_token"] == "bearer-from-env"

    def test_azure_options_nested(self) -> None:
        s = ProviderSettings(
            name="copilot",
            type="azure",
            base_url="https://x.openai.azure.com",
            api_key="azure-key",
            azure=AzureProviderOptions(api_version="2024-10-21"),
        )
        provider = _make_provider(provider_settings=s, model="gpt-4o")
        cfg = provider._resolve_sdk_provider_config()
        assert cfg == {
            "type": "azure",
            "base_url": "https://x.openai.azure.com",
            "api_key": "azure-key",
            "azure": {"api_version": "2024-10-21"},
        }

    def test_headers_passed_through(self) -> None:
        s = ProviderSettings(
            name="copilot",
            type="openai",
            base_url="http://x/v1",
            headers={"X-Custom": "yes", "Authorization": "Bearer foo"},
        )
        provider = _make_provider(provider_settings=s, model="m")
        cfg = provider._resolve_sdk_provider_config()
        assert cfg is not None
        assert cfg["headers"] == {"X-Custom": "yes", "Authorization": "Bearer foo"}

    def test_dual_credentials_in_yaml_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        s = ProviderSettings(
            name="copilot",
            base_url="http://x/v1",
            api_key="k",
            bearer_token="t",
        )
        provider = _make_provider(provider_settings=s, model="m")
        with caplog.at_level("WARNING", logger="conductor.providers.copilot"):
            cfg = provider._resolve_sdk_provider_config()
        assert cfg is not None
        assert cfg.get("api_key") == "k"
        assert cfg.get("bearer_token") == "t"
        assert any("bearer_token" in r.message for r in caplog.records), (
            "expected a warning about dual api_key+bearer_token"
        )

    def test_dual_credentials_yaml_plus_env_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Regression for silent-failures #5: the dual-credential warning
        must fire on YAML × env mixing (api_key in YAML,
        ``COPILOT_PROVIDER_BEARER_TOKEN`` in env), not just YAML-only.
        """
        monkeypatch.setenv("COPILOT_PROVIDER_BEARER_TOKEN", "bearer-from-env")
        s = ProviderSettings(name="copilot", base_url="http://x/v1", api_key="yaml-key")
        provider = _make_provider(provider_settings=s, model="m")
        with caplog.at_level("WARNING", logger="conductor.providers.copilot"):
            cfg = provider._resolve_sdk_provider_config()
        assert cfg is not None
        assert cfg["api_key"] == "yaml-key"
        assert cfg["bearer_token"] == "bearer-from-env"
        assert any("bearer_token" in r.message for r in caplog.records), (
            "expected a warning when api_key (YAML) and bearer_token (env) both resolve"
        )

    def test_raises_when_routing_resolves_to_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression for silent-failures #1: when has_custom_routing()
        is True but every resolved field is falsy (e.g. all expected env
        vars are unset), raise a clear error instead of silently
        dropping the SDK provider kwarg."""
        from conductor.exceptions import ProviderError

        # All COPILOT_PROVIDER_* and OPENAI_BASE_URL absent.
        for var in (
            "COPILOT_PROVIDER_BASE_URL",
            "OPENAI_BASE_URL",
            "COPILOT_PROVIDER_API_KEY",
            "COPILOT_PROVIDER_BEARER_TOKEN",
        ):
            monkeypatch.delenv(var, raising=False)

        # bearer_token from YAML is *intentionally* the only field set;
        # then bypass schema validation via model_construct to simulate a
        # caller (or future schema bug) that lets an empty SecretStr through.
        s = ProviderSettings.model_construct(name="copilot", bearer_token=SecretStr(""))
        provider = _make_provider(provider_settings=s, model="m")
        with pytest.raises(ProviderError, match="no usable fields"):
            provider._resolve_sdk_provider_config()

    def test_default_model_warning_on_custom_routing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        s = ProviderSettings(name="copilot", base_url="http://ollama/v1")
        with caplog.at_level("WARNING", logger="conductor.providers.copilot"):
            _make_provider(provider_settings=s)  # no model kwarg → warn
        msgs = [r.message.lower() for r in caplog.records]
        assert any("custom copilot provider routing" in m for m in msgs), msgs

    def test_no_default_model_warning_when_user_supplies_model(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Regression for silent-failures #4: the warning must be tied
        to *whether the caller supplied a model*, not to a magic-string
        comparison against the SDK fallback. A user who explicitly
        picks the same model name as the fallback must NOT see the
        warning."""
        s = ProviderSettings(name="copilot", base_url="http://ollama/v1")
        with caplog.at_level("WARNING", logger="conductor.providers.copilot"):
            _make_provider(provider_settings=s, model="gpt-4o")
        msgs = [r.message.lower() for r in caplog.records]
        assert not any("custom copilot provider routing" in m for m in msgs), (
            "explicit model should suppress the default-model warning even "
            "when it matches the SDK fallback"
        )


class TestApplyProviderConfig:
    """``_apply_provider_config`` mutates session kwargs in place."""

    def test_no_settings_leaves_kwargs_unchanged(self) -> None:
        provider = _make_provider()
        kwargs: dict[str, Any] = {"model": "gpt-4o"}
        provider._apply_provider_config(kwargs)
        assert "provider" not in kwargs

    def test_custom_routing_attaches_provider_dict(self) -> None:
        s = ProviderSettings(name="copilot", base_url="http://x/v1", api_key="k")
        provider = _make_provider(provider_settings=s, model="m")
        kwargs: dict[str, Any] = {"model": "m"}
        provider._apply_provider_config(kwargs)
        assert kwargs["provider"] == {
            "type": "openai",
            "base_url": "http://x/v1",
            "api_key": "k",
        }


class TestSessionKwargsPlumbing:
    """End-to-end: provider config reaches ``create_session`` in both
    main agent execution and dialog turns."""

    def _build_mocked_provider(
        self, captured: dict[str, Any], provider_settings: ProviderSettings | None
    ) -> CopilotProvider:
        provider = CopilotProvider(provider_settings=provider_settings, model="custom-model")
        provider._started = True

        session = AsyncMock()
        captured_callback: dict[str, Any] = {}

        def on_event(callback: Any) -> None:
            captured_callback["cb"] = callback

        session.on = on_event

        async def send(prompt: str) -> None:
            captured["sent_prompt"] = prompt
            cb = captured_callback["cb"]
            # Synthesize a minimal "assistant.message" + "session.idle" pair
            from types import SimpleNamespace

            def make_event(t: str, content: str = "") -> Any:
                ev = SimpleNamespace()
                ev.type = SimpleNamespace(value=t)
                ev.data = SimpleNamespace(message=content, content=content)
                return ev

            cb(make_event("assistant.message", "ok"))
            cb(make_event("session.idle"))

        session.send = send
        session.destroy = AsyncMock()

        async def create_session(**kwargs: Any) -> Any:
            captured["create_session_kwargs"] = kwargs
            return session

        client = AsyncMock()
        client.create_session = create_session
        # resume_session is never called when no resume id is set
        provider._client = client
        return provider

    @pytest.mark.asyncio
    async def test_dialog_turn_attaches_provider_config(self) -> None:
        s = ProviderSettings(
            name="copilot",
            type="openai",
            wire_api="completions",
            base_url="http://localhost:11434/v1",
            api_key="sk-dialog",
        )
        captured: dict[str, Any] = {}
        provider = self._build_mocked_provider(captured, s)
        await provider.execute_dialog_turn(
            system_prompt="be helpful",
            user_message="hi",
            history=[],
        )
        kwargs = captured["create_session_kwargs"]
        assert kwargs["provider"] == {
            "type": "openai",
            "wire_api": "completions",
            "base_url": "http://localhost:11434/v1",
            "api_key": "sk-dialog",
        }

    @pytest.mark.asyncio
    async def test_dialog_turn_no_provider_when_default(self) -> None:
        """Default routing (no structured settings) means no provider
        kwarg to ``create_session`` — preserves out-of-the-box SDK
        behavior."""
        captured: dict[str, Any] = {}
        provider = self._build_mocked_provider(captured, None)
        await provider.execute_dialog_turn(
            system_prompt="be helpful",
            user_message="hi",
            history=[],
        )
        kwargs = captured["create_session_kwargs"]
        assert "provider" not in kwargs

    @pytest.mark.asyncio
    async def test_execute_attaches_provider_config(self) -> None:
        """Parallel to ``test_dialog_turn_attaches_provider_config``:
        confirm that the main agent execution path also attaches the
        resolved ``ProviderConfig`` dict. A regression that removes
        the ``_apply_provider_config`` call from ``_execute_sdk_call``
        but keeps it in the dialog path would otherwise silently ship.
        """
        from conductor.config.schema import AgentDef

        s = ProviderSettings(
            name="copilot",
            type="openai",
            wire_api="completions",
            base_url="http://localhost:11434/v1",
            api_key="sk-execute",
        )
        captured: dict[str, Any] = {}
        provider = self._build_mocked_provider(captured, s)

        agent = AgentDef(name="solo", model="custom-model", prompt="hi")
        await provider.execute(agent, context={}, rendered_prompt="hi")

        kwargs = captured["create_session_kwargs"]
        assert kwargs["provider"] == {
            "type": "openai",
            "wire_api": "completions",
            "base_url": "http://localhost:11434/v1",
            "api_key": "sk-execute",
        }


class TestDescribeProviderRedaction:
    """``_describe_provider`` must never leak ``SecretStr`` values."""

    def test_secret_values_never_appear_in_output(self) -> None:
        """Regression: a refactor that swaps ``_describe_provider`` for
        ``str(provider)`` or inlines ``provider.api_key.get_secret_value()``
        must fail this test rather than silently leak credentials to
        verbose logs / event sinks."""
        from conductor.cli.run import _describe_provider

        secret_api_key = "sk-DO-NOT-LEAK-12345"
        secret_bearer = "bearer-DO-NOT-LEAK-67890"
        secret_header = "Bearer secret-header-token-abcdef"

        s = ProviderSettings(
            name="copilot",
            type="openai",
            base_url="http://localhost:11434/v1",
            api_key=secret_api_key,
            bearer_token=secret_bearer,
            headers={"Authorization": secret_header, "X-Trace": "1"},
        )
        rendered = _describe_provider(s)

        # Identifying metadata appears, redacted markers appear.
        assert "copilot" in rendered
        assert "type=openai" in rendered
        assert "base_url=http://localhost:11434/v1" in rendered
        assert "api_key=***" in rendered
        assert "bearer_token=***" in rendered

        # The secret values themselves never appear.
        assert secret_api_key not in rendered
        assert secret_bearer not in rendered
        # Header values are never rendered (only keys are listed).
        assert secret_header not in rendered

    def test_default_routing_renders_bare_name(self) -> None:
        from conductor.cli.run import _describe_provider

        assert _describe_provider(ProviderSettings(name="copilot")) == "copilot"


class TestRegistryForwardsSettings:
    """``ProviderRegistry`` forwards ``ProviderSettings`` only to the matching provider."""

    @pytest.mark.asyncio
    async def test_registry_forwards_settings_to_copilot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from conductor.config.schema import AgentDef, WorkflowConfig, WorkflowDef
        from conductor.providers.registry import ProviderRegistry

        captured: dict[str, Any] = {}

        async def fake_create_provider(**kwargs: Any) -> Any:
            captured.update(kwargs)
            mock = AsyncMock()
            mock.set_resume_session_ids = lambda *_a, **_k: None
            return mock

        monkeypatch.setattr("conductor.providers.registry.create_provider", fake_create_provider)

        runtime = RuntimeConfig.model_validate(
            {
                "provider": {
                    "name": "copilot",
                    "base_url": "http://localhost:11434/v1",
                    "type": "openai",
                }
            }
        )
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="a", runtime=runtime),
            agents=[AgentDef(name="a", prompt="p")],
            output={"r": "x"},
        )

        async with ProviderRegistry(config) as registry:
            await registry.get_provider(config.agents[0])

        assert captured["provider_type"] == "copilot"
        assert captured["provider_settings"] is runtime.provider
        assert captured["provider_settings"].base_url == "http://localhost:11434/v1"

    @pytest.mark.asyncio
    async def test_registry_isolates_copilot_settings_from_claude_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression for the inline guard in ``registry.py``: when the
        workflow default is structured Copilot routing but an agent
        overrides to ``claude``, the registry must pass
        ``provider_settings=None`` to the Claude factory call.
        Otherwise Copilot-shaped routing (``bearer_token`` / ``headers``
        / etc.) would silently bleed into the Claude provider.
        """
        from conductor.config.schema import AgentDef, WorkflowConfig, WorkflowDef
        from conductor.providers.registry import ProviderRegistry

        captured: list[dict[str, Any]] = []

        async def fake_create_provider(**kwargs: Any) -> Any:
            captured.append(dict(kwargs))
            mock = AsyncMock()
            mock.set_resume_session_ids = lambda *_a, **_k: None
            return mock

        monkeypatch.setattr("conductor.providers.registry.create_provider", fake_create_provider)

        runtime = RuntimeConfig.model_validate(
            {
                "provider": {
                    "name": "copilot",
                    "type": "openai",
                    "base_url": "http://localhost:11434/v1",
                    "api_key": "sk-copilot-only",
                }
            }
        )
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="copilot_agent", runtime=runtime),
            agents=[
                AgentDef(name="copilot_agent", prompt="p"),
                AgentDef(name="claude_agent", prompt="p", provider="claude"),
            ],
            output={"r": "x"},
        )

        async with ProviderRegistry(config) as registry:
            await registry.get_provider(config.agents[0])  # copilot
            await registry.get_provider(config.agents[1])  # claude override

        by_type = {call["provider_type"]: call for call in captured}
        assert by_type.keys() == {"copilot", "claude"}

        # Copilot call DOES receive the structured settings.
        assert by_type["copilot"]["provider_settings"] is runtime.provider

        # Claude call MUST NOT receive Copilot-shaped settings.
        assert by_type["claude"]["provider_settings"] is None
