"""Hermes parity tests for schema-shape recovery (issue #343).

Hermes already validated inside its recovery loop. These cover the two gaps
closed alongside the Copilot and Claude fixes: the wrapper unwrap, and honoring
the YAML ``max_parse_recovery_attempts`` that the other providers respect.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import Mock, patch

import pytest

from conductor.config.schema import AgentDef, OutputField, RetryPolicy
from conductor.exceptions import ProviderError, ValidationError
from conductor.providers.hermes import HermesProvider


def _make_result(final_response: str) -> dict[str, Any]:
    return {
        "final_response": final_response,
        "completed": True,
        "failed": False,
        "partial": False,
        "error": None,
        "messages": [],
        "model": "anthropic/claude-sonnet-4",
        "input_tokens": 10,
        "output_tokens": 20,
        "total_tokens": 30,
    }


def _agent(max_parse_recovery_attempts: int | None = None) -> AgentDef:
    retry = (
        RetryPolicy(max_parse_recovery_attempts=max_parse_recovery_attempts)
        if max_parse_recovery_attempts is not None
        else None
    )
    return AgentDef(
        name="reviewer",
        output={"decision": OutputField(type="string")},
        retry=retry,
    )


@pytest.fixture
def provider() -> HermesProvider:
    with (
        patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True),
        patch("conductor.providers.hermes.AIAgent"),
    ):
        return HermesProvider(model="anthropic/claude-sonnet-4", max_agent_iterations=10)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class TestHermesSchemaShapeParity:
    def test_wrapper_shape_resolves_without_a_round_trip(self, provider: HermesProvider) -> None:
        result = _make_result('{"decision": {"decision": "APPROVE", "why": "ok"}}')

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            instance = Mock()
            instance.run_conversation.return_value = result
            mock_cls.return_value = instance

            output = _run(provider.execute(_agent(), {}, "review it"))

        assert output.content == {"decision": "APPROVE"}
        assert instance.run_conversation.call_count == 1

    def test_still_recovers_from_wrong_shape(self, provider: HermesProvider) -> None:
        """Regression: the behavior the issue reported hermes already had."""
        responses = [
            _make_result('{"decision": {"a": 1, "b": 2}}'),
            _make_result('{"decision": "APPROVE"}'),
        ]

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            instance = Mock()
            instance.run_conversation.side_effect = responses
            mock_cls.return_value = instance

            output = _run(provider.execute(_agent(), {}, "review it"))

        assert output.content == {"decision": "APPROVE"}
        assert instance.run_conversation.call_count == 2

    def test_yaml_recovery_budget_is_honored(self, provider: HermesProvider) -> None:
        """Previously hardcoded to 3, ignoring the configured value."""
        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            instance = Mock()
            instance.run_conversation.return_value = _make_result('{"decision": {"a": 1, "b": 2}}')
            mock_cls.return_value = instance

            with pytest.raises(ValidationError):
                _run(provider.execute(_agent(max_parse_recovery_attempts=1), {}, "review it"))

        # Initial attempt plus exactly one recovery.
        assert instance.run_conversation.call_count == 2

    def test_default_recovery_budget_unchanged(self, provider: HermesProvider) -> None:
        """With no YAML override the hermes default of 3 still applies."""
        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            instance = Mock()
            instance.run_conversation.return_value = _make_result('{"decision": {"a": 1, "b": 2}}')
            mock_cls.return_value = instance

            with pytest.raises(ValidationError):
                _run(provider.execute(_agent(), {}, "review it"))

        assert instance.run_conversation.call_count == 4

    def test_syntax_failure_keeps_provider_error(self, provider: HermesProvider) -> None:
        """A syntax error is also a ValidationError internally; it must not
        be mistaken for a schema failure."""
        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            instance = Mock()
            instance.run_conversation.return_value = _make_result("not json at all {{{")
            mock_cls.return_value = instance

            with pytest.raises(ProviderError, match="Failed to parse structured output"):
                _run(provider.execute(_agent(max_parse_recovery_attempts=1), {}, "review it"))

    def test_emits_parse_recovery_event(self, provider: HermesProvider) -> None:
        responses = [
            _make_result('{"decision": {"a": 1, "b": 2}}'),
            _make_result('{"decision": "APPROVE"}'),
        ]
        events: list[tuple[str, dict[str, Any]]] = []

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            instance = Mock()
            instance.run_conversation.side_effect = responses
            mock_cls.return_value = instance

            _run(
                provider.execute(
                    _agent(),
                    {},
                    "review it",
                    event_callback=lambda name, data: events.append((name, data)),
                )
            )

        recovery = [d for name, d in events if name == "agent_parse_recovery"]
        assert len(recovery) == 1
        assert recovery[0]["reason"] == "schema"
