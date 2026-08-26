"""Build a Pydantic AI Agent from a Conductor ``AgentDef``.

This module provides the factory that maps Conductor agent configuration
(model, system prompt, output schema, tools, temperature, reasoning, etc.)
to a Pydantic AI ``Agent``.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from anthropic import NOT_GIVEN as ANTHROPIC_NOT_GIVEN
from anthropic import AsyncAnthropic
from anthropic.types.beta.beta_thinking_config_enabled_param import (
    BetaThinkingConfigEnabledParam,
)
from pydantic_ai import Agent, AgentRetries
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.output import ToolOutput
from pydantic_ai.providers.anthropic import AnthropicProvider

from conductor.exceptions import ValidationError
from conductor.providers._pydantic_ai.converters import (
    _sanitize_json_schema,
    output_schema_to_pydantic_model,
)
from conductor.providers.reasoning import (
    ReasoningEffort,
    effort_to_budget_tokens,
    is_claude_thinking_model,
    resolve_reasoning_effort,
)

if TYPE_CHECKING:
    from pydantic_ai import AgentToolset

    from conductor.config.schema import AgentDef

logger = logging.getLogger(__name__)


# Default model mirrors ClaudeProvider's conservative default to avoid dated
# model deprecation risk. The "-latest" suffix lets Anthropic aliases keep the
# identifier current without YAML changes.
DEFAULT_ANTHROPIC_MODEL: str = "claude-3-5-sonnet-latest"

# Default cap for total output tokens when Anthropic extended thinking is
# enabled. This matches CLAUDE_EXTENDED_THINKING_OUTPUT_CAP in reasoning.py
# and is used by the temperature/max_tokens coercion helper.
_ANTHROPIC_THINKING_OUTPUT_CAP: int = 64_000

# Headroom above the thinking budget required by the Anthropic API:
# ``max_tokens > budget_tokens``. Matches CLAUDE_ANSWER_HEADROOM_TOKENS.
_ANTHROPIC_THINKING_HEADROOM: int = 4_096

# Pydantic AI v2 splits the ``Agent(retries=...)`` budget into tool retries and
# output retries. Tool retries must stay at 0 because Conductor's
# ``execute_with_retry`` is the sole retry layer for API and tool failures.
# Output retries, however, must be > 0 so that structured-output agents recover
# when the model answers with plain text instead of calling the output tool.
# This value matches the legacy ``RetryConfig.max_parse_recovery_attempts=2``
# default used by ClaudeProvider.
_OUTPUT_RECOVERY_RETRIES: int = 2


def _resolve_anthropic_model(
    agent: AgentDef,
    default_model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    auth_token: str | None = None,
    timeout: float | None = None,
) -> AnthropicModel:
    """Build a Pydantic AI ``AnthropicModel`` from the agent definition.

    Resolves the model identifier, API key, optional base URL, auth token, and
    timeout, mirroring the client construction semantics of ``ClaudeProvider``.
    Transport-level retries are explicitly disabled because Conductor's own
    retry layer is the sole retry mechanism.

    Args:
        agent: The Conductor agent definition.
        default_model: Fallback model identifier when ``agent.model`` is unset.
        api_key: Anthropic API key. When either credential is passed
            explicitly, neither ``ANTHROPIC_API_KEY`` nor
            ``ANTHROPIC_AUTH_TOKEN`` is consulted (SDK unit semantics).
        base_url: Optional custom API endpoint.
        auth_token: Optional bearer-auth token for gateway / LiteLLM
            endpoints. Falls back to ``ANTHROPIC_AUTH_TOKEN`` env var only
            when no credential is passed explicitly.
        timeout: Request timeout in seconds. ``None`` lets the Anthropic SDK
            apply its own default.

    Returns:
        A configured Pydantic AI ``AnthropicModel`` instance.

    Raises:
        ValidationError: If no API key or auth token is available.
    """
    # The Anthropic SDK resolves credentials as a unit: passing either
    # credential explicitly disables env-var resolution for both. Mirror that
    # here so an explicit credential never mixes with an ambient env
    # credential — otherwise e.g. a YAML auth_token plus an ambient
    # ANTHROPIC_API_KEY would send both headers to whatever base_url points
    # at, leaking the user's Anthropic key to a gateway.
    if api_key is not None or auth_token is not None:
        effective_api_key = api_key
        effective_auth_token = auth_token
    else:
        effective_api_key = os.environ.get("ANTHROPIC_API_KEY")
        effective_auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")

    if effective_api_key and effective_auth_token:
        logger.warning(
            "Both api_key and auth_token are set; the Anthropic SDK sends both "
            "X-Api-Key and Authorization: Bearer headers on every request, so "
            "the api_key reaches whatever base_url points at. Set exactly one."
        )

    if not effective_api_key and not effective_auth_token:
        raise ValidationError(
            "ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN environment variables are not set "
            "and no api_key or auth_token was provided",
            suggestion="Set ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN, "
            "or pass api_key/auth_token to the provider.",
        )

    model_name = (agent.model or default_model) or DEFAULT_ANTHROPIC_MODEL

    # Disable transport-level retries; Conductor's execute_with_retry is the
    # only retry layer allowed by the provider plan (Must-NOT-Have double retry).
    anthropic_client = AsyncAnthropic(
        api_key=effective_api_key,
        auth_token=effective_auth_token,
        base_url=base_url,
        timeout=timeout if timeout is not None else ANTHROPIC_NOT_GIVEN,
        max_retries=0,
    )
    provider = AnthropicProvider(anthropic_client=anthropic_client)
    return AnthropicModel(model_name=model_name, provider=provider)


def _build_output_type(
    agent: AgentDef,
) -> type[Any] | ToolOutput[Any] | None:
    """Translate the agent's ``output`` schema into a Pydantic AI output spec.

    An empty or missing schema means plain text output (``None`` is returned
    so callers fall back to the default ``str`` output). Otherwise, the schema
    is converted to a dynamic Pydantic model and wrapped in ``ToolOutput`` to
    request tool-based structured output as required by the provider plan.

    Args:
        agent: The Conductor agent definition.

    Returns:
        A ``ToolOutput`` wrapping the dynamic model, or ``None`` for text output.
    """
    dynamic_model = output_schema_to_pydantic_model(
        f"{agent.name}Output",
        agent.output,
    )
    if dynamic_model is None:
        return None
    return ToolOutput(dynamic_model)


def _resolve_anthropic_thinking(
    agent: AgentDef,
    model: str,
    default_reasoning_effort: ReasoningEffort | None,
) -> dict[str, Any] | None:
    """Resolve extended-thinking kwargs for AnthropicModelSettings.

    Combines the per-agent ``reasoning.effort`` with the workflow-wide default,
    validates that the model supports extended thinking, and returns the raw
    ``anthropic_thinking`` dict that Pydantic AI forwards to the Anthropic SDK.

    Args:
        agent: The Conductor agent definition.
        model: Resolved model identifier.
        default_reasoning_effort: Workflow-wide default reasoning effort.

    Returns:
        ``{"type": "enabled", "budget_tokens": N}`` when reasoning is requested,
        or ``None`` when no reasoning effort is configured.

    Raises:
        ValidationError: If reasoning effort is requested for a model that does
            not support extended thinking.
    """
    effort = resolve_reasoning_effort(agent, default_reasoning_effort)
    if effort is None:
        return None
    if not is_claude_thinking_model(model):
        raise ValidationError(
            f"Model {model!r} does not support extended thinking, but "
            f"reasoning.effort={effort!r} was requested for agent "
            f"{agent.name!r}.",
            suggestion=(
                "Use a Claude 3.7+ or 4.x model (e.g. claude-3-7-sonnet-latest, "
                "claude-opus-4-20250514) or remove the reasoning config."
            ),
        )
    return {"type": "enabled", "budget_tokens": effort_to_budget_tokens(effort)}


def _coerce_for_thinking(
    temperature: float | None,
    max_tokens: int | None,
    thinking: dict[str, Any] | None,
    model: str,
) -> tuple[float | None, int | None]:
    """Adjust temperature and max_tokens to satisfy Anthropic thinking constraints.

    When extended thinking is enabled, the Anthropic API requires
    ``temperature == 1.0`` (or omitted) and ``max_tokens > budget_tokens``.
    This helper mirrors ``ClaudeProvider._coerce_for_thinking`` by forcing the
    temperature to 1.0 and bumping ``max_tokens`` to at least the thinking
    budget plus headroom, clamped to the extended-thinking output cap.

    Args:
        temperature: User-configured temperature (may be ``None``).
        max_tokens: User-configured max output tokens (may be ``None``).
        thinking: Resolved ``anthropic_thinking`` dict or ``None``.
        model: Resolved model identifier (used only for log messages).

    Returns:
        Tuple of ``(effective_temperature, effective_max_tokens)``.

    Raises:
        ValidationError: If the per-model cap cannot satisfy the thinking budget.
    """
    if thinking is None:
        return temperature, max_tokens

    budget = int(thinking.get("budget_tokens", 0))
    effective_max_tokens = max_tokens if max_tokens is not None else 0
    required = budget + _ANTHROPIC_THINKING_HEADROOM
    if max_tokens is not None and effective_max_tokens < required:
        logger.info(
            "Raising max_tokens from %s to %s for extended thinking on model %s "
            "(budget_tokens=%s + headroom=%s)",
            max_tokens,
            required,
            model,
            budget,
            _ANTHROPIC_THINKING_HEADROOM,
        )
    effective_max_tokens = max(effective_max_tokens, required)
    if effective_max_tokens > _ANTHROPIC_THINKING_OUTPUT_CAP:
        logger.info(
            "Clamping max_tokens %s to %s for extended thinking on model %s "
            "(Anthropic API per-model cap)",
            effective_max_tokens,
            _ANTHROPIC_THINKING_OUTPUT_CAP,
            model,
        )
        effective_max_tokens = _ANTHROPIC_THINKING_OUTPUT_CAP
    if effective_max_tokens <= budget:
        raise ValidationError(
            f"Cannot satisfy thinking budget_tokens={budget} on model "
            f"{model!r}: per-model cap {_ANTHROPIC_THINKING_OUTPUT_CAP} is not greater "
            f"than the requested budget.",
            suggestion="Lower reasoning.effort or use a model with a higher cap.",
        )

    if temperature is not None and temperature != 1.0:
        logger.info(
            "Coercing temperature %s to 1.0 for extended thinking on model %s "
            "(Anthropic API requirement)",
            temperature,
            model,
        )

    return 1.0, effective_max_tokens


def _build_model_settings(
    agent: AgentDef,
    default_temperature: float | None,
    default_max_tokens: int | None,
    default_reasoning_effort: ReasoningEffort | None,
    default_model: str | None = None,
    timeout: float | None = None,
) -> AnthropicModelSettings:
    """Build ``AnthropicModelSettings`` from the agent and runtime defaults.

    Args:
        agent: The Conductor agent definition.
        default_temperature: Workflow-level default temperature.
        default_max_tokens: Workflow-level default ``max_tokens``.
        default_reasoning_effort: Workflow-wide default reasoning effort.
        default_model: Fallback model identifier when ``agent.model`` is unset.
            Used for the thinking-support check so it matches the model the run
            will actually use instead of the hardcoded library default.
        timeout: Optional per-request timeout in seconds. When set, it is
            forwarded to the model settings so individual model calls are
            bounded even when the caller does not wrap the run in its own
            timeout.

    Returns:
        A TypedDict of Anthropic-specific settings ready for ``Agent``.
    """
    model_name = (agent.model or default_model) or DEFAULT_ANTHROPIC_MODEL

    thinking = _resolve_anthropic_thinking(
        agent,
        model_name,
        default_reasoning_effort,
    )

    agent_temperature = getattr(agent, "temperature", None)
    temperature = agent_temperature if agent_temperature is not None else default_temperature
    agent_max_tokens = getattr(agent, "max_tokens", None)
    max_tokens = agent_max_tokens if agent_max_tokens is not None else default_max_tokens

    effective_temperature, effective_max_tokens = _coerce_for_thinking(
        temperature,
        max_tokens,
        thinking,
        model_name,
    )

    settings: AnthropicModelSettings = AnthropicModelSettings()
    if effective_temperature is not None:
        settings["temperature"] = effective_temperature
    if effective_max_tokens is not None:
        settings["max_tokens"] = effective_max_tokens
    if timeout is not None:
        settings["timeout"] = timeout
    if thinking is not None:
        settings["anthropic_thinking"] = BetaThinkingConfigEnabledParam(
            type="enabled",
            budget_tokens=thinking["budget_tokens"],
        )
    return settings


def build_agent(
    agent: AgentDef,
    system_prompt: str,
    rendered_prompt: str,
    default_model: str | None = None,
    default_temperature: float | None = None,
    default_max_tokens: int | None = None,
    default_reasoning_effort: ReasoningEffort | None = None,
    max_parse_recovery_attempts: int = _OUTPUT_RECOVERY_RETRIES,
    api_key: str | None = None,
    auth_token: str | None = None,
    base_url: str | None = None,
    timeout: float | None = None,
    toolsets: list[AgentToolset[Any]] | None = None,
    tools: list[Any] | None = None,
) -> Agent[Any, Any]:
    """Build a Pydantic AI Agent from a Conductor agent definition.

    Args:
        agent: The Conductor agent definition.
        system_prompt: The rendered system prompt/instructions.
        rendered_prompt: The rendered user prompt (used as the initial task).
        default_model: Fallback model identifier when ``agent.model`` is unset.
        default_temperature: Workflow-level default temperature.
        default_max_tokens: Workflow-level default ``max_tokens``.
        default_reasoning_effort: Workflow-level default reasoning effort.
        max_parse_recovery_attempts: Output correction retries handled inside
            the Pydantic AI agent.
        api_key: Anthropic API key. Falls back to ``ANTHROPIC_API_KEY`` env var.
        auth_token: Optional bearer-auth token for gateway / LiteLLM endpoints.
        base_url: Optional custom API endpoint.
        timeout: Request timeout in seconds. ``None`` lets the Anthropic SDK use
            its own default.
        toolsets: Optional Pydantic AI toolsets to register (e.g. the MCP tool
            bridge).
        tools: Optional plain Pydantic AI tools to register.

    Returns:
        A configured Pydantic AI ``Agent`` ready to run.
    """
    model = _resolve_anthropic_model(
        agent, default_model, api_key, base_url, auth_token=auth_token, timeout=timeout
    )

    output_type = _build_output_type(agent)
    if output_type is None:
        output_type = str

    model_settings = _build_model_settings(
        agent,
        default_temperature,
        default_max_tokens,
        default_reasoning_effort,
        default_model=default_model,
        timeout=timeout,
    )

    pydantic_agent: Agent[Any, Any] = Agent(
        model=model,
        output_type=output_type,
        system_prompt=system_prompt,
        name=agent.name,
        description=agent.description,
        model_settings=model_settings,
        retries=AgentRetries(tools=0, output=max_parse_recovery_attempts),
        toolsets=toolsets or [],
        tools=tools or [],
    )

    if isinstance(pydantic_agent.output_type, ToolOutput):
        toolset = pydantic_agent._output_schema.toolset
        if toolset is not None:
            for tool_def in toolset._tool_defs:
                _sanitize_json_schema(tool_def.parameters_json_schema)

    return pydantic_agent
