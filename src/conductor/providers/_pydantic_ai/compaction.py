"""Tiered context-window compaction for Pydantic AI agents.

This module assembles the harness's :class:`TieredCompaction` into a conductor
wrapper that:

1. Gates compaction on a reserve-based token trigger (window minus the output
   limit minus the tool buffer).
2. Wraps each escalation tier individually so a failing LLM summarizer still
   yields to the deterministic sliding-window fallback.
3. Fails open: any unexpected error in the gate or tier chain is logged and the
   original request context is returned unchanged, so a compaction bug never
   aborts a workflow run.
4. Emits ``agent_compaction_start`` / ``agent_compaction_complete`` events through
   the per-execute callback so the console, JSONL log, and dashboard can observe
   compaction activity.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
from pydantic_ai.tools import RunContext

from conductor.providers._pydantic_ai.events import (
    emit_compaction_complete,
    emit_compaction_complete_error,
    emit_compaction_start,
)

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


def _estimate_after_compaction_tokens(
    before_messages: list[ModelMessage],
    after_messages: list[ModelMessage],
    before_estimate: int,
) -> int:
    """Estimate the post-compaction token count, compensating for the anchor.

    ``estimate_context_tokens`` anchors on the most recent ``ModelResponse``
    with provider-reported ``usage.input_tokens``. That anchoring response
    survives compaction (every tier keeps the recent tail), so a naive
    after-estimate still describes the pre-rewrite request and always reports
    ``after == before`` — i.e. ``tokens_saved == 0`` — no matter how much
    history was dropped. ``TieredCompaction._escalate`` compensates for this
    internally by subtracting the tier's measured heuristic reclaim from its
    anchored baseline; this helper mirrors that compensation so the telemetry
    reports the same numbers the escalation loop acted on.
    """
    from pydantic_ai_harness.compaction import estimate_token_count

    reclaimed = estimate_token_count(before_messages, None) - estimate_token_count(
        after_messages, None
    )
    return max(before_estimate - reclaimed, 0)


class _TierWrapper(AbstractCapability[Any]):
    """Wrap a single compaction tier so its failure does not stop the chain.

    ``TieredCompaction._escalate`` calls each tier's ``compact`` directly and
    does not catch exceptions.  Wrapping ``compact`` lets a failing summarizer
    still yield to the deterministic sliding-window fallback. A tier that
    raised sets ``failed`` so the outer wrapper can name the degraded tiers in
    the ``agent_compaction_complete`` event instead of reporting false success;
    the flag is reset by the outer wrapper before every request.
    """

    def __init__(self, inner: Any, *, tier_name: str) -> None:
        self._inner = inner
        self._tier_name = tier_name
        self.failed = False

    async def compact(
        self,
        messages: list[ModelMessage],
        ctx: RunContext[Any],
    ) -> list[ModelMessage]:
        try:
            return await self._inner.compact(messages, ctx)
        except Exception as exc:  # noqa: BLE001 - tier failure is recoverable
            self.failed = True
            logger.warning(
                "Compaction tier %s raised %s; continuing to next tier.",
                self._tier_name,
                type(exc).__name__,
                exc_info=True,
            )
            return messages


class _FailOpenCompactionWrapper(AbstractCapability[Any]):
    """Outer gate + fail-open wrapper around the tiered strategy.

    The token gate lives here rather than in a separate capability: measuring
    the context once per request (not once in a gate and again inside the
    inner strategy's own trigger check) halves the estimator work.

    Failure handling is zoned:

    - **Gate measurement failure** — the before-estimate itself raised. Log a
      warning and return the context unchanged; no event and no disable latch,
      because a broken estimate says nothing about the compaction path.
    - **Inner strategy failure** — log, emit an errored
      ``agent_compaction_complete``, engage the per-execution disable latch,
      and return the original context unchanged.
    - **After-telemetry failure** — compaction already happened, so the
      compacted result is returned; a warning is logged but no errored event
      is emitted and the latch stays off.

    A per-execution disable latch is set after an *inner* failure so the
    wrapper short-circuits on subsequent requests rather than retrying a
    deterministically broken compaction path.
    """

    def __init__(
        self,
        inner: AbstractCapability[Any],
        *,
        config: CompactionConfig,
        tier_wrappers: list[_TierWrapper] | None = None,
    ) -> None:
        self._inner = inner
        self._config = config
        self._tier_wrappers: list[_TierWrapper] = tier_wrappers or []
        self._disabled = False

    def _on_before(self, estimate: int, messages_before: int) -> None:
        """Emit ``agent_compaction_start`` through the per-execute callback."""
        emit_compaction_start(
            self._config.event_callback,
            agent_name=self._config.agent_name,
            strategy="tiered",
            model=self._config.model_name,
            context_window=self._config.window_tokens,
            context_window_source=self._config.window_source,
            output_limit=self._config.output_limit_tokens,
            output_limit_source=self._config.output_limit_source,
            trigger_tokens=self._config.trigger_tokens,
            target_tokens=self._config.target_tokens,
            messages_before=messages_before,
            tokens_before=estimate,
        )

    def _on_after(
        self,
        *,
        before_messages: list[ModelMessage],
        after_messages: list[ModelMessage],
        before_estimate: int,
        after_estimate: int,
        elapsed_seconds: float,
        degraded_tiers: list[str],
    ) -> None:
        """Emit a success-shaped ``agent_compaction_complete`` event."""
        emit_compaction_complete(
            self._config.event_callback,
            agent_name=self._config.agent_name,
            strategy="tiered",
            model=self._config.model_name,
            context_window=self._config.window_tokens,
            context_window_source=self._config.window_source,
            messages_before=len(before_messages),
            messages_after=len(after_messages),
            tokens_before=before_estimate,
            tokens_after=after_estimate,
            elapsed=elapsed_seconds,
            degraded_tiers=degraded_tiers,
            still_over_trigger=after_estimate > self._config.trigger_tokens,
        )

    def _on_error(self, exc: Exception) -> None:
        """Emit an errored ``agent_compaction_complete`` event and disable compaction."""
        self._disabled = True
        emit_compaction_complete_error(
            self._config.event_callback,
            agent_name=self._config.agent_name,
            strategy="tiered",
            model=self._config.model_name,
            exc=exc,
            context_window=self._config.window_tokens,
            context_window_source=self._config.window_source,
        )

    async def before_model_request(
        self,
        ctx: RunContext[Any],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        if self._disabled:
            return request_context

        # Zone (a): gate measurement. A broken estimate says nothing about
        # the compaction path, so this warns and skips compaction for this
        # request only — no errored event, no disable latch.
        try:
            before_messages = list(request_context.messages)
            before_estimate = await _estimate_context_tokens(
                before_messages,
                request_context.model_request_parameters,
            )
        except Exception:  # noqa: BLE001 - estimation must never fail the run
            logger.warning(
                "Compaction gate measurement failed for agent %r; "
                "skipping compaction for this request.",
                self._config.agent_name,
                exc_info=True,
            )
            return request_context

        # Gate on the token estimate, not the message count: one large
        # prompt can exceed the trigger with nothing to drop.
        if before_estimate <= self._config.trigger_tokens:
            return request_context

        self._on_before(estimate=before_estimate, messages_before=len(before_messages))
        for tier in self._tier_wrappers:
            tier.failed = False
        start = time.monotonic()

        # Zone (b): inner strategy. Fail open with the original context, emit
        # the errored event, and latch the per-execution disable flag.
        try:
            result = await self._inner.before_model_request(ctx, request_context)
        except Exception as exc:  # noqa: BLE001 - compaction must never fail the run
            logger.warning(
                "Compaction failed for agent %r: %s. Continuing without compaction.",
                self._config.agent_name,
                exc,
                exc_info=True,
            )
            self._on_error(exc)
            return request_context

        degraded_tiers = [t._tier_name for t in self._tier_wrappers if t.failed]

        # Zone (c): after-telemetry. Compaction already happened, so a failure
        # here must still return the compacted result — warn, emit nothing,
        # and leave the latch off.
        try:
            after_estimate = _estimate_after_compaction_tokens(
                before_messages,
                list(result.messages),
                before_estimate,
            )
            self._on_after(
                before_messages=before_messages,
                after_messages=list(result.messages),
                before_estimate=before_estimate,
                after_estimate=after_estimate,
                elapsed_seconds=time.monotonic() - start,
                degraded_tiers=degraded_tiers,
            )
        except Exception:  # noqa: BLE001 - telemetry must never fail the run
            logger.warning(
                "Compaction telemetry failed for agent %r; keeping compacted context.",
                self._config.agent_name,
                exc_info=True,
            )
        return result


def build_tiered_compaction(config: CompactionConfig) -> AbstractCapability[Any]:
    """Assemble the tiered compaction capability stack.

    The returned capability is safe to pass to :class:`pydantic_ai.Agent` via
    ``capabilities=[wrapper]``.

    The stack is, from outside in:

    1. ``_FailOpenCompactionWrapper`` — owns the token gate (measured once per
       request), catches unexpected errors, and returns the original context
       unchanged when the inner strategy fails.
    2. ``TieredCompaction`` — escalates through the three tiers.
    3. Per-tier wrappers around ``ClearToolResults``, ``SummarizingCompaction``,
       and ``SlidingWindowCompaction`` so a non-final tier failure still proceeds
       to the deterministic final tier and is named in ``degraded_tiers``.
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

    return _FailOpenCompactionWrapper(
        tiered,
        config=config,
        tier_wrappers=[clear_tier, summarize_tier, slide_tier],
    )


__all__ = [
    "CompactionConfig",
    "build_tiered_compaction",
]
