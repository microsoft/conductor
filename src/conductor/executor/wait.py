"""Wait step execution for Conductor workflow steps.

This module provides the :class:`WaitExecutor` for ``type: wait`` agent
definitions. A wait step pauses workflow execution for a parsed duration
via :func:`asyncio.sleep`. The sleep races against an optional
``interrupt_event`` so Esc / Ctrl+G cancels an in-flight wait
immediately; workflow-level timeout enforcement is layered on top by
the engine via :meth:`LimitEnforcer.wait_for_with_timeout`.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from conductor.config.schema import MAX_WAIT_DURATION_SECONDS
from conductor.duration import parse_duration
from conductor.exceptions import ValidationError
from conductor.executor.template import TemplateRenderer


def _verbose_log(message: str, style: str = "dim") -> None:
    """Log a verbose message via the CLI run module.

    Uses a deferred import to avoid a circular import between
    executor.wait and cli.run (cli.run imports WorkflowEngine which
    imports executor modules).
    """
    from conductor.cli.run import verbose_log

    verbose_log(message, style)


if TYPE_CHECKING:
    from conductor.config.schema import AgentDef


@dataclass
class WaitOutput:
    """Result of a wait step execution.

    Attributes:
        waited_seconds: Wall-clock seconds actually slept. May be less
            than ``requested_seconds`` if an interrupt cut the sleep short.
        requested_seconds: Parsed duration value from the agent config.
        reason: The rendered ``reason`` field, or ``None`` if not set.
        interrupted: ``True`` if an interrupt signal cancelled the sleep
            before ``requested_seconds`` elapsed.
    """

    waited_seconds: float
    requested_seconds: float
    reason: str | None
    interrupted: bool


class WaitExecutor:
    """Executes wait steps via :func:`asyncio.sleep`.

    Renders the agent's ``duration`` (and optional ``reason``) Jinja2
    templates, parses the duration string, then sleeps. If an
    ``interrupt_event`` is provided, the sleep is raced against it so
    the engine's Esc/Ctrl+G handler can cancel an in-flight wait
    without waiting for the full duration.

    Example::

        executor = WaitExecutor()
        output = await executor.execute(agent, context, interrupt_event=ev)
        print(output.waited_seconds, output.interrupted)
    """

    def __init__(self) -> None:
        """Initialize the WaitExecutor with a template renderer."""
        self.renderer = TemplateRenderer()

    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        interrupt_event: asyncio.Event | None = None,
    ) -> WaitOutput:
        """Execute a wait step.

        Renders ``agent.duration`` and ``agent.reason`` with Jinja2,
        parses the duration, then sleeps. Honors ``interrupt_event``
        for early cancellation. Does NOT clear ``interrupt_event`` —
        the engine's between-step interrupt check consumes it after
        the wait returns so the user gets the normal interrupt menu.

        Args:
            agent: Agent definition with ``type='wait'``.
            context: Workflow context for template rendering.
            interrupt_event: Optional event signaling user interrupt.

        Returns:
            :class:`WaitOutput` with elapsed time and interrupt flag.

        Raises:
            ValidationError: If the rendered duration cannot be parsed
                or falls outside ``(0, 24h]``.
        """
        if agent.duration is None:
            # Defensive: the schema validator forbids this, but keep a
            # clear error in case of bypass.
            raise ValidationError(
                f"Wait '{agent.name}': duration is required",
            )

        # Render duration through Jinja2 (templated durations are common
        # for poll-interval patterns). Coerce to str so int/float values
        # render predictably.
        rendered_duration = self.renderer.render(str(agent.duration), context)
        rendered_reason: str | None = None
        if agent.reason is not None:
            rendered_reason = self.renderer.render(agent.reason, context)

        try:
            seconds = parse_duration(rendered_duration)
        except ValueError as exc:
            raise ValidationError(
                f"Wait '{agent.name}': {exc}",
                suggestion=(
                    "Provide a number of seconds or a duration string "
                    "like '60s', '5m', '1h', '500ms'."
                ),
            ) from exc

        if seconds <= 0:
            raise ValidationError(
                f"Wait '{agent.name}': duration must be > 0 seconds (got {seconds!r})",
            )
        if seconds > MAX_WAIT_DURATION_SECONDS:
            raise ValidationError(
                f"Wait '{agent.name}': duration {seconds!r}s exceeds the "
                f"24h cap ({MAX_WAIT_DURATION_SECONDS}s)",
                suggestion="Reconsider using 'limits.timeout_seconds' instead.",
            )

        _verbose_log(f"  Wait: {seconds}s" + (f" — {rendered_reason}" if rendered_reason else ""))

        start = time.monotonic()
        interrupted = await self._sleep_with_interrupt(seconds, interrupt_event)
        elapsed = time.monotonic() - start

        return WaitOutput(
            waited_seconds=elapsed,
            requested_seconds=seconds,
            reason=rendered_reason,
            interrupted=interrupted,
        )

    @staticmethod
    async def _sleep_with_interrupt(seconds: float, interrupt_event: asyncio.Event | None) -> bool:
        """Sleep for ``seconds``, returning early on interrupt.

        Returns:
            ``True`` if the sleep was cancelled by ``interrupt_event``,
            ``False`` if the full duration elapsed.
        """
        if interrupt_event is None:
            await asyncio.sleep(seconds)
            return False

        sleep_task = asyncio.create_task(asyncio.sleep(seconds))
        interrupt_task = asyncio.create_task(interrupt_event.wait())
        try:
            done, pending = await asyncio.wait(
                {sleep_task, interrupt_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
        except BaseException:
            # Cancellation from outside (e.g., workflow timeout) — clean
            # up both tasks and re-raise so wait_for / outer cancellation
            # handlers see the original exception. Only suppress
            # CancelledError during cleanup (matches the success path
            # above); a genuine exception from a future, non-trivial
            # awaitable should not be silently swallowed.
            for t in (sleep_task, interrupt_task):
                if not t.done():
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await t
            raise

        return interrupt_task in done
