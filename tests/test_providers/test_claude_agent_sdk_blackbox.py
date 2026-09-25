"""Black-box regressions for the ``claude-agent-sdk`` provider's SDK session.

Nothing here fakes the SDK. The provider talks to the real ``ClaudeSDKClient``,
``Query`` and ``parse_message``; only the process boundary is replaced, by a
real :class:`claude_agent_sdk.Transport` (``FakeTransport``). That is what makes
the failure these tests exist for visible at all: with the previous ``query()``
entry point the SDK runs its *own* teardown inside the ``__anext__()`` the
provider is awaiting, so pre-empting a read could cut the CLI's shutdown short.
A test whose fake models teardown inside the fake cannot see that.

No real ``claude`` process is spawned and no credential is read.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

pytest.importorskip(
    "claude_agent_sdk",
    reason="claude-agent-sdk extra not installed (pip install conductor[claude-agent-sdk])",
)

from claude_agent_sdk import CLIConnectionError  # noqa: E402

from conductor.config.schema import AgentDef  # noqa: E402
from conductor.exceptions import ProviderError  # noqa: E402
from conductor.providers.claude_agent_sdk import ClaudeAgentSdkProvider  # noqa: E402
from tests.test_providers.claude_sdk_harness import (  # noqa: E402
    HANG_GUARD_SECONDS,
    FakeTransport,
    assistant_frame,
    gated_scope,
    malformed_assistant_frame,
    patch_sdk_entry,
    result_frame,
    settle,
    trace_mcp_removal,
)

_AGENT = AgentDef(name="t", prompt="hi")
_SERVERS = {"docs": {"type": "stdio", "command": "docs-server"}}


def _start(provider: ClaudeAgentSdkProvider, **kwargs) -> asyncio.Task:
    return asyncio.create_task(
        provider.execute(agent=_AGENT, context={}, rendered_prompt="hi", **kwargs)
    )


class TestRealSdkSanity:
    """The harness itself drives a real session end to end."""

    async def test_a_normal_response_completes_through_the_real_sdk(self) -> None:
        transport = FakeTransport()
        transport.reply_with(assistant_frame("hello there"), result_frame(result="hello there"))

        with patch_sdk_entry(transport):
            provider = ClaudeAgentSdkProvider()
            output = await asyncio.wait_for(_start(provider), HANG_GUARD_SECONDS)

        assert output.partial is False
        assert "hello there" in str(output.content)
        # The SDK negotiated the control protocol and sent our prompt verbatim.
        assert "initialize" in transport.control_requests
        assert transport.user_messages[0]["message"]["content"] == "hi"
        # Shutdown ran to completion, so the transport is not left alive.
        assert transport.close_completed == 1
        assert transport.alive is False

    async def test_interrupt_pre_empts_a_blocked_real_read(self) -> None:
        """The stream simply never answers; the interrupt must still land."""
        before = asyncio.all_tasks()
        transport = FakeTransport()
        interrupt = asyncio.Event()

        async with gated_scope(before) as scope:
            scope.watch(transport)
            with patch_sdk_entry(transport):
                provider = ClaudeAgentSdkProvider()
                execution = scope.track(_start(provider, interrupt_signal=interrupt))
                await asyncio.wait_for(
                    _wait_for(lambda: transport.user_messages), HANG_GUARD_SECONDS
                )

                interrupt.set()
                output = await asyncio.wait_for(execution, HANG_GUARD_SECONDS)

        assert output.partial is True
        assert transport.close_completed == 1
        assert transport.alive is False


class TestSdkInitiatedTeardownIsNeverCancelled:
    """F1: the SDK can already be shutting down when a signal arrives.

    A malformed frame makes the SDK end the stream itself. On the ``query()``
    path that teardown runs inside the provider's in-flight ``__anext__()``, so
    cancelling that read cancels ``transport.close()`` half-way -- the child is
    discarded from the SDK's registry while still alive and no later
    ``aclose()`` can restart cleanup.
    """

    @pytest.mark.parametrize("signal", ["interrupt", "deadline", "outer_cancel"])
    async def test_a_signal_during_sdk_shutdown_never_cancels_close(self, signal: str) -> None:
        before = asyncio.all_tasks()
        transport = FakeTransport()
        # One shared trace, so the ordering between the SDK's own shutdown and
        # the removal of the credentials file is asserted rather than assumed.
        trace = transport.trace
        transport.close_gate.clear()  # park the SDK inside its own teardown
        transport.reply_with(malformed_assistant_frame())
        interrupt = asyncio.Event()

        async with gated_scope(before) as scope:
            scope.watch(transport)
            with patch_sdk_entry(transport), trace_mcp_removal(trace):
                provider = ClaudeAgentSdkProvider(
                    mcp_servers=_SERVERS,
                    max_session_seconds=0.2 if signal == "deadline" else None,
                )
                execution = scope.track(_start(provider, interrupt_signal=interrupt))

                # Wait until the SDK is *inside* close(), which is the only
                # moment this regression is about.
                await asyncio.wait_for(transport.close_started.wait(), HANG_GUARD_SECONDS)

                if signal == "interrupt":
                    interrupt.set()
                elif signal == "outer_cancel":
                    execution.cancel()
                    await asyncio.sleep(0)
                    execution.cancel()  # repeated cancellation must also be absorbed
                else:
                    await asyncio.sleep(0.25)  # let the absolute deadline pass

                await settle()

                # The signal must not have reached the SDK's shutdown.
                assert transport.close_cancelled == 0
                assert not execution.done()

                transport.close_gate.set()
                with pytest.raises((ProviderError, asyncio.CancelledError)):
                    await asyncio.wait_for(execution, HANG_GUARD_SECONDS)

        assert transport.close_cancelled == 0
        assert transport.close_completed == 1
        # Shutdown finished, so the transport is not left alive behind us...
        assert transport.alive is False
        # ...and only then was the MCP configuration removed.
        assert trace[-2:] == ["close_completed", "remove_mcp_config"]


class TestConnectIsNeverCancelled:
    """The spawn window: between process spawn and ``Query`` creation there is
    no public cleanup handle, so ``connect()`` is never cancelled by a signal."""

    @pytest.mark.parametrize("signal", ["interrupt", "deadline", "outer_cancel"])
    async def test_a_signal_during_connect_waits_for_connect_to_finish(self, signal: str) -> None:
        before = asyncio.all_tasks()
        transport = FakeTransport()
        transport.connect_gate.clear()
        transport.reply_with(assistant_frame("late"), result_frame(result="late"))
        interrupt = asyncio.Event()

        async with gated_scope(before) as scope:
            scope.watch(transport)
            with patch_sdk_entry(transport):
                provider = ClaudeAgentSdkProvider(
                    max_session_seconds=0.2 if signal == "deadline" else None,
                )
                execution = scope.track(_start(provider, interrupt_signal=interrupt))
                await asyncio.wait_for(transport.connect_started.wait(), HANG_GUARD_SECONDS)

                if signal == "interrupt":
                    interrupt.set()
                elif signal == "outer_cancel":
                    execution.cancel()
                    await asyncio.sleep(0)
                    execution.cancel()
                else:
                    await asyncio.sleep(0.25)

                await settle()

                # Startup is not interruptible: the signal is honoured only once
                # connect() has finished, so the CLI is never left half-spawned.
                assert transport.connect_cancelled == 0
                assert not execution.done()

                transport.connect_gate.set()
                if signal == "outer_cancel":
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(execution, HANG_GUARD_SECONDS)
                elif signal == "deadline":
                    with pytest.raises(ProviderError, match="exceeded maximum session duration"):
                        await asyncio.wait_for(execution, HANG_GUARD_SECONDS)
                else:
                    output = await asyncio.wait_for(execution, HANG_GUARD_SECONDS)
                    assert output.partial is True

        assert transport.connect_cancelled == 0
        # Shutdown still ran, even though nothing was ever asked of the session.
        assert transport.close_completed == 1
        assert transport.alive is False

    async def test_connect_failure_is_mapped_and_shutdown_still_runs(self) -> None:
        """A failing initialize is an SDK error, not an interrupt or a timeout.

        The interrupt is raised while the SDK is still starting up, so it is
        pending when connect fails -- the pre-set case is S0's short-circuit and
        is covered separately.
        """
        before = asyncio.all_tasks()
        transport = FakeTransport(fail_initialize=True)
        transport.write_gate.clear()
        interrupt = asyncio.Event()

        async with gated_scope(before) as scope:
            scope.watch(transport)
            with patch_sdk_entry(transport):
                provider = ClaudeAgentSdkProvider()
                execution = scope.track(_start(provider, interrupt_signal=interrupt))
                await asyncio.wait_for(transport.write_started.wait(), HANG_GUARD_SECONDS)

                interrupt.set()
                await settle()
                transport.write_gate.set()

                with pytest.raises(ProviderError) as err:
                    await asyncio.wait_for(execution, HANG_GUARD_SECONDS)

        assert "initialize refused" in str(err.value)
        assert transport.connect_cancelled == 0
        assert transport.close_completed >= 1
        assert transport.alive is False


class TestOwnedShutdownRetry:
    """``disconnect()`` is retried once, sequentially, when it did not return."""

    async def test_a_failing_first_shutdown_is_retried_and_recovers(self) -> None:
        before = asyncio.all_tasks()
        transport = FakeTransport(close_failures=[CLIConnectionError("first close failed")])
        transport.reply_with(assistant_frame("hi there"), result_frame(result="hi there"))

        async with gated_scope(before) as scope:
            scope.watch(transport)
            with patch_sdk_entry(transport):
                provider = ClaudeAgentSdkProvider()
                output = await asyncio.wait_for(_start(provider), HANG_GUARD_SECONDS)

        # The retry re-entered the real Query.close(): the client stays
        # re-enterable because disconnect() clears _query only after close()
        # returns. That is private SDK state, which is exactly why it is pinned
        # here against the real object rather than assumed.
        assert transport.close_calls == 2
        assert transport.close_completed == 1
        assert transport.alive is False
        assert output.partial is False

    async def test_shutdown_attempts_never_overlap(self) -> None:
        """``FakeTransport.close`` asserts on re-entry; both attempts are serial."""
        before = asyncio.all_tasks()
        transport = FakeTransport(close_failures=[RuntimeError("boom"), RuntimeError("boom")])
        transport.reply_with(assistant_frame("x"), result_frame(result="x"))

        async with gated_scope(before) as scope:
            scope.watch(transport)
            with patch_sdk_entry(transport):
                provider = ClaudeAgentSdkProvider()
                with pytest.raises(ProviderError, match="could not be confirmed shut down") as err:
                    await asyncio.wait_for(_start(provider), HANG_GUARD_SECONDS)

        assert transport.close_calls == 2
        assert transport.close_completed == 0
        # The result is discarded rather than reported, and the cause is kept.
        assert err.value.is_retryable is False
        assert isinstance(err.value.__cause__, RuntimeError)


async def _wait_for(predicate) -> None:
    while not predicate():
        await asyncio.sleep(0)


class TestSendIsCancellable:
    """Unlike connect, the prompt write is safe to pre-empt: the session already
    exists, so ``disconnect()`` still owns every resource behind it."""

    @pytest.mark.parametrize("signal", ["interrupt", "deadline", "outer_cancel", "repeated_cancel"])
    async def test_a_signal_during_send_cancels_the_write_and_still_shuts_down(
        self, signal: str
    ) -> None:
        before = asyncio.all_tasks()
        transport = FakeTransport()
        trace = transport.trace
        transport.prompt_gate.clear()
        interrupt = asyncio.Event()

        async with gated_scope(before) as scope:
            scope.watch(transport)
            with patch_sdk_entry(transport), trace_mcp_removal(trace):
                provider = ClaudeAgentSdkProvider(
                    mcp_servers=_SERVERS,
                    max_session_seconds=0.2 if signal == "deadline" else None,
                )
                execution = scope.track(_start(provider, interrupt_signal=interrupt))
                await asyncio.wait_for(transport.prompt_write_started.wait(), HANG_GUARD_SECONDS)

                if signal == "interrupt":
                    interrupt.set()
                elif signal == "deadline":
                    await asyncio.sleep(0.25)
                else:
                    execution.cancel()
                    if signal == "repeated_cancel":
                        await asyncio.sleep(0)
                        execution.cancel()

                if signal in ("outer_cancel", "repeated_cancel"):
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(execution, HANG_GUARD_SECONDS)
                elif signal == "deadline":
                    with pytest.raises(ProviderError, match="exceeded maximum session duration"):
                        await asyncio.wait_for(execution, HANG_GUARD_SECONDS)
                else:
                    output = await asyncio.wait_for(execution, HANG_GUARD_SECONDS)
                    assert output.partial is True

        # The write was pre-empted, and shutdown still completed afterwards.
        assert transport.write_cancelled == 1
        assert transport.close_completed == 1
        assert transport.alive is False
        assert trace[-2:] == ["close_completed", "remove_mcp_config"]

    async def test_a_failing_send_is_mapped_to_a_provider_error(self) -> None:
        before = asyncio.all_tasks()

        class _Boom(FakeTransport):
            async def write(self, data: str) -> None:
                if '"control_request"' in data:
                    await super().write(data)
                    return
                raise CLIConnectionError("stdin is gone")

        transport = _Boom()

        async with gated_scope(before) as scope:
            scope.watch(transport)
            with patch_sdk_entry(transport):
                provider = ClaudeAgentSdkProvider()
                with pytest.raises(ProviderError, match="stdin is gone"):
                    await asyncio.wait_for(_start(provider), HANG_GUARD_SECONDS)

        assert transport.close_completed == 1
        assert transport.alive is False


class TestSdkPropertiesThisDesignRestsOn:
    """Characterization of the SDK itself, so a change there fails a test here
    instead of silently degrading the provider."""

    async def test_disconnect_works_from_a_task_other_than_the_one_that_connected(
        self,
    ) -> None:
        """The provider connects and disconnects from two different owned tasks."""
        import claude_agent_sdk

        transport = FakeTransport()
        client = claude_agent_sdk.ClaudeSDKClient(
            claude_agent_sdk.ClaudeAgentOptions(), transport=transport
        )

        connector = asyncio.create_task(client.connect())
        await asyncio.wait_for(connector, HANG_GUARD_SECONDS)

        disconnector = asyncio.create_task(client.disconnect())
        await asyncio.wait_for(disconnector, HANG_GUARD_SECONDS)

        assert connector is not disconnector
        assert transport.close_completed == 1
        assert transport.alive is False

    async def test_cancelling_connect_leaves_no_way_to_close_the_transport(self) -> None:
        """Why connect is never cancelled.

        Parked at the spawn step, the client has a transport but no ``Query``,
        and ``disconnect()`` only closes through ``_query`` -- so a cancellation
        here closes nothing. With a real subprocess that is an orphaned CLI.
        """
        import claude_agent_sdk

        transport = FakeTransport()
        transport.connect_gate.clear()
        client = claude_agent_sdk.ClaudeSDKClient(
            claude_agent_sdk.ClaudeAgentOptions(), transport=transport
        )

        async with gated_scope() as scope:
            scope.watch(transport)
            connector = scope.track(asyncio.create_task(client.connect()))
            await asyncio.wait_for(transport.connect_started.wait(), HANG_GUARD_SECONDS)

            connector.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(connector, HANG_GUARD_SECONDS)

            # Nothing was closed: the SDK's own failure-path disconnect() had no
            # handle to close through.
            assert transport.close_calls == 0


class TestShutdownFailureNeverReplacesTheOutcome:
    """A shutdown that could not be confirmed is reported, not substituted --
    except on a run that would otherwise be a success, where reporting one
    while the CLI may still be alive would be the lie."""

    @staticmethod
    def _always_failing() -> FakeTransport:
        return FakeTransport(
            close_failures=[RuntimeError("close failed"), RuntimeError("close failed again")]
        )

    async def test_an_interrupted_run_still_returns_its_partial_output(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        before = asyncio.all_tasks()
        transport = self._always_failing()
        interrupt = asyncio.Event()

        async with gated_scope(before) as scope:
            scope.watch(transport)
            with patch_sdk_entry(transport):
                provider = ClaudeAgentSdkProvider()
                execution = scope.track(_start(provider, interrupt_signal=interrupt))
                await asyncio.wait_for(transport.prompt_write_started.wait(), HANG_GUARD_SECONDS)
                interrupt.set()
                with caplog.at_level(logging.ERROR, logger="conductor.providers.claude_agent_sdk"):
                    output = await asyncio.wait_for(execution, HANG_GUARD_SECONDS)

        assert output.partial is True  # the outcome is untouched
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors and errors[-1].exc_info is not None
        assert transport.close_calls == 2

    async def test_a_timed_out_run_still_raises_its_timeout(self) -> None:
        before = asyncio.all_tasks()
        transport = self._always_failing()

        async with gated_scope(before) as scope:
            scope.watch(transport)
            with patch_sdk_entry(transport):
                provider = ClaudeAgentSdkProvider(max_session_seconds=0.05)
                with pytest.raises(ProviderError, match="exceeded maximum session duration"):
                    await asyncio.wait_for(_start(provider), HANG_GUARD_SECONDS)

        assert transport.close_calls == 2

    async def test_a_cancelled_run_still_raises_cancelled_error(self) -> None:
        before = asyncio.all_tasks()
        transport = self._always_failing()

        async with gated_scope(before) as scope:
            scope.watch(transport)
            with patch_sdk_entry(transport):
                provider = ClaudeAgentSdkProvider()
                execution = scope.track(_start(provider))
                await asyncio.wait_for(transport.prompt_write_started.wait(), HANG_GUARD_SECONDS)
                execution.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(execution, HANG_GUARD_SECONDS)

        assert transport.close_calls == 2


class TestDoubleShutdownFailureReportsTheBrokenOrdering:
    """When shutdown cannot be confirmed, the MCP configuration is removed
    anyway -- resolved credentials must not sit on disk indefinitely -- but that
    inverts the ordering the provider otherwise guarantees, so it is stated
    rather than left to be inferred from two earlier warnings."""

    _ORDERING = "shutdown-before-config-removal ordering could not be honored"

    @staticmethod
    def _doubly_failing() -> FakeTransport:
        return FakeTransport(
            close_failures=[RuntimeError("close failed"), RuntimeError("close failed again")]
        )

    @staticmethod
    def _ordering_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
        return [
            r
            for r in caplog.records
            if r.levelno >= logging.ERROR
            and TestDoubleShutdownFailureReportsTheBrokenOrdering._ORDERING in r.getMessage()
        ]

    async def test_an_interrupted_run_keeps_its_outcome_and_reports_the_ordering(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        before = asyncio.all_tasks()
        transport = self._doubly_failing()
        trace = transport.trace  # one shared list, so ordering is asserted
        interrupt = asyncio.Event()

        async with gated_scope(before) as scope:
            scope.watch(transport)
            with patch_sdk_entry(transport), trace_mcp_removal(trace):
                provider = ClaudeAgentSdkProvider(mcp_servers=_SERVERS)
                execution = scope.track(_start(provider, interrupt_signal=interrupt))
                await asyncio.wait_for(
                    _wait_for(lambda: transport.user_messages), HANG_GUARD_SECONDS
                )
                interrupt.set()
                with caplog.at_level(logging.ERROR, logger="conductor.providers.claude_agent_sdk"):
                    output = await asyncio.wait_for(execution, HANG_GUARD_SECONDS)

            # The primary outcome is untouched: a failed shutdown is reported,
            # never substituted.
            assert output.partial is True

            # Exactly two attempts, neither overlapping the other --
            # ``FakeTransport.close`` asserts on re-entry, and neither returned.
            assert transport.close_calls == 2
            assert transport.close_completed == 0

            # The config was removed only after the second failure.
            assert trace[-3:] == ["close_failed", "close_failed", "remove_mcp_config"]

            records = self._ordering_records(caplog)
            assert records, "the broken ordering was never reported"
            record = records[-1]
            assert record.exc_info is not None, "logged without exc_info, so the cause is lost"
            message = record.getMessage()
            assert "2 of 2 attempts" in message
            assert "phase=disconnect" in message
            assert "provider=claude-agent-sdk" in message
            assert "'t'" in message  # the agent name
            # Nothing sensitive: no config path, no prompt, no environment.
            assert ".json" not in message
            assert "docs-server" not in message

    async def test_an_otherwise_successful_run_still_raises_and_reports_the_ordering(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        before = asyncio.all_tasks()
        transport = self._doubly_failing()
        trace = transport.trace
        transport.reply_with(assistant_frame("all done"), result_frame(result="all done"))

        async with gated_scope(before) as scope:
            scope.watch(transport)
            with patch_sdk_entry(transport), trace_mcp_removal(trace):
                provider = ClaudeAgentSdkProvider(mcp_servers=_SERVERS)
                with (
                    caplog.at_level(logging.ERROR, logger="conductor.providers.claude_agent_sdk"),
                    pytest.raises(ProviderError, match="could not be confirmed shut down") as err,
                ):
                    await asyncio.wait_for(_start(provider), HANG_GUARD_SECONDS)

            # Policy unchanged: the result is discarded rather than reported.
            assert err.value.is_retryable is False
            assert isinstance(err.value.__cause__, RuntimeError)

            assert transport.close_calls == 2
            assert transport.close_completed == 0
            assert trace[-3:] == ["close_failed", "close_failed", "remove_mcp_config"]

            records = self._ordering_records(caplog)
            assert records, "the broken ordering was never reported"
            assert records[-1].exc_info is not None

    async def test_a_confirmed_shutdown_reports_no_ordering_problem(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Negative control: the ordinary path must stay quiet, or the log above
        would be noise a reader learns to ignore."""
        before = asyncio.all_tasks()
        transport = FakeTransport()
        trace = transport.trace
        transport.reply_with(assistant_frame("fine"), result_frame(result="fine"))

        async with gated_scope(before) as scope:
            scope.watch(transport)
            with patch_sdk_entry(transport), trace_mcp_removal(trace):
                provider = ClaudeAgentSdkProvider(mcp_servers=_SERVERS)
                with caplog.at_level(logging.ERROR, logger="conductor.providers.claude_agent_sdk"):
                    output = await asyncio.wait_for(_start(provider), HANG_GUARD_SECONDS)

            assert output.partial is False
            assert trace[-2:] == ["close_completed", "remove_mcp_config"]
            assert self._ordering_records(caplog) == []
