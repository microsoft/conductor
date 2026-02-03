"""Safety limit enforcement for workflow execution.

This module provides the LimitEnforcer class for tracking and enforcing
iteration and timeout limits during workflow execution.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from conductor.exceptions import (
    MaxIterationsError,
)
from conductor.exceptions import (
    TimeoutError as ConductorTimeoutError,
)


@dataclass
class LimitEnforcer:
    """Enforces iteration and timeout limits on workflow execution.

    The LimitEnforcer tracks workflow execution progress and enforces
    safety limits to prevent runaway workflows:
    - max_iterations: Maximum number of agent executions
    - timeout_seconds: Maximum wall-clock time for entire workflow (None = unlimited)

    Attributes:
        max_iterations: Maximum number of agent executions allowed.
        timeout_seconds: Maximum wall-clock time for entire workflow. None means unlimited.
        current_iteration: Current iteration count.
        start_time: Workflow start timestamp (monotonic).
        execution_history: Ordered list of executed agent names.
        current_agent: Name of the currently executing agent.

    Example:
        >>> enforcer = LimitEnforcer(max_iterations=5, timeout_seconds=60)
        >>> enforcer.start()
        >>> enforcer.check_iteration("agent1")  # OK
        >>> enforcer.record_execution("agent1")
        >>> enforcer.check_timeout()  # OK if within timeout
    """

    max_iterations: int = 10
    """Maximum number of agent executions."""

    timeout_seconds: int | None = None
    """Maximum wall-clock time for entire workflow. None means unlimited."""

    current_iteration: int = 0
    """Current iteration count."""

    start_time: float | None = None
    """Workflow start timestamp."""

    execution_history: list[str] = field(default_factory=list)
    """Ordered list of executed agent names."""

    current_agent: str | None = None
    """Currently executing agent name."""

    def start(self) -> None:
        """Mark workflow start for timeout tracking.

        Resets the iteration counter, execution history, and starts
        the timeout clock.
        """
        self.start_time = time.monotonic()
        self.current_iteration = 0
        self.execution_history = []
        self.current_agent = None

    def check_iteration(self, agent_name: str) -> None:
        """Check iteration limit before agent execution.

        This should be called before each agent execution. If the
        iteration limit has been reached, raises MaxIterationsError.

        Args:
            agent_name: Name of agent about to execute.

        Raises:
            MaxIterationsError: If max iterations exceeded.
        """
        self.current_agent = agent_name

        if self.current_iteration >= self.max_iterations:
            raise MaxIterationsError(
                f"Workflow exceeded maximum iterations ({self.max_iterations})",
                suggestion=(
                    f"Increase max_iterations in workflow.limits or fix the loop "
                    f"causing this issue. Last {min(5, len(self.execution_history))} "
                    f"agents: {self.execution_history[-5:]}"
                ),
                iterations=self.current_iteration,
                max_iterations=self.max_iterations,
                agent_history=self.execution_history.copy(),
            )

    def check_parallel_group_iteration(self, group_name: str, agent_count: int) -> None:
        """Check iteration limit before parallel group execution.

        This checks if all agents in the parallel group can execute without
        exceeding the iteration limit.

        Args:
            group_name: Name of parallel group about to execute.
            agent_count: Number of agents in the parallel group.

        Raises:
            MaxIterationsError: If executing all parallel agents would exceed max iterations.
        """
        self.current_agent = group_name

        # Check if executing all parallel agents would exceed the limit
        if self.current_iteration + agent_count > self.max_iterations:
            remaining = self.max_iterations - self.current_iteration
            raise MaxIterationsError(
                f"Parallel group '{group_name}' with {agent_count} agents would exceed "
                f"maximum iterations ({self.max_iterations}). Only {remaining} "
                f"iteration(s) remaining.",
                suggestion=(
                    f"Increase max_iterations in workflow.limits or reduce the number "
                    f"of agents in the parallel group. Current iteration: "
                    f"{self.current_iteration}/{self.max_iterations}"
                ),
                iterations=self.current_iteration,
                max_iterations=self.max_iterations,
                agent_history=self.execution_history.copy(),
            )

    def record_execution(self, agent_name: str, count: int = 1) -> None:
        """Record successful agent execution.

        Updates the execution history and increments the iteration counter.
        This should be called after each successful agent execution.

        Args:
            agent_name: Name of agent that just completed.
            count: Number of iterations to record (default 1, >1 for parallel groups).
        """
        self.execution_history.append(agent_name)
        self.current_iteration += count

    def increase_limit(self, additional: int) -> None:
        """Increase the max_iterations limit by the given amount.

        This allows dynamically extending the iteration limit during workflow
        execution, typically in response to a user prompt when the limit is reached.

        Args:
            additional: Number of additional iterations to allow. Must be positive.

        Example:
            >>> enforcer = LimitEnforcer(max_iterations=10)
            >>> enforcer.increase_limit(5)
            >>> assert enforcer.max_iterations == 15
        """
        if additional > 0:
            self.max_iterations += additional

    def check_timeout(self) -> None:
        """Check if workflow has exceeded timeout.

        This should be called periodically during workflow execution.
        If the timeout has been exceeded, raises ConductorTimeoutError.
        If timeout_seconds is None, no timeout is enforced.

        Raises:
            ConductorTimeoutError: If timeout exceeded.
        """
        if self.start_time is None:
            return

        # No timeout if not set
        if self.timeout_seconds is None:
            return

        elapsed = time.monotonic() - self.start_time
        if elapsed >= self.timeout_seconds:
            raise ConductorTimeoutError(
                f"Workflow exceeded timeout ({self.timeout_seconds}s)",
                suggestion=(
                    "Increase timeout_seconds in workflow.limits or optimize agent execution time"
                ),
                elapsed_seconds=elapsed,
                timeout_seconds=float(self.timeout_seconds),
                current_agent=self.current_agent,
            )

    def get_elapsed_time(self) -> float:
        """Get the elapsed time since workflow start.

        Returns:
            Elapsed time in seconds, or 0 if not started.
        """
        if self.start_time is None:
            return 0.0
        return time.monotonic() - self.start_time

    def get_remaining_timeout(self) -> float | None:
        """Get the remaining time before timeout.

        Returns:
            Remaining time in seconds, full timeout if not started,
            or None if no timeout is set.
        """
        if self.timeout_seconds is None:
            return None
        if self.start_time is None:
            return float(self.timeout_seconds)
        elapsed = time.monotonic() - self.start_time
        return max(0.0, self.timeout_seconds - elapsed)

    @asynccontextmanager
    async def timeout_context(self) -> AsyncIterator[None]:
        """Async context manager for timeout enforcement.

        Uses asyncio.timeout() to enforce the timeout limit. If the
        timeout is exceeded, converts the asyncio.TimeoutError to a
        ConductorTimeoutError with context information.

        If timeout_seconds is None, no timeout is enforced.

        Usage:
            async with enforcer.timeout_context():
                await run_workflow()

        Yields:
            None

        Raises:
            ConductorTimeoutError: If timeout exceeded.
        """
        if self.start_time is None:
            self.start()

        # No timeout if not set
        if self.timeout_seconds is None:
            yield
            return

        try:
            async with asyncio.timeout(self.timeout_seconds):
                yield
        except TimeoutError:
            elapsed = time.monotonic() - self.start_time if self.start_time else 0
            raise ConductorTimeoutError(
                f"Workflow exceeded timeout ({self.timeout_seconds}s)",
                suggestion=(
                    "Increase timeout_seconds in workflow.limits or optimize agent execution time"
                ),
                elapsed_seconds=elapsed,
                timeout_seconds=float(self.timeout_seconds),
                current_agent=self.current_agent,
            ) from None

    async def wait_for_with_timeout(self, coro: Any, operation_name: str = "operation") -> Any:
        """Execute a coroutine with timeout enforcement.

        Uses asyncio.wait_for with the remaining timeout. This is useful
        for parallel execution to ensure each parallel group respects
        the overall workflow timeout.

        Args:
            coro: Coroutine to execute.
            operation_name: Name of operation for error messages (default "operation").

        Returns:
            Result of the coroutine.

        Raises:
            ConductorTimeoutError: If timeout exceeded.
        """
        remaining = self.get_remaining_timeout()

        # No timeout if not set
        if remaining is None:
            return await coro

        try:
            return await asyncio.wait_for(coro, timeout=remaining)
        except TimeoutError:
            elapsed = time.monotonic() - self.start_time if self.start_time else 0
            raise ConductorTimeoutError(
                f"Timeout during {operation_name} ({self.timeout_seconds}s limit)",
                suggestion=(
                    "Increase timeout_seconds in workflow.limits or optimize "
                    f"{operation_name} execution time"
                ),
                elapsed_seconds=elapsed,
                timeout_seconds=float(self.timeout_seconds) if self.timeout_seconds else 0.0,
                current_agent=self.current_agent,
            ) from None
