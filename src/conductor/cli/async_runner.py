"""Public stdlib loop ownership for both synchronous workflow commands."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from conductor import redaction

logger = logging.getLogger(__name__)
_CLOSE_BUDGET_SECONDS = 30.0


async def _drain_owned_loop(loop: asyncio.AbstractEventLoop) -> bool:
    """Share one additional deadline across tasks, generators and the executor."""
    deadline = loop.time() + _CLOSE_BUDGET_SECONDS
    current = asyncio.current_task()
    tasks = asyncio.all_tasks(loop) - {current}
    # Root already requested targeted teardown. Cancelling native drain/reap
    # infrastructure here would destroy the remaining chance of an actual join.
    # This quarantined path observes that existing episode, not cancel-all again.
    pending = set()
    if tasks:
        _, pending = await asyncio.wait(tasks, timeout=max(0, deadline - loop.time()))
    # Both public shutdown APIs get an attempt even if task drain used the budget.
    shutdown = {
        asyncio.create_task(loop.shutdown_asyncgens()),
        asyncio.create_task(loop.shutdown_default_executor()),
    }
    _, unfinished = await asyncio.wait(shutdown, timeout=max(0, deadline - loop.time()))
    for task in tasks | shutdown:
        if task.done() and not task.cancelled() and task.exception() is not None:
            loop.call_exception_handler(
                {"message": "Error during owned CLI loop shutdown", "exception": task.exception()}
            )
    return not pending and not unfinished


def run_owned[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Keep Runner's context/signals, bypassing its unbounded close only for quarantine.

    Only this function owns the loop. Direct engine callers retain their own
    loop and its shutdown obligations. A responsive event loop is assumed;
    closing it does not join threads, prove native release or bound interpreter exit.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("A synchronous CLI command cannot run inside a running event loop")
    from conductor.engine.execution_resolution import quarantined_secrets, release_loop_secrets

    runner = asyncio.Runner()
    loop = runner.get_loop()
    previous_handler = loop.get_exception_handler()

    def scrub(value: str) -> str:
        for _cache, redactor in quarantined_secrets(loop):
            value = redactor.scrub(value)
        current = redaction.current()
        return current.scrub(value) if current is not None else value

    def report(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        current = redaction.current()
        if not quarantined_secrets(loop) and (current is None or not current.active):
            if previous_handler is not None:
                previous_handler(_loop, context)
            else:
                loop.default_exception_handler(context)
            return

        def describe(exc: BaseException) -> str:
            text = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, BaseExceptionGroup):
                text += "; " + "; ".join(describe(child) for child in exc.exceptions)
            return text

        message = str(context.get("message", "Owned CLI loop diagnostic"))
        error = context.get("exception")
        if isinstance(error, BaseException):
            message += ": " + describe(error)
        logger.warning("%s", scrub(message))

    # Components such as the dashboard capture their original handler here.
    # Restoring it during late cleanup must restore this guard, not raw stderr.
    loop.set_exception_handler(report)
    try:
        return runner.run(coroutine)
    finally:
        secrets = quarantined_secrets(loop)
        if not secrets:
            runner.close()
        else:
            loop.set_exception_handler(report)
            try:
                joined = loop.run_until_complete(_drain_owned_loop(loop))
                joined = joined and not asyncio.all_tasks(loop)
                logger.warning(
                    "%s",
                    scrub(
                        "Closing the CLI-owned loop after managed cleanup uncertainty; "
                        + (
                            "remaining Python owners joined. "
                            if joined
                            else "some owners did not join. "
                        )
                        + "Loop close is not native release proof "
                        "or a bounded interpreter-exit guarantee."
                    ),
                )
            except BaseException as error:
                report(loop, {"message": "Owned CLI loop shutdown failed", "exception": error})
            finally:
                asyncio.set_event_loop(None)
                loop.close()
                release_loop_secrets(loop)
