# Implementation Plan: `conductor mcp serve` — workflows as MCP tools

> **Source design (authoritative):**
> [`docs/projects/mcp-server/conductor-mcp.design.md`](./conductor-mcp.design.md)
> — *Solution Design: `conductor mcp serve` — workflows as MCP tools*.
> Source issue: [microsoft/conductor#432](https://github.com/microsoft/conductor/issues/432)
> (absorbs [#135](https://github.com/microsoft/conductor/issues/135)).
>
> **Revision notes:** Initial draft.
>
> This plan consumes an already-reviewed design. It does **not** re-derive or
> re-litigate design decisions; each epic names the design section or decision
> it delivers (e.g. *Key Components → 1. Catalogue builder*, DD2, FR5). Genuine
> gaps that blocked confident planning are surfaced in **Open Questions**
> rather than silently resolved.
>
> **Grounding.** Every file path, symbol, and line reference below was checked
> against the working tree at `b6c5b11` (the commit that added the design), and
> the pinned `mcp` 1.28.1 SDK surface was exercised in this repo's own `.venv`
> rather than taken from the design's report. Where verification produced a
> fact the design did not have, it is called out inline as **Verified**.

---

## Open Questions

**None.** All four questions this plan raised were put to a stakeholder and
answered; they are recorded as decisions R1–R4 below and propagated through the
phases, file lists, and epics. Drafting raised nothing further that needs a
human decision — every remaining uncertainty was recoverable by reading the
code, and where reading the code contradicted an assumption, the correction is
recorded inline rather than escalated.

### Resolved decisions

These are plan-level decisions, not design decisions. They resolve gaps and
internal inconsistencies in the source design that blocked confident planning;
they do not revisit anything the design settled (DD0–DD13).

---

**R1 — The terminal run record reaches `conductor status`, `fleet list`, and
the TUI's History screen in v1.** *(affects E2, E4)*

The design was internally inconsistent here: DD1 justifies the tombstone partly
by saying it "stands alone as a fix: `conductor status` gains completed runs,
the TUI's History screen gains outcome data it currently cannot show (rendered
output, error message)", while *Impact Analysis → Areas touched* lists only
`cli/run.py`, `fleet/records.py` and `fleet/retention.py`, and FR12 covers only
retention.

**Resolved: full reach in v1.** `conductor status`, `conductor fleet list`, and
the History screen all surface completed runs, and the resulting contract change
and test churn are accepted. Consequences propagated:

- `conductor status` and `fleet list` stop meaning "runs alive right now". Both
  gain a live section and a completed section, and both gain a `--live` flag
  that restores the pre-change scope for scripts that depend on it.
- The `conductor status --json` payload keeps its existing `running` array
  unchanged and **adds** a sibling `completed` array, so an existing machine
  consumer reading `["running"]` is not broken by the change (E4-T2).
- `fleet/history.py`'s `HistoryEntry` gains the two fields the design named —
  rendered `output:` and error type/message — sourced from the terminal record
  by `run_id`. History keeps enumerating **event logs**, not records (its module
  docstring's reason still holds: a completed run's *live* record is already
  gone). The terminal record is an enrichment joined onto that enumeration, not
  a replacement for it.
- This makes E4 a first-class epic rather than a follow-up, and it is what turns
  E2 from an MCP-only prerequisite into the standalone fix DD1 claims it is.

---

**R2 — A floating registry ref resolves offline through a
`_meta/_refs/<ref-slug>.json` pointer in `registry/cache.py`.** *(affects E5)*

NFR1 ("zero network I/O" to first `tools/list` on a warm cache) and DD6 (every
exposed workflow pinned to an immutable identity) were **unsatisfiable together**
against the current code. Verified: `registry/cache.py` keys everything by SHA
(`_meta/<sha[:12]>/`), and the only way to learn what a floating ref (`latest`,
a branch, a tag) currently resolves to is `version_resolver.materialize_to_sha`
→ `github.resolve_ref_to_sha`, an API call. No ref→SHA pointer exists anywhere
in `registry/`.

**Resolved: add the pointer, mirroring `plugins/fetch.py`.** Conductor already
solved exactly this problem once — `plugins/fetch.py` writes `_refs/<slug>.json`
recording what a floating ref last resolved to, precisely so the offline
fallback has a checkout to choose (`AGENTS.md`, *Git-backed plugin sources*:
"without a record of what a floating ref last meant, the offline fallback has no
checkout to choose"). The registry cache gains the same shape, written on every
successful online resolution and read when a caller declares the network
off-limits. Startup is then genuinely zero-network on a warm cache, and DD6's
interval drift re-check is what refreshes the pointer.

---

**R3 — `--max-concurrent-runs` ships in v1, defaulting to `0` (unbounded).**
*(affects E9)*

DD2 carries a ⚠️ that nothing bounds how many detached runs accumulate — each
invocation forks a real process with its own dashboard port, `0600` token file,
event log and two capture logs; a model retrying in a loop can spawn them
without limit; and DD11's never-self-terminating gated run makes it worse. It
names `--max-concurrent-runs` as "the natural remedy", but no functional
requirement lists the flag.

**Resolved: implement it, default `0` = unbounded.** Behaviour is unchanged
unless an operator opts in, so the flag adds a control without imposing a policy
the design did not choose. Over the cap, a launch is **rejected with an
instructive message** rather than queued — matching the design's stated posture
of bounding things at startup rather than introducing runtime scheduling the
server does not otherwise have.

---

**R4 — `introspect` returns prompt and output in full; tool payloads are
reduced to `{name, status, byte_size}` unless `--introspect-full` is passed.**
*(affects E11)*

The design's own Open Question 2 assumed "structured, field-selected,
size-bounded" records without naming the fields, and that selection is the whole
task.

**Resolved: prompt and output in full — they are the toolset's purpose — with
tool arguments and results reduced by default and an explicit
`--introspect-full` opt-in restoring them.**

⚠️ **Correction, verified against the code, to where this decision bites.** The
plan's original framing said `derive_step_detail`'s activity stream carries tool
arguments and results. It does not: `fleet/summary.py:1066` and `:1068` build
`ActivityLine("tool", …)` / `ActivityLine("tool_result", …)` from
`data.get("tool_name")` **only**, discarding `arguments` and `result` entirely.
The payloads live in the **raw event records** — `agent_tool_start` carries
`arguments` and `agent_tool_complete` carries `result`
(`providers/copilot.py:2066`, `:2079`) — which is what `read_event_log_full`
returns. So the decision applies as follows:

- **`conductor_run_events`** is where the reduction is actually needed, and is
  where `--introspect-full` gates it (E11-T2).
- **`conductor_node_detail`** already satisfies the decision by construction.
  E11-T2 asserts that rather than assuming it, so a future change to
  `ActivityLine` cannot silently reopen the exposure.

---

## Implementation Phases

Seven phases. The ordering is the design's own *Dependencies → Sequencing*
(bound the SDK, then the terminal record, then the index/cache groundwork, then
the server), expanded so that each phase has an exit criterion checkable without
the next phase existing, and split so that R1's now-in-scope CLI surfacing work
is its own step rather than riding inside the record epic.

### Phase 0 — Dependency safety (E1)

Independent of the feature and urgent on its own (DD0). Ships alone and can
merge before anything else in this plan is written.

**Exit criteria**
- [ ] `pyproject.toml` declares `mcp>=1.28.1,<2`; `uv.lock` re-resolved and
      still pinning `1.28.1`.
- [ ] A test fails if `mcp.types.Tool.inputSchema` (the camelCase attribute
      `mcp/manager.py:207` reads) is not accessible, and if
      `mcp.server.fastmcp` / `mcp.server.lowlevel.Server` cannot be imported.
- [ ] `make check && make test` green.

### Phase 1 — Terminal run record and its consumers (E2, E3, E4)

The design's *Key Components → 4* plus DD13, and — per R1 — the CLI and TUI
surfaces that make it the standalone fix DD1 claims. Nothing here imports the
MCP SDK, and the whole phase is user-visible value without a single MCP tool
existing.

**Exit criteria**
- [ ] A completed run is resolvable by `run_id` after its process has exited,
      returning terminal status, rendered `output:`, error type/message, and
      token/cost totals (P3, G5).
- [ ] `[fleet.retention]`'s existing sweep deletes a run's terminal record in
      lockstep with its event log, and cannot delete a live run's (FR12, DD13).
- [ ] `conductor status`, `conductor fleet list`, and the TUI History screen all
      show completed runs, with `--live` restoring the previous scope and the
      `status --json` `running` array unchanged (R1).
- [ ] History shows a failed run's error message and a completed run's rendered
      output — data it cannot show today (R1, DD1).

### Phase 2 — Catalogue groundwork (E5, E6)

The two prerequisites the design sequences ahead of the server that are not
about run state: an offline-answerable registry, and a typed `mcp:` block.

**Exit criteria**
- [ ] A warm registry cache answers "what is this workflow's `input:` and
      `mcp:` block, at what SHA" with **zero** network calls, proven by a test
      that patches `registry/github.py` to raise on any call (NFR1, G9, R2).
- [ ] A floating ref resolves offline through the `_meta/_refs/` pointer, and
      a cold cache still resolves online exactly as today (R2).
- [ ] `workflow.mcp:` parses, validates, and is reported by `conductor
      validate`; an unknown key inside it is an error, not silence (FR11, DD4).
- [ ] A registry index may carry optional `input:` / `mcp:` per workflow, and
      an index without them still loads byte-identically.

### Phase 3 — Catalogue (E7)

Everything between configuration and a frozen list of `mcp.types.Tool` objects.
No protocol, no process launching — pure functions over the registry.

**Exit criteria**
- [ ] `build_catalogue(...)` returns an immutable catalogue from a fixture
      registry in under 2s with the network patched to raise (NFR1).
- [ ] The four-rung exposure ladder (`--deny` > `--allow` > `mcp.expose` >
      default-on) is exercised in every ordering that distinguishes the rungs
      (FR2, DD4).
- [ ] Two registries publishing the same slug yield *both* tools qualified,
      never just the loser (DD10).
- [ ] A workflow whose YAML cannot be parsed (missing `${VAR}`, unresolvable
      `!file ../x.md`) is still present, with a permissive schema and an
      explanatory description (NFR2).
- [ ] A workflow declaring an input named `_wait_seconds` is rejected from the
      catalogue with a logged reason, not exposed (Tool generator ⚠️).

### Phase 4 — Protocol and invocation (E8, E9)

The first phase with an observable end-to-end result.

**Exit criteria**
- [ ] `conductor mcp serve` speaks stdio JSON-RPC, answers `initialize` and
      `tools/list`, and writes **nothing** to stdout that is not protocol.
- [ ] The startup summary — exposed count, mode, per-tool registry and pin,
      every collision qualified — appears on stderr (FR10).
- [ ] Invoking a generated tool launches a detached run via
      `launch_background()` and returns a handle carrying `run_id`, dashboard
      `url`, pinned identity and status (FR4, G4).
- [ ] `_wait_seconds: 0`, `_wait_seconds: 120`, and omission all resolve per
      FR5, with `--max-wait-seconds` as a hard ceiling that a `mode: sync`
      workflow also obeys.
- [ ] `--max-concurrent-runs` defaults to `0` and changes nothing until an
      operator sets it; over the cap a launch is rejected, not queued (R3).

### Phase 5 — Lifecycle and diagnosis (E10, E11)

**Exit criteria**
- [ ] `conductor_run_status` answers for a live run, a run parked at a gate
      (with prompt, options and approval URL), and a run whose process exited
      (FR6, FR7).
- [ ] `conductor_cancel_run` reuses `cli/app.py::stop_records` and reports
      honestly when a run was already terminal.
- [ ] `conductor_run_logs` returns `ResourceLink` content blocks and bounded
      metadata and **never** file bytes; a pruned path reports
      `exists: false` (DD12, FR8, NFR6).
- [ ] `conductor_run_events` reduces tool arguments and results to
      `{name, status, byte_size}` unless `--introspect-full` is set; prompt and
      output come back in full (R4).
- [ ] `conductor_doctor` and `conductor_validate_workflow` return structured
      reports over the existing `providers/diagnostics.gather()` and
      `cli/validate.validate_workflow`.

### Phase 6 — Scale and release (E12, E13, E14)

**Exit criteria**
- [ ] A registry above `--max-direct-tools` serves the two-tool discovery pair
      instead of per-workflow tools, decided at startup and logged (FR9, DD3).
- [ ] `conductor doctor` reports what a server *would* expose without a host
      attached (Impact Analysis → Operational).
- [ ] `docs/mcp-server.md` exists, `docs/mcp-tools.md` disambiguates client
      from server, `AGENTS.md` documents the new package and CLI group.
- [ ] `make check && make test` green; `make validate-examples` green.

---

## Files Affected

### New Files

| File Path | Purpose |
|-----------|---------|
| `src/conductor/mcp/serve/__init__.py` | Package marker. Re-exports nothing eagerly — `cli/app.py` imports `cli/mcp.py` at every `conductor` invocation, so the MCP SDK must not be imported unless `mcp serve` actually runs. |
| `src/conductor/mcp/serve/options.py` | Frozen `ServeOptions` dataclass holding every startup argument (`registries`, `workflow_dirs`, `allow`, `deny`, `toolsets`, `max_direct_tools`, `max_wait_seconds`, `tool_prefix`, `max_concurrent_runs`, `introspect_full`). The single artifact that makes NFR3's "startup argument, never a tool parameter" boundary checkable. |
| `src/conductor/mcp/serve/naming.py` | Slugify a `workflow.name` to the spec's `A-Za-z0-9_-.` set (folding `-`→`_`, 1–128 chars), apply `--tool-prefix`, detect collisions and qualify *all* colliding tools with their registry, and keep the reverse `tool name → (registry, workflow)` map (DD10). |
| `src/conductor/mcp/serve/sanitize.py` | Strip control characters and instruction-shaped markers from YAML-authored description text and hard length-cap it before it reaches a tool schema (NFR4). |
| `src/conductor/mcp/serve/toolgen.py` | `InputDef` → JSON Schema property; assemble `mcp.types.Tool` with `inputSchema`, the reserved `_wait_seconds` parameter, and `annotations` from the `mcp:` block (FR3, FR5, DD5). |
| `src/conductor/mcp/serve/pinning.py` | Resolve an exposed workflow to an immutable identity — commit SHA for GitHub registries, content hash for path registries and `--workflow-dir` — and re-check it on an interval, reporting drift without mutating the live catalogue (DD6). |
| `src/conductor/mcp/serve/catalogue.py` | `build_catalogue(...)` — the design's *Key Components → 1*: enumerate registries, apply the exposure ladder, resolve schemas through the three-tier ladder, pin, sanitize, qualify collisions, and decide direct-tools vs discovery. Returns an immutable `Catalogue`. |
| `src/conductor/mcp/serve/invoke.py` | Validate typed inputs, enforce `--max-concurrent-runs` (R3), call `cli/bg_runner.py::launch_background()`, shape the run handle, and run the bounded `_wait_seconds` poll with `notifications/progress` (FR4, FR5, DD2). |
| `src/conductor/mcp/serve/runs.py` | `conductor_run_status` / `conductor_await_run` / `conductor_cancel_run` / `conductor_list_runs` over `fleet/records` + `fleet/summary` + the terminal record (FR6, FR7). |
| `src/conductor/mcp/serve/introspect.py` | `conductor_run_events` / `conductor_node_detail` / `conductor_plan_tree` — thin adapters over `read_event_log_full`, `derive_step_detail`, and `WorkflowConfig`, with R4's tool-payload reduction applied to the event query. |
| `src/conductor/mcp/serve/diagnose.py` | `conductor_doctor` / `conductor_validate_workflow` / `conductor_run_logs`; the last returns `ResourceLink`s and bounded metadata only (FR8, DD12). |
| `src/conductor/mcp/serve/discovery.py` | `conductor_find_workflow` / `conductor_run_workflow`, the above-threshold replacement for per-workflow tools (FR9). |
| `src/conductor/mcp/serve/server.py` | Wires the frozen catalogue and enabled toolsets onto the pinned SDK's low-level `Server`, runs it over `mcp.server.stdio.stdio_server`, and emits the stderr startup summary (FR1, FR10, DD3, DD9). |
| `src/conductor/cli/mcp.py` | The `mcp` Typer sub-app with a single `serve` command, following the `checkpoint` / `gate` / `registry` sub-app pattern (`no_args_is_help=True`). |
| `docs/mcp-server.md` | User guide for the server: host configuration snippets, the exposure ladder, toolsets, the `mcp:` block, and the diagnostic tools. |
| `examples/mcp-serve.yaml` | A workflow carrying a populated `mcp:` block, so `make validate-examples` covers the new schema block. |
| `tests/test_mcp/conftest.py` | Fixtures: a fixture path registry with an `index.yaml`, a fake GitHub registry whose network layer raises, and a warm-cache builder. |
| `tests/test_mcp/test_sdk_bound.py` | DD0 regression: `Tool.inputSchema` and `CallToolResult.isError` are accessible, `mcp.server.lowlevel.Server` and `mcp.server.fastmcp` import, and the declared bound excludes 2.x. |
| `tests/test_mcp/test_serve_naming.py` | Slugification, 1–128-char and charset enforcement, `--tool-prefix`, and collision qualification of *all* colliding tools. |
| `tests/test_mcp/test_serve_toolgen.py` | `InputDef` → JSON Schema for all five types, `required` / `default` / `description` preservation, `_wait_seconds` injection, and rejection of a workflow that declares `_wait_seconds` itself. |
| `tests/test_mcp/test_serve_catalogue.py` | Exposure ladder, three-tier schema ladder, NFR1 zero-network assertion, NFR2 degraded-schema fallback, and the direct-vs-discovery decision. |
| `tests/test_mcp/test_serve_pinning.py` | SHA pin for a GitHub registry, content hash for a path registry, and drift reported without the live catalogue changing. |
| `tests/test_mcp/test_serve_invoke.py` | Handle shape, always-detached launch, `_wait_seconds` resolution, `--max-wait-seconds` ceiling, gate-reached short-circuit, and the `--max-concurrent-runs` rejection path (R3). |
| `tests/test_mcp/test_serve_runs.py` | Status for live / at-gate / terminal / crashed runs; cancel; list with `status` and `workflow` filters. |
| `tests/test_mcp/test_serve_introspect.py` | Event query, node detail, plan tree, size bounding, and R4's reduction plus its `--introspect-full` restoration. |
| `tests/test_mcp/test_serve_diagnose.py` | `conductor_run_logs` returns links not bytes; `exists: false` for a pruned path; doctor and validate adapters. |
| `tests/test_mcp/test_serve_discovery.py` | The pair replaces per-workflow tools above the cap, is fixed at startup, and never accepts a path (NFR3). |
| `tests/test_mcp/test_serve_server.py` | `initialize` / `tools/list` / `tools/call` over an in-memory stream pair; the tool list is byte-identical across two connections (DD3); stdout carries only protocol. |
| `tests/test_fleet/test_terminal_records.py` | Terminal record round-trip, subdirectory invisibility to `read_run_records` / `scan_run_records` / `remove_run_record_for_current_process`, and never-raises contract. |
| `tests/test_config/test_mcp_block.py` | `McpConfig` defaults, `extra="forbid"` rejection of a typo, and `mode` / `estimated_minutes` bounds. |
| `tests/test_cli/test_mcp_serve.py` | `conductor mcp serve --help`, argument parsing, startup summary on stderr, stdout purity, and the sub-app's help panel. |

### Modified Files

| File Path | Changes |
|-----------|---------|
| `pyproject.toml` | `mcp>=1.28.1` → `mcp>=1.28.1,<2` (DD0, NFR7). |
| `uv.lock` | Re-resolved under the new bound (`uv lock`). |
| `CHANGELOG.md` | Unreleased entries for the SDK bound, the terminal record, the completed-run surfacing (R1, a user-facing contract change), the `mcp:` block, and `conductor mcp serve`. |
| `src/conductor/fleet/records.py` | Add `TerminalRunRecord`, `terminal_records_dir()` (= `run_records_dir()/"terminal"`), `write_terminal_record` / `read_terminal_record` / `read_terminal_records` / `remove_terminal_record`. No change to the nine-field `RunRecord` or to any existing glob. |
| `src/conductor/cli/run.py` | Write the terminal record in the two `finally` blocks (`:2274`, `:3018`) that already call `_remove_run_record_for_current_process_safe()`; capture outcome, rendered output, error and usage totals for it. Add an `mcp serve` exclusion to the update-hint gate at `:391`. |
| `src/conductor/fleet/retention.py` | Treat the terminal record as a fourth `run_id`-matched companion of the event log in `_companion_paths` / `_prune_event_logs_impl`, plus a bounded orphan sweep for records whose log is already gone (DD13). |
| `src/conductor/cli/app.py` | **R1:** `status` gains a completed-runs section from `read_terminal_records()`, a `--live` flag restoring the old scope, and a sibling `completed` array in `--json` (the existing `running` array is untouched). Plus `app.add_typer(mcp_app, rich_help_panel="Environment")` alongside the existing four sub-apps at `:60–64`. |
| `src/conductor/cli/fleet.py` | **R1:** `fleet list` gains completed rows with their real terminal status (`completed` / `failed` rather than the current hard-coded `"running"` at `:124`), bounded by `[fleet.retention].keep_last`, and a `--live` flag. |
| `src/conductor/fleet/history.py` | **R1:** `HistoryEntry` gains `output`, `error_type`, `error_message`, enriched from the terminal record by `run_id` *after* the single-pass log scan, so issue #436's forward-only constraint on `_scan_history_events` is untouched. |
| `src/conductor/fleet/tui/screens/history.py` | **R1:** surface the failure reason and rendered output the enriched `HistoryEntry` now carries — the outcome data the design says History "currently cannot show". |
| `src/conductor/fleet/launch.py` | Add `build_typed_launch_inputs(values, input_defs)` — the required-input/default half of `build_launch_inputs` without the string coercion, since MCP values arrive already JSON-typed (*Key Components → 3*). |
| `src/conductor/config/schema.py` | New `McpConfig` (`expose`, `mode`, `read_only`, `destructive`, `estimated_minutes`) with `extra="forbid"`, and `WorkflowDef.mcp: McpConfig` (DD4). |
| `src/conductor/config/validator.py` | Cross-check the `mcp:` block: `estimated_minutes` bounds, an input named `_wait_seconds`, and a workflow name that cannot be slugified to a legal tool name. |
| `src/conductor/cli/validate.py` | `_report_mcp(...)`, modelled on the existing `_report_plugins` / `_report_skill_discovery`, printing the block and the tool name the workflow would publish (FR11). |
| `src/conductor/registry/index.py` | `WorkflowInfo` gains optional `input: dict[str, InputDef] \| None` and `mcp: McpConfig \| None`. Both default to `None`; `WorkflowInfo` has no `extra="forbid"`, so old and new indexes stay mutually loadable. |
| `src/conductor/registry/cache.py` | **R2:** a `_meta/_refs/<ref-slug>.json` ref→SHA pointer mirroring `plugins/fetch.py`, plus a SHA-keyed tool-definition parse cache under the existing `_meta/<sha[:12]>/`, reusing `CACHE_LAYOUT_VERSION` for invalidation and the `.complete` sentinel convention, and an `allow_network` seam. |
| `src/conductor/providers/diagnostics.py` | `McpServeDiagnostic` + `gather_mcp_serve()`, following the existing `RegistryDiagnostic` / `to_dict()` shape. |
| `src/conductor/cli/doctor.py` | Render the MCP section (exposed count, mode, collisions, unresolved schemas). |
| `docs/mcp-tools.md` | Disambiguate: this page is the MCP *client*; link to `docs/mcp-server.md` for the server. |
| `docs/cli-reference.md` | `conductor mcp serve` section and its flags; **R1:** update the `conductor status` and `conductor fleet list` sections, which currently document them as listing running workflows only, and document `--live`. |
| `docs/workflow-syntax.md` | The `mcp:` block. |
| `docs/fleet.md` | Terminal records: what they hold, where they live, that `[fleet.retention]` prunes them with their event log, and (R1) that History now shows outcome detail. |
| `docs/configuration.md` | Note that `[fleet.retention].keep_last` now bounds terminal records too. |
| `AGENTS.md` | Document `mcp/serve/`, `cli/mcp.py`, the terminal record, the completed-run scope change to `status` / `fleet list` (R1), and the `mcp:` block. |
| `tests/test_cli/test_status.py` | **R1:** existing assertions rewritten for the new two-section output; add completed-run cases, `--live`, and a regression asserting the `--json` `running` array is unchanged. |
| `tests/test_cli/test_fleet_list.py` | **R1:** completed rows with real terminal status, `--live`, and the empty case for both sections. |
| `tests/test_fleet/test_history.py` | **R1:** enrichment from the terminal record by `run_id`; a log with no matching record still yields a usable entry; enrichment never raises. |
| `tests/test_fleet/test_tui_history.py` | **R1:** the failure reason and rendered output render on the History screen. |
| `tests/test_cli/test_help_panels.py` | Add `mcp` to the noun-group list and the panel mapping. |
| `tests/test_cli/test_doctor.py` | Cover the new MCP section. |
| `tests/test_fleet/test_retention.py` | Terminal record pruned with its log, kept when the log is live, orphan sweep bounded. |
| `tests/test_fleet/test_run_record_wiring.py` | A terminal record is written on clean exit, on `WorkflowTerminated`, and on an unexpected exception; none is written for a `kill -9`. |
| `tests/test_registry/test_index.py` | Optional `input` / `mcp` round-trip; an index without them is unchanged. |
| `tests/test_registry/test_cache.py` | **R2:** ref pointer write/read and slug safety; parse cache hit/miss; `CACHE_LAYOUT_VERSION` bump invalidates. |
| `tests/test_cli/test_validate.py` | `mcp:` block reporting and its error cases. |

### Deleted Files

| File Path | Reason |
|-----------|--------|
| *(none)* | This design is purely additive. The existing MCP **client** (`src/conductor/mcp/manager.py`) is untouched — DD0's bound protects it, but no code in it changes. |

---

## Implementation Plan

Fourteen epics. Each names the design section or decision it delivers — and,
where a stakeholder answer settled a gap, the resolution (R1–R4) it implements.
Each is scoped to keep its file count at or below seven wherever the underlying
change allows.

---

### E1 — Bound the `mcp` SDK dependency (DD0)

**Status: DONE** (completed 2026-08-22)

**Goal.** Close the live hazard the design calls "the highest-priority item in
the document and independent of everything else in it": a lock refresh pulling
`mcp` 2.x silently breaks the *existing* MCP client, because
`mcp/manager.py:207` reads the camelCase `tool.inputSchema` that 2.0.0 renamed
to `input_schema` — a runtime `AttributeError` on every server connection that
the `except ImportError` guard at `mcp/manager.py:39` cannot catch.

**Prerequisites.** None. Ships alone, ahead of every other epic here.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E1-T1 | IMPL | Change `mcp>=1.28.1` to `mcp>=1.28.1,<2` (NFR7). Comment the bound in place with the reason, matching the existing commented bounds in this file (`claude-agent-sdk`, `textual`, `regex` all carry one). | `pyproject.toml` | DONE |
| E1-T2 | IMPL | Re-resolve with `uv lock` and confirm `mcp` still resolves to `1.28.1` and that no transitive `httpx2` / `mcp-types` / `opentelemetry-api` entry appears. | `uv.lock` | DONE |
| E1-T3 | TEST | Smoke test asserting the surface the bound protects: `mcp.types.Tool` exposes `inputSchema` (not `input_schema`), `mcp.types.CallToolResult` exposes `isError`, and both `mcp.server.fastmcp` and `mcp.server.lowlevel.Server` import. Assert against the *attribute*, not the version string, so the test fails for the reason that actually matters. | `tests/test_mcp/test_sdk_bound.py` | DONE |
| E1-T4 | TEST | Parse the declared bound out of `pyproject.toml` and assert it excludes `2.0.0`, so a future widening of the specifier fails here rather than in a user's lockfile. | `tests/test_mcp/test_sdk_bound.py` | DONE |
| E1-T5 | IMPL | Changelog entry under Unreleased, stating that an installation whose lock already floated to 2.x had non-functional MCP tools and that this restores them (*Impact Analysis → Backward compatibility*, ⚠️ row). | `CHANGELOG.md` | DONE |

**Acceptance criteria**
- [x] `mcp` is bounded `>=1.28.1,<2` in `pyproject.toml` and the lock still pins `1.28.1`.
- [x] `tests/test_mcp/test_sdk_bound.py` fails if `Tool.inputSchema` stops resolving.
- [x] `make check && make test` green.
- [x] The change is mergeable on its own, with no dependency on any other epic.

---

### E2 — The terminal run record (DD1, P3, G5) — DONE

**Status: DONE** (completed 2026-08-22)

**Goal.** Make a completed run resolvable by `run_id` after its process has
exited — the design's "one genuinely new artifact" and the hard prerequisite
for the async invocation model. Per **R1**, this epic delivers the record and
its read/write API; E4 delivers the surfaces that consume it.

**Prerequisites.** None (independent of E1).

**Grounding.** Verified: `cli/run.py` calls
`_remove_run_record_for_current_process_safe()` unconditionally in both
`finally` blocks (`:2274` in `run_workflow_async`, `:3018` in
`resume_workflow_async`), and `fleet/records.py::_read_and_prune` deletes the
records of dead processes, so nothing keyed by `run_id` survives a run today.
The design's *Why a subdirectory, not a sibling file* analysis is confirmed
against the code: `read_run_records()` (`:1019`) and
`remove_run_record_for_current_process()` (`:1104`) both glob
`run_records_dir().glob("*.json")` non-recursively, and `scan_run_records()`
(`:971`) does too — a third caller the design does not name, with the same
`record.run_id != f.stem` identity check, so the subdirectory placement is what
protects all three.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E2-T1 | IMPL | Define `TerminalRunRecord` (frozen dataclass, `to_dict`/`from_dict` mirroring `RunRecord`'s tolerant coercion) carrying the identifying fields plus `status`, `ended_at`, `output` (the rendered `output:` dict), `error_type`, `error_message`, `total_tokens`, `total_cost_usd`, `unpriced_agent_count`, `event_log_path`, `bg_stderr_log`, `bg_stdout_log` (*Key Components → 4*). `from_dict` must tolerate every field being absent so a record written by a newer Conductor still parses. | `src/conductor/fleet/records.py` | DONE |
| E2-T2 | IMPL | `terminal_records_dir()` returning `run_records_dir() / "terminal"`, plus `write_terminal_record` (temp file + `_replace_with_retry`, reusing the existing atomic-write helper), `read_terminal_record(run_id)`, `read_terminal_records()`, and `remove_terminal_record(run_id)`. Reuse `_validate_run_id` / `_RUN_ID_PATTERN` so a non-path-safe id can never produce a file. `read_terminal_records()` sorts newest-first by `ended_at` and takes a `limit`, since E4 renders it per invocation. | `src/conductor/fleet/records.py` | DONE |
| E2-T3 | IMPL | Capture the terminal outcome in `run_workflow_async` so the `finally` has something to write: today `result` is a `try`-local and the exception is not retained. Add locals for status / error / output set on the success, `WorkflowTerminated`, and `except BaseException` paths, then write the record in the `finally` immediately before `_remove_run_record_for_current_process_safe()`. Wrap in a never-raises guard modelled on that function (`cli/run.py:1892`) — a diagnostic write must not break the dashboard/event-log cleanup that follows. | `src/conductor/cli/run.py` | DONE |
| E2-T4 | IMPL | Mirror E2-T3 in `resume_workflow_async` (`:3018`), per the Run/Resume Parity rule in `AGENTS.md`. A resumed run reuses its predecessor's `run_id`, so the write **replaces** the earlier terminal record rather than creating a second one — assert it rather than assuming it. | `src/conductor/cli/run.py` | DONE |
| E2-T5 | IMPL | Populate token/cost totals from `engine.get_execution_summary()["usage"]` (`engine/workflow.py:7188`), which already exposes `total_tokens`, `total_cost_usd` and `unpriced_agent_count`. Read it unconditionally rather than only when `cost.show_summary` is set — the existing call site is gated on that flag, and a terminal record that silently omits cost for half of all runs is worse than one that omits it never. | `src/conductor/cli/run.py` | DONE |
| E2-T6 | TEST | Round-trip; a record written under `terminal/` is invisible to `read_run_records()`, `scan_run_records()` **and** `remove_run_record_for_current_process()` (all three glob non-recursively — assert each, since the design only names two); a corrupt terminal record is skipped, not raised; `write_terminal_record` never raises on a read-only directory; `read_terminal_records()` honours its limit and newest-first order. | `tests/test_fleet/test_terminal_records.py` | DONE |
| E2-T7 | TEST | Wiring: a terminal record appears after a clean run, after `WorkflowTerminated`, and after an unexpected exception, with the right `status` in each case, the rendered `output:` on success, and `error_type`/`error_message` on failure. A resumed run replaces rather than duplicates. Assert explicitly that a `kill -9`-style exit leaves **no** terminal record — this is the ⚠️ limitation in *Key Components → 4* and must be a documented, tested boundary rather than a surprise. | `tests/test_fleet/test_run_record_wiring.py` | DONE |

**Acceptance criteria**
- [x] `read_terminal_record(run_id)` returns status, rendered output, error, and usage totals for a run whose process has exited.
- [x] `conductor stop`'s semantics for **live** runs are unchanged (NFR5) — its tests pass untouched.
- [x] A crashed run produces no terminal record, and a test says so.
- [x] The write path never raises.

---

### E3 — Terminal-record retention (DD13, FR12) — DONE

**Goal.** Bound terminal records by the existing `[fleet.retention].keep_last`,
pruned in the same sweep as the event log they point at, so "a `run_id`
resolves completely or not at all".

**Prerequisites.** E2.

**Grounding.** Verified: `fleet/retention.py::_companion_paths` already matches
a run's `.bg.stderr.log` / `.bg.stdout.log` to its event log by the `run_id`
embedded in each filename (`_RUN_ID_FROM_EVENT_LOG`), and
`_prune_event_logs_impl` keeps the `keep_last < 1` "prune nothing" guard and
the fail-closed `_live_event_log_paths()` check. The terminal record becomes a
fourth companion matched the same way — but it lives under
`run_records_dir()/"terminal"/`, not `event_log_root()`, so the sweep needs one
extra directory rather than a wider glob.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E3-T1 | IMPL | Extend `_companion_paths` (or add a sibling resolver, so the events-log-directory glob stays as-is) to also yield `terminal_records_dir()/<run_id>.json` for the `run_id` extracted from the event log's filename. Deleted and retained sets both follow the events log, unchanged in policy language. | `src/conductor/fleet/retention.py` | DONE |
| E3-T2 | IMPL | Add a bounded orphan sweep for terminal records whose event log is already gone (pruned earlier, or reaped by the OS). Without it the directory still grows without limit for exactly the runs whose logs disappeared first, which is the accumulation risk the design's own risk table names. Bound it by the same `keep_last`, newest-first by `ended_at`, and inherit the `keep_last < 1` guard. | `src/conductor/fleet/retention.py` | DONE |
| E3-T3 | IMPL | Never delete a terminal record for a run that still has a **live** run record (a resumed run reuses its `run_id`, so the terminal record of its previous leg is about to be replaced). Source liveness from the same `_live_event_log_paths()` call rather than a second `read_run_records()`. | `src/conductor/fleet/retention.py` | DONE |
| E3-T4 | TEST | A terminal record is deleted with its event log and kept with it; a live run's record survives regardless of age; `keep_last=0`/negative prunes nothing; the orphan sweep bounds records whose log is already absent; a read-only directory produces `failed` entries rather than an exception. | `tests/test_fleet/test_retention.py` | DONE |
| E3-T5 | IMPL | Document terminal records in the fleet guide (what they hold, where they live, that `[fleet.retention]` prunes them with the event log, and that a crashed run has none) and note in the configuration reference that `keep_last` now bounds them too. | `docs/fleet.md`, `docs/configuration.md` | DONE |

**Acceptance criteria**
- [x] Record and log are deleted in the same sweep, never split.
- [x] `conductor fleet prune` covers terminal records with no new verb or setting.
- [x] The opportunistic startup sweep (`cli/run.py`, via `maybe_prune_event_logs()`) still never raises.

---

### E4 — Surface completed runs in `status`, `fleet list`, and History (R1, DD1) — DONE

**Goal.** Deliver the standalone value DD1 claims for the terminal record: a
finished run stops being invisible. This is a **user-facing contract change** —
`conductor status` and `conductor fleet list` currently mean "runs alive right
now" — accepted at stakeholder review (R1) together with its test churn.

**Prerequisites.** E2. (E3 is independent; retention bounds what E4 displays but
E4 does not depend on it.)

**Grounding.** Verified: `conductor status` (`cli/app.py:1374`) reads
`scan_run_records()` deliberately rather than `read_run_records()`, because the
read-only command must not prune — the completed-run source must preserve that
property. `fleet list` (`cli/fleet.py:82`) hard-codes the string `"running"` in
its Status column (`:124`) with a comment explaining that every record it sees
is live by construction; that comment stops being true here. `fleet/history.py`
enumerates event logs rather than records, for the reason its docstring gives
(a completed run's *live* record is already gone) — so the terminal record is an
**enrichment joined onto** that enumeration by `run_id`, not a replacement for
it, and the join must happen after `_scan_history_events` so issue #436's
single-forward-pass constraint is untouched.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E4-T1 | IMPL | `conductor status`: render a second section listing recently-completed runs from `read_terminal_records(limit=…)`, with terminal status, ended-at, duration, tokens/cost, and error type for failures. Keep the live section sourced from `scan_run_records()` and keep it non-destructive — this command's whole reason for existing is that it never prunes. Add `--live` to restore the previous scope exactly. | `src/conductor/cli/app.py` | DONE |
| E4-T2 | IMPL | `conductor status --json`: **keep the existing `running` array byte-compatible** and add a sibling `completed` array, so a machine consumer reading `payload["running"]` is unaffected by the contract change. `--live` emits `running` only. | `src/conductor/cli/app.py` | DONE |
| E4-T3 | IMPL | `fleet list`: add completed rows and replace the hard-coded `"running"` Status cell (`cli/fleet.py:124`) with the row's real status — live rows keep the coarse `running` (deriving the richer vocabulary needs a per-row event-log read, which that comment correctly rules out for this table), completed rows carry their terminal `completed` / `failed`. Bound the completed set by `[fleet.retention].keep_last`. Add `--live`. Update the stale comment rather than leaving it contradicting the code. | `src/conductor/cli/fleet.py` | DONE |
| E4-T4 | IMPL | `HistoryEntry` gains `output`, `error_type` and `error_message`, enriched from `read_terminal_record(entry.run_id)` after the log scan completes. An entry whose `run_id` is `None` (an unrecognized filename) or whose record is absent (a pre-upgrade or crashed run) keeps working with the fields `None` — enrichment must never turn a displayable row into a dropped one, and `build_history_entries`'s never-raises contract is inherited. | `src/conductor/fleet/history.py` | DONE |
| E4-T5 | IMPL | History screen: surface the failure reason for a failed run and the rendered output for a completed one — the outcome data the design says the screen "currently cannot show". Keep the existing five columns intact and put the new detail where a row selection can reach it, so the table stays readable at the existing width. | `src/conductor/fleet/tui/screens/history.py` | DONE |
| E4-T6 | TEST | `status`: existing assertions updated for the two-section output; a completed run appears with its terminal status and error; `--live` reproduces the old output exactly; the `--json` `running` array is unchanged and `completed` is additive; the command still prunes nothing (assert the record files survive the call). | `tests/test_cli/test_status.py` | DONE |
| E4-T7 | TEST | `fleet list`: completed rows show real terminal status; live rows still show `running`; `--live` restores the old scope; both-empty and one-empty cases render without error; the completed set honours `keep_last`. | `tests/test_cli/test_fleet_list.py` | DONE |
| E4-T8 | TEST | History: an entry with a matching terminal record carries output and error; one without keeps working with `None`s; a corrupt terminal record does not drop the row or raise; the TUI screen renders the failure reason. | `tests/test_fleet/test_history.py`, `tests/test_fleet/test_tui_history.py` | DONE |
| E4-T9 | IMPL | Update the `conductor status` and `conductor fleet list` sections of the CLI reference, which currently describe them as listing running workflows only, and document `--live`. Note the change in the changelog as a contract change, not a feature bullet. | `docs/cli-reference.md`, `CHANGELOG.md` | DONE |

**Acceptance criteria**
- [x] A run that finished five minutes ago is visible in `conductor status`, `conductor fleet list`, and History.
- [x] History shows a failed run's error message and a completed run's rendered output.
- [x] `--live` reproduces the pre-change output for both commands.
- [x] `conductor status` still prunes nothing.
- [x] The `status --json` `running` array is unchanged.

---

### E5 — Registry index fields, offline ref pointer, and parse cache (P4, NFR1, G9, R2) — DONE (completed 2026-08-22)

**Goal.** Make the catalogue answerable from a warm cache with zero network
I/O — the three-tier schema ladder's first two tiers (*Key Components → 1*) —
and close the NFR1/DD6 conflict R2 resolves.

**Prerequisites.** E6 (for `McpConfig`, which E5-T1 imports); otherwise
independent of E1–E4.

**Grounding.** Verified: `WorkflowInfo` is exactly `{description, path}`
(`registry/index.py:26`) with no `extra="forbid"`, so added optional fields are
backward- and forward-compatible in both directions. `registry/cache.py`
already establishes `_meta/<sha[:12]>/` for per-SHA metadata "outside the
SHA-rooted mirror", the `.complete` readiness sentinel written last, and
`CACHE_LAYOUT_VERSION` as the invalidation lever. **What does not exist** is any
ref→SHA pointer: `version_resolver.materialize_to_sha` always calls the GitHub
API, so a floating ref cannot be resolved offline — the gap R2 closes.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E5-T1 | IMPL | Add optional `input: dict[str, InputDef] \| None = None` and `mcp: McpConfig \| None = None` to `WorkflowInfo`. Import both from `config/schema.py`; verify no import cycle (`registry/` does not currently import `config/`, so check this explicitly and use a `TYPE_CHECKING` guard plus a string annotation if one appears). Absent fields must leave every existing `index.yaml` loading identically. | `src/conductor/registry/index.py` | DONE |
| E5-T2 | IMPL | SHA-keyed parse cache: store a normalized, already-resolved tool definition (name, description, input schema, `mcp:` block) under the existing `_meta/<sha[:12]>/` directory, guarded by the same `.complete`-style sentinel written last and invalidated by `CACHE_LAYOUT_VERSION`. A SHA-keyed entry is immutable, which is what makes reuse safe. | `src/conductor/registry/cache.py` | DONE |
| E5-T3 | IMPL | **R2:** a `_meta/_refs/<ref-slug>.json` pointer recording what a floating ref (`latest`, branch, tag) last resolved to, written on every successful online resolution and read when the caller declares network access is not permitted. Modelled directly on `plugins/fetch.py`'s `_refs/<slug>.json`, which exists for exactly this reason. Slugify the ref so a `/`-bearing branch name cannot escape the directory, and reuse the atomic temp-file-plus-rename convention the cache already uses so a concurrent reader never sees a half-written pointer. | `src/conductor/registry/cache.py` | DONE |
| E5-T4 | IMPL | An `allow_network: bool` seam on the cache read path so a caller can demand cache-only resolution and get a typed failure rather than a silent HTTP call — the same posture `plugins/resolution.py` takes between `conductor run` and `conductor validate`. With `allow_network=False`, a floating ref resolves through the E5-T3 pointer, and a cold pointer is a typed error naming the fetch path. | `src/conductor/registry/cache.py` | DONE |
| E5-T5 | TEST | Index round-trip with and without the new fields; an old index loads unchanged; a new index loads on a build that ignores the fields. Ref pointer write/read, slug safety, and atomicity. Parse-cache hit avoids re-parse; `CACHE_LAYOUT_VERSION` bump invalidates. **The load-bearing test:** with every function in `registry/github.py` patched to raise, a warm cache still resolves a GitHub registry's workflows to schemas and SHAs (NFR1, G9, R2). | `tests/test_registry/test_index.py`, `tests/test_registry/test_cache.py` | DONE |

**Acceptance criteria**
- [x] A warm cache answers "input schema + `mcp:` block + pinned SHA" for every workflow with the network patched to raise, including for a floating ref.
- [x] Existing registries and indexes work unchanged.
- [x] A cold cache still resolves online exactly as today, and writes the pointer as a side effect.

**Implementation note.** E5-T1 imports `McpConfig` from `config/schema.py`
per its own prerequisite on E6. Rather than block on the full E6 epic, a
minimal `McpConfig` model (the `E6-T1` field set: `expose`, `mode`,
`read_only`, `destructive`, `estimated_minutes`, `extra="forbid"`) was added
to `config/schema.py` as a shared prerequisite so E5-T1 has a concrete type
to import. **E6 remains otherwise unimplemented**: `WorkflowDef.mcp` is not
wired up, there are no validator cross-checks (`_wait_seconds` collision,
unslugifiable name), no `_report_mcp` CLI reporting, and no docs/examples —
all of E6-T2 through E6-T6 are still `TO DO` and belong to a dedicated E6
implementation pass.

---

### E6 — The `mcp:` workflow block (DD4, FR11)

**Goal.** A typed, validated `workflow.mcp:` block, so a typo is an error
rather than silence — the reason the design rejected riding the untyped
`metadata` dict.

**Prerequisites.** None. Lands before or with E5, which imports `McpConfig`.

**Grounding.** Verified: `WorkflowDef` sets `model_config =
ConfigDict(extra="forbid")` (`config/schema.py:3532`), so this cannot be added
by convention — it must be a real field. `cli/validate.py` already has the
reporting pattern to copy (`_report_plugins`, `_report_skill_discovery`).

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E6-T1 | IMPL | `McpConfig` with `extra="forbid"`: `expose: bool = True`, `mode: Literal["async","sync","auto"] = "async"`, `read_only: bool = False`, `destructive: bool = False`, `estimated_minutes: int \| None = None` (positive when present). Add `WorkflowDef.mcp: McpConfig = Field(default_factory=McpConfig)` so an absent block behaves identically to a default one and no existing workflow needs editing. | `src/conductor/config/schema.py` | TO DO |
| E6-T2 | IMPL | Cross-checks in the workflow validator: an input named `_wait_seconds` collides with the reserved parameter (Tool generator ⚠️) and is an error; a `workflow.name` that cannot slugify to a legal 1–128-character tool name is an error naming the rule. Both must fire regardless of whether the workflow declares an `mcp:` block, since default-on exposure (DD4) means every workflow is a candidate. | `src/conductor/config/validator.py` | TO DO |
| E6-T3 | IMPL | `_report_mcp(...)` in the validate CLI, printing the effective block and the tool name the workflow would publish — modelled on `_report_plugins` (`cli/validate.py:77`). Making the generated name inspectable without attaching a host is the point (FR11). | `src/conductor/cli/validate.py` | TO DO |
| E6-T4 | TEST | Defaults; `extra="forbid"` rejects `expse: false`; `mode` rejects an unknown value; `estimated_minutes` rejects zero and negatives; an absent block equals a default block. | `tests/test_config/test_mcp_block.py` | TO DO |
| E6-T5 | TEST | `conductor validate` reports the block, errors on a `_wait_seconds` input, and errors on an unslugifiable workflow name. | `tests/test_cli/test_validate.py` | TO DO |
| E6-T6 | IMPL | Document the block in the workflow-syntax reference, and add `examples/mcp-serve.yaml` carrying a populated one so `make validate-examples` exercises it. | `docs/workflow-syntax.md`, `examples/mcp-serve.yaml` | TO DO |

**Acceptance criteria**
- [ ] `workflow.mcp:` parses, validates, and appears in `conductor validate` output.
- [ ] A misspelled key is a validation error.
- [ ] Every existing example and workflow still validates.

---

### E7 — Catalogue builder: exposure, schema ladder, naming, pinning (FR2, FR3, NFR1, NFR2, NFR4, DD4, DD6, DD10)

**Goal.** The design's *Key Components → 1 and 2*: turn configuration into a
frozen, immutable list of tool definitions at startup — no protocol, no
process launching.

**Prerequisites.** E5, E6.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E7-T1 | IMPL | `ServeOptions` — a frozen dataclass holding every startup argument, including `max_concurrent_runs` (R3) and `introspect_full` (R4). Existing as a single artifact is what makes NFR3 checkable: any value not on it did not come from the operator. | `src/conductor/mcp/serve/options.py` | TO DO |
| E7-T2 | IMPL | Naming: slugify to the spec's `A-Za-z0-9_-.` set, fold `-`→`_`, enforce 1–128 characters and within-server uniqueness, apply `--tool-prefix`, and on collision qualify **all** colliding tools with their registry (never only the loser). Keep the reverse `tool name → (registry, workflow)` map the invocation layer needs (DD10). | `src/conductor/mcp/serve/naming.py` | TO DO |
| E7-T3 | IMPL | Sanitize YAML-authored description text: strip control characters and instruction-shaped markers, hard length-cap, and do it before the text reaches a tool schema (NFR4, *Security → Tool descriptions are attack surface*). | `src/conductor/mcp/serve/sanitize.py` | TO DO |
| E7-T4 | IMPL | Tool generation: `InputDef` → JSON Schema property for all five types with `required` / `default` / `description` preserved; inject the reserved `_wait_seconds`; map the `mcp:` block onto `annotations`. Publish **no** `outputSchema` (DD5). Document the `enum`/`integer`/`items` fidelity gap in the module docstring rather than inventing structure the author did not declare. | `src/conductor/mcp/serve/toolgen.py` | TO DO |
| E7-T5 | IMPL | Pinning: commit SHA for GitHub registries (via E5-T3's pointer when offline), content hash of the YAML for path registries and `--workflow-dir` — `version_resolver` raises for a ref on a path registry, so a hash is the only available identity there. Include the pin in every invocation result and expose a re-check that reports drift **without** mutating the live catalogue (DD6; the spec forbids per-connection variation, DD3). | `src/conductor/mcp/serve/pinning.py` | TO DO |
| E7-T6 | IMPL | `build_catalogue(...)`: enumerate → filter by the four-rung ladder (`--deny` > `--allow` > `mcp.expose` > default-on, with `--registry` selecting the candidate set one level above it) → resolve schemas through the three-tier ladder under a startup deadline → pin → sanitize → qualify collisions → decide direct-tools vs discovery. Return an immutable `Catalogue`. Reject a workflow whose `input:` collides with `_wait_seconds`, logging the reason per FR10. On any parse failure — including the `${VAR}`-missing and parent-directory `!file` cases from P4 — expose with `{"type": "object"}` and an explanatory description (NFR2). | `src/conductor/mcp/serve/catalogue.py` | TO DO |
| E7-T7 | TEST | Naming and sanitizing: slug charset and length, prefixing, both-sides collision qualification, control-character stripping, length cap. | `tests/test_mcp/test_serve_naming.py` | TO DO |
| E7-T8 | TEST | Tool generation: all five input types; `required`/`default`/`description` survive; `_wait_seconds` present and documented; no `outputSchema`; a workflow declaring `_wait_seconds` is rejected. | `tests/test_mcp/test_serve_toolgen.py` | TO DO |
| E7-T9 | TEST | Catalogue: every ladder ordering that distinguishes a rung (`--deny` beats `--allow`; `--allow` overrides `mcp.expose: false`; `--registry` excludes non-candidates entirely); the schema ladder's three tiers; NFR1 (network patched to raise, warm cache, under 2s); NFR2 for both parse-failure modes; the discovery threshold decision. | `tests/test_mcp/test_serve_catalogue.py` | TO DO |
| E7-T10 | TEST | Pinning: SHA for GitHub, content hash for path, drift detected and reported without the catalogue changing. | `tests/test_mcp/test_serve_pinning.py` | TO DO |

**Acceptance criteria**
- [ ] A frozen catalogue is built from a fixture registry with zero network I/O.
- [ ] The exposure ladder behaves exactly as FR2 specifies in every distinguishing case.
- [ ] No workflow is ever silently dropped for an environmental reason.
- [ ] Two registries publishing one slug yield two qualified names.

---

### E8 — The server: CLI, stdio transport, `tools/list` (FR1, FR10, DD3, DD9)

**Goal.** A running MCP server over stdio that publishes the frozen catalogue.

**Prerequisites.** E1, E7.

**Grounding.** Verified in this repo's `.venv` on `mcp` 1.28.1:
`mcp.server.lowlevel.Server` exposes `@server.list_tools()` and
`@server.call_tool()`; `call_tool` validates arguments against `inputSchema`
via `jsonschema` before dispatch (so schema enforcement is the SDK's job, not
ours) and accepts a `CallToolResult` return for full control of `content` +
`structuredContent`; `mcp.server.stdio.stdio_server` is present. Note stdout is
the protocol channel — `cli/app.py:67` already makes the shared `console`
stderr-bound, but the update hint at `cli/app.py:391` and any `output_console`
use would corrupt the stream.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E8-T1 | IMPL | `mcp` Typer sub-app with a `serve` command, following `cli/checkpoint.py` / `cli/gate.py` (`no_args_is_help=True` — this group has no default action, so it is *not* the `fleet` deviation). Flags: `--registry`, `--allow`, `--deny`, `--workflow-dir` (all repeatable), `--toolsets`, `--max-direct-tools` (default 25), `--max-wait-seconds` (default 300), `--tool-prefix`, `--max-concurrent-runs` (default 0, R3), `--introspect-full` (R4). Escape brackets in every `help=` string per the markup convention (`AGENTS.md`, rule G). | `src/conductor/cli/mcp.py` | TO DO |
| E8-T2 | IMPL | Register with `app.add_typer(mcp_app, rich_help_panel="Environment")`, matching `registry` and `plugin` — the panel for commands that configure how Conductor reaches the outside world. | `src/conductor/cli/app.py` | TO DO |
| E8-T3 | IMPL | Keep stdout protocol-pure: suppress the startup update hint for this subcommand (extend the `subcommand not in ("update", "doctor")` guard at `cli/app.py:391`), and route every server-side message through a stderr console built with `make_console(stderr=True)`. Import the MCP SDK lazily inside `serve` so `conductor --help` does not pay for it. | `src/conductor/cli/mcp.py`, `src/conductor/cli/app.py` | TO DO |
| E8-T4 | IMPL | Wire the catalogue onto the low-level `Server`: `list_tools` returns the frozen list, byte-identical on every call and every connection (DD3). Run it over `stdio_server()`. | `src/conductor/mcp/serve/server.py` | TO DO |
| E8-T5 | IMPL | Startup summary on stderr (FR10): exposed count, direct-vs-discovery mode, and per tool its name, source registry and pinned identity; every collision it qualified, at warning level naming both registries; every workflow exposed with a degraded schema and why. This is the only channel a stdio server has, and hosts surface it in their MCP logs. | `src/conductor/mcp/serve/server.py` | TO DO |
| E8-T6 | TEST | Drive the server over an in-memory stream pair: `initialize` succeeds, `tools/list` returns the expected names and schemas, and two sequential connections return an identical list (DD3's "MUST NOT vary per-connection"). | `tests/test_mcp/test_serve_server.py` | TO DO |
| E8-T7 | TEST | CLI: `--help` renders (brackets escaped), flags parse and reach `ServeOptions`, the startup summary lands on stderr, stdout carries only protocol bytes, and `mcp` appears in the noun-group and panel assertions. | `tests/test_cli/test_mcp_serve.py`, `tests/test_cli/test_help_panels.py` | TO DO |

**Acceptance criteria**
- [ ] A host can connect over stdio and list the workflows in a configured registry.
- [ ] Nothing but JSON-RPC reaches stdout.
- [ ] The tool list is fixed at startup and identical across connections.

---

### E9 — Invocation: always detached, bounded wait, bounded fleet (FR4, FR5, G3, G4, DD2, R3)

**Goal.** *Key Components → 3* and data flows A and B: a workflow tool call
never executes a workflow inside the server process — plus R3's concurrency
bound on how many detached runs one server can accumulate.

**Prerequisites.** E8.

**Grounding.** Verified: `launch_background()` (`cli/bg_runner.py`) is
keyword-only and already takes `workflow_path`, `inputs`, `provider_override`,
`skip_gates`, `web_port`, `metadata` and returns
`BackgroundLaunch(url, stderr_log, stdout_log, run_id, workflow_started,
still_running)`. `fleet/launch.py::build_launch_inputs` validates required
inputs and fills defaults but coerces *from strings*, which MCP values are not
— hence the split in E9-T2. Progress notifications are available via
`ServerSession.send_progress_notification(progress_token, progress, total,
message)`, which requires the caller to have supplied a `progressToken` (the
caveat DD2 states).

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E9-T1 | IMPL | `call_tool` dispatch: map the tool name back through the catalogue's reverse map, split off `_wait_seconds`, and reject an unknown tool with an instructive error. Argument validation against `inputSchema` is the SDK's (`validate_input=True` by default) — do not duplicate it. | `src/conductor/mcp/serve/invoke.py` | TO DO |
| E9-T2 | IMPL | `build_typed_launch_inputs(values, input_defs)` in `fleet/launch.py`: the required-input and default-filling half of `build_launch_inputs` without string coercion, since MCP values arrive already JSON-typed. Reuse rather than fork, so one module owns "what counts as a valid input set". | `src/conductor/fleet/launch.py` | TO DO |
| E9-T3 | IMPL | Launch via `launch_background(...)` with `web_port=0` and **never** `skip_gates=True` (DD11 — assert this in code with a comment naming the decision, since it is a one-parameter regression away). Pass a `metadata` stamp identifying the run as MCP-launched, which `launch_background` forwards as CLI metadata and the engine includes verbatim in `workflow_started` — so a human reading the dashboard or event log can tell where the run came from. Shape the run handle: `run_id`, `status`, `url`, `workflow: {name, registry, pinned}`, `started_at`, `next`. Surface `workflow_started=False` as an initializing note rather than a failure, matching `cli/app.py`'s existing treatment. | `src/conductor/mcp/serve/invoke.py` | TO DO |
| E9-T4 | IMPL | `_wait_seconds` resolution per FR5: `0` returns immediately; `> 0` waits up to N; omitted defers to `mcp.mode`, with `sync` resolving to the `--max-wait-seconds` ceiling rather than an unbounded wait. The ceiling applies to every blocking path. Return as soon as a terminal status **or `at-gate`** is derived — reaching a gate ends the wait (DD2's second ⚠️). | `src/conductor/mcp/serve/invoke.py` | TO DO |
| E9-T5 | IMPL | Emit `notifications/progress` during a bounded wait when the caller supplied a `progressToken`, and skip silently when it did not. On deadline return the handle plus current progress and an explicit next action, never a bare status blob. | `src/conductor/mcp/serve/invoke.py` | TO DO |
| E9-T6 | IMPL | Result shaping: the rendered `output:` dict as `structuredContent` plus a human-readable text block, with no `outputSchema` (DD5). Bound every result; anything large becomes a `resource_link` (NFR6). | `src/conductor/mcp/serve/invoke.py` | TO DO |
| E9-T7 | IMPL | **R3:** `--max-concurrent-runs`, default `0` = unbounded so behaviour is unchanged unless an operator opts in. Count the runs **this server process launched** that are still live — an in-process set of `run_id`s filtered through `read_run_record(run_id)` + liveness. Verified: `RunRecord` has no metadata field (nine fields, deliberately), so the E9-T3 stamp reaches the event log but not the record; counting by stamp would mean a bounded event-log head read per live run per launch, and counting *all* live records would charge a user's own unrelated `conductor run` against an MCP cap. The in-process set avoids both. State the consequence plainly in the docstring: restarting the server resets the count, which is consistent with the design's "the MCP server owns no execution state" principle rather than a lapse from it. At the cap, **reject** the launch with a message naming the cap and pointing at `conductor_list_runs` / `conductor_cancel_run` — never queue, since the design bounds things at startup rather than adding runtime scheduling. | `src/conductor/mcp/serve/options.py`, `src/conductor/mcp/serve/invoke.py` | TO DO |
| E9-T8 | TEST | The run is detached in **every** mode (assert `launch_background` is called with the same arguments for `_wait_seconds` 0 and 120); a dashboard URL is present in every result (G4); `skip_gates` is never `True`; the MCP metadata stamp is passed; `_wait_seconds` resolution for all four cases including `mode: sync`; the ceiling caps an over-large request; reaching a gate ends the wait early; progress is emitted only with a token. R3: default `0` never rejects; at the cap a launch is rejected with an instructive message and nothing is forked; a run that has since exited frees a slot; a live run this server did not launch does not count toward the cap. | `tests/test_mcp/test_serve_invoke.py` | TO DO |

**Acceptance criteria**
- [ ] Invoking a tool forks a detached run and returns a handle in under the launch gate's own budget.
- [ ] No code path passes `skip_gates=True`.
- [ ] A blocking call is bounded in every mode.
- [ ] `--max-concurrent-runs` defaults to unbounded and rejects rather than queues when set.

---

### E10 — Run lifecycle tools (FR6, FR7, DD11)

**Goal.** `conductor_run_status` / `conductor_await_run` / `conductor_cancel_run`
/ `conductor_list_runs` over live *and* completed runs — data flows C and D.

**Prerequisites.** E2, E9.

**Grounding.** Verified: `derive_run_summary(record)` already yields
`status` (`running` / `at-gate` / `paused` / `completed` / `failed`),
`current_step`, totals, `gate: GateInfo` with `agent_name` / `prompt` /
`options` / `option_details`, and `gate_resolvable`. `cli/app.py::stop_records`
is the shared stop implementation with a verify-then-report contract and a
`confirm=None` mode built for a non-CLI caller (the Fleet TUI already uses it)
— so cancel reuses it rather than re-implementing the ladder.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E10-T1 | IMPL | The three-source resolver from *Key Components → 4*: `read_run_record(run_id)` → `derive_run_summary` for a live run (enriched from `GET /api/info`); else `read_terminal_record(run_id)`; else `find_event_log_for_run(run_id, started_at)` for a crashed run. Return a single shape from all three, with a field naming which source answered — a caller must be able to tell "completed cleanly" from "process vanished". | `src/conductor/mcp/serve/runs.py` | TO DO |
| E10-T2 | IMPL | `conductor_run_status(run_id)`: the resolver's output plus, at a gate, the gate's prompt, options, `option_details` and the dashboard approval URL (FR7). Every MCP-launched run has a port by construction (DD2), so `gate_resolvable` is always true here — state that in the result rather than leaving the caller to infer it. | `src/conductor/mcp/serve/runs.py` | TO DO |
| E10-T3 | IMPL | `conductor_await_run(run_id, wait_seconds=60)`: bounded by `--max-wait-seconds`, emitting progress, returning on terminal **or** `at-gate`, and naming the approval URL as the next action in its timeout text (DD11's second bullet). | `src/conductor/mcp/serve/runs.py` | TO DO |
| E10-T4 | IMPL | `conductor_cancel_run(run_id, force=false)`: reuse `cli/app.py::stop_records` with `confirm=None` and a silent console, so the graceful `POST /api/stop` rung (the only one that writes a checkpoint via `handle_dashboard_stop`) is tried first and the verify-then-report contract is inherited. Report `stopped` / `failed` honestly; a run that is already terminal is a distinct, non-error outcome. | `src/conductor/mcp/serve/runs.py` | TO DO |
| E10-T5 | IMPL | `conductor_list_runs(status?, workflow?, limit=20)` over live records ∪ terminal records, deduplicated by `run_id` with the live record winning. `status="at-gate"` is the query that surfaces parked runs (DD11's third bullet). | `src/conductor/mcp/serve/runs.py` | TO DO |
| E10-T6 | TEST | Status for a live run, a run at a gate (prompt/options/URL present), a cleanly-finished run (from the terminal record, with no process, port or event log needed), and a crashed run (event-log fallback, with the source named). Await returns early at a gate and on terminal status, and its timeout text names the URL. Cancel routes through `stop_records` and reports an already-terminal run distinctly. List filters by status and workflow and dedupes a `run_id` present in both sources. | `tests/test_mcp/test_serve_runs.py` | TO DO |

**Acceptance criteria**
- [ ] A `run_id` is answerable before, during, at a gate, and after the run.
- [ ] Cancel writes a checkpoint via the graceful rung when it can.
- [ ] Nothing here re-implements the stop ladder or the status derivation.

---

### E11 — `introspect` and `diagnose` toolsets (FR8, DD12, NFR6, R4)

**Goal.** Absorb #135 as the error path of #432 — thin adapters over
`fleet/summary.py` and `providers/diagnostics.py`, adding **zero** tools to the
default footprint (both toolsets are off by default, DD3) — with R4's field
posture applied where the sensitive payloads actually live.

**Prerequisites.** E10.

**Grounding.** Verified: `read_event_log_full`, `derive_step_detail` and
`derive_run_detail` exist and are already the TUI's data source;
`providers/diagnostics.py::gather()` returns a `DoctorReport` whose components
all implement `to_dict()`; `cli/validate.py::validate_workflow` returns
`(is_valid, config)` and takes a console, so it is directly reusable with a
silent console. `mcp.types.ResourceLink` is present with `uri` / `name` /
`mimeType` / `size` / `description`.

⚠️ **Where R4 actually bites.** `derive_step_detail` builds its `tool` /
`tool_result` activity lines from `data.get("tool_name")` **only**
(`fleet/summary.py:1066`, `:1068`) — arguments and results are already
discarded. The payloads live in the raw event records that
`read_event_log_full` returns: `agent_tool_start` carries `arguments` and
`agent_tool_complete` carries `result` (`providers/copilot.py:2066`, `:2079`).
So the reduction belongs to `conductor_run_events`, and `conductor_node_detail`
satisfies R4 by construction — which E11-T2 asserts rather than assumes.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E11-T1 | IMPL | Toolset gating: `--toolsets` enables `introspect` / `diagnose`, both off by default, decided at startup and never per request (DD3). Follow the GitHub MCP server's `--toolsets` vocabulary the design cites. | `src/conductor/mcp/serve/server.py`, `src/conductor/mcp/serve/options.py` | TO DO |
| E11-T2 | IMPL | `conductor_run_events(run_id, ...)` over `read_event_log_full`, with filtering and a hard result bound. **R4:** replace `agent_tool_start.arguments` and `agent_tool_complete.result` with `{name, status, byte_size}` unless `--introspect-full` is set, computing `byte_size` from the serialized original so the caller learns the size it is not being shown. `conductor_node_detail(run_id, agent)` over `derive_step_detail` returns prompt and output **in full** (R4) — add a test asserting its activity lines still carry no payload, so a future change to `ActivityLine` cannot silently reopen the exposure. `conductor_plan_tree(...)` from the parsed `WorkflowConfig`. | `src/conductor/mcp/serve/introspect.py` | TO DO |
| E11-T3 | IMPL | `conductor_doctor()` over `providers/diagnostics.gather()` and `conductor_validate_workflow(name)` over `cli/validate.validate_workflow` with a silent console. Both return structured reports the server generated itself, so DD12's link-only rule does not apply to them — say so in the module docstring so a later reader does not "fix" it. `conductor_validate_workflow` takes a **catalogue tool name**, never a path (NFR3). | `src/conductor/mcp/serve/diagnose.py` | TO DO |
| E11-T4 | IMPL | `conductor_run_logs(run_id)`: `ResourceLink` content blocks for `.bg.stderr.log` / `.bg.stdout.log` / `.events.jsonl` plus `structuredContent` carrying status, the terminal record's error type and message, and per-file `size` / `modified_at` / `exists`. **Never** file contents, regardless of size (NFR6). A path that no longer exists reports `exists: false` with the same path. Include the "read these with your own file tools" note the design specifies. | `src/conductor/mcp/serve/diagnose.py` | TO DO |
| E11-T5 | TEST | Introspect: events query bounded and filtered; **R4** — a tool event's `arguments` / `result` are absent by default and present under `--introspect-full`, with `byte_size` reported in both the reduced and full shapes; node detail returns prompt and output in full and no tool payload either way; plan tree matches the YAML; both tools are absent unless enabled. | `tests/test_mcp/test_serve_introspect.py` | TO DO |
| E11-T6 | TEST | Diagnose: `conductor_run_logs` returns links and never bytes (assert no log line appears anywhere in the serialized result); a pruned path reports `exists: false`; the error type/message come from the terminal record; doctor and validate return structured reports; validate refuses a path-shaped argument. | `tests/test_mcp/test_serve_diagnose.py` | TO DO |

**Acceptance criteria**
- [ ] Default tool footprint is unchanged by this epic (N + 4).
- [ ] No log or event-log file contents cross the protocol boundary.
- [ ] Tool arguments and results are withheld by default and restored only by an explicit operator flag.
- [ ] A failed run is diagnosable from the same connection that started it.

---

### E12 — Discovery fallback above the tool cap (FR9, DD3, G7)

**Goal.** Degrade predictably rather than silently when a registry is large.

**Prerequisites.** E9.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E12-T1 | IMPL | `conductor_find_workflow(query)` returning catalogue entries with their descriptions and input schemas, and `conductor_run_workflow(name, inputs, _wait_seconds?)` dispatching through the same invocation layer as a generated tool. `name` is a catalogue key, never a path or registry source (NFR3). | `src/conductor/mcp/serve/discovery.py` | TO DO |
| E12-T2 | IMPL | Decide direct-vs-discovery **at startup from the exposed count** and log which mode was chosen and why (FR9, FR10). It can never be a runtime switch — the spec forbids a tool list that varies as a side effect of another request (DD3). | `src/conductor/mcp/serve/catalogue.py`, `src/conductor/mcp/serve/server.py` | TO DO |
| E12-T3 | TEST | Above the cap the per-workflow tools are absent and the pair is present; below it, the reverse; the mode does not change mid-connection; `conductor_run_workflow` refuses a path-shaped `name`; the startup log names the count and threshold. | `tests/test_mcp/test_serve_discovery.py` | TO DO |

**Acceptance criteria**
- [ ] A registry above `--max-direct-tools` serves exactly two workflow tools.
- [ ] The choice is visible in the startup summary.
- [ ] No tool anywhere accepts a filesystem path, URL, or registry source.

---

### E13 — `conductor doctor` MCP section (Impact Analysis → Operational)

**Goal.** Give the operator an out-of-band way to see what a server *would*
expose, since a stdio server has no console and a misconfiguration otherwise
presents as "the tools aren't there".

**Prerequisites.** E7.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E13-T1 | IMPL | `McpServeDiagnostic` + `gather_mcp_serve()` following the existing `RegistryDiagnostic` / `to_dict()` shape: registries enumerated, workflows exposed, mode, collisions qualified, schemas that fell back, and pins. Must never raise — `doctor` reports problems, it does not have them. | `src/conductor/providers/diagnostics.py` | TO DO |
| E13-T2 | IMPL | Render the section in the doctor CLI, following the existing thin-renderer convention. | `src/conductor/cli/doctor.py` | TO DO |
| E13-T3 | TEST | The section renders with no registries, with one, and with a collision; a broken registry degrades to a reported problem rather than an exception. | `tests/test_cli/test_doctor.py` | TO DO |

**Acceptance criteria**
- [ ] `conductor doctor` shows the would-be exposed set without a host attached.
- [ ] It never raises on a broken registry.

---

### E14 — Documentation and release (G1, FR10, Impact Analysis, R1)

**Goal.** Make the feature discoverable and its boundaries explicit — including
the ones that are deliberate (no log contents, no auto-skipped gates, no
`outputSchema`) and the one user-facing contract change R1 accepts.

**Prerequisites.** E4, E8–E13.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E14-T1 | IMPL | `docs/mcp-server.md`: host configuration snippets for Claude Code / VS Code / Cursor, the exposure ladder and its precedence, toolsets, the `mcp:` block, the run lifecycle, and an explicit *Limits* section covering DD5 (no `outputSchema`), DD11 (a gate parks indefinitely), DD12 (links not contents), R4 (tool payloads withheld unless `--introspect-full`), and DD9 (stdio only). | `docs/mcp-server.md` | TO DO |
| E14-T2 | IMPL | Disambiguate client from server at the top of the existing MCP page and cross-link both ways — `docs/mcp-tools.md` is about Conductor *calling* MCP tools, which is the mirror image of this feature. | `docs/mcp-tools.md` | TO DO |
| E14-T3 | IMPL | CLI reference entry for `conductor mcp serve`: every flag (including `--max-concurrent-runs` and `--introspect-full`), the environment it inherits, and the note that its summary goes to stderr because stdout is the protocol. | `docs/cli-reference.md` | TO DO |
| E14-T4 | IMPL | `AGENTS.md`: the `mcp/serve/` package and `cli/mcp.py` in the architecture section; the terminal record under `fleet/`; **R1's** scope change to `conductor status` / `fleet list` / History; the `mcp:` block under Key Patterns; and `tests/test_mcp/` in the test-structure list. | `AGENTS.md` | TO DO |
| E14-T5 | IMPL | Changelog entries for the terminal record, the completed-run surfacing (called out as a contract change, per R1), the `mcp:` block, and the server. | `CHANGELOG.md` | TO DO |
| E14-T6 | TEST | `make check`, `make test`, and `make validate-examples` all green, including the new `examples/mcp-serve.yaml`. | — | TO DO |

**Acceptance criteria**
- [ ] A user who has run `conductor registry add` can follow the docs to a working server with no workflow edits (G1).
- [ ] Every deliberate limitation is documented as a limitation, not omitted.
- [ ] The `status` / `fleet list` scope change is documented where a user upgrading will see it.
- [ ] Full check and test suites pass.

---

## References

**Source design (authoritative)**

- [`docs/projects/mcp-server/conductor-mcp.design.md`](./conductor-mcp.design.md)
  — *Solution Design: `conductor mcp serve` — workflows as MCP tools*. Every
  epic above names the section or decision (DD0–DD13, FR1–FR12, NFR1–NFR7,
  G1–G10) it delivers. Plan-level resolutions R1–R4 are recorded in
  **Open Questions → Resolved decisions** and do not modify the design.

**Conductor issues**

- [#432](https://github.com/microsoft/conductor/issues/432) — source issue
- [#135](https://github.com/microsoft/conductor/issues/135) — introspection MCP server (absorbed)
- [#431](https://github.com/microsoft/conductor/pull/431) — the Fleet Manager, merged as `d785a28`; the reason `introspect` is adapters rather than parsers
- [#436](https://github.com/microsoft/conductor/issues/436) — History's single-forward-pass log scan, which E4-T4's enrichment must not disturb
- [#116](https://github.com/microsoft/conductor/issues/116) — crash debugging; what `conductor_run_logs` addresses
- [#230](https://github.com/microsoft/conductor/issues/230) — publish workflow JSON Schema; shares machinery with DD5's follow-up
- [#392](https://github.com/microsoft/conductor/issues/392) — `type: mcp` step; the mirror image of this feature
- [#410](https://github.com/microsoft/conductor/issues/410) — confirmed-start launch gate that E9 inherits

**Conductor code this plan builds on** (verified at `b6c5b11`)

- `src/conductor/fleet/records.py` — `RunRecord`, `run_records_dir()`, and the three non-recursive `*.json` globs (`:971`, `:1019`, `:1104`) that the terminal subdirectory must stay invisible to
- `src/conductor/fleet/summary.py` — `derive_run_summary` / `derive_run_detail` / `derive_step_detail` / `read_event_log_head|tail|full`; note `:1066`/`:1068` build tool activity lines from `tool_name` alone (R4)
- `src/conductor/fleet/history.py` — `build_history_entries`, `HistoryEntry`, and the single-pass scan E4-T4 enriches after rather than inside
- `src/conductor/fleet/retention.py` — `_companion_paths`, `_prune_event_logs_impl`, `maybe_prune_event_logs`
- `src/conductor/fleet/launch.py` — `resolve_workflow`, `build_launch_inputs`, and its standing warning against re-implementing detached spawning
- `src/conductor/cli/bg_runner.py` — `launch_background()` and `BackgroundLaunch`
- `src/conductor/cli/app.py` — `status` (`:1374`, deliberately non-pruning), `stop_records` / `_stop_process` (the shared stop ladder), the sub-app registrations at `:60–64`, and the update-hint gate at `:391`
- `src/conductor/cli/fleet.py` — `fleet list` (`:82`) and its hard-coded `"running"` status cell (`:124`) that R1 makes conditional
- `src/conductor/registry/` — `index.py` (`WorkflowInfo`), `cache.py` (`_meta/<sha[:12]>/`, `CACHE_LAYOUT_VERSION`, `.complete` sentinel), `version_resolver.py` (`materialize_to_sha`, which is why R2's pointer is needed)
- `src/conductor/plugins/fetch.py` — the `_refs/<slug>.json` pointer pattern R2 adopts for registries
- `src/conductor/config/schema.py` — `InputDef` (`:50`), `WorkflowDef` with `extra="forbid"` (`:3529`)
- `src/conductor/providers/copilot.py` — `agent_tool_start.arguments` / `agent_tool_complete.result` (`:2066`, `:2079`), the payloads R4 reduces
- `src/conductor/providers/diagnostics.py` — `gather()` and the `to_dict()` component convention
- `docs/projects/fleet-manager/fleet-manager.plan.md` — the plan format and epic granularity this document follows

**MCP SDK surface** (verified in this repo's `.venv`, `mcp` 1.28.1)

- `mcp.types.Tool` — `inputSchema`, `outputSchema`, `annotations`, `execution`
- `mcp.types.ResourceLink` — `uri` / `name` / `mimeType` / `size` / `description`
- `mcp.types.CallToolResult` — `content`, `structuredContent`, `isError`
- `mcp.server.lowlevel.Server` — `@list_tools()`, `@call_tool(validate_input=True)` (jsonschema-validated before dispatch), `request_context`
- `mcp.server.stdio.stdio_server`, `mcp.server.session.ServerSession.send_progress_notification`
- `mcp.types.CreateTaskResult` — present, unused in v1 (DD8)

**MCP specification and guidance** — see the source design's own References
section for the full list (spec `2026-07-28` tool-naming and stability rules,
MRTR, the Tasks extension, host tool-count limits, and Anthropic's tool-writing
guidance). They are not duplicated here.
