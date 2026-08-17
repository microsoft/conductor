"""Schema-shape recovery tests for the Copilot provider (issue #343).

A response that is valid JSON but has a wrong-typed field must go through the
same recovery loop as a syntax failure, rather than escaping the provider and
killing the workflow.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from conductor.config.schema import AgentDef, OutputField, RetryPolicy
from conductor.exceptions import ProviderError, ValidationError
from conductor.providers.copilot import CopilotProvider, SDKResponse


class _FakeSession:
    session_id = "sess-343"

    async def disconnect(self) -> None:
        return None


class _FakeClient:
    async def create_session(self, **kwargs: Any) -> _FakeSession:
        return _FakeSession()


def _make_provider(responses: list[str]) -> tuple[CopilotProvider, list[str]]:
    """Build a provider whose SDK returns ``responses`` in order.

    Returns:
        The provider and the list that records each prompt sent.
    """
    provider = CopilotProvider()
    provider._client = _FakeClient()
    provider._mock_handler = None
    provider._started = True

    prompts: list[str] = []
    remaining = list(responses)

    async def _noop() -> None:
        return None

    async def _fake_send_and_wait(*args: Any, **kwargs: Any) -> SDKResponse:
        # The prompt is the second positional arg after ``session``.
        if len(args) >= 2:
            prompts.append(str(args[1]))
        content = remaining.pop(0) if remaining else remaining_last
        return SDKResponse(content=content, input_tokens=1, output_tokens=1)

    remaining_last = responses[-1]
    provider._ensure_client_started = _noop  # type: ignore[method-assign]
    provider._send_and_wait = _fake_send_and_wait  # type: ignore[method-assign]
    return provider, prompts


def _agent(max_parse_recovery_attempts: int | None = None) -> AgentDef:
    retry = (
        RetryPolicy(max_parse_recovery_attempts=max_parse_recovery_attempts)
        if max_parse_recovery_attempts is not None
        else None
    )
    return AgentDef(
        name="reviewer",
        model="gpt-4o",
        prompt="review it",
        output={"decision": OutputField(type="string")},
        retry=retry,
    )


class TestCopilotSchemaShapeRecovery:
    def test_wrong_typed_field_is_recovered(self) -> None:
        """A dict where a string was declared triggers a re-prompt and recovers."""
        provider, prompts = _make_provider(
            [
                '{"decision": {"verdict": "APPROVE", "why": "ok"}}',
                '{"decision": "APPROVE"}',
            ]
        )

        output = asyncio.run(provider.execute(_agent(), {}, "review it"))

        assert output.content == {"decision": "APPROVE"}
        # Initial prompt plus exactly one recovery prompt.
        assert len(prompts) == 2
        assert "did not match the required output schema" in prompts[1]

    def test_recovery_prompt_uses_schema_wording_not_parse_wording(self) -> None:
        provider, prompts = _make_provider(
            [
                '{"decision": {"a": 1, "b": 2}}',
                '{"decision": "APPROVE"}',
            ]
        )

        asyncio.run(provider.execute(_agent(), {}, "review it"))

        assert "could not be parsed as valid JSON" not in prompts[1]
        assert "Schema Error:" in prompts[1]

    def test_exhausted_budget_reraises_validation_error(self) -> None:
        """The specific field error survives instead of a generic parse error."""
        provider, _ = _make_provider(['{"decision": {"a": 1, "b": 2}}'])

        with pytest.raises(ValidationError) as exc_info:
            asyncio.run(provider.execute(_agent(max_parse_recovery_attempts=1), {}, "review it"))

        message = str(exc_info.value)
        assert "decision" in message
        assert "expected string, got dict" in message

    def test_syntax_failure_still_raises_provider_error(self) -> None:
        """Non-JSON keeps the existing ProviderError contract."""
        provider, _ = _make_provider(["not json at all"])

        with pytest.raises(ProviderError, match="Failed to parse structured output"):
            asyncio.run(provider.execute(_agent(max_parse_recovery_attempts=1), {}, "review it"))

    def test_recovery_budget_is_respected(self) -> None:
        """A persistently wrong shape re-prompts exactly ``max`` times."""
        provider, prompts = _make_provider(['{"decision": {"a": 1, "b": 2}}'])

        with pytest.raises(ValidationError):
            asyncio.run(provider.execute(_agent(max_parse_recovery_attempts=3), {}, "review it"))

        # Initial attempt plus three recovery prompts.
        assert len(prompts) == 4

    def test_wrapper_shape_resolves_without_a_round_trip(self) -> None:
        """The unwrap short-circuits the common wrapper case."""
        provider, prompts = _make_provider(
            ['{"decision": {"decision": "APPROVE", "reasoning": "fine"}}']
        )

        output = asyncio.run(provider.execute(_agent(), {}, "review it"))

        assert output.content == {"decision": "APPROVE"}
        assert len(prompts) == 1

    def test_emits_parse_recovery_event(self) -> None:
        provider, _ = _make_provider(
            [
                '{"decision": {"a": 1, "b": 2}}',
                '{"decision": "APPROVE"}',
            ]
        )
        events: list[tuple[str, dict[str, Any]]] = []

        asyncio.run(
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
        assert recovery[0]["attempt"] == 1

    def test_syntax_failure_event_is_labelled_syntax(self) -> None:
        provider, _ = _make_provider(["not json", '{"decision": "APPROVE"}'])
        events: list[tuple[str, dict[str, Any]]] = []

        asyncio.run(
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

    def test_bare_scalar_response_is_recovered_not_misreported(self) -> None:
        """A bare JSON scalar must re-enter the recovery loop.

        `validate_output` raises TypeError on a scalar rather than
        ValidationError, so without the normalize guard it escapes the loop's
        except clause entirely and gets zero recovery attempts.
        """
        provider, prompts = _make_provider(["42", '{"decision": "APPROVE"}'])

        output = asyncio.run(provider.execute(_agent(), {}, "review it"))

        assert output.content == {"decision": "APPROVE"}
        assert len(prompts) == 2

    def test_bare_scalar_exhaustion_reports_the_real_cause(self) -> None:
        """The error must name the shape problem, not the Copilot CLI."""
        provider, _ = _make_provider(["42"])

        with pytest.raises(ValidationError) as exc_info:
            asyncio.run(provider.execute(_agent(max_parse_recovery_attempts=1), {}, "review it"))

        message = str(exc_info.value)
        assert "not an object" in message
        assert "installed and authenticated" not in message

    def test_json_array_response_is_recovered(self) -> None:
        provider, prompts = _make_provider(['["decision", 1]', '{"decision": "APPROVE"}'])

        output = asyncio.run(provider.execute(_agent(), {}, "review it"))

        assert output.content == {"decision": "APPROVE"}
        assert len(prompts) == 2

    def test_zero_budget_makes_exactly_one_attempt(self) -> None:
        """0 is a legal configured value meaning "fail fast", not "unset"."""
        provider, prompts = _make_provider(['{"decision": {"a": 1, "b": 2}}'])

        with pytest.raises(ValidationError):
            asyncio.run(provider.execute(_agent(max_parse_recovery_attempts=0), {}, "review it"))

        assert len(prompts) == 1

    def test_syntax_recovery_prompt_keeps_parse_wording(self) -> None:
        provider, prompts = _make_provider(["not json", '{"decision": "APPROVE"}'])

        asyncio.run(provider.execute(_agent(), {}, "review it"))

        assert "could not be parsed as valid JSON" in prompts[1]
        assert "did not match the required output schema" not in prompts[1]

    def test_schema_then_syntax_exhaustion_raises_provider_error(self) -> None:
        """The error kind must follow the LAST failure, not the first."""
        provider, _ = _make_provider(['{"decision": {"a": 1, "b": 2}}', "not json at all"])

        with pytest.raises(ProviderError, match="Failed to parse structured output"):
            asyncio.run(provider.execute(_agent(max_parse_recovery_attempts=1), {}, "review it"))

    def test_syntax_then_schema_exhaustion_raises_validation_error(self) -> None:
        provider, _ = _make_provider(["not json at all", '{"decision": {"a": 1, "b": 2}}'])

        with pytest.raises(ValidationError, match="expected string, got dict"):
            asyncio.run(provider.execute(_agent(max_parse_recovery_attempts=1), {}, "review it"))

    def test_interrupted_partial_scalar_is_still_a_dict(self) -> None:
        """The partial path has no recovery loop, so a non-object there would
        reach the engine as a scalar `AgentOutput.content`."""
        provider = CopilotProvider()
        provider._client = _FakeClient()
        provider._mock_handler = None
        provider._started = True

        async def _noop() -> None:
            return None

        async def _fake_send_and_wait(*args: Any, **kwargs: Any) -> SDKResponse:
            return SDKResponse(content="42", partial=True, input_tokens=1, output_tokens=1)

        provider._ensure_client_started = _noop  # type: ignore[method-assign]
        provider._send_and_wait = _fake_send_and_wait  # type: ignore[method-assign]

        output = asyncio.run(provider.execute(_agent(), {}, "review it"))

        assert isinstance(output.content, dict)

    def test_raising_event_subscriber_does_not_break_execution(self) -> None:
        provider, _ = _make_provider(['{"decision": {"a": 1, "b": 2}}', '{"decision": "APPROVE"}'])

        def _boom(name: str, data: dict[str, Any]) -> None:
            raise RuntimeError("subscriber exploded")

        output = asyncio.run(provider.execute(_agent(), {}, "review it", event_callback=_boom))

        assert output.content == {"decision": "APPROVE"}

    def test_recovery_call_billing_and_context_figures_stay_distinct(self) -> None:
        """The exact #412 scenario: original call 559_847 input tokens, one
        recovery call 561_285 input tokens. Billing (`input_tokens`) sums
        both calls, but `last_call_input_tokens` reflects only the most
        recent (recovery) call's prompt size."""
        provider = CopilotProvider()
        provider._client = _FakeClient()
        provider._mock_handler = None
        provider._started = True

        responses = [
            SDKResponse(
                content='{"decision": {"a": 1, "b": 2}}',
                input_tokens=559_847,
                output_tokens=100,
                last_call_input_tokens=559_847,
            ),
            SDKResponse(
                content='{"decision": "APPROVE"}',
                input_tokens=561_285,
                output_tokens=50,
                last_call_input_tokens=561_285,
            ),
        ]
        remaining = list(responses)

        async def _noop() -> None:
            return None

        async def _fake_send_and_wait(*args: Any, **kwargs: Any) -> SDKResponse:
            return remaining.pop(0) if remaining else responses[-1]

        provider._ensure_client_started = _noop  # type: ignore[method-assign]
        provider._send_and_wait = _fake_send_and_wait  # type: ignore[method-assign]

        output = asyncio.run(provider.execute(_agent(), {}, "review it"))

        assert output.content == {"decision": "APPROVE"}
        assert output.input_tokens == 1_121_132
        assert output.last_call_input_tokens == 561_285
