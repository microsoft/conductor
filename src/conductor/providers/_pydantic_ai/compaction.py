"""Tiered context-window compaction for Pydantic AI agents.

This module assembles the harness's :class:`TieredCompaction` into a conductor
wrapper that:

1. Gates compaction on a reserve-based token trigger (window minus max(output
   limit, buffer)).
2. Wraps each escalation tier individually so a failing LLM summarizer still
   yields to the deterministic sliding-window fallback.
3. Fails open: any unexpected error in the gate or tier chain is logged and the
   original request context is returned unchanged, so a compaction bug never
   aborts a workflow run.

Event emission hooks are provided but left empty here; todo 5 fills them in.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
from pydantic_ai.tools import RunContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CompactionConfig:
    """Resolved compaction parameters for one agent execution.

    This is a plain data carrier so that the provider layer can
    resolve windows/limits asynchronously (called from provider ``execute()``
    before each agent run) and then hand them to
    :func:`build_tiered_compaction` inside the synchronous ``build_agent`` call.
    """

    window_tokens: int
    """Resolved context-window size in tokens."""

    window_source: str
    """Source label for the resolved window (e.g. ``"provider"``)."""

    output_limit_tokens: int
    """Resolved output-token cap in tokens."""

    output_limit_source: str
    """Source label for the resolved output limit (e.g. ``"default"``)."""

    trigger_tokens: int
    """Reserve-based trigger at which compaction fires."""

    target_tokens: int
    """Escalation-ceiling target passed to the harness tiers."""

    event_callback: Any
    """Per-execute Conductor event callback (filled in by the runner closure)."""

    agent_name: str
    """Conductor agent name, used in event payloads."""

    model_name: str
    """Resolved model name, used in event payloads."""


async def _estimate_context_tokens(
    messages: list[ModelMessage],
    model_request_parameters: ModelRequestParameters | None,
) -> int:
    """Primary token estimator using the harness helper.

    Counts message parts, instructions, and conservative tool-schema overhead
    so the gate measures the same quantity the inner tiers measure.
    """
    from pydantic_ai_harness.compaction import (
        estimate_context_tokens,
    )

    return estimate_context_tokens(
        messages,
        tokenizer=None,
        model_request_parameters=model_request_parameters,
    )


def _last_response_input_tokens(messages: list[ModelMessage]) -> int | None:
    """Cheap short-circuit estimate from the most recent response's usage.

    This is only used as a pre-check: if even the anchored previous request is
    already over the trigger, we know compaction is needed without running the
    full estimator. It must not be the sole estimator because it misses the
    suffix appended after the anchor (new user/tool messages).
    """
    for message in reversed(messages):
        if isinstance(message, ModelResponse):
            usage = message.usage
            return usage.input_tokens or None
    return None


class _TierWrapper(AbstractCapability[Any]):
    """Wrap a single compaction tier so its failure does not stop the chain.

    ``TieredCompaction._escalate`` calls each tier's ``compact`` directly and
    does not catch exceptions.  Wrapping ``compact`` lets a failing summarizer
    still yield to the deterministic sliding-window fallback.
    """

    def __init__(self, inner: Any, *, tier_name: str) -> None:
        self._inner = inner
        self._tier_name = tier_name

    async def compact(
        self,
        messages: list[ModelMessage],
        ctx: RunContext[Any],
    ) -> list[ModelMessage]:
        try:
            return await self._inner.compact(messages, ctx)
        except Exception as exc:  # noqa: BLE001 - tier failure is recoverable
            logger.warning(
                "Compaction tier %s raised %s; continuing to next tier.",
                self._tier_name,
                type(exc).__name__,
                exc_info=True,
            )
            return messages


class _ThresholdGatedCompaction(AbstractCapability[Any]):
    """Reserve-based gate that delegates to the inner tiered strategy when over budget.

    The primary estimator is ``estimate_context_tokens``; the last-response usage
    anchor is used only as a cheap short-circuit pre-check.
    """

    def __init__(
        self,
        inner: AbstractCapability[Any],
        *,
        trigger_tokens: int,
    ) -> None:
        self._inner = inner
        self._trigger_tokens = trigger_tokens

    async def before_model_request(
        self,
        ctx: RunContext[Any],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        messages = list(request_context.messages)

        anchor = _last_response_input_tokens(messages)
        if anchor is not None and anchor <= self._trigger_tokens:
            # The cheap pre-check says we are below the trigger; do the full
            # estimate to be sure, because the suffix after the anchor may push
            # us over.
            estimate = await _estimate_context_tokens(
                messages,
                request_context.model_request_parameters,
            )
            if estimate <= self._trigger_tokens:
                return request_context
        else:
            estimate = await _estimate_context_tokens(
                messages,
                request_context.model_request_parameters,
            )
            if estimate <= self._trigger_tokens:
                return request_context

        # Above trigger: delegate to the inner tiered strategy.
        return await self._inner.before_model_request(ctx, request_context)


class _FailOpenCompactionWrapper(AbstractCapability[Any]):
    """Outer fail-open wrapper: any escaping exception returns the original context.

    This is the last line of defense.  It catches errors from the gate estimator
    or from the final deterministic tier so that a compaction bug can never abort
    a workflow run.

    The wrapper carries event-emission hooks (``_on_before`` / ``_on_after``)
    so todo 5 can add start/complete events without changing this class's
    shape.
    """

    def __init__(
        self,
        inner: AbstractCapability[Any],
        *,
        config: CompactionConfig,
    ) -> None:
        self._inner = inner
        self._config = config

    def _on_before(self, messages: list[ModelMessage], estimate: int) -> None:
        """Hook called before compaction runs.  Todo 5 will emit start events here."""

    def _on_after(
        self,
        *,
        before_messages: list[ModelMessage],
        after_messages: list[ModelMessage],
        before_estimate: int,
        after_estimate: int,
        elapsed_seconds: float,
    ) -> None:
        """Hook called after compaction succeeds.  Todo 5 will emit complete events here."""

    def _on_error(self, exc: Exception) -> None:
        """Hook called when compaction fails.  Todo 5 will emit errored complete events here."""

    async def before_model_request(
        self,
        ctx: RunContext[Any],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        try:
            return await self._inner.before_model_request(ctx, request_context)
        except Exception as exc:  # noqa: BLE001 - compaction must never fail the run
            logger.warning(
                "Compaction failed for agent %r: %s. Continuing without compaction.",
                self._config.agent_name,
                exc,
                exc_info=True,
            )
            self._on_error(exc)
            return request_context


def build_tiered_compaction(config: CompactionConfig) -> AbstractCapability[Any]:
    """Assemble the tiered compaction capability stack.

    The returned capability is safe to pass to :class:`pydantic_ai.Agent` via
    ``capabilities=[wrapper]``.

    The stack is, from outside in:

    1. ``_FailOpenCompactionWrapper`` — catches unexpected errors and returns the
       original context unchanged.
    2. ``_ThresholdGatedCompaction`` — only invokes the inner strategy when the
       estimated context size exceeds the trigger.
    3. ``TieredCompaction`` — escalates through the three tiers.
    4. Per-tier wrappers around ``ClearToolResults``, ``SummarizingCompaction``,
       and ``SlidingWindowCompaction`` so a non-final tier failure still proceeds
       to the deterministic final tier.
    """
    from pydantic_ai_harness.compaction import (
        ClearToolResults,
        SlidingWindowCompaction,
        SummarizingCompaction,
        TieredCompaction,
    )

    # Tier parameters are taken from the plan and from the harness docs:
    # - ClearToolResults keeps the most recent N tool-call/result pairs.
    # - SummarizingCompaction preserves the most recent 20 messages when it
    #   replaces older history with a summary.
    # - SlidingWindowCompaction preserves the most recent 20 messages when it
    #   trims older history.
    # max_messages=1 is the harness placeholder used when the tier is driven by
    # an outer gate (its own trigger is bypassed inside TieredCompaction).
    clear_tier = _TierWrapper(
        ClearToolResults(max_messages=1, keep_pairs=3),
        tier_name="clear_tool_results",
    )
    summarize_tier = _TierWrapper(
        SummarizingCompaction(max_messages=1, keep_messages=20, model=None),
        tier_name="summarizing",
    )
    slide_tier = _TierWrapper(
        SlidingWindowCompaction(max_messages=1, keep_messages=20),
        tier_name="sliding_window",
    )

    tiered = TieredCompaction(
        tiers=[clear_tier, summarize_tier, slide_tier],
        target_tokens=config.target_tokens,
        tokenizer=None,
    )

    gated = _ThresholdGatedCompaction(
        tiered,
        trigger_tokens=config.trigger_tokens,
    )

    return _FailOpenCompactionWrapper(gated, config=config)


__all__ = [
    "CompactionConfig",
    "build_tiered_compaction",
]
