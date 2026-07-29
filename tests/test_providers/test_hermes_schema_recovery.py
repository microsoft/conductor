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

    def test_recovery_prompt_uses_schema_wording(self, provider: HermesProvider) -> None:
        """Telling a model its valid JSON 'could not be parsed' invites it to
        re-send the same payload."""
        responses = [
            _make_result('{"decision": {"a": 1, "b": 2}}'),
            _make_result('{"decision": "APPROVE"}'),
        ]

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            instance = Mock()
            instance.run_conversation.side_effect = responses
            mock_cls.return_value = instance

            _run(provider.execute(_agent(), {}, "review it"))

        recovery_prompt = instance.run_conversation.call_args_list[1].args[0]
        assert "did not match the required output schema" in recovery_prompt
        assert "could not be parsed as valid JSON" not in recovery_prompt

    def test_syntax_recovery_prompt_keeps_parse_wording(self, provider: HermesProvider) -> None:
        responses = [
            _make_result("not json at all {{{"),
            _make_result('{"decision": "APPROVE"}'),
        ]

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            instance = Mock()
            instance.run_conversation.side_effect = responses
            mock_cls.return_value = instance

            _run(provider.execute(_agent(), {}, "review it"))

        recovery_prompt = instance.run_conversation.call_args_list[1].args[0]
        assert "could not be parsed as valid JSON" in recovery_prompt

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

    def test_syntax_failure_event_is_labelled_syntax(self, provider: HermesProvider) -> None:
        responses = [_make_result("not json"), _make_result('{"decision": "APPROVE"}')]
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
        assert recovery[0]["reason"] == "syntax"

    def test_zero_budget_makes_exactly_one_attempt(self, provider: HermesProvider) -> None:
        """0 is a legal configured value meaning "fail fast", not "unset"."""
        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            instance = Mock()
            instance.run_conversation.return_value = _make_result('{"decision": {"a": 1, "b": 2}}')
            mock_cls.return_value = instance

            with pytest.raises(ValidationError):
                _run(provider.execute(_agent(max_parse_recovery_attempts=0), {}, "review it"))

        assert instance.run_conversation.call_count == 1

    def test_exhaustion_message_names_the_field_and_type(self, provider: HermesProvider) -> None:
        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            instance = Mock()
            instance.run_conversation.return_value = _make_result('{"decision": {"a": 1, "b": 2}}')
            mock_cls.return_value = instance

            with pytest.raises(ValidationError) as exc_info:
                _run(provider.execute(_agent(max_parse_recovery_attempts=1), {}, "review it"))

        message = str(exc_info.value)
        assert "decision" in message
        assert "expected string, got dict" in message

    def test_non_object_json_is_wrapped_by_parse_json_output(
        self, provider: HermesProvider
    ) -> None:
        """Documents a real divergence from Copilot and Claude.

        Hermes parses via ``parse_json_output``, which rewrites any non-dict
        into ``{"result": ...}`` before ``normalize_agent_output`` can reject
        it. So a bare scalar reaches validation as a named field rather than
        as "not an object". Pinned here so the difference is deliberate.
        """
        agent = AgentDef(name="reviewer", output={"result": OutputField(type="string")})

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            instance = Mock()
            instance.run_conversation.return_value = _make_result('"hello"')
            mock_cls.return_value = instance

            output = _run(provider.execute(agent, {}, "review it"))

        assert output.content == {"result": "hello"}
        assert instance.run_conversation.call_count == 1

    def test_raising_event_subscriber_does_not_break_execution(
        self, provider: HermesProvider
    ) -> None:
        responses = [
            _make_result('{"decision": {"a": 1, "b": 2}}'),
            _make_result('{"decision": "APPROVE"}'),
        ]

        def _boom(name: str, data: dict[str, Any]) -> None:
            raise RuntimeError("subscriber exploded")

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            instance = Mock()
            instance.run_conversation.side_effect = responses
            mock_cls.return_value = instance

            output = _run(provider.execute(_agent(), {}, "review it", event_callback=_boom))

        assert output.content == {"decision": "APPROVE"}
