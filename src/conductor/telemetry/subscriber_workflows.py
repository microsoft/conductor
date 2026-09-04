"""Workflow, group, and nested-workflow event handlers for tracing."""

from __future__ import annotations

from conductor.events import WorkflowEvent
from conductor.telemetry.semconv import (
    CONDUCTOR_GROUP_NAME,
    CONDUCTOR_RESUMED,
    CONDUCTOR_STEP_TYPE,
    CONDUCTOR_SUPERSEDED,
    GEN_AI_AGENT_NAME,
    GEN_AI_OPERATION_NAME,
    INVOKE_AGENT,
    INVOKE_WORKFLOW,
)
from conductor.telemetry.subscriber_state import SpanState
from conductor.telemetry.subscriber_types import (
    AttributeValue,
    event_number,
    event_path,
    event_text,
)


def workflow_started(state: SpanState, event: WorkflowEvent) -> None:
    """Start one root or nested workflow span, reusing resumed roots."""
    path = event_path(event, "subworkflow_path")
    run_id = event_text(event, "run_id")
    if not path and run_id:
        root_key = state.workflow_keys.get(())
        if run_id == state.run_id and root_key in state.open_spans:
            state.open_spans[root_key].set_attribute(CONDUCTOR_RESUMED, True)
            return
        if state.run_id and run_id != state.run_id:
            if root_key in state.open_spans:
                state.open_spans[root_key].set_attribute(CONDUCTOR_SUPERSEDED, True)
            state.finish_all(event, failed=False)
            state.clear_indexes()
        state.run_id = run_id
    if state.workflow_keys.get(path) in state.open_spans:
        return
    name = event_text(event, "name") or "workflow"
    attributes: dict[str, AttributeValue] = {GEN_AI_OPERATION_NAME: INVOKE_WORKFLOW}
    if not path and state.resumed:
        attributes[CONDUCTOR_RESUMED] = True
    state.workflow_keys[path] = state.start(
        event,
        f"{INVOKE_WORKFLOW} {name}",
        state.parent_for_path(path),
        attributes,
        attach=True,
    )


def workflow_completed(state: SpanState, event: WorkflowEvent) -> None:
    """Finish only the completed workflow path, preserving an outer run."""
    path = event_path(event, "subworkflow_path")
    if path:
        state.finish_path(path, event, failed=False)
        return
    state.finish_all(event, failed=False)


def workflow_failed(state: SpanState, event: WorkflowEvent) -> None:
    """Finish only the failed workflow path, preserving an outer run."""
    path = event_path(event, "subworkflow_path")
    if path:
        state.finish_path(path, event, failed=True)
        return
    state.finish_all(event, failed=True)


def group_started(state: SpanState, event: WorkflowEvent) -> None:
    """Start a parallel or for-each orchestration span."""
    path = event_path(event, "subworkflow_path")
    group = event_text(event, "group_name")
    if group is None:
        return
    kind = "parallel" if event.type == "parallel_started" else "for_each"
    index = (path, kind, group)
    if state.group_keys.get(index) in state.open_spans:
        return
    state.group_keys[index] = state.start(
        event,
        f"{INVOKE_AGENT} {group}",
        state.parent_for_path(path),
        {
            GEN_AI_OPERATION_NAME: INVOKE_AGENT,
            GEN_AI_AGENT_NAME: group,
            CONDUCTOR_STEP_TYPE: kind,
            CONDUCTOR_GROUP_NAME: group,
        },
        attach=True,
    )


def group_completed(state: SpanState, event: WorkflowEvent) -> None:
    """Finish a group and any unfinished members after partial failure."""
    path = event_path(event, "subworkflow_path")
    group = event_text(event, "group_name")
    if group is None:
        return
    kind = "parallel" if event.type == "parallel_completed" else "for_each"
    key = state.group_keys.get((path, kind, group))
    if event_number(event, "failure_count"):
        state.finish_members(key, event)
    state.end(key, event)


def subworkflow_started(state: SpanState, event: WorkflowEvent) -> None:
    """Remember the explicit parent to attach a later child root span."""
    parent_path = event_path(event, "parent_path")
    slot_key = event_text(event, "slot_key")
    agent = event_text(event, "agent_name")
    if slot_key is None:
        return
    parent = state.latest_agent(parent_path, agent)
    if parent not in state.open_spans:
        iteration = event_number(event, "iteration")
        item_index = iteration - 1 if iteration is not None else None
        parent = state.item_key(parent_path, agent, event_text(event, "item_key"), item_index)
    if parent in state.open_spans:
        state.subworkflow_parents[(*parent_path, slot_key)] = parent


def subworkflow_completed(state: SpanState, event: WorkflowEvent) -> None:
    """Close a sequential parent agent once its child workflow completes."""
    _finish_subworkflow_parent(state, event, failed=False)


def subworkflow_failed(state: SpanState, event: WorkflowEvent) -> None:
    """Close a sequential parent agent once its child workflow fails."""
    _finish_subworkflow_parent(state, event, failed=True)


def _finish_subworkflow_parent(state: SpanState, event: WorkflowEvent, *, failed: bool) -> None:
    parent_path = event_path(event, "parent_path")
    slot_key = event_text(event, "slot_key")
    if slot_key is None:
        return
    parent = state.subworkflow_parents.pop((*parent_path, slot_key), None)
    if parent and parent[0].startswith(f"{INVOKE_AGENT} "):
        state.end(parent, event, failed=failed)
