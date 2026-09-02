"""Tests for the OpenAI provider.

These tests verify construction, configuration forwarding, and the
execute()/execute_dialog_turn() surfaces without making network calls.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import openai
import pytest
from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from conductor.config.schema import AgentDef, OutputField, ProviderSettings
from conductor.exceptions import ValidationError
from conductor.providers.factory import create_provider
from conductor.providers.openai import OpenAIProvider


@pytest.fixture
def provider() -> OpenAIProvider:
    """Return a fresh OpenAIProvider instance using a dummy API key."""
    return OpenAIProvider(api_key="test-key")


@pytest.fixture
def no_mcp_manager(provider: OpenAIProvider) -> Any:
    """Disable MCP manager resolution so execute() does not spawn tools."""
    with patch.object(provider, "_get_mcp_manager_for_cwd", return_value=None) as mock:
        yield mock


def _build_text_agent(text: str) -> Agent[Any, str]:
    """Build a Pydantic AI text agent backed by TestModel."""
    return Agent(model=TestModel(custom_output_text=text), output_type=str)


class TestProviderConstruction:
    """Tests for OpenAIProvider construction and validation."""

    def test_default_model_is_gpt_5_mini(self) -> None:
        """Requirement: omitted model and cap use the OpenAI provider defaults."""
        p = OpenAIProvider(api_key="test-key")
        assert p._default_model == "gpt-5-mini"
        assert p._default_max_tokens == 16_384
        assert p._max_tokens_user_configured is False

    def test_explicit_max_tokens_records_user_provenance(self) -> None:
        """Requirement: an explicit cap remains distinguishable from the provider default."""
        p = OpenAIProvider(api_key="test-key", max_tokens=1024)
        assert p._default_max_tokens == 1024
        assert p._max_tokens_user_configured is True

    def test_explicit_model_is_used(self) -> None:
        """An explicitly passed model overrides the default."""
        p = OpenAIProvider(api_key="test-key", model="gpt-5")
        assert p._default_model == "gpt-5"

    def test_missing_api_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Construction without an API key or env var raises ValidationError."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
            OpenAIProvider()

    def test_base_url_env_fallback_used_when_yaml_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Requirement: OPENAI_BASE_URL environment variable is used as a fallback for base_url
        when not explicitly provided in the YAML config, and explicit parameters still win over
        the environment variable.
        """
        monkeypatch.setenv("OPENAI_BASE_URL", "http://env-fallback:1234/v1")

        p = OpenAIProvider(api_key="test-key")
        assert p._base_url == "http://env-fallback:1234/v1"
        assert p._client is not None
        assert str(p._client.base_url) == "http://env-fallback:1234/v1/"

        p_explicit = OpenAIProvider(api_key="test-key", base_url="http://explicit-url:5678/v1")
        assert p_explicit._base_url == "http://explicit-url:5678/v1"
        assert p_explicit._client is not None
        assert str(p_explicit._client.base_url) == "http://explicit-url:5678/v1/"

    def test_custom_base_url_requires_explicit_api_key_with_ambient_key_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Requirement: ambient OPENAI_API_KEY is never forwarded to a custom endpoint.

        When base_url is set (even via env) and no explicit api_key is passed, construction
        must raise ValidationError even if OPENAI_API_KEY is present.
        """
        monkeypatch.setenv("OPENAI_API_KEY", "sk-ambient")
        monkeypatch.setenv("OPENAI_BASE_URL", "http://custom:1234/v1")

        with pytest.raises(ValidationError, match="custom base_url requires an explicit api_key"):
            OpenAIProvider(base_url="http://custom:1234/v1")

    def test_custom_base_url_with_explicit_api_key_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Requirement: custom base_url is allowed when api_key is passed explicitly."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        p = OpenAIProvider(api_key="sk-explicit", base_url="http://custom:1234/v1")
        assert p._api_key == "sk-explicit"
        assert p._base_url == "http://custom:1234/v1"
        assert p._client is not None

    def test_api_key_stays_none_when_only_ambient_key_is_used(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Requirement: the builder guard stays reachable when only an ambient key is used.

        With no custom base_url and OPENAI_API_KEY set, construction must succeed and
        self._api_key must remain None so the builder can apply its own env fallback.
        """
        monkeypatch.setenv("OPENAI_API_KEY", "sk-ambient")
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        p = OpenAIProvider()
        assert p._api_key is None
        assert p._client is not None

    def test_temperature_validation_accepts_range(self) -> None:
        """OpenAI accepts temperatures up to 2.0."""
        p = OpenAIProvider(api_key="test-key", temperature=2.0)
        assert p._default_temperature == 2.0

    def test_temperature_validation_rejects_out_of_range(self) -> None:
        """Temperatures outside 0..2 are rejected at construction time."""
        with pytest.raises(ValidationError, match="between 0.0 and 2.0"):
            OpenAIProvider(api_key="test-key", temperature=2.5)

    def test_capabilities_are_stable(self) -> None:
        """Requirement: CAPABILITIES reflects the reviewed OpenAI contract.

        agent_reasoning_events is False because Chat Completions does not surface
        reasoning content from api.openai.com, and reasoning_effort omits xhigh
        until GPT-5.1-Codex-Max support is verified for arbitrary endpoints.
        """
        caps = OpenAIProvider.CAPABILITIES
        assert caps.tier == "stable"
        assert caps.mcp_tools is True
        assert caps.workflow_tools_passthrough is True
        assert caps.streaming_events is True
        assert caps.agent_reasoning_events is False
        assert caps.reasoning_effort == ("low", "medium", "high")
        assert caps.structured_output == "native"
        assert caps.interrupt is True
        assert caps.max_session_seconds is True
        assert caps.checkpoint_resume is False
        assert caps.usage_tracking is True
        assert caps.concurrent_safe is True
        assert caps.working_dir is True
        assert caps.skills is True
        assert caps.plugins is False

    def test_supports_native_skills_is_false(self) -> None:
        """OpenAI relies on eager skill injection by AgentExecutor."""
        p = OpenAIProvider(api_key="test-key")
        assert p.supports_native_skills is False


class TestFactoryIntegration:
    """Tests that the factory wires the openai provider correctly."""

    @pytest.mark.asyncio
    async def test_factory_creates_openai_provider(self) -> None:
        """create_provider('openai') returns an OpenAIProvider."""
        settings = ProviderSettings(name="openai", api_key=SecretStr("sk-test"))
        p = await create_provider("openai", validate=False, provider_settings=settings)
        assert isinstance(p, OpenAIProvider)
        assert p._api_key == "sk-test"
        await p.close()

    @pytest.mark.asyncio
    async def test_factory_forwards_base_url(self) -> None:
        """YAML base_url for name='openai' reaches the provider."""
        settings = ProviderSettings(
            name="openai",
            api_key=SecretStr("sk-test"),
            base_url="http://localhost:1234/v1",
        )
        p = await create_provider("openai", validate=False, provider_settings=settings)
        assert isinstance(p, OpenAIProvider)
        assert p._base_url == "http://localhost:1234/v1"
        await p.close()


class TestExecute:
    """Tests for the execute() path."""

    async def test_execute_forwards_backend_openai(
        self, provider: OpenAIProvider, no_mcp_manager: Any
    ) -> None:
        """execute() calls build_agent with backend='openai' and http_client=None."""
        agent = AgentDef(name="greeter", model="test", prompt="say hi")
        captured_kwargs: dict[str, Any] = {}

        def spy_build_agent(*args: Any, **kwargs: Any) -> Agent[Any, Any]:
            captured_kwargs.update(kwargs)
            return _build_text_agent("hello")

        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            side_effect=spy_build_agent,
        ):
            output = await provider.execute(agent, {}, "say hi")

        assert output.content == {"result": "hello"}
        assert captured_kwargs.get("backend") == "openai"
        assert captured_kwargs.get("http_client") is None
        assert captured_kwargs.get("api_key") == "test-key"

    async def test_execute_forwards_temperature_and_max_tokens(self, no_mcp_manager: Any) -> None:
        """Runtime temperature/max_tokens are passed to the agent builder."""
        provider = OpenAIProvider(api_key="test-key", temperature=0.5, max_tokens=1024)
        agent = AgentDef(name="greeter", model="test", prompt="say hi")
        captured_kwargs: dict[str, Any] = {}

        def spy_build_agent(*args: Any, **kwargs: Any) -> Agent[Any, Any]:
            captured_kwargs.update(kwargs)
            return _build_text_agent("hello")

        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            side_effect=spy_build_agent,
        ):
            await provider.execute(agent, {}, "say hi")

        assert captured_kwargs.get("default_temperature") == 0.5
        assert captured_kwargs.get("default_max_tokens") == 1024

    async def test_execute_returns_structured_output(
        self, provider: OpenAIProvider, no_mcp_manager: Any
    ) -> None:
        """execute() returns validated structured output from a Pydantic model."""
        from pydantic import BaseModel

        class AnswerModel(BaseModel):
            answer: str

        agent = AgentDef(
            name="greeter",
            model="test",
            prompt="say hi",
            output={"answer": OutputField(type="string")},
        )

        structured_agent = Agent(
            model=TestModel(custom_output_args={"answer": "hello"}),
            output_type=AnswerModel,
        )

        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=structured_agent,
        ):
            output = await provider.execute(agent, {}, "say hi")

        assert output.content == {"answer": "hello"}

    async def test_execute_rejects_max_reasoning_effort_at_runtime(
        self, provider: OpenAIProvider, no_mcp_manager: Any
    ) -> None:
        """Requirement: reasoning.effort='max' raises a ValidationError at runtime.

        Verify that an agent with reasoning.effort set to 'max' is rejected at runtime.
        """
        # Requirement: Verify 'max' effort raises ValidationError at runtime.
        from conductor.config.schema import ReasoningConfig

        agent = AgentDef(
            name="test_agent",
            model="gpt-5",
            prompt="hi",
            reasoning=ReasoningConfig(effort="max"),
        )
        with pytest.raises(ValidationError, match="resolves to reasoning.effort='max'"):
            await provider.execute(agent, {}, "say hi")

    async def test_execute_accepts_supported_reasoning_effort(
        self, provider: OpenAIProvider, no_mcp_manager: Any
    ) -> None:
        """Requirement: reasoning.effort='high' is accepted at runtime.

        Verify that a supported effort level is allowed and passed down correctly.
        """
        # Requirement: Verify supported reasoning effort levels are accepted.
        from conductor.config.schema import ReasoningConfig

        agent = AgentDef(
            name="test_agent",
            model="gpt-5-mini",
            prompt="hi",
            reasoning=ReasoningConfig(effort="high"),
        )
        captured_kwargs: dict[str, Any] = {}

        def spy_build_agent(*args: Any, **kwargs: Any) -> Agent[Any, Any]:
            captured_kwargs.update(kwargs)
            return _build_text_agent("hello")

        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            side_effect=spy_build_agent,
        ):
            await provider.execute(agent, {}, "say hi")

        assert agent.reasoning is not None
        assert agent.reasoning.effort == "high"

    async def test_execute_rejects_xhigh_reasoning_effort(
        self, provider: OpenAIProvider, no_mcp_manager: Any
    ) -> None:
        """Requirement: reasoning.effort='xhigh' is rejected at the provider level.

        xhigh is no longer in the OpenAI provider's supported reasoning_effort tuple.
        """
        # Requirement: Verify xhigh is rejected by CAPABILITIES membership.
        from conductor.config.schema import ReasoningConfig

        agent = AgentDef(
            name="test_agent",
            model="gpt-5-mini",
            prompt="hi",
            reasoning=ReasoningConfig(effort="xhigh"),
        )
        with pytest.raises(ValidationError, match="resolves to reasoning.effort='xhigh'"):
            await provider.execute(agent, {}, "say hi")


class TestExecuteDialogTurn:
    """Tests for execute_dialog_turn()."""

    async def test_dialog_turn_keeps_4096_output_cap(self, provider: OpenAIProvider) -> None:
        """Requirement: dialog turns retain their dedicated 4096-token output cap."""

        async def fake_run(*args: Any, **kwargs: Any) -> Any:
            class FakeResult:
                output = "dialog reply"

            return FakeResult()

        with (
            patch("conductor.providers._pydantic_ai.agent_builder._resolve_openai_model"),
            patch("pydantic_ai.Agent") as mock_agent_cls,
        ):
            mock_agent_cls.return_value.run = fake_run
            await provider.execute_dialog_turn("system prompt", "user message")

        model_settings = mock_agent_cls.call_args.kwargs["model_settings"]
        assert model_settings["max_tokens"] == 4096

    async def test_dialog_turn_returns_text(self, provider: OpenAIProvider) -> None:
        """execute_dialog_turn() returns the Pydantic AI text response."""

        async def fake_run(*args: Any, **kwargs: Any) -> Any:
            class FakeResult:
                output = "dialog reply"

            return FakeResult()

        with (
            patch(
                "conductor.providers._pydantic_ai.agent_builder._resolve_openai_model"
            ) as mock_resolve_model,
            patch("pydantic_ai.Agent") as mock_agent_cls,
        ):
            mock_agent = mock_agent_cls.return_value
            mock_agent.run = fake_run
            result = await provider.execute_dialog_turn(
                "system prompt",
                "user message",
                history=[{"role": "user", "content": "previous"}],
                model="gpt-test",
            )

        assert result == "dialog reply"
        kwargs = mock_resolve_model.call_args.kwargs
        assert kwargs.get("api_key") == "test-key"
        assert kwargs.get("timeout") == 600.0

    async def test_dialog_turn_accepts_reasoning_on_reasoning_model(
        self, provider: OpenAIProvider
    ) -> None:
        """Requirement: a supported effort on a reasoning-capable model succeeds."""
        provider = OpenAIProvider(api_key="test-key", default_reasoning_effort="high")

        async def fake_run(*args: Any, **kwargs: Any) -> Any:
            class FakeResult:
                output = "dialog reply"

            return FakeResult()

        with (
            patch(
                "conductor.providers._pydantic_ai.agent_builder._resolve_openai_model"
            ) as mock_resolve_model,
            patch(
                "conductor.providers._pydantic_ai.agent_builder._openai_model_supports_reasoning",
                return_value=True,
            ),
            patch("pydantic_ai.Agent") as mock_agent_cls,
        ):
            mock_agent = mock_agent_cls.return_value
            mock_agent.run = fake_run
            result = await provider.execute_dialog_turn(
                "system prompt",
                "user message",
                history=[{"role": "user", "content": "previous"}],
                model="gpt-5-mini",
            )

        assert result == "dialog reply"
        kwargs = mock_resolve_model.call_args.kwargs
        assert kwargs.get("api_key") == "test-key"
        assert kwargs.get("timeout") == 600.0

    async def test_dialog_turn_rejects_reasoning_on_non_reasoning_model(
        self, provider: OpenAIProvider
    ) -> None:
        """Requirement: a supported effort on a non-reasoning model is rejected.

        Per-model reasoning support is verified via the shared helper; a False result
        must raise ValidationError before the request is sent.
        """
        provider = OpenAIProvider(api_key="test-key", default_reasoning_effort="high")

        with (
            patch(
                "conductor.providers._pydantic_ai.agent_builder._openai_model_supports_reasoning",
                return_value=False,
            ),
            pytest.raises(ValidationError, match="does not support reasoning.effort"),
        ):
            await provider.execute_dialog_turn("system prompt", "user message", model="gpt-4o")

    async def test_dialog_turn_rejects_unsupported_reasoning_effort(self) -> None:
        """Requirement: execute_dialog_turn() rejects unsupported reasoning effort.

        Verify that a ValidationError is raised when default_reasoning_effort is set to
        an unsupported value like 'max'.
        """
        # Requirement: Rejects unsupported reasoning efforts during dialog turns.
        provider = OpenAIProvider(api_key="test-key", default_reasoning_effort="max")
        with pytest.raises(
            ValidationError, match="Default reasoning effort 'max' is not supported"
        ):
            await provider.execute_dialog_turn("system prompt", "user message")


class TestConnectionHelpers:
    """Tests for validate_connection/list_models/get_model_capabilities."""

    @pytest.mark.asyncio
    async def test_validate_connection_returns_true_when_list_succeeds(
        self, provider: OpenAIProvider, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Requirement: validate_connection() succeeds, logs models, and clears note."""
        from unittest.mock import AsyncMock

        mock_client = MagicMock()
        mock_client.models.list = AsyncMock(
            return_value=MagicMock(data=[MagicMock(id="gpt-5-mini")])
        )
        provider._client = mock_client  # type: ignore[assignment]
        with caplog.at_level("INFO"):
            assert await provider.validate_connection() is True
        assert provider._connection_probe_note is None
        assert "Available OpenAI models: gpt-5-mini" in caplog.text

    @pytest.mark.asyncio
    async def test_validate_connection_warns_when_default_model_missing(
        self, provider: OpenAIProvider, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Requirement: a default model absent from the listing triggers a warning."""
        from unittest.mock import AsyncMock

        mock_client = MagicMock()
        mock_client.models.list = AsyncMock(return_value=MagicMock(data=[MagicMock(id="other")]))
        provider._client = mock_client  # type: ignore[assignment]
        with caplog.at_level("WARNING"):
            assert await provider.validate_connection() is True
        assert "Requested model 'gpt-5-mini' is not in the list" in caplog.text

    @pytest.mark.asyncio
    async def test_validate_connection_returns_false_on_non_http_error(
        self, provider: OpenAIProvider
    ) -> None:
        """Requirement: a non-HTTP error from models.list() fails startup."""
        from unittest.mock import AsyncMock

        mock_client = MagicMock()
        mock_client.models.list = AsyncMock(side_effect=RuntimeError("boom"))
        provider._client = mock_client  # type: ignore[assignment]
        assert await provider.validate_connection() is False
        assert provider._connection_probe_note is None

    @pytest.mark.asyncio
    async def test_validate_connection_returns_false_on_api_connection_error(
        self, provider: OpenAIProvider
    ) -> None:
        """Requirement: an unreachable host fails startup."""
        from unittest.mock import AsyncMock

        request = httpx.Request("GET", "http://custom/v1/models")
        exc = openai.APIConnectionError(message="connection refused", request=request)
        mock_client = MagicMock()
        mock_client.models.list = AsyncMock(side_effect=exc)
        provider._client = mock_client  # type: ignore[assignment]
        assert await provider.validate_connection() is False
        assert provider._connection_probe_note is None

    @pytest.mark.asyncio
    async def test_validate_connection_returns_false_on_auth_error(
        self, provider: OpenAIProvider
    ) -> None:
        """Requirement: rejected credentials (401/403) fail startup."""
        from unittest.mock import AsyncMock

        request = httpx.Request("GET", "http://custom/v1/models")
        response = httpx.Response(401, request=request)
        exc = openai.APIStatusError("unauthorized", response=response, body=None)
        mock_client = MagicMock()
        mock_client.models.list = AsyncMock(side_effect=exc)
        provider._client = mock_client  # type: ignore[assignment]
        assert await provider.validate_connection() is False
        assert provider._connection_probe_note is None

    @pytest.mark.asyncio
    async def test_validate_connection_returns_true_with_note_on_404(
        self, provider: OpenAIProvider
    ) -> None:
        """Requirement: a non-auth HTTP failure is treated as inconclusive."""
        from unittest.mock import AsyncMock

        request = httpx.Request("GET", "http://custom/v1/models")
        response = httpx.Response(404, request=request)
        exc = openai.APIStatusError("not found", response=response, body=None)
        mock_client = MagicMock()
        mock_client.models.list = AsyncMock(side_effect=exc)
        provider._client = mock_client  # type: ignore[assignment]
        assert await provider.validate_connection() is True
        assert provider._connection_probe_note == "unverified (HTTP 404)"

    @pytest.mark.asyncio
    async def test_list_models_returns_ids(self, provider: OpenAIProvider) -> None:
        """list_models() returns model ids from the OpenAI API."""
        from unittest.mock import AsyncMock

        mock_client = MagicMock()
        mock_client.models.list = AsyncMock(
            return_value=MagicMock(data=[MagicMock(id="gpt-5-mini"), MagicMock(id="gpt-5")])
        )
        provider._client = mock_client  # type: ignore[assignment]
        ids = await provider.list_models()
        assert ids == ["gpt-5-mini", "gpt-5"]

    @pytest.mark.asyncio
    async def test_get_model_capabilities_reasoning_models(
        self, provider: OpenAIProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Requirement: reasoning-capable models advertise the provider effort tuple."""

        def _fake_profile(model: str) -> Any:
            class Profile:
                openai_supports_reasoning = True

            return Profile()

        monkeypatch.setattr("pydantic_ai.profiles.openai.openai_model_profile", _fake_profile)
        caps = await provider.get_model_capabilities("o3-mini")
        assert caps is not None
        assert caps.supported_reasoning_efforts == ["low", "medium", "high"]
        # Requirement: capability probes do not report model-listing token limits.
        assert caps.max_prompt_tokens is None
        assert caps.max_output_tokens is None
        assert caps.max_context_window_tokens is None

    @pytest.mark.asyncio
    async def test_get_model_capabilities_non_reasoning_models(
        self, provider: OpenAIProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Requirement: non-reasoning models advertise no supported reasoning efforts."""

        def _fake_profile(model: str) -> Any:
            class Profile:
                openai_supports_reasoning = False

            return Profile()

        monkeypatch.setattr("pydantic_ai.profiles.openai.openai_model_profile", _fake_profile)
        caps = await provider.get_model_capabilities("gpt-4o")
        assert caps is not None
        assert caps.supported_reasoning_efforts == []

    @pytest.mark.asyncio
    async def test_get_model_capabilities_openrouter_prefix_not_misclassified(
        self, provider: OpenAIProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Requirement: 'openai/gpt-4o-mini' must not be misclassified as reasoning."""

        def _fake_profile(model: str) -> Any:
            class Profile:
                openai_supports_reasoning = False

            return Profile()

        monkeypatch.setattr("pydantic_ai.profiles.openai.openai_model_profile", _fake_profile)
        caps = await provider.get_model_capabilities("openai/gpt-4o-mini")
        assert caps is not None
        assert caps.supported_reasoning_efforts == []

    @pytest.mark.asyncio
    async def test_get_model_capabilities_returns_none_when_profile_attr_missing(
        self, provider: OpenAIProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Requirement: missing profile support attribute yields unknown capabilities."""

        def _fake_profile(model: str) -> Any:
            class Profile:
                pass

            return Profile()

        monkeypatch.setattr("pydantic_ai.profiles.openai.openai_model_profile", _fake_profile)
        caps = await provider.get_model_capabilities("some-model")
        assert caps is None


class TestOpenAIModelTokenLimits:
    """Tests for vendor-advertised OpenAI-compatible model token limits."""

    @pytest.mark.asyncio
    async def test_reads_advertised_limits_and_reuses_single_page_cache(
        self, provider: OpenAIProvider
    ) -> None:
        """Requirement: prompt and output hooks reuse limits advertised by one listing page."""
        assert provider._client is not None
        model = SimpleNamespace(
            id="vendor-model-20260101",
            context_length=262_144,
            top_provider={"max_completion_tokens": 32_768},
        )

        with patch.object(
            provider._client.models,
            "list",
            new=AsyncMock(return_value=SimpleNamespace(data=[model])),
        ) as list_models:
            assert await provider.get_max_prompt_tokens("vendor-model-latest") == 262_144
            assert await provider.get_max_output_tokens("vendor-model-latest") == 32_768

        list_models.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_caches_entries_without_advertised_limits(self, provider: OpenAIProvider) -> None:
        """Requirement: an entry without limits is cached as an authoritative unknown value."""
        assert provider._client is not None
        model = SimpleNamespace(id="vendor-model")

        with patch.object(
            provider._client.models,
            "list",
            new=AsyncMock(return_value=SimpleNamespace(data=[model])),
        ) as list_models:
            assert await provider.get_max_prompt_tokens("vendor-model") is None
            assert await provider.get_max_output_tokens("vendor-model") is None

        list_models.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_listing_failure_does_not_poison_model_limits_cache(
        self, provider: OpenAIProvider
    ) -> None:
        """Requirement: a transient models.list() failure leaves cache population retryable."""
        assert provider._client is not None
        model = SimpleNamespace(id="vendor-model", max_input_tokens=131_072)

        with patch.object(
            provider._client.models,
            "list",
            new=AsyncMock(
                side_effect=[
                    RuntimeError("temporary failure"),
                    SimpleNamespace(data=[model]),
                ]
            ),
        ) as list_models:
            assert await provider.get_max_prompt_tokens("vendor-model") is None
            assert await provider.get_max_prompt_tokens("vendor-model") == 131_072

        assert list_models.await_count == 2

    @pytest.mark.asyncio
    async def test_proxy_prefix_is_not_stripped(self, provider: OpenAIProvider) -> None:
        """A bare model name does not match a vendor-prefixed listing ID."""
        # Requirement: provider prefixes remain part of model identity during limit lookup.
        assert provider._client is not None
        model = SimpleNamespace(id="moonshotai/kimi-k3", max_input_tokens=1_048_576)

        with patch.object(
            provider._client.models,
            "list",
            new=AsyncMock(return_value=SimpleNamespace(data=[model])),
        ) as list_models:
            assert await provider.get_max_prompt_tokens("kimi-k3") is None
            assert await provider.get_max_output_tokens("kimi-k3") is None

        list_models.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_successful_probe_reenables_model_limit_lookups(
        self, provider: OpenAIProvider
    ) -> None:
        """Requirement: a successful validation resets a prior unavailable-listing state."""
        assert provider._client is not None
        request = httpx.Request("GET", "http://custom/v1/models")
        response = httpx.Response(404, request=request)
        unavailable = openai.APIStatusError("not found", response=response, body=None)
        model = SimpleNamespace(id="vendor-model", max_input_tokens=65_536)

        with patch.object(
            provider._client.models,
            "list",
            new=AsyncMock(
                side_effect=[
                    unavailable,
                    SimpleNamespace(data=[model]),
                ]
            ),
        ) as list_models:
            assert await provider.validate_connection() is True
            assert await provider.get_max_prompt_tokens("vendor-model") is None
            assert provider._model_listing_unavailable_warned is True

            assert await provider.validate_connection() is True
            assert provider._model_listing_unavailable is False
            assert provider._model_listing_unavailable_warned is False
            assert await provider.get_max_prompt_tokens("vendor-model") == 65_536

        assert list_models.await_count == 2

    @pytest.mark.asyncio
    async def test_close_clears_model_limits_cache_and_listing_flags(
        self, provider: OpenAIProvider
    ) -> None:
        """Requirement: close discards model metadata and unavailable-listing state."""
        assert provider._client is not None
        model = SimpleNamespace(id="vendor-model", max_input_tokens=32_768)

        with patch.object(
            provider._client.models,
            "list",
            new=AsyncMock(return_value=SimpleNamespace(data=[model])),
        ):
            assert await provider.get_max_prompt_tokens("vendor-model") == 32_768

        provider._model_listing_unavailable = True
        provider._model_listing_unavailable_warned = True
        client = provider._client
        with patch.object(client, "close", new=AsyncMock()) as close:
            await provider.close()

        close.assert_awaited_once_with()
        assert provider._model_limits_cache is None
        assert provider._model_listing_unavailable is False
        assert provider._model_listing_unavailable_warned is False
