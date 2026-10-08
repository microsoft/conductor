"""The ``RunnerBackend`` protocol: the seam every execution backend implements.

The protocol is intentionally minimal — one typed operation
(:meth:`RunnerBackend.run_command`) plus the run-scoped lease lifecycle that
the workflow engine drives. Operations such as ``run_agent``/``open_mcp``/
``cancel`` are deliberately absent: they will arrive together with their
consumers in later contract revisions, not ahead of them.

Like the contract types, this module imports nothing from Conductor.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from conductor.execution.types import (
    CommandResult,
    CommandSpec,
    RunnerCapabilities,
    RunOutcome,
    RunSpec,
    WorkspaceIdentity,
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
        on_dispatch: Callable[[], None] | None = None,
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
            on_dispatch: Optional notification fired after pre-dispatch checks,
                immediately before creating the command process. Pre-dispatch
                failures must not call it.

        Returns:
            The command's outcome, output, and timing as data.
        """
        ...

    async def attach_run(
        self, run: RunSpec, identity: WorkspaceIdentity, *, expect_staged: bool = False
    ) -> WorkspaceLease:
        """Verify a retained workspace and return its process-local lease.

        This operation checks existence, labels, and incarnation only. It must not
        create, stage, delete, or replace any resource.
        """
        ...

    async def finalize_run(
        self, lease: WorkspaceLease, outcome: RunOutcome, *, retain: bool = False
    ) -> None:
        """Release the workspace held by a finished run.

        Idempotent cleanup keyed by the lease: safe to call exactly once per
        lease after the run terminates (successfully, on failure, or after
        cancellation has already propagated). Remote backends reclaim realm
        resources here.
        """
        ...
