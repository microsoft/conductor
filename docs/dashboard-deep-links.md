# Conductor Dashboard Deep-Link Specification

## Overview

The conductor web dashboard (`conductor run --web`) accepts URL query parameters
that deep-link into specific nodes of the workflow graph. This enables external
tools (e.g., the conductor-dashboard meta-dashboard) to generate clickable links
that open the UI focused on a particular agent or subworkflow.

## Query Parameters

| Parameter      | Format                          | Description                                    |
|----------------|---------------------------------|------------------------------------------------|
| `subworkflow`  | slash-separated agent path      | Navigate into a subworkflow context             |
| `agent`        | agent name                      | Select and center an agent node in the graph    |

Both parameters are optional. When both are present, subworkflow navigation
happens first, then the agent is selected within that subworkflow's graph.

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

### 2. Positional index (`agent#N`, 0-based)

Matches the Nth iteration among siblings sharing that `parentAgent`. Useful when
the caller doesn't know the exact `item_key` values emitted by the engine.

```
# First for_each iteration of plan_child
?subworkflow=plan_child%230

# Third iteration, then into its "design" child
?subworkflow=plan_child%232/design
```

> **Note:** `#` must be percent-encoded as `%23` in URLs.

### 3. Bare agent name

Matches if **exactly one** sibling has that `parentAgent`. Works for sequential
(non-`for_each`) subworkflows and single-iteration `for_each` groups. Returns an
**ambiguous** error when multiple iterations exist — the error message lists the
valid exact slot keys and positional alternatives.

```
# Works when there is only one "planning" subworkflow
?subworkflow=planning
```

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
| `?subworkflow=planning`                                | View planning.yaml's graph                |
| `?subworkflow=planning/design`                         | View design.yaml's graph                  |
| `?subworkflow=planning/design&agent=reviewer`          | View design.yaml, select reviewer node    |
| `?subworkflow=plan_child[item-0]`                      | View child.yaml iteration 0               |
| `?subworkflow=plan_child%230`                          | Same — positional (0-based)               |
| `?subworkflow=plan_child%231`                          | View child.yaml iteration 1               |

Each path segment is matched using the priority rules above (exact slot key →
positional → bare name).

## Agent Selection

The `agent` parameter selects and centers a node in the **currently viewed**
workflow graph:

- **Root agent** (no subworkflow context): `?agent=intake`
- **Agent inside a subworkflow**: `?subworkflow=planning&agent=architect`

**Important:** An agent that lives inside a subworkflow will NOT be found
by `?agent=reviewer` alone — you must also provide the `subworkflow` path
to navigate to the correct context first:

```
# ✗ WRONG — reviewer doesn't exist in the root workflow
?agent=reviewer

# ✓ CORRECT — navigate into planning/design, then select reviewer
?subworkflow=planning/design&agent=reviewer
```

## Behavior

1. **Parse** — On initial page load, read `subworkflow` and `agent` from
   `window.location.search`.

2. **Wait** — Do nothing until the workflow graph has been populated
   (agents arrive via WebSocket late-joiner replay).

3. **Navigate** — If `subworkflow` is present, split on `/` and call
   `navigateIntoSubworkflow()` for each segment sequentially.
   Each call is synchronous (zustand `set`/`get`), so the viewed context
   updates between calls.

4. **Select** — If `agent` is present, call `selectNode(agent)` then
   `fitView({ nodes: [{ id: agent }] })` to center the graph on the node
   with a smooth animation.

5. **Once** — Deep-link application fires exactly once per page load.
   Subsequent WebSocket events do not re-trigger navigation.

## Edge Cases

| Scenario                              | Behavior                                        |
|---------------------------------------|--------------------------------------------------|
| Unknown subworkflow path segment      | Error banner with "not found" + notation hint     |
| Ambiguous bare name (multiple for_each iterations) | Error banner listing valid alternatives |
| Unknown agent name                    | No node selected, error banner displayed          |
| Subworkflow hasn't started yet        | Navigation fails with "not found" error           |
| Page refresh                          | Deep-link re-applied from URL (full state replay) |
| Combined with breadcrumb navigation   | User can freely navigate after deep-link applies  |

## Example URLs

```
# Root workflow — default view
http://localhost:49123

# Select an agent in the root workflow
http://localhost:49123?agent=intake

# Drill into a subworkflow
http://localhost:49123?subworkflow=planning

# Drill two levels deep
http://localhost:49123?subworkflow=planning/design

# Drill into subworkflow and select an agent within it
http://localhost:49123?subworkflow=planning/design&agent=reviewer

# for_each iteration by exact slot key
http://localhost:49123?subworkflow=plan_child[item-0]

# for_each iteration by positional index (# → %23 in URL)
http://localhost:49123?subworkflow=plan_child%230

# Nested: for_each iteration, then into a child subworkflow
http://localhost:49123?subworkflow=plan_child%230/design&agent=writer
```
