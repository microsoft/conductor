"""Token usage tracking for workflow execution.

This module provides classes for tracking token usage and costs
across workflow agent executions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from conductor.engine.pricing import ModelPricing, calculate_cost, get_pricing

if TYPE_CHECKING:
    from conductor.providers.base import AgentOutput


@dataclass
class AgentUsage:
    """Token usage and cost for a single agent execution.

    Attributes:
        agent_name: Name of the agent that was executed.
        model: Model used for execution (may be None if unknown).
        input_tokens: Number of input tokens used.
        output_tokens: Number of output tokens generated.
        cache_read_tokens: Tokens read from cache (Claude).
        cache_write_tokens: Tokens written to cache (Claude).
        cost_usd: Estimated cost in USD (None if pricing unavailable).
        elapsed_seconds: Execution time in seconds.
    """

    agent_name: str
    model: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float | None
    elapsed_seconds: float


@dataclass
class WorkflowUsage:
    """Aggregated token usage for an entire workflow execution.

    Attributes:
        agents: List of per-agent usage records.
    """

    agents: list[AgentUsage] = field(default_factory=list)

    @property
    def total_input_tokens(self) -> int:
        """Total input tokens across all agents."""
        return sum(a.input_tokens for a in self.agents)

    @property
    def total_output_tokens(self) -> int:
        """Total output tokens across all agents."""
        return sum(a.output_tokens for a in self.agents)

    @property
    def total_tokens(self) -> int:
        """Total tokens (input + output) across all agents."""
        return self.total_input_tokens + self.total_output_tokens

    @property
    def total_cache_read_tokens(self) -> int:
        """Total cache read tokens across all agents."""
        return sum(a.cache_read_tokens for a in self.agents)

    @property
    def total_cache_write_tokens(self) -> int:
        """Total cache write tokens across all agents."""
        return sum(a.cache_write_tokens for a in self.agents)

    @property
    def total_cost_usd(self) -> float | None:
        """Total cost in USD across all agents.

        Returns None if no agents have cost data.
        """
        costs = [a.cost_usd for a in self.agents if a.cost_usd is not None]
        return sum(costs) if costs else None

    @property
    def total_elapsed_seconds(self) -> float:
        """Total execution time across all agents.

        Note: For parallel agents, this sums individual times,
        not wall-clock time.
        """
        return sum(a.elapsed_seconds for a in self.agents)


class UsageTracker:
    """Tracks token usage across workflow execution.

    Records usage for each agent execution and provides
    aggregated summaries. Supports custom pricing overrides.

    Example:
        >>> tracker = UsageTracker()
        >>> usage = tracker.record("answerer", agent_output, 1.5)
        >>> print(f"Agent cost: ${usage.cost_usd:.4f}")
        >>> summary = tracker.get_summary()
        >>> print(f"Total cost: ${summary.total_cost_usd:.4f}")
    """

    def __init__(
        self,
        pricing_overrides: dict[str, ModelPricing] | None = None,
    ) -> None:
        """Initialize the usage tracker.

        Args:
            pricing_overrides: Optional custom pricing for specific models.
        """
        self._agents: list[AgentUsage] = []
        self._pricing_overrides = pricing_overrides or {}

    def record(
        self,
        agent_name: str,
        output: AgentOutput,
        elapsed: float,
    ) -> AgentUsage:
        """Record usage from an agent execution.

        Args:
            agent_name: Name of the agent that executed.
            output: The agent's output containing token counts.
            elapsed: Execution time in seconds.

        Returns:
            AgentUsage record with token counts and cost.
        """
        # Safely extract integer token counts, defaulting to 0
        # Use isinstance check to handle Mock objects in tests
        input_tokens = (
            output.input_tokens
            if isinstance(output.input_tokens, int) and output.input_tokens is not None
            else 0
        )
        output_tokens = (
            output.output_tokens
            if isinstance(output.output_tokens, int) and output.output_tokens is not None
            else 0
        )
        cache_read = (
            output.cache_read_tokens
            if isinstance(output.cache_read_tokens, int) and output.cache_read_tokens is not None
            else 0
        )
        cache_write = (
            output.cache_write_tokens
            if isinstance(output.cache_write_tokens, int) and output.cache_write_tokens is not None
            else 0
        )

        cost = None
        if output.model and isinstance(output.model, str):
            cost = calculate_cost(
                model=output.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                pricing=get_pricing(output.model, self._pricing_overrides),
            )

        usage = AgentUsage(
            agent_name=agent_name,
            model=output.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cost_usd=cost,
            elapsed_seconds=elapsed,
        )
        self._agents.append(usage)
        return usage

    def get_summary(self) -> WorkflowUsage:
        """Get aggregated workflow usage.

        Returns:
            WorkflowUsage with all recorded agent executions.
        """
        return WorkflowUsage(agents=self._agents.copy())

    def reset(self) -> None:
        """Clear all recorded usage data."""
        self._agents.clear()

    def check_budget(self, budget_usd: float) -> tuple[bool, float]:
        """Check if budget is exceeded.

        Note: This method is provided for future budget enforcement.
        Currently not used in the initial implementation.

        Args:
            budget_usd: Budget limit in USD.

        Returns:
            Tuple of (exceeded, current_total) where exceeded is True
            if the current total cost exceeds the budget.
        """
        total = self.get_summary().total_cost_usd or 0.0
        return (total > budget_usd, total)
