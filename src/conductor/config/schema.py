"""Pydantic models for workflow configuration.

This module defines all Pydantic models for validating and parsing
workflow YAML configuration files.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


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


class AgentDef(BaseModel):
    """Definition for a single agent in the workflow."""

    name: str
    """Unique identifier for this agent."""

    description: str | None = None
    """Human-readable description of agent's purpose."""

    type: Literal["agent", "human_gate", "script", "workflow"] | None = None
    """Agent type. Defaults to 'agent' if not specified."""

    provider: Literal["copilot", "claude"] | None = None
    """Provider override for this agent.

    If None (default), the agent uses the workflow.runtime.provider.
    When specified, this agent will use a different provider than
    the workflow default, enabling multi-provider workflows.

    Example:
        provider: claude  # Use Claude for this agent
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

    timeout: int | None = None
    """Per-script timeout in seconds."""

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

    @field_validator("timeout")
    @classmethod
    def validate_timeout(cls, v: int | None) -> int | None:
        """Ensure timeout is positive if set."""
        if v is not None and v <= 0:
            raise ValueError("timeout must be a positive integer")
        return v

    @model_validator(mode="after")
    def validate_agent_type(self) -> AgentDef:
        """Ensure agent has required fields for its type."""
        if self.type == "human_gate":
            if not self.options:
                raise ValueError("human_gate agents require 'options'")
            if not self.prompt:
                raise ValueError("human_gate agents require 'prompt'")
            if self.input_mapping is not None:
                raise ValueError("human_gate agents cannot have 'input_mapping'")
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
            if self.output:
                raise ValueError(
                    "script agents cannot have 'output' schema "
                    "(output is always stdout/stderr/exit_code)"
                )
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
        else:
            # Regular agent or human_gate — input_mapping is not valid
            if self.input_mapping is not None:
                raise ValueError(
                    f"'{self.type or 'agent'}' agents cannot have 'input_mapping' "
                    "(only workflow agents support input_mapping)"
                )
        return self


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


class RuntimeConfig(BaseModel):
    """Provider and runtime configuration."""

    provider: Literal["copilot", "openai-agents", "claude"] = "copilot"
    """SDK provider to use for agent execution."""

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


class WorkflowDef(BaseModel):
    """Top-level workflow configuration."""

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
