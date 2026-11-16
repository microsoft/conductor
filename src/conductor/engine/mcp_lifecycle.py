"""Root custody of fenced preparation attempts and direct MCP entries."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field

from conductor.engine.mcp_entry import McpStepEntry
from conductor.execution.backend import RunnerBackend
from conductor.execution.docker import DockerRunnerBackend
from conductor.execution.docker_mcp import join_before
from conductor.execution.errors import ExecutionSpecError
from conductor.execution.types import RunOutcome, WorkspaceLease

logger = logging.getLogger(__name__)
_PENDING_ROOTS: set[McpRunLifecycle] = set()


@dataclass(eq=False, slots=True)
class PreparationAttempt:
    """Accumulate proof on the same record from provisional acquisition to release."""

    backend: RunnerBackend
    lease: WorkspaceLease | None = None
    abort: Callable[[float], Awaitable[None]] | None = None
    task: asyncio.Task[WorkspaceLease] | None = None
    committed: bool = False
    entries: list[McpStepEntry] = field(default_factory=list)
    release: asyncio.Task[None] | None = None

    def register(self, lease: WorkspaceLease, abort: Callable[[float], Awaitable[None]]) -> None:
        self.lease = lease
        self.abort = abort


class McpRunLifecycle:
    """Retain ownership beyond pool eviction, cancellation and the root's bounded wait."""

    def __init__(self, on_uncertain: Callable[[], None]) -> None:
        self.attempts: list[PreparationAttempt] = []
        self.sealed = False
        self.deadline: float | None = None
        self.uncertain = False
        self._on_uncertain = on_uncertain
        self._finish: asyncio.Task[None] | None = None

    @property
    def active(self) -> bool:
        return bool(self.attempts)

    def check_admission(self) -> None:
        if self.sealed or self.uncertain:
            raise ExecutionSpecError("Docker MCP root admission is sealed")

    def attempt(self, backend: RunnerBackend) -> PreparationAttempt:
        """Retain even an unpublished backend before calling its preparation method."""
        self.check_admission()
        attempt = PreparationAttempt(backend)
        self.attempts.append(attempt)
        _PENDING_ROOTS.add(self)
        return attempt

    async def prepare(
        self, attempt: PreparationAttempt, operation: Coroutine[None, None, WorkspaceLease]
    ) -> WorkspaceLease:
        async def own() -> WorkspaceLease:
            lease = await operation
            attempt.lease = lease
            self.check_admission()
            attempt.committed = True
            return lease

        attempt.task = asyncio.create_task(own())
        return await asyncio.shield(attempt.task)

    def register_entry(
        self, backend: RunnerBackend, lease: WorkspaceLease, entry: McpStepEntry
    ) -> None:
        self.check_admission()
        attempt = next(
            (item for item in self.attempts if item.backend is backend and item.lease is lease),
            None,
        )
        if attempt is None:
            # Explicitly supplied backend-issued leases still need the same barrier.
            attempt = self.attempt(backend)
            attempt.lease = lease
            attempt.committed = True
        attempt.entries.append(entry)
        entry.on_uncertain = self.mark_uncertain

    def seal(self, deadline: float) -> None:
        """Seal before any snapshot or await, sharing the root's absolute deadline."""
        self.sealed = True
        self.deadline = deadline if self.deadline is None else min(deadline, self.deadline)
        for attempt in self.attempts:
            if isinstance(attempt.backend, DockerRunnerBackend) and attempt.lease is not None:
                attempt.backend.seal_mcp_cleanup(
                    attempt.lease, self.deadline, uncertain=self.uncertain
                )
            for entry in attempt.entries:
                entry.seal(self.deadline)

    def mark_uncertain(self) -> None:
        already_uncertain = self.uncertain
        self.uncertain = True
        if self.deadline is None:
            self.deadline = min(
                entry.deadline
                for attempt in self.attempts
                for entry in attempt.entries
                if entry.deadline is not None
            )
        self.seal(self.deadline)
        if not already_uncertain:
            self._on_uncertain()
        for attempt in self.attempts:
            for entry in attempt.entries:
                if entry._cleanup is None or not entry._cleanup.done():
                    entry.mark_uncertain()
        if not already_uncertain:
            logger.warning(
                "Docker MCP root cleanup is unconfirmed; "
                "workspace and fence state requires independent verification, reuse refused"
            )

    async def _observe[T](self, task: asyncio.Task[T]) -> bool:
        assert self.deadline is not None
        try:
            await join_before(task, self.deadline)
        except ExecutionSpecError:
            self.mark_uncertain()
            return False
        return True

    async def finish(self, outcome: RunOutcome, *, retain: bool) -> None:
        """Run one barrier; partial completion can never authorize Docker finalization."""
        assert self.deadline is not None and self.sealed
        if self._finish is None:
            self._finish = asyncio.create_task(self._release(outcome, retain=retain))
        if await self._observe(self._finish):
            self._finish.result()

    async def _release(self, outcome: RunOutcome, *, retain: bool) -> None:
        cleanups = [
            asyncio.create_task(entry.aclose())
            for attempt in self.attempts
            for entry in attempt.entries
        ]
        for cleanup in cleanups:
            if not await self._observe(cleanup):
                continue
            if cleanup.cancelled() or cleanup.exception() is not None:
                self.mark_uncertain()
        for attempt in self.attempts:
            if attempt.task is not None:
                if not await self._observe(attempt.task):
                    continue
                if not attempt.task.cancelled():
                    attempt.task.exception()
            if any(entry.cleanup_unconfirmed for entry in attempt.entries):
                self.mark_uncertain()
        for attempt in self.attempts:
            if attempt.lease is None:
                self.mark_uncertain()
                continue
            if attempt.committed:
                if self.uncertain:
                    continue
                operation = attempt.backend.finalize_run(attempt.lease, outcome, retain=retain)
            else:
                if attempt.abort is None:
                    self.mark_uncertain()
                    continue
                assert self.deadline is not None
                operation = attempt.abort(self.deadline)

            async def release(pending: Awaitable[None] = operation) -> None:
                await pending

            attempt.release = asyncio.create_task(release())
            if await self._observe(attempt.release) and (
                attempt.release.cancelled() or attempt.release.exception() is not None
            ):
                self.mark_uncertain()
        if not self.uncertain:
            _PENDING_ROOTS.discard(self)
