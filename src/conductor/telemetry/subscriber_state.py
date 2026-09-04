"""Mutable detached-span state owned by one telemetry subscriber."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from contextvars import Token
from typing import TYPE_CHECKING

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context

from conductor.events import WorkflowEvent
from conductor.telemetry.semconv import (
    CONDUCTOR_COST_USD,
    ERROR_TYPE,
    GEN_AI_CONVERSATION_ID,
    GEN_AI_PROVIDER_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
)
from conductor.telemetry.subscriber_types import (
    AttributeValue,
    PathKey,
    SpanKey,
    ToolKey,
    event_number,
    event_path,
    event_text,
    timestamp_ns,
)

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider

_TRACER_NAME = "conductor.telemetry"
logger = logging.getLogger(__name__)


class SpanState:
    """Store open detached spans and their event-derived identities."""

    def __init__(self, tracer_provider: TracerProvider, *, resumed: bool = False) -> None:
        """Create isolated state for one active workflow run.

        Args:
            tracer_provider: OpenTelemetry SDK tracer provider for this run.
            resumed: Whether this run is a resumed workflow. When True, the
                root workflow span receives ``conductor.resumed=true``.
        """
        self.tracer = tracer_provider.get_tracer(_TRACER_NAME)
        self.open_spans: dict[SpanKey, trace.Span] = {}
        self.parents: dict[SpanKey, SpanKey | None] = {}
        self.span_paths: dict[SpanKey, PathKey] = {}
        self.span_provider: dict[SpanKey, bool] = {}
        self.workflow_keys: dict[PathKey, SpanKey] = {}
        self.group_keys: dict[tuple[PathKey, str, str], SpanKey] = {}
        self.agent_keys: dict[tuple[PathKey, str], deque[SpanKey]] = {}
        self.parallel_keys: dict[tuple[PathKey, str, str], SpanKey] = {}
        self.item_keys: dict[tuple[PathKey, str, int], SpanKey] = {}
        self.item_keys_by_name: dict[tuple[PathKey, str, str], deque[SpanKey]] = {}
        self.subworkflow_parents: dict[PathKey, SpanKey] = {}
        self.tool_keys_by_id: dict[ToolKey, SpanKey] = {}
        self.tool_queues: dict[tuple[SpanKey, str], deque[SpanKey]] = {}
        self._attach_tokens: dict[SpanKey, tuple[Token[Context], asyncio.Task[None] | None]] = {}
        self.run_id: str | None = None
        self.resumed = resumed
        self.next_key = 0

    def start(
        self,
        event: WorkflowEvent,
        name: str,
        parent: SpanKey | None,
        attributes: dict[str, AttributeValue],
        *,
        attach: bool,
    ) -> SpanKey:
        """Start a span under an explicit parent and optionally attach it to this task."""
        self.next_key += 1
        key = (name, self.next_key)
        parent_span = self.open_spans.get(parent) if parent else None
        context = trace.set_span_in_context(parent_span) if parent_span else None
        span = self.tracer.start_span(
            name,
            context=context,
            kind=trace.SpanKind.INTERNAL,
            start_time=timestamp_ns(event),
        )
        span.set_attribute(GEN_AI_CONVERSATION_ID, self.run_id or "unknown")
        for attribute, value in attributes.items():
            span.set_attribute(attribute, value)
        self.open_spans[key] = span
        self.parents[key] = parent
        self.span_paths[key] = event_path(event, "subworkflow_path")
        if attach:
            token = otel_context.attach(trace.set_span_in_context(span))
            self._attach_tokens[key] = (token, self._current_task())
        return key

    def end(self, key: SpanKey | None, event: WorkflowEvent, *, failed: bool = False) -> None:
        """Close one span, recursively closing direct children first."""
        if key is None:
            return
        if key not in self.open_spans:
            if self._detach_if_owner(key):
                self._discard_indexes(key)
            return
        for child in reversed(
            [candidate for candidate, parent in self.parents.items() if parent == key]
        ):
            self.end(child, event, failed=failed)
        span = self.open_spans.pop(key)
        self.parents.pop(key, None)
        self.span_paths.pop(key, None)
        self.span_provider.pop(key, None)
        if failed:
            error_type = event_text(event, "error_type") or "WorkflowError"
            span.set_attribute(ERROR_TYPE, error_type)
            message = event_text(event, "message")
            if message:
                span.set_attribute("error.message", message)
            span.set_status(trace.Status(trace.StatusCode.ERROR, message))
        else:
            self._set_completion_attributes(span, event)
        span.end(end_time=timestamp_ns(event))
        detached = self._detach_if_owner(key)
        if key not in self._attach_tokens or detached:
            self._discard_indexes(key)

    def finish_all(self, event: WorkflowEvent, *, failed: bool) -> None:
        """Finish all currently open spans."""
        for key in reversed(tuple(self.open_spans)):
            self.end(key, event, failed=failed)

    def finish_path(self, path: PathKey, event: WorkflowEvent, *, failed: bool) -> None:
        """Finish all spans emitted by one workflow path and its descendants."""
        for key in reversed(tuple(self.open_spans)):
            span_path = self.span_paths.get(key, ())
            if span_path[: len(path)] == path:
                self.end(key, event, failed=failed)

    def finish_members(self, group: SpanKey | None, event: WorkflowEvent) -> None:
        """Finish unfinished direct group members after a partial failure."""
        for key, parent in reversed(tuple(self.parents.items())):
            if parent == group:
                self.end(key, event)

    def detach_finished_for_current_task(self) -> None:
        """Detach contexts whose spans were ended by a different task."""
        for key in reversed(tuple(self._attach_tokens)):
            if key not in self.open_spans and self._detach_if_owner(key):
                self._discard_indexes(key)

    def detach_close_tokens(self) -> None:
        """Detach this task's leftovers and discard tokens from dead worker tasks."""
        current_task = self._current_task()
        for key, (_, owner_task) in reversed(tuple(self._attach_tokens.items())):
            if owner_task is current_task:
                if self._detach_if_owner(key):
                    self._discard_indexes(key)
            elif owner_task is not None and owner_task.done():
                self._attach_tokens.pop(key, None)
                logger.debug("Discarded OpenTelemetry context token from completed worker task")

    def parent_for_path(self, path: PathKey) -> SpanKey | None:
        """Return the nearest open workflow or remembered subworkflow parent."""
        remembered_parent = self.subworkflow_parents.get(path)
        if remembered_parent in self.open_spans:
            return remembered_parent
        for length in range(len(path), -1, -1):
            key = self.workflow_keys.get(path[:length])
            if key in self.open_spans:
                return key
        return None

    def latest_group(self, path: PathKey) -> SpanKey | None:
        """Return the latest open group in a workflow path."""
        keys = [
            key
            for (group_path, _, _), key in self.group_keys.items()
            if group_path == path and key in self.open_spans
        ]
        return keys[-1] if keys else self.parent_for_path(path)

    def latest_agent(self, path: PathKey, agent: str | None) -> SpanKey | None:
        """Return the latest open occurrence of an agent in one workflow path."""
        if agent is None:
            return None
        keys = self.agent_keys.get((path, agent), deque())
        return next((key for key in reversed(keys) if key in self.open_spans), None)

    def item_key(
        self,
        path: PathKey,
        group: str | None,
        item_key: str | None,
        index: int | None,
    ) -> SpanKey | None:
        """Resolve an open for-each item by its collision-safe identity."""
        if group is None:
            return None
        if index is not None:
            return self.item_keys.get((path, group, index))
        if item_key is None:
            return None
        candidates = [
            key
            for key in self.item_keys_by_name.get((path, group, item_key), deque())
            if key in self.open_spans
        ]
        return candidates[0] if len(candidates) == 1 else None

    def tool_parent(self, event: WorkflowEvent) -> SpanKey | None:
        """Resolve a tool event to its active item, parallel member, or agent."""
        path = event_path(event, "subworkflow_path")
        agent = event_text(event, "agent_name")
        group = event_text(event, "group_name")
        item = self.item_key(
            path,
            group or agent,
            event_text(event, "item_key"),
            event_number(event, "index"),
        )
        if item in self.open_spans:
            return item
        if agent is None:
            return None
        parallel = next(
            (
                key
                for (member_path, _, member), key in self.parallel_keys.items()
                if member_path == path and member == agent and key in self.open_spans
            ),
            None,
        )
        return parallel or self.latest_agent(path, agent)

    def clear_indexes(self) -> None:
        """Clear all run-local identities after terminal cleanup."""
        self.open_spans.clear()
        self.parents.clear()
        self.span_paths.clear()
        self.span_provider.clear()
        self.workflow_keys.clear()
        self.group_keys.clear()
        self.agent_keys.clear()
        self.parallel_keys.clear()
        self.item_keys.clear()
        self.item_keys_by_name.clear()
        self.subworkflow_parents.clear()
        self.tool_keys_by_id.clear()
        self.tool_queues.clear()
        self.run_id = None

    def _detach_if_owner(self, key: SpanKey) -> bool:
        """Detach one context only from the asyncio task that attached it."""
        attached = self._attach_tokens.get(key)
        if attached is None:
            return False
        token, owner_task = attached
        if owner_task is not self._current_task():
            return False
        try:
            otel_context.detach(token)
        except Exception:  # noqa: BLE001 -- context cleanup must not hide workflow outcomes.
            logger.debug("Failed to detach OpenTelemetry context token", exc_info=True)
            return False
        self._attach_tokens.pop(key, None)
        return True

    @staticmethod
    def _current_task() -> asyncio.Task[None] | None:
        """Return the asyncio task when called inside an event loop."""
        try:
            return asyncio.current_task()
        except RuntimeError:
            return None

    def _discard_indexes(self, key: SpanKey) -> None:
        """Remove a completed key from every identity index."""
        self._discard_key_index(self.workflow_keys, key)
        self._discard_key_index(self.group_keys, key)
        self._discard_key_index(self.parallel_keys, key)
        self._discard_key_index(self.item_keys, key)
        self._discard_queue_index(self.agent_keys, key)
        self._discard_queue_index(self.item_keys_by_name, key)
        self._discard_queue_index(self.tool_queues, key)
        for child_path, parent in tuple(self.subworkflow_parents.items()):
            if parent == key:
                self.subworkflow_parents.pop(child_path)
        for tool_identity, tool_key in tuple(self.tool_keys_by_id.items()):
            if tool_key == key:
                self.tool_keys_by_id.pop(tool_identity)

    @staticmethod
    def _discard_key_index[K](index: dict[K, SpanKey], key: SpanKey) -> None:
        """Remove a completed span from a one-to-one identity index."""
        for index_key, current_key in tuple(index.items()):
            if current_key == key:
                index.pop(index_key)

    @staticmethod
    def _discard_queue_index[K](index: dict[K, deque[SpanKey]], key: SpanKey) -> None:
        """Remove a completed span from every queue-based identity index."""
        for index_key, keys in tuple(index.items()):
            remaining = deque(candidate for candidate in keys if candidate != key)
            if remaining:
                index[index_key] = remaining
            else:
                index.pop(index_key)

    def _set_completion_attributes(self, span: trace.Span, event: WorkflowEvent) -> None:
        """Copy completion metadata that conforms to declared semantic attributes."""
        scalar_attributes = (
            ("model", GEN_AI_REQUEST_MODEL),
            ("provider", GEN_AI_PROVIDER_NAME),
            ("cost_usd", CONDUCTOR_COST_USD),
        )
        for data_key, attribute in scalar_attributes:
            value = event.data.get(data_key)
            if isinstance(value, str | int | float) and not isinstance(value, bool):
                span.set_attribute(attribute, value)
        token_attributes = (
            ("input_tokens", GEN_AI_USAGE_INPUT_TOKENS),
            ("output_tokens", GEN_AI_USAGE_OUTPUT_TOKENS),
        )
        for data_key, attribute in token_attributes:
            value = event_number(event, data_key)
            if value is not None:
                span.set_attribute(attribute, value)
