"""Tests for per-agent max_parse_recovery_attempts configuration.

Verifies that the YAML retry.max_parse_recovery_attempts field is correctly
threaded through both Copilot and Claude providers, overriding provider
defaults when set.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from conductor.config.schema import AgentDef, OutputField, RetryPolicy
from conductor.exceptions import ProviderError

# ---------------------------------------------------------------------------
# Copilot provider tests
# ---------------------------------------------------------------------------


class TestCopilotParseRecoveryConfig:
    """Tests that Copilot provider respects per-agent max_parse_recovery_attempts."""

    def _make_provider(self, mock_handler: Any = None, retry_config: Any = None) -> Any:
        from conductor.providers.copilot import CopilotProvider, RetryConfig

        config = retry_config or RetryConfig()
        return CopilotProvider(mock_handler=mock_handler, retry_config=config)

    def test_resolve_retry_config_uses_yaml_value(self) -> None:
        """Per-agent retry.max_parse_recovery_attempts overrides provider default."""
        from conductor.providers.copilot import RetryConfig

        provider = self._make_provider(
            mock_handler=lambda a, p, c: {"result": "ok"},
            retry_config=RetryConfig(max_parse_recovery_attempts=5),
        )
        agent = AgentDef(
            name="test",
            prompt="test",
            retry=RetryPolicy(max_parse_recovery_attempts=2),
        )
        resolved = provider._resolve_retry_config(agent)
        assert resolved.max_parse_recovery_attempts == 2

    def test_resolve_retry_config_falls_back_to_provider_default(self) -> None:
        """When YAML field is omitted (None), provider default is preserved."""
        from conductor.providers.copilot import RetryConfig

        provider = self._make_provider(
            mock_handler=lambda a, p, c: {"result": "ok"},
            retry_config=RetryConfig(max_parse_recovery_attempts=5),
        )
        agent = AgentDef(
            name="test",
            prompt="test",
            retry=RetryPolicy(),  # max_parse_recovery_attempts=None
        )
        resolved = provider._resolve_retry_config(agent)
        assert resolved.max_parse_recovery_attempts == 5

    def test_resolve_retry_config_zero_disables_recovery(self) -> None:
        """max_parse_recovery_attempts=0 resolves to 0 (disable recovery)."""
        from conductor.providers.copilot import RetryConfig

        provider = self._make_provider(
            mock_handler=lambda a, p, c: {"result": "ok"},
            retry_config=RetryConfig(max_parse_recovery_attempts=5),
        )
        agent = AgentDef(
            name="test",
            prompt="test",
            retry=RetryPolicy(max_parse_recovery_attempts=0),
        )
        resolved = provider._resolve_retry_config(agent)
        assert resolved.max_parse_recovery_attempts == 0

    def test_no_retry_policy_uses_provider_default(self) -> None:
        """Agent without retry policy gets provider-level default (5)."""
        from conductor.providers.copilot import RetryConfig

        provider = self._make_provider(
            mock_handler=lambda a, p, c: {"result": "ok"},
            retry_config=RetryConfig(max_parse_recovery_attempts=5),
        )
        agent = AgentDef(name="test", prompt="test")
        resolved = provider._resolve_retry_config(agent)
        assert resolved.max_parse_recovery_attempts == 5


# ---------------------------------------------------------------------------
# Claude provider tests
# ---------------------------------------------------------------------------


class TestClaudeParseRecoveryConfig:
    """Tests that Claude provider respects per-agent max_parse_recovery_attempts."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_resolve_retry_config_uses_yaml_value(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Per-agent retry.max_parse_recovery_attempts overrides provider default."""
        mock_anthropic_module.__version__ = "0.77.0"
        from conductor.providers.claude import ClaudeProvider, RetryConfig

        provider = ClaudeProvider(retry_config=RetryConfig(max_parse_recovery_attempts=2))
        agent = AgentDef(
            name="test",
            prompt="test",
            retry=RetryPolicy(max_parse_recovery_attempts=7),
        )
        resolved = provider._resolve_retry_config(agent)
        assert resolved.max_parse_recovery_attempts == 7

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_resolve_retry_config_falls_back_to_provider_default(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """When YAML field is omitted (None), provider default is preserved."""
        mock_anthropic_module.__version__ = "0.77.0"
        from conductor.providers.claude import ClaudeProvider, RetryConfig

        provider = ClaudeProvider(retry_config=RetryConfig(max_parse_recovery_attempts=2))
        agent = AgentDef(
            name="test",
            prompt="test",
            retry=RetryPolicy(),  # max_parse_recovery_attempts=None
        )
        resolved = provider._resolve_retry_config(agent)
        assert resolved.max_parse_recovery_attempts == 2

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_resolve_retry_config_zero_disables_recovery(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """max_parse_recovery_attempts=0 resolves to 0 (disable recovery)."""
        mock_anthropic_module.__version__ = "0.77.0"
        from conductor.providers.claude import ClaudeProvider, RetryConfig

        provider = ClaudeProvider(retry_config=RetryConfig(max_parse_recovery_attempts=2))
        agent = AgentDef(
            name="test",
            prompt="test",
            retry=RetryPolicy(max_parse_recovery_attempts=0),
        )
        resolved = provider._resolve_retry_config(agent)
        assert resolved.max_parse_recovery_attempts == 0

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_no_retry_policy_uses_provider_default(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Agent without retry policy gets provider-level default (2)."""
        mock_anthropic_module.__version__ = "0.77.0"
        from conductor.providers.claude import ClaudeProvider, RetryConfig

        provider = ClaudeProvider(retry_config=RetryConfig(max_parse_recovery_attempts=2))
        agent = AgentDef(name="test", prompt="test")
        resolved = provider._resolve_retry_config(agent)
        assert resolved.max_parse_recovery_attempts == 2

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_parse_recovery_zero_skips_recovery_loop(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """When max_parse_recovery_attempts=0, parse recovery raises immediately."""
        mock_anthropic_module.__version__ = "0.77.0"

        # Create a text-only response (no tool_use, no valid JSON) to trigger recovery
        text_block = Mock()
        text_block.type = "text"
        text_block.text = "This is not JSON at all"

        bad_response = Mock()
        bad_response.id = "msg_bad"
        bad_response.content = [text_block]
        bad_response.model = "claude-3-5-sonnet-latest"
        bad_response.stop_reason = "end_turn"
        bad_response.usage = Mock(input_tokens=10, output_tokens=20, cache_creation_input_tokens=0)
        bad_response.type = "message"
        bad_response.role = "assistant"

        mock_client = Mock()
        mock_client.messages = Mock()
        # Only ONE API call should be made (no recovery calls)
        mock_client.messages.create = AsyncMock(return_value=bad_response)
        mock_client.models = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_client.close = AsyncMock()
        mock_anthropic_class.return_value = mock_client

        from conductor.providers.claude import ClaudeProvider

        provider = ClaudeProvider()

        # Call _execute_with_parse_recovery directly with max_parse_recovery_attempts=0
        with pytest.raises(ProviderError, match="Failed to extract valid JSON after 0"):
            await provider._execute_with_parse_recovery(
                messages=[{"role": "user", "content": "test"}],
                model="claude-3-5-sonnet-latest",
                temperature=0.7,
                max_tokens=1024,
                tools=None,
                output_schema={"answer": OutputField(type="string")},
                max_parse_recovery_attempts=0,
            )

        # Exactly 1 API call: the initial attempt, no recovery calls
        assert mock_client.messages.create.call_count == 1
        await provider.close()


class TestCopilotParseRecoveryThreading:
    """Tests that per-agent config is threaded to the parse recovery loop in Copilot."""

    @pytest.mark.asyncio
    async def test_retry_config_threaded_to_sdk_call(self) -> None:
        """The resolved retry_config is passed to _execute_sdk_call."""
        from conductor.providers.copilot import CopilotProvider, RetryConfig

        call_args: list[dict[str, Any]] = []

        async def capture_sdk_call(self: Any, *args: Any, **kwargs: Any) -> Any:
            call_args.append(kwargs)
            # Return a simple response to avoid further processing
            return {"result": "ok"}, None

        provider = CopilotProvider(
            mock_handler=lambda a, p, c: {"result": "ok"},
            retry_config=RetryConfig(max_parse_recovery_attempts=5),
        )

        agent = AgentDef(
            name="test",
            prompt="test",
            retry=RetryPolicy(max_parse_recovery_attempts=3),
        )

        with patch.object(CopilotProvider, "_execute_sdk_call", capture_sdk_call):
            await provider.execute(
                agent=agent,
                context={"workflow": {"input": {}}},
                rendered_prompt="test",
            )

        assert len(call_args) == 1
        assert call_args[0]["retry_config"].max_parse_recovery_attempts == 3
