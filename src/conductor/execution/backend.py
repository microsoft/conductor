"""The ``RunnerBackend`` protocol: the seam every execution backend implements.

The protocol exposes typed command and agent operations
(:meth:`RunnerBackend.run_command` and :meth:`RunnerBackend.run_agent`) plus
the run-scoped lease lifecycle that the workflow engine drives. Agent calls
return a complete result or propagate cancellation after teardown; separate
``open_mcp`` and ``cancel`` operations are not part of this contract.

Like the contract types, this module imports nothing from Conductor.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from conductor.execution.types import (
    AgentEventSink,
    AgentResult,
    AgentSpec,
    CommandResult,
    CommandSpec,
    RunnerCapabilities,
    RunOutcome,
    RunSpec,
    WorkspaceLease,
)


class RunnerBackend(Protocol):
    """Execution backend contract for running commands against a workspace.

    A backend owns the processes (or remote equivalents) it spawns: on task
    cancellation it kills and reaps the child, then lets the
    ``asyncio.CancelledError`` propagate — cancellation never surfaces as a
    ``CommandResult``.
    """

    def capabilities(self) -> RunnerCapabilities:
        """Declare the backend's static capabilities.

        Returns:
            The capability set; ``batch`` is required for any backend the
            script executor will delegate to.
        """
        ...

    async def prepare_run(self, run: RunSpec) -> WorkspaceLease:
        """Acquire the workspace handle for a run.

        This must be cheap handle acquisition only — heavy allocation is
        deferred to the first command that needs it. The returned lease is
        opaque to callers and is threaded through ``run_command`` and back
        into ``finalize_run``.

        Args:
            run: The run to prepare a workspace for.

        Returns:
            An opaque, backend-owned workspace lease.
        """
        ...

    async def run_command(
        self,
        spec: CommandSpec,
        lease: WorkspaceLease | None,
        *,
        diagnostics: Callable[[str], None] | None = None,
    ) -> CommandResult:
        """Execute one command and return its data-shaped result.

        Command, infra, and timeout outcomes are all reported through the
        returned :class:`CommandResult`; this method never raises for them.
        Task cancellation kills and reaps the child, then propagates as
        ``asyncio.CancelledError``.

        Args:
            spec: The rendered command to execute.
            lease: The run's workspace lease, or ``None`` when the caller
                has none (backends without a shared workspace ignore it).
            diagnostics: Optional sink for human-facing progress lines
                (e.g. verbose script logging); the backend calls it at its
                own discretion.

        Returns:
            The command's outcome, output, and timing as data.
        """
        ...

    async def run_agent(
        self,
        spec: AgentSpec,
        lease: WorkspaceLease | None,
        *,
        on_event: AgentEventSink | None = None,
        interrupt_signal: asyncio.Event | None = None,
        execute_local: Callable[[], Awaitable[AgentResult]] | None = None,
    ) -> AgentResult:
        """Execute one agent invocation in this backend's realm.

        The call returns exactly one complete :class:`AgentResult` or raises a
        typed execution/provider error. Cancellation propagates only as
        ``asyncio.CancelledError`` after owned work has been torn down; it is
        never converted into a fabricated partial result. In-process backends
        require ``execute_local``; backends that execute outside this process
        reject it. ``interrupt_signal=None`` means no mid-flight interrupt was
        requested.

        Args:
            spec: Resolved inputs for one agent invocation.
            lease: The run's workspace lease, or ``None``.
            on_event: Optional sink for the invocation's ordinary agent events.
            interrupt_signal: Optional signal for a graceful in-flight pause.
            execute_local: In-process invocation closure, including the provider
                call and its event/interrupt wiring.

        Returns:
            The agent result without converting cancellation into data.
        """
        ...

    async def finalize_run(self, lease: WorkspaceLease, outcome: RunOutcome) -> None:
        """Release the workspace held by a finished run.

        Idempotent cleanup keyed by the lease: safe to call exactly once per
        lease after the run terminates (successfully, on failure, or after
        cancellation has already propagated). Remote backends reclaim realm
        resources here.
        """
        ...
