"""Tiered context-window compaction for Pydantic AI agents.

This module assembles the harness's :class:`TieredCompaction` into a conductor
wrapper that:

1. Gates compaction on two independent measurements: a reserve-based token
   trigger (window minus the output limit minus the tool buffer), measured by
   the primary estimator, and a hard context-window guard, measured by a
   density-calibrated safety estimate for content the primary heuristic
   undercounts.
2. Wraps each escalation tier individually so a failing LLM summarizer still
   yields to the deterministic sliding-window fallback.
3. Fails open: a failed primary measurement falls back to an independent
   density-calibrated estimate; only when both estimators fail is the original
   request context returned unchanged (with an ``agent_compaction_skipped``
   event). Any unexpected error in the tier chain is likewise logged and
   returned unchanged, so a compaction bug never aborts a workflow run.
4. Emits ``agent_compaction_start`` / ``agent_compaction_complete`` events through
   the per-execute callback so the console, JSONL log, and dashboard can observe
   compaction activity.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
from pydantic_ai.tools import RunContext

from conductor.providers._pydantic_ai.events import (
    emit_compaction_complete,
    emit_compaction_complete_error,
    emit_compaction_skipped,
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
    so the gate measures the same quantity the inner tiers measure. ``async``
    by convention for provider operations and to keep the estimator call sites
    uniform; the underlying harness call is synchronous.
    """
    from pydantic_ai_harness.compaction import (
        estimate_context_tokens,
    )

    return estimate_context_tokens(
        messages,
        tokenizer=None,
        model_request_parameters=model_request_parameters,
    )


_DENSITY_SAMPLE_CHARS = 4_096
"""Bounded leading sample used for the whitespace-density check."""

_WHITESPACE_CHARS = " \t\n\r\f\v"


def _density_text_token_bound(text: str) -> int:
    """Estimate a token count that tracks token density instead of assuming prose.

    The ~4-characters-per-token heuristic is accurate for ordinary prose but
    undercounts token-dense content by 2-4x, which is what lets a dense
    history drift past a known context window while staying below the
    compaction trigger. This bound keeps the heuristic for ordinary text and
    escalates only for text that is measurably dense:

    - text with a substantial non-ASCII share (CJK, non-Latin scripts, emoji)
      tokenizes near one token per character, so the bound is the character
      count;
    - ASCII text with almost no whitespace (base64, hex, minified data)
      tokenizes near two characters per token, so the bound is half the
      character count.

    The result is in TOKENS and matches the primary heuristic on ordinary
    prose, so it can be compared against the context window without firing on
    histories that are merely large. Multi-token-per-character sequences
    (rare emoji runs) remain a known residual undercount; the bounded leading
    sample keeps the classification cost flat for very large parts.
    """
    chars = len(text)
    if chars == 0:
        return 0
    sample = text[:_DENSITY_SAMPLE_CHARS]
    non_ascii = len(sample) - len(sample.encode("ascii", "ignore"))
    if non_ascii * 10 >= len(sample):
        return chars
    whitespace = sum(sample.count(char) for char in _WHITESPACE_CHARS)
    if whitespace * 10 < len(sample):
        return chars // 2 + 1
    return chars // 4


async def _estimate_context_tokens_density(
    messages: list[ModelMessage],
    model_request_parameters: ModelRequestParameters | None,
) -> int:
    """Density-calibrated safety estimate of the context size, in tokens.

    Uses the same harness anchoring as the primary estimator but measures the
    post-anchor suffix with :func:`_density_text_token_bound`, so the result
    tracks the primary estimate on ordinary prose and rises up to ~4x above
    it on token-dense suffixes. It is comparable against the context window
    and the compaction target; it is never mixed into token telemetry as a
    substitute for the primary estimate.
    """
    from pydantic_ai_harness.compaction import estimate_context_tokens

    return estimate_context_tokens(
        messages,
        tokenizer=_density_text_token_bound,
        model_request_parameters=model_request_parameters,
    )


async def _estimate_context_tokens_independent(messages: list[ModelMessage]) -> int:
    """Density-calibrated context estimate sharing no code with the primary path.

    The primary and density-calibrated estimators both funnel through the
    harness's text collection, so a bug there (a part whose ``str(...)``
    raises, a malformed usage anchor) fails them together. This fallback
    walks the message list directly with attribute-level access and a
    per-part guard, so one malformed part degrades the estimate instead of
    raising. It is rougher than the harness path (no instruction or
    tool-schema accounting) and is used only when the primary measurement
    itself failed.
    """
    anchor_tokens = 0
    anchor_index = -1
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        usage = getattr(message, "usage", None)
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        if isinstance(message, ModelResponse) and input_tokens:
            anchor_tokens = int(input_tokens) + int(getattr(usage, "output_tokens", 0) or 0)
            anchor_index = index
            break
    suffix = 0
    for message in messages[anchor_index + 1 :]:
        for part in getattr(message, "parts", []):
            try:
                content = getattr(part, "content", "")
                text = content if isinstance(content, str) else str(content)
                suffix += _density_text_token_bound(text)
            except Exception:  # noqa: BLE001 - one bad part must not zero the estimate
                continue
    return anchor_tokens + suffix


def _estimate_after_compaction_tokens(
    before_messages: list[ModelMessage],
    after_messages: list[ModelMessage],
    before_estimate: int,
) -> int:
    """Estimate the post-compaction token count, compensating for the anchor.

    ``estimate_context_tokens`` anchors on the most recent ``ModelResponse``
    with provider-reported ``usage.input_tokens``. That anchoring response
    usually survives compaction (every tier keeps the recent tail), so a
    naive after-estimate still describes the pre-rewrite request and always
    reports ``after == before`` — i.e. ``tokens_saved == 0`` — no matter how
    much history was dropped. ``TieredCompaction._escalate`` compensates for
    this internally by subtracting the tier's measured heuristic reclaim from
    its anchored baseline; this helper mirrors that compensation so the
    telemetry reports the same scale of numbers the escalation loop acted on.
    Two caveats: the inputs must all be on the token scale (a baseline
    produced by a different estimator invalidates the subtraction), and on a
    window-guard compaction — where the anchor may be dropped and the real
    reclaim is density-scale, which the heuristic under-measures — the
    reported ``after`` overstates the remaining tokens and understates the
    savings. That is the cheap direction: ``still_over_trigger`` stays honest
    because it compares token-scale numbers against the token trigger.
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

    Every request is measured twice, by design: the primary estimator
    (provider usage anchor plus the ~4-characters-per-token heuristic) drives
    the reserve-based trigger, and a density-calibrated safety estimate
    guards the hard context window against content the heuristic undercounts.
    On ordinary prose the two agree, so the safety estimate never fires on a
    history that is merely large.

    Failure handling is zoned:

    - **Primary measurement failure** — fall back to an independent
      density-calibrated estimate that shares no code with the primary path.
      Compaction may still run and lifecycle events are emitted as usual; the
      fallback value becomes ``tokens_before`` for this request.
    - **Safety measurement failure** — the primary estimate remains usable, so
      compaction proceeds on it alone and the complete event names
      ``"density"`` in ``degraded_estimators``; the window guard is disarmed
      for this request only.
    - **Both estimators failed** — return the context unchanged with an
      ``agent_compaction_skipped`` event; no disable latch, because a broken
      estimate says nothing about the compaction path.
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

    def _on_before(
        self,
        estimate: int,
        messages_before: int,
        *,
        trigger_reason: str,
        density_estimate: int | None,
    ) -> None:
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
            trigger_reason=trigger_reason,
            density_tokens=density_estimate,
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
        degraded_estimators: list[str],
        still_over_window: bool,
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
            degraded_estimators=degraded_estimators,
            still_over_window=still_over_window,
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

    async def _drive_tiers_under_window_guard(
        self,
        messages: list[ModelMessage],
        ctx: RunContext[Any],
        density_of: Callable[[list[ModelMessage]], Awaitable[int]],
    ) -> list[ModelMessage]:
        """Drive the tier chain until the density-calibrated estimate fits the target.

        The inner strategy's own gate measures with the primary estimator —
        the same heuristic that under-counted this content — so delegating to
        it would no-op exactly where the window guard is needed. Driving the
        tiers directly keeps the stop decision on the density-calibrated
        scale. Escalation order and per-tier failure handling are unchanged:
        a tier that raises is caught by its :class:`_TierWrapper` and named in
        ``degraded_tiers``.
        """
        compacted = list(messages)
        for tier in self._tier_wrappers:
            if await density_of(compacted) <= self._config.target_tokens:
                break
            compacted = await tier.compact(compacted, ctx)
        return compacted

    async def before_model_request(
        self,
        ctx: RunContext[Any],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        if self._disabled:
            return request_context

        before_messages = list(request_context.messages)
        primary_estimate: int | None = None
        density_estimate: int | None = None
        degraded_estimators: list[str] = []

        try:
            primary_estimate = await _estimate_context_tokens(
                before_messages,
                request_context.model_request_parameters,
            )
        except Exception:  # noqa: BLE001 - estimation must never fail the run
            logger.warning(
                "Compaction gate measurement failed for agent %r; "
                "using the density-calibrated fallback.",
                self._config.agent_name,
                exc_info=True,
            )
            degraded_estimators.append("primary")
            try:
                density_estimate = await _estimate_context_tokens_independent(before_messages)
            except Exception:  # noqa: BLE001 - estimation must never fail the run
                logger.warning(
                    "Compaction fallback measurement failed for agent %r; "
                    "skipping compaction for this request.",
                    self._config.agent_name,
                    exc_info=True,
                )
                emit_compaction_skipped(
                    self._config.event_callback,
                    agent_name=self._config.agent_name,
                    strategy="tiered",
                    model=self._config.model_name,
                    reason="estimate_unavailable",
                )
                return request_context
        else:
            try:
                density_estimate = await _estimate_context_tokens_density(
                    before_messages,
                    request_context.model_request_parameters,
                )
            except Exception:  # noqa: BLE001 - primary estimate remains usable
                logger.warning(
                    "Compaction safety measurement failed for agent %r; "
                    "using the primary estimate only.",
                    self._config.agent_name,
                    exc_info=True,
                )
                degraded_estimators.append("density")

        if primary_estimate is not None:
            before_estimate = primary_estimate
        elif density_estimate is not None:
            before_estimate = density_estimate
        else:
            return request_context  # pragma: no cover - the double-failure arm returned above

        # Two independent gates, two estimators. The reserve trigger uses the
        # primary estimate (usage anchor + heuristic), which is accurate for
        # ordinary text. The hard window guard uses the density-calibrated
        # estimate, which matches the primary on ordinary prose and rises up
        # to ~4x above it on token-dense content. The guard therefore fires
        # only when the request may really overflow the known window — never
        # merely because a history is large. ``before_estimate`` stays on the
        # primary/fallback token scale: the density value is a gate input,
        # reported separately as ``density_tokens``, never as token telemetry.
        window_guard_tripped = (
            density_estimate is not None and density_estimate >= self._config.window_tokens
        )
        trigger_tripped = before_estimate > self._config.trigger_tokens
        if not trigger_tripped and not window_guard_tripped:
            return request_context

        # The density re-measurement used by the window-guard path: the shared
        # harness-backed estimator normally, the independent fallback when the
        # primary path is broken.
        if primary_estimate is not None:

            async def density_of(messages: list[ModelMessage]) -> int:
                return await _estimate_context_tokens_density(
                    messages,
                    request_context.model_request_parameters,
                )

        else:

            async def density_of(messages: list[ModelMessage]) -> int:
                return await _estimate_context_tokens_independent(messages)

        self._on_before(
            estimate=before_estimate,
            messages_before=len(before_messages),
            trigger_reason="window_guard" if window_guard_tripped else "trigger",
            density_estimate=density_estimate,
        )
        for tier in self._tier_wrappers:
            tier.failed = False
        start = time.monotonic()

        # Inner strategy. Fail open with the original context, emit the
        # errored event, and latch the per-execution disable flag. When the
        # window guard fired and the tier chain is available, drive it
        # directly: delegating would re-gate on the same primary heuristic
        # that under-counted this content and no-op.
        try:
            if window_guard_tripped and self._tier_wrappers:
                # The tiers get the request's context, not the run's: a tier
                # that resolves a model (the summarizing one) has to reach the
                # same model the request is going to, mirroring the harness's
                # own ``context_for_request``.
                request_ctx = (
                    ctx
                    if request_context.model is ctx.model
                    else replace(ctx, model=request_context.model)
                )
                request_context.messages = await self._drive_tiers_under_window_guard(
                    before_messages, request_ctx, density_of
                )
                result = request_context
            else:
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
        guard_path = window_guard_tripped and bool(self._tier_wrappers)
        if guard_path and list(result.messages) == before_messages:
            logger.error(
                "Window guard fired for agent %r but compaction changed nothing; "
                "the request may still exceed the known context window.",
                self._config.agent_name,
            )

        # After-telemetry. Compaction already happened, so a failure here must
        # still return the compacted result — warn, emit nothing, and leave
        # the latch off.
        try:
            after_messages = list(result.messages)
            still_over_window = guard_path and (
                await density_of(after_messages) >= self._config.window_tokens
            )
            after_estimate = _estimate_after_compaction_tokens(
                before_messages,
                after_messages,
                before_estimate,
            )
            self._on_after(
                before_messages=before_messages,
                after_messages=after_messages,
                before_estimate=before_estimate,
                after_estimate=after_estimate,
                elapsed_seconds=time.monotonic() - start,
                degraded_tiers=degraded_tiers,
                degraded_estimators=degraded_estimators,
                still_over_window=still_over_window,
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

    1. ``_FailOpenCompactionWrapper`` — owns the two gates (primary trigger
       and density-calibrated window guard, each measured per request),
       catches unexpected errors, and returns the original context unchanged
       when the inner strategy fails.
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
