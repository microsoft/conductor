"""Agent, item, tool, and gate event handlers for tracing."""

from __future__ import annotations

from collections import deque

from opentelemetry import trace

from conductor.events import WorkflowEvent
from conductor.providers.capabilities import has_native_otel_spans
from conductor.telemetry.semconv import (
    CONDUCTOR_GROUP_NAME,
    CONDUCTOR_ITEM_KEY,
    CONDUCTOR_ITERATION,
    CONDUCTOR_MCP_RESULT_BYTES,
    CONDUCTOR_MCP_SERVER,
    CONDUCTOR_MCP_TRUNCATED,
    CONDUCTOR_STEP_TYPE,
    ERROR_TYPE,
    EXECUTE_TOOL,
    GEN_AI_AGENT_NAME,
    GEN_AI_OPERATION_NAME,
    GEN_AI_TOOL_CALL_ID,
    GEN_AI_TOOL_NAME,
    GEN_AI_TOOL_TYPE,
    INVOKE_AGENT,
)
from conductor.telemetry.subscriber_state import SpanState
from conductor.telemetry.subscriber_types import (
    AttributeValue,
    SpanKey,
    event_number,
    event_path,
    event_text,
)

_GATE_EVENT = "conductor.gate.event"


def parallel_agent_started(state: SpanState, event: WorkflowEvent) -> None:
    """Start a static parallel member span."""
    path = event_path(event, "subworkflow_path")
    group = event_text(event, "group_name")
    agent = event_text(event, "agent_name")
    if group is None or agent is None:
        return
    key = state.start(
        event,
        f"{INVOKE_AGENT} {agent}",
        state.group_keys.get((path, "parallel", group)),
        {
            GEN_AI_OPERATION_NAME: INVOKE_AGENT,
            GEN_AI_AGENT_NAME: agent,
            CONDUCTOR_GROUP_NAME: group,
        },
        attach=True,
    )
    state.parallel_keys[(path, group, agent)] = key
    _record_span_provider(state, key, event)
    _record_span_realm(state, key, event)


def parallel_agent_completed(state: SpanState, event: WorkflowEvent) -> None:
    """Finish one static parallel member successfully."""
    _finish_parallel_member(state, event, failed=False)


def parallel_agent_failed(state: SpanState, event: WorkflowEvent) -> None:
    """Finish one static parallel member as failed."""
    _finish_parallel_member(state, event, failed=True)


def item_started(state: SpanState, event: WorkflowEvent) -> None:
    """Start an identity-stable for-each item span."""
    path = event_path(event, "subworkflow_path")
    group = event_text(event, "group_name")
    item_name = event_text(event, "item_key")
    index = event_number(event, "index")
    if group is None or item_name is None or index is None:
        return
    key = state.start(
        event,
        f"{INVOKE_AGENT} {group}[{item_name}]",
        state.group_keys.get((path, "for_each", group)),
        {
            GEN_AI_OPERATION_NAME: INVOKE_AGENT,
            GEN_AI_AGENT_NAME: group,
            CONDUCTOR_GROUP_NAME: group,
            CONDUCTOR_ITEM_KEY: item_name,
        },
        attach=True,
    )
    state.item_keys[(path, group, index)] = key
    state.item_keys_by_name.setdefault((path, group, item_name), deque()).append(key)


def item_agent_started(state: SpanState, event: WorkflowEvent) -> None:
    """Annotate an existing item span with its resolved agent name."""
    key = state.item_key(
        event_path(event, "subworkflow_path"),
        event_text(event, "group_name"),
        event_text(event, "item_key"),
        event_number(event, "index"),
    )
    agent = event_text(event, "agent_name")
    if key in state.open_spans and agent:
        state.open_spans[key].set_attribute(GEN_AI_AGENT_NAME, agent)
        _record_span_provider(state, key, event)
        _record_span_realm(state, key, event)


def item_completed(state: SpanState, event: WorkflowEvent) -> None:
    """Finish the item span from its terminal envelope."""
    _finish_item(state, event, failed=False)


def item_failed(state: SpanState, event: WorkflowEvent) -> None:
    """Finish the item span from its failed terminal envelope."""
    _finish_item(state, event, failed=True)


def agent_started(state: SpanState, event: WorkflowEvent) -> None:
    """Start an ordinary sequential agent span."""
    path = event_path(event, "subworkflow_path")
    agent = event_text(event, "agent_name")
    if agent is None:
        return
    key = state.start(
        event,
        f"{INVOKE_AGENT} {agent}",
        state.parent_for_path(path),
        _agent_attributes(agent, event_text(event, "agent_type") or "agent", event),
        attach=True,
    )
    state.agent_keys.setdefault((path, agent), deque()).append(key)
    _record_span_provider(state, key, event)
    _record_span_realm(state, key, event)


def agent_completed(state: SpanState, event: WorkflowEvent) -> None:
    """Finish an ordinary agent span successfully."""
    _finish_agent(state, event, failed=False)


def agent_failed(state: SpanState, event: WorkflowEvent) -> None:
    """Finish an ordinary agent span as failed."""
    _finish_agent(state, event, failed=True)


def step_started(state: SpanState, event: WorkflowEvent) -> None:
    """Start a non-provider step only when no enclosing agent span exists."""
    path = event_path(event, "subworkflow_path")
    agent = event_text(event, "agent_name")
    if agent is None or state.latest_agent(path, agent) in state.open_spans:
        return
    step_type = event.type.removesuffix("_started")
    key = state.start(
        event,
        f"{INVOKE_AGENT} {agent}",
        state.latest_group(path),
        _agent_attributes(agent, step_type, event),
        attach=False,
    )
    state.agent_keys.setdefault((path, agent), deque()).append(key)


def step_completed(state: SpanState, event: WorkflowEvent) -> None:
    """Finish a non-provider step successfully."""
    _finish_agent(state, event, failed=False)


def step_failed(state: SpanState, event: WorkflowEvent) -> None:
    """Finish a non-provider step as failed."""
    _finish_agent(state, event, failed=True)


def mcp_started(state: SpanState, event: WorkflowEvent) -> None:
    """Start one deterministic MCP tool span under its orchestration parent."""
    path = event_path(event, "subworkflow_path")
    parent = state.mcp_parent(event)
    agent = event_text(event, "agent_name")
    server = event_text(event, "server")
    tool = event_text(event, "tool")
    if parent not in state.open_spans or agent is None or server is None or tool is None:
        return
    attributes: dict[str, AttributeValue] = {
        GEN_AI_OPERATION_NAME: EXECUTE_TOOL,
        GEN_AI_AGENT_NAME: agent,
        GEN_AI_TOOL_NAME: tool,
        GEN_AI_TOOL_TYPE: "function",
        CONDUCTOR_STEP_TYPE: "mcp",
        CONDUCTOR_MCP_SERVER: server,
    }
    iteration = event_number(event, "iteration")
    if iteration is not None:
        attributes[CONDUCTOR_ITERATION] = iteration
    group = event_text(event, "group_name")
    if group is not None:
        attributes[CONDUCTOR_GROUP_NAME] = group
    item = event_text(event, "item_key")
    if item is not None:
        attributes[CONDUCTOR_ITEM_KEY] = item
    key = state.start(event, f"{EXECUTE_TOOL} {tool}", parent, attributes, attach=False)
    state.mcp_queues.setdefault((path, parent, agent, server, tool), deque()).append(key)
    state.mcp_agent_queues.setdefault((path, agent), deque()).append(key)


def mcp_completed(state: SpanState, event: WorkflowEvent) -> None:
    """Finish a deterministic MCP call and retain bounded result metadata."""
    key = _mcp_key(state, event)
    if key not in state.open_spans:
        return
    span = state.open_spans[key]
    result_bytes = event_number(event, "result_bytes")
    if result_bytes is not None:
        span.set_attribute(CONDUCTOR_MCP_RESULT_BYTES, result_bytes)
    truncated = event.data.get("truncated")
    if isinstance(truncated, bool):
        span.set_attribute(CONDUCTOR_MCP_TRUNCATED, truncated)
    if event.data.get("is_error") is True:
        span.set_attribute(ERROR_TYPE, "MCPToolError")
        span.set_status(trace.Status(trace.StatusCode.ERROR))
    state.end(key, event)


def mcp_failed(state: SpanState, event: WorkflowEvent) -> None:
    """Finish a deterministic MCP call as an execution failure."""
    state.end(_mcp_key(state, event), event, failed=True)


def mcp_interrupted(state: SpanState, event: WorkflowEvent) -> None:
    """Close an interrupted MCP attempt before a user can resume the step."""
    path = event_path(event, "subworkflow_path")
    agent = event_text(event, "agent_name")
    queue = state.mcp_agent_queues.get((path, agent)) if agent is not None else None
    key = next(
        (candidate for candidate in reversed(queue or ()) if candidate in state.open_spans),
        None,
    )
    state.end(key, event)


def _mcp_key(state: SpanState, event: WorkflowEvent) -> SpanKey | None:
    path = event_path(event, "subworkflow_path")
    parent = state.mcp_parent(event)
    agent = event_text(event, "agent_name")
    server = event_text(event, "server")
    tool = event_text(event, "tool")
    if parent is None or agent is None or server is None or tool is None:
        return None
    queue = state.mcp_queues.get((path, parent, agent, server, tool))
    return queue.popleft() if queue else None


def validator_started(state: SpanState, event: WorkflowEvent) -> None:
    """Start a validator span under its primary agent span."""
    path = event_path(event, "subworkflow_path")
    agent = event_text(event, "agent_name")
    if agent is None:
        return
    validator = f"{agent} (validator)"
    index = event_number(event, "index")
    if index is not None:
        # For-each validator callbacks all share the group's display name and
        # are told apart only by index/item_key. Identity must come from the
        # full (path, group, index) key — a name-keyed deque would let two
        # overlapping item validators close each other's spans.
        key = state.start(
            event,
            f"{INVOKE_AGENT} {validator}",
            state.item_key(path, agent, event_text(event, "item_key"), index),
            _agent_attributes(validator, "validator", event),
            attach=True,
        )
        state.validator_item_keys[(path, agent, index)] = key
        return
    key = state.start(
        event,
        f"{INVOKE_AGENT} {validator}",
        state.latest_agent(path, agent),
        _agent_attributes(validator, "validator", event),
        attach=True,
    )
    state.agent_keys.setdefault((path, validator), deque()).append(key)


def validator_completed(state: SpanState, event: WorkflowEvent) -> None:
    """Finish a validator span, marking execution errors as failures."""
    path = event_path(event, "subworkflow_path")
    agent = event_text(event, "agent_name")
    if not agent:
        return
    index = event_number(event, "index")
    if index is not None:
        key = state.validator_item_keys.get((path, agent, index))
    else:
        key = state.latest_agent(path, f"{agent} (validator)")
    state.end(key, event, failed=event.data.get("errored") is True)


def tool_started(state: SpanState, event: WorkflowEvent) -> None:
    """Start a tool span under the event's active agent-like parent."""
    parent = state.tool_parent(event)
    tool = event_text(event, "tool_name")
    if (
        parent not in state.open_spans
        or tool is None
        or _parent_has_native_otel_spans(state, parent)
    ):
        return
    key = state.start(
        event,
        f"{EXECUTE_TOOL} {tool}",
        parent,
        {
            GEN_AI_OPERATION_NAME: EXECUTE_TOOL,
            GEN_AI_TOOL_NAME: tool,
            GEN_AI_TOOL_TYPE: "function",
        },
        attach=False,
    )
    call_id = event_text(event, "tool_call_id")
    if call_id:
        state.tool_keys_by_id[(event_path(event, "subworkflow_path"), parent, call_id)] = key
        state.open_spans[key].set_attribute(GEN_AI_TOOL_CALL_ID, call_id)
        return
    state.tool_queues.setdefault((parent, tool), deque()).append(key)


def tool_completed(state: SpanState, event: WorkflowEvent) -> None:
    """Finish a tool span by call ID or FIFO fallback for anonymous calls."""
    call_id = event_text(event, "tool_call_id")
    parent = state.tool_parent(event)
    if _parent_has_native_otel_spans(state, parent):
        return
    key = (
        state.tool_keys_by_id.pop((event_path(event, "subworkflow_path"), parent, call_id), None)
        if parent is not None and call_id
        else None
    )
    tool = event_text(event, "tool_name")
    if key is None and tool:
        queue = state.tool_queues.get((parent, tool)) if parent else None
        key = queue.popleft() if queue else None
    state.end(key, event)


def gate_event(state: SpanState, event: WorkflowEvent) -> None:
    """Emit one zero-duration span per human-gate state transition."""
    if event.data.get("step_type") == "questions":
        return
    path = event_path(event, "subworkflow_path")
    agent = event_text(event, "agent_name")
    parent = state.latest_agent(path, agent) if agent else state.parent_for_path(path)
    if event.type == "gate_presented" and parent in state.open_spans:
        state.end(parent, event)
    key = state.start(
        event,
        event.type,
        parent,
        {CONDUCTOR_STEP_TYPE: "human_gate", _GATE_EVENT: event.type},
        attach=False,
    )
    state.end(key, event)


def _finish_parallel_member(state: SpanState, event: WorkflowEvent, *, failed: bool) -> None:
    path = event_path(event, "subworkflow_path")
    group = event_text(event, "group_name")
    agent = event_text(event, "agent_name")
    if group is not None and agent is not None:
        key = state.parallel_keys.get((path, group, agent))
        _record_span_realm(state, key, event)
        state.end(key, event, failed=failed)


def _finish_item(state: SpanState, event: WorkflowEvent, *, failed: bool) -> None:
    key = state.item_key(
        event_path(event, "subworkflow_path"),
        event_text(event, "group_name"),
        event_text(event, "item_key"),
        event_number(event, "index"),
    )
    _record_span_realm(state, key, event)
    state.end(key, event, failed=failed)


def _finish_agent(state: SpanState, event: WorkflowEvent, *, failed: bool) -> None:
    path = event_path(event, "subworkflow_path")
    agent = event_text(event, "agent_name")
    if agent is not None:
        key = state.latest_agent(path, agent)
        _record_span_realm(state, key, event)
        state.end(key, event, failed=failed)


def _record_span_realm(state: SpanState, key: SpanKey | None, event: WorkflowEvent) -> None:
    """Project present realm identity fields onto an open agent span."""
    span = state.open_spans.get(key) if key is not None else None
    if span is None:
        return
    for field, attribute in (
        ("execution_backend", "conductor.execution.backend"),
        ("realm_image", "conductor.realm.image"),
    ):
        value = event_text(event, field)
        if value is not None:
            span.set_attribute(attribute, value)


def _record_span_provider(state: SpanState, key: SpanKey, event: WorkflowEvent) -> None:
    """Associate a provider-aware span with resolved native-span availability."""
    native_otel_spans_active = event.data.get("native_otel_spans_active")
    if isinstance(native_otel_spans_active, bool):
        state.span_provider[key] = native_otel_spans_active
        return
    provider = event_text(event, "provider")
    if provider is not None:
        state.span_provider[key] = has_native_otel_spans(provider) is True


def _parent_has_native_otel_spans(state: SpanState, parent: SpanKey | None) -> bool:
    """Return whether the tool parent is covered by provider-native tracing."""
    return state.span_provider.get(parent, False) if parent is not None else False


def _agent_attributes(
    agent: str,
    step_type: str,
    event: WorkflowEvent,
) -> dict[str, AttributeValue]:
    attributes: dict[str, AttributeValue] = {
        GEN_AI_OPERATION_NAME: INVOKE_AGENT,
        GEN_AI_AGENT_NAME: agent,
        CONDUCTOR_STEP_TYPE: step_type,
    }
    iteration = event_number(event, "iteration")
    if iteration is not None:
        attributes[CONDUCTOR_ITERATION] = iteration
    return attributes
