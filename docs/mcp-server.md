# MCP Server

`conductor mcp serve` exposes your registered Conductor workflows as
[Model Context Protocol (MCP)](https://modelcontextprotocol.io/) tools, so
any MCP-compatible host — Claude Code, VS Code, Cursor, or your own agent —
can discover and invoke them directly, with a typed input schema derived
from each workflow's own `input:` block.

> **This page is about Conductor *being* an MCP server.** For the mirror
> image — Conductor workflows *calling* MCP tools via `runtime.mcp_servers`
> — see [MCP Tools](mcp-tools.md).

## Quick Start

If you already have a registry configured, there is no workflow editing
required (G1):

```bash
# One-time: register a source of workflows, if you haven't already
conductor registry add official myorg/conductor-workflows --default

# Start the server
conductor mcp serve
```

Every workflow in every configured registry is exposed as a tool by
default. Point your MCP host at the command above (see
[Host Configuration](#host-configuration) below) and its `tools/list`
request will return one tool per workflow, named after the workflow's own
(slugified) name — no `conductor_` prefix, no registry-qualified name,
unless two registries publish a workflow with the same name (see
[Naming and collisions](#naming-and-collisions)).

## Host Configuration

`conductor mcp serve` speaks **stdio only** in v1 ([Limits](#limits),
DD9) — the transport every current MCP host spawns a local server over.
Configure your host to run the command as a subprocess.

### Claude Code

```json
{
  "mcpServers": {
    "conductor": {
      "command": "conductor",
      "args": ["mcp", "serve"]
    }
  }
}
```

Add this to `.mcp.json` in your project root, or register it globally with
`claude mcp add`.

### VS Code

Add to `.vscode/mcp.json` (or your user MCP settings):

```json
{
  "servers": {
    "conductor": {
      "type": "stdio",
      "command": "conductor",
      "args": ["mcp", "serve"]
    }
  }
}
```

### Cursor

Add to `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "conductor": {
      "command": "conductor",
      "args": ["mcp", "serve"]
    }
  }
}
```

Any of these can narrow or widen what gets exposed by adding flags to
`args`, e.g. `["mcp", "serve", "--registry", "official", "--allow", "release-*"]`.
See [`conductor mcp serve`](cli-reference.md#conductor-mcp-serve) in the
CLI reference for every flag.

## The Exposure Ladder

Whether a given workflow becomes a tool is decided by a four-rung ladder,
evaluated in this order — each rung outranks the ones below it (DD4):

1. **`--deny <glob>`** (repeatable) — the highest-precedence rung. A match
   excludes a workflow unconditionally, even one an `--allow` pattern also
   matches.
2. **`--allow <glob>`** (repeatable) — a non-empty `--allow` list switches
   the server into allow-list mode: only matching workflows are
   candidates, and a match overrides a workflow's own `mcp.expose: false`.
3. **The workflow's own `mcp.expose`** (see [The `mcp:` Block](#the-mcp-block)
   below) — `false` opts a specific workflow out.
4. **Default: exposed.** A workflow with no `mcp:` block, and not matched
   by `--deny`/`--allow`, is exposed. This is deliberate (DD4): opt-in
   exposure would expose **zero** tools from any existing registry, since
   no workflow anywhere carries an `mcp:` block yet.

`--registry <glob>` (repeatable) sits one level above this ladder — it
narrows which *registries* are even considered before the ladder runs.
`--workflow-dir <path>` (repeatable) adds workflows from a local directory
alongside, or instead of, any registry.

The startup summary the server prints to stderr (see
[Startup Summary](#startup-summary)) names every workflow it *does*
expose, its source registry, and its pinned identity — a workflow
silently gaining exposure because a registry gained one is only silent
if nothing says otherwise, and this is the channel that says otherwise.
It does not enumerate workflows the ladder excluded (via `--deny`, a
non-matching `--allow`, or `mcp.expose: false`); those are simply absent
from the published tool list.

## The `mcp:` Block

A workflow opts into non-default MCP behavior with a `mcp:` block under
`workflow:`. Every field defaults to the value that keeps an existing
workflow (with no block at all) exposed exactly as before — nothing here
is required:

```yaml
workflow:
  name: review-pr
  description: Reviews a pull request across correctness, tests, and security.
  mcp:
    expose: true # candidate for exposure (the default)
    mode: async # async (default) | sync | auto -- a default, not a mandate
    read_only: false
    destructive: true
    estimated_minutes: 8
```

| Field | Default | Meaning |
|---|---|---|
| `expose` | `true` | Whether this workflow is a candidate for exposure at all (rung 3 of the ladder above). |
| `mode` | `async` | The invocation mode a caller's omitted `_wait_seconds` resolves to. `sync` resolves to the server's `--max-wait-seconds` ceiling rather than an unbounded wait; `async`/`auto` return immediately. This is a **default a caller can still override** per call, not a mandate. |
| `read_only` | `false` | Surfaced as the generated tool's `readOnlyHint` annotation. |
| `destructive` | `false` | Surfaced as the generated tool's `destructiveHint` annotation. |
| `estimated_minutes` | unset | A client-side hint for how long a run typically takes; must be positive when set. |

`WorkflowDef` forbids unknown keys, so a typo (`expse: false`) is a schema
error `conductor validate` reports, not a silently-ignored value (FR11).
See `examples/mcp-serve.yaml` for a complete, runnable example.

## Toolsets

The server groups its tools into named toolsets, decided once at startup
from `--toolsets` and never re-evaluated per connection or per request
(DD3):

| Toolset | Default | Contents |
|---|---|---|
| `workflows` | **on** | One generated tool per exposed workflow (or, above `--max-direct-tools`, the two-tool discovery pair — see [Discovery Mode](#discovery-mode)). |
| `runs` | **on** | `conductor_run_status`, `conductor_await_run`, `conductor_cancel_run`, `conductor_list_runs` — see [The Run Lifecycle](#the-run-lifecycle). |
| `introspect` | off | `conductor_run_events`, `conductor_node_detail`, `conductor_plan_tree` — event-level and per-step detail for a run. |
| `diagnose` | off | `conductor_doctor`, `conductor_validate_workflow`, `conductor_run_logs` — environment health, pre-flight validation, and links to a run's raw logs. |

`introspect` and `diagnose` add **zero** tools to the default footprint —
enable them explicitly with
`--toolsets workflows --toolsets runs --toolsets introspect --toolsets diagnose`
(the option is repeatable, one toolset name per occurrence) when you need
failure diagnosis from the same MCP connection that started the run.

## Discovery Mode

Above `--max-direct-tools` (default 25) exposed workflows, the server
serves a fixed **two-tool discovery pair** instead of one tool per
workflow, so a large registry degrades predictably rather than silently
overflowing a host's tool-count limit (FR9, G7):

- **`conductor_find_workflow(query)`** — searches the exposed catalogue by
  name/description and returns matching workflow identities.
- **`conductor_run_workflow(name, inputs, ...)`** — invokes a workflow
  found via `conductor_find_workflow` by name. No tool anywhere accepts a
  filesystem path, URL, or registry source as a parameter (NFR3) — only a
  name already present in the frozen catalogue.

Whether the server is in direct or discovery mode is decided once at
startup from the exposed count, never re-decided later, and is named in
the startup summary.

## The Run Lifecycle

Invoking a workflow tool (in either direct or discovery mode) **never**
executes the workflow inside the server process. It always forks a
detached `conductor run ... --web` child, the same launch path
`conductor run --web-bg` uses, and returns a handle immediately or after a
bounded wait — the caller chooses per call (G3, G4):

- **`_wait_seconds`** — every generated workflow tool accepts this reserved
  parameter. `0` returns immediately; `> 0` blocks up to that many seconds
  for a terminal state, capped by the server's `--max-wait-seconds`
  ceiling regardless of the value requested; omitted defers to the
  workflow's own `mcp.mode`. Reaching a human gate ends a bounded wait
  early — see [Limits](#limits).
- Immediately, at a gate, on failure, or when a bounded wait's deadline is
  reached, the result is a **handle**: it always includes a **dashboard
  `url`** (G4), the run's pinned workflow identity, and its `run_id`. See
  [the result shapes](#no-outputschema-dd5) below for what a *completed*
  run returns instead.
- **`conductor_run_status(run_id)`** — status, current step, and (at a
  gate) the gate's prompt, options, and the dashboard approval URL.
  Works for a live run, a run parked at a gate, and a run whose process
  has already exited.
- **`conductor_await_run(run_id, wait_seconds=60)`** — blocks, bounded by
  `--max-wait-seconds`, returning on a terminal status **or** on reaching a
  gate; its timeout text names the approval URL as the next action.
- **`conductor_cancel_run(run_id, force=false)`** — stops a run through the
  same graceful-then-force ladder `conductor stop` uses, so a checkpoint is
  written when the graceful rung succeeds. Reports an already-terminal run
  as a distinct, non-error outcome.
- **`conductor_list_runs(status?, workflow?, limit=20)`** — over live and
  completed runs alike; `status="at-gate"` is the query that surfaces every
  parked run.

A `run_id` is answerable through this lifecycle **before, during, at a
gate, and after** the run — including once the launching process has long
since exited, via the terminal run record every run writes on completion
(see [`conductor status` / `conductor fleet list`](cli-reference.md#conductor-status)
and [R1's scope change](#r1-completed-runs-are-now-visible-by-default) below).

`--max-concurrent-runs` (default `0`, unbounded) bounds how many runs
*this server process* has launched and still has live; over the cap, a
launch is **rejected** with a message pointing at
`conductor_list_runs`/`conductor_cancel_run`, never queued. Restarting the
server resets the count — the server owns no execution state of its own,
so there is nothing else for it to remember.

## Startup Summary

Stdout is the JSON-RPC transport (DD9); nothing may write to it but
protocol bytes. So the server's one channel for operator-facing
information is **stderr** — the startup summary (FR10), printed once per
process start and also written through the standard `logging` module at
warning level for anomalies, so a host's own MCP log aggregation captures
them too:

```
conductor mcp serve: exposing 12 workflow(s) in direct mode (--max-direct-tools=25).
Toolsets enabled: runs, workflows.
  review_pr <- official/review-pr (pin: a1b2c3d4e5f6)
  summarize_topic <- official/summarize-topic (pin: 9f8e7d6c5b4a)
  ...
```

It names, in order: the exposed count and direct-vs-discovery mode; every
published tool with its source registry and pinned identity; every
workflow exposed with a degraded schema and why (see
[the schema resolution ladder](#no-outputschema-dd5) note below);
and every tool-name collision it qualified, naming both registries. This
is the only place a stdio server has to say "here is what I did" — use
[`conductor doctor`](cli-reference.md#conductor-doctor) to see the same
information without attaching a host at all.

## Naming and Collisions

Tool names are **bare** — the slugified workflow name, with no
`conductor_` prefix — unless two configured registries publish a workflow
with the same name, in which case **both** are qualified with their
registry (never just the loser), or `--tool-prefix` is set, which prefixes
every generated workflow tool name (DD10). The static run-lifecycle,
introspect, and diagnose tools always carry their `conductor_` prefix,
since there is nothing to disambiguate.

## Limits

The following are **deliberate boundaries**, not gaps to be worked around:

### No `outputSchema` (DD5)

`WorkflowDef.output` is a `dict[str, str]` of untyped Jinja2 templates, so
an honest `outputSchema` cannot currently be published — no invocation
result declares one (spec-legal, and what most MCP servers do), which
means a calling model learns the result shape by reading a result rather
than by contract. Three distinct shapes exist:

- **Immediate, at-gate, failed, or timed-out** — a handle: `structuredContent`
  carries `run_id`, `status`, the dashboard `url`, and the run's pinned
  workflow identity (plus a `gate`/`error` block where relevant).
- **Inline completion** (the rendered `output:` dict serialized to 50 KB
  or less) — `structuredContent` is the `output:` dict itself, with no
  wrapping envelope, plus a human-readable text fallback.
- **Spilled completion** (serialized `output:` over 50 KB) — the output is
  written to a file and `structuredContent` is `{run_id, status, note}`
  plus a `resource_link` to the file; the output is never embedded inline.

Deriving an `outputSchema` from referenced agents' typed `OutputField`s is
a tracked follow-up, not part of this release.

### A gate always parks (DD11)

The server **never** passes `--skip-gates` to a launched run. A run that
reaches a human gate reports `status: "at-gate"` with the gate's prompt,
options, and a dashboard approval URL, and **stays there** until a human
resolves it via the dashboard, `conductor gate respond`, or the Fleet TUI
— indefinitely, if nobody is watching. This is intentional: a gate is a
control the workflow's author deliberately placed in the path, and the
caller being a model rather than a person is the strongest argument for
keeping it, not for removing it. `conductor_await_run` returns as soon as
it reaches the gate rather than burning its budget waiting for one to
resolve itself, and `conductor_list_runs(status="at-gate")` enumerates
every parked run so none of them are silently forgotten. v1 ships no park
timeout and no reaper.

### Log tools return links, never contents (DD12)

`conductor_run_logs` (the `diagnose` toolset) returns `resource_link`
content blocks pointing at a run's `.bg.stderr.log`, `.bg.stdout.log`, and
`.events.jsonl`, plus bounded metadata (`size`, `modified_at`, `exists`)
and the terminal record's error type/message — **never file contents**,
regardless of size. Conductor has no redaction layer today, and these
files (including `runtime.tool_output` spill files) may contain secrets;
returning only paths routes the read through the consent boundary the
user already configured for their host's own file tools, rather than
Conductor becoming an unredacted exfiltration surface. A host with no
file-reading tool simply cannot follow the link — better than the
alternative of a globbed temp directory, but still a real limitation.

### Tool payloads are withheld by default (R4)

`conductor_run_events` (the `introspect` toolset) reduces each tool call's
`arguments` and `result` to `{name, status, byte_size}` by default —
`byte_size` is computed from the original serialized payload, so a caller
learns the size of what it is not being shown. Pass `--introspect-full`
at server startup to restore the original arguments and results. This
does **not** affect `conductor_node_detail`, which returns a run's prompt
and output in full regardless — that toolset's activity lines never
carried tool payloads to begin with.

### stdio only (DD9)

No Streamable HTTP transport, no OAuth, in v1. stdio is what every current
MCP host already spawns, needs no auth story beyond process ownership,
and has no observed tool-call timeout. `web/auth.py`'s existing token-file
model is the intended reuse point when HTTP transport lands.

### R1: completed runs are now visible by default

Every `conductor mcp serve` invocation launches a real `conductor run`
under the hood, and that run now writes a **terminal record** on exit —
which means `conductor status`, `conductor fleet list`, and the Fleet
TUI's History screen all gained a completed-runs section as a side effect
of this feature shipping, not something opt-in to this server. See the
[CHANGELOG](../CHANGELOG.md) and
[`conductor status`](cli-reference.md#conductor-status) /
[`conductor fleet list`](cli-reference.md#conductor-fleet-list) for the
full scope-change description and the `--live` flag that restores the
pre-change behavior.

## See Also

- [MCP Tools](mcp-tools.md) — the mirror image: Conductor workflows
  *calling* MCP tools via `runtime.mcp_servers`.
- [`conductor mcp serve`](cli-reference.md#conductor-mcp-serve) — full CLI
  flag reference.
- [Fleet Manager](fleet.md) — `conductor fleet`, terminal records, and
  retention, which every MCP-launched run participates in identically to
  a CLI-launched one.
- [Workflow Syntax Reference](workflow-syntax.md) — the full schema,
  including the `mcp:` block.
- [`conductor doctor`](cli-reference.md#conductor-doctor) — inspect what a
  server *would* expose without attaching a host.
