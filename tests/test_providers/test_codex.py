"""Unit tests for the OpenAI Codex provider."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from conductor.config.schema import AgentDef, OutputField
from conductor.providers import codex as codex_module
from conductor.providers.codex import CodexProvider


class _FakeCodexConfig:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _FakeSandbox:
    read_only = "read-only"
    workspace_write = "workspace-write"
    full_access = "full-access"


class _FakeApprovalMode:
    deny_all = "deny_all"
    auto_review = "auto_review"


class _FakeReasoningEffort:
    low = "low"
    medium = "medium"
    high = "high"
    xhigh = "xhigh"


class _FakeTurn:
    def __init__(self, events: list[Any], event_delay: float = 0.0) -> None:
        self.id = "turn-1"
        self.thread_id = "thread-new"
        self._events = events
        self._event_delay = event_delay
        self.interrupted = False

    async def interrupt(self) -> None:
        self.interrupted = True

    async def _iter_events(self):
        if self._event_delay:
            await asyncio.sleep(self._event_delay)
        for event in self._events:
            yield event

    def stream(self):
        return self._iter_events()


class _FakeThread:
    last_turn_kwargs: dict[str, Any] = {}
    event_delay: float = 0.0

    def __init__(self, thread_id: str = "thread-new") -> None:
        self.id = thread_id

    async def turn(self, input_text: str, **kwargs: Any) -> _FakeTurn:
        _FakeThread.last_turn_kwargs = {"input": input_text, **kwargs}
        agent_message = SimpleNamespace(
            type="agentMessage",
            phase=SimpleNamespace(value="final_answer"),
            text='{"answer": "42"}',
        )
        usage = SimpleNamespace(
            total=SimpleNamespace(
                input_tokens=3,
                output_tokens=4,
                total_tokens=7,
                cached_input_tokens=1,
            )
        )
        events = [
            SimpleNamespace(method="turn/started", payload=SimpleNamespace()),
            SimpleNamespace(
                method="item/reasoning/textDelta",
                payload=SimpleNamespace(delta="thinking"),
            ),
            SimpleNamespace(
                method="item/agentMessage/delta",
                payload=SimpleNamespace(delta='{"answer": "42"}'),
            ),
            SimpleNamespace(
                method="item/completed",
                payload=SimpleNamespace(turn_id="turn-1", item=agent_message),
            ),
            SimpleNamespace(
                method="thread/tokenUsage/updated",
                payload=SimpleNamespace(turn_id="turn-1", token_usage=usage),
            ),
            SimpleNamespace(
                method="turn/completed",
                payload=SimpleNamespace(turn=SimpleNamespace(status=SimpleNamespace(value="completed"))),
            ),
        ]
        return _FakeTurn(events, event_delay=self.event_delay)

    async def run(self, input_text: str, **kwargs: Any) -> Any:
        _FakeThread.last_turn_kwargs = {"input": input_text, **kwargs}
        return SimpleNamespace(final_response="dialog response")


class _FakeAsyncCodex:
    last_thread_start_kwargs: dict[str, Any] = {}
    last_thread_resume_kwargs: dict[str, Any] = {}
    account_response: Any = SimpleNamespace(requires_openai_auth=False)

    def __init__(self, config: Any | None = None) -> None:
        self.config = config

    async def __aenter__(self) -> _FakeAsyncCodex:
        return self

    async def __aexit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        return None

    async def account(self, *, refresh_token: bool = False) -> Any:
        return self.account_response

    async def models(self) -> Any:
        return SimpleNamespace(
            models=[
                SimpleNamespace(
                    id="gpt-5.4",
                    model="gpt-5.4",
                    supported_reasoning_efforts=[
                        SimpleNamespace(reasoning_effort=SimpleNamespace(value="low")),
                        SimpleNamespace(reasoning_effort=SimpleNamespace(value="medium")),
                        SimpleNamespace(reasoning_effort=SimpleNamespace(value="high")),
                        SimpleNamespace(reasoning_effort=SimpleNamespace(value="xhigh")),
                    ],
                )
            ]
        )

    async def thread_start(self, **kwargs: Any) -> _FakeThread:
        _FakeAsyncCodex.last_thread_start_kwargs = kwargs
        return _FakeThread()

    async def thread_resume(self, thread_id: str, **kwargs: Any) -> _FakeThread:
        _FakeAsyncCodex.last_thread_resume_kwargs = {"thread_id": thread_id, **kwargs}
        return _FakeThread(thread_id)


@pytest.fixture(autouse=True)
def fake_codex_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAsyncCodex.account_response = SimpleNamespace(requires_openai_auth=False)
    _FakeThread.event_delay = 0.0
    monkeypatch.setattr(codex_module, "CODEX_SDK_AVAILABLE", True)
    monkeypatch.setattr(codex_module, "AsyncCodex", _FakeAsyncCodex)
    monkeypatch.setattr(codex_module, "CodexConfig", _FakeCodexConfig)
    monkeypatch.setattr(codex_module, "CodexReasoningEffort", _FakeReasoningEffort)
    monkeypatch.setattr(codex_module, "Sandbox", _FakeSandbox)
    monkeypatch.setattr(codex_module, "ApprovalMode", _FakeApprovalMode)


@pytest.mark.asyncio
async def test_execute_uses_native_output_schema_and_tracks_usage() -> None:
    provider = CodexProvider(model="gpt-5.4", default_reasoning_effort="high")
    agent = AgentDef(
        name="answerer",
        prompt="answer",
        output={"answer": OutputField(type="string")},
    )
    events: list[tuple[str, dict[str, Any]]] = []

    result = await provider.execute(
        agent=agent,
        context={},
        rendered_prompt="answer",
        event_callback=lambda event_type, data: events.append((event_type, data)),
    )

    assert result.content == {"answer": "42"}
    assert result.tokens_used == 7
    assert result.input_tokens == 3
    assert result.output_tokens == 4
    assert result.cache_read_tokens == 1
    assert result.model == "gpt-5.4"
    assert provider.get_session_ids() == {"answerer": "thread-new"}
    assert _FakeThread.last_turn_kwargs["effort"] == "high"
    assert _FakeThread.last_turn_kwargs["output_schema"]["properties"]["answer"]["type"] == "string"
    assert ("agent_turn_start", {"turn": "awaiting_model"}) in events
    assert any(event_type == "agent_reasoning" for event_type, _ in events)
    assert any(event_type == "agent_message" for event_type, _ in events)


@pytest.mark.asyncio
async def test_execute_does_not_cancel_codex_stream_while_polling_interrupts() -> None:
    _FakeThread.event_delay = 0.3
    provider = CodexProvider(model="gpt-5.4", default_reasoning_effort="medium")
    agent = AgentDef(
        name="answerer",
        prompt="answer",
        output={"answer": OutputField(type="string")},
    )

    result = await provider.execute(agent=agent, context={}, rendered_prompt="answer")

    assert result.content == {"answer": "42"}


@pytest.mark.asyncio
async def test_resume_thread_id_is_used() -> None:
    provider = CodexProvider(model="gpt-5.4")
    provider.set_resume_session_ids({"answerer": "thread-old"})
    agent = AgentDef(name="answerer", prompt="answer")

    result = await provider.execute(agent=agent, context={}, rendered_prompt="answer")

    assert result.content == {"result": '{"answer": "42"}'}
    assert _FakeAsyncCodex.last_thread_resume_kwargs["thread_id"] == "thread-old"
    assert provider.get_session_ids() == {"answerer": "thread-old"}


def test_mcp_config_translates_agent_tool_filter() -> None:
    provider = CodexProvider(
        model="gpt-5.4",
        mcp_servers={
            "docs": {
                "type": "stdio",
                "command": "docs-server",
                "args": ["--stdio"],
                "tools": ["search", "read"],
                "timeout": 2000,
            }
        },
    )
    agent = AgentDef(name="researcher", prompt="hi", tools=["docs__search"])

    config = provider._codex_config_for_agent(agent, ["docs__search"])

    assert config == {
        "mcp_servers": {
            "docs": {
                "command": "docs-server",
                "args": ["--stdio"],
                "startup_timeout_sec": 2,
                "tool_timeout_sec": 2,
                "enabled_tools": ["search"],
            }
        }
    }


@pytest.mark.asyncio
async def test_validate_connection_uses_account_state() -> None:
    provider = CodexProvider(model="gpt-5.4")

    assert await provider.validate_connection() is True


@pytest.mark.asyncio
async def test_validate_connection_accepts_chatgpt_account_requiring_openai_auth() -> None:
    _FakeAsyncCodex.account_response = SimpleNamespace(
        account=SimpleNamespace(email="user@example.com"),
        requires_openai_auth=True,
    )
    provider = CodexProvider(model="gpt-5.4")

    assert await provider.validate_connection() is True


@pytest.mark.asyncio
async def test_execute_dialog_turn() -> None:
    provider = CodexProvider(model="gpt-5.4")

    result = await provider.execute_dialog_turn(
        system_prompt="be brief",
        user_message="hello",
        history=[{"role": "assistant", "content": "previous"}],
    )

    assert result == "dialog response"
    assert "assistant: previous" in _FakeThread.last_turn_kwargs["input"]
