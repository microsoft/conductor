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

The `subworkflow` parameter is a `/`-separated path of **parent agent names**
that invoke each sub-workflow, starting from the root workflow.

Given this workflow nesting:

```
root
├── intake          (agent)
├── planning        (workflow agent → planning.yaml)
│   ├── architect   (agent)
│   └── design      (workflow agent → design.yaml)
│       ├── reviewer   (agent)
│       └── writer     (agent)
└── close_out       (agent)
```

| URL                                          | Result                                    |
|----------------------------------------------|-------------------------------------------|
| `?subworkflow=planning`                      | View planning.yaml's graph                |
| `?subworkflow=planning/design`               | View design.yaml's graph                  |
| `?subworkflow=planning/design&agent=reviewer` | View design.yaml, select reviewer node    |

Each path segment must match the `name` of the workflow-type agent in its
parent workflow — this is the same value shown in the breadcrumb bar.

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
| Unknown subworkflow path segment      | Navigation stops at the last valid level          |
| Unknown agent name                    | No node selected, graph shows default view        |
| Subworkflow hasn't started yet        | Navigation fails silently (no context exists)     |
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
```
