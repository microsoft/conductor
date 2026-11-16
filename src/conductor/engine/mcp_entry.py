"""Ownership of an attempted Docker direct MCP connection."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from conductor.execution.errors import ExecutionSpecError
from conductor.execution.types import McpStdioTarget

if TYPE_CHECKING:
    from conductor.mcp.manager import MCPManager

logger = logging.getLogger(__name__)
_PENDING_ENTRIES: set[McpStepEntry] = set()


@dataclass(eq=False, slots=True)
class McpStepEntry:
    """Retain the manager and allocated target until their ordered cleanup finishes."""

    manager: MCPManager
    target: McpStdioTarget | None = None
    allocation: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    cleanup_unconfirmed: bool = field(default=False, init=False)
    sealed: bool = field(default=False, init=False)
    deadline: float | None = field(default=None, init=False)
    on_uncertain: Callable[[], None] | None = field(default=None, init=False, repr=False)
    _expired: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _timer: asyncio.TimerHandle | None = field(default=None, init=False, repr=False)
    _manager_close: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _cleanup: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    async def allocate(self, operation: Callable[[], Awaitable[McpStdioTarget]]) -> McpStdioTarget:
        """Register the allocation owner before dispatch and retain its late result."""

        async def own() -> None:
            self.target = await operation()

        _PENDING_ENTRIES.add(self)
        self.allocation = asyncio.create_task(own())
        await asyncio.shield(self.allocation)
        if self.sealed:
            raise ExecutionSpecError("Docker MCP admission is sealed")
        assert self.target is not None
        return self.target

    def seal(self, deadline: float) -> None:
        """Only shorten the existing observation budget; cancellation never renews it."""
        self.sealed = True
        self.deadline = deadline if self.deadline is None else min(deadline, self.deadline)
        if self._timer is not None:
            self._timer.cancel()
        if self._cleanup is not None and self._cleanup.done():
            return
        self._timer = asyncio.get_running_loop().call_at(self.deadline, self._expired.set)

    def mark_uncertain(self) -> None:
        """Latch lost proof even if an owner subsequently finishes successfully."""
        already_uncertain = self.cleanup_unconfirmed
        self.cleanup_unconfirmed = True
        self._expired.set()
        if not already_uncertain and self.on_uncertain is not None:
            self.on_uncertain()

    async def _close(self) -> None:
        # Allocation owns the target until assignment. A None target while it is
        # pending cannot prove that there is nothing to release.
        if self.allocation is not None:
            await asyncio.wait({self.allocation})
            if not self.allocation.cancelled():
                self.allocation.exception()
        self._manager_close = asyncio.create_task(self.manager.close(strict=True))
        expiry = asyncio.create_task(self._expired.wait())
        try:
            await asyncio.wait({self._manager_close, expiry}, return_when=asyncio.FIRST_COMPLETED)
            if not self._manager_close.done():
                self.mark_uncertain()
            else:
                self._manager_close.result()
        except (Exception, asyncio.CancelledError):
            # This is a resource-owner boundary: retain failure, then always
            # attempt the second custodian without replacing the primary error.
            self.mark_uncertain()
            logger.warning("Docker MCP strict manager cleanup is unconfirmed")
        finally:
            expiry.cancel()
            await asyncio.wait({expiry})
            try:
                if self.target is not None:
                    await self.target.aclose()
            except (Exception, asyncio.CancelledError):
                self.mark_uncertain()
                logger.warning("Docker MCP target cleanup is unconfirmed")
            # Keep the manager custodian strongly reachable after observation
            # expires. The target has already received its own cleanup attempt.
            await asyncio.wait({self._manager_close})
            if not self._manager_close.cancelled():
                self._manager_close.exception()
            if self._timer is not None:
                self._timer.cancel()
            _PENDING_ENTRIES.discard(self)

    async def aclose(self) -> None:
        """Close the SDK owner before the target, including after partial startup."""
        self.seal(self.deadline or (asyncio.get_running_loop().time() + 30))
        if self._cleanup is None:
            _PENDING_ENTRIES.add(self)
            self._cleanup = asyncio.create_task(self._close())
        cancelled = False
        expiry = asyncio.create_task(self._expired.wait())
        try:
            while not self._cleanup.done() and not self._expired.is_set():
                try:
                    await asyncio.wait({self._cleanup, expiry}, return_when=asyncio.FIRST_COMPLETED)
                except asyncio.CancelledError:
                    cancelled = True
            if not self._cleanup.done():
                self.mark_uncertain()
            else:
                self._cleanup.result()
        finally:
            expiry.cancel()
        if self.cleanup_unconfirmed:
            raise ExecutionSpecError("Docker MCP entry cleanup is unconfirmed")
        if cancelled:
            raise asyncio.CancelledError
