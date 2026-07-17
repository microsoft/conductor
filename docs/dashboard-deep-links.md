# Conductor Dashboard Deep-Link Specification

## Overview

The conductor web dashboard (`conductor run --web`) accepts URL query parameters
that deep-link into specific nodes of the workflow graph. This enables external
tools (e.g., the conductor-dashboard meta-dashboard) to generate clickable links
that open the UI focused on a particular agent or subworkflow.

Targets are surfaced **inline**: the deep-link expands the subworkflow containers
along the path so the node appears within the full root graph and is centered,
rather than re-rooting the view into that subworkflow. Focus-mode drill-down
stays available via double-click and the breadcrumb bar.

## Query Parameters

| Parameter      | Format                          | Description                                    |
|----------------|---------------------------------|------------------------------------------------|
| `subworkflow`  | slash-separated agent path      | Reveal a subworkflow inline (expand its container) |
| `agent`        | agent name                      | Select and center an agent node in the graph    |

Both parameters are optional. When both are present, the `subworkflow` path
locates the context; the agent is then revealed and selected inside it.

## URL Format

```
http://localhost:{port}[?subworkflow={path}][&agent={name}]
```

## Subworkflow Path

The `subworkflow` parameter is a `/`-separated path of segments, starting from
the root workflow. Each segment is matched against sibling subworkflow contexts
in priority order:

### 1. Exact slot key

Matches the engine-emitted `slot_key` verbatim. For sequential subworkflows the
slot key equals the agent name. For `for_each` iterations the slot key includes
the item key in brackets, e.g. `plan_child[item-0]`.

```
?subworkflow=plan_child[item-0]/design
```

### 2. Bare parent-agent name (fallback)

When no context's `slot_key` matches the segment exactly, resolution falls back
to matching the context's `parentAgent` — the name of the `type: workflow` step
(or `for_each` group) that spawned it. This lets a caller that only knows the
agent name reach a subworkflow without constructing the exact slot key:

```
# Reaches the "planning" subworkflow by its spawning agent name
?subworkflow=planning
```

For a `for_each` group with multiple iterations, several contexts share the same
`parentAgent`; the **newest** matching iteration wins (the same precedence the
engine uses to route live events). To target a specific iteration, use its exact
slot key (tier 1) instead — e.g. `plan_child[item-0]`.

Given this workflow nesting:

```
root
├── intake          (agent)
├── planning        (workflow agent → planning.yaml)
│   ├── architect   (agent)
│   └── design      (workflow agent → design.yaml)
│       ├── reviewer   (agent)
│       └── writer     (agent)
├── plan_child      (for_each workflow agent → child.yaml)
│   ├── plan_child[item-0]   (iteration 0)
│   └── plan_child[item-1]   (iteration 1)
└── close_out       (agent)
```

| URL                                                    | Result                                    |
|--------------------------------------------------------|-------------------------------------------|
| `?subworkflow=planning`                                | Expand planning inline, centered          |
| `?subworkflow=planning/design`                         | Expand planning → design inline, centered |
| `?subworkflow=planning/design&agent=reviewer`          | Expand planning → design inline, select reviewer |
| `?subworkflow=plan_child[item-0]`                      | Expand plan_child iteration 0 inline      |
| `?subworkflow=plan_child[item-1]`                      | Expand plan_child iteration 1 inline      |

Each path segment is matched using the priority rules above (exact slot key →
bare parent-agent name). The resolved subworkflow (and every container above it)
is expanded **in place** in the root graph — the view is not re-rooted, so you
keep the surrounding workflow context. Drill-down focus mode remains available
by double-clicking a subworkflow node or using the breadcrumb bar.

## Agent Selection

The `agent` parameter selects a node and centers the view on it, expanding its
ancestor containers so it surfaces inline in the root graph.

- **Root agent** (no subworkflow context): `?agent=intake`
- **Agent inside a subworkflow**: `?subworkflow=planning&agent=architect`

**Agent-only links search transitively.** `?agent=reviewer` (no `subworkflow`)
walks the root workflow first, then every sub-workflow / `for_each` iteration.
If the agent exists in exactly one place it is revealed there; when it ran in
many places (e.g. once per `for_each` iteration) the **running → deepest →
newest** match wins.

```
# Reveal reviewer wherever it lives (transitive search)
?agent=reviewer

# Pin to a specific context, then select reviewer inside it
?subworkflow=planning/design&agent=reviewer
```

When an explicit `subworkflow` path is given but the agent isn't in that exact
context, the requested subworkflow is still revealed and the error banner lists
the locations where the agent was actually found.

## Behavior

1. **Parse** — On initial page load, read `subworkflow` and `agent` from
   `window.location.search`.

2. **Wait** — Do nothing until the workflow graph has been populated
   (agents arrive via WebSocket late-joiner replay).

3. **Reveal** — Resolve the `subworkflow` path (and/or transitively locate the
   `agent`) to a target context, then `expandContexts()` that context's ancestor
   chain (and the target context itself) so it renders inline in the root graph.
   The view stays rooted at the top workflow (it is **not** re-rooted); a
   `for_each` iteration target expands both its group container and its own
   inner DAG.

4. **Select** — If `agent` is present, select its namespaced node and
   `fitView()` to center it with a smooth animation. Subworkflow-only links
   center on the revealed container node.

5. **Once** — Deep-link application fires exactly once per page load.
   Subsequent WebSocket events do not re-trigger the reveal.

## Edge Cases

| Scenario                              | Behavior                                        |
|---------------------------------------|--------------------------------------------------|
| Unknown subworkflow path segment      | Error banner: `Subworkflow "X" not found …`       |
| Bare name with multiple for_each iterations | Newest matching iteration wins (no error)   |
| Unknown agent name                    | No node selected, error banner displayed          |
| Subworkflow hasn't started yet        | Resolution fails with "not found" error           |
| Page refresh                          | Deep-link re-applied from URL (full state replay) |
| Combined with breadcrumb navigation   | User can freely navigate after deep-link applies  |

## Example URLs

```
# Root workflow — default view
http://localhost:49123

# Select an agent in the root workflow
http://localhost:49123?agent=intake

# Reveal a subworkflow inline
http://localhost:49123?subworkflow=planning

# Reveal two levels deep, inline
http://localhost:49123?subworkflow=planning/design

# Reveal a subworkflow and select an agent within it
http://localhost:49123?subworkflow=planning/design&agent=reviewer

# for_each iteration by exact slot key
http://localhost:49123?subworkflow=plan_child[item-0]

# Nested: for_each iteration, then into a child subworkflow
http://localhost:49123?subworkflow=plan_child[item-0]/design&agent=writer
```
