"""Pydantic models for workflow configuration.

This module defines all Pydantic models for validating and parsing
workflow YAML configuration files.
"""

from __future__ import annotations

import functools
from typing import Annotated, Any, Literal, get_args
from urllib.parse import urlparse

import regex
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    ValidationInfo,
    ValidatorFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from conductor.duration import parse_duration
from conductor.file_string import FileString
from conductor.providers.context_tier import ContextTier
from conductor.providers.reasoning import ReasoningEffort
from conductor.skills.discovery import DiscoverySource
from conductor.templating import is_jinja_template

BudgetMode = Literal["audit", "enforce"]
"""How the engine responds when a workflow cost budget is exceeded.

Shared between :class:`LimitsConfig` and :class:`conductor.engine.limits.LimitEnforcer`
so the literal type is defined in exactly one place.
"""

# Maximum allowed wait-step duration (24 hours). Anything longer almost
# certainly wants ``limits.timeout_seconds`` reconsidered first.
MAX_WAIT_DURATION_SECONDS = 24 * 60 * 60

# Wall-clock bound for a single pattern match. Model output is untrusted input
# and Python ``re`` has no timeout, so matching uses the third-party ``regex``
# engine which supports deadlines (and releases the GIL, so a pathological
# pattern cannot stall the event loop and neighboring parallel agents).
PATTERN_MATCH_TIMEOUT_SECONDS = 1.0


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


class McpConfig(BaseModel):
    """Per-workflow configuration for exposure as an MCP tool.

    Backs ``WorkflowDef.mcp`` (E6, DD4, FR11): a typed, validated block so a
    typo (``expse: false``) is a schema error rather than silently ignored
    ``metadata``. Every workflow is a candidate for exposure by default
    (``expose: True``); ``conductor mcp serve``'s ``--allow``/``--deny``
    flags outrank this block, which in turn outranks the default.
    """

    model_config = ConfigDict(extra="forbid")

    expose: bool = True
    """Whether this workflow is a candidate for MCP tool exposure."""

    mode: Literal["async", "sync", "auto"] = "async"
    """Invocation mode the MCP server should use for this workflow."""

    read_only: bool = False
    """Whether this workflow only reads state (no side effects)."""

    destructive: bool = False
    """Whether this workflow can destroy or irreversibly modify state."""

    estimated_minutes: int | None = None
    """Estimated wall-clock runtime in minutes, for client-side hints."""

    @field_validator("estimated_minutes")
    @classmethod
    def validate_estimated_minutes(cls, v: int | None) -> int | None:
        """Reject a non-positive estimate rather than silently accepting one."""
        if v is not None and v <= 0:
            raise ValueError("estimated_minutes must be positive when present")
        return v


class OutputField(BaseModel):
    """Schema for a single output field from an agent."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["string", "number", "boolean", "array", "object"]
    """The type of the output field."""

    description: str | None = None
    """Human-readable description of the output field."""

    items: OutputField | None = None
    """For array types, the schema of array items."""

    properties: dict[str, OutputField] | None = None
    """For object types, the schema of object properties."""

    enum: list[Any] | None = None
    """Allowed values for scalar types."""

    pattern: str | None = None
    """Regular expression pattern for string types."""

    minimum: int | float | None = None
    """Minimum value for number types."""

    maximum: int | float | None = None
    """Maximum value for number types."""

    minLength: int | None = None
    """Minimum length for string types."""

    maxLength: int | None = None
    """Maximum length for string types."""

    required: bool = True
    """Whether the field is required when used as an object property."""

    nullable: bool = False
    """Whether the field value may be null."""

    @functools.cached_property
    def compiled_pattern(self) -> Any:
        """Return a compiled regex pattern, or ``None`` when no pattern is set.

        Annotated as ``Any`` because the repo type checker (ty) does not yet
        read the ``regex`` package stubs; the runtime object is always a
        ``regex.Pattern`` or ``None``.
        """

        if self.pattern is None:
            return None
        return regex.compile(self.pattern)

    @model_validator(mode="after")
    def validate_type_specific_fields(self) -> OutputField:
        """Ensure type-specific fields are properly set and consistent."""
        if self.type == "array" and self.items is None:
            # Items are optional but recommended for arrays
            pass
        if self.type == "object" and self.properties is None:
            # Properties are optional but recommended for objects
            pass

        # String-only constraints.
        if self.type != "string":
            for field_name in ("pattern", "minLength", "maxLength"):
                value = getattr(self, field_name)
                if value is not None:
                    raise ValueError(f"{field_name} can only be set when type is 'string'")

        # Number-only constraints.
        if self.type != "number":
            for field_name in ("minimum", "maximum"):
                value = getattr(self, field_name)
                if value is not None:
                    raise ValueError(f"{field_name} can only be set when type is 'number'")

        # Enum validation.
        if self.enum is not None:
            if self.type in ("array", "object"):
                raise ValueError("enum can only be set for scalar types")

            if len(self.enum) == 0:
                raise ValueError("enum must contain at least one value")

            if any(value is None for value in self.enum):
                raise ValueError(
                    "enum cannot contain null; use nullable: true to allow null values"
                )

            type_checks = {
                "string": lambda x: isinstance(x, str),
                "number": lambda x: isinstance(x, int | float) and not isinstance(x, bool),
                "boolean": lambda x: isinstance(x, bool),
            }
            check = type_checks.get(self.type)
            if check is not None and not all(check(value) for value in self.enum):
                raise ValueError(f"enum values must match the declared type '{self.type}'")

        # String length validation.
        if self.minLength is not None and self.minLength < 0:
            raise ValueError("minLength must be non-negative")
        if self.maxLength is not None and self.maxLength < 0:
            raise ValueError("maxLength must be non-negative")
        if (
            self.minLength is not None
            and self.maxLength is not None
            and self.minLength > self.maxLength
        ):
            raise ValueError("minLength cannot be greater than maxLength")

        # Number range validation.
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum cannot be greater than maximum")

        # Pattern compilation. ``regex`` is a strict superset of the stdlib
        # ``re`` module, so every previously valid pattern still compiles.
        if self.pattern is not None:
            try:
                regex.compile(self.pattern)
            except regex.error as exc:
                raise ValueError(f"pattern is not a valid regular expression: {exc}") from exc

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


def validate_dotted_source(v: str) -> str:
    """Validate a dotted context reference (``agent_name.output.field``).

    Shared by ``ForEachDef.source`` and ``AgentDef.source`` so the two stay
    enforced identically — a reference that names the convention without
    inheriting its checks is the worst of both.

    Args:
        v: The dotted path to check.

    Returns:
        The path unchanged.

    Raises:
        ValueError: If the path has fewer than three parts or its first
            segment is not a valid identifier. This is a format check only;
            actual resolution happens at runtime.
    """
    parts = v.split(".")
    if len(parts) < 3:
        raise ValueError(
            f"Invalid source format: '{v}'. "
            f"Expected format: 'agent_name.output.field' (minimum 3 parts)"
        )
    if not parts[0].isidentifier():
        raise ValueError(f"Invalid agent name in source: '{parts[0]}' is not a valid identifier")
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

    multiline: bool = False
    """Whether the ``prompt_for`` input accepts multi-line text.

    Defaults to False so existing gates keep single-line behavior (Enter
    submits). When True, the terminal reads until a lone ``.`` or EOF and
    the dashboard renders a textarea where Enter inserts a newline.
    """


class QuestionDef(BaseModel):
    """One question in a ``type: questions`` node.

    A ``source:`` that resolves to plain strings is coerced into these with
    only ``text`` populated, so an agent emitting ``array of string`` needs no
    change to gain choices later.
    """

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    """Stable key for this question's answer. Defaults to ``q1``..``qN``.

    Set it explicitly when downstream templates reference a specific answer,
    so inserting a question upstream doesn't renumber the keys under them.
    """

    text: str
    """The question, rendered as a Jinja2 template."""

    hint: str | None = None
    """Optional clarifying text shown beneath the question."""

    choices: list[str] | None = None
    """Suggested answers to offer as selectable options.

    Lets an agent propose candidate answers rather than only asking
    open-ended questions, which is a far lower-effort interaction.
    """

    allow_free_text: bool = True
    """Whether to offer a "write your own" option alongside ``choices``."""

    default: str | None = None
    """Answer recorded when the question is skipped."""

    required: bool = False
    """Whether an answer is mandatory.

    Blocks *submission*, never navigation — otherwise a user could be trapped
    on a question they cannot answer yet.
    """

    multiline: bool = True
    """Whether the free-text path accepts multi-line input.

    Inert when ``allow_free_text`` is false — there is no free-text path.
    """

    @model_validator(mode="after")
    def validate_answerable(self) -> QuestionDef:
        """Reject a question the user has no way to answer.

        Without choices and without free text there is nothing to select. At
        runtime that surfaces either as an empty-choice ``HumanGateError`` or,
        worse, as a question whose only control is Skip — which is refused
        when ``required`` is set, leaving the user with no way forward.

        Returns:
            The validated model.

        Raises:
            ValueError: If the question offers neither choices nor free text.
        """
        if not self.choices and not self.allow_free_text:
            raise ValueError(
                f"Question {self.id or self.text!r} is unanswerable: it has no 'choices' "
                "and 'allow_free_text' is false. Add choices or allow free text."
            )
        return self


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

    budget_usd: float | None = Field(default=None, gt=0.0)
    """Maximum cost budget for the workflow in USD.

    When set, the engine tracks cumulative cost and acts according to
    ``budget_mode`` when the budget is exceeded. Must be strictly positive
    (a zero budget would trip after the first priced token, which is never
    a useful limit). Default is None (no budget tracking).
    """

    budget_mode: BudgetMode = "audit"
    """How the engine responds when ``budget_usd`` is exceeded.

    - ``audit``: emit a ``budget_exceeded`` event and log a warning,
      but allow the workflow to continue. Use this to discover cost
      profiles before applying hard limits.
    - ``enforce``: emit a ``budget_exceeded`` event, save a checkpoint,
      and stop the workflow with a ``BudgetExceededError``.

    Only takes effect when ``budget_usd`` is set. Default is ``audit``.
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
    """Base delay in seconds before the first retry.

    Also raises the provider's internal 30s backoff cap when set above 30
    (the effective cap is ``max(30, delay_seconds)``); a value below 30
    leaves the cap unchanged. See the Retry section of
    docs/workflow-syntax.md for the resulting wait sequence.
    """

    retry_on: list[Literal["provider_error", "timeout"]] = Field(
        default_factory=lambda: ["provider_error", "timeout"]
    )
    """Error categories that trigger a retry.

    - ``provider_error``: API 500s, rate limits, transient provider failures.
    - ``timeout``: Agent-level timeout exceeded.

    Validation errors (output schema mismatches) are never retried because
    they indicate prompt/schema issues, not transience.
    """

    max_parse_recovery_attempts: int | None = Field(default=None, ge=0, le=10)
    """Maximum in-session parse-recovery attempts before giving up.

    When an agent's response fails JSON extraction, Conductor sends a correction
    prompt in the same session. This field controls how many correction prompts
    to send.

    - ``None`` (default): Use the provider default (Copilot=5, Claude=2).
    - ``0``: Disable parse recovery entirely (fail immediately on bad JSON).
    - ``1-10``: Custom limit.
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
    start a conversation with the user. It is read only by the trigger
    evaluator; the agent holding the conversation never sees it.
    """

    conversation_prompt: str | None = None
    """Instructions for the agent while it holds the conversation.

    Appended to the built-in dialog system prompt for every turn once the
    dialog has opened, and stated there to take precedence over the built-in
    rules where they conflict. This is the only field a workflow author has
    that reaches the conversing agent -- ``trigger_prompt`` reaches the
    evaluator alone.
    """


class ValidatorConfig(BaseModel):
    """Configuration for semantic output validation with retry-once.

    When present on a provider-backed agent, the engine runs a **second
    LLM call** after the primary agent completes. The validator receives
    the primary agent's rendered prompt, its output, and the ``criteria``
    rubric, and must answer whether the output passes
    (``{"passed": bool, "issues": [str, ...]}``).

    If the validator returns ``passed: false`` and ``max_retries > 0``, the
    primary agent is re-run **once** and the second output is taken as
    final — there is no second validation loop. On the ``claude``,
    ``openai``, and ``hermes`` providers the re-run continues the completed
    conversation,
    with the validator's feedback as the next user turn; other providers
    rebuild the prompt with the feedback appended.

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
      the effort level (low=2k, medium=8k, high=16k, xhigh=32k, max=59904 tokens).

    Validation happens at execute time. Claude rejects models that don't
    match the supported prefix list; Copilot consults the SDK's advertised
    ``supported_reasoning_efforts`` (when available) and otherwise allows
    the request through to the SDK.

    Example YAML::

        reasoning:
          effort: high

    Supports Jinja2 templates::

        reasoning:
          effort: "{{ workflow.input.effort }}"

    A templated ``effort`` is accepted at load time and resolved + validated
    at runtime (in :mod:`conductor.executor.agent`), mirroring how ``model``
    and the ``wait`` step's ``duration`` are handled. A *literal* value must
    be one of :data:`~conductor.providers.reasoning.ReasoningEffort`.
    """

    effort: ReasoningEffort | str
    """Reasoning effort level applied to the agent's model calls.

    Either a literal level (``low`` / ``medium`` / ``high`` / ``xhigh`` /
    ``max``) or a ``{{ ... }}`` Jinja2 template resolved at runtime.
    """

    @model_validator(mode="after")
    def _validate_effort(self) -> ReasoningConfig:
        """Accept literal efforts or defer ``{{ }}`` / ``{% %}`` templates.

        A templated value (detected by
        :func:`~conductor.templating.is_jinja_template`, matching ``{{`` or
        ``{%``) skips literal validation here and is rendered + validated at
        execute time (:mod:`conductor.executor.agent`, the same place the
        ``model`` field is rendered). A non-templated value must be a valid
        :data:`ReasoningEffort` literal.

        Note: this is a broader check than
        :meth:`AgentDef._validate_wait_duration`, which intentionally matches
        only ``{{``.
        """
        value = self.effort
        if is_jinja_template(value):
            return self
        if value not in get_args(ReasoningEffort):
            raise ValueError(
                f"reasoning.effort must be one of {list(get_args(ReasoningEffort))} "
                f"or a '{{{{ ... }}}}' template (got {value!r})"
            )
        return self


class SandboxConfig(BaseModel):
    """Per-agent override block for the ``aca`` (Azure Container Apps) sandbox
    provider.

    Only meaningful when the agent's effective provider is ``aca``; the
    fields validate structurally regardless of provider (Literal
    enforcement, ``extra="forbid"``) but are consumed only by
    :class:`~conductor.providers.aca.AcaRuntimeProvider` at runtime.

    Example YAML::

        sandbox:
          identifier_scope: item
          working_dir: /workspace
    """

    model_config = ConfigDict(extra="forbid")

    identifier_scope: Literal["workflow", "agent", "item", "none"] | None = None
    """Override ``runtime.provider.identifier_scope`` for this agent's session
    identifier. ``None`` (default) inherits the workflow-wide setting."""

    working_dir: str | None = None
    """Working directory inside the sandbox session filesystem.

    Unlike :attr:`AgentDef.working_dir` (a *host* path resolved against the
    workflow file's directory), this is interpreted **container-relative** —
    a path inside the remote session filesystem (e.g. ``/workspace``, the
    runner image's default home directory) — because a host path is
    meaningless in a remote container. A subdirectory such as
    ``/workspace/repo`` only exists once something (e.g. a ``git clone``
    step earlier in the workflow) has created it — it does not exist at
    session start, so using it as the *initial* ``working_dir`` is a
    runtime error, never a silent host fallback. Defaults to the runner's
    working directory when unset.
    """


class PluginSourceDef(BaseModel):
    """One entry in a ``plugin_sources:`` mapping.

    Accepts a string shorthand or an object, mirroring the
    ``provider:`` and ``plugins:`` precedents::

        plugin_sources:
          acme: acme/agent-plugins#v1.4.0
          beta:
            source: git@github.com:beta/plugins.git#3f2a1c9
            path: packages/plugins
            plugin: reviewer

    A source declares *where a marketplace comes from*; ``plugins:``
    declares which of its plugins to enable. The split is not invented
    here — it is the one the Copilot CLI already uses in its settings
    (``extraKnownMarketplaces`` alongside ``enabledPlugins``), and it is
    what stops a repository shared by eleven plugins being cloned eleven
    times or pinned to eleven different refs.
    """

    model_config = ConfigDict(extra="forbid")

    source: str
    """Where the marketplace comes from.

    ``owner/repo``, ``owner/repo#ref``, an http/https/ssh URL with an
    optional ``#ref``, a ``git@host:path`` remote, or a local path. The
    grammar is the Copilot CLI's, so a source already written for that
    works here unchanged.

    A ref that is a full 40-character SHA is **pinned**: fetched once and
    never re-checked. Anything else — a tag, a branch, or no ref at all —
    floats, and is re-resolved on every run. Pinning is the only thing
    that stops a source changing what it ships between two runs.
    """

    path: str | None = None
    """Subdirectory within the source holding the marketplace.

    For a repository that keeps its plugins somewhere other than the
    root. Repo-relative and may not escape the checkout.
    """

    plugin: str | None = None
    """Name of the single plugin this source provides.

    Only needed when a repository is *both* a catalog and a plugin —
    it holds a ``marketplace.json`` and a ``plugin.json`` at the same
    level — which is otherwise refused rather than guessed at.
    """

    @field_validator("source")
    @classmethod
    def validate_source(cls, v: str) -> str:
        """Reject a source string that matches none of the known forms.

        Parsed eagerly so a typo fails at load time naming the source the
        author wrote, rather than at fetch time naming a directory they
        never typed.
        """
        from conductor.plugins.sources import parse_plugin_source

        parse_plugin_source(v)
        return v.strip()

    @field_validator("path", "plugin")
    @classmethod
    def validate_optional_text(cls, v: str | None) -> str | None:
        """Reject an empty or whitespace-only optional field."""
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("plugin_sources 'path' and 'plugin' must be non-empty when set")
        return stripped


def _coerce_plugin_sources(value: Any) -> Any:
    """Expand string shorthands in a ``plugin_sources:`` mapping."""
    if not isinstance(value, dict):
        return value
    return {
        key: {"source": entry} if isinstance(entry, str) else entry for key, entry in value.items()
    }


def _validate_plugin_source_names(value: dict[str, PluginSourceDef]) -> dict[str, PluginSourceDef]:
    """Check each marketplace name is usable as a name and a path segment.

    A marketplace name is written after ``@`` in a ``plugins:`` entry and
    becomes a directory component in messages, so it is held to the same
    :data:`~conductor.plugins.manifest.SAFE_NAME` pattern as a plugin.
    """
    from conductor.plugins.manifest import SAFE_NAME

    for name in value:
        if not SAFE_NAME.match(name):
            raise ValueError(
                f"plugin_sources key {name!r} must match {SAFE_NAME.pattern}. The name "
                "is what a plugins entry references after '@'."
            )
    return value


class PluginDef(BaseModel):
    """One entry in a ``plugins:`` list.

    Accepts either a string shorthand (``- prs``) or an object with
    per-component switches, mirroring the ``provider:`` string/object
    precedent::

        plugins:
          - prs                      # everything the plugin ships
          - name: ado
            mcp: false               # skills and agents only

    ``name`` is an **installed plugin name**, a
    **``plugin@marketplace``** reference, or a **filesystem path**. The
    first two are classified by the same syntactic rule ``skills:`` uses
    (path when it starts with ``~``/``.`` or contains a separator).
    Resolution needs the workflow file's directory and the declared
    ``plugin_sources``, neither of which the schema has, so only the
    entry's shape is checked here.

    Every component defaults to **on**. Defaulting one off would
    reproduce the partial-loading bug this feature exists to fix — a
    plugin that loads its instructions but not the subagents or MCP
    tools those instructions call for.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    """Installed plugin name, ``plugin@marketplace``, or a path to a plugin root."""

    skills: bool = True
    """Load the plugin's ``skills/``."""

    agents: bool = True
    """Register the plugin's ``agents/*.agent.md`` as subagents."""

    mcp: bool = True
    """Register the MCP servers the plugin declares.

    Worth a moment's thought before leaving on: an MCP server is a
    subprocess launched with the user's credentials, not text injected
    into a prompt. Conductor starts it only because the workflow named
    the plugin — never because it happened to be installed.
    """

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Reject an empty entry, or a name carrying glob metacharacters.

        A bare name is interpolated into the installed-plugin glob, so
        ``plugins: ["*"]`` would match every installed plugin and report
        itself as *ambiguous across 13 plugins* rather than as the
        nonsense it is. Path entries are left alone — a glob character is
        legal in a directory name.

        The ``plugin@marketplace`` form is split here too, so a malformed
        half fails at load time rather than resolving to something odd.
        """
        stripped = v.strip()
        if not stripped:
            raise ValueError("plugins entries must be non-empty strings")
        from conductor.plugins.manifest import SAFE_NAME
        from conductor.skills import is_path_entry

        if is_path_entry(stripped):
            return stripped

        # Path check first, so './tools/my@plugin' stays a path.
        plugin, marketplace = _split_marketplace(stripped)
        if marketplace is not None and (
            not SAFE_NAME.match(plugin) or not SAFE_NAME.match(marketplace)
        ):
            raise ValueError(
                f"plugins entry {stripped!r} is not a valid 'plugin@marketplace' "
                f"reference. Both halves must match {SAFE_NAME.pattern}."
            )
        if any(char in stripped for char in "*?[]"):
            raise ValueError(
                f"plugins entry {stripped!r} contains a glob metacharacter. An entry is "
                "either an installed plugin name, a 'plugin@marketplace' reference, or "
                "a path (starting with '.' or '~', or containing a separator)."
            )
        return stripped


def _split_marketplace(entry: str) -> tuple[str, str | None]:
    """Split a ``plugin@marketplace`` entry into its two halves.

    Splits on the **last** ``@``, so a plugin name containing one keeps
    it. Callers must establish that ``entry`` is not a path first — a
    directory may legitimately contain ``@``.

    Returns:
        ``(plugin, marketplace)``, with ``marketplace`` ``None`` when the
        entry named none.
    """
    if "@" not in entry:
        return entry, None
    plugin, _, marketplace = entry.rpartition("@")
    return plugin.strip(), marketplace.strip()


def _coerce_plugin_entries(value: Any) -> Any:
    """Expand string shorthands in a ``plugins:`` list.

    Mirrors :meth:`RuntimeConfig._coerce_provider`: a bare string is the
    common case and should not require the object form.
    """
    if not isinstance(value, list):
        return value
    return [{"name": entry} if isinstance(entry, str) else entry for entry in value]


def _validate_plugin_entries(entries: list[PluginDef]) -> list[PluginDef]:
    """Reject a ``plugins:`` list that names the same entry twice.

    Duplicate entries are refused rather than deduplicated because the
    two may disagree about components — ``[prs, {name: prs, mcp: false}]``
    has no correct merge, and silently keeping one would be the wrong
    kind of quiet.
    """
    seen: set[str] = set()
    for entry in entries:
        if entry.name in seen:
            raise ValueError(
                f"plugins contains duplicate entry {entry.name!r}. List each plugin "
                "once, with the components you want on that single entry."
            )
        seen.add(entry.name)
    return entries


def _validate_skill_entries(entries: list[str]) -> list[str]:
    """Validate the shape of ``skills:`` entries at config-load time.

    Bare **names** are checked eagerly against the built-in registry —
    they need no base directory, so an unknown name still surfaces at
    load time exactly as it did before path entries existed.

    **Path** entries are only shape-checked here. Resolving them needs
    the workflow file's directory, which the schema does not have, so
    that happens in :func:`conductor.config.validator.validate_workflow_config`
    (statically) and in ``AgentExecutor`` (at run time).

    Args:
        entries: The raw ``skills:`` list.

    Returns:
        The list unchanged.

    Raises:
        ValueError: If an entry is not a non-empty string, or is a bare
            name that no built-in skill matches.
    """
    from conductor.skills import SkillNotFoundError, get_skill_directory, is_path_entry

    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(f"skills entries must be non-empty strings, got {entry!r}")
        if is_path_entry(entry):
            continue
        try:
            get_skill_directory(entry)
        except SkillNotFoundError as exc:
            raise ValueError(str(exc)) from exc
    return entries


ProviderName = Literal["copilot", "openai", "claude", "claude-agent-sdk", "hermes", "aca"]
"""Canonical set of supported agent provider names.

Used by :attr:`AgentDef.provider` and :attr:`ProviderSettings.name` so the
schema, factory, and registry cannot drift out of sync.
"""


class StepBase(BaseModel):
    """Common identity and input fields for executable workflow steps."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    input: list[str] = Field(default_factory=list)


class RoutableStepBase(StepBase):
    """Common fields for workflow steps that route after execution."""

    routes: list[RouteDef] = Field(default_factory=list)


def _normalize_step_type(value: Any) -> Any:
    if isinstance(value, dict) and value.get("type") is None:
        return {**value, "type": "agent"}
    return value


def _preserve_file_string(value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
    """Keep a ``FileString`` (``!file``-loaded prompt) uncoerced so the renderer
    can resolve relative ``{% include %}`` paths against its source file."""
    if isinstance(value, FileString):
        return value
    return handler(value)


def _require_step_type_in_schema(schema: dict[str, Any]) -> None:
    """Tighten the published JSON Schema so non-LLM steps require an explicit ``type``.

    Runtime parsing supplies each variant's discriminator default itself (and
    routes untagged mappings to ``AgentDef`` via the union's before-validator),
    but a generated ``oneOf`` schema has no such normalization: with every
    variant's ``type`` defaulted, an untagged agent mapping matches several
    branches at once and ``oneOf`` rejects it. Requiring the discriminator on
    every non-LLM branch restores exactly-one-match for those payloads. This is
    a schema-only tightening — runtime types and constructor defaults are
    unchanged, so programmatic construction without an explicit ``type`` keeps
    working. Applied at class level, the tightening also shapes each variant's
    standalone and serialization schemas; that is deliberate, since a dumped
    instance always carries its canonical ``type``.
    """
    required = schema.setdefault("required", [])
    if "type" not in required:
        required.append("type")


def _agent_step_type_in_schema(schema: dict[str, Any]) -> None:
    """Widen the LLM branch's ``type`` to the three forms the loader accepts.

    ``_normalize_step_type`` maps an omitted or explicit ``null`` ``type`` to
    ``"agent"`` at runtime; the published schema must admit the same inputs or
    schema-aware tooling (editors, external linters) rejects workflows that
    Conductor runs without complaint.
    """
    properties = schema.get("properties")
    if isinstance(properties, dict) and "type" in properties:
        properties["type"] = {
            "anyOf": [{"const": "agent"}, {"type": "null"}],
            "default": "agent",
            "title": "Type",
        }


class AgentDef(RoutableStepBase):
    """Provider-backed LLM agent definition."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_agent_step_type_in_schema)

    type: Literal["agent"] = "agent"
    provider: ProviderName | None = None
    model: str | None = None
    context_tier: ContextTier | str | None = None
    tools: list[str] | None = None
    system_prompt: str | None = None
    prompt: str = ""
    output: dict[str, OutputField] | None = None
    output_mode: Literal["raw", "envelope"] | None = None
    working_dir: str | None = None
    settings_dir: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None = (
        None
    )
    timeout_seconds: float | None = Field(None, ge=1.0)
    max_session_seconds: float | None = Field(None, ge=1.0)
    max_agent_iterations: int | None = Field(None, ge=1, le=500)
    session_key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None = (
        None
    )
    retry: RetryPolicy | None = None
    dialog: DialogConfig | None = None
    reasoning: ReasoningConfig | None = None
    validator: ValidatorConfig | None = None
    sandbox: SandboxConfig | None = None
    skills: list[str] | None = None
    plugins: list[PluginDef] | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_type(cls, value: Any) -> Any:
        return _normalize_step_type(value)

    @field_validator("prompt", "system_prompt", mode="wrap")
    @classmethod
    def preserve_file_string(cls, value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
        return _preserve_file_string(value, handler)

    @field_validator("session_key")
    @classmethod
    def validate_session_key(cls, value: str | None) -> str | None:
        if value is not None and is_jinja_template(value):
            raise ValueError(
                f"session_key {value!r} looks like a Jinja2 template, but session_key is "
                "never rendered — it would be used verbatim as a single literal key shared "
                "by every execution. Use a static label."
            )
        return value

    @field_validator("skills")
    @classmethod
    def validate_skills(cls, value: list[str] | None) -> list[str] | None:
        return value if value is None else _validate_skill_entries(value)

    @field_validator("plugins", mode="before")
    @classmethod
    def coerce_plugins(cls, value: Any) -> Any:
        return _coerce_plugin_entries(value)

    @field_validator("plugins")
    @classmethod
    def validate_plugins(cls, value: list[PluginDef] | None) -> list[PluginDef] | None:
        return value if value is None else _validate_plugin_entries(value)

    @model_validator(mode="after")
    def validate_agent(self) -> AgentDef:
        if (
            self.context_tier is not None
            and not is_jinja_template(self.context_tier)
            and self.context_tier not in get_args(ContextTier)
        ):
            raise ValueError(
                f"context_tier must be one of {list(get_args(ContextTier))} "
                f"or a '{{{{ ... }}}}' template (got {self.context_tier!r})"
            )
        if self.output_mode == "raw" and self.output:
            raise ValueError(
                "output_mode 'raw' is incompatible with output schema; remove the output: "
                "block or use output_mode: envelope"
            )
        return self

    def effective_output_schema(self) -> dict[str, OutputField] | None:
        if self.output and self.output_mode != "raw":
            return self.output
        return None


class HumanGateStepDef(RoutableStepBase):
    """Human decision gate definition."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_require_step_type_in_schema)

    type: Literal["human_gate"] = "human_gate"
    prompt: str
    options: list[GateOption]

    @field_validator("prompt", mode="wrap")
    @classmethod
    def preserve_prompt_file_string(cls, value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
        return _preserve_file_string(value, handler)

    @model_validator(mode="after")
    def validate_gate(self) -> HumanGateStepDef:
        if not self.options:
            raise ValueError("human_gate agents require 'options'")
        if not self.prompt:
            raise ValueError("human_gate agents require 'prompt'")
        return self


class QuestionsStepDef(RoutableStepBase):
    """Interactive questions step definition."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_require_step_type_in_schema)

    type: Literal["questions"] = "questions"
    prompt: str = ""
    questions: list[QuestionDef] | None = None
    source: str | None = None
    allow_back: bool | None = None
    allow_skip: bool | None = None
    allow_skip_all: bool | None = None
    allow_abort: bool | None = None
    abort_route: str | None = None

    @field_validator("prompt", mode="wrap")
    @classmethod
    def preserve_prompt_file_string(cls, value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
        return _preserve_file_string(value, handler)

    @model_validator(mode="after")
    def validate_source(self) -> QuestionsStepDef:
        if not self.questions and not self.source:
            raise ValueError("questions agents require either 'questions' or 'source'")
        if self.questions and self.source:
            raise ValueError(
                "questions agents cannot set both 'questions' and 'source' (use one or the other)"
            )
        if self.abort_route is not None and not self.allow_abort:
            raise ValueError(
                "questions agents cannot set 'abort_route' without 'allow_abort: true'"
            )
        if self.source is not None:
            validate_dotted_source(self.source)
        return self


class ScriptStepDef(RoutableStepBase):
    """Subprocess-backed script step definition."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_require_step_type_in_schema)

    type: Literal["script"] = "script"
    output: dict[str, OutputField] | None = None
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    working_dir: str | None = None
    stdin: str | None = None
    timeout: int | None = Field(None, gt=0)

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: str) -> str:
        if not value:
            raise ValueError("script agents require 'command'")
        return value

    @model_validator(mode="before")
    @classmethod
    def require_command(cls, value: Any) -> Any:
        if isinstance(value, dict) and not value.get("command"):
            raise ValueError("script agents require 'command'")
        return value

    @model_validator(mode="after")
    def validate_command_present(self) -> ScriptStepDef:
        """Re-assert the command invariant when an existing instance is revalidated.

        Pydantic skips field and before-model validators for an already-built
        instance (e.g. a ``model_copy`` result nested inside ``WorkflowConfig``),
        so the checks above never fire on that path; this after-model validator
        does, keeping a mutated copy from pushing an empty command past
        ``WorkflowConfig.model_validate``.
        """
        if not self.command:
            raise ValueError("script agents require 'command'")
        return self


class MCPStepDef(RoutableStepBase):
    """Direct MCP tool-call step definition."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_require_step_type_in_schema)

    type: Literal["mcp"] = "mcp"
    output: dict[str, OutputField] | None = None
    timeout: int | None = Field(None, gt=0)
    server: str
    tool: str
    arguments: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def require_target(cls, value: Any) -> Any:
        if isinstance(value, dict):
            if not value.get("server"):
                raise ValueError("mcp agents require 'server'")
            if not value.get("tool"):
                raise ValueError("mcp agents require 'tool'")
        return value

    @field_validator("server", "tool")
    @classmethod
    def validate_name(cls, value: str, info: ValidationInfo) -> str:
        if not value:
            raise ValueError(f"mcp agents require '{info.field_name}'")
        if is_jinja_template(value):
            raise ValueError(
                f"{info.field_name} {value!r} looks like a Jinja2 template, but "
                f"{info.field_name} is never rendered — static validation of the server/tool "
                "pair requires a literal value. Use a static name."
            )
        return value

    @model_validator(mode="after")
    def validate_target_present(self) -> MCPStepDef:
        """Re-assert the required-target invariant on instance revalidation.

        Pydantic skips field and before-model validators for an already-built
        instance (e.g. a ``model_copy`` result nested inside ``WorkflowConfig``),
        so the checks above never fire on that path; this after-model validator
        does.
        """
        if not self.server:
            raise ValueError("mcp agents require 'server'")
        if not self.tool:
            raise ValueError("mcp agents require 'tool'")
        return self


def _check_wait_duration(value: Any) -> None:
    """Parse ``value`` as a wait duration and enforce ``0 < d <= 24h``.

    Shared by ``WaitStepDef``'s field validator (first-pass, mapping input) and
    its after-model validator (revalidation of an existing instance) so both
    paths enforce the same rule. Templated durations (containing ``{{``) defer
    all literal validation to runtime.
    """
    if isinstance(value, bool):
        raise ValueError(f"duration must be a number or duration string, not boolean: {value!r}")
    if isinstance(value, str) and "{{" in value:
        return
    try:
        seconds = parse_duration(value)
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


class WaitStepDef(RoutableStepBase):
    """Cancellable delay step definition."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_require_step_type_in_schema)

    type: Literal["wait"] = "wait"
    duration: str | int | float
    reason: str | None = None

    @field_validator("duration", mode="before")
    @classmethod
    def validate_duration(cls, value: Any) -> Any:
        _check_wait_duration(value)
        return value

    @model_validator(mode="after")
    def validate_duration_value(self) -> WaitStepDef:
        """Re-assert the duration bounds on instance revalidation.

        Pydantic skips field validators for an already-built instance (e.g. a
        ``model_copy`` result nested inside ``WorkflowConfig``), so a mutated
        copy carrying an out-of-bounds duration would otherwise sail through
        config validation.
        """
        _check_wait_duration(self.duration)
        return self


class SetStepDef(RoutableStepBase):
    """Context-binding step definition."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_require_step_type_in_schema)

    type: Literal["set"] = "set"
    output: dict[str, OutputField] | None = None
    value: str | None = None
    values: dict[str, str] | None = None
    output_type: (
        Literal["auto", "string", "number", "integer", "boolean", "list", "dict"] | None
    ) = None

    @model_validator(mode="after")
    def validate_bindings(self) -> SetStepDef:
        if (self.value is None) == (self.values is None):
            raise ValueError("set agents require exactly one of 'value' or 'values'")
        if self.values is not None and self.output_type is not None:
            raise ValueError(
                "set agents with 'values:' cannot have 'output_type' "
                "(it only applies to single 'value:'; per-key typing is not yet supported)"
            )
        return self


class TerminateStepDef(StepBase):
    """Explicit terminal outcome step definition."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_require_step_type_in_schema)

    type: Literal["terminate"] = "terminate"
    status: Literal["success", "failed"]
    reason: str
    output_template: dict[str, str] | None = None

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("terminate agents require a non-empty 'reason'")
        return value

    @model_validator(mode="after")
    def validate_termination(self) -> TerminateStepDef:
        """Re-assert the status/reason invariants on instance revalidation.

        Pydantic skips field validators — including the ``Literal`` check on
        ``status`` — for an already-built instance (e.g. a ``model_copy``
        result nested inside ``WorkflowConfig``), so the checks above never
        fire on that path; this after-model validator does. Both checks are
        written defensively: the stored values never passed through coercion,
        so they may be ``None`` or otherwise wrongly typed.
        """
        if self.status not in ("success", "failed"):
            raise ValueError("terminate agents require 'status' (must be 'success' or 'failed')")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("terminate agents require a non-empty 'reason'")
        return self


class WorkflowStepDef(RoutableStepBase):
    """Nested workflow step definition."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_require_step_type_in_schema)

    type: Literal["workflow"] = "workflow"
    output: dict[str, OutputField] | None = None
    workflow: str
    input_mapping: dict[str, str] | None = None
    max_depth: int | None = Field(None, ge=1, le=10)

    @model_validator(mode="before")
    @classmethod
    def require_workflow(cls, value: Any) -> Any:
        if isinstance(value, dict) and not value.get("workflow"):
            raise ValueError("workflow agents require 'workflow' path")
        return value

    @field_validator("workflow")
    @classmethod
    def validate_workflow(cls, value: str) -> str:
        if not value:
            raise ValueError("workflow agents require 'workflow' path")
        return value

    @model_validator(mode="after")
    def validate_workflow_path(self) -> WorkflowStepDef:
        """Re-assert the workflow-path invariant on instance revalidation.

        Pydantic skips field and before-model validators for an already-built
        instance (e.g. a ``model_copy`` result nested inside ``WorkflowConfig``),
        so the checks above never fire on that path; this after-model validator
        does.
        """
        if not self.workflow:
            raise ValueError("workflow agents require 'workflow' path")
        return self


StepDef = Annotated[
    AgentDef
    | HumanGateStepDef
    | QuestionsStepDef
    | ScriptStepDef
    | MCPStepDef
    | WaitStepDef
    | SetStepDef
    | TerminateStepDef
    | WorkflowStepDef,
    Field(discriminator="type"),
    BeforeValidator(_normalize_step_type),
]


class ForEachDef(BaseModel):
    """Dynamic parallel execution group definition."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    type: Literal["for_each"]
    source: str
    as_: str = Field(..., serialization_alias="as", validation_alias="as")
    agent: StepDef
    max_concurrent: int = Field(default=10, ge=1, le=100)
    failure_mode: Literal["fail_fast", "continue_on_error", "all_or_nothing"] = "fail_fast"
    key_by: str | None = None
    routes: list[RouteDef] = Field(default_factory=list)

    @field_validator("as_")
    @classmethod
    def validate_loop_variable(cls, value: str) -> str:
        reserved = {"workflow", "context", "output", "_index", "_key"}
        if value in reserved:
            raise ValueError(
                f"Loop variable '{value}' conflicts with reserved name. Reserved names: {reserved}"
            )
        if not value.isidentifier():
            raise ValueError(f"Loop variable '{value}' must be a valid Python identifier")
        return value

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        return validate_dotted_source(value)


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

    Note: With the Claude and Claude Agent SDK providers, env vars are passed
    correctly to MCP server subprocesses. However, the Copilot provider
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


def explicit_auth_mode_setting_sources_error(auth_mode: str, setting_sources: Any) -> str:
    """Explain why an explicit ``claude-agent-sdk`` ``auth_mode`` refuses settings tiers.

    Shared by the static check on :class:`ProviderSettings` and the run-time
    check in ``ClaudeAgentSdkProvider._check_auth_readiness`` (``conductor run``
    never calls the static validator), so both boundaries state the same cause
    and the same two remedies.
    """
    return (
        f"auth_mode '{auth_mode}' cannot be combined with runtime.provider."
        f"setting_sources ({', '.join(setting_sources)}): a Claude Code settings file's "
        "'env' block is applied by the CLI after Conductor configures the child "
        "environment, so it can inject a credential or backend selector that overrides "
        "the explicit mode, and the SDK offers no override that outranks it. Remove "
        "setting_sources, or use auth_mode 'auto'."
    )


class ProviderSettings(BaseModel):
    """Structured provider configuration for ``runtime.provider``.

    Supports two YAML shapes via :meth:`RuntimeConfig._coerce_provider`:

    - String shorthand: ``provider: copilot`` (equivalent to
      ``provider: {name: copilot}``).
    - Object form: configures custom model-provider routing, an existing
      Copilot runtime connection, or both. Copilot routing fields are forwarded
      to ``copilot.client.create_session(provider=...)``; runtime fields select
      how the SDK reaches the Copilot CLI process.

    Custom routing activates only when a routing field is set and fills missing
    values from environment variables (see :meth:`has_custom_routing`). Runtime
    connection settings are tracked separately by :meth:`has_external_runtime`.

    The model is frozen after construction (``frozen=True``) because
    structured provider settings are set once at config load. This avoids the
    Pydantic gotcha where ``model_validator(mode="after")``
    cross-field invariants do not re-fire on per-attribute assignment
    even with ``validate_assignment=True``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: ProviderName = "copilot"
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

    Sent as ``Authorization: Bearer <token>`` by the Anthropic SDK. Use for
    Databricks AI Gateway, LiteLLM proxies, or any endpoint that expects a
    bearer token rather than an ``x-api-key`` credential. Set exactly one of
    ``auth_token`` / ``api_key``: the Anthropic SDK does not choose between
    them — when both are set it sends both ``X-Api-Key`` and
    ``Authorization: Bearer`` headers on every request, so the API key
    reaches whatever ``base_url`` points at.

    Credentials resolve as a unit: setting either credential in YAML
    suppresses both ``ANTHROPIC_API_KEY`` and ``ANTHROPIC_AUTH_TOKEN`` env
    vars. The env fallback applies only when no credential is set in YAML.

    Example::

        provider:
          name: claude
          base_url: https://my-gateway.example.com/api/v1
          auth_token: ${DATABRICKS_TOKEN}
    """

    auth_mode: Literal["auto", "subscription", "api_key"] | None = None
    """Credential path for the ``claude`` child process. ``claude-agent-sdk`` only.

    ``"auto"`` leaves the inherited environment untouched, so the CLI applies
    its own credential precedence. ``"subscription"`` blanks API-key, token,
    OAuth, and Bedrock/Vertex/Foundry variables in the child environment so
    the logged-in session is used; ``"api_key"`` requires a nonblank
    ``ANTHROPIC_API_KEY`` and blanks the others. Both explicit modes refuse
    ``setting_sources``. Selects a credential, not an endpoint.
    """

    headers: dict[str, str] | None = None
    """Extra HTTP headers to send with every request. Copilot-only."""

    setting_sources: list[Literal["user", "project", "local"]] | None = None
    """Claude Code settings tiers the session may load. claude-agent-sdk-only.

    Defaults to ``None``, which Conductor sends to the CLI as ``[]`` — load
    nothing ambient. That default is the skills counterpart of
    ``strict_mcp_config``: a workflow gets the skills, instructions and hooks
    it *declares*, never whatever the machine running it happens to have
    installed, so a run reproduces on another developer's laptop.

    Set it to opt a workflow back in. The motivating case is an agent working
    against a *target* repository that ships its own ``.claude/skills``:
    ``["project"]`` loads that repository's skills natively, without it
    needing to package them as a Claude Code plugin (the CLI has
    ``--plugin-dir`` but no ``--skill-dir``, so a plugin root is otherwise
    the only handle).

    Which directory the ``project`` tier reads is chosen per agent. Skills
    come from cwd (:attr:`AgentDef.working_dir`) *and* from
    :attr:`AgentDef.settings_dir`; everything else the tier defines --
    ``CLAUDE.md``, ``.claude/settings.json``, ``.claude/agents`` — follows
    cwd alone. Prefer ``settings_dir`` when the agent also needs a wider cwd:
    the CLI advertises cwd as its sole MCP root, so narrowing cwd onto the
    target repository narrows what the agent's MCP servers may read.

    Each tier brings everything that tier defines, hooks included: ``project``
    reads ``<cwd>/.claude/settings.json``, whose ``hooks`` entries run
    commands. Enable it only for repositories trusted to the same degree as
    the workflow itself.

    ``user`` additionally reads ``~/.claude/settings.json``, which makes a run
    depend on the operator's personal machine — reproducible only by
    accident. Prefer ``["project"]``.

    Scope: this field is workflow-global, while ``working_dir`` is per agent,
    so every agent on the provider loads the enabled tiers — each resolving
    them against its own ``working_dir``, and agents without one against the
    directory ``conductor run`` was launched in. Point the tier-using agents
    at the target repo, and give any agent that must stay hermetic an explicit
    ``skills: []``, which opts that agent out of the tiers entirely.

    Example::

        runtime:
          provider:
            name: claude-agent-sdk
            setting_sources: [project]
    """

    azure: AzureProviderOptions | None = None
    """Azure-specific options (e.g. ``api_version``). Requires
    ``type: azure``. Copilot-only."""

    runtime_url: str | None = None
    """Connect to an already-running Copilot runtime instead of spawning a
    nested one. Copilot-only.

    Accepts ``"port"``, ``"host:port"``, or a full URL. When set, the Copilot
    provider connects to the external runtime via the SDK's
    ``RuntimeConnection.for_uri(...)`` — no child ``copilot`` process is
    spawned. Agents share the authenticated runtime process while retaining
    separate SDK sessions. This is the recommended way to run Conductor inside
    an external orchestrator that already owns an authenticated
    ``copilot --headless`` process.

    Falls back to the ``COPILOT_PROVIDER_RUNTIME_URL`` environment variable
    when not set in YAML. May be combined with custom model-provider routing:
    the runtime URL selects the CLI transport, while ``base_url`` / ``api_key``
    and related fields configure the model endpoint for each SDK session.

    Example::

        provider:
          name: copilot
          runtime_url: localhost:3000
          runtime_token: ${COPILOT_RUNTIME_TOKEN}
    """

    runtime_token: SecretStr | None = None
    """Shared secret authenticating the connection to ``runtime_url``. Copilot-only.

    Required when the server was started with a connection token. Prefer
    ``${COPILOT_RUNTIME_TOKEN}`` interpolation so the literal value never lands
    in ``workflow_started`` events or checkpoints. Falls back to the
    ``COPILOT_PROVIDER_RUNTIME_TOKEN`` environment variable when not set in
    YAML. Requires ``runtime_url``."""

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

    pool_endpoint: str | None = None
    """Azure Container Apps dynamic-sessions pool management endpoint. Aca-only.

    Required when ``name: aca``. The host issues requests to
    ``{pool_endpoint}/execute?identifier=<id>&api-version=<v>`` to run the
    agent inside a pool session. Must be ``https://`` — AAD bearer tokens and
    forwarded provider credentials (``inner_provider_settings``) are sent to
    this endpoint on every request.
    """

    api_version: str | None = None
    """ACA management API version (e.g. ``"2025-07-01"``). Aca-only."""

    inner_provider: Literal["copilot", "claude-agent-sdk"] | None = None
    """SDK the in-sandbox runner drives. Aca-only.

    Defaults to ``"copilot"`` when ``name: aca`` and unset. **MVP: ``copilot``
    only.** Claude-inside requires the containerizable ``claude-agent-sdk``
    CLI; the bare ``claude`` (Anthropic-API) provider has no in-process tool
    runtime and is not a valid inner provider.
    """

    identifier_scope: Literal["workflow", "agent", "item", "none"] | None = None
    """Default granularity for *sequential* session-identifier reuse. Aca-only.

    Defaults to ``"agent"`` when ``name: aca`` and unset: one session per
    agent, reused across that agent's sequential re-executions (loop-backs).
    Concurrent units (parallel members, for-each iterations) always diverge
    the identifier regardless of scope, so ``concurrent_safe`` stays honest.
    Overridable per-agent via the ``sandbox:`` block (:class:`SandboxConfig`).
    """

    egress: Literal["enabled", "disabled"] | None = None
    """Advisory mirror of the pool's ``sessionNetworkConfiguration.status``. Aca-only.

    The pool itself governs actual network egress; this field only informs
    ``conductor validate`` / dashboards of the expected posture.
    """

    lifecycle: Literal["timed", "on_container_exit"] | None = None
    """Advisory mirror of the pool's session lifecycle mode. Aca-only."""

    auth: Literal["azure_default"] | None = None
    """Session Executor authentication strategy. Aca-only.

    Defaults to ``"azure_default"`` when ``name: aca`` and unset, meaning the
    host acquires a ``dynamicsessions.io`` bearer token via
    ``DefaultAzureCredential``. Currently the only supported strategy.
    """

    @model_validator(mode="after")
    def _check_field_compatibility(self) -> ProviderSettings:
        copilot_only_fields = {
            "type": self.type,
            "wire_api": self.wire_api,
            "bearer_token": self.bearer_token,
            "headers": self.headers,
            "azure": self.azure,
            "runtime_url": self.runtime_url,
            "runtime_token": self.runtime_token,
        }
        claude_only_fields = {
            "auth_token": self.auth_token,
        }
        aca_only_fields = {
            "pool_endpoint": self.pool_endpoint,
            "api_version": self.api_version,
            "inner_provider": self.inner_provider,
            "identifier_scope": self.identifier_scope,
            "egress": self.egress,
            "lifecycle": self.lifecycle,
            "auth": self.auth,
        }
        if self.name != "copilot":
            extras = sorted(k for k, v in copilot_only_fields.items() if v is not None)
            if self.name == "openai" and extras:
                if "wire_api" in extras:
                    raise ValueError(
                        "Provider fields ['wire_api'] are Copilot-only. "
                        "The 'openai' provider always speaks the Chat Completions "
                        "wire API; remove the field."
                    )
                if "type" in extras:
                    raise ValueError(
                        "Provider fields ['type'] are Copilot-only. "
                        "The 'openai' provider always speaks the Chat Completions "
                        "wire API; remove the field."
                    )
            if extras:
                raise ValueError(
                    f"Provider fields {extras} are only supported when name='copilot'. "
                    "Structured provider config for other providers is not yet implemented."
                )
        if self.name not in ("copilot", "openai", "claude", "hermes") and (
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
        if self.auth_mode is not None and self.name != "claude-agent-sdk":
            raise ValueError("'auth_mode' is only supported when name='claude-agent-sdk'")
        if self.name != "aca":
            extras = sorted(k for k, v in aca_only_fields.items() if v is not None)
            if extras:
                raise ValueError(f"Provider fields {extras} are only supported when name='aca'.")

        if self.setting_sources is not None and self.name != "claude-agent-sdk":
            raise ValueError(
                "'setting_sources' is only supported when name='claude-agent-sdk' "
                f"(got name={self.name!r}). It selects Claude Code settings tiers, "
                "which no other provider reads."
            )

        # Explicit auth modes refuse settings tiers; ``auto`` keeps them.
        # Mirrored at run time in ``_check_auth_readiness``.
        if (
            self.name == "claude-agent-sdk"
            and self.setting_sources
            and self.auth_mode in ("subscription", "api_key")
        ):
            raise ValueError(
                explicit_auth_mode_setting_sources_error(self.auth_mode, self.setting_sources)
            )

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

        # Reject empty containers and empty/whitespace-only SecretStr — they
        # activate custom routing via has_custom_routing() but resolve to falsy
        # values in the resolver and would silently drop the entire SDK provider
        # kwarg. The provider strips these values at runtime, so a whitespace-only
        # secret would silently normalize to None; reject it here (non-mutating
        # .strip() check) so `conductor validate` matches the resolver.
        if self.headers is not None and len(self.headers) == 0:
            raise ValueError(
                "'headers' must contain at least one entry; remove the key to omit headers"
            )
        for secret_field, value in (
            ("api_key", self.api_key),
            ("bearer_token", self.bearer_token),
            ("auth_token", self.auth_token),
            ("runtime_token", self.runtime_token),
        ):
            if value is not None and value.get_secret_value().strip() == "":
                raise ValueError(
                    f"'{secret_field}' is empty; remove the key or supply a value "
                    "(typo / unset env interpolation?)"
                )

        # An empty (or whitespace-only) runtime_url must also be rejected: because
        # "" is not None, has_external_runtime() would return True and the
        # runtime_token guard (runtime_url is None) would pass, yet the provider
        # treats "" as falsy and silently falls back to env / a nested spawn while
        # dropping the token.
        if self.runtime_url is not None and self.runtime_url.strip() == "":
            raise ValueError(
                "'runtime_url' is empty; remove the key or supply a value "
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

        # A connection token is meaningless without a URL to connect to.
        if self.runtime_token is not None and self.runtime_url is None:
            raise ValueError("'runtime_token' requires 'runtime_url' to also be set")

        # 'aca' has no equivalent of the copilot/claude anchor fields — the
        # pool endpoint IS the anchor. Reject empty/whitespace the same way
        # runtime_url is rejected above, so a typo'd or unset env
        # interpolation fails at config time rather than at the first
        # dynamic-sessions request.
        if self.name == "aca" and (self.pool_endpoint is None or self.pool_endpoint.strip() == ""):
            raise ValueError("'pool_endpoint' is required when name='aca'")

        # AAD bearer tokens (DefaultAzureCredential) and, in Phase 1,
        # forwarded provider credentials (inner_provider_settings) are sent
        # to this endpoint on every request — plain HTTP would leak both in
        # transit. Require HTTPS explicitly since pool_endpoint has no other
        # transport-security guardrail. Parse the full URL (not just the
        # scheme prefix): a missing hostname (``https://``) or a query/
        # fragment (``https://host?x=1``) both produce a malformed request
        # URL once `_build_url` appends `/execute` and the `identifier` /
        # `api-version` query params (aca.py).
        if self.name == "aca" and self.pool_endpoint is not None:
            parsed = urlparse(self.pool_endpoint.strip())
            if parsed.scheme != "https":
                raise ValueError(
                    "'pool_endpoint' must use https:// — AAD bearer tokens and "
                    "forwarded provider credentials are sent to this endpoint "
                    "and must not travel over an unencrypted connection"
                )
            if not parsed.hostname:
                raise ValueError("'pool_endpoint' must include a hostname (e.g. https://<pool>)")
            if parsed.query or parsed.fragment:
                raise ValueError(
                    "'pool_endpoint' must not include a query string or fragment — it is "
                    "a base URL that 'identifier' and 'api-version' are appended to "
                    "(e.g. https://<pool-management-endpoint>, not one with '?' or '#')"
                )

        # Apply 'aca' defaults for fields left unset in YAML. These can't be
        # ordinary Pydantic field defaults because the gating checks above
        # (and the copilot/claude branches) rely on `None` meaning "not set
        # in YAML" regardless of `name`. `object.__setattr__` bypasses the
        # model's `frozen=True` (a deliberate, narrow escape hatch — see the
        # class docstring for why the model is frozen at all) so these
        # defaults are applied exactly once, after validation, without
        # re-triggering `model_validator`.
        if self.name == "aca":
            if self.inner_provider is None:
                object.__setattr__(self, "inner_provider", "copilot")
            if self.identifier_scope is None:
                object.__setattr__(self, "identifier_scope", "agent")
            if self.auth is None:
                object.__setattr__(self, "auth", "azure_default")
        if self.name == "claude-agent-sdk" and self.auth_mode is None:
            object.__setattr__(self, "auth_mode", "auto")

        return self

    def has_custom_routing(self) -> bool:
        """Return True when YAML explicitly opted into custom routing.

        Custom routing is gated on at least one non-``name`` field being
        set. We never activate from ambient environment variables alone —
        that would silently divert default Copilot traffic based on
        unrelated shell state.

        Note: this covers only *endpoint* routing (``base_url`` and friends).
        Connecting to an existing runtime (``runtime_url``) is a separate
        axis — see :meth:`has_external_runtime` — and is intentionally
        excluded so it does not activate the endpoint-provider resolver.
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

    def has_external_runtime(self) -> bool:
        """Return True when YAML configured connecting to an existing runtime.

        Gated on ``runtime_url`` being set in YAML. As with custom routing,
        ambient environment variables never activate this on their own here;
        the ``COPILOT_PROVIDER_RUNTIME_URL`` fallback is resolved at the
        provider layer.
        """
        return self.runtime_url is not None

    def has_aca_config(self) -> bool:
        """Return True when YAML configured the ``aca`` sandbox provider.

        Gated on ``name == "aca"`` — ``pool_endpoint`` (and any other
        ``aca``-only field) is required whenever ``name == "aca"`` (enforced
        by :meth:`_check_field_compatibility`), so this is equivalent to
        checking ``pool_endpoint is not None`` but reads clearer at call
        sites and stays correct even if that requirement is ever relaxed.
        """
        return self.name == "aca"

    def has_structured_config(self) -> bool:
        """Return True when the provider has any non-default structured settings."""
        return (
            self.has_custom_routing()
            or self.has_external_runtime()
            or self.has_aca_config()
            or self.setting_sources is not None
            or self.auth_mode in ("subscription", "api_key")
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
        if not self.has_structured_config():
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


class ToolOutputConfig(BaseModel):
    """MCP tool result output-size configuration.

    Controls how the result of each individual MCP tool call is handled when it
    exceeds a per-result character limit. This is a per-result cap, not a
    cumulative context-window budget: each tool result is evaluated
    independently against ``max_chars``.

    When ``spill_to_file`` is enabled, the full oversized result is written to
    a process-private temporary file and the model receives a truncated prefix
    plus a marker pointing to that file. When disabled, the result is simply
    truncated to the limit (provider-specific behavior may differ, e.g. the
    Copilot SDK disables large-output handling entirely).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    """Whether per-result MCP tool output size limiting is active."""

    max_chars: int = Field(default=50000, ge=1000)
    """Maximum number of characters to retain from each individual tool result.

    Results longer than this are truncated; the kept prefix is delivered to
    the model and the remainder is either spilled to a file or discarded
    according to ``spill_to_file``.
    """

    spill_to_file: bool = True
    """Whether oversized results should be written to a temporary file.

    When ``True``, the full result is persisted to disk and a marker containing
    the file path is appended to the truncated prefix. When ``False``, no file is
    created and truncation is applied in-place.
    """

    spill_dir: str | None = None
    """Directory used for spilled tool output files.

    ``None`` (the default) resolves to ``<tempfile.gettempdir()>/conductor/tool-output``.
    Relative paths are resolved against the current working directory of the
    process. Parent directories are created automatically if needed.
    """

    @field_validator("spill_dir")
    @classmethod
    def _normalize_spill_dir(cls, v: str | None) -> str | None:
        """Normalize an empty/whitespace ``spill_dir`` to ``None``.

        Without this, consumers disagree on what ``""`` means: the MCP manager
        treats it as falsy and falls back to the default temp dir, while the
        Copilot provider checks ``is not None`` and forwards an empty
        ``output_directory`` to the SDK. One normalization point keeps the
        behavior identical across providers.
        """
        if v is None:
            return None
        return v.strip() or None


class SkillInjectionConfig(BaseModel):
    """Size limits for eagerly injected skill content.

    Providers without a native skill surface (``claude``, ``hermes``)
    have no progressive disclosure: :class:`~conductor.executor.agent.AgentExecutor`
    prepends every enabled skill's ``SKILL.md`` **plus its entire
    ``references/`` tree** to the rendered prompt, on every agent call and
    every retry. The bundled ``conductor`` skill alone is ~132KB (~33K
    tokens), so an unbounded list is easy to turn into most of a context
    window by accident.

    Both limits are measured against the exact string that gets
    prepended. Setting either to ``null`` disables that limit.

    Example YAML::

        runtime:
            skill_injection:
                warn_bytes: 65536     # warn above 64KB
                max_bytes: 163840     # fail above 160KB
    """

    # Frozen for the reason ``ProviderSettings`` documents: this model carries a
    # cross-field invariant in a ``model_validator(mode="after")``, and that does
    # not re-fire on per-attribute assignment even under the enclosing
    # ``RuntimeConfig``'s ``validate_assignment=True``.
    model_config = ConfigDict(extra="forbid", frozen=True)

    warn_bytes: int | None = Field(default=64 * 1024, ge=0)
    """Log a warning when injected skill content exceeds this many bytes.

    ``None`` disables the warning. The 64KB default is below the bundled
    ``conductor`` skill's ~132KB so that combination is surfaced rather
    than passing silently.
    """

    max_bytes: int | None = Field(default=160 * 1024, ge=0)
    """Fail the agent when injected skill content exceeds this many bytes.

    ``None`` disables the limit. The 160KB default is above the bundled
    ``conductor`` skill's ~132KB, so enabling it does not break an
    existing single-skill workflow — it catches accumulation.

    Raised from 128KB once the bundled skill grew past it: the ceiling was
    chosen when that skill was ~117KB, and two independent documentation
    additions carried it over. A default that the shipped skill fails is
    not a limit, it is a broken workflow, so it tracks the skill with
    headroom rather than pinning a number the content has outgrown.
    """

    @model_validator(mode="after")
    def validate_thresholds(self) -> SkillInjectionConfig:
        """Reject a warning threshold above the hard limit.

        Such a config can never warn: the error fires first, so the
        warning is unreachable and the author's intent is ambiguous.
        """
        if (
            self.warn_bytes is not None
            and self.max_bytes is not None
            and self.warn_bytes > self.max_bytes
        ):
            raise ValueError(
                f"skill_injection.warn_bytes ({self.warn_bytes}) must not exceed "
                f"max_bytes ({self.max_bytes}); the error would fire before the "
                "warning could ever be emitted."
            )
        return self


class SkillDiscoveryConfig(BaseModel):
    """Opt in to skills already installed in the user's environment.

    ``skills:`` names skills one at a time. Discovery is the alternative
    for someone who already keeps a personal or team skill library: point
    at *categories* of well-known location and pick up whatever is there.

    Conductor scans the union of both CLIs' locations itself rather than
    asking each provider to discover its own — see
    :mod:`conductor.skills.discovery` for why that distinction is the
    whole point of the feature. Discovered skills join the workflow-level
    default set, so an agent that declares its own ``skills:`` (including
    ``skills: []``) overrides discovery exactly as it overrides
    :attr:`RuntimeConfig.skills`.

    **Off by default**, and worth leaving off unless you want it: an
    ambient set makes the same YAML behave differently on a different
    machine or in CI, which is the opposite of a reproducible run.
    ``conductor validate`` prints the set it puts in effect, so it is at
    least inspectable before you commit the workflow.

    Not usable on every provider. ``claude`` and ``hermes`` have no
    native skill surface and would eagerly inject the entire discovered
    set into every prompt, so ``conductor validate`` rejects the
    combination; ``claude-agent-sdk`` can only load a discovered skill
    that lives inside a Claude Code plugin, and warns about the rest.

    Example YAML::

        runtime:
            skill_discovery:
                sources: [personal, project]
                exclude: [scratch-notes]
    """

    # Frozen for the reason ``SkillInjectionConfig`` and ``ProviderSettings``
    # document: the field validators below are shape checks that do not
    # re-fire on per-attribute assignment under the enclosing
    # ``RuntimeConfig``'s ``validate_assignment=True``. The fields are
    # tuples rather than lists so ``frozen`` means what it says — a list
    # would still allow ``config.sources.append(...)``, and would make the
    # model unhashable despite Pydantic generating ``__hash__`` for it.
    model_config = ConfigDict(extra="forbid", frozen=True)

    sources: tuple[DiscoverySource, ...] = ()
    """Which categories of location to scan. Empty disables discovery.

    * ``personal`` — ``~/.copilot/skills``, ``~/.claude/skills``
    * ``project`` — ``.github/skills`` and ``.claude/skills``, in the
      workflow file's directory and each ancestor up to the repository
      root, or that directory alone when it is not inside a repository

    Scanned in a fixed order (``project``, then ``personal``) whatever
    order they are written in, so reordering this list cannot change
    which of two same-named skills wins.

    There is no ``plugins`` source: taking a plugin's ``skills/`` and
    leaving its subagents and MCP servers behind is the partial load
    ``runtime.plugins`` exists to fix. Name plugins there instead — that
    also reproduces on another machine, which a scan does not.
    """

    exclude: tuple[str, ...] = ()
    """Skill names to drop from the discovered set.

    Applies to discovered skills only. Removing an explicitly declared
    skill is a matter of deleting its line from ``skills:``.
    """

    @field_validator("sources")
    @classmethod
    def validate_sources(cls, v: tuple[DiscoverySource, ...]) -> tuple[DiscoverySource, ...]:
        """Reject a repeated source.

        Listing one twice has no effect, so it always means the author
        believed it would — most likely a merge artefact.
        """
        duplicates = sorted({source for source in v if v.count(source) > 1})
        if duplicates:
            raise ValueError(
                f"skill_discovery.sources contains duplicate entries: {duplicates!r}. "
                "Each source is scanned once regardless."
            )
        return v

    @field_validator("exclude")
    @classmethod
    def validate_exclude(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank exclusions, which match no skill name."""
        for name in v:
            if not name.strip():
                raise ValueError(
                    f"skill_discovery.exclude entries must be non-empty skill names, got {name!r}"
                )
        return v

    @property
    def is_enabled(self) -> bool:
        """Whether any discovery source is active."""
        return bool(self.sources)


class RuntimeConfig(BaseModel):
    """Provider and runtime configuration."""

    model_config = ConfigDict(validate_assignment=True)

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_telemetry(cls, value: Any) -> Any:
        if isinstance(value, dict) and "telemetry" in value:
            raise ValueError(
                "runtime.telemetry was removed; tracing is enabled via the "
                "OTEL_EXPORTER_OTLP_ENDPOINT environment variable"
            )
        return value

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
        le=2.0,
        description="Controls randomness. Range: 0.0-2.0",
    )
    """Temperature parameter for models. Controls randomness in responses."""

    max_tokens: int | None = Field(
        None,
        ge=1,
        le=200000,
        description=(
            "Maximum OUTPUT tokens generated per response (NOT context window limit). "
            "When omitted, the Claude and OpenAI providers apply a unified default of 16384. "
            "Context window: 200K tokens input+output combined (separate from this setting)"
        ),
    )
    """Maximum number of output tokens to generate per response.

    Note: This controls response length, NOT context window. Context trimming
    is handled separately by the workflow engine if needed.

    When omitted, the Claude and OpenAI providers apply a unified default of
    16384 tokens per response.
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

    idle_timeout_seconds: float | None = Field(None, ge=1.0)
    """Time without SDK events before a Copilot session is treated as idle.

    Copilot provider only; other providers ignore this field. Default is
    None, which uses the provider's built-in default (90s). A session is
    only considered idle when no SDK events at all have arrived within the
    window — an in-flight tool call (between ``tool.execution_start`` and
    ``tool.execution_complete``) suppresses the check entirely, since most
    tools emit nothing during execution (see
    ``IdleRecoveryConfig.idle_timeout_seconds`` in ``providers/copilot.py``
    for the full rationale).
    """

    max_idle_recovery_attempts: int | None = Field(None, ge=0)
    """Maximum number of "please continue" prompts sent to an idle Copilot session.

    Copilot provider only; other providers ignore this field. Default is
    None, which uses the provider's built-in default (5). ``0`` means the
    session fails on the first genuine idle timeout without ever injecting
    a recovery prompt.
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

    tool_output: ToolOutputConfig = Field(default_factory=ToolOutputConfig)
    """MCP tool result output-size configuration.

    Controls per-result truncation and spill-to-file behavior for MCP tool
    outputs. Defaults to enabled with a 50000-character limit and spill-to-file
    active. See :class:`ToolOutputConfig`.
    """

    default_context_tier: ContextTier | None = None
    """Workflow-wide default context-window tier (Copilot provider only).

    Each agent may override with its own ``context_tier``. ``long_context``
    selects a model's long-context (e.g. 1M-token) window; ``default`` selects
    the standard tier; ``None`` sends no value.

    Only the Copilot provider forwards this (maps to the SDK's
    ``create_session`` ``context_tier`` param). Other providers ignore it.
    """

    working_dir: str | None = None
    """Workflow-wide default working directory for provider-backed agents.

    Acts as the fallback for every LLM agent that does not set its own
    ``working_dir`` (agent value wins). Supports Jinja2 templating and is
    resolved by the engine against the workflow file's directory before
    reaching the provider. ``conductor validate`` errors when the resolved
    provider declares ``capabilities.working_dir=False``.
    """

    skills: list[str] = Field(default_factory=list)
    """Workflow-wide default skills for every provider-backed agent.

    Each entry is either a registered built-in name (e.g. ``conductor``)
    or a filesystem path — see :attr:`AgentDef.skills` for the full
    resolution rules. Every provider-backed agent inherits this list as
    its default; individual agents override by setting their own
    ``skills:`` field (use ``skills: []`` for explicit opt-out).

    Skill content reaches the model differently per provider:

    * **Copilot** — registered on the SDK session via ``skill_directories``
    * **Claude Agent SDK** — the owning plugin is registered via
      ``--plugin-dir`` and the skill enabled by its ``<plugin>:<skill>``
      name, so the CLI loads it on demand
    * **Claude** — eagerly injected into the rendered prompt inside
      ``<skills><skill name="...">...</skill></skills>`` tags, bounded by
      :attr:`skill_injection`

    Defaults to an empty list (no skills). Conductor ships one built-in
    skill (``conductor``); anything else is referenced by path.

    Example YAML::

        runtime:
            skills:
              - conductor
              - ./team-skills/acme-widgets
    """

    skill_injection: SkillInjectionConfig = Field(default_factory=SkillInjectionConfig)
    """Size limits for *eagerly injected* skill content.

    Only affects providers without a native skill surface (``claude``,
    ``hermes``), where the full skill body is prepended to every agent
    call. Providers with progressive disclosure (``copilot``,
    ``claude-agent-sdk``) send only frontmatter up front and are
    unaffected.
    """

    skill_discovery: SkillDiscoveryConfig = Field(default_factory=SkillDiscoveryConfig)
    """Opt in to skills already installed in the user's environment.

    Off by default. When enabled, the discovered skills join this
    workflow-level default set, so an agent declaring its own ``skills:``
    overrides them along with :attr:`skills`. See
    :class:`SkillDiscoveryConfig`.
    """

    plugin_sources: dict[str, PluginSourceDef] = Field(default_factory=dict)
    """Where the marketplaces named in ``plugins:`` come from.

    This is what makes a workflow using plugins **standalone**. Without
    it a ``plugins:`` entry resolves against machine state — an installed
    plugin, or a path — so a shared workflow needs "first install these"
    in a README, and a teammate who skips that gets an error rather than
    a run.

    Maps a marketplace name to a source. Entries take a string shorthand
    or an object; see :class:`PluginSourceDef`. A ``plugins:`` entry then
    references one as ``plugin@marketplace``.

    A declared source registers its name into the *same* resolution table
    the installed marketplaces populate, so ``prs@acme`` means the same
    thing whether ``acme`` was declared here, installed via the CLI, or is
    a local directory. A declared source wins over an installed
    marketplace of the same name, with a warning when it shadows one.

    Sources are fetched by ``conductor run`` (and by ``conductor plugin
    fetch``); ``conductor validate`` never touches the network and reads
    the cache only.

    Example YAML::

        runtime:
            plugin_sources:
              acme: acme/agent-plugins#v1.4.0
              beta:
                source: git@github.com:beta/plugins.git#3f2a1c9
                path: packages/plugins
              local-dev: ./vendor/plugins
            plugins:
              - prs@acme
              - name: ado@acme
                mcp: false
    """

    plugins: list[PluginDef] = Field(default_factory=list)
    """Workflow-wide default plugins for every provider-backed agent.

    Every provider-backed agent inherits this list unless it sets its own
    ``plugins:`` field (use ``plugins: []`` for explicit opt-out). See
    :attr:`AgentDef.plugins` for entry grammar and per-component
    switches.

    Enabling a plugin here registers its skills, its subagents, and the
    MCP servers it declares — the whole unit the user installed, rather
    than the one component of it Conductor used to load.

    Example YAML::

        runtime:
            plugins:
              - prs
              - name: ado
                mcp: false
    """

    @field_validator("plugins", mode="before")
    @classmethod
    def _coerce_plugins(cls, value: Any) -> Any:
        """Expand ``- prs`` string shorthands into ``{name: prs}``."""
        return _coerce_plugin_entries(value)

    @field_validator("plugin_sources", mode="before")
    @classmethod
    def _coerce_plugin_sources(cls, value: Any) -> Any:
        """Expand ``acme: owner/repo`` shorthands into ``{source: ...}``."""
        return _coerce_plugin_sources(value)

    @field_validator("plugin_sources")
    @classmethod
    def validate_plugin_sources(cls, v: dict[str, PluginSourceDef]) -> dict[str, PluginSourceDef]:
        """Check each marketplace name is usable after an ``@``."""
        return _validate_plugin_source_names(v)

    @field_validator("plugins")
    @classmethod
    def validate_plugins(cls, v: list[PluginDef]) -> list[PluginDef]:
        """Reject duplicate workflow-default ``plugins:`` entries."""
        return _validate_plugin_entries(v)

    @field_validator("skills")
    @classmethod
    def validate_skills(cls, v: list[str]) -> list[str]:
        """Validate workflow-default ``skills:`` entry shape and built-in names."""
        return _validate_skill_entries(v)


_REMOVED_WORKFLOW_FIELDS: dict[str, str] = {
    "hooks": (
        "`workflow.hooks:` (on_start/on_complete/on_error) was removed in #476. "
        "The hook templates were rendered and then discarded, so the block never "
        "had any observable effect. Remove it from your workflow. For "
        "completion or failure notification, subscribe to the `workflow_completed` "
        "/ `workflow_failed` events in the JSONL event log instead."
    ),
}


class WorkflowDef(BaseModel):
    """Top-level workflow configuration."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_fields(cls, data: Any) -> Any:
        """Give a targeted error for fields removed from the schema.

        ``extra="forbid"`` already rejects an unknown ``hooks:`` key, but its
        generic "extra inputs are not permitted" message reads like a typo and
        points the user back at the (now-deleted) docs. Naming the removal and
        the replacement keeps an upgrading workflow from silently misdiagnosing
        the failure. See #476.
        """
        if isinstance(data, dict):
            for key, guidance in _REMOVED_WORKFLOW_FIELDS.items():
                if key in data:
                    raise ValueError(guidance)
        return data

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

    mcp: McpConfig = Field(default_factory=McpConfig)
    """Exposure settings for ``conductor mcp serve``.

    Absent from the YAML behaves identically to an explicit default block
    (``expose: true``), so no existing workflow needs editing to keep its
    current behavior once MCP exposure defaults on (DD4).
    """

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

    agents: list[StepDef]
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
            for route in getattr(agent, "routes", []):
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

    @model_validator(mode="after")
    def validate_root_level_output_required(self) -> WorkflowConfig:
        """Reject top-level optional output fields on agents and for-each agents.

        Object properties may still be optional; the policy only applies to the
        root output dict of an agent definition.
        """
        for agent in self.agents:
            output = getattr(agent, "output", None)
            if output:
                for field_name, field in output.items():
                    if not field.required:
                        raise ValueError(
                            f"Agent '{agent.name}' output field '{field_name}': "
                            "root-level output fields cannot be optional "
                            "(required: false is only allowed inside object properties)"
                        )
        for for_each_group in self.for_each:
            agent = for_each_group.agent
            output = getattr(agent, "output", None)
            if output:
                for field_name, field in output.items():
                    if not field.required:
                        raise ValueError(
                            f"Agent '{agent.name}' output field '{field_name}': "
                            "root-level output fields cannot be optional "
                            "(required: false is only allowed inside object properties)"
                        )
        return self
