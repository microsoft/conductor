"""Pydantic models for workflow configuration.

This module defines all Pydantic models for validating and parsing
workflow YAML configuration files.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_serializer,
    model_validator,
)

from conductor.duration import parse_duration
from conductor.providers.context_tier import ContextTier
from conductor.providers.reasoning import ReasoningEffort

# Maximum allowed wait-step duration (24 hours). Anything longer almost
# certainly wants ``limits.timeout_seconds`` reconsidered first.
MAX_WAIT_DURATION_SECONDS = 24 * 60 * 60


class InputDef(BaseModel):
    """Definition for a workflow input parameter."""

    type: Literal["string", "number", "boolean", "array", "object"]
    """The type of the input parameter."""

    required: bool = True
    """Whether the input is required."""

    default: Any = None
    """Default value if the input is not provided."""

    description: str | None = None
    """Human-readable description of the input."""

    @field_validator("default")
    @classmethod
    def validate_default_type(cls, v: Any, info) -> Any:
        """Ensure default value matches declared type."""
        if v is None:
            return v

        # Get the declared type from the data being validated
        type_value = info.data.get("type")
        if type_value is None:
            return v

        # Type validation based on declared type
        type_checks = {
            "string": lambda x: isinstance(x, str),
            "number": lambda x: isinstance(x, int | float) and not isinstance(x, bool),
            "boolean": lambda x: isinstance(x, bool),
            "array": lambda x: isinstance(x, list),
            "object": lambda x: isinstance(x, dict),
        }

        check = type_checks.get(type_value)
        if check and not check(v):
            raise ValueError(
                f"default value must be of type '{type_value}', got {type(v).__name__}"
            )

        return v


class OutputField(BaseModel):
    """Schema for a single output field from an agent."""

    type: Literal["string", "number", "boolean", "array", "object"]
    """The type of the output field."""

    description: str | None = None
    """Human-readable description of the output field."""

    items: OutputField | None = None
    """For array types, the schema of array items."""

    properties: dict[str, OutputField] | None = None
    """For object types, the schema of object properties."""

    @model_validator(mode="after")
    def validate_type_specific_fields(self) -> OutputField:
        """Ensure type-specific fields are properly set."""
        if self.type == "array" and self.items is None:
            # Items are optional but recommended for arrays
            pass
        if self.type == "object" and self.properties is None:
            # Properties are optional but recommended for objects
            pass
        return self


class RouteDef(BaseModel):
    """Definition for a routing rule."""

    model_config = ConfigDict(extra="forbid")

    to: str
    """Target agent name, '$end', or human gate name."""

    when: str | None = None
    """Optional condition expression (Jinja2 template that evaluates to bool)."""

    output: dict[str, str] | None = None
    """Optional output transformation (template expressions)."""

    @field_validator("to")
    @classmethod
    def validate_target(cls, v: str) -> str:
        """Validate route target format."""
        if not v:
            raise ValueError("Route target cannot be empty")
        return v


class ParallelGroup(BaseModel):
    """Definition for a parallel agent execution group."""

    model_config = ConfigDict(extra="forbid")

    name: str
    """Unique identifier for this parallel group."""

    description: str | None = None
    """Human-readable description of the parallel group's purpose."""

    agents: list[str]
    """Names of agents to execute in parallel."""

    failure_mode: Literal["fail_fast", "continue_on_error", "all_or_nothing"] = "fail_fast"
    """
    Failure handling mode:
    - fail_fast: Stop immediately on first agent failure (default)
    - continue_on_error: Continue if at least one agent succeeds
    - all_or_nothing: All agents must succeed or entire group fails
    """

    routes: list[RouteDef] = Field(default_factory=list)
    """Routing rules evaluated in order after parallel group execution."""

    @field_validator("agents")
    @classmethod
    def validate_agents_count(cls, v: list[str]) -> list[str]:
        """Ensure at least 2 agents in parallel group."""
        if len(v) < 2:
            raise ValueError("Parallel groups must contain at least 2 agents")
        return v


class ForEachDef(BaseModel):
    """Definition for a dynamic parallel (for-each) agent group.

    For-each groups spawn N parallel agent instances at runtime based on
    an array resolved from workflow context (e.g., a previous agent's output).

    Example:
        ```yaml
        for_each:
          - name: analyzers
            type: for_each
            source: finder.output.kpis
            as: kpi
            max_concurrent: 5
            agent:
              model: opus-4.5
              prompt: "Analyze {{ kpi.kpi_id }}"
              output:
                success: { type: boolean }
        ```
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    """Unique identifier for this for-each group."""

    description: str | None = None
    """Human-readable description."""

    type: Literal["for_each"]
    """Discriminator for union types in routing."""

    source: str
    """Reference to array in context (e.g., 'finder.output.kpis').
    Must resolve to a list at runtime. Uses dotted path notation."""

    as_: str = Field(..., serialization_alias="as", validation_alias="as")
    """Loop variable name (e.g., 'kpi').
    Accessible in templates as {{ kpi }}.
    Note: Uses as_ internally to avoid Python keyword conflict.
    Pydantic aliases ensure YAML uses 'as' while Python uses 'as_'."""

    agent: AgentDef
    """Inline agent definition used as template for each item.
    Each instance gets a copy with loop variables injected into context."""

    max_concurrent: int = 10
    """Maximum number of concurrent executions per batch.
    Items are processed in sequential batches of this size.
    Default: 10 (prevents unbounded parallelism)."""

    failure_mode: Literal["fail_fast", "continue_on_error", "all_or_nothing"] = "fail_fast"
    """Failure handling strategy:
    - fail_fast: Stop on first error, raise immediately
    - continue_on_error: Continue all items, fail only if ALL fail
    - all_or_nothing: Continue all items, fail if ANY fail"""

    key_by: str | None = None
    """Optional: Path to extract key from each item for dict-based outputs.
    Example: 'kpi.kpi_id' → outputs becomes {kpi_id: {...}, ...}
    instead of [{...}, ...]. Enables key-based access: outputs["KPI123"]."""

    routes: list[RouteDef] = Field(default_factory=list)
    """Routing rules evaluated after for-each execution.
    Routes have access to aggregated outputs via {{ analyzers.outputs }}."""

    @field_validator("as_")
    @classmethod
    def validate_loop_variable(cls, v: str) -> str:
        """Ensure loop variable doesn't conflict with reserved names.

        Reserved names: workflow, context, output, _index, _key
        These are reserved for workflow internals.
        """
        reserved = {"workflow", "context", "output", "_index", "_key"}
        if v in reserved:
            raise ValueError(
                f"Loop variable '{v}' conflicts with reserved name. Reserved names: {reserved}"
            )
        # Also validate it's a valid Python identifier
        if not v.isidentifier():
            raise ValueError(f"Loop variable '{v}' must be a valid Python identifier")
        return v

    @field_validator("source")
    @classmethod
    def validate_source_format(cls, v: str) -> str:
        """Validate source reference format (agent_name.output.field).

        This is a basic format check - actual resolution happens at runtime.
        """
        parts = v.split(".")
        if len(parts) < 3:
            raise ValueError(
                f"Invalid source format: '{v}'. "
                f"Expected format: 'agent_name.output.field' (minimum 3 parts)"
            )
        # First part should be a valid identifier
        if not parts[0].isidentifier():
            raise ValueError(
                f"Invalid agent name in source: '{parts[0]}' is not a valid identifier"
            )
        return v

    @field_validator("max_concurrent")
    @classmethod
    def validate_max_concurrent(cls, v: int) -> int:
        """Ensure max_concurrent is reasonable."""
        if v < 1:
            raise ValueError("max_concurrent must be at least 1")
        if v > 100:
            raise ValueError(
                "max_concurrent cannot exceed 100 (consider batching for larger arrays)"
            )
        return v


class GateOption(BaseModel):
    """Option presented in a human gate."""

    label: str
    """Display text for the option."""

    value: str
    """Value stored when option selected."""

    route: str
    """Agent to route to when selected."""

    prompt_for: str | None = None
    """Optional: field name to prompt for text input."""


class ContextConfig(BaseModel):
    """Configuration for context accumulation behavior."""

    mode: Literal["accumulate", "last_only", "explicit"] = "accumulate"
    """
    Context accumulation mode:
    - accumulate: All prior outputs available (default)
    - last_only: Only previous agent's output available
    - explicit: Only inputs listed in the agent's `input` array are available;
                nothing is automatically accumulated from prior agents
    """

    max_tokens: int | None = None
    """Maximum context tokens before trimming."""

    trim_strategy: Literal["summarize", "truncate", "drop_oldest"] | None = None
    """Strategy for reducing context size when limit exceeded."""


class LimitsConfig(BaseModel):
    """Safety limits for workflow execution."""

    max_iterations: int = Field(default=10, ge=1, le=500)
    """Maximum number of agent executions before forced termination."""

    timeout_seconds: int | None = Field(default=None, ge=1)
    """Maximum wall-clock time for entire workflow in seconds.

    Default is None (unlimited). Idle detection at the session level (5 min)
    handles most stuck cases. Set an explicit value for workflows that need
    a hard time limit.
    """


class PricingOverride(BaseModel):
    """Custom pricing for a specific model.

    Used to override default pricing or add pricing for models
    not in the default pricing table.
    """

    input_per_mtok: float = Field(ge=0, description="Cost per million input tokens (USD)")
    output_per_mtok: float = Field(ge=0, description="Cost per million output tokens (USD)")
    cache_read_per_mtok: float = Field(
        default=0.0, ge=0, description="Cost per million cache read tokens (USD)"
    )
    cache_write_per_mtok: float = Field(
        default=0.0, ge=0, description="Cost per million cache write tokens (USD)"
    )


class CostConfig(BaseModel):
    """Cost tracking configuration.

    Controls how token usage and costs are tracked and displayed.
    """

    show_per_agent: bool = True
    """Whether to show cost per agent in verbose output."""

    show_summary: bool = True
    """Whether to show cost summary at end of workflow."""

    pricing: dict[str, PricingOverride] = Field(default_factory=dict)
    """Custom pricing overrides for specific models."""


class HooksConfig(BaseModel):
    """Lifecycle hooks for workflow events."""

    on_start: str | None = None
    """Expression evaluated when workflow starts."""

    on_complete: str | None = None
    """Expression evaluated when workflow completes successfully."""

    on_error: str | None = None
    """Expression evaluated when workflow fails."""


class RetryPolicy(BaseModel):
    """Per-agent retry policy for transient failure resilience.

    Controls how an agent retries on transient failures such as API errors,
    rate limits, and timeouts. Retry counter resets per agent execution.

    Example YAML::

        retry:
          max_attempts: 3
          backoff: exponential
          delay_seconds: 2
          retry_on:
            - provider_error
            - timeout
    """

    max_attempts: int = Field(default=1, ge=1, le=10)
    """Maximum number of attempts (including the first). 1 = no retry."""

    backoff: Literal["fixed", "exponential"] = "exponential"
    """Backoff strategy between retries."""

    delay_seconds: float = Field(default=2.0, ge=0.0, le=300.0)
    """Base delay in seconds before the first retry."""

    retry_on: list[Literal["provider_error", "timeout"]] = Field(
        default_factory=lambda: ["provider_error", "timeout"]
    )
    """Error categories that trigger a retry.

    - ``provider_error``: API 500s, rate limits, transient provider failures.
    - ``timeout``: Agent-level timeout exceeded.

    Validation errors (output schema mismatches) are never retried because
    they indicate prompt/schema issues, not transience.
    """


class DialogConfig(BaseModel):
    """Configuration for agent dialog mode.

    When present on an agent, enables the agent to conditionally pause
    after execution and enter a free-form conversation with the user.

    An evaluator LLM call examines the agent's output against the
    user-defined trigger_prompt criteria and decides whether to pause
    and start a conversation.

    Example YAML::

        dialog:
          trigger_prompt: |
            Enter dialog if the agent expresses uncertainty about
            the user's intent or needs clarification on requirements.
    """

    trigger_prompt: str
    """User-defined criteria for when to enter dialog mode.

    This prompt is wrapped in a system message and evaluated against
    the agent's output. The evaluator decides whether to pause and
    start a conversation with the user.
    """


class ValidatorConfig(BaseModel):
    """Configuration for semantic output validation with retry-once.

    When present on a provider-backed agent, the engine runs a **second
    LLM call** after the primary agent completes. The validator receives
    the primary agent's rendered prompt, its output, and the ``criteria``
    rubric, and must answer whether the output passes
    (``{"passed": bool, "issues": [str, ...]}``).

    If the validator returns ``passed: false`` and ``max_retries > 0``, the
    primary agent is re-run **once** with the validator's feedback appended
    to its prompt. The second output is taken as final — there is no second
    validation loop.

    This is distinct from ``retry:`` (transient/provider failures, same
    prompt) and the ``output:`` schema (shape/type, not content quality).
    It targets structurally valid but semantically wrong, incomplete, or
    off-rubric output.

    Example YAML::

        validator:
          model: claude-sonnet-4-5   # optional; defaults to the agent's model
          criteria: |
            Verify the review identifies all null-safety issues, every
            suggestion is actionable, and no function names are fabricated.
          max_retries: 1
    """

    model_config = ConfigDict(extra="forbid")

    criteria: str
    """User-defined rubric the primary output is checked against.

    Wrapped in the validator's system prompt. Should describe concretely
    what a *good* output looks like (the checks the validator must perform),
    not merely restate the agent's task.
    """

    model: str | None = None
    """Model for the validator call. Defaults to the primary agent's model.

    Often set to a cheaper or faster model than the primary agent, since
    grading an output is usually lighter than producing it.
    """

    max_retries: int = Field(default=1, ge=0, le=1)
    """Number of times the primary agent is re-run on validation failure.

    Hard-capped at 1 by design — beyond a single feedback-driven retry you
    are fighting prompt design, not output noise. ``0`` validates and
    reports (emitting ``agent_validation_failed``) but never re-runs the
    primary agent.
    """

    @field_validator("criteria")
    @classmethod
    def validate_criteria(cls, v: str) -> str:
        """Reject criteria that is empty or whitespace-only.

        The original (unstripped) value is returned so multi-line rubric
        formatting is preserved.
        """
        if not v or not v.strip():
            raise ValueError("validator 'criteria' must be a non-empty string")
        return v


class ReasoningConfig(BaseModel):
    """Configuration for model reasoning / extended thinking effort.

    When present on an agent (or as a runtime default), enables the
    provider's reasoning capability:

    - **Copilot SDK** sets ``reasoning_effort`` on the session.
    - **Anthropic SDK** enables extended thinking with a budget mapped from
      the effort level (low=2k, medium=8k, high=16k, xhigh=32k tokens).

    Validation happens at execute time. Claude rejects models that don't
    match the supported prefix list; Copilot consults the SDK's advertised
    ``supported_reasoning_efforts`` (when available) and otherwise allows
    the request through to the SDK.

    Example YAML::

        reasoning:
          effort: high
    """

    effort: ReasoningEffort
    """Reasoning effort level applied to the agent's model calls."""


class AgentDef(BaseModel):
    """Definition for a single agent in the workflow.

    A single Pydantic model covers all step kinds. The ``type`` field
    discriminates between them:

    - ``agent`` (default): LLM-backed agent. Requires ``prompt``; supports
      ``model``, ``provider``, ``tools``, ``output``, ``reasoning``, ``retry``,
      ``dialog``, and ``timeout_seconds``.
    - ``human_gate``: Pause for user decision. Requires ``prompt`` and
      ``options``.
    - ``script``: Shell command step. Requires ``command``; supports
      ``args``, ``env``, ``working_dir``, ``timeout``. Output is always
      ``{stdout, stderr, exit_code}`` with parsed-JSON keys merged on top
      when ``stdout`` is valid JSON.
    - ``workflow``: Sub-workflow black-box step. Requires ``workflow:``
      (path or registry reference); supports ``input_mapping`` and
      ``max_depth``.
    - ``terminate``: Explicit terminal step. Requires ``status`` (``success``
      | ``failed``) and ``reason``; supports optional ``output_template``.
      Reaching one ends the workflow immediately (no routes evaluated
      after) and surfaces in the CLI exit code / dashboard / event log as
      a distinct, intentional outcome — distinguishable from a generic
      crash via ``is_explicit: true`` on the emitted lifecycle event.

    Per-type field forbidden-lists are enforced in
    :meth:`validate_agent_type`. Cross-cutting structural rules (e.g.,
    terminate steps cannot appear as parallel-group members or as a
    for_each inline agent) are enforced in
    :func:`conductor.config.validator.validate_workflow_config`.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    """Unique identifier for this agent."""

    description: str | None = None
    """Human-readable description of agent's purpose."""

    type: (
        Literal["agent", "human_gate", "script", "set", "terminate", "wait", "workflow"] | None
    ) = None
    """Agent type. Defaults to 'agent' if not specified."""

    provider: Literal["copilot", "claude", "claude-agent-sdk", "hermes"] | None = None
    """Provider override for this agent.

    If None (default), the agent uses the workflow.runtime.provider.
    When specified, this agent will use a different provider than
    the workflow default, enabling multi-provider workflows.

    Example:
        provider: claude  # Use Claude for this agent
        provider: hermes  # Use Hermes Agent for this agent
    """

    model: str | None = None
    """Model identifier.

    Examples:
    - GitHub Copilot: 'claude-sonnet-4', 'gpt-4', etc.
    - Claude (recommended default): 'claude-3-5-sonnet-latest' (stable, auto-updates)
    - Claude 4.5 Series (newest): 'claude-sonnet-4-5-20250929'
    - Claude 4 Series: 'claude-sonnet-4-20250514'
    - Claude 3.7 Series: 'claude-3-7-sonnet-20250219'
    - Claude 3.5 Series: 'claude-3-5-sonnet-20241022'
    - Claude 3 Series (legacy): 'claude-3-opus-20240229', 'claude-3-sonnet-20240229',
      'claude-3-haiku-20240307'

    Supports environment variables: ${MODEL:-default_value}
    Supports Jinja2 templates: {{ workflow.input.model_name }}
    """

    context_tier: ContextTier | None = None
    """Context-window tier for models that support it (Copilot provider only).

    Set ``context_tier: long_context`` to pin a heavy-reasoning agent to the
    model's long-context (e.g. 1M-token) window. ``default`` selects the
    standard tier; ``None`` sends no value (provider default).

    Falls back to ``runtime.default_context_tier`` when unset. Composes
    independently with ``reasoning`` — an agent may set both.

    Only the Copilot provider forwards this today (maps to the SDK's
    ``create_session`` ``context_tier`` param). Other providers ignore it.

    Only applies to provider-backed agents (type='agent' or None).

    Example YAML::

        context_tier: long_context
    """

    input: list[str] = Field(default_factory=list)
    """Context dependencies. Format: 'agent_name.output' or 'workflow.input.param'.
    Suffix with '?' for optional dependencies."""

    tools: list[str] | None = None
    """Tools available to this agent. None = all, [] = none."""

    system_prompt: str | None = None
    """System message for the agent (always included)."""

    prompt: str = ""
    """User prompt template (Jinja2)."""

    output: dict[str, OutputField] | None = None
    """Expected output schema for validation."""

    routes: list[RouteDef] = Field(default_factory=list)
    """Routing rules evaluated in order after execution."""

    options: list[GateOption] | None = None
    """Options for human_gate type agents."""

    command: str | None = None
    """Command to execute (required for script type). Supports Jinja2 templating."""

    args: list[str] = Field(default_factory=list)
    """Command-line arguments for script type. Each supports Jinja2 templating."""

    env: dict[str, str] = Field(default_factory=dict)
    """Environment variables for script subprocess."""

    working_dir: str | None = None
    """Working directory for script subprocess execution."""

    stdin: str | None = None
    """Payload written to the script subprocess's stdin (script type only).

    A Jinja2 string template rendered against the workflow context and written
    to the child process's stdin as UTF-8. Use this to hand large structured
    payloads to scripts without hitting OS command-line length limits (notably
    Windows's ~32 KB command-line cap):

    - JSON: ``stdin: "{{ upstream.output.evaluations | tojson }}"`` — the
      built-in ``tojson`` filter emits valid JSON.
    - Arbitrary text: ``stdin: "{{ diff }}"``.

    Semantics:

    - Omitted (``None``) — the child inherits the parent's stdin (the
      unchanged legacy behavior).
    - Present (any string, including ``""``) — stdin is piped; an explicit
      empty string sends immediate EOF.
    - Orthogonal to ``args`` — when both are set, ``args`` are still passed on
      the command line and ``stdin`` is piped.
    """

    timeout: int | None = None
    """Per-script timeout in seconds."""

    duration: str | int | float | None = None
    """Duration to pause for ``type='wait'`` steps.

    Accepts:
    - Plain ``int`` or ``float`` — interpreted as seconds.
    - String with a unit suffix: ``ms``, ``s``, ``m``, ``h``
      (e.g. ``"500ms"``, ``"60s"``, ``"2.5m"``, ``"1h"``).
    - A Jinja2 template that renders to one of the above
      (e.g. ``"{{ workflow.input.poll_interval_seconds }}s"``).

    The resolved duration must be greater than 0 and no more than 24h.
    Templated durations defer literal validation to runtime.
    """

    reason: str | None = None
    """Optional human-readable reason shown in the dashboard for ``type='wait'`` steps."""

    value: str | None = None
    """Jinja2 expression bound into context (required for single-binding 'set' type).

    The rendered string is auto-coerced to a typed value (see ``output_type``).
    The result is stored under ``<agent_name>.output``.

    Example::

        value: "{{ workflow.input.org }}/{{ workflow.input.repo }}"
    """

    values: dict[str, str] | None = None
    """Named Jinja2 expressions bound into context (for multi-binding 'set' type).

    Each value is rendered against the *original* pre-step context — bindings
    cannot reference one another within the same step. Chain multiple ``set``
    steps if you need ordered dependencies.

    Each binding is auto-coerced to a typed value (see ``output_type`` for the
    detection rules). The result is stored as a dict under
    ``<agent_name>.output.<key>``.

    Example::

        values:
          is_breaking: "{{ research.output.severity in ['high', 'critical'] }}"
          target_branch: "{{ workflow.input.branch or 'main' }}"
    """

    output_type: (
        Literal["auto", "string", "number", "integer", "boolean", "list", "dict"] | None
    ) = None
    """Override type detection for a single-binding 'set' step.

    Only valid with ``value:``. For ``values:``, every binding uses
    ``auto`` detection; per-key ``output_type`` is not supported.

    - ``auto`` / unset: render the template and run ``yaml.safe_load`` on the
      result; fall back to the raw string on parse failure. Empty/whitespace-only
      rendered strings become ``""`` (not ``None``).
    - ``string``: keep the raw rendered string.
    - ``number``: try ``int`` then ``float``; raise on failure.
    - ``integer``: ``int``; raise on failure.
    - ``boolean``: case-insensitive ``true``/``false``/``1``/``0``/``yes``/``no``.
    - ``list`` / ``dict``: parse via YAML and assert the type.
    """

    workflow: str | None = None
    """Path to sub-workflow YAML file (required for type='workflow').

    The path is resolved relative to the parent workflow file.
    Sub-workflows run as black boxes — their internal agents are not
    visible to the parent workflow.

    Example:
        workflow: ./research-pipeline.yaml
    """

    input_mapping: dict[str, str] | None = None
    """Optional mapping of sub-workflow input names to Jinja2 expressions.

    Each key is a sub-workflow input parameter name. Each value is a Jinja2
    template expression evaluated against the parent workflow's context.

    When present, the rendered values are passed as the sub-workflow's inputs
    instead of forwarding the parent's workflow.input.* values.

    Only valid for type='workflow' agents.

    Example::

        input_mapping:
          work_item_id: "{{ task_manager.output.current_issue_id }}"
          title: "{{ task_manager.output.current_issue_title }}"
    """

    max_depth: int | None = Field(None, ge=1, le=10)
    """Per-agent sub-workflow depth limit.

    Overrides the global MAX_SUBWORKFLOW_DEPTH (10) with a tighter bound.
    Only valid for type='workflow' agents. Useful for self-referential
    workflows to set an explicit recursion limit.

    Example::

        max_depth: 3  # Allow at most 3 levels of recursion
    """

    timeout_seconds: float | None = Field(None, ge=1.0)
    """Hard wall-clock timeout for this agent's execution in seconds.

    When set, the engine wraps the entire agent execution in
    ``asyncio.wait_for()``. If exceeded, raises ``AgentTimeoutError``
    which is handled by existing error semantics (``fail_fast``,
    ``continue_on_error``).

    The effective timeout is ``min(timeout_seconds, remaining_workflow_timeout)``
    so agent timeouts never exceed the workflow-level limit.

    Only applies to provider-backed agents (not script, human_gate,
    or workflow types). This is a hard cancellation — unlike
    ``max_session_seconds`` which checks between provider iterations.

    Because this is a hard cancellation, in-flight provider sessions,
    MCP tool calls, and HTTP connections receive ``CancelledError``
    mid-flight and may not get a clean shutdown. External state (e.g.,
    partially-written files, open MCP tool handles) may be left
    inconsistent.

    Note: Agent-level timeouts are non-retryable. The retry policy
    operates inside the provider and is cancelled along with the agent.

    Example::

        timeout_seconds: 120  # Cancel agent after 2 minutes
    """

    max_session_seconds: float | None = Field(None, ge=1.0)
    """Maximum wall-clock duration for this agent's session in seconds.

    Overrides the workflow-level runtime.max_session_seconds for this agent.
    Only applies to provider-backed agents (not script or human_gate).

    Example: A source-gathering agent that should finish in ~60s can set
    max_session_seconds: 60 instead of using the default timeout.
    """

    max_agent_iterations: int | None = Field(None, ge=1, le=500)
    """Maximum tool-use iterations for this agent execution.

    Overrides the workflow-level runtime.max_agent_iterations for this agent.
    Only applies to provider-backed agents (not script or human_gate).

    Example: A complex coding agent that needs many tool calls can set
    max_agent_iterations: 200 instead of using the default limit.
    """

    retry: RetryPolicy | None = None
    """Per-agent retry policy for transient failures.

    When set, the provider wraps agent execution in a retry loop with
    the specified backoff strategy. Only applies to provider-backed agents
    (not script or human_gate).

    Example YAML::

        retry:
          max_attempts: 3
          backoff: exponential
          delay_seconds: 2
          retry_on:
            - provider_error
            - timeout
    """

    dialog: DialogConfig | None = None
    """Optional dialog mode configuration.

    When set, enables this agent to conditionally pause after execution
    and enter a free-form conversation with the user. A lightweight
    evaluator LLM call uses the trigger_prompt to decide whether dialog
    should be triggered based on the agent's output.

    Only applies to provider-backed agents (type='agent' or None).

    Example YAML::

        dialog:
          trigger_prompt: |
            Enter dialog if the agent is uncertain about the user's
            intent or needs clarification on ambiguous requirements.
    """

    reasoning: ReasoningConfig | None = None
    """Optional reasoning / extended-thinking effort for this agent.

    When set, the provider configures its reasoning capability:

    - Copilot: passes ``reasoning_effort`` to ``create_session``.
    - Claude: enables ``thinking`` with a budget mapped from the effort
      level (low=2k, medium=8k, high=16k, xhigh=32k tokens).

    Falls back to ``runtime.default_reasoning_effort`` when unset.

    Only applies to provider-backed agents (type='agent' or None).

    Example YAML::

        reasoning:
          effort: high
    """

    validator: ValidatorConfig | None = None
    """Optional semantic output validation with retry-once.

    When set, the engine runs a second LLM call after this agent completes,
    checking the output against ``validator.criteria``. On failure the
    primary agent is re-run once with the validator's feedback appended.

    Distinct from ``retry:`` (transient failures, same prompt) and
    ``output:`` (shape validation). Only applies to provider-backed agents
    (type='agent' or None). Works in the main loop, parallel groups, and
    for-each loops.

    Example YAML::

        validator:
          criteria: |
            Verify every issue has an actionable suggestion and no
            function names are fabricated.
          max_retries: 1
    """

    status: Literal["success", "failed"] | None = None
    """Outcome status for ``type: terminate`` steps.

    ``success`` ends the workflow cleanly (exit code 0, dashboard ✅,
    ``workflow_completed`` event with ``is_explicit: true``). ``failed``
    ends the workflow as an explicit error (non-zero exit code, dashboard
    ❌, ``workflow_failed`` event with ``is_explicit: true``). Required
    for ``type: terminate``; forbidden on all other step types.

    Example YAML::

        type: terminate
        status: failed
        reason: "Upstream service returned unprocessable data"
    """

    reason: str | None = None
    """Termination reason for ``type: terminate`` steps (Jinja2-rendered).

    Surfaced in the ``workflow_completed`` / ``workflow_failed`` event as
    ``termination_reason`` and stored in the step's context entry. Required
    for ``type: terminate``; forbidden on all other step types.

    Supports Jinja2 templating against accumulated context.

    Example YAML::

        reason: "{{ precheck.output.reason }}"
    """

    output_template: dict[str, str] | None = None
    """Optional final-output mapping for ``type: terminate`` steps.

    When present, *replaces* the workflow-level ``output:`` mapping for
    this termination path. Each value is a Jinja2 expression evaluated
    against the accumulated context (including the terminate step's own
    ``status`` / ``reason``). When omitted, the workflow-level ``output:``
    mapping is rendered as usual.

    Each rendered value is then passed through the engine's JSON-coercion
    helper before being placed in the final output dict: literal strings
    ``"true"`` / ``"false"`` become Python booleans, numeric strings become
    ``int`` / ``float``, and strings that parse as JSON objects/arrays are
    deserialised. This matches the behaviour of workflow-level ``output:``
    and route output transforms, but it means the example below produces
    ``{"aborted": True, "stage": "precheck", ...}`` — not all-string values.
    Quote with backslashes if you genuinely want the literal text ``"true"``.

    Forbidden on all step types other than ``terminate``.

    Example YAML::

        output_template:
          aborted: "true"            # rendered to Python True
          stage: precheck
          reason: "{{ precheck.output.reason }}"
    """

    @field_validator("timeout")
    @classmethod
    def validate_timeout(cls, v: int | None) -> int | None:
        """Ensure timeout is positive if set."""
        if v is not None and v <= 0:
            raise ValueError("timeout must be a positive integer")
        return v

    @field_validator("duration", mode="before")
    @classmethod
    def reject_bool_duration(cls, v: Any) -> Any:
        """Reject boolean values for ``duration`` before Pydantic coerces them to int.

        Pydantic v2 coerces ``True``/``False`` to ``1``/``0`` when the union
        accepts ``int``. Catch it pre-coercion so a YAML ``duration: true`` is
        rejected with a clear message instead of silently becoming a 1-second
        wait.
        """
        if isinstance(v, bool):
            raise ValueError(f"duration must be a number or duration string, not boolean: {v!r}")
        return v

    @model_validator(mode="after")
    def validate_agent_type(self) -> AgentDef:
        """Ensure agent has required fields for its type."""
        # Fields exclusive to ``type: terminate`` — reject if set on any
        # other type. This is enforced before the per-type branches so the
        # error message clearly names the conflict.
        #
        # NOTE: ``reason`` is intentionally NOT in this list because it is
        # shared with ``type: wait`` (which uses it as an optional dashboard
        # label, vs. terminate's required Jinja2-rendered message). The wait
        # PR's cross-rejection block at the end of this method enforces
        # "not allowed on anything except wait OR terminate" for ``reason``.
        if self.type != "terminate":
            for field_name in ("status", "output_template"):
                if getattr(self, field_name) is not None:
                    raise ValueError(
                        f"'{self.type or 'agent'}' agents cannot have '{field_name}' "
                        "(only 'terminate' agents support this field)"
                    )

        # Field exclusive to ``type: script`` — reject if set on any other
        # type. No per-type branch below inspects ``stdin``, so this single
        # guard is the sole rejection path for every non-script type. It
        # mirrors the terminate-exclusive guard above so the message names the
        # conflict; being a standalone guard (rather than a per-branch check)
        # it also covers ``agent`` / ``human_gate``, which have no
        # ``command``/``args`` branch.
        if self.type != "script" and self.stdin is not None:
            raise ValueError(
                f"'{self.type or 'agent'}' agents cannot have 'stdin' "
                "(only 'script' agents support this field)"
            )

        if self.type == "human_gate":
            if not self.options:
                raise ValueError("human_gate agents require 'options'")
            if not self.prompt:
                raise ValueError("human_gate agents require 'prompt'")
            if self.input_mapping is not None:
                raise ValueError("human_gate agents cannot have 'input_mapping'")
            if self.dialog is not None:
                raise ValueError("human_gate agents cannot have 'dialog'")
            if self.validator is not None:
                raise ValueError("human_gate agents cannot have 'validator'")
            if self.max_depth is not None:
                raise ValueError("human_gate agents cannot have 'max_depth'")
            if self.reasoning is not None:
                raise ValueError("human_gate agents cannot have 'reasoning'")
            if self.context_tier is not None:
                raise ValueError("human_gate agents cannot have 'context_tier'")
            if self.timeout_seconds is not None:
                raise ValueError("human_gate agents cannot have 'timeout_seconds'")
            if self.value is not None:
                raise ValueError("human_gate agents cannot have 'value' (only 'set' agents do)")
            if self.values is not None:
                raise ValueError("human_gate agents cannot have 'values' (only 'set' agents do)")
            if self.output_type is not None:
                raise ValueError(
                    "human_gate agents cannot have 'output_type' (only 'set' agents do)"
                )
        elif self.type == "script":
            if not self.command:
                raise ValueError("script agents require 'command'")
            if self.prompt:
                raise ValueError("script agents cannot have 'prompt'")
            if self.provider:
                raise ValueError("script agents cannot have 'provider'")
            if self.model:
                raise ValueError("script agents cannot have 'model'")
            if self.tools is not None:
                raise ValueError("script agents cannot have 'tools'")
            if self.system_prompt:
                raise ValueError("script agents cannot have 'system_prompt'")
            if self.options:
                raise ValueError("script agents cannot have 'options'")
            if self.max_session_seconds:
                raise ValueError("script agents cannot have 'max_session_seconds'")
            if self.max_agent_iterations is not None:
                raise ValueError("script agents cannot have 'max_agent_iterations'")
            if self.retry is not None:
                raise ValueError("script agents cannot have 'retry'")
            if self.input_mapping is not None:
                raise ValueError("script agents cannot have 'input_mapping'")
            if self.dialog is not None:
                raise ValueError("script agents cannot have 'dialog'")
            if self.validator is not None:
                raise ValueError("script agents cannot have 'validator'")
            if self.max_depth is not None:
                raise ValueError("script agents cannot have 'max_depth'")
            if self.reasoning is not None:
                raise ValueError("script agents cannot have 'reasoning'")
            if self.context_tier is not None:
                raise ValueError("script agents cannot have 'context_tier'")
            if self.timeout_seconds is not None:
                raise ValueError(
                    "script agents cannot have 'timeout_seconds' "
                    "(use 'timeout' for script-specific timeouts)"
                )
            if self.value is not None:
                raise ValueError("script agents cannot have 'value' (only 'set' agents do)")
            if self.values is not None:
                raise ValueError("script agents cannot have 'values' (only 'set' agents do)")
            if self.output_type is not None:
                raise ValueError("script agents cannot have 'output_type' (only 'set' agents do)")
        elif self.type == "workflow":
            if not self.workflow:
                raise ValueError("workflow agents require 'workflow' path")
            if self.prompt:
                raise ValueError("workflow agents cannot have 'prompt'")
            if self.provider:
                raise ValueError("workflow agents cannot have 'provider'")
            if self.model:
                raise ValueError("workflow agents cannot have 'model'")
            if self.tools is not None:
                raise ValueError("workflow agents cannot have 'tools'")
            if self.system_prompt:
                raise ValueError("workflow agents cannot have 'system_prompt'")
            if self.options:
                raise ValueError("workflow agents cannot have 'options'")
            if self.command:
                raise ValueError("workflow agents cannot have 'command'")
            if self.max_session_seconds:
                raise ValueError("workflow agents cannot have 'max_session_seconds'")
            if self.max_agent_iterations is not None:
                raise ValueError("workflow agents cannot have 'max_agent_iterations'")
            if self.retry is not None:
                raise ValueError("workflow agents cannot have 'retry'")
            if self.dialog is not None:
                raise ValueError("workflow agents cannot have 'dialog'")
            if self.validator is not None:
                raise ValueError("workflow agents cannot have 'validator'")
            if self.timeout_seconds is not None:
                raise ValueError("workflow agents cannot have 'timeout_seconds'")
            if self.value is not None:
                raise ValueError("workflow agents cannot have 'value' (only 'set' agents do)")
            if self.values is not None:
                raise ValueError("workflow agents cannot have 'values' (only 'set' agents do)")
            if self.output_type is not None:
                raise ValueError("workflow agents cannot have 'output_type' (only 'set' agents do)")
        elif self.type == "wait":
            if self.duration is None:
                raise ValueError("wait agents require 'duration'")
            if self.prompt:
                raise ValueError("wait agents cannot have 'prompt'")
            if self.provider:
                raise ValueError("wait agents cannot have 'provider'")
            if self.model:
                raise ValueError("wait agents cannot have 'model'")
            if self.tools is not None:
                raise ValueError("wait agents cannot have 'tools'")
            if self.system_prompt:
                raise ValueError("wait agents cannot have 'system_prompt'")
            if self.options:
                raise ValueError("wait agents cannot have 'options'")
            if self.command:
                raise ValueError("wait agents cannot have 'command'")
            if self.args:
                raise ValueError("wait agents cannot have 'args'")
            if self.env:
                raise ValueError("wait agents cannot have 'env'")
            if self.working_dir:
                raise ValueError("wait agents cannot have 'working_dir'")
            if self.timeout is not None:
                raise ValueError("wait agents cannot have 'timeout'")
            if self.workflow:
                raise ValueError("wait agents cannot have 'workflow'")
            if self.input_mapping is not None:
                raise ValueError("wait agents cannot have 'input_mapping'")
            if self.max_depth is not None:
                raise ValueError("wait agents cannot have 'max_depth'")
            if self.max_session_seconds:
                raise ValueError("wait agents cannot have 'max_session_seconds'")
            if self.max_agent_iterations is not None:
                raise ValueError("wait agents cannot have 'max_agent_iterations'")
            if self.retry is not None:
                raise ValueError("wait agents cannot have 'retry'")
            if self.dialog is not None:
                raise ValueError("wait agents cannot have 'dialog'")
            if self.validator is not None:
                raise ValueError("wait agents cannot have 'validator'")
            if self.reasoning is not None:
                raise ValueError("wait agents cannot have 'reasoning'")
            if self.context_tier is not None:
                raise ValueError("wait agents cannot have 'context_tier'")
            if self.timeout_seconds is not None:
                raise ValueError("wait agents cannot have 'timeout_seconds'")
            if self.output is not None:
                raise ValueError(
                    "wait agents cannot have 'output' (output is fixed: {'waited_seconds': float})"
                )
            if self.value is not None:
                raise ValueError("wait agents cannot have 'value' (only 'set' agents do)")
            if self.values is not None:
                raise ValueError("wait agents cannot have 'values' (only 'set' agents do)")
            if self.output_type is not None:
                raise ValueError("wait agents cannot have 'output_type' (only 'set' agents do)")
            self._validate_wait_duration()
        elif self.type == "set":
            if (self.value is None) == (self.values is None):
                raise ValueError("set agents require exactly one of 'value' or 'values'")
            if self.values is not None and self.output_type is not None:
                raise ValueError(
                    "set agents with 'values:' cannot have 'output_type' "
                    "(it only applies to single 'value:'; per-key typing is not yet supported)"
                )
            if self.prompt:
                raise ValueError("set agents cannot have 'prompt'")
            if self.provider:
                raise ValueError("set agents cannot have 'provider'")
            if self.model:
                raise ValueError("set agents cannot have 'model'")
            if self.tools is not None:
                raise ValueError("set agents cannot have 'tools'")
            if self.system_prompt:
                raise ValueError("set agents cannot have 'system_prompt'")
            if self.options:
                raise ValueError("set agents cannot have 'options'")
            if self.command:
                raise ValueError("set agents cannot have 'command'")
            if self.args:
                raise ValueError("set agents cannot have 'args'")
            if self.env:
                raise ValueError("set agents cannot have 'env'")
            if self.working_dir:
                raise ValueError("set agents cannot have 'working_dir'")
            if self.timeout is not None:
                raise ValueError("set agents cannot have 'timeout'")
            if self.workflow:
                raise ValueError("set agents cannot have 'workflow'")
            if self.input_mapping is not None:
                raise ValueError("set agents cannot have 'input_mapping'")
            if self.max_depth is not None:
                raise ValueError("set agents cannot have 'max_depth'")
            if self.max_session_seconds is not None:
                raise ValueError("set agents cannot have 'max_session_seconds'")
            if self.max_agent_iterations is not None:
                raise ValueError("set agents cannot have 'max_agent_iterations'")
            if self.retry is not None:
                raise ValueError("set agents cannot have 'retry'")
            if self.dialog is not None:
                raise ValueError("set agents cannot have 'dialog'")
            if self.validator is not None:
                raise ValueError("set agents cannot have 'validator'")
            if self.reasoning is not None:
                raise ValueError("set agents cannot have 'reasoning'")
            if self.context_tier is not None:
                raise ValueError("set agents cannot have 'context_tier'")
            if self.timeout_seconds is not None:
                raise ValueError("set agents cannot have 'timeout_seconds'")
            if self.duration is not None:
                raise ValueError("set agents cannot have 'duration' (only 'wait' agents do)")
        elif self.type == "terminate":
            # Required fields
            if self.status is None:
                raise ValueError(
                    "terminate agents require 'status' (must be 'success' or 'failed')"
                )
            if not self.reason or not self.reason.strip():
                raise ValueError("terminate agents require a non-empty 'reason'")
            # Routing and per-step machinery are meaningless on a terminal
            # step — the engine ends the workflow as soon as it dispatches.
            if self.routes:
                raise ValueError(
                    "terminate agents cannot have 'routes' "
                    "(reaching a terminate step ends the workflow immediately)"
                )
            if self.tools is not None:
                raise ValueError("terminate agents cannot have 'tools'")
            if self.output is not None:
                raise ValueError(
                    "terminate agents cannot have 'output' "
                    "(use 'output_template' to override the workflow's final output)"
                )
            if self.prompt:
                raise ValueError("terminate agents cannot have 'prompt'")
            if self.model:
                raise ValueError("terminate agents cannot have 'model'")
            if self.provider:
                raise ValueError("terminate agents cannot have 'provider'")
            if self.system_prompt:
                raise ValueError("terminate agents cannot have 'system_prompt'")
            if self.command:
                raise ValueError("terminate agents cannot have 'command'")
            if self.args:
                raise ValueError("terminate agents cannot have 'args'")
            if self.env:
                raise ValueError("terminate agents cannot have 'env'")
            if self.working_dir:
                raise ValueError("terminate agents cannot have 'working_dir'")
            if self.timeout is not None:
                raise ValueError("terminate agents cannot have 'timeout'")
            if self.timeout_seconds is not None:
                raise ValueError("terminate agents cannot have 'timeout_seconds'")
            if self.max_session_seconds is not None:
                raise ValueError("terminate agents cannot have 'max_session_seconds'")
            if self.max_agent_iterations is not None:
                raise ValueError("terminate agents cannot have 'max_agent_iterations'")
            if self.max_depth is not None:
                raise ValueError("terminate agents cannot have 'max_depth'")
            if self.retry is not None:
                raise ValueError("terminate agents cannot have 'retry'")
            if self.dialog is not None:
                raise ValueError("terminate agents cannot have 'dialog'")
            if self.validator is not None:
                raise ValueError("terminate agents cannot have 'validator'")
            if self.reasoning is not None:
                raise ValueError("terminate agents cannot have 'reasoning'")
            if self.context_tier is not None:
                raise ValueError("terminate agents cannot have 'context_tier'")
            if self.workflow:
                raise ValueError("terminate agents cannot have 'workflow'")
            if self.input_mapping is not None:
                raise ValueError("terminate agents cannot have 'input_mapping'")
            if self.options:
                raise ValueError("terminate agents cannot have 'options'")
            # Cross-rejection with sibling step types: terminate has its own
            # `reason` so we do NOT reject it (the `if self.type not in ...`
            # block at the bottom of this method handles the
            # other-type-rejection for `reason`). But these are exclusive to
            # other step types and must not leak in.
            if self.value is not None:
                raise ValueError("terminate agents cannot have 'value' (only 'set' agents do)")
            if self.values is not None:
                raise ValueError("terminate agents cannot have 'values' (only 'set' agents do)")
            if self.output_type is not None:
                raise ValueError(
                    "terminate agents cannot have 'output_type' (only 'set' agents do)"
                )
            if self.duration is not None:
                raise ValueError("terminate agents cannot have 'duration' (only 'wait' agents do)")
        else:
            # Regular agent or human_gate — input_mapping is not valid
            if self.input_mapping is not None:
                raise ValueError(
                    f"'{self.type or 'agent'}' agents cannot have 'input_mapping' "
                    "(only workflow agents support input_mapping)"
                )
            if self.max_depth is not None:
                raise ValueError(
                    f"'{self.type or 'agent'}' agents cannot have 'max_depth' "
                    "(only workflow agents support max_depth)"
                )
            if self.value is not None:
                raise ValueError(
                    f"'{self.type or 'agent'}' agents cannot have 'value' "
                    "(only 'set' agents support value)"
                )
            if self.values is not None:
                raise ValueError(
                    f"'{self.type or 'agent'}' agents cannot have 'values' "
                    "(only 'set' agents support values)"
                )
            if self.output_type is not None:
                raise ValueError(
                    f"'{self.type or 'agent'}' agents cannot have 'output_type' "
                    "(only 'set' agents support output_type)"
                )
        if self.type == "workflow" and self.reasoning is not None:
            raise ValueError("workflow agents cannot have 'reasoning'")
        if self.type == "workflow" and self.context_tier is not None:
            raise ValueError("workflow agents cannot have 'context_tier'")

        # Wait-only fields are forbidden on every other type. ``reason`` is
        # shared with ``type: terminate`` (which has its own required-non-
        # empty semantics enforced earlier), so it is rejected on every
        # non-wait, non-terminate type with a message naming both owners.
        if self.type != "wait":
            if self.duration is not None:
                raise ValueError(
                    f"'{self.type or 'agent'}' agents cannot have 'duration' "
                    "(only wait agents support duration)"
                )
            if self.type != "terminate" and self.reason is not None:
                raise ValueError(
                    f"'{self.type or 'agent'}' agents cannot have 'reason' "
                    "(only 'terminate' and 'wait' agents support this field)"
                )
        return self

    def _validate_wait_duration(self) -> None:
        """Validate ``duration`` for a ``wait`` agent.

        Templated durations (containing ``{{``) defer all literal
        validation to runtime; for everything else we parse the value
        and enforce ``0 < d <= MAX_WAIT_DURATION_SECONDS``.

        Note: Booleans are already rejected pre-coercion by the
        :meth:`reject_bool_duration` ``mode="before"`` field validator,
        so this method never sees ``True``/``False``.
        """
        value = self.duration

        if isinstance(value, str) and "{{" in value:
            return

        try:
            seconds = parse_duration(value)  # type: ignore[arg-type]
        except ValueError as exc:
            raise ValueError(f"wait duration is invalid: {exc}") from exc

        if seconds <= 0:
            raise ValueError(f"wait duration must be > 0 seconds (got {seconds!r})")
        if seconds > MAX_WAIT_DURATION_SECONDS:
            raise ValueError(
                f"wait duration {seconds!r}s exceeds the 24h cap "
                f"({MAX_WAIT_DURATION_SECONDS}s); reconsider using "
                "'limits.timeout_seconds' instead"
            )


class MCPServerDef(BaseModel):
    """Definition for an MCP server."""

    type: Literal["stdio", "http", "sse"] = "stdio"
    """Type of MCP server: 'stdio' for command-based, 'http' or 'sse' for remote."""

    command: str | None = None
    """Command to run the MCP server (required for stdio type)."""

    args: list[str] = Field(default_factory=list)
    """Command-line arguments for the MCP server (stdio type only)."""

    env: dict[str, str] = Field(default_factory=dict)
    """Environment variables for the MCP server (stdio type only).

    Supports ${VAR} and ${VAR:-default} syntax for environment variable
    interpolation at runtime.

    Note: With the Claude provider, env vars are passed correctly to MCP
    server subprocesses via the MCP SDK. However, the Copilot provider
    has a known bug where env vars are not passed to MCP servers.
    See: https://github.com/github/copilot-sdk/issues/163
    """

    url: str | None = None
    """URL for the MCP server (required for http/sse type)."""

    headers: dict[str, str] = Field(default_factory=dict)
    """HTTP headers for the MCP server (http/sse type only)."""

    timeout: int | None = None
    """Timeout in milliseconds for the MCP server."""

    tools: list[str] = Field(default_factory=lambda: ["*"])
    """List of tools to enable. ["*"] means all tools."""

    @model_validator(mode="after")
    def validate_type_requirements(self) -> MCPServerDef:
        """Ensure required fields are set based on type."""
        if self.type == "stdio" and not self.command:
            raise ValueError("'command' is required for stdio type MCP servers")
        if self.type in ("http", "sse") and not self.url:
            raise ValueError("'url' is required for http/sse type MCP servers")
        return self


class AzureProviderOptions(BaseModel):
    """Azure-specific provider options forwarded to the Copilot SDK.

    Mirrors :class:`copilot.session.AzureProviderOptions`. Currently only
    ``api_version`` is recognized; additional fields the SDK adds in the
    future can be enumerated here.
    """

    model_config = ConfigDict(extra="forbid")

    api_version: str | None = None
    """Azure OpenAI API version (e.g. ``"2024-10-21"``). Optional; the SDK
    falls back to its own default when unset."""


class ProviderSettings(BaseModel):
    """Structured provider configuration for ``runtime.provider``.

    Supports two YAML shapes via :meth:`RuntimeConfig._coerce_provider`:

    - String shorthand: ``provider: copilot`` (equivalent to
      ``provider: {name: copilot}``).
    - Object form: enables routing the Copilot SDK at custom endpoints
      such as Azure OpenAI, Ollama, vLLM, LM Studio, or any other
      OpenAI-compatible server. Object fields beyond ``name`` are
      currently supported only for ``name: copilot``; they are forwarded
      verbatim to ``copilot.client.create_session(provider=...)``.

    When any field beyond ``name`` is set, the Copilot provider activates
    "custom routing" mode and fills any missing field from environment
    variables (see :meth:`has_custom_routing`).

    The model is frozen after construction (``frozen=True``) because
    custom routing is set-once at config load. This avoids the
    Pydantic gotcha where ``model_validator(mode="after")``
    cross-field invariants do not re-fire on per-attribute assignment
    even with ``validate_assignment=True``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Literal["copilot", "openai-agents", "claude", "claude-agent-sdk", "hermes"] = "copilot"
    """SDK provider to use for agent execution."""

    type: Literal["openai", "azure", "anthropic"] | None = None
    """Wire-format dialect for the upstream endpoint. Copilot-only.

    Defaults to ``"openai"`` at activation time when ``base_url`` is set
    but ``type`` is not.
    """

    wire_api: Literal["completions", "responses"] | None = None
    """OpenAI wire API variant. Copilot-only.

    ``"completions"`` for the classic ``/v1/chat/completions`` shape used by
    Ollama, vLLM, LM Studio, and the legacy OpenAI API. ``"responses"`` for
    the newer OpenAI Responses API.
    """

    base_url: str | None = None
    """Endpoint base URL (e.g. ``http://localhost:11434/v1``)."""

    api_key: SecretStr | None = None
    """API key for the endpoint. Prefer ``${OPENAI_API_KEY}`` interpolation
    in YAML so the literal value never lands in ``workflow_started`` events
    or checkpoints."""

    bearer_token: SecretStr | None = None
    """Bearer token. Takes precedence over ``api_key`` when both are set.
    Copilot-only."""

    auth_token: SecretStr | None = None
    """Bearer token for OAuth / gateway authentication. Claude-only.

    Sent as ``Authorization: Bearer <token>`` by the Anthropic SDK instead
    of the usual ``x-api-key`` header. Use for Databricks AI Gateway,
    LiteLLM proxies, or any endpoint that expects a bearer token.

    Falls back to ``ANTHROPIC_AUTH_TOKEN`` env var when not set in YAML.

    Example::

        provider:
          name: claude
          base_url: https://my-gateway.example.com/api/v1
          auth_token: ${DATABRICKS_TOKEN}
    """

    headers: dict[str, str] | None = None
    """Extra HTTP headers to send with every request. Copilot-only."""

    azure: AzureProviderOptions | None = None
    """Azure-specific options (e.g. ``api_version``). Requires
    ``type: azure``. Copilot-only."""

    hermes_home: str | None = None
    """Path to a Hermes home directory (profile). Hermes-only.

    When set, the Hermes provider loads its config (soul, memory, toolsets)
    from this path instead of the default ``~/.hermes``. Supports
    ``${ENV_VAR}`` interpolation.

    Example:
        hermes_home: ~/.hermes-research
    """

    hermes_toolsets: list[str] | None = None
    """Hermes toolset names to enable for all agents. Hermes-only.

    When set, restricts which Hermes toolsets are available during agent
    execution. ``None`` (default) = Hermes uses all available toolsets.
    Empty list = no tools at all.

    Example:
        hermes_toolsets: [filesystem, web]
    """

    hermes_skip_memory: bool | None = None
    """Skip loading Hermes memory files during agent initialization. Hermes-only.

    ``None`` (default) = the hermes-agent library default applies (memory is loaded).
    Set to ``True`` to explicitly disable memory for stateless workflows.
    """

    hermes_skip_context_files: bool | None = None
    """Skip loading Hermes context/soul files during agent initialization. Hermes-only.

    ``None`` (default) = the hermes-agent library default applies (context files
    including SOUL.md are loaded, preserving the agent's persona).
    Set to ``True`` to explicitly disable context file loading.
    """

    @model_validator(mode="after")
    def _check_field_compatibility(self) -> ProviderSettings:
        copilot_only_fields = {
            "type": self.type,
            "wire_api": self.wire_api,
            "bearer_token": self.bearer_token,
            "headers": self.headers,
            "azure": self.azure,
        }
        claude_only_fields = {
            "auth_token": self.auth_token,
        }
        if self.name != "copilot":
            extras = sorted(k for k, v in copilot_only_fields.items() if v is not None)
            if extras:
                raise ValueError(
                    f"Provider fields {extras} are only supported when name='copilot'. "
                    "Structured provider config for other providers is not yet implemented."
                )
        if self.name not in ("copilot", "claude", "hermes") and (
            self.base_url is not None or self.api_key is not None
        ):
            raise ValueError(
                f"Structured provider config (base_url/api_key) for name='{self.name}' "
                "is not yet implemented; use environment variables for the underlying SDK."
            )
        if self.name != "claude":
            extras = sorted(k for k, v in claude_only_fields.items() if v is not None)
            if extras:
                raise ValueError(f"Provider fields {extras} are only supported when name='claude'.")

        if self.hermes_home is not None and self.name != "hermes":
            raise ValueError("'hermes_home' is only supported when name='hermes'.")

        if self.hermes_toolsets is not None and self.name != "hermes":
            raise ValueError("'hermes_toolsets' is only supported when name='hermes'.")

        if self.hermes_skip_memory is not None and self.name != "hermes":
            raise ValueError("'hermes_skip_memory' is only supported when name='hermes'.")

        if self.hermes_skip_context_files is not None and self.name != "hermes":
            raise ValueError("'hermes_skip_context_files' is only supported when name='hermes'.")

        if self.azure is not None and self.type != "azure":
            raise ValueError("'azure' options require type='azure'")

        # Reject empty containers and empty SecretStr — they activate
        # custom routing via has_custom_routing() but resolve to falsy
        # values in the resolver and would silently drop the entire
        # SDK provider kwarg.
        if self.headers is not None and len(self.headers) == 0:
            raise ValueError(
                "'headers' must contain at least one entry; remove the key to omit headers"
            )
        for secret_field, value in (
            ("api_key", self.api_key),
            ("bearer_token", self.bearer_token),
            ("auth_token", self.auth_token),
        ):
            if value is not None and value.get_secret_value() == "":
                raise ValueError(
                    f"'{secret_field}' is empty; remove the key or supply a value "
                    "(typo / unset env interpolation?)"
                )

        # Positive precondition: structured fields that only make sense
        # alongside an endpoint must not be the *only* thing set.
        # ``base_url`` may still come from an env-var fallback, so this
        # check is intentionally narrow: ``wire_api`` / ``type`` /
        # ``headers`` / ``azure`` alone (with no other field) is almost
        # certainly a misconfiguration.
        if self.base_url is None and self.api_key is None and self.bearer_token is None:
            anchorless = sorted(
                k
                for k in ("type", "wire_api", "headers", "azure")
                if copilot_only_fields.get(k) is not None
            )
            if anchorless:
                raise ValueError(
                    f"Provider fields {anchorless} require base_url, api_key, or "
                    "bearer_token to also be set (in YAML or via environment variables); "
                    "they cannot stand alone."
                )

        if self.azure is not None and self.azure.api_version is None:
            raise ValueError(
                "'azure' block is empty; either set azure.api_version or remove the block"
            )

        return self

    def has_custom_routing(self) -> bool:
        """Return True when YAML explicitly opted into custom routing.

        Custom routing is gated on at least one non-``name`` field being
        set. We never activate from ambient environment variables alone —
        that would silently divert default Copilot traffic based on
        unrelated shell state.
        """
        return any(
            value is not None
            for value in (
                self.type,
                self.wire_api,
                self.base_url,
                self.api_key,
                self.bearer_token,
                self.auth_token,
                self.headers,
                self.azure,
            )
        )

    @model_serializer(mode="wrap")
    def _serialize(self, nxt: Any) -> Any:
        """Collapse to bare string when only ``name`` is set.

        Preserves backward compatibility with the original
        ``provider: copilot`` YAML/JSON shape: a ``ProviderSettings`` with
        no custom routing round-trips as the plain string ``"copilot"``,
        not as ``{"name": "copilot"}``. Once any structured field is set,
        the full object is emitted.
        """
        if not self.has_custom_routing():
            return self.name
        return nxt(self)


class CheckpointConfig(BaseModel):
    """Periodic checkpoint configuration (issue #244).

    Opt-in automatic checkpointing at workflow step boundaries so a stalled or
    hard-killed long-running workflow can be resumed without an exception ever
    being raised. All triggers default to off — the existing failure-only
    checkpoint behavior is unchanged unless at least one trigger is set.

    Checkpoints are evaluated at each step boundary (after a step's output is
    committed to context, before the next step runs). There is no background
    wall-clock timer: the engine only commits recoverable state at step
    boundaries, so ``every_seconds`` is enforced as a throttle evaluated at
    those boundaries.
    """

    model_config = ConfigDict(extra="forbid")

    every_agent: bool = False
    """Save a checkpoint at every step boundary (after each agent, parallel
    group, for-each group, gate, script, set, wait, or sub-workflow step). When
    true it governs on its own and ``every_seconds`` is ignored (a save already
    fires at every boundary)."""

    every_seconds: int | None = Field(default=None, ge=1)
    """Minimum seconds between periodic checkpoints, evaluated at step
    boundaries.

    A checkpoint is saved at the first boundary reached after this many seconds
    have elapsed since the last checkpoint. ``None`` disables the time-based
    trigger. The first periodic checkpoint of a run fires at the first eligible
    boundary; the interval only throttles subsequent saves.

    Note: if a single step runs longer than this interval, no checkpoint fires
    during that step — the boundary checkpoint taken *before* the step started
    is the recovery point.
    """

    keep_last: int = Field(default=5, ge=1, le=100)
    """Number of recent periodic checkpoints to retain per run.

    Older periodic checkpoints for the same run are deleted after each save.
    Failure checkpoints are never rotated.
    """

    @property
    def is_enabled(self) -> bool:
        """Return True if any periodic checkpoint trigger is configured."""
        return self.every_agent or self.every_seconds is not None


class RuntimeConfig(BaseModel):
    """Provider and runtime configuration."""

    model_config = ConfigDict(validate_assignment=True)

    provider: ProviderSettings = Field(default_factory=ProviderSettings)
    """SDK provider configuration.

    Accepts either a string shorthand (``provider: copilot``) or a
    structured :class:`ProviderSettings` object. See
    :class:`ProviderSettings` for the full field reference and custom
    routing semantics.
    """

    @field_validator("provider", mode="before")
    @classmethod
    def _coerce_provider(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"name": value}
        return value

    default_model: str | None = None
    """Default model for agents that don't specify one."""

    mcp_servers: dict[str, MCPServerDef] = Field(default_factory=dict)
    """MCP server configurations keyed by server name."""

    temperature: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Controls randomness. Range: 0.0-1.0",
    )
    """Temperature parameter for models. Controls randomness in responses."""

    max_tokens: int | None = Field(
        None,
        ge=1,
        le=200000,
        description=(
            "Maximum OUTPUT tokens generated per response (NOT context window limit). "
            "Claude 4: max 8192 (Opus/Sonnet) or 4096 (Haiku). "
            "Context window: 200K tokens input+output combined (separate from this setting)"
        ),
    )
    """Maximum number of output tokens to generate per response.

    Note: This controls response length, NOT context window. Context trimming
    is handled separately by the workflow engine if needed.

    Claude 4 limits: Opus/Sonnet 8192, Haiku 4096.
    """

    timeout: float | None = Field(
        None,
        ge=1.0,
        description=(
            "Request timeout in seconds for each individual API call (NOT per-workflow). "
            "Default: 600s. Each agent execution gets its own timeout. "
            "For workflow-level timeout, use limits.timeout_seconds instead."
        ),
    )
    """Timeout for individual API requests (per-request, not per-workflow).

    This timeout applies to each agent execution independently. For example,
    if timeout=60 and a workflow has 3 agents, each agent gets 60 seconds.

    For workflow-level timeout enforcement, use `limits.timeout_seconds` instead,
    which limits the total wall-clock time for the entire workflow.
    """

    max_session_seconds: float | None = Field(None, ge=1.0)
    """Maximum wall-clock duration for agent sessions in seconds.

    Sets the default max_session_seconds for all agents.
    Individual agents can override this with their own max_session_seconds field.

    Default is None, which uses the provider's built-in default
    (Copilot: 1800s / 30 min, Claude: unlimited).
    Set a lower value for workflows where agents should finish quickly.
    """

    max_agent_iterations: int | None = Field(None, ge=1, le=500)
    """Maximum tool-use iterations per agent execution.

    Caps the number of tool-use roundtrips an agent can perform in a single
    execution. This prevents runaway tool loops.

    Default is None, which uses the provider's built-in default
    (Claude: 50, Copilot: unlimited).
    """

    default_reasoning_effort: ReasoningEffort | None = None
    """Workflow-wide default reasoning effort applied to provider-backed agents.

    Each agent may override with its own ``reasoning.effort``. Providers
    translate this into their native parameter:

    - Copilot: ``reasoning_effort`` on ``create_session``
    - Claude: ``thinking`` with budget mapped from effort level

    Validation happens at execute time. Claude rejects models that don't
    match the supported prefix list; Copilot consults the SDK's advertised
    ``supported_reasoning_efforts`` (when available) and otherwise allows
    the request through to the SDK.
    """

    checkpoint: CheckpointConfig = Field(default_factory=CheckpointConfig)
    """Periodic checkpoint configuration.

    Opt-in automatic checkpointing at step boundaries so stalled or killed
    long-running workflows stay resumable. Defaults to off (failure-only
    checkpoints). See :class:`CheckpointConfig`.
    """

    default_context_tier: ContextTier | None = None
    """Workflow-wide default context-window tier (Copilot provider only).

    Each agent may override with its own ``context_tier``. ``long_context``
    selects a model's long-context (e.g. 1M-token) window; ``default`` selects
    the standard tier; ``None`` sends no value.

    Only the Copilot provider forwards this (maps to the SDK's
    ``create_session`` ``context_tier`` param). Other providers ignore it.
    """


class WorkflowDef(BaseModel):
    """Top-level workflow configuration."""

    model_config = ConfigDict(extra="forbid")

    name: str
    """Unique workflow identifier."""

    description: str | None = None
    """Human-readable workflow description."""

    version: str | None = None
    """Semantic version string."""

    entry_point: str
    """Name of the first agent to execute."""

    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    """Provider and runtime settings."""

    input: dict[str, InputDef] = Field(default_factory=dict)
    """Workflow input parameter definitions."""

    context: ContextConfig = Field(default_factory=ContextConfig)
    """Context accumulation settings."""

    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    """Execution safety limits."""

    cost: CostConfig = Field(default_factory=CostConfig)
    """Cost tracking configuration."""

    hooks: HooksConfig | None = None
    """Lifecycle event hooks."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Arbitrary key-value metadata for external tooling (dashboards, trackers, etc.).

    Included verbatim in the ``workflow_started`` event so downstream
    consumers can use it for enrichment without parsing the YAML source.
    """

    instructions: list[str] = Field(default_factory=list)
    """Workspace instruction file contents or inline text.

    Each entry can be:
    - A ``!file`` tag reference (resolved by the YAML loader)
    - Inline text included as-is

    Instructions from all entries are concatenated and prepended to every
    agent's prompt as workspace context. Use this for self-contained
    workflows where the YAML lives alongside the code.

    For workflows distributed as skills (where the YAML lives far from
    the target repo), use the ``--workspace-instructions`` CLI flag
    instead for automatic discovery.

    Example::

        instructions:
          - !file ../AGENTS.md
          - "Always respond in English."
    """


class WorkflowConfig(BaseModel):
    """Complete workflow configuration file."""

    model_config = ConfigDict(extra="forbid")

    workflow: WorkflowDef
    """Workflow-level settings."""

    tools: list[str] = Field(default_factory=list)
    """Tools available to agents in this workflow."""

    agents: list[AgentDef]
    """Agent definitions."""

    parallel: list[ParallelGroup] = Field(default_factory=list)
    """Parallel execution group definitions."""

    for_each: list[ForEachDef] = Field(default_factory=list)
    """Dynamic parallel (for-each) group definitions."""

    output: dict[str, str] = Field(default_factory=dict)
    """Final output template expressions."""

    @model_validator(mode="after")
    def validate_references(self) -> WorkflowConfig:
        """Validate all agent references exist."""
        agent_names = {a.name for a in self.agents}
        parallel_names = {p.name for p in self.parallel}
        for_each_names = {f.name for f in self.for_each}

        # Validate entry_point exists
        all_names = agent_names | parallel_names | for_each_names
        if self.workflow.entry_point not in all_names:
            raise ValueError(
                f"entry_point '{self.workflow.entry_point}' not found in "
                f"agents, parallel groups, or for-each groups"
            )

        # Validate route targets exist
        for agent in self.agents:
            for route in agent.routes:
                if route.to != "$end" and route.to not in all_names:
                    raise ValueError(
                        f"Agent '{agent.name}' routes to unknown agent, "
                        f"parallel group, or for-each group '{route.to}'"
                    )

        # Validate parallel group agent references exist
        for parallel_group in self.parallel:
            for agent_name in parallel_group.agents:
                if agent_name not in agent_names:
                    raise ValueError(
                        f"Parallel group '{parallel_group.name}' "
                        f"references unknown agent '{agent_name}'"
                    )
            # Validate parallel group route targets
            for route in parallel_group.routes:
                if route.to != "$end" and route.to not in all_names:
                    raise ValueError(
                        f"Parallel group '{parallel_group.name}' "
                        f"routes to unknown target '{route.to}'"
                    )

        # Validate for-each group route targets and nested prohibition
        for for_each_group in self.for_each:
            # Check for nested for-each groups
            if for_each_group.agent.name in for_each_names:
                raise ValueError(
                    f"Nested for-each groups are not allowed. "
                    f"For-each group '{for_each_group.name}' references "
                    f"another for-each group '{for_each_group.agent.name}'"
                )

            # Validate for-each group route targets
            for route in for_each_group.routes:
                if route.to != "$end" and route.to not in all_names:
                    raise ValueError(
                        f"For-each group '{for_each_group.name}' "
                        f"routes to unknown target '{route.to}'"
                    )

        return self
