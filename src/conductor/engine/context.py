"""Workflow context management for Conductor.

This module provides the WorkflowContext class for managing workflow state,
including inputs, agent outputs, and execution history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from conductor.providers.base import AgentProvider

# Token estimation constants
# Average characters per token (conservative estimate)
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Estimate the number of tokens in a text string.

    Uses a simple character-based estimate. For more accurate token
    counting, use tiktoken or the provider's tokenizer.

    Args:
        text: The text to estimate tokens for.

    Returns:
        Estimated number of tokens.
    """
    return len(text) // CHARS_PER_TOKEN


def estimate_dict_tokens(data: dict[str, Any]) -> int:
    """Estimate the number of tokens in a dictionary (serialized as JSON).

    Args:
        data: The dictionary to estimate tokens for.

    Returns:
        Estimated number of tokens.
    """
    import json

    try:
        text = json.dumps(data)
        return estimate_tokens(text)
    except (TypeError, ValueError):
        # If we can't serialize, estimate from string representation
        return estimate_tokens(str(data))


@dataclass
class WorkflowContext:
    """Manages workflow execution state and context accumulation.

    The WorkflowContext stores all state needed during workflow execution:
    - workflow_inputs: Initial inputs provided when starting the workflow
    - agent_outputs: Outputs from each executed agent
    - current_iteration: Counter for iteration limit enforcement
    - execution_history: Ordered list of executed agent names

    Context modes:
    - accumulate: All prior agent outputs are available to subsequent agents
    - last_only: Only the most recent agent's output is available
    - explicit: Only inputs explicitly declared in the agent's input list are available

    Example:
        >>> ctx = WorkflowContext()
        >>> ctx.set_workflow_inputs({"question": "What is Python?"})
        >>> ctx.store("answerer", {"answer": "A programming language"})
        >>> agent_ctx = ctx.build_for_agent("checker", ["answerer.output"], "accumulate")
        >>> agent_ctx["answerer"]["output"]["answer"]
        'A programming language'
    """

    workflow_inputs: dict[str, Any] = field(default_factory=dict)
    """Inputs provided at workflow start."""

    agent_outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Outputs from executed agents, keyed by agent name."""

    current_iteration: int = 0
    """Current execution iteration count."""

    execution_history: list[str] = field(default_factory=list)
    """Ordered list of executed agent names."""

    def set_workflow_inputs(self, inputs: dict[str, Any]) -> None:
        """Store workflow-level inputs.

        Args:
            inputs: Dictionary of input values provided at workflow start.
        """
        self.workflow_inputs = inputs.copy()

    def store(self, agent_name: str, output: dict[str, Any]) -> None:
        """Store an agent's output in context.

        This method updates the agent_outputs dictionary, appends the agent
        to execution_history, and increments the iteration counter.

        Args:
            agent_name: The name of the agent whose output is being stored.
            output: The structured output from the agent.
        """
        self.agent_outputs[agent_name] = output
        self.execution_history.append(agent_name)
        self.current_iteration += 1

    def build_for_agent(
        self,
        agent_name: str,
        inputs: list[str],
        mode: str = "accumulate",
    ) -> dict[str, Any]:
        """Build context dict for a specific agent based on its input declarations.

        The context includes:
        - workflow: Contains workflow-level inputs under workflow.input
        - context: Metadata about execution (iteration, history)
        - Agent outputs: Based on the accumulation mode

        Args:
            agent_name: Name of the agent needing context.
            inputs: List of input references (e.g., ['workflow.input.goal', 'planner.output']).
            mode: Context mode - accumulate, last_only, or explicit.

        Returns:
            Dict with 'workflow', agent outputs, and 'context' metadata.

        Raises:
            KeyError: If explicit mode is used and a required (non-optional) input is missing.
        """
        # For explicit mode, start with empty workflow inputs
        # For other modes, include all workflow inputs
        if mode == "explicit":
            ctx: dict[str, Any] = {
                "workflow": {"input": {}},
                "context": {
                    "iteration": self.current_iteration,
                    "history": self.execution_history.copy(),
                },
            }
            # Only declared inputs - parse input references
            for input_ref in inputs:
                self._add_explicit_input(ctx, input_ref)
        else:
            ctx = {
                "workflow": {"input": self.workflow_inputs.copy()},
                "context": {
                    "iteration": self.current_iteration,
                    "history": self.execution_history.copy(),
                },
            }

            if mode == "accumulate":
                # All prior agent outputs available
                for agent, output in self.agent_outputs.items():
                    # Check if this is a parallel group output
                    # (has 'outputs' and 'errors' keys at top level)
                    is_parallel_group = (
                        isinstance(output, dict) and "outputs" in output and "errors" in output
                    )

                    if is_parallel_group:
                        # Parallel groups store their structure directly
                        ctx[agent] = output
                    else:
                        # Regular agents wrap output in {"output": ...}
                        ctx[agent] = {"output": output}

            elif mode == "last_only" and self.execution_history:
                # Only the most recent agent's output
                last_agent = self.execution_history[-1]
                last_output = self.agent_outputs.get(last_agent, {})

                # Check if this is a parallel group output
                is_parallel_group = (
                    isinstance(last_output, dict)
                    and "outputs" in last_output
                    and "errors" in last_output
                )

                if is_parallel_group:
                    ctx[last_agent] = last_output
                else:
                    ctx[last_agent] = {"output": last_output}

        return ctx

    def _add_explicit_input(self, ctx: dict[str, Any], input_ref: str) -> None:
        """Add an explicit input reference to the context.

        Handles optional dependencies with '?' suffix - missing optional
        dependencies are silently skipped instead of raising errors.

        Input reference formats:
        - workflow.input.param_name - References a workflow input
        - agent_name.output - References an agent's entire output
        - agent_name.output.field - References a specific output field
        - agent_name.field - Shorthand for agent_name.output.field (deprecated but supported)
        - parallel_group.outputs - References all parallel group outputs
        - parallel_group.outputs.agent_name - References a specific parallel agent's output
        - parallel_group.outputs.agent_name.field - Specific field from parallel agent
        - parallel_group.errors - References all parallel group errors
        - Any reference with '?' suffix - Optional dependency

        Args:
            ctx: The context dictionary to update.
            input_ref: The input reference string.

        Raises:
            KeyError: If a required (non-optional) input is not found.
        """
        # Check for optional suffix
        is_optional = input_ref.endswith("?")
        ref = input_ref.rstrip("?")

        parts = ref.split(".")

        if len(parts) < 2:
            if not is_optional:
                raise KeyError(f"Invalid input reference format: {input_ref}")
            return

        if parts[0] == "workflow":
            # workflow.input.param_name format
            if len(parts) >= 3 and parts[1] == "input":
                param_name = parts[2]
                # Ensure workflow.input exists in ctx
                if "workflow" not in ctx:
                    ctx["workflow"] = {"input": {}}
                elif "input" not in ctx["workflow"]:
                    ctx["workflow"]["input"] = {}

                if param_name in self.workflow_inputs:
                    ctx["workflow"]["input"][param_name] = self.workflow_inputs[param_name]
                elif is_optional:
                    # Set optional inputs to None so templates can check them
                    ctx["workflow"]["input"][param_name] = None
                else:
                    raise KeyError(f"Missing required workflow input: {param_name}")
        else:
            # Could be agent_name.output or parallel_group.outputs
            entity_name = parts[0]

            if entity_name in self.agent_outputs:
                agent_output = self.agent_outputs[entity_name]

                # Check if this is a parallel group (has 'outputs' and 'errors' keys)
                is_parallel_group = (
                    isinstance(agent_output, dict)
                    and "outputs" in agent_output
                    and "errors" in agent_output
                )

                if is_parallel_group:
                    # Handle parallel group references
                    self._add_parallel_group_input(ctx, entity_name, parts[1:], is_optional)
                else:
                    # Handle regular agent references
                    self._add_agent_input(ctx, entity_name, parts[1:], is_optional)
            elif not is_optional:
                raise KeyError(f"Missing required agent output: {entity_name}")

    def _add_agent_input(
        self, ctx: dict[str, Any], agent_name: str, remaining_parts: list[str], is_optional: bool
    ) -> None:
        """Add a regular agent output reference to context.

        Args:
            ctx: The context dictionary to update.
            agent_name: The name of the agent.
            remaining_parts: The remaining path parts after agent name.
            is_optional: Whether this is an optional reference.

        Raises:
            KeyError: If a required field is missing.
        """
        # Ensure the agent context exists
        if agent_name not in ctx:
            ctx[agent_name] = {"output": {}}
        elif "output" not in ctx[agent_name]:
            ctx[agent_name]["output"] = {}

        agent_output = self.agent_outputs[agent_name]

        if not remaining_parts:
            # Just agent_name - copy entire output
            ctx[agent_name]["output"] = agent_output.copy()
        elif len(remaining_parts) == 1 and remaining_parts[0] == "output":
            # agent_name.output - copy entire output
            ctx[agent_name]["output"] = agent_output.copy()
        elif len(remaining_parts) >= 2 and remaining_parts[0] == "output":
            # agent_name.output.field - copy specific field
            field_name = remaining_parts[1]
            if field_name in agent_output:
                ctx[agent_name]["output"][field_name] = agent_output[field_name]
            elif not is_optional:
                raise KeyError(f"Missing output field '{field_name}' from agent '{agent_name}'")
        elif len(remaining_parts) == 1 and remaining_parts[0] != "output":
            # Shorthand format: agent_name.field -> agent_name.output.field
            field_name = remaining_parts[0]
            if field_name in agent_output:
                ctx[agent_name]["output"][field_name] = agent_output[field_name]
            elif not is_optional:
                raise KeyError(f"Missing output field '{field_name}' from agent '{agent_name}'")

    def _add_parallel_group_input(
        self, ctx: dict[str, Any], group_name: str, remaining_parts: list[str], is_optional: bool
    ) -> None:
        """Add a parallel/for-each group output reference to context.

        Supports patterns for static parallel groups:
        - parallel_group.outputs - All outputs
        - parallel_group.outputs.agent_name - Specific agent's output
        - parallel_group.outputs.agent_name.field - Specific field
        - parallel_group.errors - All errors

        Supports patterns for for-each groups (list-based):
        - for_each.outputs - All outputs (list)
        - for_each.outputs[0] - Cannot be handled here (requires template eval)
        - for_each.errors - All errors

        Supports patterns for for-each groups (dict-based with key_by):
        - for_each.outputs - All outputs (dict)
        - for_each.outputs["key"] - Cannot be handled here (requires template eval)
        - for_each.outputs.key - Specific key's output
        - for_each.errors - All errors

        Note: Index/key access like outputs[0] or outputs["key"] is handled
        by the template engine, not by this method. This method only handles
        dotted path access in explicit input declarations.

        Args:
            ctx: The context dictionary to update.
            group_name: The name of the parallel/for-each group.
            remaining_parts: The remaining path parts after group name.
            is_optional: Whether this is an optional reference.

        Raises:
            KeyError: If a required field is missing.
        """
        # Ensure the parallel group context exists
        if group_name not in ctx:
            ctx[group_name] = {}

        group_output = self.agent_outputs[group_name]

        if not remaining_parts:
            # Just parallel_group - copy entire output structure
            ctx[group_name] = group_output.copy()
        elif remaining_parts[0] == "outputs":
            # Determine if this is a for-each group or static parallel group using 'type' field
            outputs = group_output["outputs"]
            group_type = group_output.get("type")  # 'parallel' or 'for_each'

            # For backward compatibility, fall back to heuristic if 'type' is missing
            if group_type is None:
                is_for_each_list = isinstance(outputs, list)
                is_for_each_dict = (
                    isinstance(outputs, dict) and group_output.get("count") is not None
                )
            else:
                is_for_each_list = group_type == "for_each" and isinstance(outputs, list)
                is_for_each_dict = group_type == "for_each" and isinstance(outputs, dict)

            if len(remaining_parts) == 1:
                # group.outputs - copy all outputs (works for both static parallel and for-each)
                ctx[group_name]["outputs"] = outputs.copy()
            elif is_for_each_list:
                # For-each group with list outputs
                # Cannot handle index access (outputs.0), use template syntax outputs[0]
                # This would be an error in input declaration
                if not is_optional:
                    raise KeyError(
                        f"Cannot use dotted path with list-based for-each outputs. "
                        f"Use template syntax like '{{{{ {group_name}.outputs[0] }}}}' instead of "
                        f"declaring '{group_name}.{'.'.join(remaining_parts)}' in inputs."
                    )
            elif is_for_each_dict or len(remaining_parts) == 2:
                # For-each group with dict outputs OR static parallel group
                # Both use: group.outputs.key_or_agent_name
                key_or_agent = remaining_parts[1]

                if "outputs" not in ctx[group_name]:
                    ctx[group_name]["outputs"] = {}

                if key_or_agent in outputs:
                    ctx[group_name]["outputs"][key_or_agent] = outputs[key_or_agent]
                elif not is_optional:
                    raise KeyError(
                        f"Missing key/agent '{key_or_agent}' in outputs of '{group_name}'"
                    )
            elif len(remaining_parts) >= 3:
                # group.outputs.key_or_agent.field - access specific field
                key_or_agent = remaining_parts[1]
                field_name = remaining_parts[2]

                if "outputs" not in ctx[group_name]:
                    ctx[group_name]["outputs"] = {}

                if key_or_agent in outputs:
                    item_output = outputs[key_or_agent]
                    if isinstance(item_output, dict) and field_name in item_output:
                        # Ensure the key/agent dict exists in context
                        if key_or_agent not in ctx[group_name]["outputs"]:
                            ctx[group_name]["outputs"][key_or_agent] = {}
                        ctx[group_name]["outputs"][key_or_agent][field_name] = item_output[
                            field_name
                        ]
                    elif not is_optional:
                        raise KeyError(
                            f"Missing field '{field_name}' in outputs['{key_or_agent}'] "
                            f"of '{group_name}'"
                        )
                elif not is_optional:
                    raise KeyError(
                        f"Missing key/agent '{key_or_agent}' in outputs of '{group_name}'"
                    )
        elif remaining_parts[0] == "errors":
            # group.errors - copy errors dict (works for both static parallel and for-each)
            if "errors" not in ctx[group_name]:
                ctx[group_name]["errors"] = {}
            ctx[group_name]["errors"] = group_output["errors"].copy()
        elif remaining_parts[0] == "count":
            # group.count - for-each groups only
            if "count" in group_output:
                ctx[group_name]["count"] = group_output["count"]
            elif not is_optional:
                raise KeyError(
                    f"'{group_name}' is not a for-each group (no 'count' field available)"
                )

    def get_for_template(self) -> dict[str, Any]:
        """Get full context for template rendering.

        Returns a context dictionary with all agent outputs and workflow
        inputs available for use in output template expressions.

        Returns:
            Dict with workflow inputs and all agent outputs.
        """
        return self.build_for_agent("__template__", [], mode="accumulate")

    def get_latest_output(self) -> dict[str, Any] | None:
        """Get the output from the most recently executed agent.

        Returns:
            The output dictionary from the last agent, or None if no agents executed.
        """
        if not self.execution_history:
            return None
        last_agent = self.execution_history[-1]
        return self.agent_outputs.get(last_agent)

    def estimate_context_tokens(self) -> int:
        """Estimate the total number of tokens in the current context.

        Returns:
            Estimated number of tokens in the full context.
        """
        ctx = self.get_for_template()
        return estimate_dict_tokens(ctx)

    def trim_context(
        self,
        max_tokens: int,
        strategy: Literal["truncate", "drop_oldest", "summarize"] = "drop_oldest",
        provider: AgentProvider | None = None,
    ) -> int:
        """Trim context to fit within max_tokens.

        Applies the specified trimming strategy to reduce context size.

        Strategies:
        - truncate: Cut oldest content from each output to fit
        - drop_oldest: Remove entire oldest agent outputs FIFO until within limit
        - summarize: Use LLM provider to summarize context (requires provider)

        Args:
            max_tokens: Maximum number of tokens allowed.
            strategy: Trimming strategy to use.
            provider: Provider for summarize strategy (required for summarize).

        Returns:
            Number of tokens after trimming.

        Raises:
            ValueError: If summarize strategy is used without a provider.
        """
        current_tokens = self.estimate_context_tokens()

        if current_tokens <= max_tokens:
            return current_tokens

        if strategy == "drop_oldest":
            return self._trim_drop_oldest(max_tokens)
        elif strategy == "truncate":
            return self._trim_truncate(max_tokens)
        elif strategy == "summarize":
            if provider is None:
                raise ValueError("summarize strategy requires a provider")
            return self._trim_summarize(max_tokens, provider)
        else:
            raise ValueError(f"Unknown trimming strategy: {strategy}")

    def _trim_drop_oldest(self, max_tokens: int) -> int:
        """Trim by dropping oldest agent outputs.

        Removes entire agent outputs from oldest to newest until
        the context fits within max_tokens.

        Args:
            max_tokens: Maximum number of tokens allowed.

        Returns:
            Number of tokens after trimming.
        """
        # Get unique agents in execution order (first occurrence)
        seen_agents: list[str] = []
        for agent in self.execution_history:
            if agent not in seen_agents:
                seen_agents.append(agent)

        # Drop from oldest to newest
        for agent_name in seen_agents:
            if self.estimate_context_tokens() <= max_tokens:
                break

            if agent_name in self.agent_outputs:
                del self.agent_outputs[agent_name]

        return self.estimate_context_tokens()

    def _trim_truncate(self, max_tokens: int) -> int:
        """Trim by truncating oldest content from outputs.

        Truncates string values in agent outputs, starting from the
        oldest agent, until the context fits within max_tokens.

        Args:
            max_tokens: Maximum number of tokens allowed.

        Returns:
            Number of tokens after trimming.
        """
        # Get unique agents in execution order
        seen_agents: list[str] = []
        for agent in self.execution_history:
            if agent not in seen_agents:
                seen_agents.append(agent)

        # Calculate how many tokens we need to cut
        current_tokens = self.estimate_context_tokens()
        tokens_to_cut = current_tokens - max_tokens

        if tokens_to_cut <= 0:
            return current_tokens

        # Truncate string values in oldest outputs first
        for agent_name in seen_agents:
            if tokens_to_cut <= 0:
                break

            if agent_name not in self.agent_outputs:
                continue

            output = self.agent_outputs[agent_name]
            for key, value in list(output.items()):
                if isinstance(value, str) and len(value) > 100:
                    # Calculate how much to truncate from this value
                    value_tokens = estimate_tokens(value)
                    if value_tokens > 50:
                        # Keep at least some content
                        chars_to_keep = max(50 * CHARS_PER_TOKEN, len(value) // 4)
                        truncated = value[:chars_to_keep] + "... [truncated]"
                        output[key] = truncated
                        tokens_cut = estimate_tokens(value) - estimate_tokens(truncated)
                        tokens_to_cut -= tokens_cut

            if tokens_to_cut <= 0:
                break

        return self.estimate_context_tokens()

    def _trim_summarize(self, max_tokens: int, provider: AgentProvider) -> int:
        """Trim by summarizing context with LLM.

        Uses the provider to generate a summary of older context,
        replacing detailed outputs with a condensed summary.

        Note: This is a simplified implementation. A full implementation
        would need to be async and actually call the provider.

        Args:
            max_tokens: Maximum number of tokens allowed.
            provider: Provider to use for summarization.

        Returns:
            Number of tokens after trimming.
        """
        # Get unique agents in execution order
        seen_agents: list[str] = []
        for agent in self.execution_history:
            if agent not in seen_agents:
                seen_agents.append(agent)

        # For simplicity, we'll use drop_oldest as a fallback
        # A real implementation would call the provider to summarize
        # the oldest outputs before dropping them.

        # Summarize strategy: keep recent outputs, summarize/drop old ones
        # Keep the most recent half of agents
        recent_count = max(1, len(seen_agents) // 2)

        # For agents we're dropping, create a summary entry
        dropped_agents = seen_agents[:-recent_count] if recent_count < len(seen_agents) else []

        if dropped_agents:
            # Create a summary of dropped agents
            summary_parts = []
            for agent_name in dropped_agents:
                if agent_name in self.agent_outputs:
                    output = self.agent_outputs[agent_name]
                    # Create a brief summary of the output
                    summary = f"{agent_name}: "
                    for key, value in output.items():
                        if isinstance(value, str):
                            summary += f"{key}={value[:50]}... "
                        else:
                            summary += f"{key}={value} "
                    summary_parts.append(summary.strip())
                    del self.agent_outputs[agent_name]

            # Store summary as a special context entry
            if summary_parts:
                self.agent_outputs["_context_summary"] = {
                    "summary": "; ".join(summary_parts)[:500],
                    "dropped_agents": dropped_agents,
                }

        return self.estimate_context_tokens()
