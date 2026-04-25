"""Workflow execution engine for Conductor.

This module provides the WorkflowEngine class for orchestrating
multi-agent workflow execution.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from conductor.engine.checkpoint import CheckpointManager
from conductor.engine.context import WorkflowContext
from conductor.engine.limits import LimitEnforcer
from conductor.engine.pricing import ModelPricing
from conductor.engine.router import Router, RouteResult
from conductor.engine.usage import UsageTracker
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.exceptions import (
    ConductorError,
    ExecutionError,
    InterruptError,
    MaxIterationsError,
)
from conductor.exceptions import (
    TimeoutError as ConductorTimeoutError,
)
from conductor.executor.agent import AgentExecutor
from conductor.executor.linkify import linkify_markdown
from conductor.executor.script import ScriptExecutor, ScriptOutput
from conductor.executor.template import TemplateRenderer
from conductor.gates.human import (
    GateResult,
    HumanGateHandler,
    MaxIterationsHandler,
)
from conductor.gates.interrupt import InterruptAction, InterruptHandler, InterruptResult
from conductor.providers.base import AgentOutput

logger = logging.getLogger(__name__)

# Maximum nesting depth for sub-workflow composition.
# Prevents runaway recursion when workflows reference each other.
MAX_SUBWORKFLOW_DEPTH = 10


if TYPE_CHECKING:
    from conductor.config.schema import AgentDef, ForEachDef, ParallelGroup, WorkflowConfig
    from conductor.interrupt.listener import KeyboardListener
    from conductor.providers.base import AgentProvider
    from conductor.providers.registry import ProviderRegistry
    from conductor.web.server import WebDashboard


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
        workflow_path: Path | None = None,
        interrupt_event: asyncio.Event | None = None,
        event_emitter: WorkflowEventEmitter | None = None,
        keyboard_listener: KeyboardListener | None = None,
        web_dashboard: WebDashboard | None = None,
        _subworkflow_depth: int = 0,
        run_id: str = "",
        log_file: str = "",
        dashboard_port: int | None = None,
        bg_mode: bool = False,
    ) -> None:
        """Initialize the WorkflowEngine.

        Args:
            config: The workflow configuration.
            provider: Single provider for backward compatibility (deprecated).
                If both provider and registry are None, agents cannot be executed.
            registry: Provider registry for multi-provider support.
                When provided, each agent can use a different provider based
                on the agent's ``provider`` field or the workflow default.
            skip_gates: If True, auto-selects first option at human gates.
            workflow_path: Path to the workflow YAML file. Used for checkpoint
                metadata when saving state on failure.
            interrupt_event: Optional asyncio.Event for interrupt signaling.
                When set, the engine checks for user interrupts between agents.
            event_emitter: Optional event emitter for publishing workflow events.
                When provided, the engine emits events at each execution point
                (agent start/complete, routing, parallel groups, etc.).
                When None, zero overhead (early return in _emit()).
            keyboard_listener: Optional keyboard listener to suspend/resume
                around interactive prompts (human gates, max iterations).
                When provided, the listener is suspended before stdin reads
                and resumed afterward, preventing cbreak mode conflicts.
            web_dashboard: Optional web dashboard for bidirectional gate input.
                When provided and connected, gate input is accepted from
                both CLI stdin and web UI, with first response winning.
            _subworkflow_depth: Current nesting depth for sub-workflow composition.
                Used internally to enforce MAX_SUBWORKFLOW_DEPTH. Callers should
                not set this directly.

        Note:
            If both provider and registry are provided, registry takes precedence.
            The single provider parameter is deprecated but still supported for
            backward compatibility.
        """
        self.config = config
        self.skip_gates = skip_gates
        self.workflow_path = workflow_path
        self._run_id = run_id
        self._log_file = log_file
        self.context = WorkflowContext(
            workflow_dir=str(Path(workflow_path).resolve().parent) if workflow_path else "",
            workflow_file=str(Path(workflow_path).resolve()) if workflow_path else "",
            workflow_name=config.workflow.name,
        )
        self.renderer = TemplateRenderer()
        self.router = Router()
        self.limits = LimitEnforcer(
            max_iterations=config.workflow.limits.max_iterations,
            timeout_seconds=config.workflow.limits.timeout_seconds,
        )
        self.gate_handler = HumanGateHandler(skip_gates=skip_gates)
        self.max_iterations_handler = MaxIterationsHandler(skip_gates=skip_gates)
        self.script_executor = ScriptExecutor()
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

        # Interrupt support
        self._interrupt_event = interrupt_event
        self._interrupt_handler = InterruptHandler(skip_gates=skip_gates)
        self._keyboard_listener = keyboard_listener

        # Event emitter for workflow observability
        self._event_emitter = event_emitter

        # Web dashboard for bidirectional gate input
        self._web_dashboard = web_dashboard

        # Checkpoint tracking
        self._current_agent_name: str | None = None
        self._last_checkpoint_path: Path | None = None

        # Sub-workflow depth tracking
        self._subworkflow_depth = _subworkflow_depth

        # System metadata fields (set by CLI, used in workflow_started event)
        self._dashboard_port = dashboard_port
        self._bg_mode = bg_mode
        self._system_metadata: dict[str, Any] = {}

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

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        """Emit a workflow event if an emitter is configured.

        Creates a WorkflowEvent and dispatches it to the emitter. When no
        emitter is configured (None), this is a no-op with zero overhead.

        Args:
            event_type: The event type identifier (e.g., "agent_started").
            data: Event-specific payload data.
        """
        if self._event_emitter is None:
            return
        event = WorkflowEvent(type=event_type, timestamp=_time.time(), data=data)
        self._event_emitter.emit(event)

    def _yaml_source_field(self) -> dict[str, str]:
        """Return ``{"yaml_source": <text>}`` if the workflow file is readable."""
        if self.workflow_path is None:
            return {}
        try:
            return {"yaml_source": Path(self.workflow_path).read_text(encoding="utf-8")}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _conductor_version() -> str:
        """Return the installed conductor-cli version."""
        try:
            from conductor import __version__

            return __version__
        except Exception:
            return "unknown"

    def _build_system_metadata(self) -> dict[str, Any]:
        """Build system metadata dict for the workflow_started event.

        Captures runtime diagnostics that would be lost if the process crashes:
        PID, platform, Python version, working directory, etc.

        Returns:
            Dict with system metadata fields.
        """
        import os
        import platform as _platform
        import sys
        from datetime import UTC, datetime

        system: dict[str, Any] = {
            "pid": os.getpid(),
            "platform": sys.platform,
            "python_version": _platform.python_version(),
            "conductor_version": self._conductor_version(),
            "cwd": os.getcwd(),
            "started_at": datetime.now(UTC).isoformat(),
            "run_id": self._run_id,
            "log_file": self._log_file,
            "bg_mode": self._bg_mode,
        }

        # Conditional fields — only when dashboard is active
        if self._dashboard_port is not None:
            system["dashboard_port"] = self._dashboard_port
            system["dashboard_url"] = f"http://127.0.0.1:{self._dashboard_port}"

        # Parent PID is useful in --web-bg to trace back to the forking CLI process
        if self._bg_mode:
            system["parent_pid"] = os.getppid()

        return system

    def _make_event_callback(self, agent_name: str) -> Any:
        """Create an event callback for an agent that forwards to the emitter.

        Returns None when no emitter is configured, so the callback plumbing
        is entirely skipped in non-dashboard mode.

        Args:
            agent_name: The agent name to inject into forwarded events.

        Returns:
            An EventCallback function, or None if no emitter is configured.
        """
        if self._event_emitter is None:
            return None

        def _callback(event_type: str, data: dict[str, Any]) -> None:
            data_with_agent = {"agent_name": agent_name, **data}
            self._emit(event_type, data_with_agent)

        return _callback

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

    async def _execute_script(self, agent: AgentDef, context: dict[str, Any]) -> ScriptOutput:
        """Execute a script step with workflow-level timeout enforcement.

        Args:
            agent: Script agent definition.
            context: Workflow context for template rendering.

        Returns:
            ScriptOutput with stdout, stderr, and exit_code.

        Raises:
            ExecutionError: If script fails or times out.
        """
        return await self.limits.wait_for_with_timeout(
            self.script_executor.execute(agent, context),
            operation_name=f"script '{agent.name}'",
        )

    async def _execute_subworkflow(
        self,
        agent: AgentDef,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a sub-workflow as a black-box step.

        Loads the referenced workflow YAML, creates a child WorkflowEngine,
        and runs it with the parent agent's context as input. The sub-workflow's
        final output is returned as the agent's output.

        Args:
            agent: Workflow agent definition with ``workflow`` path.
            context: Workflow context for template rendering (used as sub-workflow input).

        Returns:
            The sub-workflow's final output dict.

        Raises:
            ExecutionError: If the sub-workflow file cannot be loaded,
                depth limit is exceeded, or execution fails.
        """
        from conductor.config.loader import load_config

        if self._subworkflow_depth >= MAX_SUBWORKFLOW_DEPTH:
            raise ExecutionError(
                f"Sub-workflow depth limit exceeded ({MAX_SUBWORKFLOW_DEPTH}). "
                f"Agent '{agent.name}' cannot invoke sub-workflow '{agent.workflow}'.",
                suggestion=("Check for circular sub-workflow references or reduce nesting depth."),
            )

        # Per-agent depth limit (stricter than global MAX_SUBWORKFLOW_DEPTH)
        if agent.max_depth is not None and self._subworkflow_depth >= agent.max_depth:
            raise ExecutionError(
                f"Agent '{agent.name}' max_depth ({agent.max_depth}) exceeded "
                f"at depth {self._subworkflow_depth}.",
                suggestion="Increase max_depth or restructure to reduce nesting.",
            )

        assert agent.workflow is not None  # noqa: S101

        # Resolve sub-workflow path relative to parent workflow file
        if self.workflow_path is not None:
            base_dir = Path(self.workflow_path).resolve().parent
        else:
            base_dir = Path.cwd()

        sub_path = (base_dir / agent.workflow).resolve()

        if not sub_path.exists():
            raise ExecutionError(
                f"Sub-workflow file not found: {sub_path} (referenced by agent '{agent.name}')",
                suggestion="Check that the 'workflow' path is correct and the file exists.",
            )

        try:
            sub_config = load_config(sub_path)
        except Exception as exc:
            raise ExecutionError(
                f"Failed to load sub-workflow '{sub_path}' "
                f"(referenced by agent '{agent.name}'): {exc}",
                suggestion="Check the sub-workflow YAML for syntax or validation errors.",
            ) from exc

        # Build sub-workflow inputs from the parent context
        sub_inputs: dict[str, Any]
        if agent.input_mapping:
            # Dynamic inputs: render each Jinja2 expression against parent context
            renderer = TemplateRenderer()
            sub_inputs = {}
            for key, template_expr in agent.input_mapping.items():
                rendered = renderer.render(template_expr, context)
                # Attempt to parse rendered values as JSON for non-string types
                try:
                    sub_inputs[key] = json.loads(rendered)
                except (json.JSONDecodeError, ValueError):
                    sub_inputs[key] = rendered
        else:
            # Default: forward parent's workflow.input.* values
            workflow_ctx = context.get("workflow", {})
            sub_inputs = (
                dict(workflow_ctx.get("input", {})) if isinstance(workflow_ctx, dict) else {}
            )

        # Create child engine inheriting provider/registry but with deeper depth
        child_engine = WorkflowEngine(
            config=sub_config,
            provider=self._single_provider,
            registry=self._registry,
            skip_gates=self.skip_gates,
            workflow_path=sub_path,
            interrupt_event=self._interrupt_event,
            event_emitter=self._event_emitter,
            keyboard_listener=self._keyboard_listener,
            web_dashboard=self._web_dashboard,
            _subworkflow_depth=self._subworkflow_depth + 1,
        )

        # Inject parent agent outputs into the child workflow's context.
        # This allows sub-workflow agents that declare parent agents in their
        # input: list (e.g., task_manager.output?) to access parent state
        # even when input_mapping doesn't cover all fields.
        for key, value in context.items():
            if key not in ("workflow", "context") and isinstance(value, dict):
                child_engine.context.agent_outputs[key] = value.get("output", value)

        return await child_engine.run(sub_inputs)

    async def _execute_subworkflow_with_inputs(
        self,
        agent: AgentDef,
        sub_inputs: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a sub-workflow with pre-built inputs.

        Like _execute_subworkflow but accepts explicit inputs instead of
        extracting them from context. Used by for_each groups where
        input_mapping has already been rendered with loop variables.

        Args:
            agent: Workflow agent definition with ``workflow`` path.
            sub_inputs: Pre-built input dict for the sub-workflow.
            context: Optional parent workflow context. When provided, parent
                agent outputs are injected into the child workflow's context
                so sub-workflow agents can reference them.

        Returns:
            The sub-workflow's final output dict.
        """
        from conductor.config.loader import load_config

        if self._subworkflow_depth >= MAX_SUBWORKFLOW_DEPTH:
            raise ExecutionError(
                f"Sub-workflow depth limit exceeded ({MAX_SUBWORKFLOW_DEPTH}). "
                f"Agent '{agent.name}' cannot invoke sub-workflow '{agent.workflow}'.",
                suggestion="Check for circular sub-workflow references or reduce nesting depth.",
            )

        # Per-agent depth limit (stricter than global MAX_SUBWORKFLOW_DEPTH)
        if agent.max_depth is not None and self._subworkflow_depth >= agent.max_depth:
            raise ExecutionError(
                f"Agent '{agent.name}' max_depth ({agent.max_depth}) exceeded "
                f"at depth {self._subworkflow_depth}.",
                suggestion="Increase max_depth or restructure to reduce nesting.",
            )

        assert agent.workflow is not None  # noqa: S101

        if self.workflow_path is not None:
            base_dir = Path(self.workflow_path).resolve().parent
        else:
            base_dir = Path.cwd()

        sub_path = (base_dir / agent.workflow).resolve()

        if not sub_path.exists():
            raise ExecutionError(
                f"Sub-workflow file not found: {sub_path} (referenced by agent '{agent.name}')",
                suggestion="Check that the 'workflow' path is correct and the file exists.",
            )

        try:
            sub_config = load_config(sub_path)
        except Exception as exc:
            raise ExecutionError(
                f"Failed to load sub-workflow '{sub_path}' "
                f"(referenced by agent '{agent.name}'): {exc}",
                suggestion="Check the sub-workflow YAML for syntax or validation errors.",
            ) from exc

        child_engine = WorkflowEngine(
            config=sub_config,
            provider=self._single_provider,
            registry=self._registry,
            skip_gates=self.skip_gates,
            workflow_path=sub_path,
            interrupt_event=self._interrupt_event,
            event_emitter=self._event_emitter,
            keyboard_listener=self._keyboard_listener,
            web_dashboard=self._web_dashboard,
            _subworkflow_depth=self._subworkflow_depth + 1,
        )

        # Inject parent agent outputs into the child workflow's context.
        # This allows sub-workflow agents that declare parent agents in their
        # input: list (e.g., task_manager.output?) to access parent state
        # even when input_mapping doesn't cover all fields.
        if context is not None:
            for key, value in context.items():
                if key not in ("workflow", "context") and isinstance(value, dict):
                    child_engine.context.agent_outputs[key] = value.get("output", value)

        return await child_engine.run(sub_inputs)

    def _get_context_window_for_agent(self, agent: AgentDef) -> int | None:
        """Return the context window size for an agent's model."""
        from conductor.engine.pricing import get_pricing

        model = agent.model
        if not model:
            return None

        pricing = get_pricing(model)
        return pricing.context_window if pricing else None

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

        return await self._execute_loop(current_agent_name)

    async def resume(self, current_agent_name: str) -> dict[str, Any]:
        """Resume workflow execution from a specific agent.

        Assumes ``self.context`` and ``self.limits`` have been pre-loaded
        from checkpoint data via :meth:`set_context` and :meth:`set_limits`.
        Enters the main execution loop at *current_agent_name* without
        resetting iteration counters.

        Args:
            current_agent_name: Name of the agent to resume from.

        Returns:
            Final output dict built from output templates.

        Raises:
            ExecutionError: If the agent is not found or execution fails.
            MaxIterationsError: If max iterations limit is exceeded.
            TimeoutError: If timeout limit is exceeded.
        """
        # Fresh timeout window for resumed execution
        self.limits.start_time = _time.monotonic()

        # Execute on_start hook (signals resume)
        self._execute_hook("on_start")

        return await self._execute_loop(current_agent_name)

    def set_context(self, context: WorkflowContext) -> None:
        """Replace the engine's workflow context with a restored one.

        Used by the CLI resume path to inject context reconstructed from
        a checkpoint file.

        Args:
            context: A WorkflowContext restored via ``WorkflowContext.from_dict()``.
        """
        self.context = context

    def set_limits(self, limits: LimitEnforcer) -> None:
        """Replace the engine's limit enforcer with a restored one.

        Used by the CLI resume path to inject limits reconstructed from
        a checkpoint file.

        Args:
            limits: A LimitEnforcer restored via ``LimitEnforcer.from_dict()``.
        """
        self.limits = limits

    def _save_checkpoint_on_failure(self, error: BaseException) -> None:
        """Attempt to save a checkpoint after a failure.

        This method never raises — on failure it logs a warning so the
        original error is not masked.

        Args:
            error: The exception that triggered the checkpoint save.
        """
        if self.workflow_path is None:
            logger.debug("No workflow_path set; skipping checkpoint save")
            return

        # Collect session IDs from provider if available
        copilot_session_ids: dict[str, str] | None = None
        provider = self._single_provider
        if provider is not None and hasattr(provider, "get_session_ids"):
            copilot_session_ids = provider.get_session_ids()  # type: ignore[union-attr]
        elif self._registry is not None:
            for p in self._registry.get_active_providers().values():
                if hasattr(p, "get_session_ids"):
                    copilot_session_ids = p.get_session_ids()  # type: ignore[union-attr]
                    break

        checkpoint_path = CheckpointManager.save_checkpoint(
            workflow_path=self.workflow_path,
            context=self.context,
            limits=self.limits,
            current_agent=self._current_agent_name or "unknown",
            error=error,
            inputs=self.context.workflow_inputs,
            copilot_session_ids=copilot_session_ids,
            system_metadata=self._system_metadata,
        )
        self._last_checkpoint_path = checkpoint_path
        if checkpoint_path is not None:
            self._emit(
                "checkpoint_saved",
                {
                    "path": str(checkpoint_path),
                    "agent_name": self._current_agent_name,
                    "error_type": type(error).__name__,
                },
            )

    def _get_top_level_agent_names(self) -> list[str]:
        """Return names of top-level agents (excluding parallel/for-each nested agents).

        Used by the interrupt handler to populate the list of agents available
        for "skip to agent".

        Returns:
            List of top-level agent names.
        """
        return [a.name for a in self.config.agents]

    async def _suspend_listener(self) -> None:
        """Suspend the keyboard listener before interactive stdin prompts."""
        if self._keyboard_listener is not None:
            await self._keyboard_listener.suspend()

    async def _resume_listener(self) -> None:
        """Resume the keyboard listener after interactive stdin prompts."""
        if self._keyboard_listener is not None:
            await self._keyboard_listener.resume()

    async def _handle_gate_with_web(
        self,
        agent: AgentDef,
        agent_context: dict[str, Any],
    ) -> GateResult:
        """Handle a human gate, racing CLI input against web dashboard input.

        When a web dashboard is connected, both the CLI prompt and the web
        dashboard wait concurrently.  The first response wins and the other
        is cancelled.  When no web dashboard is available, falls back to
        CLI-only input.

        Args:
            agent: The human_gate agent definition.
            agent_context: Current workflow context for template rendering.

        Returns:
            GateResult from whichever input source responded first.
        """
        # If no web dashboard at all, use CLI only.
        if self._web_dashboard is None:
            gate_base = Path(self.workflow_path).resolve().parent if self.workflow_path else None
            return await self.gate_handler.handle_gate(agent, agent_context, base_dir=gate_base)

        # Race CLI vs web input. We start the web task unconditionally (not only
        # when a client is currently connected), because the human often opens
        # the per-run dashboard AFTER seeing the gate-waiting notification.
        # If we bail early when ``has_connections()`` is False, a later click
        # in the dashboard pushes a message to ``_gate_response_queue`` that
        # nobody is awaiting, and the workflow hangs forever.
        gate_base = Path(self.workflow_path).resolve().parent if self.workflow_path else None
        cli_task = asyncio.create_task(
            self.gate_handler.handle_gate(agent, agent_context, base_dir=gate_base),
            name="gate_cli",
        )
        web_task = asyncio.create_task(
            self._wait_for_web_gate(agent),
            name="gate_web",
        )

        done, pending = await asyncio.wait(
            {cli_task, web_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel the loser
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        # Get the result from the winner
        winner = done.pop()
        return winner.result()

    async def _wait_for_web_gate(self, agent: AgentDef) -> GateResult:
        """Wait for a gate response from the web dashboard.

        Translates the raw JSON message from the web client into a
        ``GateResult`` by matching ``selected_value`` against the
        agent's options.

        Args:
            agent: The human_gate agent definition with options.

        Returns:
            GateResult with the selected option, route, and any
            additional input from the web client.

        Raises:
            HumanGateError: If the selected value doesn't match any option.
        """
        from conductor.exceptions import HumanGateError

        assert self._web_dashboard is not None  # noqa: S101

        msg = await self._web_dashboard.wait_for_gate_response(agent.name)
        selected_value = msg.get("selected_value", "")

        # Find matching option
        for option in agent.options or []:
            if option.value == selected_value:
                additional_input = msg.get("additional_input", {})
                if not isinstance(additional_input, dict):
                    additional_input = {}
                return GateResult(
                    selected_option=option,
                    route=option.route,
                    additional_input=additional_input,
                )

        raise HumanGateError(
            f"Web gate response value '{selected_value}' does not match any option "
            f"for gate '{agent.name}'",
            suggestion="Check the option values in the workflow YAML",
        )

    # ------------------------------------------------------------------
    # Interrupt support
    # ------------------------------------------------------------------

    async def _check_interrupt(self, current_agent_name: str) -> InterruptResult | None:
        """Check for a pending interrupt and handle it if present.

        If the interrupt event is set, clears it, builds an output preview
        from the last stored output, and delegates to the InterruptHandler
        for user interaction.

        In web mode (dashboard connected), the interrupt is consumed
        silently — the provider-level racing handles the actual pause/resume
        flow, so the between-agent check just needs to clear the stale flag.

        Args:
            current_agent_name: Name of the agent that just completed
                (or the next agent about to run).

        Returns:
            InterruptResult if an interrupt was handled, None otherwise.
        """
        if self._interrupt_event is None or not self._interrupt_event.is_set():
            return None

        self._interrupt_event.clear()

        # In web mode, the interrupt was already handled at the provider level
        # (partial output → _handle_web_pause). Consume the stale flag silently.
        # EXCEPTION: in subworkflows (depth > 0), propagate the interrupt so it
        # unwinds the child engine back to the parent, stopping the workflow.
        if self._web_dashboard is not None:
            if self._subworkflow_depth > 0:
                raise InterruptError(agent_name=current_agent_name)
            return None

        # Build output preview from last stored output
        last_output = self.context.get_latest_output()
        last_output_preview: str | None = None
        if last_output is not None:
            try:
                preview = json.dumps(last_output, indent=2, default=str)
                last_output_preview = preview[:500]
            except (TypeError, ValueError):
                last_output_preview = str(last_output)[:500]

        # Suspend keyboard listener so stdin works normally for the prompt
        await self._suspend_listener()
        try:
            return await self._interrupt_handler.handle_interrupt(
                current_agent=current_agent_name,
                iteration=self.context.current_iteration,
                last_output_preview=last_output_preview,
                available_agents=self._get_top_level_agent_names(),
                accumulated_guidance=list(self.context.user_guidance),
            )
        finally:
            await self._resume_listener()

    async def _handle_interrupt_result(
        self,
        result: InterruptResult,
        current_agent_name: str,
    ) -> str:
        """Apply the result of an interrupt interaction.

        Args:
            result: The InterruptResult from the handler.
            current_agent_name: The current agent name (for error context).

        Returns:
            The next agent name to execute (may be unchanged, or a skip target).

        Raises:
            InterruptError: If the user selected "stop workflow".
        """
        match result.action:
            case InterruptAction.CONTINUE:
                if result.guidance:
                    self.context.add_guidance(result.guidance)
                return current_agent_name
            case InterruptAction.SKIP:
                return result.skip_target or current_agent_name
            case InterruptAction.STOP:
                raise InterruptError(agent_name=current_agent_name)
            case InterruptAction.CANCEL:
                return current_agent_name

    async def _handle_web_pause(self, agent_name: str, partial_output: AgentOutput) -> bool:
        """Handle a mid-agent interrupt when the web dashboard is connected.

        Emits an ``agent_paused`` event and waits for the user to click
        Resume or Kill in the dashboard.  If all browser clients disconnect
        while waiting, auto-resumes to avoid hanging the workflow.

        Args:
            agent_name: The name of the interrupted agent.
            partial_output: The partial output from the interrupted agent.

        Returns:
            True if the agent should be re-executed (Resume chosen or
            all clients disconnected), False if no web dashboard is
            connected (caller should invoke ``_handle_partial_output``).

        Raises:
            InterruptError: If the user chose Kill (``POST /api/kill``).
        """
        if self._web_dashboard is None or not self._web_dashboard.has_connections():
            return False

        try:
            preview = json.dumps(partial_output.content, indent=2, default=str)[:500]
        except (TypeError, ValueError):
            preview = str(partial_output.content)[:500]

        self._emit(
            "agent_paused",
            {"agent_name": agent_name, "partial_content": preview},
        )
        logger.info("Agent '%s' paused — waiting for dashboard resume", agent_name)

        resume_event = self._web_dashboard.resume_event
        kill_event = self._web_dashboard.kill_event
        disconnect_event = self._web_dashboard.disconnect_event

        # Clear stale signals from prior pause cycles, then create wait tasks.
        # We must check is_set() after creating tasks to close the race window
        # where an HTTP handler sets the event between clear() and wait().
        resume_event.clear()
        kill_event.clear()
        disconnect_event.clear()

        resume_task = asyncio.create_task(resume_event.wait())
        kill_task = asyncio.create_task(kill_event.wait())
        disconnect_task = asyncio.create_task(disconnect_event.wait())
        tasks = {resume_task, kill_task, disconnect_task}

        # In subworkflows, also watch the interrupt_event so that a second
        # Stop click while paused will stop the workflow without requiring
        # the user to first Resume then wait for the next between-agent check.
        stop_task = None
        if self._subworkflow_depth > 0 and self._interrupt_event is not None:
            self._interrupt_event.clear()
            stop_task = asyncio.create_task(self._interrupt_event.wait())
            tasks.add(stop_task)

        # If any event was set between clear() and task creation, the task
        # will already be done — no need to wait, but we still fall through
        # to the normal done/pending handling below.
        try:
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
        except Exception:
            for t in tasks:
                if not t.done():
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await t
            raise

        if kill_task in done:
            raise InterruptError(agent_name=agent_name)

        # Stop-while-paused in a subworkflow: treat as interrupt
        if stop_task is not None and stop_task in done:
            if self._interrupt_event is not None:
                self._interrupt_event.clear()
            raise InterruptError(agent_name=agent_name)

        if disconnect_task in done:
            logger.info(
                "All dashboard clients disconnected while '%s' was paused — auto-resuming",
                agent_name,
            )

        # Clear resume_event after consumption so a stale signal from a
        # double-click or prior API call doesn't skip the next legitimate pause.
        resume_event.clear()

        self._emit("agent_resumed", {"agent_name": agent_name})
        logger.info("Agent '%s' resumed — re-executing", agent_name)
        return True

    async def _handle_partial_output(
        self,
        agent: AgentDef,
        partial_output: AgentOutput,
        agent_context: dict[str, Any],
        guidance_section: str | None,
        executor: AgentExecutor,
        agent_start_time: float,
    ) -> AgentOutput:
        """Handle partial output from a mid-agent interrupt.

        Invokes the interrupt handler to collect user guidance, then either:
        - Sends a follow-up to the interrupted session (Copilot provider), or
        - Re-executes the agent with guidance appended (other providers).

        Args:
            agent: The agent that was interrupted.
            partial_output: The partial output from the interrupted agent.
            agent_context: The context used for the agent execution.
            guidance_section: The guidance section used in the original execution.
            executor: The executor used for the agent.
            agent_start_time: The start time of the agent execution.

        Returns:
            The final (non-partial) AgentOutput after handling the interrupt.
        """
        from conductor.providers.copilot import CopilotProvider

        # Build preview from partial output
        try:
            preview = json.dumps(partial_output.content, indent=2, default=str)[:500]
        except (TypeError, ValueError):
            preview = str(partial_output.content)[:500]

        # CLI mode: invoke interactive interrupt handler
        interrupt_result = await self._interrupt_handler.handle_interrupt(
            current_agent=agent.name,
            iteration=self.context.current_iteration,
            last_output_preview=preview,
            available_agents=self._get_top_level_agent_names(),
            accumulated_guidance=list(self.context.user_guidance),
        )

        # Apply the interrupt result
        if interrupt_result.action == InterruptAction.STOP:
            raise InterruptError(agent_name=agent.name)

        if interrupt_result.action == InterruptAction.CANCEL or not interrupt_result.guidance:
            # No guidance provided — use partial output as final
            partial_output.partial = False
            return partial_output

        # Add guidance to context
        self.context.add_guidance(interrupt_result.guidance)

        # Try Copilot follow-up if provider supports it
        provider = executor.provider
        if isinstance(provider, CopilotProvider):
            session = provider.get_interrupted_session()
            if session is not None:
                return await provider.send_followup(session, interrupt_result.guidance)

        # Fallback: re-execute the agent with guidance appended to prompt
        new_guidance_section = self.context.get_guidance_prompt_section()
        return await executor.execute(agent, agent_context, guidance_section=new_guidance_section)

    async def _execute_loop(self, current_agent_name: str) -> dict[str, Any]:
        """Core execution loop shared by :meth:`run` and :meth:`resume`.

        Iterates through agents following routing rules until ``$end`` is
        reached.  On failure the current state is saved to a checkpoint
        file (if ``workflow_path`` is set) and the original exception is
        re-raised.

        Args:
            current_agent_name: Name of the first agent to execute.

        Returns:
            Final output dict built from output templates.
        """
        try:
            async with self.limits.timeout_context():
                # Emit workflow_started before the execution loop
                self._system_metadata = self._build_system_metadata()
                self._emit(
                    "workflow_started",
                    {
                        "name": self.config.workflow.name,
                        "version": self._conductor_version(),
                        "entry_point": self.config.workflow.entry_point,
                        "agents": [
                            {
                                "name": a.name,
                                "type": a.type or "agent",
                                "model": a.model,
                            }
                            for a in self.config.agents
                        ],
                        "parallel_groups": [
                            {
                                "name": p.name,
                                "agents": p.agents,
                            }
                            for p in self.config.parallel
                        ],
                        "for_each_groups": [
                            {
                                "name": f.name,
                                "source": f.source,
                            }
                            for f in self.config.for_each
                        ],
                        "routes": [
                            {
                                "from": a.name,
                                "to": r.to,
                                "when": r.when,
                            }
                            for a in self.config.agents
                            for r in a.routes
                        ]
                        + [
                            {
                                "from": p.name,
                                "to": r.to,
                                "when": r.when,
                            }
                            for p in self.config.parallel
                            for r in p.routes
                        ]
                        + [
                            {
                                "from": f.name,
                                "to": r.to,
                                "when": r.when,
                            }
                            for f in self.config.for_each
                            for r in f.routes
                        ],
                        **self._yaml_source_field(),
                        "metadata": self.config.workflow.metadata,
                        "system": self._build_system_metadata(),
                        "run_id": self._run_id,
                        "log_file": self._log_file,
                    },
                )

                _workflow_start = _time.time()

                while True:
                    self._current_agent_name = current_agent_name

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

                        # Trim context if max_tokens is configured
                        self._trim_context_if_needed()

                        # Execute for-each group with timeout enforcement
                        _group_start = _time.time()
                        for_each_output = await self.limits.wait_for_with_timeout(
                            self._execute_for_each_group(for_each_group),
                            operation_name=f"for-each group '{for_each_group.name}'",
                        )
                        _group_elapsed = _time.time() - _group_start

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

                        self._emit(
                            "route_taken",
                            {
                                "from_agent": for_each_group.name,
                                "to_agent": route_result.target,
                            },
                        )

                        if route_result.target == "$end":
                            result = self._build_final_output(route_result.output_transform)
                            self._emit(
                                "workflow_completed",
                                {
                                    "elapsed": _time.time() - _workflow_start,
                                    "output": result,
                                },
                            )
                            self._execute_hook("on_complete", result=result)
                            return result

                        current_agent_name = route_result.target

                    # Handle parallel group execution
                    if parallel_group is not None:
                        # Check iteration limit for all parallel agents before executing
                        await self._check_parallel_group_iteration_with_prompt(
                            parallel_group.name, len(parallel_group.agents)
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

                        self._emit(
                            "route_taken",
                            {
                                "from_agent": parallel_group.name,
                                "to_agent": route_result.target,
                            },
                        )

                        if route_result.target == "$end":
                            result = self._build_final_output(route_result.output_transform)
                            self._emit(
                                "workflow_completed",
                                {
                                    "elapsed": _time.time() - _workflow_start,
                                    "output": result,
                                },
                            )
                            self._execute_hook("on_complete", result=result)
                            return result

                        current_agent_name = route_result.target

                    # Handle regular agent execution
                    if agent is not None:
                        # Check iteration limit before executing
                        await self._check_iteration_with_prompt(current_agent_name)

                        # Count how many times this specific agent has been executed
                        # (for per-agent iteration tracking in the web dashboard)
                        agent_execution_count = (
                            self.limits.get_agent_execution_count(agent.name) + 1
                        )

                        self._emit(
                            "agent_started",
                            {
                                "agent_name": agent.name,
                                "iteration": agent_execution_count,
                                "agent_type": agent.type or "agent",
                                "context_window_max": self._get_context_window_for_agent(agent),
                            },
                        )

                        # Trim context if max_tokens is configured
                        self._trim_context_if_needed()

                        # Handle human gates
                        if agent.type == "human_gate":
                            # Build context for the gate prompt
                            agent_context = self.context.get_for_template()

                            # Emit gate_presented with full option details for web UI
                            gate_options_data = [
                                {
                                    "label": o.label,
                                    "value": o.value,
                                    "route": o.route,
                                    "prompt_for": o.prompt_for,
                                }
                                for o in (agent.options or [])
                            ]

                            # Render prompt and auto-linkify paths/URLs for markdown display
                            rendered_prompt = self.renderer.render(agent.prompt, agent_context)
                            gate_base_dir = (
                                Path(self.workflow_path).resolve().parent
                                if self.workflow_path
                                else None
                            )
                            rendered_prompt = linkify_markdown(
                                rendered_prompt, base_dir=gate_base_dir
                            )

                            self._emit(
                                "gate_presented",
                                {
                                    "agent_name": agent.name,
                                    "options": [o.value for o in (agent.options or [])],
                                    "option_details": gate_options_data,
                                    "prompt": rendered_prompt,
                                },
                            )

                            # Use the gate handler for interaction
                            # Suspend keyboard listener so stdin works normally
                            await self._suspend_listener()
                            try:
                                gate_result = await self._handle_gate_with_web(agent, agent_context)
                            finally:
                                await self._resume_listener()

                            self._emit(
                                "gate_resolved",
                                {
                                    "agent_name": agent.name,
                                    "selected_option": gate_result.selected_option.value,
                                    "route": gate_result.route,
                                    "additional_input": gate_result.additional_input,
                                },
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
                                self._emit(
                                    "workflow_completed",
                                    {
                                        "elapsed": _time.time() - _workflow_start,
                                        "output": result,
                                    },
                                )
                                self._execute_hook("on_complete", result=result)
                                return result
                            current_agent_name = gate_result.route
                            continue

                        # Handle script steps
                        if agent.type == "script":
                            agent_context = self.context.build_for_agent(
                                agent.name,
                                agent.input,
                                mode=self.config.workflow.context.mode,
                            )
                            # Script args are rendered locally (no LLM cost), so
                            # workflow inputs must always be available for template
                            # resolution — even in explicit mode where they'd
                            # otherwise be filtered out.
                            agent_context.setdefault("workflow", {})["input"] = (
                                self.context.workflow_inputs.copy()
                            )
                            _script_start = _time.time()

                            # Count how many times this specific script has been executed
                            # (for per-agent iteration tracking in the web dashboard)
                            script_execution_count = (
                                self.limits.get_agent_execution_count(agent.name) + 1
                            )

                            self._emit(
                                "script_started",
                                {
                                    "agent_name": agent.name,
                                    "iteration": script_execution_count,
                                },
                            )

                            try:
                                script_output = await self._execute_script(agent, agent_context)
                            except Exception as exc:
                                _script_elapsed = _time.time() - _script_start
                                self._emit(
                                    "script_failed",
                                    {
                                        "agent_name": agent.name,
                                        "elapsed": _script_elapsed,
                                        "error_type": type(exc).__name__,
                                        "message": str(exc),
                                    },
                                )
                                raise
                            _script_elapsed = _time.time() - _script_start

                            self._emit(
                                "script_completed",
                                {
                                    "agent_name": agent.name,
                                    "elapsed": _script_elapsed,
                                    "stdout": script_output.stdout,
                                    "stderr": script_output.stderr,
                                    "exit_code": script_output.exit_code,
                                },
                            )

                            # Store structured output in context
                            output_content = {
                                "stdout": script_output.stdout,
                                "stderr": script_output.stderr,
                                "exit_code": script_output.exit_code,
                            }
                            self.context.store(agent.name, output_content)
                            self.limits.record_execution(agent.name)
                            self.limits.check_timeout()

                            route_result = self._evaluate_routes(agent, output_content)

                            self._emit(
                                "route_taken",
                                {
                                    "from_agent": agent.name,
                                    "to_agent": route_result.target,
                                },
                            )

                            if route_result.target == "$end":
                                result = self._build_final_output(route_result.output_transform)
                                self._emit(
                                    "workflow_completed",
                                    {
                                        "elapsed": _time.time() - _workflow_start,
                                        "output": result,
                                    },
                                )
                                self._execute_hook("on_complete", result=result)
                                return result

                            current_agent_name = route_result.target

                            # Check for interrupt after script step
                            interrupt_result = await self._check_interrupt(current_agent_name)
                            if interrupt_result is not None:
                                current_agent_name = await self._handle_interrupt_result(
                                    interrupt_result, current_agent_name
                                )
                            continue

                        # Handle sub-workflow steps
                        if agent.type == "workflow":
                            agent_context = self.context.build_for_agent(
                                agent.name,
                                agent.input,
                                mode=self.config.workflow.context.mode,
                            )
                            # input_mapping templates are rendered locally (no LLM
                            # cost), so workflow inputs must always be available —
                            # even in explicit mode.
                            agent_context.setdefault("workflow", {})["input"] = (
                                self.context.workflow_inputs.copy()
                            )
                            _sub_start = _time.time()

                            sub_execution_count = (
                                self.limits.get_agent_execution_count(agent.name) + 1
                            )

                            self._emit(
                                "subworkflow_started",
                                {
                                    "agent_name": agent.name,
                                    "iteration": sub_execution_count,
                                    "workflow": agent.workflow,
                                },
                            )

                            try:
                                sub_output = await self._execute_subworkflow(agent, agent_context)
                            except Exception as exc:
                                _sub_elapsed = _time.time() - _sub_start
                                self._emit(
                                    "subworkflow_failed",
                                    {
                                        "agent_name": agent.name,
                                        "elapsed": _sub_elapsed,
                                        "error_type": type(exc).__name__,
                                        "message": str(exc),
                                    },
                                )
                                raise
                            _sub_elapsed = _time.time() - _sub_start

                            self._emit(
                                "subworkflow_completed",
                                {
                                    "agent_name": agent.name,
                                    "elapsed": _sub_elapsed,
                                    "output": sub_output,
                                },
                            )

                            # Store sub-workflow output in context
                            self.context.store(agent.name, sub_output)
                            self.limits.record_execution(agent.name)
                            self.limits.check_timeout()

                            route_result = self._evaluate_routes(agent, sub_output)

                            self._emit(
                                "route_taken",
                                {
                                    "from_agent": agent.name,
                                    "to_agent": route_result.target,
                                },
                            )

                            if route_result.target == "$end":
                                result = self._build_final_output(route_result.output_transform)
                                self._emit(
                                    "workflow_completed",
                                    {
                                        "elapsed": _time.time() - _workflow_start,
                                        "output": result,
                                    },
                                )
                                self._execute_hook("on_complete", result=result)
                                return result

                            current_agent_name = route_result.target

                            # Check for interrupt after sub-workflow step
                            interrupt_result = await self._check_interrupt(current_agent_name)
                            if interrupt_result is not None:
                                current_agent_name = await self._handle_interrupt_result(
                                    interrupt_result, current_agent_name
                                )
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
                        guidance_section = self.context.get_guidance_prompt_section()
                        event_callback = self._make_event_callback(agent.name)
                        output = await executor.execute(
                            agent,
                            agent_context,
                            guidance_section=guidance_section,
                            interrupt_signal=self._interrupt_event,
                            event_callback=event_callback,
                        )
                        _agent_elapsed = _time.time() - _agent_start

                        # Handle mid-agent interrupt (partial output)
                        if output.partial:
                            if await self._handle_web_pause(agent.name, output):
                                # Web mode: agent paused then resumed → re-execute.
                                # Clear interrupt_event to prevent the re-executed agent
                                # from seeing the stale signal and returning partial again.
                                if self._interrupt_event is not None:
                                    self._interrupt_event.clear()
                                continue
                            # In web mode with no connections, auto-resume rather than
                            # falling through to the CLI interactive handler (which would
                            # block on stdin with no tty in --web-bg mode).
                            if self._web_dashboard is not None:
                                logger.info(
                                    "No dashboard connections for '%s' — auto-resuming",
                                    agent.name,
                                )
                                if self._interrupt_event is not None:
                                    self._interrupt_event.clear()
                                continue
                            output = await self._handle_partial_output(
                                agent,
                                output,
                                agent_context,
                                guidance_section,
                                executor,
                                _agent_start,
                            )
                            _agent_elapsed = _time.time() - _agent_start

                        # Record usage and calculate cost
                        usage = self.usage_tracker.record(agent.name, output, _agent_elapsed)

                        output_keys = (
                            list(output.content.keys()) if isinstance(output.content, dict) else []
                        )

                        self._emit(
                            "agent_completed",
                            {
                                "agent_name": agent.name,
                                "elapsed": _agent_elapsed,
                                "model": output.model,
                                "tokens": output.tokens_used,
                                "input_tokens": output.input_tokens,
                                "output_tokens": output.output_tokens,
                                "cost_usd": usage.cost_usd,
                                "output": output.content,
                                "output_keys": output_keys,
                                "context_window_used": output.input_tokens,
                                "context_window_max": self._get_context_window_for_agent(agent),
                            },
                        )

                        # Store output
                        self.context.store(agent.name, output.content)

                        # Record successful execution
                        self.limits.record_execution(agent.name)

                        # Check timeout after each agent
                        self.limits.check_timeout()

                        # Evaluate routes using the Router
                        route_result = self._evaluate_routes(agent, output.content)

                        self._emit(
                            "route_taken",
                            {
                                "from_agent": agent.name,
                                "to_agent": route_result.target,
                            },
                        )

                        if route_result.target == "$end":
                            result = self._build_final_output(route_result.output_transform)
                            self._emit(
                                "workflow_completed",
                                {
                                    "elapsed": _time.time() - _workflow_start,
                                    "output": result,
                                },
                            )
                            self._execute_hook("on_complete", result=result)
                            return result

                        current_agent_name = route_result.target

                    # Check for interrupt between agents (deferred for parallel/for-each)
                    interrupt_result = await self._check_interrupt(current_agent_name)
                    if interrupt_result is not None:
                        current_agent_name = await self._handle_interrupt_result(
                            interrupt_result, current_agent_name
                        )

        except KeyboardInterrupt:
            self._save_checkpoint_on_failure(KeyboardInterrupt("Workflow interrupted by user"))
            raise
        except ConductorError as e:
            fail_data: dict[str, Any] = {
                "error_type": type(e).__name__,
                "message": str(e),
                "agent_name": self._current_agent_name,
            }
            if isinstance(e, ConductorTimeoutError):
                fail_data["elapsed_seconds"] = e.elapsed_seconds
                fail_data["timeout_seconds"] = e.timeout_seconds
                fail_data["current_agent"] = e.current_agent
            self._emit("workflow_failed", fail_data)
            # Execute on_error hook with error information
            self._execute_hook("on_error", error=e)
            self._save_checkpoint_on_failure(e)
            raise
        except Exception as e:
            self._emit(
                "workflow_failed",
                {
                    "error_type": type(e).__name__,
                    "message": str(e),
                    "agent_name": self._current_agent_name,
                },
            )
            # Execute on_error hook for unexpected errors
            self._execute_hook("on_error", error=e)
            self._save_checkpoint_on_failure(e)
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
                    logger.debug(
                        "Summarize strategy unavailable in multi-provider mode "
                        "before first agent execution. Falling back to drop_oldest."
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
            await self._suspend_listener()
            try:
                result = await self.max_iterations_handler.handle_limit_reached(
                    current_iteration=self.limits.current_iteration,
                    max_iterations=self.limits.max_iterations,
                    agent_history=self.limits.execution_history,
                )
            finally:
                await self._resume_listener()
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
            await self._suspend_listener()
            try:
                result = await self.max_iterations_handler.handle_limit_reached(
                    current_iteration=self.limits.current_iteration,
                    max_iterations=self.limits.max_iterations,
                    agent_history=self.limits.execution_history,
                )
            finally:
                await self._resume_listener()
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

        Navigates dotted path notation to extract an array from agent outputs
        or workflow inputs. Handles the same wrapping logic as build_for_agent
        (regular agents are wrapped with {"output": ...}, parallel/for-each
        groups are stored directly).

        Supports two reference styles:
        - Agent output: ``finder.output.kpis`` → agent_outputs["finder"]["output"]["kpis"]
        - Workflow input: ``workflow.input.items`` → workflow_inputs["items"]

        Args:
            source: Dotted path reference (e.g., 'finder.output.kpis'
                or 'workflow.input.items').

        Returns:
            The resolved array (list).

        Raises:
            ExecutionError: If path doesn't exist, value is not an array.
        """
        parts = source.split(".")

        if len(parts) < 3:
            raise ExecutionError(
                f"Invalid source reference format: '{source}'",
                suggestion=(
                    "Source must have at least 3 parts "
                    "(e.g., 'agent_name.output.field' or 'workflow.input.field')"
                ),
            )

        # Handle workflow.input.* references
        if parts[0] == "workflow" and parts[1] == "input":
            return self._resolve_workflow_input_array(source, parts[2:])

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

    def _resolve_workflow_input_array(self, source: str, field_parts: list[str]) -> list[Any]:
        """Resolve a workflow.input.* reference to a runtime array.

        Navigates into ``self.context.workflow_inputs`` using the remaining
        dotted path segments after ``workflow.input``.

        Args:
            source: The full dotted source string (for error messages).
            field_parts: Path segments after ``workflow.input``
                (e.g., ``["items"]`` for ``workflow.input.items``).

        Returns:
            The resolved array (list).

        Raises:
            ExecutionError: If the path doesn't exist or value is not an array.
        """
        if not field_parts:
            raise ExecutionError(
                f"Invalid source reference: '{source}'",
                suggestion="workflow.input references need a field name "
                "(e.g., 'workflow.input.items')",
            )

        current: Any = self.context.workflow_inputs
        path_traversed = ["workflow", "input"]

        for part in field_parts:
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
                        else "Check the workflow input parameters"
                    ),
                )

            current = current[part]

        # Handle JSON string inputs (CLI passes arrays as strings)
        if isinstance(current, str):
            try:
                parsed = json.loads(current)
            except (ValueError, TypeError):
                raise ExecutionError(
                    f"Source '{source}' resolved to a string that is not valid JSON: {current!r}",
                    suggestion="Ensure the input is a JSON array string "
                    '(e.g., --input items=\'["a", "b"]\')',
                ) from None
            if not isinstance(parsed, list):
                raise ExecutionError(
                    f"Source '{source}' parsed from JSON string but got "
                    f"{type(parsed).__name__}, expected array",
                    suggestion="Ensure the input is a JSON array "
                    '(e.g., --input items=\'["a", "b"]\')',
                )
            return parsed

        if not isinstance(current, (list, tuple)):
            raise ExecutionError(
                f"Source '{source}' resolved to {type(current).__name__}, expected list or tuple",
                suggestion=f"Ensure '{source}' contains an array value",
            )

        return list(current)

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
        self._emit(
            "parallel_started",
            {
                "group_name": parallel_group.name,
                "agents": parallel_group.agents,
            },
        )

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
                event_callback = self._make_event_callback(agent.name)
                output = await executor.execute(
                    agent,
                    agent_context,
                    event_callback=event_callback,
                )
                _agent_elapsed = _time.time() - _agent_start

                # Record usage and calculate cost
                usage = self.usage_tracker.record(agent.name, output, _agent_elapsed)

                self._emit(
                    "parallel_agent_completed",
                    {
                        "group_name": parallel_group.name,
                        "agent_name": agent.name,
                        "elapsed": _agent_elapsed,
                        "model": output.model,
                        "tokens": output.tokens_used,
                        "cost_usd": usage.cost_usd,
                        "context_window_used": output.input_tokens,
                        "context_window_max": self._get_context_window_for_agent(agent),
                    },
                )

                # Individual parallel agents are counted toward iteration limit
                # at the parallel group level after all agents complete
                return (agent.name, output.content)
            except Exception as e:
                _agent_elapsed = _time.time() - _agent_start

                # Verbose: Log agent failure
                self._emit(
                    "parallel_agent_failed",
                    {
                        "group_name": parallel_group.name,
                        "agent_name": agent.name,
                        "elapsed": _agent_elapsed,
                        "error_type": type(e).__name__,
                        "message": str(e),
                    },
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
                self._emit(
                    "parallel_completed",
                    {
                        "group_name": parallel_group.name,
                        "success_count": len(parallel_output.outputs),
                        "failure_count": len(parallel_output.errors),
                        "elapsed": _group_elapsed,
                    },
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
            self._emit(
                "parallel_completed",
                {
                    "group_name": parallel_group.name,
                    "success_count": len(parallel_output.outputs),
                    "failure_count": len(parallel_output.errors),
                    "elapsed": _group_elapsed,
                },
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
            self._emit(
                "parallel_completed",
                {
                    "group_name": parallel_group.name,
                    "success_count": len(parallel_output.outputs),
                    "failure_count": len(parallel_output.errors),
                    "elapsed": _group_elapsed,
                },
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
            logger.debug(
                "Failed to extract key from item %s using '%s': %s. "
                "Falling back to index-based key.",
                fallback_index,
                key_by_path,
                e,
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
            logger.debug(
                "For-each group '%s': Empty array, skipping execution",
                for_each_group.name,
            )
            # Return empty output with appropriate structure
            empty_outputs = {} if for_each_group.key_by else []
            return ForEachGroupOutput(outputs=empty_outputs, errors={}, count=0)

        self._emit(
            "for_each_started",
            {
                "group_name": for_each_group.name,
                "item_count": len(items),
                "max_concurrent": for_each_group.max_concurrent,
                "failure_mode": for_each_group.failure_mode,
            },
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

            self._emit(
                "for_each_item_started",
                {
                    "group_name": for_each_group.name,
                    "item_key": key,
                    "index": index,
                },
            )

            try:
                # Build context for this item using the snapshot
                agent_context = context_snapshot.build_for_agent(
                    for_each_group.agent.name,
                    for_each_group.agent.input,
                    mode=self.config.workflow.context.mode,
                )
                # input_mapping templates for sub-workflows are rendered
                # locally (no LLM cost), so workflow inputs must always
                # be available — even in explicit mode.
                if for_each_group.agent.type == "workflow":
                    agent_context.setdefault("workflow", {})["input"] = (
                        context_snapshot.workflow_inputs.copy()
                    )

                # Inject loop variables into context
                self._inject_loop_variables(
                    agent_context,
                    for_each_group.as_,
                    item,
                    index,
                    key if for_each_group.key_by else None,
                )

                # Execute agent — sub-workflow or regular
                if for_each_group.agent.type == "workflow":
                    # Build sub-workflow inputs from input_mapping with loop vars
                    if for_each_group.agent.input_mapping:
                        renderer = TemplateRenderer()
                        sub_inputs: dict[str, Any] = {}
                        for k, tmpl in for_each_group.agent.input_mapping.items():
                            rendered = renderer.render(tmpl, agent_context)
                            try:
                                sub_inputs[k] = json.loads(rendered)
                            except (json.JSONDecodeError, ValueError):
                                sub_inputs[k] = rendered
                    else:
                        wf_ctx = agent_context.get("workflow", {})
                        sub_inputs = (
                            dict(wf_ctx.get("input", {})) if isinstance(wf_ctx, dict) else {}
                        )

                    # Execute sub-workflow
                    self._emit(
                        "subworkflow_started",
                        {
                            "agent_name": for_each_group.name,
                            "item_key": key,
                            "workflow": for_each_group.agent.workflow,
                        },
                    )
                    output_content = await self._execute_subworkflow_with_inputs(
                        for_each_group.agent, sub_inputs, agent_context
                    )
                    _item_elapsed = _time.time() - _item_start

                    self._emit(
                        "for_each_item_completed",
                        {
                            "group_name": for_each_group.name,
                            "item_key": key,
                            "elapsed": _item_elapsed,
                            "tokens": 0,
                            "cost_usd": 0.0,
                            "output": output_content,
                        },
                    )
                    return (key, output_content)

                # Regular agent execution
                executor = await self._get_executor_for_agent(for_each_group.agent)

                # Item-scoped event callback that tags all streaming events with item_key
                def _item_callback(event_type: str, data: dict[str, Any]) -> None:
                    data_with_agent = {"agent_name": for_each_group.name, "item_key": key, **data}
                    self._emit(event_type, data_with_agent)

                event_callback = _item_callback if self._event_emitter else None
                output = await executor.execute(
                    for_each_group.agent,
                    agent_context,
                    event_callback=event_callback,
                )
                _item_elapsed = _time.time() - _item_start

                # Record usage and calculate cost
                usage = self.usage_tracker.record(
                    f"{for_each_group.name}[{key}]", output, _item_elapsed
                )

                self._emit(
                    "for_each_item_completed",
                    {
                        "group_name": for_each_group.name,
                        "item_key": key,
                        "elapsed": _item_elapsed,
                        "tokens": output.tokens_used,
                        "cost_usd": usage.cost_usd,
                        "output": output.content,
                    },
                )

                return (key, output.content)
            except Exception as e:
                _item_elapsed = _time.time() - _item_start

                # Verbose: Log item failure
                self._emit(
                    "for_each_item_failed",
                    {
                        "group_name": for_each_group.name,
                        "item_key": key,
                        "elapsed": _item_elapsed,
                        "error_type": type(e).__name__,
                        "message": str(e),
                    },
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
        self._emit(
            "for_each_completed",
            {
                "group_name": for_each_group.name,
                "success_count": success_count,
                "failure_count": failure_count,
                "elapsed": _group_elapsed,
            },
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
