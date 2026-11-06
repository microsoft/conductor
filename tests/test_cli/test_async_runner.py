"""Public stdlib owned-loop wrapper contracts."""

from __future__ import annotations

import asyncio
import contextvars

from conductor.config.environment import builtin_local_environment
from conductor.engine.execution_resolution import (
    _QUARANTINED_SECRETS,
    ExecutionResolverSession,
)


def test_owned_runner_keeps_stdlib_context_and_healthy_shutdown() -> None:
    # Requirement: healthy CLI lifetimes preserve caller context and join ordinary task teardown.
    from conductor.cli.async_runner import run_owned

    value = contextvars.ContextVar("value", default="outside")
    value.set("caller")
    closed = []

    async def main() -> str:
        assert value.get() == "caller"

        async def resource() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                closed.append(True)

        ready = asyncio.Event()

        async def start() -> None:
            ready.set()
            await resource()

        asyncio.create_task(start())
        await ready.wait()
        value.set("inside")
        return "payload"

    assert run_owned(main()) == "payload"
    assert closed == [True]
    assert value.get() == "caller"


def test_quarantined_loop_keeps_scrubber_through_last_owner_and_skips_runner_close(
    monkeypatch, caplog
) -> None:
    # Requirement: quarantined CLI shutdown never re-enters Runner.close or clears late-log secrets.
    from conductor.cli.async_runner import run_owned

    session = ExecutionResolverSession(builtin_local_environment())
    secret = "synthetic-late-secret"
    session.redactor.register((secret,))
    owners = []
    loops = []
    native_close = asyncio.Runner.close
    closes = []

    def close(runner):
        closes.append(runner)
        native_close(runner)

    monkeypatch.setattr(asyncio.Runner, "close", close)

    async def main() -> str:
        loop = asyncio.get_running_loop()
        loops.append(loop)
        # Unit input is existing root accounting, not a fabricated native release proof.
        _QUARANTINED_SECRETS.setdefault(loop, set()).add((session.secret_cache, session.redactor))
        ready = asyncio.Event()

        async def owner() -> None:
            ready.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                assert session.redactor.active
                raise RuntimeError(secret) from None

        owners.append(asyncio.create_task(owner()))
        session._track(owners[-1])
        await ready.wait()
        owners[-1].cancel()
        return "payload"

    assert run_owned(main()) == "payload"
    assert owners[0].done()
    assert loops[0].is_closed()
    assert not closes
    assert not session.redactor.active
    assert secret not in caplog.text
    assert any(record.name == "conductor.engine.execution_resolution" for record in caplog.records)


def test_task_asyncgen_executor_shutdown_share_one_deadline(monkeypatch) -> None:
    # Requirement: time spent draining tasks is deducted from both public shutdown attempts.
    from conductor.cli.async_runner import run_owned

    session = ExecutionResolverSession(builtin_local_environment())
    observed = []
    attempts = []
    real_wait = asyncio.wait
    clock = [100.0]
    finish = asyncio.Event()

    async def wait(tasks, *, timeout):
        observed.append(timeout)
        if len(observed) == 1:
            finish.set()
        result = await real_wait(tasks, timeout=0)
        clock[0] += 20
        return result

    async def main() -> None:
        loop = asyncio.get_running_loop()
        _QUARANTINED_SECRETS.setdefault(loop, set()).add((session.secret_cache, session.redactor))
        monkeypatch.setattr(loop, "time", lambda: clock[0])

        async def asyncgens() -> None:
            attempts.append("asyncgens")

        async def executor() -> None:
            attempts.append("executor")

        monkeypatch.setattr(loop, "shutdown_asyncgens", asyncgens)
        monkeypatch.setattr(loop, "shutdown_default_executor", executor)
        ready = asyncio.Event()

        async def owner() -> None:
            ready.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await finish.wait()

        task = asyncio.create_task(owner())
        await ready.wait()
        task.cancel()
        monkeypatch.setattr("conductor.cli.async_runner.asyncio.wait", wait)

    run_owned(main())
    assert observed == [30.0, 10.0]
    assert attempts == ["asyncgens", "executor"]


def test_component_restoring_initial_handler_keeps_late_diagnostics_scrubbed(
    monkeypatch, caplog
) -> None:
    # Requirement: dashboard-style handler restoration cannot bypass late-owner secret scrubbing.
    from conductor.cli import async_runner

    session = ExecutionResolverSession(builtin_local_environment())
    secret = "component-restoration-secret"
    session.redactor.register((secret,))
    finish = asyncio.Event()
    native_drain = async_runner._drain_owned_loop

    async def drain(loop):
        finish.set()
        return await native_drain(loop)

    monkeypatch.setattr(async_runner, "_drain_owned_loop", drain)

    async def main() -> None:
        loop = asyncio.get_running_loop()
        original_handler = loop.get_exception_handler()
        _QUARANTINED_SECRETS.setdefault(loop, set()).add((session.secret_cache, session.redactor))
        ready = asyncio.Event()

        async def component():
            ready.set()
            await finish.wait()
            loop.set_exception_handler(original_handler)
            loop.call_exception_handler(
                {
                    "message": "component callback",
                    "exception": ExceptionGroup(secret, [RuntimeError(secret)]),
                }
            )

        asyncio.create_task(component())
        await ready.wait()

    async_runner.run_owned(main())
    assert secret not in caplog.text
