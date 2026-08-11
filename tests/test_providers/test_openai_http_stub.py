"""HTTP-stub integration tests for the OpenAI Pydantic AI pipeline.

These tests drive the *real* ``OpenAIChatModel`` and ``AsyncOpenAI`` client
through the shared ``run_agent_pipeline`` helper with no external network.
Responses are served via ``httpx.MockTransport`` so we can assert exact
request counts and retry behavior while using the same code paths a live
workflow would exercise.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic_ai.exceptions import ModelHTTPError

from conductor.config.schema import AgentDef, ToolOutputConfig
from conductor.exceptions import ProviderError
from conductor.providers._pydantic_ai.agent_builder import build_agent
from conductor.providers._pydantic_ai.retry import RetryConfig
from conductor.providers._pydantic_ai.runner import run_agent_pipeline


def _make_openai_success_sse(content: str) -> str:
    """Return a realistic Chat Completions SSE stream for ``httpx.MockTransport``.

    Pydantic AI's ``run_with_interrupt`` streams responses, so a plain JSON body
    is not accepted. The stream emits the assistant role, the content delta, a
    finish chunk, and a final usage-only chunk, followed by ``[DONE]``.
    """
    content_json = json.dumps(content)
    chunks = [
        '{"id":"chatcmpl-test-1","object":"chat.completion.chunk","model":"gpt-5-mini","created":1,"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}',
        f'{{"id":"chatcmpl-test-1","object":"chat.completion.chunk","model":"gpt-5-mini","created":1,"choices":[{{"index":0,"delta":{{"content":{content_json}}},"finish_reason":null}}]}}',
        '{"id":"chatcmpl-test-1","object":"chat.completion.chunk","model":"gpt-5-mini","created":1,"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
        '{"id":"chatcmpl-test-1","object":"chat.completion.chunk","model":"gpt-5-mini","created":1,"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}',
    ]
    return "".join(f"data: {chunk}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def _make_openai_400_response(reasoning_param: str = "openai_reasoning_effort") -> dict[str, Any]:
    """Return a realistic OpenAI 400 error body for an unsupported reasoning parameter."""
    return {
        "error": {
            "message": f"Unsupported parameter: '{reasoning_param}'",
            "type": "invalid_request_error",
            "param": reasoning_param,
            "code": None,
        }
    }


def _make_openai_429_response() -> dict[str, Any]:
    """Return a realistic OpenAI 429 rate-limit error body."""
    return {
        "error": {
            "message": "Rate limit reached for requests",
            "type": "rate_limit_error",
            "param": None,
            "code": "rate_limit",
        }
    }


def _build_mock_transport(
    responses: list[tuple[int, dict[str, Any] | str]],
    captured: dict[str, Any],
) -> httpx.MockTransport:
    """Build an ``httpx.MockTransport`` that serves a sequence of stub responses.

    Args:
        responses: Ordered list of ``(status_code, body)`` tuples. ``body`` may
            be a ``dict`` for a JSON error response or a ``str`` for an SSE body.
        captured: Dictionary that receives request diagnostics for assertions.
    """
    call_index: list[int] = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_index[0] += 1
        captured.setdefault("urls", []).append(str(request.url))
        captured.setdefault("bodies", []).append(request.content)

        status, body = responses[call_index[0] - 1]
        if isinstance(body, str):
            return httpx.Response(status, text=body, headers={"content-type": "text/event-stream"})
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def _build_pipeline_runner(
    agent: AgentDef,
    responses: list[tuple[int, dict[str, Any] | str]],
    retry_config: RetryConfig | None = None,
) -> tuple[Callable[[], Any], dict[str, Any]]:
    """Assemble a callable that executes ``run_agent_pipeline`` against stub responses.

    Returns a ``(coroutine_factory, captured)`` pair. The factory is suitable for
    passing to ``asyncio.run`` in a test and uses a real ``OpenAIChatModel``
    backed by ``httpx.MockTransport``.
    """
    captured: dict[str, Any] = {}
    transport = _build_mock_transport(responses, captured)
    http_client = httpx.AsyncClient(transport=transport)

    default_retry = retry_config or RetryConfig(
        max_attempts=3,
        base_delay=0.0,
        max_delay=0.0,
        jitter=0.0,
        backoff="fixed",
    )

    def build_agent_fn(toolsets: list[Any], *, max_parse_recovery_attempts: int) -> Any:
        """Return a pre-built OpenAI-backed Pydantic AI agent."""
        return build_agent(
            agent=agent,
            system_prompt=agent.system_prompt or "",
            rendered_prompt="",
            backend="openai",
            http_client=http_client,
            api_key="sk-test",
            default_model="gpt-5-mini",
            default_temperature=0.5,
            default_max_tokens=1024,
            toolsets=toolsets,
            max_parse_recovery_attempts=max_parse_recovery_attempts,
        )

    async def _run() -> Any:
        return await run_agent_pipeline(
            agent=agent,
            rendered_prompt="say hello",
            mcp_manager=None,
            tools=[],
            tool_output_config=ToolOutputConfig(),
            retry_config=default_retry,
            interrupt_signal=None,
            event_callback=None,
            max_agent_iterations=10,
            max_session_seconds=None,
            default_model="gpt-5-mini",
            retry_history=[],
            build_agent_fn=build_agent_fn,
        )

    return _run, captured


@pytest.mark.asyncio
async def test_openai_pipeline_success_maps_usage() -> None:
    """A successful streaming Chat Completions response maps tokens and content."""
    agent = AgentDef(name="greeter", model="gpt-5-mini", prompt="say hi")
    run, captured = _build_pipeline_runner(
        agent,
        responses=[(200, _make_openai_success_sse("hello"))],
    )

    output = await run()

    assert output.content == {"result": "hello"}
    assert output.model == "gpt-5-mini"
    assert output.tokens_used == 8
    assert output.input_tokens == 5
    assert output.output_tokens == 3
    assert output.partial is False
    assert len(captured["urls"]) == 1


@pytest.mark.asyncio
async def test_openai_pipeline_400_reasoning_effort_is_fatal_one_request() -> None:
    """A 400 from ``openai_reasoning_effort`` is non-retryable and one request."""
    agent = AgentDef(
        name="reasoner",
        model="gpt-5-mini",
        prompt="think",
        reasoning={"effort": "low"},  # type: ignore[dict-item]
    )
    run, captured = _build_pipeline_runner(
        agent,
        responses=[(400, _make_openai_400_response("openai_reasoning_effort"))],
    )

    with pytest.raises(ProviderError) as exc_info:
        await run()

    error = exc_info.value
    assert error.is_retryable is False
    cause = error.__cause__
    assert cause is not None
    assert isinstance(cause, ModelHTTPError)
    assert cause.status_code == 400
    assert len(captured["urls"]) == 1


@pytest.mark.asyncio
async def test_openai_pipeline_429_retries_then_succeeds_with_two_requests() -> None:
    """A 429 followed by a 200 retries once and results in exactly two Chat Completions requests."""
    agent = AgentDef(name="greeter", model="gpt-5-mini", prompt="say hi")
    run, captured = _build_pipeline_runner(
        agent,
        responses=[
            (429, _make_openai_429_response()),
            (200, _make_openai_success_sse("hello")),
        ],
    )

    output = await run()

    assert output.content == {"result": "hello"}
    assert output.model == "gpt-5-mini"
    assert len(captured["urls"]) == 2
