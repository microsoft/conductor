# Solution Design: `conductor mcp serve` — workflows as MCP tools

> Source issue: [microsoft/conductor#432](https://github.com/microsoft/conductor/issues/432)
> — *Feature: `conductor mcp serve` — expose workflows as MCP tools, with run
> introspection and diagnostics*. Status: `idea` (speculative, not yet
> committed). Absorbs and supersedes [#135](https://github.com/microsoft/conductor/issues/135).
>
> This document is a **solution design** for engineering and architecture
> review. It covers *what* and *why*; the epic/task breakdown and file-by-file
> changes belong to a separate planning step that consumes this design.
>
> **Verification note.** Every claim about Conductor's own code was re-checked
> against `main` at commit `0554517` and is cited by file and symbol. Every
> claim about the MCP specification and the Python SDK was checked against a
> primary source, and several claims carried over from the source issue were
> found to be **wrong** — they are corrected inline and flagged with ⚠️. Two
> load-bearing SDK behaviours were verified *empirically*, by installing both
> major versions into throwaway virtualenvs and running the imports and
> attribute accesses Conductor actually performs (see DD0).
>
> **Revision note (stakeholder review).** The five open questions this document
> previously carried have been answered and are now recorded as decisions DD4
> and DD10–DD13, with their consequences propagated through the requirements,
> data flows, security, and risk sections. Two of those answers prompted
> additional verification against primary sources — MCP host tool-name
> prefixing and the spec's own tool-naming rules (DD10), and the presence of
> `ResourceLink` in the pinned SDK (DD12) — and one previously-unverifiable
> claim (VS Code's 128-tool limit) was confirmed and upgraded in DD3. Two new
> questions raised *by* those answers are in **Open Questions**.

## Executive Summary

Conductor's blessed workflows are reachable today only by teaching an AI agent
the CLI through a hand-written skill — a wrapper that costs tokens on every
call and produces the weaker artifact, since a skill is advice a model may
ignore while a tool is a contract it invokes. This design adds
**`conductor mcp serve`**, a Model Context Protocol *server* that publishes the
workflows in a user's configured registries as MCP tools, so any MCP host
(Claude Code, VS Code/Copilot, Cursor) can invoke a governed, routed,
budget-capped, checkpointed workflow as a single typed tool call — and, when
one fails, can query its event log, per-step I/O, and environment health from
the same connection. Invocation is **async-first and always detached**: every
call launches the existing `--web-bg` child, so the run survives the MCP host
that started it, returns a dashboard URL in every mode, and lets the caller
choose per call whether to block. The design is unusually cheap because most of
its surface already exists — `WorkflowDef.input` is already a JSON Schema
property, the registry is already a workflow catalogue keyed to immutable
commit SHAs, and the Fleet Manager ([#431](https://github.com/microsoft/conductor/pull/431))
already derives run status, gates, per-agent detail, and per-step I/O from the
event log. The genuinely new work is a **terminal run record** (a run's record
is deleted when its process exits today, so a completed run cannot be looked up
by `run_id`), an **input-schema path that does not require a network fetch per
server spawn**, and the MCP protocol layer itself. The governing posture,
settled at review, is that the server **adds a caller, not an exemption**: every
registry workflow is exposed by default and named for the capability it
delivers, but a human gate still parks the run and returns an approval URL
rather than being auto-skipped, and diagnostic tools return links to log files
rather than their contents, since Conductor has no redaction layer to lean on.

## Decision Status & Review Ask

**What reviewers are asked to approve now:** the *architecture direction* (an
MCP server that deconstructs the registry into tools, with an always-detached
async run lifecycle and toolset-gated introspection) and the *scope boundary*
between v1 and follow-ups.

**Stakeholder review is complete for the five questions this document
previously carried.** Exposure posture, tool naming, log exposure, gate
behaviour, and terminal-record retention are now settled and appear below as
DD4 and DD10–DD13; each was a product or security call rather than an
engineering one, and each is recorded with the consequences propagated through
the requirements, security, and risk sections. Two *new* questions — both
direct consequences of those answers, neither answerable by reading the code —
are raised in **Open Questions**.

| Decision | Status | Notes |
|---|---|---|
| DD0 — bound `mcp` to `<2` immediately, independent of this feature | **Urgent** | Empirically verified live breakage of the *existing* client; see below |
| DD1 — reuse the Fleet Manager; add a terminal record, not a new index | Proposed | Materially smaller than the source issue's "Gap 1" proposal |
| DD2 — always detached; async default; caller-side `_wait_seconds` | Proposed | Carried from the issue's decision D, re-grounded |
| DD3 — static toolsets, startup-fixed | Proposed | Spec *forbids* varying the tool list per connection — see DD3 |
| DD4 — expose by default via a first-class `mcp:` schema block; `--allow`/`--deny` beat YAML | **Decided** (review) | Opt-in would expose zero tools on day one; operator flags are the counterweight |
| DD5 — inputs map directly; outputs ship unschematized in v1 | Proposed | `output:` is untyped; honest under-delivery |
| DD6 — pin every exposed workflow to a SHA / content hash at startup | Proposed | Spec offers no pinning primitive; we build it |
| DD7 — gates surface as an approval URL, never elicitation | Proposed | Elicitation's mechanism was *replaced* in spec `2026-07-28` |
| DD8 — shape run handles so `run_id` becomes a Tasks `taskId` later | Proposed | SDK already has the types; hosts do not |
| DD9 — stdio transport only in v1 | Proposed | HTTP + OAuth deferred |
| DD10 — bare tool names, registry-qualified only on collision | **Decided** (review) | Hosts already prefix by server, so bare ≠ unnamespaced |
| DD11 — always park at a gate; the server never passes `--skip-gates` | **Decided** (review) | Auto-skip would silently discard an author's approval step |
| DD12 — log tools return `resource_link`s + bounded metadata, never contents | **Decided** (review) | ⚠️ No redaction exists anywhere to reuse; this avoids building one |
| DD13 — terminal records bounded by `[fleet.retention].keep_last`, same sweep | **Decided** (review) | Records and their event logs disappear together |

---

## Background

### What exists today

Conductor is a CLI that executes multi-agent workflows defined in YAML. The
pieces this design builds on are all present and load-bearing.

**Workflow definition.** `WorkflowDef` (`config/schema.py:3529`) declares
`name`, `description`, `version`, `entry_point`, `input`, `metadata`, and
runtime settings, with `model_config = ConfigDict(extra="forbid")`.
`WorkflowDef.input` is `dict[str, InputDef]` (`:3549`), and `InputDef`
(`:50–92`) carries exactly `type` (one of `string` / `number` / `boolean` /
`array` / `object`), `required: bool = True`, `default: Any`, and
`description: str | None`. That is already a JSON Schema property in all but
name. `WorkflowConfig.output` (`:3614`) is `dict[str, str]` — Jinja2 template
strings, with no declared types.

**Registries.** `RegistriesConfig` (`registry/config.py:41–48`) holds
`default: str | None` and `registries: dict[str, RegistryEntry]`, persisted at
`~/.conductor/registries.toml` (honouring `$CONDUCTOR_HOME`). A `RegistryEntry`
is `{type: github | path, source: str}`. `RegistryIndex`
(`registry/index.py:34`) is `{workflows: dict[str, WorkflowInfo]}`, loaded from
an `index.yaml` or `index.json` at the registry root. `WorkflowInfo`
(`:26–32`) is `{description: str = "", path: str}` — **and nothing else**.
For GitHub registries, `version_resolver.materialize_to_sha` resolves a ref to
a full immutable commit SHA, and `registry/cache.py` mirrors fetched files
under `$CONDUCTOR_HOME/cache/registries/<registry>/<sha[:12]>/…` with a
`.complete` readiness sentinel written last. `resolve_ref` +
`resolve_and_fetch` are the pair `conductor show` already uses to turn a
reference like `qa-bot@my-registry@1.0.0` into a local path.

**Background execution.** `cli/bg_runner.py::launch_background()` forks a
genuinely detached child (`start_new_session=True` on POSIX;
`CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB` on Windows) running
`conductor run --web`, captures its stdout/stderr to files, and returns a
`BackgroundLaunch(url, stderr_log, stdout_log, run_id, workflow_started,
still_running)`. Its launch health gate is three-staged: a socket-connect loop
that watches `proc.poll()`, then a poll of the child's own run record, then a
poll of `GET /api/info` until it reports a `started_at` key — so a returned
launch means the *engine* started, not merely that a port opened
([#410](https://github.com/microsoft/conductor/issues/410)).

**The dashboard.** `web/server.py` serves `/api/state`, `/api/info`,
`/api/logs`, `/api/gate-status`, `/api/files/{path}`, `/ws`, and the mutating
`/api/stop`, `/api/kill`, `/api/resume`, `/api/gate-respond`, `/api/guidance`.
`web/auth.py` mints a per-run token and persists it at
`~/.conductor/runs/dashboard-<port>.token` (mode `0600`), with
`resolve_cli_token(port, token)` as the shared `--token` > `CONDUCTOR_GATE_TOKEN`
> token-file resolver that `conductor gate respond`, `conductor guide`, and
`conductor stop` all use.

**Diagnostics.** `providers/diagnostics.py::gather()` returns a `DoctorReport`
whose every component implements `to_dict()`. `conductor doctor` is a thin
renderer over it.

### What changed: the Fleet Manager

The source issue was written before the Fleet Manager
([#431](https://github.com/microsoft/conductor/pull/431), merged as `d785a28`)
landed, and it changes this design materially. Every `conductor run` and
`resume` — foreground, `--web`, and `--web-bg` alike — now writes a
`RunRecord` to `<run_records_dir>/<run_id>.json`:

```python
@dataclass(frozen=True)
class RunRecord:                      # fleet/records.py:297
    run_id: str
    pid: int
    workflow_path: str
    workflow_name: str
    started_at: str
    event_log_path: str
    port: int | None
    mode: RunMode                     # Literal["fg", "fg-web", "bg"]
    checkpoint_dir: str | None
```

More importantly, `fleet/summary.py` is already a general **event-log query
layer**, not a TUI helper:

| Function | Returns | What it derives |
|---|---|---|
| `derive_run_summary(record)` | `RunSummary` | status, current step, elapsed, tokens, cost, gate, topology, inputs |
| `derive_run_detail(record)` | `RunDetail` | per-agent status/elapsed/tokens/cost across the whole run |
| `derive_step_detail(record, agent_name)` | `StepDetail` | one step's rendered prompt, structured output, and activity stream |
| `read_event_log_head/tail/full(path)` | `list[dict]` | bounded JSONL reads |

`RunSummary.status` is a `Literal["running", "at-gate", "paused",
"completed", "failed"]` derived from explicit event markers
(`gate_presented` / `gate_resolved` / `workflow_completed` / `workflow_failed`)
and never inferred from timing. `GateInfo` carries the gate's
`agent_name`, `prompt`, `options`, and full `option_details` — parsed from the
`gate_presented` event, which the engine emits with every option's `label`,
`value`, `route`, `prompt_for`, and `multiline` (`engine/workflow.py:4592`).
Note this is strictly richer than the live `GET /api/gate-status`, which
returns only `{waiting, agent_name, prompt_id}`.

`fleet/launch.py::launch_workflow(workflow_path, raw_values, input_defs, …)`
already validates and type-coerces raw input values against `InputDef`s and
delegates to `launch_background()`.

**The consequence for this design is large: roughly the whole `introspect`
toolset the source issue proposed to build already exists as library code.**
It has one consumer (the TUI) and no protocol surface.

---

## Problem Statement

### P1 — Making a workflow callable costs a hand-written skill, and buys a weaker artifact

The only way to let an agent invoke a Conductor workflow is to write a skill
that teaches it the CLI. The bundled `plugins/conductor/skills/conductor/` is
~117KB (~29K tokens) and is injected eagerly on non-native providers; two
installed plugins in the wild (`ship`, `fusion`) each carry an entire
`SKILL.md` whose substantive job is one `conductor run --web-bg` invocation.
That is the rule of three, arrived at without anyone planning it.

The deeper problem is not cost but *kind*. A skill is prose the model may
paraphrase, half-read, or skip under context pressure — which is exactly what
the bundled skill's own "do not simulate the workflow yourself" warnings are
defending against. A tool call has a typed schema, a defined result, and no
opportunity to freelance. Conductor already enforces routing, gates, retries,
budget caps, schema-validated agent outputs, and a checkpointed audit trail;
none of that governance is reachable through an interface that depends on the
model choosing to follow instructions.

### P2 — A failed run has no structured explanation

When a run misbehaves, the recovery path is to locate
`$TMPDIR/conductor/conductor-<name>-<ts>-<run_id>.events.jsonl` by globbing a
temp directory and parse it by hand
([#116](https://github.com/microsoft/conductor/issues/116)). An agent that
invoked a workflow and got back "failed" has no way to find out *why* without
being taught that filename convention. This was [#135](https://github.com/microsoft/conductor/issues/135);
it is not a separate product, it is the error path of P1.

### P3 — A completed run cannot be looked up by `run_id`

This is the hard prerequisite for any async invocation model, and it is
**still open after the Fleet Manager**, though for a narrower reason than the
source issue described.

`cli/run.py` calls `_remove_run_record_for_current_process_safe()`
unconditionally in the `finally` blocks of both `run_workflow_async` and
`resume_workflow_async` (`:2274`, `:3018`), which deletes
`<run_id>.json`. Independently, `read_run_records()` filters to processes
passing `is_process_alive` and *deletes* the records of those that are not
(`fleet/records.py:802`). So the moment a run finishes, its record is gone:

- `read_run_records()` — cannot see it (filtered, then pruned).
- `read_run_record(run_id)` — does not check liveness, but the file has been
  deleted, so it returns `None`.
- `fleet/history.py::build_history_entries()` — enumerates completed runs, but
  does so by globbing `$TMPDIR/conductor/*.events.jsonl` directly, because (as
  its own module docstring says) "a completed run's record has already been
  removed by the time this screen would show it". It offers **list only, no
  lookup by `run_id`**, is capped at 200 entries, and its `HistoryEntry`
  carries outcome/timing/tokens/cost but **not the workflow's rendered
  `output:` dict and not the error message** — precisely the two things a tool
  caller needs.
- `find_event_log_for_run(run_id, started_at)` (`fleet/records.py:856`) can
  find the log, but requires `started_at` to disambiguate and returns `None`
  when it cannot, because a resumed run deliberately reuses its predecessor's
  id.

An agent that starts a 20-minute workflow and checks back afterwards — the
*normal* case — therefore has nothing to read.

### P4 — The registry index cannot answer `tools/list`

`WorkflowInfo` is `{description, path}`. It carries no `input`, so building an
MCP `inputSchema` requires resolving, fetching, and parsing the workflow YAML
itself. MCP stdio servers are respawned aggressively by hosts (per session, per
config reload, per restart), so a 20-workflow GitHub registry would mean 20
HTTP round-trips before the server can answer its first `tools/list`.

Two further hazards make fetch-and-parse unreliable rather than merely slow:

- `config/loader.py::resolve_env_vars` **raises** `ConfigurationError` when a
  `${VAR}` reference has no value and no `:-default`. A workflow that
  interpolates an environment variable the MCP server process does not happen
  to have will fail to load — so its tool would vanish from the catalogue for
  environmental reasons unrelated to the workflow's validity.
- `registry/cache.py::fetch_workflow` mirrors the workflow's **own directory**
  from the source repository — the workflow file plus every sibling in the
  same directory, fetched best-effort (`cache.py:785–806`; the `Raises:` note
  at `:412` confirms sibling failures are swallowed, not propagated). A
  same-directory reference like `!file ./prompt.md` therefore *does* resolve
  from the cache. Only a reference that climbs to a **parent** directory, such
  as `!file ../AGENTS.md`, cannot: `fetch_workflow` never mirrors anything
  above the workflow's own directory, so that include fails to resolve from
  the cache regardless of how the sibling-fetch step goes. (This is a
  pre-existing constraint of registry-distributed workflows, not one this
  design introduces, but it becomes visible at scale here.)

### P5 — The dependency floor is unbounded, and the ceiling has already shipped

`pyproject.toml` declares `mcp>=1.28.1` with no upper bound; `uv.lock` pins
`1.28.1`. **`mcp` 2.0.0 is a final, non-yanked release, uploaded 2026-07-28**,
and it requires `httpx2>=2.5.0` and `mcp-types==2.0.0`. Any lock refresh jumps
a major version. This is a live hazard to the *existing* MCP client, entirely
independent of building a server — see DD0 for the empirical result.

---

## Goals and Non-Goals

### Goals

1. **G1** — A user who has already run `conductor registry add` gets their
   workflows as MCP tools from `conductor mcp serve` with **no further
   configuration and no workflow edits**, in any MCP host that speaks stdio.
2. **G2** — Each generated tool publishes a faithful `inputSchema` derived
   from `WorkflowDef.input`, so the calling model gets a typed contract rather
   than prose.
3. **G3** — Invocation is **async-first and always detached**: a run survives
   the MCP host process, and the caller can choose per call whether to block
   and for how long.
4. **G4** — Every invocation returns a dashboard URL, in every mode.
5. **G5** — A run can be queried by `run_id` — status, progress, result,
   failure — **after it has finished and its process has exited**.
6. **G6** — A failing run is diagnosable from the same connection: event
   query, per-step I/O, environment health, pre-flight validation, and a
   resolvable link to its raw launch logs.
7. **G7** — The default tool footprint stays small enough to be safe in hosts
   with tool-count limits, and degrades predictably rather than silently when
   a registry is large.
8. **G8** — Every exposed workflow is pinned to an immutable identity at
   session start, and drift is reported rather than silently applied.
9. **G9** — Server startup does not depend on the network when the registry
   cache is warm.
10. **G10** — Every governance control a workflow author declared still applies
    when the caller is a model: gates gate, budgets cap, schemas validate. The
    server adds a caller, not an exemption.

### Non-Goals

- **Streamable HTTP transport and OAuth.** stdio only in v1 (DD9).
- **Migrating to `mcp` 2.x / spec `2026-07-28`.** v1 targets the 1.x SDK
  (DD0); the migration is a tracked follow-up.
- **Implementing the MCP Tasks extension.** No host supports it (DD8).
- **Elicitation-based gates.** Its mechanism was replaced in the current spec
  (DD7).
- **Auto-skipping human gates.** The server never passes `--skip-gates`; a
  gate parks the run and returns an approval URL (DD11).
- **Returning log or event-log file *contents* over the protocol.** Diagnostic
  tools return `resource_link`s and bounded metadata; the host's own file tools
  fetch contents under the user's existing consent (DD12).
- **Building a redaction layer.** Conductor has none today, and DD12 is
  specifically the design that avoids needing one for v1.
- **Publishing an `outputSchema`.** Blocked on typing `output:` (DD5).
- **Conductor *calling* MCP tools deterministically.** That is the mirror
  image, [#392](https://github.com/microsoft/conductor/issues/392)'s
  `type: mcp` step.
- **#135's authoring skill kit** (scaffolds, validation skills, dry-run
  helpers). Content, not protocol; partly covered by the bundled skill since.
- **A general-purpose "run any YAML at this path" tool.** Never (Security).
- **Auto-discovering registries.** Only registries the user explicitly added.

---

## Requirements

### Functional

| ID | Requirement |
|---|---|
| FR1 | `conductor mcp serve` starts an MCP server over stdio, serving every registry in `RegistriesConfig.registries` by default. |
| FR2 | `--registry` / `--allow` / `--deny` (repeatable, glob-capable) narrow the exposed set; `--workflow-dir` additionally exposes a local directory. All are **startup arguments only**, never tool parameters. Precedence, highest first: `--deny` > `--allow` > `mcp.expose` > default `true` (DD4). |
| FR3 | Each exposed workflow becomes one tool whose `inputSchema` is derived from `WorkflowDef.input`, with `required`, `default`, and `description` preserved. Tool names are **bare** (the slugified workflow name), registry-qualified only on collision; `--tool-prefix` optionally prefixes all generated names (DD10). |
| FR4 | Invoking a workflow tool always launches a detached run via `launch_background()` and returns a handle containing `run_id`, dashboard `url`, workflow identity hash, and status. |
| FR5 | Every generated tool accepts a reserved `_wait_seconds` parameter: `0` forces immediate return; `> 0` blocks up to N seconds for a terminal state, capped at a server-side hard ceiling (`--max-wait-seconds`, default 300) regardless of the value requested; omitted defers to the workflow's declared `mcp.mode`, with a `mode: sync` workflow resolving to that same ceiling rather than an unbounded wait. |
| FR6 | `conductor_run_status`, `conductor_await_run`, `conductor_cancel_run`, `conductor_list_runs` operate on a `run_id` and work for **live and completed** runs alike. |
| FR7 | A run parked at a human gate reports `status: at-gate` with the gate's prompt, options, and a dashboard approval URL. The server **never** passes `skip_gates=True` to `launch_background()` (DD11). |
| FR8 | Toolsets `introspect` and `diagnose` expose event query, per-step detail, plan tree, `doctor`, and `validate`. `conductor_run_logs` returns `resource_link`s plus bounded metadata — never file contents (DD12). |
| FR9 | When the exposed workflow count exceeds `--max-direct-tools`, the server serves a discovery pair instead of per-workflow tools, and logs that it did so. |
| FR10 | The server logs a startup summary naming every exposed tool, its source registry, its pinned identity, and any name collisions it qualified. |
| FR11 | `conductor validate` reports `mcp:` block errors, as it does for every other schema block. |
| FR12 | Terminal run records are bounded by `[fleet.retention].keep_last` and pruned by the same sweep that prunes event logs, so a record and the log it points at disappear together (DD13). |

### Non-functional

| ID | Requirement |
|---|---|
| NFR1 | Cold-start to first `tools/list` response ≤ 2s with a warm registry cache, with **zero network I/O**. |
| NFR2 | A workflow whose schema cannot be resolved is exposed with a permissive schema and a description saying so — never silently dropped. |
| NFR3 | No tool **accepts** a filesystem path, URL, or registry source as a parameter. (Returning a path *outward*, as DD12's `resource_link`s do, is the opposite direction and is not constrained by this rule.) |
| NFR4 | Any YAML-authored text reaching a tool `description` is sanitized and length-capped. |
| NFR5 | Adding the server must not change the behaviour of any existing command; the run-record change must not alter `conductor stop` / `fleet list` semantics for live runs. |
| NFR6 | Every tool result is bounded in size; large payloads are returned as a `resource_link` rather than inline. Log-bearing payloads are **always** a `resource_link`, regardless of size (DD12). |
| NFR7 | The MCP SDK dependency is version-bounded such that a lock refresh cannot silently change major versions. |

---

## Proposed Design

### Architecture Overview

```
        MCP host (Claude Code / VS Code / Cursor)
                     │  stdio, JSON-RPC
        ┌────────────▼─────────────────────────────────────────┐
        │  conductor mcp serve                                 │
        │                                                      │
        │  ┌── Catalogue ───────────┐  ┌── Toolsets ────────┐  │
        │  │ registries.toml        │  │ workflows  (N)     │  │
        │  │ RegistryIndex          │  │ runs       (4)     │  │
        │  │ + schema resolution    │  │ introspect (3) off │  │
        │  │ + pin (SHA / hash)     │  │ diagnose   (3) off │  │
        │  │ + sanitize + collide   │  │ discovery  (2) auto│  │
        │  └───────────┬────────────┘  └─────────┬──────────┘  │
        │              │                          │             │
        │  ┌───────────▼──────────────────────────▼──────────┐  │
        │  │ Invocation & lifecycle                          │  │
        │  │  launch → handle → (optional bounded wait)      │  │
        │  └───────────┬─────────────────────┬───────────────┘  │
        └──────────────│─────────────────────│──────────────────┘
                       │                     │
        ┌──────────────▼──────────┐   ┌──────▼─────────────────────┐
        │ cli/bg_runner.py        │   │ fleet/ query layer          │
        │ launch_background()     │   │ records · summary · history │
        │  → detached child       │   │ derive_run_summary/detail   │
        └──────────────┬──────────┘   └──────▲─────────────────────┘
                       │                     │ reads
        ┌──────────────▼─────────────────────┴─────────────────────┐
        │ Detached run: engine + web dashboard (own process)             │
        │  ~/.conductor/runs/<run_id>.json               (live record)  │
        │  ~/.conductor/runs/terminal/<run_id>.json  (terminal record) ★│
        │  $TMPDIR/conductor/*.events.jsonl              (event log)    │
        │  dashboard :port  → /api/info /api/stop /api/gate-respond     │
        └─────────────────────────────────────────────────────────────┘
                                       ★ = the one genuinely new artifact
```

The organising principle is that **the MCP server owns no execution state**. It
is a protocol adapter over three existing subsystems: the registry (catalogue),
`bg_runner` (launch), and `fleet` (query). It holds no run in memory, so
killing it loses nothing; a run started by one server instance is fully
queryable by the next.

### Key Components

#### 1. Catalogue builder (`mcp/serve/catalogue.py`)

Turns configuration into a frozen list of tool definitions at startup.

```
build_catalogue(
    registries: list[str] | None,      # None = all in RegistriesConfig
    workflow_dirs: list[Path],
    allow: list[str], deny: list[str],
    max_direct_tools: int,
) -> Catalogue
```

Responsibilities, in order: enumerate registries → load each `RegistryIndex` →
apply `allow`/`deny` and each workflow's `mcp.expose` → resolve an
`inputSchema` per workflow (see below) → pin an identity → sanitize
descriptions → detect and qualify name collisions → decide direct-tools vs
discovery mode. The result is immutable for the process lifetime, which is what
DD3 requires.

**Exposure precedence** is a fixed four-rung ladder, highest first (DD4):

| Rung | Rule |
|---|---|
| 1 | `--deny <glob>` — matched workflows are excluded, unconditionally. Deny beats allow. |
| 2 | `--allow <glob>` — if given at least once, the server switches to allow-list mode: only matching workflows are candidates, **and a match overrides `mcp.expose: false`**. |
| 3 | `mcp.expose` in the workflow YAML — `false` hides it. |
| 4 | Default — exposed. |

`--registry` operates one level above the ladder: it selects which registries
are enumerated at all, so nothing outside the selected set is ever a candidate.
The operator flags beating YAML is the deliberate counterweight to default-on
exposure — an operator can constrain (or force) a registry they do not own
without forking it.

**Schema resolution is a three-tier ladder**, and this is what makes NFR1
achievable:

1. **Index-provided** — read optional `input:` and `mcp:` blocks straight from
   `WorkflowInfo`. Requires extending `WorkflowInfo` with two optional fields;
   absent fields keep every existing index working unchanged.
2. **SHA-keyed parse cache** — a parsed, normalized tool definition stored
   beside the existing mirror under
   `$CONDUCTOR_HOME/cache/registries/<registry>/_meta/<sha[:12]>/`. A SHA-keyed
   entry is immutable, so a warm cache makes startup a no-network operation.
   This reuses `registry/cache.py`'s existing layout, sentinel convention, and
   `CACHE_LAYOUT_VERSION` invalidation rather than inventing a second cache.
3. **Fetch and parse**, under a startup deadline. On failure — including the
   `${VAR}`-missing and `!file`-unresolvable cases from P4 — the workflow is
   still exposed with a permissive `{"type": "object"}` schema and a
   description stating that its parameters could not be resolved (NFR2). A
   tool that is present but coarse is strictly better than one that vanishes
   for an environmental reason.

#### 2. Tool generator

Maps `InputDef` → JSON Schema property. The mapping is direct and total for
scalars:

| `InputDef` | JSON Schema |
|---|---|
| `type: string \| number \| boolean \| array \| object` | `{"type": …}` — identical vocabulary |
| `required: true` | name appears in the schema's `required` array |
| `default` | `{"default": …}` |
| `description` | `{"description": …}` |

⚠️ **Known fidelity gap.** `InputDef` has no `enum`, no `integer`, and no
`items` / `properties`, so an `array` or `object` input publishes as untyped.
This is an asymmetry inside Conductor's own schema, not an MCP limitation:
`OutputField` (`config/schema.py:95–219`) *does* carry `items`, `properties`,
`enum`, `pattern`, `minimum`, `maximum`, `minLength`, `maxLength`, and
`nullable`. The design **does not invent** structure the author did not
declare; enriching `InputDef` toward `OutputField`'s vocabulary is a natural
follow-up that improves `conductor show`, `fleet`'s New Run screen, and this
feature at once.

⚠️ **Reserved-parameter collision.** `WorkflowDef.input` keys are arbitrary
strings, so a workflow can legitimately declare an input named `_wait_seconds`
— which the reserved parameter (FR5) would then silently shadow — or an input
whose name is not a valid tool-parameter identifier (`pr-number`, with a
hyphen). The catalogue builder must reject a workflow whose `input:` collides
with `_wait_seconds` at build time, the same way it rejects other catalogue
problems (logged per FR10), rather than exposing a tool whose contract the
YAML did not describe. Whether other input names pass through to the
generated schema verbatim, or get a normalization pass of their own, is left
to implementation; this design does not invent one.

#### 3. Invocation layer

The single most important property: **a workflow tool call never executes a
workflow inside the MCP server process.** It always calls
`launch_background()`. `_wait_seconds` changes only whether the tool call waits
for the detached child.

Input values arrive already JSON-typed from the MCP host, so they bypass the
string-coercion half of `fleet/launch.py::launch_workflow` but reuse its
required-input validation; the launch itself goes through the same
`launch_background()` call, per that module's standing warning that
re-implementing detached spawning would make a launched run die with its
launcher.

#### 4. Run lifecycle & the terminal record

`conductor_run_status(run_id)` resolves through one code path with two sources:

```
read_run_record(run_id)  ──found & alive──►  derive_run_summary(record)      (live)
        │                                     └─ enrich from GET /api/info
        └──not found──►  read_terminal_record(run_id)  ──►  RunSummary-shaped (finished)
                                 └──not found──►  find_event_log_for_run(...)  (crash / pre-existing fallback)
```

⚠️ **A crashed or `kill -9`'d run produces no terminal record.** The tombstone
is written in `cli/run.py`'s `finally`, so it exists only for a run that
unwinds normally; a process that dies before that `finally` runs — the exact
scenario #116 and G6/DD12 are about diagnosing — leaves only a live record
that `read_run_records()` eventually prunes for a dead `pid`, and no
`terminal/<run_id>.json` behind it. The third rung above,
`find_event_log_for_run(...)`, is the fallback that actually covers this case
by locating the event log directly rather than through either record; it is
not merely a compatibility shim for pre-existing runs, and it is what bounds
what `conductor_run_status` can promise for a run that never exits cleanly.

The **terminal record** is the one new artifact. When `cli/run.py`'s `finally`
removes the live record, it writes a companion file to
`run_records_dir()/"terminal"/<run_id>.json`: the same identifying fields plus
terminal status, ended-at, the rendered `output:` dict, the error type and
message on failure, token/cost totals, and the paths to the event log and bg
capture logs. It is small, so it *could* outlive the event log it points at —
but by DD13 it deliberately does not: `[fleet.retention]` (default
`keep_last = 200`) prunes event logs, and "pruning an event log makes that
run's history permanently unavailable" (`docs/fleet.md`), so a record whose
log is gone would advertise a run whose detail cannot be fetched.

**Why a subdirectory, not a sibling file.** An earlier version of this design
put the tombstone directly beside the live record, as `<run_id>.done.json` in
`run_records_dir()` itself. That collides with two functions that already
non-recursively glob `run_records_dir().glob("*.json")`
(`fleet/records.py:1019`, `:1104`). `read_run_records()` compares each file's
`Path.stem` against the record's own `run_id` field
(`require_run_id_match=True`); `Path("<run_id>.done.json").stem` is
`"<run_id>.done"`, not `"<run_id>"`, so the identity check fails and the file
is deleted outright as corrupt (`fleet/records.py:794`) — irrespective of
liveness. Separately, `remove_run_record_for_current_process()` globs the same
directory and returns on the *first* file whose `pid` field matches the
current process; a tombstone written before that call in the same `finally`
carries the same `pid` as the run that just ended, so it can win that race
instead of the live record, leaving the live record orphaned. Filing the
tombstone one directory down avoids both: `Path.glob("*.json")` is
non-recursive, so neither function — nor an older Conductor sharing the same
`run_records_dir()` — ever lists a file under `terminal/`.

Writing it in the existing `finally` means no new engine plumbing, and it
composes with the existing `remove_run_record_for_current_process()` by living
entirely outside the directory that function scans, rather than depending on
write ordering within it. It must inherit that function's "never raises"
contract.

**Retention (DD13).** `fleet/retention.py::_prune_event_logs_impl` already
matches a run's `.bg.stderr.log` / `.bg.stdout.log` companions to its event log
by the shared `run_id` embedded in each filename (`_companion_paths`, and the
module docstring's note that the `ts` segments can differ by a clock tick). The
terminal record is a **fourth companion** of the same run, matched the same way
— by `run_id`, not filename prefix — and deleted in the same sweep, so the four
artefacts of one run are never split apart. It lives under
`run_records_dir()/"terminal"/` rather than `event_log_root()`, so the sweep
gains one extra directory to look in and no new policy language. The existing
`keep_last < 1` guard ("prune nothing", not "delete everything") is inherited
unchanged.

#### 5. Toolsets

| Toolset | Default | Tools | Backing |
|---|:---:|---|---|
| `workflows` | on | *N generated* | `bg_runner.launch_background` |
| `runs` | on | `conductor_await_run`, `conductor_run_status`, `conductor_cancel_run`, `conductor_list_runs` | `fleet/records` + `fleet/summary` + `/api/stop`\|`/api/kill` |
| `introspect` | off | `conductor_run_events`, `conductor_node_detail`, `conductor_plan_tree` | `read_event_log_full`, `derive_step_detail`, `WorkflowConfig` |
| `diagnose` | off | `conductor_doctor`, `conductor_validate_workflow`, `conductor_run_logs` | `providers/diagnostics.gather()`, `cli/validate`, `ResourceLink`s to the bg capture logs (DD12) |
| `discovery` | auto | `conductor_find_workflow`, `conductor_run_workflow` | replaces `workflows` above the cap |

Default footprint is **N + 4**. `introspect` and `diagnose` are almost entirely
thin adapters over `fleet/summary.py` and `providers/diagnostics.py`, which is
why absorbing #135 is cheap rather than a second product. Note the split of
responsibility DD12 creates: `conductor_doctor` and
`conductor_validate_workflow` return *structured reports* the server generated
itself and are unaffected; only `conductor_run_logs` — the one tool whose
payload is verbatim third-party text — is reduced to links and metadata.

### Data Flow

**A: invoke and return a handle (`_wait_seconds` omitted or `0`)**

1. Host calls `review_pr({pr_number: 42})`.
2. Server validates against the pinned `inputSchema`; rejects unknown/missing.
3. `launch_background(workflow_path=<cached path>, inputs={...}, web_port=0)`.
4. `bg_runner` forks the detached child, waits for the port, waits for the
   child's own run record, then polls `/api/info` for `started_at`.
5. Server returns `structuredContent`:
   `{run_id, status: "running", url, workflow: {name, registry, pinned_sha},
     started_at, next: "call conductor_await_run(run_id) …"}`
   plus a human-readable text block.

**B: invoke and wait (`_wait_seconds: 120`)**

Steps 1–4 identical — *the run is detached either way*. The server then polls
the run's state, emitting `notifications/progress` as steps complete, until a
terminal status or the deadline. On deadline it returns the same handle plus
current progress and an instruction to call `conductor_await_run` again; the
run keeps going. On completion it returns the rendered `output:` dict as
`structuredContent`.

**C: status of a finished run**

`read_run_record` misses → `read_terminal_record` hits → return status,
output, cost, and log paths. No process, port, or event log needed.

**D: a gate is reached**

The engine emits `gate_presented` with full `option_details`. Any status query
derives `status: "at-gate"` with `GateInfo` via `derive_run_summary`, and
returns the dashboard URL as the approval target. The caller (or its human)
resolves it via the dashboard, `conductor gate respond`, or the TUI — all of
which already share one HTTP endpoint and one token resolver. Because every
MCP-launched run has a dashboard port by construction (DD2), it is *always*
gate-resolvable, which `docs/fleet.md` notes is not true of a plain foreground
run. The run **parks indefinitely** — the server never passes
`skip_gates=True` (DD11), so nothing auto-selects an option on the caller's
behalf.

**E: diagnosing a failed run**

`conductor_run_status` reports `failed` with the error type and message from the
terminal record. `conductor_run_events(run_id, …)` and
`conductor_node_detail(run_id, agent)` answer *where* it failed from the
structured event log. `conductor_run_logs(run_id)` answers the one thing the
event log cannot — a child that died before the engine emitted anything — and
does so by returning `ResourceLink` content blocks (`file://` URIs for the
`.bg.stderr.log` / `.bg.stdout.log` / `.events.jsonl` artefacts) plus their
sizes, modification times, and the run's terminal event summary. The host's own
file-reading tool fetches the bytes, under the file-access consent the user has
already granted it (DD12).

### API Contracts

**Workflow tool (generated).** The name is the **bare** slugified workflow name
— no `conductor_` prefix (DD10). On collision, *all* colliding tools are
qualified with their registry (`official_review_pr`, `team_review_pr`) rather
than only the loser, so a name never silently changes meaning when an unrelated
registry is added. Slugification lowercases and maps every character outside the
spec's recommended set to `_`, additionally folding `-` to `_` so a generated
name matches the snake_case of the server's own static tools; the catalogue
holds the reverse `tool name → (registry, workflow name)` map it needs for
invocation anyway. MCP `2026-07-28` recommends names be 1–128 characters drawn
from `A-Za-z0-9_-.` and unique within a server — the generator enforces all
three.

```jsonc
{
  "name": "review_pr",
  "description": "<sanitized workflow.description> (async; ~8 min)",
  "inputSchema": {
    "type": "object",
    "properties": {
      "pr_number": {"type": "number", "description": "..."},
      "depth":     {"type": "string", "default": "standard"},
      "_wait_seconds": {"type": "number",
        "description": "0 = return immediately; >0 = wait up to N seconds."}
    },
    "required": ["pr_number"]
  },
  "annotations": {"readOnlyHint": false, "destructiveHint": true,
                  "idempotentHint": false, "openWorldHint": true}
}
```

**Run handle (every invocation, every mode).**

```jsonc
{ "run_id": "a1b2c3d4", "status": "running",
  "url": "http://127.0.0.1:8763",
  "workflow": {"name": "review-pr", "registry": "official",
               "pinned": "sha:9f2c1e0b4a77"},
  "started_at": 1786100000.0, "next": "..." }
```

`status` reuses the Fleet Manager's existing vocabulary — `running` /
`at-gate` / `paused` / `completed` / `failed` — rather than inventing a second
one. Those five map cleanly onto MCP Tasks' `working` / `input_required` /
`completed` / `failed` / `cancelled` when hosts ship it (DD8).

**Lifecycle tools.**

| Tool | Signature | Notes |
|---|---|---|
| `conductor_run_status` | `(run_id)` | Live or finished. Includes `GateInfo` when at a gate. |
| `conductor_await_run` | `(run_id, wait_seconds=60)` | Bounded. Emits progress. On timeout returns an instruction, not a bare blob. |
| `conductor_cancel_run` | `(run_id, force=false)` | `POST /api/stop` then `/api/kill`; routes through `handle_dashboard_stop` so a checkpoint is written. Also the remedy for a run parked at an unattended gate (DD11). |
| `conductor_list_runs` | `(status?, workflow?, limit=20)` | Live records ∪ terminal records. `status: "at-gate"` is the query that surfaces parked runs. |

**Diagnostic log tool (DD12).** `conductor_run_logs` returns links, never
bytes:

```jsonc
{
  "content": [
    {"type": "resource_link", "uri": "file:///tmp/conductor/conductor-review-pr-…-a1b2c3d4.bg.stderr.log",
     "name": "stderr", "mimeType": "text/plain", "size": 41233},
    {"type": "resource_link", "uri": "file:///tmp/conductor/conductor-review-pr-…-a1b2c3d4.events.jsonl",
     "name": "events", "mimeType": "application/x-ndjson", "size": 918442}
  ],
  "structuredContent": {
    "run_id": "a1b2c3d4", "status": "failed",
    "error": {"type": "ProviderError", "message": "<from the terminal record>"},
    "logs": [{"stream": "stderr", "path": "…", "size": 41233, "modified_at": 1786100411.2,
              "exists": true}],
    "note": "Contents are not returned over MCP. Read these paths with your own file tools."
  }
}
```

`ResourceLink` is a first-class content type in the pinned SDK (`mcp.types
.ResourceLink`, verified on 1.28.1, carrying `uri` / `name` / `mimeType` /
`size` / `description`), so `size` *is* the bounded metadata rather than a field
we invent. A path that no longer exists — pruned by `[fleet.retention]` or
reaped by the OS — is reported as `exists: false` with the same path, which is a
more useful answer than omitting it.

### Design Decisions

#### DD0 — Bound `mcp` to `<2` now; build the server on the 1.x API

**This is the highest-priority item in the document and is independent of
everything else in it.**

`mcp` 2.0.0 is a final, non-yanked release (uploaded 2026-07-28) requiring
`httpx2>=2.5.0` and `mcp-types==2.0.0`. I installed both majors into isolated
virtualenvs and exercised the exact surface Conductor uses.

**Every import Conductor's existing client performs still succeeds under
2.0.0** — so the source issue's framing ("this can break MCP tool support
today") is right about the risk but wrong about the mechanism, and the
`except ImportError` guard at `mcp/manager.py:39` that sets
`MCP_SDK_AVAILABLE = False` (`:45`) **would not catch it**. The break is at *runtime*
and is caused by a case change:

| Surface | `mcp` 1.28.1 | `mcp` 2.0.0 | Conductor uses it? |
|---|---|---|---|
| `Tool.inputSchema` | present | **`AttributeError`** → `input_schema` | ✅ `mcp/manager.py:207` |
| `CallToolResult.isError` | present | **`AttributeError`** → `is_error` | — |
| `ToolAnnotations.readOnlyHint` | camelCase | snake_case | (server-side) |
| `mcp.server.fastmcp.FastMCP` | present | **`ModuleNotFoundError`** | (server-side) |
| lowlevel `Server` | `@server.list_tools()` decorators | `on_*` constructor params | (server-side) |
| `mcp.client.stdio`, `ClientSession`, `TextContent` | present | present | ✅ |

`mcp/manager.py:207` reads `tool.inputSchema` while building each connected
server's tool list, inside `own_connection_lifecycle`. Under 2.0.0 that raises
for **every** MCP server, on every connection — so a lock refresh alone turns
all MCP tool support into a connection failure, with no code change and no
import error.

Two conclusions follow. First, **add the upper bound as a standalone change**,
ahead of and independent of this feature. Second, there is **no server API
spanning both majors** — `FastMCP` is gone (renamed to `MCPServer`) and the
low-level `Server` swapped its decorators for `on_*` constructor params — so
"write it against the low-level API for forward compatibility" does not work.
v1 targets 1.x, and the 2.x / `2026-07-28` migration is a tracked follow-up
that moves client and server together.

#### DD1 — Reuse the Fleet Manager; add a terminal record, not a parallel index

The source issue proposed a new append-only run index under
`~/.conductor/runs/`. Since #431 that directory already holds a per-run JSON
record, and `fleet/summary.py` already derives everything a status tool needs.
Building a second index beside it would duplicate the store, the derivation,
and the retention policy, and would leave two answers to "what is this run
doing".

The actual defect is narrow and precisely locatable: the record is **deleted**
on process exit, and nothing keyed by `run_id` replaces it. So the change is a
tombstone written in the same `finally` — materially smaller than the issue's
proposal, and it makes `conductor_run_events` / `conductor_node_detail` thin
adapters over `read_event_log_full` and `derive_step_detail` rather than new
parsers.

It also stands alone as a fix: `conductor status` gains completed runs, the
TUI's History screen gains outcome data it currently cannot show (rendered
output, error message), and post-hoc debugging stops depending on a temp
directory the OS may reap.

#### DD2 — Always detached; async by default; the caller chooses per call

Async is the default for four reasons, only one of which is about transport:

1. **Process lifetime.** An MCP stdio server is spawned and owned by the host.
   A workflow executed inside it dies when the host quits, restarts, or reloads
   its MCP config. Conductor's detached child genuinely outlives its launcher.
   This alone settles it.
2. **A blocking call blocks the whole agent session** for the duration.
3. **Cancellation is hostile** on HTTP transports, where a transient
   disconnect is indistinguishable from a cancel.
4. **Parallelism** — one agent can start three workflows and await them all.

⚠️ **Nothing yet bounds how many of these accumulate.** Each invocation forks a
real process with its own dashboard port, token file, event log, and capture
logs; a model retrying in a loop can spawn these without limit, and a gated run
guaranteed by DD11 to never self-terminate makes that worse, not better. A
`--max-concurrent-runs` startup flag — rejecting a launch with an instructive
message once the count of live MCP-launched records is at the cap, the same
posture the design already takes toward bounding things at startup rather than
at runtime — is the natural remedy and pairs with Open Question 1.

But the *sync-or-async* choice depends on what else the caller is doing, which
the workflow author cannot know. So `mcp.mode` in YAML is only a default and
`_wait_seconds` overrides it per call. The critical property is that
`_wait_seconds > 0` does **not** mean "run inline": the run is detached in
every mode, and foreground only changes whether the tool call waits. That makes
foreground free of downside — and it is why a dashboard URL is available
unconditionally (G4), and why every MCP-launched run is gate-resolvable (data
flow D).

⚠️ **A blocking call is bounded, in every mode.** DD11 accepts that a gated run
may sit parked indefinitely, but the *tool call* that is waiting on it must
not: `--max-wait-seconds` (FR5) is a hard ceiling applied to any blocking
invocation, including a `mode: sync` workflow whose `_wait_seconds` was
omitted — that case resolves to the ceiling rather than an unbounded wait. The
call returns as soon as `status: at-gate` is derived, exactly as
`conductor_await_run` already does, so reaching a gate is what ends the wait,
not the ceiling itself; the ceiling exists for the run that is neither gated
nor finished when time runs out.

The honest cost of async is that **models stop polling** — declaring "I started
it!" and never checking back. `conductor_await_run` is the deliberate
mitigation: it collapses N poll round-trips into one call, stays inside safe
transport windows by construction, returns the *next action* in its timeout
response rather than a bare status blob, and emits `notifications/progress`
throughout so the human sees liveness even when the model is idle — though
this last part only holds when the host itself renders `notifications/progress`
and the caller supplied a `progressToken`; a host or caller that omits either
gets no visible liveness signal from this channel. And because the run is
detached regardless, an agent that wanders off has cost nothing.

#### DD3 — Static toolsets, fixed at startup

Toolsets follow the GitHub MCP server's pattern (`--toolsets`, verified in its
`docs/remote-server.md`), chosen because it is the one that survives a
tool-count budget.

⚠️ **Correction to the source issue's evidence, partly upgraded.** The issue
cited Cursor's 40-tool cap and VS Code's 128-tool cap as fact. **VS Code's is
confirmed**: its MCP documentation states "Cannot have more than 128 tools per
request" and offers `github.copilot.chat.virtualTools.threshold` to manage it.
**Cursor's 40 remains unverified** against any primary source and should be
treated as community-observed. What is also primary: Anthropic's
[writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
gives no numeric cap but is explicit that "more tools don't always lead to
better outcomes" and that "too many tools or overlapping tools can also distract
agents", and states "For Claude Code, we restrict tool responses to 25,000
tokens by default." The design therefore treats `--max-direct-tools` as a
**tunable threshold with a conservative default (25)**, not as an encoding of a
specific client's limit, and the startup log states the count so an operator can
tune it against their own host.

⚠️ **A spec constraint the issue did not account for.** MCP `2026-07-28`
requires that a server's tool set "MUST NOT vary per-connection or as a side
effect of other requests on the connection." So the discovery fallback must be
decided **at startup, from the exposed count** — it can never be a runtime
switch, and Zapier-style "enable this tool now" mutation of the live tool list
is off the table, since that is precisely a side effect of another request on
the connection. Fixing the catalogue at startup (component 1) is what makes the
server compliant. The spec does permit a set that "MAY change over time" when
the server announces it via `notifications/tools/list_changed`; that is the
sanctioned channel for a genuine catalogue change, and DD6 deliberately declines
to use it for drift.

#### DD4 — Expose by default, opt out per workflow, via a first-class `mcp:` block

**Decided at stakeholder review: expose by default; opt out per workflow; and
server-level `--allow` / `--deny` override the YAML.**

Opt-in exposure is unworkable on day one: no workflow in any existing registry
carries an `mcp:` block, so an opt-in server would expose **zero** tools, and a
third-party registry would never be callable without forking it. The premise of
the feature is that a registry is already a catalogue of blessed procedures.

```yaml
workflow:
  name: review-pr
  description: Reviews a pull request across correctness, tests, and security.
  mcp:
    expose: true            # default true
    mode: async             # async (default) | sync | auto — a default, not a mandate
    read_only: false
    destructive: true
    estimated_minutes: 8
```

`WorkflowDef` sets `extra="forbid"`, so this must be a real schema field —
it cannot ride on the existing untyped `metadata: dict[str, Any]`. That is the
right outcome anyway: a typed block is validated, appears in `conductor
validate`, and makes a typo an error instead of silence.

**The counter-argument, and how it is answered.** Default-on means a registry
gaining a workflow silently gains a callable tool in every user's agent. That is
real, and the review accepted it against three specific counterweights, all of
which are load-bearing rather than decorative:

1. **`--allow` / `--deny` beat YAML** (component 1's precedence ladder). An
   operator can constrain — or force — a registry they do not own. This is why
   the flags override rather than merely intersect with `mcp.expose`: a
   defensive operator running `--allow 'release-*'` gets an allow-list whose
   membership does not depend on what a remote author later writes in their
   YAML, and a workflow whose author set `expose: false` for their own reasons
   can still be enabled locally by the person who owns the machine.
2. **The startup summary names the exact exposed set** (FR10), on stderr, where
   hosts surface it in their MCP logs. A silently-gained tool is only silent if
   nothing says otherwise.
3. **Pinning reports drift** (DD6). A *changed* workflow is detectable even when
   its name is unchanged, which is the sharper version of the same worry — a
   `review-pr` that quietly grows a `type: script` step is worse than a new tool
   appearing.

`expose: false` remains the right control for a workflow that is genuinely not a
callable unit — a sub-workflow fragment, an internal helper — and is the setting
a registry author should reach for, since it travels with the workflow instead
of living in one operator's shell history.

#### DD5 — Inputs are contractual; outputs are structured but unschematized

`WorkflowConfig.output` is `dict[str, str]` (`config/schema.py:3614`) — Jinja2
templates with no declared types — so an honest `outputSchema` cannot be
published. v1 returns the rendered `output:` dict as `structuredContent` plus a
text fallback, with **no `outputSchema`**. This is spec-legal and is what most
servers do; the cost is that the model learns the result shape by reading it
rather than by contract. (`mcp` 1.28.1 does support `outputSchema` and
validates `structuredContent` against it when declared — the blocker is
Conductor's schema, not the SDK.)

The follow-up is to derive `outputSchema` from the referenced agents'
`OutputField` types, which *are* richly typed, falling back to `string` where
provenance cannot be traced; it shares machinery with
[#230](https://github.com/microsoft/conductor/issues/230). This is the one
place the feature knowingly under-delivers against "it gets the inputs and
outputs".

#### DD6 — Pin every exposed workflow at session start

At startup each exposed workflow is resolved to an immutable identity: the
already-resolved commit SHA for GitHub registries (`materialize_to_sha`), and a
content hash of the YAML for path registries and `--workflow-dir` (which cannot
be SHA-pinned — `version_resolver` raises for a ref on a path registry). That
identity is included in every invocation result, written to the terminal
record, and re-checked on an interval; **drift is logged loudly and the tool
description is not silently updated mid-session**.

Rug-pull — tool definitions changing after a user approved them — is the
documented MCP attack, and the spec offers no primitive against it. It requires
tool lists be stable *within* a connection and tells clients to treat
annotations as untrusted, but says nothing about stability *across* reconnects;
`tools/list`'s `ttlMs` / `cacheScope` in `2026-07-28` are caching hints, not
integrity controls. Conductor is unusually exposed here because a
registry-sourced workflow is remote, user-authored content that can contain
`type: script` shell steps — so we build the pin ourselves.

#### DD7 — Gates surface as an approval URL, not elicitation

MCP elicitation's schema subset maps onto Conductor's gates almost exactly
(`GateOption` → `oneOf: [{const, title}]`; `QuestionDef.choices` +
`allow_free_text` → enum plus a sibling string). It is still the wrong
mechanism to build on now, for a reason stronger than "no host support":

Spec `2026-07-28` **replaced** it. Multi Round-Trip Requests are explicit that
"servers MUST send server-to-client requests (such as `roots/list`,
`sampling/createMessage`, or `elicitation/create`) using the MRTR pattern. The
previous pattern of server-initiated requests is no longer supported. This is a
breaking change." Building v1 on server-initiated elicitation would target a
mechanism that has already been removed.

The approval-URL path, by contrast, is entirely existing machinery: the
dashboard endpoint, the `0600` token file, `resolve_cli_token`, and the
`gate_presented` event that already carries every option's details. It composes
with async naturally — a run that hits a gate simply reports `at-gate` plus a
URL — and it is the same path `conductor gate respond` and the Fleet TUI
already use.

DD7 and DD11 are separate decisions on the same subject and are easy to
conflate. **DD7 is the mechanism** — *how* an approval is collected, given that
the spec's in-band mechanism was withdrawn. **DD11 is the policy** — *whether*
an approval is collected at all when the caller is an agent. DD7 would be
unchanged if elicitation returned tomorrow; DD11 would not.

#### DD8 — Shape run handles so they become MCP Tasks later

MCP Tasks (`io.modelcontextprotocol/tasks`, SEP-2663) is exactly this
submit → handle → poll → terminal-result pattern, standardized. So an
async-first design is not a workaround for missing Tasks support; it is the
shape the spec converged on.

Two verified facts set the v1 posture. Tasks now lives in an **experimental
extension repo** (`modelcontextprotocol/ext-tasks`), self-described as possibly
changing significantly or being discontinued, and it **does not appear in the
official MCP client matrix at all** — so no major host supports it. But
interestingly, **the pinned SDK already has the whole type surface**: `mcp`
1.28.1 exposes `CreateTaskResult`, `tasks/get`, `tasks/list`, `tasks/cancel`,
`tasks/result`, `TaskStatusNotificationParams`, and `ClientTasksCapability`,
and the low-level `call_tool` return union already includes
`types.CreateTaskResult`.

So Tasks is implementable on the dependency we already have, gated purely on
client capability negotiation. v1 does not implement it; it keeps `run_id`
opaque and the status vocabulary aligned so adoption is a mapping, not a
redesign. ⚠️ Note the extension is churning: the SDK exposes `tasks/result`
and `tasks/list` while the current ext-tasks documentation describes
`tasks/update` — another reason not to build on it yet.

#### DD9 — stdio only in v1

stdio is what hosts spawn, has no observed tool-call timeout, and needs no
auth story beyond process ownership. Streamable HTTP additionally requires
OAuth, origin/host validation, and — under `2026-07-28` — the restructured
stateless model with `server/discover` and `subscriptions/listen`. Deferring it
keeps v1 from absorbing the spec migration. `web/auth.py`'s token-file model is
directly reusable when HTTP lands.

#### DD10 — Bare tool names, registry-qualified only on collision

**Decided at stakeholder review: bare names (`review_pr`), qualified with the
registry only when two registries collide.** No `conductor_` prefix on
generated workflow tools.

The apparent conflict this question raised — the source issue wanting bare names
so "the consumer never learns Conductor exists", against Anthropic's guidance
recommending namespacing — **dissolves once you look at where namespacing
actually happens.** Three primary sources settle it:

1. **Hosts already prefix by server.** Claude Code exposes an MCP tool to the
   model as `mcp__<serverName>__<toolName>`; the pattern is visible throughout
   its own permission globs (`mcp__puppeteer__*`), hook matchers
   (`mcp__memory__.*`), and deny-all example (`mcp__*`). A bare `review_pr`
   published by a server the user added as `conductor` is therefore already
   `mcp__conductor__review_pr` in the model's context. Bare is not
   un-namespaced; it is namespaced *by the layer that knows the server's local
   name*. (⚠️ Verified for Claude Code only. VS Code and Cursor document
   per-server *grouping* in their tool pickers but do not document what name
   string they send to the model; neither confirms nor contradicts prefixing.)
2. **Anthropic's guidance says so itself.** The exact sentence is: "Namespacing
   (grouping related tools under common prefixes) can help delineate boundaries
   between lots of tools; **MCP clients sometimes do this by default.**" The
   guidance is about *boundaries existing*, not about who draws them.
3. **The spec assigns cross-server disambiguation to the client.** MCP
   `2026-07-28` says tool-name uniqueness "is scoped to a single server", and
   that clients or proxies aggregating multiple servers "SHOULD implement a
   disambiguation strategy such as prefixing tool names with a server
   identifier". A server that prefixes itself is doing the client's job a second
   time.

So a `conductor_` prefix would buy a boundary that already exists, and it would
cost three things: ~10 characters × N tools of schema context; a tool name that
no longer matches the `workflow.name` shown by `conductor run`, `conductor
show`, the registry index, and the dashboard; and, most importantly, the framing
the feature exists for — a consumer that sees *capabilities* rather than a
Conductor API.

The static lifecycle tools keep their `conductor_` prefix, and that asymmetry is
deliberate rather than an oversight: `conductor_run_status` is genuinely an
operation *on Conductor*, so naming it after Conductor is accurate. `review_pr`
is not an operation on Conductor; it is a capability Conductor happens to
deliver. The prefix marks a real distinction instead of decorating everything
uniformly.

**Collision handling** is unchanged from the API contract above: when two served
registries publish the same slug, *all* colliding tools are qualified
(`official_review_pr`, `team_review_pr`) — never just the loser, because a name
that silently changes meaning when an unrelated registry is added is worse than
one that is consistently qualified. The collision is logged at warning level
naming both registries (FR10), and `--registry` / `--deny` are the deterministic
escape hatches. Collision *with another MCP server's* tool of the same name is
explicitly not our problem to solve, per the spec text above — and the host
prefix means it does not arise in the one host where we can verify the naming.

Because that prefixing is verified for exactly one host, the design keeps a
cheap escape hatch rather than betting on it: **`--tool-prefix <str>`**, a
startup flag that prepends an operator-chosen prefix to every *generated* name
(`acme_review_pr`). It is off by default, so it costs nothing to the common
case, and it means a user on a host that turns out not to prefix — or one who
simply wants the boundary visible in the transcript — can have namespacing
without this decision being reopened. Note the flag prefixes generated workflow
tools only; the static lifecycle tools already carry a meaningful prefix.

#### DD11 — A gate always parks; the server never passes `--skip-gates`

**Decided at stakeholder review: always park at the gate and return the approval
URL; never auto-skip.**

`launch_background()` already accepts `skip_gates: bool = False`
(`cli/bg_runner.py:974`, `:1132`), so auto-skipping would have been a
one-parameter change — which is exactly why the decision needed making
explicitly rather than by default. It is rejected because a human gate is a
control the workflow author deliberately placed in the path, and the caller
being a model rather than a person is the *strongest* argument for keeping it,
not for removing it. Silently auto-selecting the first option would convert an
approval step into a rubber stamp while leaving the workflow's YAML looking
governed.

The consequence, accepted knowingly: **a run can sit at a gate indefinitely** if
nobody is watching. Three things make that visible rather than silent:

- Every status query reports `status: "at-gate"` with the gate's prompt, its
  options, and the dashboard approval URL — `GateInfo` already carries
  `agent_name` / `prompt` / `options` / `option_details` parsed from
  `gate_presented`, which is strictly richer than the live `GET
  /api/gate-status`'s `{waiting, agent_name, prompt_id}`.
- `conductor_await_run` returns on reaching a gate rather than burning its
  budget waiting for one to resolve itself; its timeout text names the approval
  URL as the next action.
- `conductor_list_runs(status="at-gate")` enumerates every parked run, and
  `conductor fleet` shows them in the TUI.

Every MCP-launched run has a dashboard port by construction (DD2), so it is
always gate-resolvable — via the dashboard, `conductor gate respond`, or the
Fleet TUI, all of which already share one endpoint and one token resolver.
`--skip-gates` remains available to a human at the CLI, where the person
choosing it is the person accountable for it.

v1 ships **no park timeout and no reaper** — see Open Question 1.

#### DD12 — Log tools return resource links and bounded metadata, never contents

**Decided at stakeholder review: return only file paths and bounded metadata;
let the host's own file tools fetch contents under existing user consent.**

The source issue asserted that `conductor_run_logs` "must apply the same
redaction the dashboard does". **The dashboard has no redaction** — there is
none anywhere in `web/` or `engine/event_log.py`; the event log is written
verbatim, and `runtime.tool_output` spill files are explicitly documented as
possibly containing secrets. So the choice was really between *building* a
redaction layer, *shipping* an unredacted exfiltration surface, or *not
returning contents at all*.

Returning links is the option that is both safe and honest, and MCP has the
exact vehicle for it: `ResourceLink` is a first-class content type carrying
`uri`, `name`, `mimeType`, `size` and `description` (verified present in the
pinned `mcp` 1.28.1 as `mcp.types.ResourceLink`). `size` and `modified_at` are
the bounded metadata; the terminal record supplies the error type and message,
which is the single most useful *fact* about a failure and is Conductor's own
structured field rather than scraped log text.

The reasoning that makes this more than a dodge: **the host already has a file
tool, and the user already consented to it.** A local stdio server and its host
are on the same machine and usually the same user. Returning a path routes the
read through the consent boundary the user actually configured — the host's own
read/permission rules, its own path allowlists, its own audit — instead of
tunnelling file contents through a channel that has none of that. It also means
Conductor never *becomes* the redaction authority for content it did not
generate.

The costs, stated plainly:

- **A host with no file-reading tool cannot follow the link.** For those, the
  tool degrades to "here is where to look", which is still better than the
  status quo of globbing a temp directory by hand (#116).
- **The links are absolute local paths, so they do not survive the deferred HTTP
  transport** (DD9). When HTTP lands, the same tool must either serve the bytes
  through an authenticated MCP *resource* — at which point redaction becomes
  unavoidable and in scope — or refuse. This design deliberately does not
  pre-commit to which.
- Note this does **not** conflict with NFR3. NFR3 forbids a tool *accepting* a
  path from the model, which is the RCE control. Returning a path the server
  itself derived is the opposite direction and carries none of that risk.

Building redaction is the right long-term answer and is a tracked follow-up; it
is out of v1 because a redaction layer that is trusted but incomplete is worse
than an explicit boundary, and because the run's *structured* data —
status, error, per-step I/O, cost — already answers most diagnostic questions
without raw log text.

#### DD13 — Terminal records are bounded by `[fleet.retention]`, in the same sweep

**Decided at stakeholder review: bound terminal records by the existing
`[fleet.retention].keep_last` setting, pruned in the same sweep, so records and
logs disappear together.**

The alternative was tempting precisely because it is cheap: a terminal record is
a few hundred bytes and an event log is megabytes, so records *could* be kept
far longer, giving `conductor_run_status` a much longer memory. The review
rejected it because a record that outlives its log advertises a run whose detail
is already unfetchable — `conductor_run_status` would answer "completed, here is
the output", `conductor_run_events` would answer "gone", and the two would be
correct and useless at the same time. Keeping them coupled means a `run_id`
either resolves completely or not at all.

Mechanically this is small, because retention already does exactly this shape of
work. `fleet/retention.py` matches a run's `.bg.stderr.log` / `.bg.stdout.log`
companions to its event log by the `run_id` embedded in each filename — not by
filename prefix, because the parent and child write their `ts` segments
independently and can differ by a clock tick — and prunes or keeps all of them
together. The terminal record becomes a fourth companion matched the same
way, by `run_id`. It lives at `run_records_dir()/"terminal"/<run_id>.json` —
a dedicated subdirectory of the run-records directory, not `event_log_root()`
— so the sweep gains one extra directory to look in and no new policy
language, and it inherits the existing `keep_last < 1` guard ("prune nothing",
not "delete everything") unchanged.

The subdirectory is load-bearing, not cosmetic. `read_run_records()` and
`remove_run_record_for_current_process()` both already glob
`run_records_dir().glob("*.json")` non-recursively (`fleet/records.py:1019`,
`:1104`) to find live records. A terminal record filed as a sibling
`<run_id>.done.json` in that same directory would be visible to both: the
first treats it as corrupt (its filename `stem` is `"<run_id>.done"`, which
never equals the record's own `run_id` field, so the identity check fails and
`fleet/records.py:794` deletes it regardless of liveness) and the second can
race it against the live record it is meant to replace, since both carry the
same `pid` and the function returns on the first match it globs. Filing it one
directory down means neither function — on this Conductor or an older one
sharing the same `run_records_dir()` — ever lists it, which is what makes the
"leaves it alone" degradation claimed below actually hold.

Two properties fall out for free. The sweep already runs opportunistically at
the top of every `conductor run` and `conductor resume`
(`cli/run.py:2072`, `:2924`, via `maybe_prune_event_logs()`), and **every
MCP invocation launches exactly such a run** — so an MCP-only user gets the
sweep on the same schedule as a CLI user, without the server needing its own
timer. And `conductor fleet prune` — the explicit, settings-overriding entry
point — covers records with no new verb.

A record whose log was reaped by the OS rather than by us is not a contradiction
of this decision, merely a case it cannot prevent; `conductor_run_logs` reports
`exists: false` for such a path rather than pretending.

---

## Alternatives Considered

**A new run index vs. extending the Fleet Manager (DD1).** A standalone
append-only index (the issue's proposal) would be self-contained and could
carry exactly the fields MCP wants. But it duplicates a store, a derivation
layer, and a retention policy that shipped six weeks ago, and creates two
answers to "what is this run doing". The tombstone approach reuses
`derive_run_summary` / `derive_run_detail` / `derive_step_detail` wholesale and
touches two `finally` blocks. *Chosen: tombstone.*

**One tool per workflow vs. a single `run_workflow(name, inputs)` meta-tool.**
A single meta-tool is immune to tool-count limits and needs no schema
resolution at all — startup becomes trivial. But it discards the entire value
proposition: the calling model gets an untyped `inputs: object` and must
already know the workflow name, so the capability is invisible and the contract
is gone. *Chosen: per-workflow tools, with the meta-tool pair retained as the
automatic above-threshold fallback, which gets the tail case without taxing the
common one.*

**Riding the existing `metadata: dict[str, Any]` vs. a new `mcp:` block
(DD4).** `metadata` is already untyped and already flows into
`workflow_started`, so `metadata.mcp.expose` would need no schema change. But
`extra="forbid"` elsewhere in the schema exists precisely because silent typos
are the failure mode, and an untyped block gets no `conductor validate`
coverage — a misspelled `expse: false` would silently expose a workflow the
author meant to hide. *Chosen: a typed `mcp:` block.*

**Blocking (sync) invocation as the default (DD2).** Simpler for the model —
call it, get the answer — and stdio has no observed tool timeout, so it is
technically survivable. Rejected on process lifetime: the run would die with
the host, which is a 40-minute loss for a config reload. Async-plus-bounded-wait
gets the ergonomics back via `_wait_seconds` and `conductor_await_run` without
that exposure.

**Elicitation-based gates (DD7).** Better UX in principle — the approval
happens in the host, in-band. Rejected because the mechanism was removed in the
current spec revision and no host has confirmed support for the replacement.

**Separate servers for invocation (#432) and introspection (#135).** Different
consumers, so plausibly different products. Rejected: an agent that calls
`review_pr` and gets `status: failed` needs `conductor_run_logs` in the same
breath, from the same connection, without the user having installed a second
MCP server. The toolset mechanism makes the merge cost-free — absorbing #135
adds **zero** tools to the default footprint.

**Opt-in exposure via an explicit `mcp.expose: true` (DD4).** The safer default
posture, and the one a security reviewer reaches for first. Rejected on a
concrete rather than a theoretical ground: no workflow in any existing registry
carries an `mcp:` block, so an opt-in server would publish **zero** tools on the
day it shipped, and a third-party registry could never be made callable without
forking every workflow in it. That is not a conservative default, it is a
non-functional one. *Chosen: expose by default, with `--allow`/`--deny`
overriding YAML so the safe posture is one flag away for anyone who wants it.*

**Namespaced tool names, `conductor_review_pr` (DD10).** Argued for by
Anthropic's tool-writing guidance and genuinely helpful when an agent has dozens
of servers attached. Rejected because the namespace already exists one layer up:
Claude Code presents an MCP tool as `mcp__<serverName>__<toolName>`, the spec
assigns cross-server disambiguation to the client, and Anthropic's own sentence
notes that "MCP clients sometimes do this by default". Self-prefixing would pay
schema context for a boundary already drawn, and would break the property that a
tool's name is the workflow's name everywhere else in the product. *Chosen:
bare, registry-qualified on collision.*

**Auto-skipping gates by passing `--skip-gates` (DD11).** Keeps the calling
agent unblocked and is a one-parameter change, since `launch_background()`
already takes `skip_gates`. Rejected: it converts a control the author
deliberately placed into a rubber stamp, and does so invisibly — the workflow
YAML still reads as gated. The cost of the alternative (a run may park
indefinitely) is real but is *visible*, and visibility is recoverable where a
silent auto-approval is not. *Chosen: always park; surface the approval URL;
`conductor_cancel_run` as the remedy.*

**Building a redaction layer for log contents (DD12).** The most capable option
— an agent could read its own failure logs in-band, which is the fastest
possible debugging loop. Rejected for v1 on the grounds that Conductor has *no*
redaction anywhere to extend, that the content in question (tool results, spill
files, provider errors) is arbitrary third-party text with no schema to redact
against, and that a redaction layer trusted but incomplete is more dangerous
than an explicit boundary. **Omitting `conductor_run_logs` entirely** was the
other alternative and was rejected as strictly worse than links: the bg capture
log is the only artefact that explains a child that died before emitting any
event, so removing it would leave #116's failure mode unaddressed. *Chosen:
`resource_link`s plus bounded metadata; redaction is a tracked follow-up that
becomes mandatory when HTTP transport lands.*

**Keeping terminal records longer than event logs, or under their own retention
knob (DD13).** Attractive because the record is tiny and the log is not, so a
`run_id` could stay answerable for months at negligible cost. Rejected because
the two would then disagree: `conductor_run_status` would report a completed run
whose `conductor_run_events` and `conductor_node_detail` return nothing, and a
caller has no way to predict which half survived. A second retention setting
would also give the same directory two policies. *Chosen: one `keep_last`, one
sweep, one lifetime — a `run_id` resolves completely or not at all.*

---

## Dependencies

**External.**

- `mcp` (Python SDK), **bounded to `>=1.28.1,<2`** — see DD0. Server built on
  the 1.x low-level `Server` API or `mcp.server.fastmcp.FastMCP`, both of which
  exist in 1.28.1 and neither of which survives into 2.x unchanged.
- No new runtime dependencies. `httpx`, `pydantic`, `typer`, and `rich` are
  already present. Note `mcp` 2.x would introduce `httpx2` and
  `opentelemetry-api` — another reason the bound is not merely hygiene.

**Internal.**

- `registry/` — `config`, `index`, `cache`, `resolver`, `version_resolver`,
  `github`. This design extends `WorkflowInfo` with two optional fields and
  adds a parse-cache entry inside the existing `_meta/<sha[:12]>/` layout.
- `cli/bg_runner.py` — `launch_background()` used as-is.
- `fleet/` — `records`, `summary`, `history`, `retention`. Extended with a
  terminal record; `retention.py`'s sweep gains the record as a fourth
  `run_id`-matched companion (DD13); the query layer is consumed unchanged.
- `cli/run.py` — the two `finally` blocks that currently remove the live run
  record also write the terminal record.
- `config/schema.py` — a new `McpConfig` on `WorkflowDef`.
- `config/validator.py`, `cli/validate.py` — validate and report the new block.
- `providers/diagnostics.py`, `cli/gate.py`, `web/auth.py` — consumed unchanged.

**Sequencing.**

1. **`mcp` upper bound** — one line, independent, protects the existing client.
   Do first (DD0).
2. **Terminal run record** — prerequisite for the async lifecycle (P3);
   independently testable through `conductor status`.
3. **`WorkflowInfo` extension + SHA-keyed parse cache** — prerequisite for
   NFR1/G9; independently useful to `conductor registry list`.
4. The server, toolsets, and result/gate shaping build on 1–3.

Neither piece of groundwork has an independent consumer today, which is why
both are sequenced here rather than split into separate designs.

---

## Impact Analysis

**Areas touched.** New `mcp/serve/` package (the existing `mcp/manager.py` is
the *client* and is untouched by the server work, though DD0's bound protects
it). Modified: `config/schema.py`, `config/validator.py`, `cli/validate.py`,
`cli/app.py` (a new `mcp` sub-app, following the `registry` / `plugin` /
`checkpoint` / `gate` grouping convention), `cli/run.py` (terminal record),
`fleet/records.py` (terminal record read/write), `fleet/retention.py` (prune the
terminal record alongside its event log, DD13), `registry/index.py` and
`registry/cache.py` (optional index fields, parse cache), `pyproject.toml`.

**Backward compatibility.**

- The `WorkflowInfo` extension is additive and optional; existing
  `index.yaml` files keep working with no change, taking tier 2 or 3 of the
  resolution ladder.
- The `mcp:` block is optional with `expose` defaulting to true, so no existing
  workflow needs editing (DD4).
- The terminal record is a **new file**, written to a dedicated
  `run_records_dir()/"terminal"/` subdirectory rather than beside
  `<run_id>.json`. This is load-bearing, not cosmetic: `read_run_records()`
  and `remove_run_record_for_current_process()` both glob `*.json`
  non-recursively directly under `run_records_dir()` (`fleet/records.py:1019`,
  `:1104`). A same-directory sibling would be visible to both — deleted by the
  first (its filename `stem` fails the `run_id` identity check, so it is
  pruned as corrupt regardless of liveness) and able to race the second into
  removing the tombstone instead of the live record it is meant to replace,
  since both carry the same `pid`. Filed one directory down, neither function
  — on this Conductor or an older one sharing the same `run_records_dir()` —
  ever lists it, so `read_run_records()`'s liveness filter and pruning, and
  `remove_run_record_for_current_process()`'s own-record removal, are
  genuinely unchanged; `conductor stop`, `conductor status`, and `fleet list`
  keep their exact current semantics for live runs (NFR5).
- The retention sweep gains a subdirectory to also scan but no new setting
  (DD13), so a user's existing `[fleet.retention]` block keeps its exact
  meaning. An older Conductor sharing the same machine never lists
  `run_records_dir()/"terminal"/` at all — its own `*.json` glob is
  non-recursive over the run-records directory only — so it leaves every
  terminal record untouched. That is the correct degradation, and unlike a
  same-directory suffix it holds regardless of filename convention.
- ⚠️ The `mcp` upper bound is a **behavioural change for anyone whose lock
  already floated to 2.x**. Given `mcp/manager.py:207` breaks under 2.0.0, such
  an installation already has non-functional MCP tools; the bound restores
  them.

**Performance.** Startup is the sensitive path (hosts respawn stdio servers
aggressively). Warm cache: local reads only, no network (NFR1). Cold cache: one
index fetch plus one fetch per unresolved workflow, under a deadline, with
degraded-schema fallback. Per-invocation cost is one process fork plus the
existing three-stage health gate — the same cost `conductor run --web-bg`
already pays. `conductor_run_status` on a live run is a bounded tail read of
the event log, the same operation the Fleet TUI performs on a ~2s poll.

**Operational.** A new failure surface that is *invisible by default*: stdio
servers have no console, so a misconfigured server appears to the user as "the
tools aren't there". The startup summary (FR10) must therefore go to stderr,
which hosts surface in their MCP logs, and must state the exposed count, the
mode (direct vs discovery), and every collision it resolved. `conductor doctor`
gains an MCP section as the out-of-band way to inspect what a server *would*
expose without attaching a host.

---

## Security Considerations

The threat model is unusually sharp: **Conductor workflows contain
`type: script` shell steps and can be fetched from git registries.** An MCP
server that runs user-authored workflows is a remote-code-execution surface
wearing a tool schema.

- **No tool accepts a path, URL, or registry source** (NFR3). `--workflow-dir`
  and `--registry` are startup arguments the user typed; the distinction
  between a startup argument and a model-supplied parameter is the entire
  control. A `run_workflow(path)` tool must never exist.
- **Serving all registries by default is defensible only because a registry is
  something the user explicitly ran `conductor registry add` for.** The server
  never auto-discovers a registry, and never reads ambient MCP or plugin
  configuration. Within that set, exposure defaults to on (DD4), and the
  operator's counterweight is `--deny` / `--allow`, which beat the workflow's
  own YAML in both directions.
- **Tool descriptions are attack surface.** `workflow.description` is remote,
  user-controlled content that flows into text the host model reads and acts
  on — the canonical MCP tool-poisoning vector. Sanitize (strip control
  characters and instruction-shaped markers) and hard length-cap before it
  reaches a schema (NFR4).
- **Pin and re-check** (DD6). The spec has no primitive for this; note also
  that it instructs clients to treat `annotations` as untrusted, so
  `readOnlyHint` / `destructiveHint` are consent-prompt hints, never controls.
- **Human gates are preserved as a control, not bypassed** (DD11). The server
  never passes `skip_gates=True`, so a workflow author's approval step still
  gates an agent-initiated run exactly as it gates a human-initiated one. The
  accepted cost is an indefinitely parked run, which is a liveness problem, not
  a security one.
- **Log contents are never returned over the protocol** (DD12). ⚠️ The source
  issue stated `conductor_run_logs` "must apply the same redaction the dashboard
  does". **The dashboard has no redaction** — there is none anywhere in `web/`
  or `engine/event_log.py`; the event log is written verbatim, and
  `runtime.tool_output` spill files are explicitly documented as possibly
  containing secrets. Rather than ship an unredacted exfiltration channel or
  build a redaction layer we cannot yet make complete, the tool returns
  `ResourceLink`s and bounded metadata, routing the actual read through the
  host's own file-access consent. This is a *boundary*, not a redaction: a host
  whose file tool is unrestricted will read the same bytes — but it will do so
  under a permission the user granted for exactly that purpose, and with that
  host's audit trail. Note the residual: the same unredacted content still
  reaches an agent through `introspect`'s structured surfaces (Open Question 2).
- **Path containment for any file the server returns** must follow
  `/api/files/{path}`'s existing posture (reject absolute, drive-qualified,
  UNC and scheme-prefixed paths; resolve strictly; bound size).
- **Token handling.** stdio inherits the parent's environment; the server must
  not echo `CONDUCTOR_GATE_TOKEN` or a dashboard token into any tool result.
  The approval URL it returns is a plain dashboard URL, and the token stays in
  the `0600` file where `resolve_cli_token` finds it.
- ⚠️ **No CVEs are cited.** The source issue referenced MCP tool-poisoning /
  rug-pull CVE identifiers; I could not verify any against NVD or GitHub
  Security Advisories. The attack *classes* are documented in the spec's own
  security guidance; the identifiers should not be repeated in Conductor's docs.

---

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| A lock refresh pulls `mcp` 2.x and silently breaks all existing MCP tool support | **High** | **High** | DD0 — add `<2` as a standalone change, ahead of this feature; add a smoke test that asserts `Tool.inputSchema` access still works |
| Model starts a run and never polls, so work completes unnoticed | High | Medium | `conductor_await_run` collapses polling into one call; `notifications/progress` gives the *human* liveness **only when the host renders it and the caller supplied a `progressToken`** — no host's primary docs confirm rendering it; run is detached so nothing is lost |
| Cold-cache startup is slow or network-dependent, and hosts respawn aggressively | Medium | High | Three-tier schema ladder; SHA-keyed immutable cache; startup deadline with degraded-schema fallback (NFR2) |
| A large registry blows past an unknown host tool cap | Medium | Medium | `--max-direct-tools` with a conservative default and automatic discovery fallback; loud startup log. VS Code's 128-per-request cap is documented; Cursor's 40 is community-observed only, so the threshold is tunable rather than hard-coded |
| Malicious or accidental prompt injection via `workflow.description` | Medium | High | Sanitize + length-cap (NFR4); pin and report drift (DD6); `--allow`/`--deny` beats YAML (DD4) |
| A registry silently gains a workflow, and every user's agent silently gains a callable tool | Medium | Medium | Accepted with DD4; countered by `--deny`/`--allow` overriding YAML, the stderr startup summary naming the exposed set (FR10), and drift reporting (DD6) |
| `introspect` returns unredacted secrets that DD12 kept out of `conductor_run_logs` | Medium | High | Open Question 2 — `derive_step_detail` returns rendered prompts and structured outputs; working assumption is field-selected, size-bounded structured records with no raw log text, but the posture needs a decision |
| A run parks at a gate indefinitely because no human is watching | **High** | Medium | Accepted with DD11 (auto-skip is worse); `status: at-gate` on every query, `conductor_list_runs(status="at-gate")`, an approval URL in the timeout text, and `conductor_cancel_run` as the remedy. No reaper in v1 — Open Question 1 |
| A workflow fails to parse for environmental reasons (`${VAR}`, `!file`) and its tool vanishes | Medium | Medium | Expose with permissive schema and an explanatory description; never drop silently (NFR2) |
| Terminal records accumulate unbounded in `~/.conductor/runs/` | Medium | Low | DD13 — pruned by the existing `[fleet.retention]` sweep as a fourth `run_id`-matched companion of the event log; inherits the `keep_last < 1` guard |
| A `run_id` resolves to a status but its detail is already pruned | Low | Low | DD13 couples record and log lifetime, so a `run_id` resolves completely or not at all; an OS-reaped path reports `exists: false` rather than pretending |
| Spec `2026-07-28` / SDK 2.x migration is larger than expected and blocks HTTP transport | Medium | Medium | v1 is stdio-only and 1.x-only; migration is a tracked follow-up moving client and server together |
| MCP Tasks lands with a shape our `run_id` handles do not map onto | Low | Medium | Status vocabulary already aligns 1:1; extension is experimental and unadopted, so waiting is the low-risk option |

---

## Open Questions

The five questions this document previously carried — default exposure posture,
tool naming, log exposure, gate behaviour, and terminal-record retention — were
**answered at stakeholder review** and are recorded as DD4 and DD10–DD13. Two
questions remain, both raised *by* those answers rather than left over from
before them. Each states the working assumption the design currently encodes, so
a reader knows what was assumed rather than confirmed.

1. **Does a parked gate need a timeout, or a reaper?** DD11 settles that a gate
   always parks and is never auto-skipped, and the review accepted that a run
   may therefore sit indefinitely. It did not settle what happens to a run that
   is *never* answered: today it holds a detached process, a bound port, a
   `0600` dashboard token file, and a live run record forever, and it is
   invisible unless someone calls `conductor_list_runs` or opens the Fleet TUI.

   *Working assumption encoded in this design:* no timeout and no reaper in v1.
   Visibility (`status: at-gate` on every query, the approval URL in every
   timeout response, `conductor_list_runs(status="at-gate")`) plus an explicit
   `conductor_cancel_run` are the remedy, on the reasoning that a
   Conductor-imposed deadline that *fails* a run someone was about to approve is
   its own kind of silent discard. The alternatives are a configurable
   max-park duration that terminates the run (checkpointed, so it is resumable),
   or a warning-only threshold that escalates through
   `notifications/progress` without terminating anything.

2. **Does DD12's paths-only posture extend to the `introspect` toolset?** DD12
   keeps raw log *text* out of tool results, but `introspect` is a different
   shape and was not covered by the review's question. `derive_step_detail`
   returns a step's **rendered prompt** and **structured output** verbatim
   (`fleet/summary.py:991`), and `conductor_run_events` returns event records
   that include tool arguments and results. Those are exactly the fields most
   likely to carry a credential, so the toolset that was reduced to links has a
   sibling that still returns the sensitive content — in a tidier wrapper.

   *Working assumption encoded in this design:* `introspect` returns
   **structured, field-selected, size-bounded** records rather than raw log
   text, and is **off by default** (DD3), so enabling it is an explicit operator
   act. The justification is that these are typed events Conductor generated and
   the Fleet TUI already renders, not opaque third-party bytes — but that
   justification is about *shape*, not about *sensitivity*, and the two are not
   the same argument. The alternatives are to apply DD12 uniformly (return an
   event-log link and nothing else, which would gut the toolset's purpose), to
   redact a named field list (prompt, tool arguments, tool results) while
   returning the rest, or to accept the exposure as scoped by `introspect`
   being opt-in.

---

## References

**Conductor**

- Source issue: [microsoft/conductor#432](https://github.com/microsoft/conductor/issues/432)
- Absorbed: [#135](https://github.com/microsoft/conductor/issues/135) (introspection MCP server)
- Related: [#230](https://github.com/microsoft/conductor/issues/230) (publish workflow JSON Schema), [#392](https://github.com/microsoft/conductor/issues/392) (`type: mcp` step), [#116](https://github.com/microsoft/conductor/issues/116) (crash debugging), [#404](https://github.com/microsoft/conductor/issues/404) / [#410](https://github.com/microsoft/conductor/issues/410) (bg run id, confirmed start)
- Fleet Manager design: `docs/projects/fleet-manager/fleet-manager.design.md`; user guide `docs/fleet.md`
- Existing MCP *client* docs: `docs/mcp-tools.md`

**MCP specification** (revision `2026-07-28`, verified current)

- [Specification `2026-07-28`](https://modelcontextprotocol.io/specification/2026-07-28) — stateless base protocol, `resultType` on all results
- [Server → Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools) — tool-name rules (1–128 chars, `A-Za-z0-9_-.`, unique within a server), the "MUST NOT vary per-connection" stability requirement, and the note assigning cross-server collision disambiguation to **clients and proxies** (DD3, DD10)
- [`server/discover`](https://modelcontextprotocol.io/specification/2026-07-28/server/discover)
- [Multi Round-Trip Requests](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr) — replaces server-initiated requests (breaking)
- [Subscriptions](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/subscriptions) — `subscriptions/listen` replaces the HTTP GET stream
- [Tasks extension overview](https://modelcontextprotocol.io/extensions/tasks/overview) and [`modelcontextprotocol/ext-tasks`](https://github.com/modelcontextprotocol/ext-tasks) — experimental (SEP-2663; cited in the `ext-tasks` README and the `2026-07-28` changelog). ⚠️ A previously-cited SEP-1686 could not be verified against any primary source and has been dropped.
- [Client matrix](https://modelcontextprotocol.io/extensions/client-matrix) — Tasks absent

**Python SDK**

- [`modelcontextprotocol/python-sdk` releases](https://github.com/modelcontextprotocol/python-sdk/releases) and [migration guide](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/migration.md)
- [`mcp` 2.0.0 on PyPI](https://pypi.org/project/mcp/2.0.0/) — final release, 2026-07-28, requires `httpx2` + `mcp-types`
- `mcp.types.ResourceLink` — verified present in the pinned 1.28.1 with `uri` / `name` / `mimeType` / `size` / `description` (DD12)

**Host behaviour** (for DD3 and DD10)

- [Claude Code — MCP](https://code.claude.com/docs/en/mcp), [permissions](https://code.claude.com/docs/en/permissions), [hooks](https://code.claude.com/docs/en/hooks) — MCP tools are exposed as `mcp__<serverName>__<toolName>`, visible in permission globs (`mcp__puppeteer__*`), hook matchers (`mcp__memory__.*`), and the deny-all example (`mcp__*`)
- [VS Code — Use tools with agents](https://code.visualstudio.com/docs/agents/run/tools) — FAQ: "Cannot have more than 128 tools per request" / "A chat request can have a maximum of 128 tools enabled at a time", with `github.copilot.chat.virtualTools.threshold` as the mitigation. ⚠️ Does **not** document whether tool names are prefixed by server before reaching the model
- [Cursor — MCP](https://cursor.com/docs/mcp) — ⚠️ likewise documents per-server grouping but not name prefixing; the 40-tool cap the source issue cited remains **unverified** against a primary source

**Prior art and guidance**

- [Anthropic — Writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents) — "Namespacing … can help delineate boundaries between lots of tools; **MCP clients sometimes do this by default**"; "For Claude Code, we restrict tool responses to 25,000 tokens by default"
- [GitHub MCP server — toolsets](https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md) — `--toolsets` / `GITHUB_TOOLSETS` / `X-MCP-Toolsets`
- [n8n MCP Server Trigger](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-langchain.mcptrigger.md) — workflows as tools over SSE/HTTP
- [Windmill #7129](https://github.com/windmill-labs/windmill/issues/7129) — per-flow MCP granularity request
- [Trail of Bits — `mcp-context-protector`](https://github.com/trailofbits/mcp-context-protector)
- [Microsoft — MCP security best practices](https://github.com/microsoft/mcp-for-beginners/blob/main/02-Security/mcp-security-best-practices-2025.md)
