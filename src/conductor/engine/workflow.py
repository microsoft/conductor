"""Workflow execution engine for Conductor.

This module provides the WorkflowEngine class for orchestrating
multi-agent workflow execution.
"""

from __future__ import annotations

import asyncio
import copy
import time as _time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from conductor.engine.context import WorkflowContext
from conductor.engine.limits import LimitEnforcer
from conductor.engine.pricing import ModelPricing
from conductor.engine.router import Router, RouteResult
from conductor.engine.usage import UsageTracker
from conductor.exceptions import ConductorError, ExecutionError, MaxIterationsError
from conductor.executor.agent import AgentExecutor
from conductor.executor.template import TemplateRenderer
from conductor.gates.human import (
    GateResult,
    HumanGateHandler,
    MaxIterationsHandler,
)


def _verbose_log(message: str, style: str = "dim") -> None:
    """Lazy import wrapper for verbose_log to avoid circular imports."""
    from conductor.cli.run import verbose_log

    verbose_log(message, style)


def _verbose_log_timing(operation: str, elapsed: float) -> None:
    """Lazy import wrapper for verbose_log_timing to avoid circular imports."""
    from conductor.cli.run import verbose_log_timing

    verbose_log_timing(operation, elapsed)


def _verbose_log_agent_start(agent_name: str, iteration: int) -> None:
    """Lazy import wrapper for verbose_log_agent_start to avoid circular imports."""
    from conductor.cli.run import verbose_log_agent_start

    verbose_log_agent_start(agent_name, iteration)


def _verbose_log_agent_complete(
    agent_name: str,
    elapsed: float,
    *,
    model: str | None = None,
    tokens: int | None = None,
    output_keys: list[str] | None = None,
    cost_usd: float | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    """Lazy import wrapper for verbose_log_agent_complete to avoid circular imports."""
    from conductor.cli.run import verbose_log_agent_complete

    verbose_log_agent_complete(
        agent_name,
        elapsed,
        model=model,
        tokens=tokens,
        output_keys=output_keys,
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _verbose_log_route(target: str) -> None:
    """Lazy import wrapper for verbose_log_route to avoid circular imports."""
    from conductor.cli.run import verbose_log_route

    verbose_log_route(target)


def _verbose_log_parallel_start(group_name: str, agent_count: int) -> None:
    """Lazy import wrapper for verbose_log_parallel_start to avoid circular imports."""
    from conductor.cli.run import verbose_log_parallel_start

    verbose_log_parallel_start(group_name, agent_count)


def _verbose_log_parallel_agent_complete(
    agent_name: str,
    elapsed: float,
    *,
    model: str | None = None,
    tokens: int | None = None,
    cost_usd: float | None = None,
) -> None:
    """Lazy import wrapper for verbose_log_parallel_agent_complete to avoid circular imports."""
    from conductor.cli.run import verbose_log_parallel_agent_complete

    verbose_log_parallel_agent_complete(
        agent_name, elapsed, model=model, tokens=tokens, cost_usd=cost_usd
    )


def _verbose_log_parallel_agent_failed(
    agent_name: str,
    elapsed: float,
    exception_type: str,
    message: str,
) -> None:
    """Lazy import wrapper for verbose_log_parallel_agent_failed to avoid circular imports."""
    from conductor.cli.run import verbose_log_parallel_agent_failed

    verbose_log_parallel_agent_failed(agent_name, elapsed, exception_type, message)


def _verbose_log_parallel_summary(
    group_name: str,
    success_count: int,
    failure_count: int,
    total_elapsed: float,
) -> None:
    """Lazy import wrapper for verbose_log_parallel_summary to avoid circular imports."""
    from conductor.cli.run import verbose_log_parallel_summary

    verbose_log_parallel_summary(group_name, success_count, failure_count, total_elapsed)


def _verbose_log_for_each_start(
    group_name: str,
    item_count: int,
    max_concurrent: int,
    failure_mode: str,
) -> None:
    """Lazy import wrapper for verbose_log_for_each_start to avoid circular imports."""
    from conductor.cli.run import verbose_log_for_each_start

    verbose_log_for_each_start(group_name, item_count, max_concurrent, failure_mode)


def _verbose_log_for_each_item_complete(
    item_key: str,
    elapsed: float,
    *,
    tokens: int | None = None,
    cost_usd: float | None = None,
) -> None:
    """Lazy import wrapper for verbose_log_for_each_item_complete to avoid circular imports."""
    from conductor.cli.run import verbose_log_for_each_item_complete

    verbose_log_for_each_item_complete(item_key, elapsed, tokens=tokens, cost_usd=cost_usd)


def _verbose_log_for_each_item_failed(
    item_key: str,
    elapsed: float,
    exception_type: str,
    message: str,
) -> None:
    """Lazy import wrapper for verbose_log_for_each_item_failed to avoid circular imports."""
    from conductor.cli.run import verbose_log_for_each_item_failed

    verbose_log_for_each_item_failed(item_key, elapsed, exception_type, message)


def _verbose_log_for_each_summary(
    group_name: str,
    success_count: int,
    failure_count: int,
    total_elapsed: float,
) -> None:
    """Lazy import wrapper for verbose_log_for_each_summary to avoid circular imports."""
    from conductor.cli.run import verbose_log_for_each_summary

    verbose_log_for_each_summary(group_name, success_count, failure_count, total_elapsed)


if TYPE_CHECKING:
    from conductor.config.schema import AgentDef, ForEachDef, ParallelGroup, WorkflowConfig
    from conductor.providers.base import AgentProvider
    from conductor.providers.registry import ProviderRegistry


@dataclass
class ParallelAgentError:
    """Error information from a failed parallel agent execution.

    Attributes:
        agent_name: Name of the agent that failed.
        exception_type: Type of the exception (e.g., "ValidationError").
        message: Error message.
        suggestion: Optional suggestion for fixing the error.

    Example:
        error = ParallelAgentError(
            agent_name="validator",
            exception_type="ValidationError",
            message="Missing required field 'email'",
            suggestion="Ensure all required fields are present"
        )
    """

    agent_name: str
    exception_type: str
    message: str
    suggestion: str | None = None


@dataclass
class ParallelGroupOutput:
    """Aggregated output from a parallel group execution.

    Attributes:
        outputs: Dictionary mapping successful agent names to their outputs.
        errors: Dictionary mapping failed agent names to their errors.

    Example:
        output = ParallelGroupOutput(
            outputs={"agent1": {"result": "success"}, "agent2": {"value": 42}},
            errors={"agent3": ParallelAgentError(...)}
        )
        # Access via: output.outputs["agent1"]["result"]
    """

    outputs: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, ParallelAgentError] = field(default_factory=dict)


@dataclass
class ForEachError:
    """Error information from a failed for-each item execution.

    Attributes:
        item_key: Key or index of the item that failed (string representation).
        exception_type: Type of the exception (e.g., "ValidationError").
        message: Error message.
        suggestion: Optional suggestion for fixing the error.

    Example:
        error = ForEachError(
            item_key="2",
            exception_type="ValidationError",
            message="Missing required field 'email'",
            suggestion="Ensure all required fields are present"
        )
    """

    item_key: str
    exception_type: str
    message: str
    suggestion: str | None = None


@dataclass
class ForEachGroupOutput:
    """Aggregated output from a for-each group execution.

    Attributes:
        outputs: List or dict of successful outputs (list by default, dict if key_by used).
        errors: Dictionary mapping item key/index to error info.
        count: Total number of items processed.

    Example (list-based):
        output = ForEachGroupOutput(
            outputs=[{"result": "success"}, {"result": "success"}],
            errors={"3": ForEachError(...)},
            count=5
        )
        # Access via: output.outputs[0]["result"]

    Example (dict-based with key_by):
        output = ForEachGroupOutput(
            outputs={"KPI123": {"result": "success"}, "KPI456": {"result": "success"}},
            errors={"KPI789": ForEachError(...)},
            count=3
        )
        # Access via: output.outputs["KPI123"]["result"]
    """

    outputs: list[Any] | dict[str, Any] = field(default_factory=list)
    errors: dict[str, ForEachError] = field(default_factory=dict)
    count: int = 0


@dataclass
class LifecycleHookResult:
    """Result of executing a lifecycle hook.

    Attributes:
        hook_name: Name of the hook (on_start, on_complete, on_error).
        executed: Whether the hook was executed.
        result: The rendered result of the hook template.
        error: Any error that occurred during hook execution.
    """

    hook_name: str
    executed: bool
    result: str | None = None
    error: str | None = None


@dataclass
class ExecutionStep:
    """A single step in the execution plan.

    Represents an agent or parallel group that will be executed during workflow execution,
    along with its configuration and possible routing destinations.
    """

    agent_name: str
    """Name of the agent or parallel group."""

    agent_type: str
    """Type: 'agent', 'human_gate', or 'parallel_group'."""

    model: str | None
    """Model used by this agent (None for parallel groups)."""

    routes: list[dict[str, Any]] = field(default_factory=list)
    """Possible routes from this agent or parallel group."""

    is_loop_target: bool = False
    """True if this agent could be a loop-back target."""

    parallel_agents: list[str] | None = None
    """For parallel groups, list of agent names that execute in parallel."""

    failure_mode: str | None = None
    """For parallel groups, the failure handling mode."""


@dataclass
class ExecutionPlan:
    """Represents the workflow execution plan without actually running.

    This provides a static analysis of the workflow structure, showing
    all possible agents that may be executed and their routing paths.
    Used by the --dry-run flag to display the execution plan.
    """

    workflow_name: str
    """Name of the workflow."""

    entry_point: str
    """Name of the first agent."""

    steps: list[ExecutionStep] = field(default_factory=list)
    """Ordered steps in the execution plan."""

    max_iterations: int = 10
    """Maximum iterations configured."""

    timeout_seconds: int | None = None
    """Timeout configured. None means unlimited."""

    possible_paths: list[list[str]] = field(default_factory=list)
    """Possible execution paths through the workflow."""


class WorkflowEngine:
    """Orchestrates multi-agent workflow execution.

    The WorkflowEngine manages the complete lifecycle of a workflow:
    1. Initialize context with workflow inputs
    2. Execute agents in sequence following routing rules
    3. Accumulate context between agents
    4. Build final output from templates

    Example (single provider):
        >>> from conductor.config.loader import load_workflow
        >>> from conductor.providers.factory import create_provider
        >>> config = load_workflow("workflow.yaml")
        >>> provider = await create_provider(config.workflow.runtime.provider)
        >>> engine = WorkflowEngine(config, provider=provider)
        >>> result = await engine.run({"question": "What is Python?"})

    Example (multi-provider with registry):
        >>> from conductor.providers.registry import ProviderRegistry
        >>> async with ProviderRegistry(config) as registry:
        ...     engine = WorkflowEngine(config, registry=registry)
        ...     result = await engine.run({"question": "What is Python?"})
    """

    def __init__(
        self,
        config: WorkflowConfig,
        provider: AgentProvider | None = None,
        registry: ProviderRegistry | None = None,
        skip_gates: bool = False,
    ) -> None:
        """Initialize the WorkflowEngine.

        Args:
            config: The workflow configuration.
            provider: Single provider for backward compatibility (deprecated).
                If both provider and registry are None, agents cannot be executed.
            registry: Provider registry for multi-provider support.
                When provided, each agent can use a different provider based
                on the agent's `provider` field or the workflow default.
            skip_gates: If True, auto-selects first option at human gates.

        Note:
            If both provider and registry are provided, registry takes precedence.
            The single provider parameter is deprecated but still supported for
            backward compatibility.
        """
        self.config = config
        self.skip_gates = skip_gates
        self.context = WorkflowContext()
        self.renderer = TemplateRenderer()
        self.router = Router()
        self.limits = LimitEnforcer(
            max_iterations=config.workflow.limits.max_iterations,
            timeout_seconds=config.workflow.limits.timeout_seconds,
        )
        self.gate_handler = HumanGateHandler(skip_gates=skip_gates)
        self.max_iterations_handler = MaxIterationsHandler(skip_gates=skip_gates)
        self.usage_tracker = UsageTracker(
            pricing_overrides=self._build_pricing_overrides(),
        )

        # Multi-provider support: registry takes precedence
        self._registry = registry
        self._single_provider = provider

        # For backward compatibility, create a default executor with single provider
        # This is used when registry is None
        if provider is not None:
            self.executor = AgentExecutor(provider, workflow_tools=config.tools)
            self.provider = provider  # Keep for backward compatibility
        else:
            # Create a placeholder - will be created per-agent when using registry
            self.executor = None
            self.provider = None

    def _build_pricing_overrides(self) -> dict[str, ModelPricing] | None:
        """Build pricing overrides from workflow cost configuration.

        Converts PricingOverride Pydantic models from the workflow config
        into ModelPricing dataclasses for use by the UsageTracker.

        Returns:
            Dictionary mapping model names to ModelPricing, or None if no overrides.
        """
        cost_config = self.config.workflow.cost
        if not cost_config.pricing:
            return None

        overrides: dict[str, ModelPricing] = {}
        for model_name, pricing_override in cost_config.pricing.items():
            overrides[model_name] = ModelPricing(
                input_per_mtok=pricing_override.input_per_mtok,
                output_per_mtok=pricing_override.output_per_mtok,
                cache_read_per_mtok=pricing_override.cache_read_per_mtok,
                cache_write_per_mtok=pricing_override.cache_write_per_mtok,
            )
        return overrides

    async def _get_executor_for_agent(self, agent: AgentDef) -> AgentExecutor:
        """Get the appropriate executor for an agent.

        When using a ProviderRegistry (multi-provider mode), this creates
        an executor with the provider appropriate for the agent. When using
        a single provider (backward compat mode), returns the shared executor.

        Args:
            agent: The agent definition.

        Returns:
            AgentExecutor configured for the agent's provider.

        Raises:
            ExecutionError: If no provider or registry is configured.
        """
        if self._registry is not None:
            # Multi-provider mode: get provider from registry
            provider = await self._registry.get_provider(agent)
            return AgentExecutor(provider, workflow_tools=self.config.tools)
        elif self.executor is not None:
            # Single provider mode (backward compatibility)
            return self.executor
        else:
            raise ExecutionError(
                "No provider configured for workflow execution",
                suggestion="Provide either a provider or registry to WorkflowEngine",
            )

    async def run(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Execute the workflow from entry_point to $end.

        This is the main entry point for workflow execution. It:
        1. Calls on_start lifecycle hook if defined
        2. Sets up the context with the provided inputs
        3. Enforces iteration and timeout limits
        4. Executes agents in sequence based on routing rules
        5. Calls on_complete/on_error lifecycle hooks as appropriate
        6. Returns the final output built from output templates

        Args:
            inputs: Workflow input values.

        Returns:
            Final output dict built from output templates.

        Raises:
            ExecutionError: If an agent is not found or execution fails.
            MaxIterationsError: If max iterations limit is exceeded.
            TimeoutError: If timeout limit is exceeded.
            ValidationError: If agent output doesn't match schema.
            TemplateError: If template rendering fails.
        """
        # Apply defaults from input schema for optional inputs not provided
        merged_inputs = self._apply_input_defaults(inputs)
        self.context.set_workflow_inputs(merged_inputs)
        self.limits.start()
        current_agent_name = self.config.workflow.entry_point

        # Execute on_start hook
        self._execute_hook("on_start")

        try:
            async with self.limits.timeout_context():
                while True:
                    # Try to find agent, parallel group, or for-each group
                    agent = self._find_agent(current_agent_name)
                    parallel_group = self._find_parallel_group(current_agent_name)
                    for_each_group = self._find_for_each_group(current_agent_name)

                    if agent is None and parallel_group is None and for_each_group is None:
                        raise ExecutionError(
                            f"Agent, parallel group, or for-each group not found: "
                            f"{current_agent_name}",
                            suggestion=(
                                f"Ensure '{current_agent_name}' is defined in the workflow"
                            ),
                        )

                    # Handle for-each group execution
                    if for_each_group is not None:
                        # Check iteration limit (count TBD based on array size)
                        # For safety, check with current limit before resolving array
                        await self._check_iteration_with_prompt(for_each_group.name)

                        # Verbose: Log for-each group execution start
                        iteration = self.limits.current_iteration + 1
                        _verbose_log(
                            f"[{iteration}] Executing for-each group: {for_each_group.name} "
                            f"(source: {for_each_group.source}, "
                            f"{for_each_group.failure_mode} mode)",
                            style="bold cyan",
                        )

                        # Trim context if max_tokens is configured
                        self._trim_context_if_needed()

                        # Execute for-each group with timeout enforcement
                        _group_start = _time.time()
                        for_each_output = await self.limits.wait_for_with_timeout(
                            self._execute_for_each_group(for_each_group),
                            operation_name=f"for-each group '{for_each_group.name}'",
                        )
                        _group_elapsed = _time.time() - _group_start

                        # Verbose: Log for-each group completion
                        _verbose_log_timing(
                            f"For-each group '{for_each_group.name}' completed", _group_elapsed
                        )

                        # Store for-each group output in context
                        # Format: {type: 'for_each', outputs: [...] or {...},
                        #   errors: {key: {...}}, count: N}
                        for_each_output_dict = {
                            "type": "for_each",
                            "outputs": for_each_output.outputs,
                            "errors": {
                                key: {
                                    "item_key": error.item_key,
                                    "exception_type": error.exception_type,
                                    "message": error.message,
                                    "suggestion": error.suggestion,
                                }
                                for key, error in for_each_output.errors.items()
                            },
                            "count": for_each_output.count,
                        }
                        self.context.store(for_each_group.name, for_each_output_dict)

                        # Record execution: count all items that executed
                        self.limits.record_execution(
                            for_each_group.name, count=for_each_output.count
                        )

                        # Check timeout after for-each group
                        self.limits.check_timeout()

                        # Evaluate routes from for-each group
                        route_result = self._evaluate_for_each_routes(
                            for_each_group, for_each_output_dict
                        )

                        # Verbose: Log routing decision
                        _verbose_log_route(route_result.target)

                        if route_result.target == "$end":
                            result = self._build_final_output(route_result.output_transform)
                            self._execute_hook("on_complete", result=result)
                            return result

                        current_agent_name = route_result.target

                    # Handle parallel group execution
                    if parallel_group is not None:
                        # Check iteration limit for all parallel agents before executing
                        await self._check_parallel_group_iteration_with_prompt(
                            parallel_group.name, len(parallel_group.agents)
                        )

                        # Verbose: Log parallel group execution start
                        iteration = self.limits.current_iteration + 1
                        _verbose_log(
                            f"[{iteration}] Executing parallel group: {parallel_group.name} "
                            f"({len(parallel_group.agents)} agents, "
                            f"{parallel_group.failure_mode} mode)",
                            style="bold cyan",
                        )

                        # Trim context if max_tokens is configured
                        self._trim_context_if_needed()

                        # Execute parallel group with timeout enforcement
                        _group_start = _time.time()
                        parallel_output = await self.limits.wait_for_with_timeout(
                            self._execute_parallel_group(parallel_group),
                            operation_name=f"parallel group '{parallel_group.name}'",
                        )
                        _group_elapsed = _time.time() - _group_start

                        # Verbose: Log parallel group completion
                        _verbose_log_timing(
                            f"Parallel group '{parallel_group.name}' completed", _group_elapsed
                        )

                        # Store parallel group output in context
                        # Format: {type: 'parallel', outputs: {agent1: {...}, ...},
                        #   errors: {agent1: {...}}}
                        parallel_output_dict = {
                            "type": "parallel",
                            "outputs": parallel_output.outputs,
                            "errors": {
                                name: {
                                    "agent_name": error.agent_name,
                                    "exception_type": error.exception_type,
                                    "message": error.message,
                                    "suggestion": error.suggestion,
                                }
                                for name, error in parallel_output.errors.items()
                            },
                        }
                        self.context.store(parallel_group.name, parallel_output_dict)

                        # Record execution: count all parallel agents that executed
                        # (both successful and failed agents count toward iteration limit)
                        agent_count = len(parallel_group.agents)
                        self.limits.record_execution(parallel_group.name, count=agent_count)

                        # Check timeout after parallel group
                        self.limits.check_timeout()

                        # Evaluate routes from parallel group
                        route_result = self._evaluate_parallel_routes(
                            parallel_group, parallel_output_dict
                        )

                        # Verbose: Log routing decision
                        _verbose_log_route(route_result.target)

                        if route_result.target == "$end":
                            result = self._build_final_output(route_result.output_transform)
                            self._execute_hook("on_complete", result=result)
                            return result

                        current_agent_name = route_result.target

                    # Handle regular agent execution
                    if agent is not None:
                        # Check iteration limit before executing
                        await self._check_iteration_with_prompt(current_agent_name)

                        # Verbose: Log agent execution start (1-indexed for user display)
                        iteration = self.limits.current_iteration + 1
                        _verbose_log_agent_start(current_agent_name, iteration)

                        # Trim context if max_tokens is configured
                        self._trim_context_if_needed()

                        # Handle human gates
                        if agent.type == "human_gate":
                            # Build context for the gate prompt
                            agent_context = self.context.get_for_template()

                            # Use the gate handler for interaction
                            gate_result: GateResult = await self.gate_handler.handle_gate(
                                agent, agent_context
                            )

                            # Store gate result in context
                            self.context.store(
                                agent.name,
                                {
                                    "selected": gate_result.selected_option.value,
                                    **gate_result.additional_input,
                                },
                            )

                            # Record human gate as executed
                            self.limits.record_execution(agent.name)

                            if gate_result.route == "$end":
                                result = self._build_final_output()
                                self._execute_hook("on_complete", result=result)
                                return result
                            current_agent_name = gate_result.route
                            continue

                        # Build context for this agent
                        agent_context = self.context.build_for_agent(
                            agent.name,
                            agent.input,
                            mode=self.config.workflow.context.mode,
                        )

                        # Execute agent (get executor for multi-provider support)
                        _agent_start = _time.time()
                        executor = await self._get_executor_for_agent(agent)
                        output = await executor.execute(agent, agent_context)
                        _agent_elapsed = _time.time() - _agent_start

                        # Record usage and calculate cost
                        usage = self.usage_tracker.record(agent.name, output, _agent_elapsed)

                        # Verbose: Log agent output summary with cost
                        output_keys = (
                            list(output.content.keys()) if isinstance(output.content, dict) else []
                        )
                        _verbose_log_agent_complete(
                            agent.name,
                            _agent_elapsed,
                            model=output.model,
                            tokens=output.tokens_used,
                            output_keys=output_keys,
                            cost_usd=usage.cost_usd,
                            input_tokens=output.input_tokens,
                            output_tokens=output.output_tokens,
                        )

                        # Store output
                        self.context.store(agent.name, output.content)

                        # Record successful execution
                        self.limits.record_execution(agent.name)

                        # Check timeout after each agent
                        self.limits.check_timeout()

                        # Evaluate routes using the Router
                        route_result = self._evaluate_routes(agent, output.content)

                        # Verbose: Log routing decision
                        _verbose_log_route(route_result.target)

                        if route_result.target == "$end":
                            result = self._build_final_output(route_result.output_transform)
                            self._execute_hook("on_complete", result=result)
                            return result

                        current_agent_name = route_result.target

        except ConductorError as e:
            # Execute on_error hook with error information
            self._execute_hook("on_error", error=e)
            raise
        except Exception as e:
            # Execute on_error hook for unexpected errors
            self._execute_hook("on_error", error=e)
            raise

    def _apply_input_defaults(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Apply default values from input schema for missing optional inputs.

        This ensures all defined inputs are present in the context, either
        with provided values or their schema defaults (None if no default).

        Args:
            inputs: The input values provided at runtime.

        Returns:
            Dictionary with all defined inputs, including defaults for missing optionals.
        """
        merged = inputs.copy()

        for name, input_def in self.config.workflow.input.items():
            if name not in merged:
                # Input not provided - check if it has a default or is optional
                if input_def.default is not None:
                    merged[name] = input_def.default
                elif not input_def.required:
                    # Optional with no default - set to None so templates can check it
                    merged[name] = None

        return merged

    def _execute_hook(
        self,
        hook_name: str,
        result: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> LifecycleHookResult:
        """Execute a lifecycle hook if defined.

        Renders the hook template with the current context plus any
        additional information (result for on_complete, error for on_error).

        Args:
            hook_name: Name of the hook (on_start, on_complete, on_error).
            result: Workflow result (for on_complete hook).
            error: Exception that occurred (for on_error hook).

        Returns:
            LifecycleHookResult with execution status and any rendered result.
        """
        hooks = self.config.workflow.hooks
        if hooks is None:
            return LifecycleHookResult(hook_name=hook_name, executed=False)

        hook_template = getattr(hooks, hook_name, None)
        if not hook_template:
            return LifecycleHookResult(hook_name=hook_name, executed=False)

        try:
            # Build context for hook template
            ctx = self.context.get_for_template()

            # Add hook-specific context
            if result is not None:
                ctx["result"] = result

            if error is not None:
                ctx["error"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                }
                if hasattr(error, "suggestion") and error.suggestion:
                    ctx["error"]["suggestion"] = error.suggestion

            # Render the hook template
            rendered = self.renderer.render(hook_template, ctx)

            return LifecycleHookResult(
                hook_name=hook_name,
                executed=True,
                result=rendered,
            )

        except Exception as e:
            # Hook execution errors should not fail the workflow
            return LifecycleHookResult(
                hook_name=hook_name,
                executed=True,
                error=str(e),
            )

    def _trim_context_if_needed(self) -> None:
        """Trim context if max_tokens is configured and exceeded.

        Uses the configured trim_strategy or defaults to drop_oldest.

        Note: When using multi-provider mode (registry), the summarize strategy
        requires a provider but may not have one available. In that case,
        it falls back to drop_oldest.
        """
        context_config = self.config.workflow.context
        if context_config.max_tokens is None:
            return

        current_tokens = self.context.estimate_context_tokens()
        if current_tokens <= context_config.max_tokens:
            return

        strategy = context_config.trim_strategy or "drop_oldest"

        # Get provider for summarize strategy
        # In multi-provider mode, use the default provider if available
        provider = None
        if strategy == "summarize":
            if self._single_provider is not None:
                provider = self._single_provider
            elif self._registry is not None:
                # Check if the default provider is already active
                default_type = self._registry.default_provider_type
                if self._registry.is_provider_active(default_type):
                    provider = self._registry.get_active_providers().get(default_type)
                # If no provider is active yet, fall back to drop_oldest
                if provider is None:
                    _verbose_log(
                        "Summarize strategy unavailable in multi-provider mode "
                        "before first agent execution. Falling back to drop_oldest.",
                        style="yellow dim",
                    )
                    strategy = "drop_oldest"

        self.context.trim_context(
            max_tokens=context_config.max_tokens,
            strategy=strategy,
            provider=provider,
        )

    async def _check_iteration_with_prompt(self, agent_name: str) -> None:
        """Check iteration limit with interactive prompt on limit reached.

        This method wraps the standard iteration check with interactive handling.
        When the limit is reached, it prompts the user for additional iterations
        instead of immediately raising MaxIterationsError.

        Args:
            agent_name: Name of agent about to execute.

        Raises:
            MaxIterationsError: If limit exceeded and user chooses not to continue.
        """
        try:
            self.limits.check_iteration(agent_name)
        except MaxIterationsError:
            # Prompt user for more iterations
            result = await self.max_iterations_handler.handle_limit_reached(
                current_iteration=self.limits.current_iteration,
                max_iterations=self.limits.max_iterations,
                agent_history=self.limits.execution_history,
            )
            if result.continue_execution:
                self.limits.increase_limit(result.additional_iterations)
                # Re-check should now pass
                self.limits.check_iteration(agent_name)
            else:
                raise  # Re-raise MaxIterationsError

    async def _check_parallel_group_iteration_with_prompt(
        self, group_name: str, agent_count: int
    ) -> None:
        """Check parallel group iteration limit with interactive prompt.

        This method wraps the parallel group iteration check with interactive handling.
        When the limit would be exceeded, it prompts the user for additional iterations.

        Args:
            group_name: Name of parallel group about to execute.
            agent_count: Number of agents in the parallel group.

        Raises:
            MaxIterationsError: If limit exceeded and user chooses not to continue.
        """
        try:
            self.limits.check_parallel_group_iteration(group_name, agent_count)
        except MaxIterationsError:
            # Prompt user for more iterations
            result = await self.max_iterations_handler.handle_limit_reached(
                current_iteration=self.limits.current_iteration,
                max_iterations=self.limits.max_iterations,
                agent_history=self.limits.execution_history,
            )
            if result.continue_execution:
                self.limits.increase_limit(result.additional_iterations)
                # Re-check should now pass
                self.limits.check_parallel_group_iteration(group_name, agent_count)
            else:
                raise  # Re-raise MaxIterationsError

    def _find_agent(self, name: str) -> AgentDef | None:
        """Find agent by name.

        Args:
            name: The agent name to find.

        Returns:
            The agent definition if found, None otherwise.
        """
        return next((a for a in self.config.agents if a.name == name), None)

    def _find_parallel_group(self, name: str) -> ParallelGroup | None:
        """Find parallel group by name.

        Args:
            name: The parallel group name to find.

        Returns:
            The parallel group definition if found, None otherwise.
        """
        return next((p for p in self.config.parallel if p.name == name), None)

    def _find_for_each_group(self, name: str) -> ForEachDef | None:
        """Find for-each group by name.

        Args:
            name: The for-each group name to find.

        Returns:
            The for-each group definition if found, None otherwise.
        """
        return next((f for f in self.config.for_each if f.name == name), None)

    def _resolve_array_reference(self, source: str) -> list[Any]:
        """Resolve a source reference to a runtime array from workflow context.

        Navigates dotted path notation to extract an array from agent outputs.
        Handles the same wrapping logic as build_for_agent (regular agents are
        wrapped with {"output": ...}, parallel/for-each groups are stored directly).

        Example:
            source = "finder.output.kpis"
            1. Lookup agent_outputs["finder"]
            2. Wrap with {"output": ...} if not a parallel/for-each group
            3. Navigate to ["output"]["kpis"]
            4. Return the array value

        Args:
            source: Dotted path reference (e.g., 'finder.output.kpis').

        Returns:
            The resolved array (list).

        Raises:
            ExecutionError: If path doesn't exist, value is not an array.
        """
        parts = source.split(".")

        if len(parts) < 3:
            raise ExecutionError(
                f"Invalid source reference format: '{source}'",
                suggestion="Source must have at least 3 parts (e.g., 'agent_name.output.field')",
            )

        # First part is the agent name
        agent_name = parts[0]

        # Check if agent output exists
        if agent_name not in self.context.agent_outputs:
            # Provide helpful suggestion about execution order
            executed = list(self.context.agent_outputs.keys())
            if executed:
                raise ExecutionError(
                    f"Agent '{agent_name}' output not found for source '{source}'",
                    suggestion=f"Agent '{agent_name}' must execute before this for-each group. "
                    f"Executed agents so far: {executed}",
                )
            else:
                raise ExecutionError(
                    f"Agent '{agent_name}' output not found for source '{source}'",
                    suggestion=f"Agent '{agent_name}' must execute before this for-each group",
                )

        # Get the agent's raw output
        raw_output = self.context.agent_outputs[agent_name]

        # Check if this is a parallel/for-each group output
        # (has 'outputs' and 'errors' keys at top level)
        is_group_output = (
            isinstance(raw_output, dict) and "outputs" in raw_output and "errors" in raw_output
        )

        # Wrap regular agent outputs with {"output": ...}
        # (matches the behavior of build_for_agent)
        wrapped_output = raw_output if is_group_output else {"output": raw_output}

        # Navigate through the dotted path (starting from second part)
        current = wrapped_output
        path_traversed = [agent_name]

        for part in parts[1:]:
            path_traversed.append(part)

            if not isinstance(current, dict):
                parent_path = ".".join(path_traversed[:-1])
                raise ExecutionError(
                    f"Cannot navigate to '{part}' in source '{source}': "
                    f"'{parent_path}' is not a dictionary (type: {type(current).__name__})",
                    suggestion=f"Check that '{parent_path}' returns a dictionary structure",
                )

            if part not in current:
                parent_path = ".".join(path_traversed[:-1])
                available_keys = list(current.keys()) if isinstance(current, dict) else []
                raise ExecutionError(
                    f"Field '{part}' not found in '{parent_path}' for source '{source}'",
                    suggestion=(
                        f"Available keys: {available_keys}"
                        if available_keys
                        else f"Check the output structure of '{agent_name}'"
                    ),
                )

            current = current[part]

        # Validate that the final value is a list or tuple
        if not isinstance(current, (list, tuple)):
            raise ExecutionError(
                f"Source '{source}' resolved to {type(current).__name__}, expected list or tuple",
                suggestion=f"Ensure '{source}' returns an array/list from the agent output",
            )

        return current

    def _inject_loop_variables(
        self,
        context: dict[str, Any],
        var_name: str,
        item: Any,
        index: int,
        key: str | None = None,
    ) -> None:
        """Inject loop variables into an agent's context dictionary.

        This method modifies the context dictionary in-place to add loop variables
        that are accessible in agent templates during for-each execution.

        Loop variables injected:
        - {{ <var_name> }}: The current item from the array
        - {{ _index }}: Zero-based index of the current item
        - {{ _key }}: Extracted key value (if key_by is specified in ForEachDef)

        Example:
            For-each definition: `for_each.as_="kpi"`, item={kpi_id: "K1"}, index=0
            After injection, templates can use:
            - {{ kpi.kpi_id }} → "K1"
            - {{ _index }} → 0
            - {{ _key }} → "K1" (if key_by="kpi.kpi_id")

        Args:
            context: The context dictionary to inject variables into (modified in-place).
            var_name: The loop variable name (from ForEachDef.as_).
            item: The current array item being processed.
            index: Zero-based index of the current item in the source array.
            key: Optional extracted key value (if key_by is specified).

        Note:
            This method assumes var_name has already been validated to not conflict
            with reserved names (workflow, context, output, _index, _key).
        """
        # Inject the loop variable (e.g., {{ kpi }})
        context[var_name] = item

        # Inject the index variable (e.g., {{ _index }})
        context["_index"] = index

        # Inject the key variable if provided (e.g., {{ _key }})
        if key is not None:
            context["_key"] = key

    async def _execute_parallel_group(self, parallel_group: ParallelGroup) -> ParallelGroupOutput:
        """Execute agents in parallel with context isolation.

        This method:
        1. Creates an immutable context snapshot for all parallel agents
        2. Executes all agents concurrently using asyncio.gather()
        3. Aggregates successful outputs and errors
        4. Applies the failure mode policy

        Args:
            parallel_group: The parallel group definition.

        Returns:
            ParallelGroupOutput with aggregated outputs and errors.

        Raises:
            ExecutionError: Based on failure_mode:
                - fail_fast: Immediately on first agent failure
                - all_or_nothing: If any agent fails after all complete
                - continue_on_error: If all agents fail
        """
        # Verbose: Log parallel group start
        _verbose_log_parallel_start(parallel_group.name, len(parallel_group.agents))

        # Track timing for summary
        _group_start = _time.time()

        # Create immutable context snapshot
        context_snapshot = copy.deepcopy(self.context)

        # Find and validate agents immediately
        agent_names = parallel_group.agents
        agents = []
        for name in agent_names:
            agent = self._find_agent(name)
            if agent is None:
                raise ExecutionError(
                    f"Agent not found in parallel group: {name}",
                    suggestion=f"Ensure '{name}' is defined in the workflow",
                )
            agents.append(agent)

        async def execute_single_agent(agent: AgentDef) -> tuple[str, Any]:
            """Execute a single agent with the context snapshot.

            Returns:
                Tuple of (agent_name, output_content, elapsed, model, tokens)

            Raises:
                Exception: Any exception from agent execution (wrapped).
            """
            _agent_start = _time.time()
            try:
                # Build context for this agent using the snapshot
                agent_context = context_snapshot.build_for_agent(
                    agent.name,
                    agent.input,
                    mode=self.config.workflow.context.mode,
                )

                # Execute agent (get executor for multi-provider support)
                executor = await self._get_executor_for_agent(agent)
                output = await executor.execute(agent, agent_context)
                _agent_elapsed = _time.time() - _agent_start

                # Record usage and calculate cost
                usage = self.usage_tracker.record(agent.name, output, _agent_elapsed)

                # Verbose: Log agent completion with cost
                _verbose_log_parallel_agent_complete(
                    agent.name,
                    _agent_elapsed,
                    model=output.model,
                    tokens=output.tokens_used,
                    cost_usd=usage.cost_usd,
                )

                # Individual parallel agents are counted toward iteration limit
                # at the parallel group level after all agents complete
                return (agent.name, output.content)
            except Exception as e:
                _agent_elapsed = _time.time() - _agent_start

                # Verbose: Log agent failure
                _verbose_log_parallel_agent_failed(
                    agent.name,
                    _agent_elapsed,
                    type(e).__name__,
                    str(e),
                )

                # Wrap exception with agent name and timing for better error reporting
                if not hasattr(e, "_parallel_agent_name"):
                    e._parallel_agent_name = agent.name  # type: ignore
                if not hasattr(e, "_parallel_agent_elapsed"):
                    e._parallel_agent_elapsed = _agent_elapsed  # type: ignore
                raise

        # Execute based on failure mode
        parallel_output = ParallelGroupOutput()

        if parallel_group.failure_mode == "fail_fast":
            # Fail immediately on first error
            try:
                results = await asyncio.gather(
                    *[execute_single_agent(agent) for agent in agents],
                    return_exceptions=False,
                )
                # All succeeded
                for agent_name, output_content in results:
                    parallel_output.outputs[agent_name] = output_content

            except Exception as e:
                # Extract agent name and exception type from wrapped exception
                agent_name = getattr(e, "_parallel_agent_name", "unknown")
                exception_type = type(e).__name__

                # Create error message with exception type and mode
                if agent_name != "unknown":
                    error_msg = (
                        f"Agent '{agent_name}' in parallel group '{parallel_group.name}' "
                        f"failed (fail_fast mode): {exception_type}: {str(e)}"
                    )
                else:
                    error_msg = (
                        f"Parallel group '{parallel_group.name}' failed (fail_fast mode): "
                        f"{exception_type}: {str(e)}"
                    )

                suggestion = getattr(e, "suggestion", None)
                raise ExecutionError(
                    error_msg,
                    suggestion=suggestion or "Check agent configuration and inputs",
                ) from e
            finally:
                # Verbose: Log summary even on failure
                _group_elapsed = _time.time() - _group_start
                _verbose_log_parallel_summary(
                    parallel_group.name,
                    len(parallel_output.outputs),
                    len(parallel_output.errors),
                    _group_elapsed,
                )

        elif parallel_group.failure_mode == "continue_on_error":
            # Collect all results and exceptions
            results = await asyncio.gather(
                *[execute_single_agent(agent) for agent in agents],
                return_exceptions=True,
            )

            # Separate successes and failures
            for i, result in enumerate(results):
                agent_name = agent_names[i]

                if isinstance(result, Exception):
                    # Agent failed - store error
                    parallel_output.errors[agent_name] = ParallelAgentError(
                        agent_name=agent_name,
                        exception_type=type(result).__name__,
                        message=str(result),
                        suggestion=getattr(result, "suggestion", None),
                    )
                else:
                    # Agent succeeded - store output
                    # result is a tuple (agent_name, output_content) when not an Exception
                    success_result: tuple[str, Any] = result  # type: ignore[assignment]
                    agent_name_from_result, output_content = success_result
                    parallel_output.outputs[agent_name_from_result] = output_content

            # Verbose: Log summary
            _group_elapsed = _time.time() - _group_start
            _verbose_log_parallel_summary(
                parallel_group.name,
                len(parallel_output.outputs),
                len(parallel_output.errors),
                _group_elapsed,
            )

            # Fail if ALL agents failed
            if len(parallel_output.outputs) == 0:
                error_details = []
                for agent_name, error in parallel_output.errors.items():
                    error_line = f"  - {agent_name}: {error.exception_type}: {error.message}"
                    if error.suggestion:
                        error_line += f" (Suggestion: {error.suggestion})"
                    error_details.append(error_line)
                error_msg = (
                    f"All agents in parallel group '{parallel_group.name}' failed:\n"
                    + "\n".join(error_details)
                )
                raise ExecutionError(
                    error_msg,
                    suggestion="At least one agent must succeed in continue_on_error mode",
                )

        elif parallel_group.failure_mode == "all_or_nothing":
            # Execute all agents and collect results
            results = await asyncio.gather(
                *[execute_single_agent(agent) for agent in agents],
                return_exceptions=True,
            )

            # Separate successes and failures
            for i, result in enumerate(results):
                agent_name = agent_names[i]

                if isinstance(result, Exception):
                    # Agent failed - store error
                    parallel_output.errors[agent_name] = ParallelAgentError(
                        agent_name=agent_name,
                        exception_type=type(result).__name__,
                        message=str(result),
                        suggestion=getattr(result, "suggestion", None),
                    )
                else:
                    # Agent succeeded - store output
                    # result is a tuple (agent_name, output_content) when not an Exception
                    success_result: tuple[str, Any] = result  # type: ignore[assignment]
                    agent_name_from_result, output_content = success_result
                    parallel_output.outputs[agent_name_from_result] = output_content

            # Verbose: Log summary
            _group_elapsed = _time.time() - _group_start
            _verbose_log_parallel_summary(
                parallel_group.name,
                len(parallel_output.outputs),
                len(parallel_output.errors),
                _group_elapsed,
            )

            # Fail if ANY agent failed
            if len(parallel_output.errors) > 0:
                error_details = []
                for agent_name, error in parallel_output.errors.items():
                    error_line = f"  - {agent_name}: {error.exception_type}: {error.message}"
                    if error.suggestion:
                        error_line += f" (Suggestion: {error.suggestion})"
                    error_details.append(error_line)
                success_count = len(parallel_output.outputs)
                failure_count = len(parallel_output.errors)
                error_msg = (
                    f"Parallel group '{parallel_group.name}' failed "
                    f"({success_count} succeeded, {failure_count} failed):\n"
                    + "\n".join(error_details)
                )
                raise ExecutionError(
                    error_msg,
                    suggestion="All agents must succeed in all_or_nothing mode",
                )

        return parallel_output

    def _extract_key_from_item(self, item: Any, key_by_path: str, fallback_index: int) -> str:
        """Extract a key from an item using a dotted path.

        Args:
            item: The item to extract the key from.
            key_by_path: Dotted path to the key field (e.g., "kpi.kpi_id").
            fallback_index: Index to use as fallback if extraction fails.

        Returns:
            The extracted key as a string, or the fallback index as a string if extraction fails.
        """
        try:
            # Navigate key_by path (e.g., "kpi.kpi_id")
            key_parts = key_by_path.split(".")
            current = item
            for part in key_parts:
                current = current[part] if isinstance(current, dict) else getattr(current, part)
            return str(current)
        except (KeyError, AttributeError, IndexError) as e:
            # Fallback to index-based key if extraction fails
            _verbose_log(
                f"Warning: Failed to extract key from item {fallback_index} "
                f"using '{key_by_path}': {e}. Falling back to index-based key.",
                style="dim yellow",
            )
            return str(fallback_index)

    async def _execute_for_each_group(self, for_each_group: ForEachDef) -> ForEachGroupOutput:
        """Execute for-each group with batched parallel execution.

        This method:
        1. Resolves the source array from workflow context
        2. Creates an immutable context snapshot for all items
        3. Processes items in sequential batches of max_concurrent size
        4. Injects loop variables ({{ var }}, {{ _index }}, {{ _key }}) into each agent's context
        5. Aggregates outputs (list or dict based on key_by)
        6. Applies the failure mode policy

        Args:
            for_each_group: The for-each group definition.

        Returns:
            ForEachGroupOutput with aggregated outputs and errors.

        Raises:
            ExecutionError: Based on failure_mode:
                - fail_fast: Immediately on first item failure
                - all_or_nothing: If any item fails after all complete
                - continue_on_error: If all items fail
        """
        # Resolve the source array from context
        items = self._resolve_array_reference(for_each_group.source)

        # Handle empty arrays gracefully
        if not items:
            _verbose_log(
                f"For-each group '{for_each_group.name}': Empty array, skipping execution",
                style="dim yellow",
            )
            # Return empty output with appropriate structure
            empty_outputs = {} if for_each_group.key_by else []
            return ForEachGroupOutput(outputs=empty_outputs, errors={}, count=0)

        # Verbose: Log for-each group start
        _verbose_log_for_each_start(
            for_each_group.name,
            len(items),
            for_each_group.max_concurrent,
            for_each_group.failure_mode,
        )

        # Track timing for summary
        _group_start = _time.time()

        # Create immutable context snapshot (shared across all items)
        context_snapshot = copy.deepcopy(self.context)

        # Extract keys if key_by is specified
        item_keys: list[str] = []
        if for_each_group.key_by:
            for idx, item in enumerate(items):
                item_keys.append(self._extract_key_from_item(item, for_each_group.key_by, idx))
        else:
            # Use index-based keys
            item_keys = [str(i) for i in range(len(items))]

        async def execute_single_item(item: Any, index: int, key: str) -> tuple[str, Any]:
            """Execute a single for-each item with injected loop variables.

            Returns:
                Tuple of (item_key, output_content)

            Raises:
                Exception: Any exception from agent execution (wrapped with metadata).
            """
            _item_start = _time.time()
            try:
                # Build context for this item using the snapshot
                agent_context = context_snapshot.build_for_agent(
                    for_each_group.agent.name,
                    for_each_group.agent.input,
                    mode=self.config.workflow.context.mode,
                )

                # Inject loop variables into context
                self._inject_loop_variables(
                    agent_context,
                    for_each_group.as_,
                    item,
                    index,
                    key if for_each_group.key_by else None,
                )

                # Execute agent with injected context (get executor for multi-provider)
                executor = await self._get_executor_for_agent(for_each_group.agent)
                output = await executor.execute(for_each_group.agent, agent_context)
                _item_elapsed = _time.time() - _item_start

                # Record usage and calculate cost
                usage = self.usage_tracker.record(
                    f"{for_each_group.name}[{key}]", output, _item_elapsed
                )

                # Verbose: Log item completion with cost
                _verbose_log_for_each_item_complete(
                    key,
                    _item_elapsed,
                    tokens=output.tokens_used,
                    cost_usd=usage.cost_usd,
                )

                return (key, output.content)
            except Exception as e:
                _item_elapsed = _time.time() - _item_start

                # Verbose: Log item failure
                _verbose_log_for_each_item_failed(
                    key,
                    _item_elapsed,
                    type(e).__name__,
                    str(e),
                )

                # Attach metadata for error reporting
                if not hasattr(e, "_for_each_item_key"):
                    e._for_each_item_key = key  # type: ignore
                if not hasattr(e, "_for_each_item_elapsed"):
                    e._for_each_item_elapsed = _item_elapsed  # type: ignore
                raise

        # Process items in sequential batches
        for_each_output = ForEachGroupOutput(
            outputs={} if for_each_group.key_by else [], errors={}, count=len(items)
        )

        # Determine batch size
        max_concurrent = for_each_group.max_concurrent
        batch_count = (len(items) + max_concurrent - 1) // max_concurrent

        for batch_idx in range(batch_count):
            batch_start_idx = batch_idx * max_concurrent
            batch_end_idx = min((batch_idx + 1) * max_concurrent, len(items))
            batch_items = items[batch_start_idx:batch_end_idx]
            batch_keys = item_keys[batch_start_idx:batch_end_idx]

            _verbose_log(
                f"Batch {batch_idx + 1}/{batch_count}: "
                f"Processing items {batch_start_idx} to {batch_end_idx - 1}",
                style="dim cyan",
            )

            # Execute based on failure mode
            if for_each_group.failure_mode == "fail_fast":
                # Fail immediately on first error
                try:
                    results = await asyncio.gather(
                        *[
                            execute_single_item(item, batch_start_idx + i, batch_keys[i])
                            for i, item in enumerate(batch_items)
                        ],
                        return_exceptions=False,
                    )
                    # All succeeded - store outputs
                    for item_key, output_content in results:
                        if for_each_group.key_by:
                            for_each_output.outputs[item_key] = output_content
                        else:
                            for_each_output.outputs.append(output_content)  # type: ignore[union-attr]

                except Exception as e:
                    # Extract item key from wrapped exception
                    item_key = getattr(e, "_for_each_item_key", "unknown")
                    exception_type = type(e).__name__

                    error_msg = (
                        f"Item '{item_key}' in for-each group '{for_each_group.name}' "
                        f"failed (fail_fast mode): {exception_type}: {str(e)}"
                    )

                    suggestion = getattr(e, "suggestion", None)
                    raise ExecutionError(
                        error_msg,
                        suggestion=suggestion or "Check item data and agent configuration",
                    ) from e

            elif for_each_group.failure_mode == "continue_on_error":
                # Collect all results and exceptions
                results = await asyncio.gather(
                    *[
                        execute_single_item(item, batch_start_idx + i, batch_keys[i])
                        for i, item in enumerate(batch_items)
                    ],
                    return_exceptions=True,
                )

                # Separate successes and failures
                for i, result in enumerate(results):
                    item_key = batch_keys[i]

                    if isinstance(result, Exception):
                        # Item failed - store error
                        for_each_output.errors[item_key] = ForEachError(
                            item_key=item_key,
                            exception_type=type(result).__name__,
                            message=str(result),
                            suggestion=getattr(result, "suggestion", None),
                        )
                    else:
                        # Item succeeded - store output
                        # result is a tuple (key, output) when not an Exception
                        success_result: tuple[str, Any] = result  # type: ignore[assignment]
                        key_from_result, output_content = success_result
                        if for_each_group.key_by:
                            for_each_output.outputs[key_from_result] = output_content  # type: ignore[index]
                        else:
                            for_each_output.outputs.append(output_content)  # type: ignore[union-attr]

            elif for_each_group.failure_mode == "all_or_nothing":
                # Execute all items and collect results
                results = await asyncio.gather(
                    *[
                        execute_single_item(item, batch_start_idx + i, batch_keys[i])
                        for i, item in enumerate(batch_items)
                    ],
                    return_exceptions=True,
                )

                # Separate successes and failures
                for i, result in enumerate(results):
                    item_key = batch_keys[i]

                    if isinstance(result, Exception):
                        # Item failed - store error
                        for_each_output.errors[item_key] = ForEachError(
                            item_key=item_key,
                            exception_type=type(result).__name__,
                            message=str(result),
                            suggestion=getattr(result, "suggestion", None),
                        )
                    else:
                        # Item succeeded - store output
                        # result is a tuple (key, output) when not an Exception
                        success_result: tuple[str, Any] = result  # type: ignore[assignment]
                        key_from_result, output_content = success_result
                        if for_each_group.key_by:
                            for_each_output.outputs[key_from_result] = output_content  # type: ignore[index]
                        else:
                            for_each_output.outputs.append(output_content)  # type: ignore[union-attr]

        # Verbose: Log summary
        _group_elapsed = _time.time() - _group_start
        success_count = (
            len(for_each_output.outputs)
            if isinstance(for_each_output.outputs, dict)
            else len(for_each_output.outputs)
        )
        failure_count = len(for_each_output.errors)
        _verbose_log_for_each_summary(
            for_each_group.name,
            success_count,
            failure_count,
            _group_elapsed,
        )

        # Apply failure mode policy (for continue_on_error and all_or_nothing)
        if for_each_group.failure_mode == "continue_on_error":
            # Fail if ALL items failed
            if success_count == 0:
                error_details = []
                for item_key, error in for_each_output.errors.items():
                    error_line = f"  - [{item_key}]: {error.exception_type}: {error.message}"
                    if error.suggestion:
                        error_line += f" (Suggestion: {error.suggestion})"
                    error_details.append(error_line)
                error_msg = (
                    f"All items in for-each group '{for_each_group.name}' failed:\n"
                    + "\n".join(error_details)
                )
                raise ExecutionError(
                    error_msg,
                    suggestion="At least one item must succeed in continue_on_error mode",
                )

        elif for_each_group.failure_mode == "all_or_nothing" and failure_count > 0:
            # Fail if ANY item failed
            error_details = []
            for item_key, error in for_each_output.errors.items():
                error_line = f"  - [{item_key}]: {error.exception_type}: {error.message}"
                if error.suggestion:
                    error_line += f" (Suggestion: {error.suggestion})"
                error_details.append(error_line)
            error_msg = (
                f"For-each group '{for_each_group.name}' failed "
                f"({success_count} succeeded, {failure_count} failed):\n" + "\n".join(error_details)
            )
            raise ExecutionError(
                error_msg,
                suggestion="All items must succeed in all_or_nothing mode",
            )

        return for_each_output

    def _get_next_agent(self, agent: AgentDef, output: dict[str, Any]) -> str:
        """Get next agent from routes (legacy method, use _evaluate_routes instead).

        This method is kept for backward compatibility but delegates to _evaluate_routes.

        Args:
            agent: The current agent definition.
            output: The agent's output content.

        Returns:
            The name of the next agent or "$end".
        """
        result = self._evaluate_routes(agent, output)
        return result.target

    def _evaluate_routes(self, agent: AgentDef, output: dict[str, Any]) -> RouteResult:
        """Evaluate routes using the Router.

        Uses the Router to evaluate routing rules and determine the next agent.
        Supports both Jinja2 template conditions and simpleeval arithmetic expressions.

        Args:
            agent: The current agent definition.
            output: The agent's output content.

        Returns:
            RouteResult with target and optional output transform.
        """
        if not agent.routes:
            # No routes defined - default to $end
            return RouteResult(target="$end")

        # Build context for condition evaluation
        eval_context = self.context.get_for_template()

        return self.router.evaluate(agent.routes, output, eval_context)

    def _evaluate_parallel_routes(
        self, parallel_group: ParallelGroup, output: dict[str, Any]
    ) -> RouteResult:
        """Evaluate routes from a parallel group using the Router.

        Uses the Router to evaluate routing rules and determine the next agent
        after a parallel group completes.

        Args:
            parallel_group: The parallel group definition.
            output: The parallel group's aggregated output.

        Returns:
            RouteResult with target and optional output transform.
        """
        if not parallel_group.routes:
            # No routes defined - default to $end
            return RouteResult(target="$end")

        # Build context for condition evaluation
        eval_context = self.context.get_for_template()

        return self.router.evaluate(parallel_group.routes, output, eval_context)

    def _evaluate_for_each_routes(
        self, for_each_group: ForEachDef, output: dict[str, Any]
    ) -> RouteResult:
        """Evaluate routes from a for-each group using the Router.

        Uses the Router to evaluate routing rules and determine the next agent
        after a for-each group completes.

        Args:
            for_each_group: The for-each group definition.
            output: The for-each group's aggregated output.

        Returns:
            RouteResult with target and optional output transform.
        """
        if not for_each_group.routes:
            # No routes defined - default to $end
            return RouteResult(target="$end")

        # Build context for condition evaluation
        eval_context = self.context.get_for_template()

        return self.router.evaluate(for_each_group.routes, output, eval_context)

    def _build_final_output(
        self, route_output_transform: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Build final output using output templates.

        Renders each output template expression with the full context.
        If a route output transform is provided, it will be merged with
        the template-rendered output (transform values take precedence).

        Args:
            route_output_transform: Optional output values from the $end route.

        Returns:
            Dict with rendered output values.
        """
        ctx = self.context.get_for_template()
        result: dict[str, Any] = {}

        for key, template in self.config.output.items():
            rendered = self.renderer.render(template, ctx)
            # Try to parse as JSON if it looks like JSON
            result[key] = self._maybe_parse_json(rendered)

        # Merge route output transform if provided (takes precedence)
        if route_output_transform:
            for key, value in route_output_transform.items():
                result[key] = self._maybe_parse_json(value) if isinstance(value, str) else value

        return result

    @staticmethod
    def _maybe_parse_json(value: str) -> Any:
        """Attempt to parse a string as JSON.

        Args:
            value: The string to parse.

        Returns:
            Parsed JSON value if successful, original string otherwise.
        """
        import json

        stripped = value.strip()
        if stripped.startswith(("{", "[", '"')) or stripped in ("true", "false", "null"):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                pass
        # Try to convert numeric strings
        try:
            if "." in stripped:
                return float(stripped)
            return int(stripped)
        except ValueError:
            pass
        return value

    def get_execution_summary(self) -> dict[str, Any]:
        """Get a summary of the workflow execution.

        Returns:
            Dict with execution statistics including iterations,
            agents executed, context mode, elapsed time, limits, and
            parallel group statistics.
        """
        # Count parallel group executions from execution history
        parallel_groups_executed = []
        for name in self.limits.execution_history:
            # Check if this name corresponds to a parallel group
            if self._find_parallel_group(name) is not None:
                parallel_groups_executed.append(name)

        # Count individual parallel agents that executed
        parallel_agents_count = 0
        for group_name in parallel_groups_executed:
            parallel_group = self._find_parallel_group(group_name)
            if parallel_group is not None:
                parallel_agents_count += len(parallel_group.agents)

        summary = {
            "iterations": self.limits.current_iteration,
            "agents_executed": self.limits.execution_history.copy(),
            "context_mode": self.config.workflow.context.mode,
            "elapsed_seconds": self.limits.get_elapsed_time(),
            "max_iterations": self.limits.max_iterations,
            "timeout_seconds": self.limits.timeout_seconds,
        }

        # Add parallel group stats if any were executed
        if parallel_groups_executed:
            summary["parallel_groups_executed"] = parallel_groups_executed
            summary["parallel_agents_count"] = parallel_agents_count

        # Add usage/cost information
        usage = self.usage_tracker.get_summary()
        summary["usage"] = {
            "total_input_tokens": usage.total_input_tokens,
            "total_output_tokens": usage.total_output_tokens,
            "total_tokens": usage.total_tokens,
            "total_cost_usd": usage.total_cost_usd,
            "agents": [
                {
                    "agent_name": a.agent_name,
                    "model": a.model,
                    "input_tokens": a.input_tokens,
                    "output_tokens": a.output_tokens,
                    "cost_usd": a.cost_usd,
                    "elapsed_seconds": a.elapsed_seconds,
                }
                for a in usage.agents
            ],
        }

        return summary

    def build_execution_plan(self) -> ExecutionPlan:
        """Build an execution plan by analyzing the workflow.

        This traces all possible paths through the workflow without
        actually executing any agents. Used for --dry-run mode.

        Returns:
            ExecutionPlan with steps and possible paths.
        """
        plan = ExecutionPlan(
            workflow_name=self.config.workflow.name,
            entry_point=self.config.workflow.entry_point,
            max_iterations=self.config.workflow.limits.max_iterations,
            timeout_seconds=self.config.workflow.limits.timeout_seconds,
        )

        visited: set[str] = set()
        loop_targets: set[str] = set()

        # Trace from entry_point
        self._trace_path(
            self.config.workflow.entry_point,
            plan,
            visited,
            loop_targets,
        )

        # Mark loop targets in steps
        for step in plan.steps:
            if step.agent_name in loop_targets:
                step.is_loop_target = True

        return plan

    def _trace_path(
        self,
        agent_name: str,
        plan: ExecutionPlan,
        visited: set[str],
        loop_targets: set[str],
    ) -> None:
        """Recursively trace execution path from an agent or parallel group.

        This method performs a depth-first traversal of the workflow graph,
        building up the execution plan with all reachable agents and parallel groups.

        Args:
            agent_name: Name of the current agent or parallel group to trace.
            plan: The execution plan being built.
            visited: Set of already visited names (to detect loops).
            loop_targets: Set of names that are targets of loop-back routes.
        """
        if agent_name == "$end":
            return

        # Try to find agent first, then parallel group
        agent = self._find_agent(agent_name)
        parallel_group = self._find_parallel_group(agent_name)

        if agent is None and parallel_group is None:
            return

        # Check for loop
        is_loop = agent_name in visited
        if is_loop:
            # Mark as loop target and don't recurse further
            loop_targets.add(agent_name)
            return

        visited.add(agent_name)

        # Handle parallel group
        if parallel_group is not None:
            routes_info: list[dict[str, Any]] = []
            route_targets: list[str] = []

            if parallel_group.routes:
                for route in parallel_group.routes:
                    routes_info.append(
                        {
                            "to": route.to,
                            "when": route.when,
                            "is_conditional": route.when is not None,
                        }
                    )
                    route_targets.append(route.to)

            # Build step for parallel group
            step = ExecutionStep(
                agent_name=parallel_group.name,
                agent_type="parallel_group",
                model=None,
                routes=routes_info,
                is_loop_target=False,  # Will be updated after traversal
                parallel_agents=parallel_group.agents.copy(),
                failure_mode=parallel_group.failure_mode,
            )
            plan.steps.append(step)

            # Trace routes from parallel group
            for target in route_targets:
                if target != "$end":
                    self._trace_path(target, plan, visited, loop_targets)

            return

        # Handle regular agent
        if agent is not None:
            # Get routes from the agent (handle both regular agents and human gates)
            routes_info = []
            route_targets = []

            if agent.routes:
                for route in agent.routes:
                    routes_info.append(
                        {
                            "to": route.to,
                            "when": route.when,
                            "is_conditional": route.when is not None,
                        }
                    )
                    route_targets.append(route.to)
            elif agent.options:
                # Human gate with options
                for option in agent.options:
                    routes_info.append(
                        {
                            "to": option.route,
                            "when": f"selection == '{option.value}'",
                            "is_conditional": True,
                            "label": option.label,
                        }
                    )
                    route_targets.append(option.route)

            # Build step
            step = ExecutionStep(
                agent_name=agent_name,
                agent_type=agent.type or "agent",
                model=agent.model,
                routes=routes_info,
                is_loop_target=False,  # Will be updated after traversal
            )
            plan.steps.append(step)

            # Trace routes
            for target in route_targets:
                if target != "$end":
                    self._trace_path(target, plan, visited, loop_targets)
