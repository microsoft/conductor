"""Tests for mid-agent interrupt support in the Copilot provider.

Tests cover:
- Partial output returned when interrupt_signal fires during _send_and_wait
- Session abort via session.abort() method
- Session abort via raw RPC fallback
- Graceful fallback when abort is unavailable
- Post-abort event handling (idle, error, timeout)
- Partial content captured correctly
- Session kept alive for follow-up after interrupt
- send_followup() sends guidance and disconnects session
- AgentOutput.partial flag propagation
"""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor.config.schema import AgentDef, OutputField
from conductor.providers.base import AgentOutput
from conductor.providers.copilot import CopilotProvider, SDKResponse


class FakeEvent:
    """Fake SDK event for testing."""

    def __init__(self, event_type: str, data: Any = None) -> None:
        self.type = MagicMock()
        self.type.value = event_type
        self.data = data or MagicMock()


class FakeSession:
    """Fake Copilot SDK session for testing interrupt behavior."""

    def __init__(
        self,
        response_content: str = '{"result": "test response"}',
        has_abort: bool = True,
        abort_raises: bool = False,
        has_rpc: bool = False,
        rpc_raises: bool = False,
        post_abort_event: str = "session.idle",
        post_abort_delay: float = 0.0,
        done_event: asyncio.Event | None = None,
    ) -> None:
        self._response_content = response_content
        self._has_abort = has_abort
        self._abort_raises = abort_raises
        self._has_rpc = has_rpc
        self._rpc_raises = rpc_raises
        self._post_abort_event = post_abort_event
        self._post_abort_delay = post_abort_delay
        self._callback: Any = None
        self._disconnected = False
        self._abort_called = False
        self._rpc_called = False
        self.session_id = "test-session-id"
        self._done_event = done_event

        if has_abort:
            self.abort = AsyncMock(side_effect=self._do_abort)
        if has_rpc:
            self.rpc = AsyncMock(side_effect=self._do_rpc)

    def on(self, callback: Any) -> None:
        self._callback = callback

    async def send(self, data: Any) -> None:
        """Simulate sending a prompt and producing events."""
        # Schedule events to be delivered after a small delay
        asyncio.get_event_loop().call_soon(self._deliver_message)

    def _deliver_message(self) -> None:
        if self._callback:
            msg_data = MagicMock()
            msg_data.content = self._response_content
            self._callback(FakeEvent("assistant.message", msg_data))

    async def _do_abort(self) -> None:
        self._abort_called = True
        if self._abort_raises:
            raise RuntimeError("abort failed")
        # Schedule post-abort event
        asyncio.get_event_loop().call_later(self._post_abort_delay, self._deliver_post_abort)

    async def _do_rpc(self, method: str, params: dict) -> None:
        self._rpc_called = True
        if self._rpc_raises:
            raise RuntimeError("RPC failed")
        asyncio.get_event_loop().call_later(self._post_abort_delay, self._deliver_post_abort)

    def _deliver_post_abort(self) -> None:
        if self._callback:
            self._callback(FakeEvent(self._post_abort_event))
        if self._done_event is not None:
            self._done_event.set()

    async def disconnect(self) -> None:
        self._disconnected = True


@pytest.fixture
def agent_with_output() -> AgentDef:
    """Agent definition with output schema."""
    return AgentDef(
        name="test_agent",
        model="gpt-4",
        prompt="Test prompt",
        output={"result": OutputField(type="string")},
    )


@pytest.fixture
def agent_no_output() -> AgentDef:
    """Agent definition without output schema."""
    return AgentDef(
        name="test_agent",
        model="gpt-4",
        prompt="Test prompt",
    )


class TestSendAndWaitWithInterrupt:
    """Tests for interrupt handling in _send_and_wait."""

    @pytest.mark.asyncio
    async def test_normal_completion_without_interrupt(self) -> None:
        """When interrupt_signal is not set, _send_and_wait completes normally."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "test"})
        session = FakeSession(response_content="normal response")

        # Override to deliver idle event
        original_send = session.send

        async def send_with_idle(data: Any) -> None:
            await original_send(data)
            await asyncio.sleep(0.01)
            session._callback(FakeEvent("session.idle"))

        session.send = send_with_idle

        interrupt = asyncio.Event()  # Not set
        result = await provider._send_and_wait(
            session, "test prompt", False, False, interrupt_signal=interrupt
        )

        assert result.content == "normal response"
        assert result.partial is False

    @pytest.mark.asyncio
    async def test_interrupt_returns_partial(self) -> None:
        """When interrupt fires, _send_and_wait returns partial response."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "test"})
        session = FakeSession(
            response_content="partial response",
            has_abort=True,
            post_abort_event="session.idle",
        )

        interrupt = asyncio.Event()

        # Set interrupt before sending so it fires immediately
        interrupt.set()

        result = await provider._send_and_wait(
            session, "test prompt", False, False, interrupt_signal=interrupt
        )

        assert result.partial is True
        assert result.content == "partial response"
        assert not interrupt.is_set()  # Signal should be cleared

    @pytest.mark.asyncio
    async def test_no_interrupt_signal_uses_idle_detection(self) -> None:
        """When interrupt_signal is None, uses idle detection path."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "test"})
        session = FakeSession(response_content="normal response")

        original_send = session.send

        async def send_with_idle(data: Any) -> None:
            await original_send(data)
            await asyncio.sleep(0.01)
            session._callback(FakeEvent("session.idle"))

        session.send = send_with_idle

        result = await provider._send_and_wait(
            session, "test prompt", False, False, interrupt_signal=None
        )

        assert result.content == "normal response"
        assert result.partial is False


class TestAbortSession:
    """Tests for session abort behavior."""

    @pytest.mark.asyncio
    async def test_abort_via_method(self) -> None:
        """Abort is called via session.abort() when available."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})

        done = asyncio.Event()
        session = FakeSession(has_abort=True, post_abort_event="session.idle", done_event=done)

        await provider._abort_session(session, done)

        session.abort.assert_awaited_once()
        assert provider._abort_supported is True

    @pytest.mark.asyncio
    async def test_abort_fallback_to_rpc(self) -> None:
        """Falls back to raw RPC when session.abort() fails."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})

        done = asyncio.Event()
        session = FakeSession(
            has_abort=True,
            abort_raises=True,
            has_rpc=True,
            post_abort_event="session.idle",
            done_event=done,
        )

        await provider._abort_session(session, done)

        session.abort.assert_awaited_once()
        session.rpc.assert_awaited_once_with("session/abort", {})
        assert provider._abort_supported is True

    @pytest.mark.asyncio
    async def test_abort_rpc_only(self) -> None:
        """Uses RPC when session.abort() method not available."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})

        done = asyncio.Event()
        session = FakeSession(
            has_abort=False,
            has_rpc=True,
            post_abort_event="session.idle",
            done_event=done,
        )

        await provider._abort_session(session, done)

        assert not hasattr(session, "abort") or not session._abort_called
        session.rpc.assert_awaited_once_with("session/abort", {})
        assert provider._abort_supported is True

    @pytest.mark.asyncio
    async def test_abort_unavailable(self) -> None:
        """Graceful fallback when no abort capability exists."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        session = FakeSession(has_abort=False, has_rpc=False)

        done = asyncio.Event()
        await provider._abort_session(session, done)

        assert provider._abort_supported is False

    @pytest.mark.asyncio
    async def test_post_abort_error_event(self) -> None:
        """Post-abort error event is handled gracefully."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        session = FakeSession(
            has_abort=True,
            post_abort_event="error",
        )
        # Register a callback (normally done by _send_and_wait)
        session.on(lambda event: None)

        done = asyncio.Event()

        # Override abort to set done immediately (simulating post-abort event)
        async def abort_and_set_done() -> None:
            session._abort_called = True
            done.set()

        session.abort = AsyncMock(side_effect=abort_and_set_done)
        await provider._abort_session(session, done)

        assert provider._abort_supported is True

    @pytest.mark.asyncio
    async def test_post_abort_timeout(self) -> None:
        """Post-abort waits up to 5 seconds then continues."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        session = FakeSession(has_abort=True)

        # Don't deliver any post-abort events
        async def abort_no_event() -> None:
            session._abort_called = True

        session.abort = AsyncMock(side_effect=abort_no_event)

        done = asyncio.Event()

        # Patch wait_for to raise TimeoutError while properly closing the coroutine
        with patch("conductor.providers.copilot.asyncio.wait_for") as mock_wait:

            async def close_coro_and_raise(coro: Any, **kwargs: Any) -> None:
                coro.close()
                raise TimeoutError()

            mock_wait.side_effect = close_coro_and_raise
            await provider._abort_session(session, done)

        assert provider._abort_supported is True


class TestPartialOutputPropagation:
    """Tests for partial output flag propagation through the provider stack."""

    @pytest.mark.asyncio
    async def test_execute_returns_partial_output(self) -> None:
        """CopilotProvider.execute() returns AgentOutput with partial=True
        when mock handler is used (mock path doesn't support interrupts,
        so this tests the flag on AgentOutput dataclass)."""
        output = AgentOutput(
            content={"result": "partial"},
            raw_response="partial",
            partial=True,
        )
        assert output.partial is True

    def test_agent_output_default_not_partial(self) -> None:
        """AgentOutput.partial defaults to False."""
        output = AgentOutput(content={"result": "full"}, raw_response="full")
        assert output.partial is False

    def test_sdk_response_partial_flag(self) -> None:
        """SDKResponse.partial flag works correctly."""
        response = SDKResponse(content="test", partial=True)
        assert response.partial is True

        response_normal = SDKResponse(content="test")
        assert response_normal.partial is False


class TestInterruptedSessionHandling:
    """Tests for interrupted session lifecycle."""

    def test_get_interrupted_session_returns_and_clears(self) -> None:
        """get_interrupted_session returns the session and clears it."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        fake_session = MagicMock()
        provider._interrupted_session = fake_session

        result = provider.get_interrupted_session()
        assert result is fake_session
        assert provider._interrupted_session is None

    def test_get_interrupted_session_returns_none_when_empty(self) -> None:
        """get_interrupted_session returns None when no session is stored."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        result = provider.get_interrupted_session()
        assert result is None


class TestSendFollowup:
    """Tests for send_followup() method."""

    @pytest.mark.asyncio
    async def test_send_followup_sends_guidance(self) -> None:
        """send_followup sends guidance and returns AgentOutput."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        session = FakeSession(response_content='{"result": "followup response"}')

        original_send = session.send

        async def send_with_idle(data: Any) -> None:
            await original_send(data)
            await asyncio.sleep(0.01)
            session._callback(FakeEvent("session.idle"))

        session.send = send_with_idle

        with (
            patch("conductor.cli.app.is_verbose", return_value=False),
            patch("conductor.cli.app.is_full", return_value=False),
        ):
            result = await provider.send_followup(session, "Focus on Python 3")

        assert result.content == {"result": "followup response"}
        assert result.partial is False
        assert result.model == "gpt-4o"
        assert session._disconnected is True

    @pytest.mark.asyncio
    async def test_send_followup_disconnects_session(self) -> None:
        """send_followup always disconnects the session, even on error."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        session = FakeSession(response_content="not json")

        original_send = session.send

        async def send_with_idle(data: Any) -> None:
            await original_send(data)
            await asyncio.sleep(0.01)
            session._callback(FakeEvent("session.idle"))

        session.send = send_with_idle

        with (
            patch("conductor.cli.app.is_verbose", return_value=False),
            patch("conductor.cli.app.is_full", return_value=False),
        ):
            result = await provider.send_followup(session, "guidance text")

        # Non-JSON response should be wrapped
        assert result.content == {"result": "not json"}
        assert session._disconnected is True

    @pytest.mark.asyncio
    async def test_send_followup_on_error_disconnects_session(self) -> None:
        """send_followup disconnects session even if send fails."""
        from conductor.exceptions import ProviderError

        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        session = FakeSession()

        # Make send raise an error
        async def send_error(data: Any) -> None:
            session._callback(FakeEvent("error", MagicMock(message="test error")))
            session._callback(FakeEvent("session.idle"))

        session.send = send_error

        with (
            patch("conductor.cli.app.is_verbose", return_value=False),
            patch("conductor.cli.app.is_full", return_value=False),
            pytest.raises(ProviderError),
        ):
            await provider.send_followup(session, "guidance")

        assert session._disconnected is True


class TestExecuteSdkCallWithInterrupt:
    """Tests for interrupt handling in _execute_sdk_call (SDK path)."""

    @pytest.mark.asyncio
    async def test_mock_handler_ignores_interrupt_signal(self) -> None:
        """Mock handler path ignores interrupt_signal parameter."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "mock response"})
        agent = AgentDef(
            name="test",
            model="gpt-4",
            prompt="test",
            output={"result": OutputField(type="string")},
        )

        interrupt = asyncio.Event()
        interrupt.set()  # Set but should be ignored by mock

        output = await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="test prompt",
            interrupt_signal=interrupt,
        )

        assert output.content == {"result": "mock response"}
        assert output.partial is False

    @pytest.mark.asyncio
    async def test_execute_with_retry_propagates_partial(self) -> None:
        """_execute_with_retry propagates partial flag from SDK response."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "test"})

        # Patch _execute_sdk_call to return partial response
        partial_content = {"result": "partial data"}
        partial_response = SDKResponse(
            content=json.dumps(partial_content),
            input_tokens=100,
            output_tokens=50,
            partial=True,
        )

        agent = AgentDef(
            name="test",
            model="gpt-4",
            prompt="test",
            output={"result": OutputField(type="string")},
        )

        with patch.object(
            provider,
            "_execute_sdk_call",
            return_value=(partial_content, partial_response),
        ):
            output = await provider._execute_with_retry(
                agent,
                {},
                "test prompt",
                None,
                interrupt_signal=asyncio.Event(),
            )

        assert output.partial is True
        assert output.content == {"result": "partial data"}

    @pytest.mark.asyncio
    async def test_execute_without_interrupt_not_partial(self) -> None:
        """Normal execution without interrupt returns non-partial output."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "full response"})
        agent = AgentDef(
            name="test",
            model="gpt-4",
            prompt="test",
            output={"result": OutputField(type="string")},
        )

        output = await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="test prompt",
            interrupt_signal=None,
        )

        assert output.partial is False
        assert output.content == {"result": "full response"}


class TestAbortCapabilityDetection:
    """Tests for runtime abort capability detection."""

    @pytest.mark.asyncio
    async def test_abort_supported_flag_set_on_success(self) -> None:
        """_abort_supported is True after successful abort."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})

        done = asyncio.Event()
        session = FakeSession(has_abort=True, post_abort_event="session.idle", done_event=done)

        await provider._abort_session(session, done)

        assert provider._abort_supported is True

    @pytest.mark.asyncio
    async def test_abort_supported_flag_false_when_unavailable(self) -> None:
        """_abort_supported is False when no abort capability exists."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        session = FakeSession(has_abort=False, has_rpc=False)

        done = asyncio.Event()
        await provider._abort_session(session, done)

        assert provider._abort_supported is False

    def test_abort_supported_initially_none(self) -> None:
        """_abort_supported starts as None (unknown)."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        assert provider._abort_supported is None

    @pytest.mark.asyncio
    async def test_abort_skipped_when_previously_unsupported(self) -> None:
        """_abort_session returns immediately when _abort_supported is False."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        provider._abort_supported = False

        session = FakeSession(has_abort=True, done_event=asyncio.Event())
        done = asyncio.Event()

        await provider._abort_session(session, done)

        # abort should not have been called since we skipped
        assert not session._abort_called
        assert provider._abort_supported is False
