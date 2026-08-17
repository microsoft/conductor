# Solution Design: Fleet Manager

> Status: `idea` — speculative, not yet committed. Source issue: TBD.
>
> This document is a **solution design** for engineering and architecture
> review. It covers *what* and *why*; the epic/task breakdown and
> file-by-file changes belong to a separate planning step that consumes
> this design.

## Summary

Introduce a higher-level management layer for operating **multiple concurrent
Conductor workflow runs**: a terminal UI (`conductor fleet`) that lists every
running workflow with its live status, opens the existing per-run web
dashboard on demand, exposes registries and providers as drill-down views, and
launches new runs from a file or registry reference with typed inputs.

The prerequisite — and the larger half of the work — is that **Conductor
cannot currently enumerate its own running workflows**. Only `--web-bg` runs
write a run record; foreground runs are invisible. That must be fixed in core
before any UI is worth building, and it fixes a real bug on its own.

## Motivation

A single user routinely has several Conductor workflows in flight at once —
a long design run, a release-prep run, a batch triage loop. Today there is no
way to see them together. The available tools are:

- `conductor stop`, which lists only `--web-bg` runs and cannot see a
  foreground run at all.
- One web dashboard per run, each on its own port, each requiring its URL to
  have been captured at launch time.
- A directory of JSONL event logs in `$TMPDIR/conductor/` with no index and no
  retention.

The practical consequences: a workflow parked at a human gate can sit unnoticed
for an hour; a run that died silently is indistinguishable from one still
working; there is no aggregate view of tokens or spend; and starting a new run
means finding the workflow file, remembering its inputs, and composing a
command line.

## Goals

- Enumerate all live runs reliably, including foreground runs.
- Per run, at a glance: workflow name, current step, total elapsed, time on
  current step, status (including **at a human gate**), tokens, cost.
- Open the existing web dashboard for any run that has one.
- Browse configured registries and the workflows they contain.
- Browse available providers and the models each supports, as a drill-down
  (counts first, detail on demand).
- Launch a workflow from a file path or registry reference, with a form
  generated from its declared `input:` schema.
- Work correctly when **nothing** is running — the empty state is a normal
  state, not an error.
- Stay usable over SSH.

## Non-goals (v1)

- **Multi-user or team visibility.** Scope is explicitly a single user on one
  machine. See "Design decisions" for what this removes and what it would
  take to change.
- **Replacing the per-run web dashboard.** The TUI is deliberately breadth-only;
  depth stays in the dashboard.
- **Supervising child processes.** The TUI launches detached runs and forgets
  them; it is not a daemon and runs must outlive it.
- **Rendering the workflow DAG, agent messages, or tool output** in the
  terminal. That is the dashboard's job and duplicating it is the main way
  this feature could become expensive.
- **Live token counts for the in-flight agent.** Not currently available in the
  event stream (see "Known data gaps").
- **Git worktree isolation per run.** Conductor already has per-agent
  `working_dir`; worktree management is a coding-agent concern.

## Background: what exists today

| Artifact | Written for | Location | Contains |
|---|---|---|---|
| Run/PID record | **`--web-bg` runs only** | `~/.conductor/runs/<name>-<port>.pid` | pid, port, workflow, started_at, run_id, log_file |
| JSONL event log | **every run, always** | `$TMPDIR/conductor/conductor-<name>-<ts>-<runid>.events.jsonl` | full structured event stream |

`write_pid_file` has exactly one call site, `cli/bg_runner.py`. Foreground
`conductor run` and `conductor run --web` write no record.

Four of the five goals are largely assembly over existing surfaces:

| Goal | Existing surface |
|---|---|
| Per-run observability | `EventLogSubscriber` JSONL. `workflow_started` carries full topology (agents, types, models, providers, version); `agent_started` / `agent_completed` carry `elapsed`, `tokens`, `cost_usd`, `context_window_used/max`; `gate_presented` / `gate_resolved` carry gate state. |
| Launch dashboard | Dashboard already runs per-run on its own port; the run record already carries the port. |
| Registries + workflows | `registry/config.py::load_config`, `registry/index.py::load_index`, `providers/diagnostics.py::gather_registries`. |
| Providers + models | `conductor doctor --json` already emits env + providers + models, with per-model reasoning-effort support and context-window limits (issue #301), plus `ProviderCapabilities` tier. Stable serialized contract — the TUI never touches provider internals. |
| Launch with inputs | `resolve_ref` / `resolve_and_fetch` handle file and registry refs; `wf.input` is a typed dict (`type`, `required`, `default`, `description`) already rendered by `conductor show`. |

## The blocking problem: run discovery

### Why the event log alone is not enough

The tempting shortcut is to scan `$TMPDIR/conductor/*.events.jsonl` and treat
"no terminal event yet" as "still running". Measured on a real developer
machine:

```
scanned 1522 files in 0.136s
non-terminal logs idle >1h (definitely dead, would show 'running'): 228
non-terminal logs touched <1h ago (ambiguous):                        0
```

**228 false positives, 0 true positives.** Every crashed, killed, or `Ctrl-C`'d
run leaves a log with no terminal event, permanently. Absence of a terminal
event is not evidence of life. A fleet view built on this alone is noise.

Scan *performance* is fine — the problem is correctness.

### The fix

Write a run record for **every** run, and confirm liveness with a process
probe.

- **Key by `run_id`, not port.** Foreground runs have no port, and port is
  already a poor key — a stale `design-41949.pid` from a long-dead process
  demonstrates the collision risk.
- **Record**: `run_id`, `pid`, `workflow_path`, `workflow_name`, `started_at`,
  `event_log_path`, `port | null`, `mode` (`fg` / `fg-web` / `bg`),
  `checkpoint_dir`.
- **Remove on exit.** `remove_pid_file_for_current_process()` is the existing
  shape.
- **Liveness = `cli/pid.py::_is_process_alive(pid)`.** This already exists and
  is already hardened for the Windows footguns (issues #166, #344:
  `OpenProcess` / `GetExitCodeProcess` rather than `os.kill(pid, 0)`). Reuse
  it; do not reimplement.
- **Write atomically** (temp file + `os.replace`). A single *user* is not a
  single *process* — concurrent runs are the entire point of this feature.
  Readers must tolerate partially-written or vanished files; `read_pid_files()`
  already has the right posture, treating unparseable files as removable rather
  than crashing.

This is independently valuable and should ship regardless of whether the TUI
follows: it fixes `conductor stop`'s blindness to foreground runs, and the run
record is also the handle a fleet-wide kill needs.

### Second-order cleanup

`$TMPDIR/conductor/` accumulates without bound — 1522 files / 12 MB observed,
mixing real runs with test artifacts. If run history is offered, that directory
needs retention. Mirror the existing `keep_last` /
`rotate_periodic_checkpoints` vocabulary in `engine/checkpoint.py` rather than
inventing a second policy language.

## User experience

```
┌─ conductor fleet ───────────────────────────────────────────────┐
│ RUNS (3 active)                                    ↻ 2s   ? help│
├─────────────────────────────────────────────────────────────────┤
│ ● design        architect        18m  12m  191k tok  ~$2.14  :48│
│ ▲ release-prep  review_gate     1h04   —    88k tok  ~$0.91  :51│
│ ● triage        for_each[3/7]    4m    1m   23k tok      —      │
├─────────────────────────────────────────────────────────────────┤
│ [enter] detail [w] dashboard [n] new run [k] kill [K] kill-all  │
│ [p] providers   [r] registries  [h] history  [q] quit           │
└─────────────────────────────────────────────────────────────────┘
   ▲ = at human gate     ● = running
   columns: workflow · current step · total elapsed · elapsed on step ·
            tokens · cost · dashboard port
```

Screens, using Textual's `Screen` push/pop so drill-down has a real
Escape-to-return stack:

- **Runs** (home) — flat list, sorted by recency, polled refresh.
- **Run detail** — topology from `workflow_started`, per-agent timings from the
  event log, current step highlighted. Not a DAG.
- **Providers** → drill to models. Collapsed summary first (`copilot — 14
  models`), expand for per-model reasoning effort and context window.
- **Registries** → drill to workflows → drill to inputs.
- **New run** — pick file or registry ref, form generated from `wf.input`,
  choose mode, shell out to `conductor run --web-bg`.
- **History** — completed runs, subject to retention.

Empty state is a first-class screen: when nothing is running, the Runs screen
shows the launch affordance rather than an empty table.

### Status vocabulary

Status is a small explicit state machine, not a boolean:

`running` · `at-gate` · `paused` · `completed` · `failed`

`at-gate` is the highest-value cell in the table — it is the reason to look at
the screen at all — and is rendered as a persistent badge, not a transient
notification.

## Implementation

New module `src/conductor/fleet/`:

- `records.py` — run-record read/write/prune. Core, no new dependency.
- `summary.py` — derive a `RunSummary` from a run record plus its event-log
  tail (current step, elapsed-on-step, token total, gate state).
- `tui/` — Textual app, screens, widgets. Imported only by `conductor fleet`.

CLI surface — a **noun sub-app**, matching the existing `registry` / `gate` /
`checkpoint` groups:

- `conductor fleet` — the TUI. Requires the `tui` extra; if `textual` is
  missing, fail with the install hint rather than a traceback.
- `conductor fleet list` — non-interactive Rich table. Core, no new dependency.

Killing is deliberately **not** `conductor fleet kill`. `conductor stop`
already exists as a flat verb and becomes substantially more capable once
Phase 0 makes every run discoverable (it gains foreground runs and a
meaningful `--all`). The TUI's `k` / `K` bindings call that same code path
rather than introducing a second way to do one thing.

One deliberate deviation: the other three sub-apps set `no_args_is_help=True`,
so a bare `conductor registry` prints help. `fleet` instead uses a
`invoke_without_command=True` callback so bare `conductor fleet` launches the
TUI. This is justified because the TUI *is* the feature and is the hot path,
whereas `registry` / `gate` / `checkpoint` have no sensible default action.

Dependency, following the existing optional-extra precedent
(`claude-agent-sdk`, `aca`):

```toml
[project.optional-dependencies]
tui = ["textual>=1.0"]
```

`pip install conductor` is unchanged for users who never want a TUI;
`conductor fleet list` works for everyone.

### Refresh model

Poll on a timer (~2 s). Full rescan of the run-record directory plus an
event-log tail seek per live run. The upper-bound measurement above (1522
event logs in 0.136 s) is far beyond the realistic working set of ~3–15
concurrent runs. **Do not build a file watcher** — `inotify` /
`ReadDirectoryChangesW` is more code, platform-specific, and buys nothing at
this scale.

### Launch model: viewer, not supervisor

The TUI shells out to `conductor run --web-bg` and forgets.

`cli/bg_runner.py` has already solved detached launching on both platforms
(`CREATE_BREAKAWAY_FROM_JOB` on Windows, `setsid` on POSIX, captured
stdout/stderr for post-mortem, PID file). Re-implementing supervision inside
the TUI would re-introduce every problem that file already fixed, and would
make runs die when the TUI exits — the opposite of the point.

The consequence is a feature: the TUI is **stateless and disposable**. Open it,
close it, reopen it over SSH from another machine; it just re-reads run
records. No daemon, no socket, no lifecycle, nothing to leak.

### Phasing

| Phase | Deliverable | Depends on | Standalone value |
|---|---|---|---|
| **0** | Run record for every run + `conductor fleet list` + `$TMPDIR` retention | — | **High** — fixes `conductor stop`'s blindness to foreground runs today |
| **1** | Textual fleet list, read-only, auto-refresh; open dashboard; kill / kill-all | 0 | The core feature |
| **2** | Providers + registries drill-down | 1 | Pure reuse of `doctor --json` |
| **3** | Launch-with-inputs | 1 | Completes the feature |
| **4** | Gate visibility polish, notifications, history | 0, 1 | Nice-to-have |

Phase 0 is where to start and is worth doing even if the TUI is deferred
indefinitely.

## Known data gaps

**Live token counts.** Verified across real event logs: token and cost fields
appear **only** on `agent_completed`. There is no mid-flight usage event, so
the fleet list can show tokens for completed agents but not a live counter for
the agent currently running. Two options:

- Accept it, and label the column honestly (`Σ tokens (completed)`).
  Recommended for v1; zero core change.
- Emit incremental usage at turn boundaries. This is a **provider-parity
  change** — per AGENTS.md it must land across Copilot, Claude,
  claude-agent-sdk, hermes and aca together, and be reflected in the dashboard,
  JSONL logger and console subscriber. Defer unless triage proves it necessary.

**Cost is frequently `null`.** Unpriced models are a known existing condition
(issue #265) and `WorkflowUsage` already exposes `unpriced_agents` /
`has_unpriced`. The fleet total must reuse that convention and render
`~$X (N unpriced)`. Silently summing nulls into a confident-looking fleet total
would regress a problem this repo already solved once.

**Gates in foreground runs.** A gate parked in a foreground run owns that
terminal's stdin. The TUI can display it but cannot resolve it. See "Open
questions".

## Design decisions and rationale

### TUI rather than web

Considered alternatives: extend the existing React dashboard into a multi-run
app; build a separate web service; build a TUI.

The comparable prior art splits cleanly, and the split is informative rather
than a matter of taste. herdr (Rust/Ratatui, ~25k stars), claude-squad
(Go/Bubble Tea, ~8k) and uzi are TUIs *because they wrap opaque interactive
CLIs* — they need PTY ownership or tmux screen-scraping precisely because
there is no structured event stream. AgentManager reached for
`claude --output-format stream-json` in order to build a web UI. The mature
workflow orchestrators (Temporal, Prefect, Dagster, Airflow) are all web,
because they are multi-user services with server-side history.

Conductor already emits the structured event stream, which makes it
architecturally closer to Temporal/Prefect than to herdr. That is a genuine
argument *for* web, and it is what makes the single-user constraint decisive:

1. **Single user, one machine, no sharing.** The entire class of reasons to
   choose web — multi-user access, phones and laptops, shareable links,
   central history — does not apply. What remains is a local operator triaging
   their own processes.
2. **The existing dashboard is single-run by construction.** One
   FastAPI + uvicorn server per `WorkflowEngine`, `_event_history` in memory,
   one WebSocket, stop/kill/gate endpoints bound to *that* engine's
   `asyncio.Event`s. Making it multi-run means a separate long-lived server,
   cross-run persistence, a new lifecycle and an auth story — a new
   application either way. "Reuse the dashboard" is largely an illusion.
3. **Terminal-native fits the workload.** The user is already in a terminal
   running `conductor run`; triage-and-launch is keyboard work.
4. **SSH.** For a single user moving between machines, SSH *is* the
   remote-access story; no service required.
5. **Security posture.** A fleet manager that can launch arbitrary workflows is
   an arbitrary-code-execution surface (`type: script` runs shell). A local TUI
   with no network listener is the conservative default and nothing pulls the
   other way.
6. **Stack fit.** Textual (36.8k stars, five years old, actively maintained)
   builds on Rich, already a direct dependency. Typer + Rich + Textual is
   coherent.

`textual-serve` — which runs the same Textual app in a browser over WebSockets
— is noted as a **free escape hatch, not part of the justification**. It was
the decisive tie-breaker while scope was uncertain because it made the choice
reversible; with single-user settled, the TUI stands on its own and
`textual-serve` is insurance.

### Command naming

The repo's CLI convention decides this: **flat commands are verbs** (`run`,
`resume`, `validate`, `show`, `stop`, `replay`, `update`, `doctor`), while
**grouped sub-apps are singular nouns** (`registry`, `gate`, `checkpoint`).

This feature is inherently a noun with several operations — list runs, launch,
browse providers, browse registries — so it belongs in the grouped half.
`conductor fleet` also matches the domain vocabulary already used throughout
this document.

Rejected alternatives:

| Candidate | Why not |
|---|---|
| `top` | Not a verb and not a noun group — fits neither half of the convention, and cannot host subcommands (`conductor top providers` reads wrong). Borrowed from Unix `top`, which implies **resource** monitoring (CPU/memory); this measures workflow progress, not machine load. |
| `ps` | Same structural problem as `top`. Works as a *sub*command name at best, and `fleet list` is clearer. |
| `watch` | **Already claimed** by open issue #181 (`conductor watch` — a convergence primitive for iterative fix-validate loops). A genuine collision with an unrelated feature. |
| `runs` | One character from the flagship `run` verb — `conductor run` vs `conductor runs` is a live typo hazard, and the singular form is unavailable. Also breaks the singular-noun pattern of `registry` / `gate` / `checkpoint`. |
| `dashboard` | Collides with the existing per-run web dashboard, the one concept this feature must stay clearly distinct from. |
| `monitor`, `status` | Verbs, so they would have to be flat, which forfeits subcommands. Both also imply one-shot output rather than a live view. |

### Division of labor

> **TUI = breadth. Web dashboard = depth.**
>
> The TUI answers *"what is happening across my fleet, and what needs me?"*
> The dashboard answers *"what exactly is this one run doing?"*

They compose: the TUI's per-run action is to open the browser at that run's
dashboard. No rendering logic is duplicated and neither surface grows into the
other's job. This is also what keeps the TUI cheap — it never needs to render
a DAG, stream agent messages, or display tool output.

### What single-user removes

Deliberate deletions, not oversights:

| Dropped | Why |
|---|---|
| Auth / sessions / tokens | No network listener; nothing to authenticate to |
| Database | Filesystem records + JSONL are sufficient |
| Multi-tenancy in state | No ownership field, no per-owner filtering |
| Pagination / server-side filtering | ~3–15 concurrent runs; render everything |
| Watch-based refresh | Polling is simpler and portable; see "Refresh model" |
| Long-horizon audit history | No compliance consumer; keep last N |
| Push notification service | Terminal bell / OSC 9 is the whole feature |
| Kill-all safety interlocks | No risk of killing another user's run; one confirm |

Surviving despite single-user, and easy to wrongly discard: **atomic writes**
(concurrent runs are the point) and **stale-record cleanup** (`kill -9`
orphans records regardless of user count).

### Same repo, not a separate one

The fleet layer reads the event vocabulary, the run-record format, checkpoint
metadata, `ProviderCapabilities` and the registry index. All of those change in
this repo, frequently. Across a repo boundary every event-type addition becomes
a versioned compatibility problem — working directly against the lockstep
discipline the "Provider Parity" section of AGENTS.md already enforces.

herdr is a separate repo for a reason that does not apply here: it manages
*other people's* agents and must not depend on any of them. Conductor's fleet
manager manages *Conductor*; the coupling is inherent and desirable.

`conductor fleet` is also discoverable from `conductor --help`, where a separate
`conductor-fleet` package would be a second install, release and documentation
surface.

Revisit if the tool grows to manage non-Conductor processes, or becomes a
persistent multi-user service.

### Patterns adopted from prior art

- **Status as a small state machine** — herdr's `working / blocked / idle /
  done` and Temporal's `Running / Waiting on signal / Completed / Failed` are
  both four-state models.
- **"Needs human" as a first-class list state** — persistent badge, not a
  notification. Maps directly onto `gate_presented`.
- **Flat list sorted by recency, not grouped by workflow definition** —
  Prefect's explicit design lesson: operators triage by *which run needs
  attention*, not by which file it came from. Worth resisting the instinct to
  group.
- **Structured step events over log tailing** — Dagster. Conductor gets this
  for free; drill-down should present discrete steps, not scrolling text.
- **A kill switch is not optional.** AgentManager documents an incident in
  which an orchestrator spawned a swarm that reviewed, approved, merged and
  deployed each other's PRs; taking the server down did not stop it, because
  the agents held their own credentials. A view that can see ten runs at once
  needs a kill-all that works by signal — via the run record — independent of
  any dashboard or API being reachable.

## Open questions

1. **Do live token counts justify a five-provider parity change?**
   Recommendation: no for v1; label the column `Σ tokens (completed)` and
   revisit if triage proves it insufficient.
2. **Migration of existing run records.** `~/.conductor/runs/` is currently
   port-keyed and `--web-bg`-only. Re-keying by `run_id` could migrate existing
   files or simply let them age out — they self-prune on the liveness check, so
   ignoring them is defensible.
3. ~~**Command naming.**~~ **Resolved: `conductor fleet`** (noun sub-app). See
   "Command naming" under Design decisions.
4. **Gates in foreground runs.** The TUI can show but not resolve them. Is
   showing enough, or should `--web` become the recommended way to start
   anything intended to be managed from the fleet view? This affects what the
   TUI can *do* rather than what it can display, and is the most consequential
   remaining question.

## Future work

- `textual-serve` to expose the same view in a browser, if remote access
  without SSH is ever wanted.
- Notifications on gate-entry and run-failure (terminal bell / OSC 9).
- Run history browsing with retention, once `$TMPDIR` cleanup exists.
- Multi-user service. If ever needed, **Phase 0 is the reusable asset** — the
  run-record format and liveness contract would become that service's data
  model, so nothing in Phase 0 is wasted by a later pivot.
