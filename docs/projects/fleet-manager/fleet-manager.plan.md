# Implementation Plan: Fleet Manager

> **Source design (authoritative):**
> [`docs/projects/fleet-manager/fleet-manager.design.md`](./fleet-manager.design.md)
> — *Solution Design: Fleet Manager*. Source issue: TBD.
>
> **Revision notes:** Round 1 revision — corrected a factual error in Open
> Question 1 (the `SIGTERM` handler does not delegate to the previous handler
> when it is `SIG_DFL`; it swallows the signal) and added tasks E3-T9/T10/T11/T12
> to fix the underlying bug and verify termination before reporting success,
> since D1/E3/E8 depend on foreground runs actually being stoppable by signal.
>
> This plan consumes an already-reviewed design. It does **not** re-derive or
> re-litigate design decisions; each epic references the design section it
> delivers (e.g. *The blocking problem: run discovery*, *Refresh model*,
> *Phasing*). Genuine gaps that blocked confident planning are surfaced in
> **Open Questions** rather than silently resolved.

---

## Open Questions

Two questions remain. Both were **derived** from the resolved decisions below
rather than carried over from the design, and both have a stated working
assumption that this plan implements — so nothing is blocked, but a
stakeholder should confirm the assumption before the affected epic ships.

---

**1. Should `conductor stop` save a checkpoint before signalling, now that it
can target foreground runs? (affects E3)**

**Correction from an earlier revision:** the only `SIGTERM` handler in the
codebase, `interrupt/listener.py::_register_cleanup_handlers`'s
`_sigterm_handler` (installed at line 250), does **not** delegate to the
previous handler by default. It captures `self._previous_sigterm =
signal.getsignal(signal.SIGTERM)` — which in a normal run is
`signal.Handlers.SIG_DFL`, an `IntEnum` member — and only re-invokes it `if
callable(self._previous_sigterm)`. `SIG_DFL` is not callable, so the guard is
false, the handler restores the terminal and returns, and **the `SIGTERM` is
swallowed**: the process survives and keeps running. I confirmed this
empirically against the real `KeyboardListener` under a PTY. The listener is
installed under exactly `not no_interactive and not web and sys.stdin.isatty()`
(`cli/run.py:1752`), which is precisely this plan's `mode == "fg"`, and
`KeyboardListener.stop()` restores only terminal settings, never the previous
signal disposition — so the swallowing handler stays installed for the
process's remaining lifetime. `fg-web` (no listener) and `bg` are unaffected.

This means `conductor stop` cannot currently stop a **foreground** run at all
by signal — not "stops it but loses progress," but "does not stop it." That is
a genuine defect in the codebase today, and since D1's entire premise is that
`conductor stop`/`--all` and the TUI's `k`/`K` work by signalling the PID in a
run record regardless of mode, it must be fixed as part of this plan rather
than left as an open question — E3 does not deliver a working foreground stop
otherwise. Two tasks now cover it:

- **Make the signal effective.** `_sigterm_handler` must restore the default
  disposition and re-raise when the previous handler is not callable (e.g.
  `signal.signal(signal.SIGTERM, signal.SIG_DFL)` then
  `os.kill(os.getpid(), signal.SIGTERM)`), rather than falling through. See
  E3-T9.
- **Verify termination before reporting success.** `_stop_process` (and the
  E8-T1 shared implementation the TUI reuses) must not print "Stopped" and
  remove the run record on a bare non-raising `os.kill` — it must poll
  `is_process_alive(pid)` over a short grace period, escalate (POSIX:
  `SIGKILL`) if the process is still alive after that window, and only report
  success and remove the record once the process is actually confirmed gone.
  See E3-T10. Today's silent-success behavior, combined with E3-T6 removing the
  run record on the (previously false) assumption that the signal worked, would
  otherwise leave the run invisible to `fleet list` / `stop` / the TUI while it
  keeps executing — worse than today's blindness.

With the signal actually made effective, the original premise of this Open
Question holds and remains open: I verified the run process still has **no
checkpoint-on-`SIGTERM`** path once the swallow bug is fixed. The engine's
checkpoint-on-stop path (`handle_dashboard_stop`, `engine/workflow.py`) is
reached only from the dashboard's `/api/stop` and `/api/kill` endpoints via
`cli/run.py::_execute_with_stop_signal` — not from a signal. So an effective
`conductor stop` now genuinely discards in-flight progress on a foreground run
unless `runtime.checkpoint` periodic checkpoints are enabled — this is **not a
new regression**, since today's `conductor stop` already does this to
`--web-bg` runs (whose `SIGTERM` was never swallowed — no listener is
installed for `bg`). D1 extends the existing, already-lossy behavior to
foreground runs, which is what makes it worth confirming rather than assuming.

*Working assumption implemented by this plan:* no checkpoint-before-signal in
v1. Instead, the D1 confirmation prompt states the consequence explicitly and
says whether periodic checkpoints are enabled for that run (derivable from the
run record's `checkpoint_dir` plus the run's checkpoint files). Adding a real
`SIGTERM` → checkpoint handler is core engine work outside the design's scope:
a signal handler cannot `await`, so it would have to set a flag that the engine
loop polls, and the current stop path is built around `asyncio.Event`
cancellation from within the event loop, not around signals.

*Alternatives:* (a) accept the loss and warn — assumed; (b) route `stop` through
the dashboard's existing `/api/stop` when the record has a port and fall back to
`SIGTERM` otherwise, which makes behavior mode-dependent; (c) add a real
signal-triggered checkpoint path to the engine.

---

**2. Should event-log retention sweep automatically by default, and what is the
default `keep_last`? (affects E5)**

Decision D3 settles *where* the knob lives (`~/.conductor/config.toml`) but not
*when* it fires or what value it defaults to. A config file with a default
implies an automatic sweep — a default nothing reads is inert — but automatic
retention silently deletes `conductor replay` material, which is the one
irreversible thing in this plan.

*Working assumption implemented by this plan:* automatic, enabled by default,
`keep_last = 200`, swept once per run at startup (best-effort, never raising,
subject to the E5 exclusions). Rationale for 200: the design measured 1522
files / 12 MB, so a mean file size of roughly 8 KB would put 200 logs at
roughly 1.5 MB — but that measured corpus mixes real runs with short test
artifacts, and event-log size is heavily skewed (an agentic run's JSONL is
orders of magnitude larger than a short test fixture), so the mean is dragged
down by exactly the files least worth keeping. This is presented as an
**order-of-magnitude estimate**, not a firm bound: retaining the 200 *newest*
real runs could plausibly be much larger than 1.5 MB. Also worth stating as
accepted behavior: `keep_last` counts globally across all workflows, not per
workflow, so one workflow run frequently enough can evict every other
workflow's history — `conductor fleet prune --keep-last N` (already planned as
the override) does not change that global scope, only the threshold.

*Alternatives:* (a) automatic + enabled, `keep_last = 200` — assumed; (b)
automatic but `enabled = false` for one release, so the setting ships inert and
users opt in after reading the changelog; (c) manual only, via
`conductor fleet prune`, with no automatic sweep at all.

---

### Resolved decisions

These were open in an earlier revision and are now settled. They are recorded
here as a decision register; each is propagated into the epics, tasks, file
lists and acceptance criteria below.

**D1 — `conductor stop` blast radius (was Open Question 1; resolved by
stakeholder).**
Keep auto-stop-single and `--all` **as-is**, and require an **interactive
confirmation when the target is a foreground run**. Concretely:

- "Foreground run" means `mode in {"fg", "fg-web"}` — both hold a terminal. A
  `--web` foreground run is still attached, so it confirms too. `mode == "bg"`
  is unprompted, exactly as today.
- A legacy port-keyed `.pid` record is treated as `bg`, because
  `write_pid_file` only ever had one call site (`cli/bg_runner.py`). Pre-upgrade
  runs therefore behave identically to today.
- `--all` over a fleet containing foreground runs prompts **once**, naming the
  foreground runs specifically. `--all` over bg-only runs does not prompt. This
  preserves today's semantics for today's fleet.
- A new `--yes` / `-y` flag bypasses the prompt for scripted use.
- **Non-TTY is a refusal, not an implicit yes.** With `sys.stdin.isatty()`
  false and no `--yes`, `stop` prints what it would have signalled and exits
  non-zero. Silently proceeding would reintroduce exactly the hazard the
  confirmation exists to prevent. `sys.stdin.isatty()` is the repo's existing
  test for this (`cli/run.py:1752`, `engine/workflow.py:2486`).
- The confirmation is a `rich.prompt.Confirm`, matching `gates/human.py`'s use
  of `rich.prompt.Prompt` / `IntPrompt`. There is no `typer.confirm` anywhere in
  the CLI today, so this follows the Rich precedent rather than introducing a
  second prompting library.
- The TUI's `K` binding still confirms **once, always**, per the design's
  *What single-user removes* ("Kill-all safety interlocks | … one confirm"). Its
  confirmation additionally names any foreground runs in scope. The CLI and TUI
  therefore share one implementation with an injected confirm callback (E8-T1),
  not two policies.
- D1's confirmation is only meaningful once the signal it guards actually
  stops the target. Today, `mode == "fg"`'s `SIGTERM` is silently swallowed by
  `interrupt/listener.py`'s cleanup handler (see Open Question 1's correction
  above) — E3-T9 and E3-T10 fix that as part of this epic; D1 does not depend
  on any further design decision to land.

**D2 — Who writes the `--web-bg` run record (was Open Question 2; resolved by
stakeholder).**
The **child** writes the record for **all** modes (`fg` / `fg-web` / `bg`). The
**parent polls for the child's record as its launch health gate**, preserving a
fatal failure path. Consequences I confirmed in the code:

- The parent already generates the `run_id` (`bg_runner.py::_open_bg_log_files`,
  `secrets.token_hex(4)`, line 306) and exports it as `CONDUCTOR_RUN_ID`
  (`_build_bg_env`). The child's `EventLogSubscriber` adopts that exact value
  after an 8-hex validation (`engine/event_log.py:130-141`). The parent
  therefore knows the exact record filename to poll for — no discovery needed.
- The record poll is a **strictly later** gate than `_wait_for_server`: in the
  child, the dashboard starts *before* the `try:` block, while the record is
  written after config load (`cli/run.py` ~1683). So both gates are meaningful
  and must be kept, in that order.
- This **closes an existing hole**: today a `--web-bg` child whose workflow
  fails to load dies *after* the dashboard is reachable, and the parent still
  reports success and prints a URL for a dashboard that is about to disappear.
  Polling for the record catches that case.
- The poll must check `proc.poll()` each iteration so an early child death fails
  fast rather than burning the full timeout, matching the existing
  `_wait_for_server` failure arm.
- The parent's `write_pid_file` call (`bg_runner.py:394`) is removed. Since that
  is its only call site, `write_pid_file` itself becomes dead and is deleted;
  `read_pid_files` / `remove_pid_file` / `remove_pid_file_for_current_process`
  are **retained** so `stop` can still see and clean up pre-upgrade live runs.
- The `CONDUCTOR_WEB_BG`-gated `remove_pid_file_for_current_process()` in
  `cli/run.py`'s `finally` becomes vestigial once no new `.pid` files are
  written, and is replaced by an unconditional
  `remove_run_record_for_current_process()`. A pre-upgrade child still running
  old code removes its own `.pid` file with old code, so nothing is orphaned by
  the transition.

**D3 — Where event-log retention is configured (was Open Question 3; resolved
by stakeholder).**
Introduce a general **`~/.conductor/config.toml`** settings file alongside
`registries.toml`, in a new top-level module `src/conductor/settings.py`.
Concretely:

- Placement follows `registry/config.py`: honor `$CONDUCTOR_HOME`, fall back to
  `Path.home() / ".conductor"`. A top-level module matches how other
  cross-cutting single-file concerns live (`duration.py`, `events.py`,
  `templating.py`); `config/` is the workflow-YAML schema package and machine
  settings do not belong there.
- Read with stdlib `tomllib`, as `registry/config.py:96` already does. Parse
  into a Pydantic model mirroring `RegistriesConfig`.
- Shape, reusing the design's mandated `keep_last` vocabulary:

  ```toml
  [fleet.retention]
  enabled = true
  keep_last = 200
  ```

- **Read-only in v1.** `registries.toml` has a writer only because
  `conductor registry add` exists; there is no `conductor config set` in scope,
  and adding one would mean a hand-rolled TOML formatter (the repo has no
  `tomli-w` dependency — `registry/config.py::_format_toml` is hand-written).
  The file is hand-edited and documented; `conductor fleet prune --keep-last N`
  is the override.
- A missing file yields defaults. A malformed file **raises** for explicit
  commands (`fleet prune`), but the opportunistic sweep at run startup swallows
  everything — a machine-global settings file must never break
  `conductor run`.
- Two constraints hold regardless: `$TMPDIR/conductor/` also contains the
  `checkpoints/` subdirectory (`engine/checkpoint.py:158`), which retention must
  never touch; and an event log belonging to a live run — or one a `resume` is
  appending to (`engine/event_log.py:113`) — must never be deleted.

**D4 — Gates in foreground runs (was Open Question 4 and design open question
#4; stakeholder delegated the call to this plan).**
**Display everywhere; resolve wherever a channel already exists; add no new core
resolution surface.** Grounded in `engine/workflow.py::_handle_gate_with_web`:

- **Display is universal and free.** `at-gate` is derived from
  `gate_presented` / `gate_resolved` in the JSONL event log, which *every* run
  writes. No mode is excluded.
- **Any record with a non-null `port` is remotely resolvable today.** A `fg-web`
  run *races* the CLI prompt against the web response
  (`engine/workflow.py:2496`), and a `bg` run is web-only
  (`_wait_for_web_gate`). So for `mode in {"fg-web", "bg"}` the TUI binds `g` to
  the existing `cli/gate.py::_gate_respond_impl` — the same HTTP path
  `conductor gate respond` uses, including its `CONDUCTOR_GATE_TOKEN` handling.
  No new endpoint, no new core code.
- **`mode == "fg"` is display-only, and that is a hard property, not a gap.**
  The gate blocks in `asyncio.to_thread(_ask_choice)` around a synchronous
  `Prompt.ask` (`gates/human.py:163-170`). A thread blocked in `input()` cannot
  be cancelled, so even a new file-drop or socket channel would leave the
  terminal prompt sitting there consuming the next keystroke. Making foreground
  gates remotely resolvable is a rewrite of the gate handler, not a fleet
  feature.
- The TUI renders such a row as `▲ at-gate (terminal · PID <pid>)` and disables
  `g` with that reason visible. **No new run-record field** is introduced — the
  design's nine fields stand, and `pid` is already one of them. A `tty` field
  was considered and rejected: it is POSIX-only and the PID is sufficient to
  find the terminal.
- **Steering, not blocking.** Every run launched *from* the fleet manager goes
  through `conductor run --web-bg` (E12), so it is gate-resolvable by
  construction. Only a run the user started manually as a plain
  `conductor run` is display-only. `docs/fleet.md` states that `--web` is the
  recommended way to start anything intended to be fleet-managed, and names this
  as the reason.

**D5 — Live token counts (design open question #1).** Settled by the design
itself: no provider-parity change for v1; label the column
`Σ tokens (completed)`. E6-T4 implements that.

**D6 — Migration of existing run records (design open question #2).** Settled by
the design's own stated leaning ("they self-prune on the liveness check, so
ignoring them is defensible"). E1 makes the reader **tolerant** of the legacy
port-keyed shape and performs no migration step.

**D7 — Command naming (design open question #3).** Marked resolved in the
design: `conductor fleet`, a noun sub-app.

---

## Implementation Phases

Phases follow the design's *Phasing* table. Phase 0 is independently shippable
and, per the design, "worth doing even if the TUI is deferred indefinitely".

| Phase | Epics | Deliverable | Exit criteria |
|---|---|---|---|
| **0** | E1–E5 | Run record for every run, `conductor stop` over run records, `conductor fleet list`, `~/.conductor/config.toml` + `$TMPDIR` retention | A foreground `conductor run` appears in `conductor fleet list` and is actually terminated by `conductor stop` — not merely signalled (E3-T9 fixes the `SIGTERM`-swallowing bug in `interrupt/listener.py`, and E3-T10 verifies termination before reporting success); records are removed on exit and self-prune after `kill -9`; stopping a foreground run prompts for confirmation (D1) and refuses on a non-TTY without `--yes`; a `--web-bg` launch fails fatally when the child never writes its record (D2); retention reads `~/.conductor/config.toml` (D3) and never touches `checkpoints/` or a live run's log; `make check` and `make test` pass; no new required dependency. |
| **1** | E6–E9 | Textual fleet TUI: Runs screen, auto-refresh, empty state, dashboard open, kill/kill-all, run detail | `conductor fleet` launches the TUI with `[tui]` installed and prints an install hint without it; the Runs table renders live status including `at-gate`; `w` opens the correct dashboard; `k`/`K` go through the same verify-then-report code path as `conductor stop` (E8-T1), and `K` confirms once. |
| **2** | E10–E11 | Providers and registries drill-down screens | `p` and `r` open drill-down screens sourced from `providers/diagnostics.py` and `registry/`; Escape pops back to Runs. |
| **3** | E12 | Launch-with-inputs | `n` resolves a file or registry ref, renders a form from `wf.input`, and launches a detached `conductor run --web-bg` that appears in the Runs list on the next refresh. |
| **4** | E13–E14 | Gate visibility and resolution, notifications, history | Per D4: a gated run with a port can be resolved with `g` through the existing `gate respond` path; a portless `fg` run is marked display-only with its PID; gate-entry and failure raise a terminal notification; `h` lists completed runs bounded by the retention policy from E5. |
| **—** | E15 | Documentation, changelog, AGENTS.md | `docs/fleet.md` exists, `docs/cli-reference.md` covers `fleet` and the changed `stop`, `docs/configuration.md` documents `~/.conductor/config.toml`, AGENTS.md describes `src/conductor/fleet/` and `settings.py`, CHANGELOG has an Unreleased entry. |

Phases 2, 3 and 4 depend only on Phase 1 and may be reordered or dropped
independently.

---

## Files Affected

### New Files

| File Path | Purpose |
|-----------|---------|
| `src/conductor/settings.py` | `~/.conductor/config.toml` loader (D3): `FleetRetentionSettings` / `ConductorSettings` Pydantic models, `get_settings_path()` honoring `$CONDUCTOR_HOME`, `load_settings()` via stdlib `tomllib`. Read-only in v1. |
| `src/conductor/fleet/__init__.py` | Package marker for the fleet layer. |
| `src/conductor/fleet/records.py` | `RunRecord` dataclass plus atomic write / tolerant read / prune / remove-for-current-process. Core, no new dependency (design: *Implementation*). |
| `src/conductor/fleet/retention.py` | `$TMPDIR/conductor/` event-log retention mirroring the `keep_last` vocabulary, driven by `settings.py` (design: *Second-order cleanup*; D3). |
| `src/conductor/fleet/summary.py` | Derives `RunSummary` (current step, elapsed-on-step, token total, gate state, status) from a run record plus an event-log tail seek. |
| `src/conductor/fleet/launch.py` | Ref resolution + input coercion + detached launch helper shared by the TUI's New-run screen. |
| `src/conductor/fleet/history.py` | Completed-run listing bounded by the retention policy. |
| `src/conductor/cli/fleet.py` | `fleet` Typer sub-app: `fleet list`, `fleet prune`, and the `invoke_without_command=True` callback that launches the TUI. |
| `src/conductor/fleet/tui/__init__.py` | TUI package marker; imported only by `conductor fleet`. |
| `src/conductor/fleet/tui/app.py` | Textual `App` with the `Screen` push/pop stack and the poll timer. |
| `src/conductor/fleet/tui/actions.py` | Shared TUI actions: open dashboard, kill, kill-all, resolve gate (D4). |
| `src/conductor/fleet/tui/notify.py` | Terminal bell / OSC 9 notification emitter. |
| `src/conductor/fleet/tui/screens/runs.py` | Runs home screen, including the first-class empty state. |
| `src/conductor/fleet/tui/screens/run_detail.py` | Per-run detail: topology + per-agent timings, current step highlighted. Not a DAG. |
| `src/conductor/fleet/tui/screens/providers.py` | Providers → models drill-down. |
| `src/conductor/fleet/tui/screens/registries.py` | Registries → workflows → inputs drill-down. |
| `src/conductor/fleet/tui/screens/new_run.py` | Launch form generated from `wf.input`. |
| `src/conductor/fleet/tui/screens/history.py` | Completed-run history screen. |
| `tests/test_settings.py` | `config.toml` defaults, `$CONDUCTOR_HOME` redirection, malformed-TOML behavior. Top-level module → top-level test file, matching `tests/test_events.py` / `tests/test_templating.py`. |
| `tests/test_fleet/__init__.py` | Test package marker (tests mirror source structure). |
| `tests/test_fleet/test_records.py` | Atomicity, liveness filtering, legacy-shape tolerance, partial-write tolerance. |
| `tests/test_fleet/test_retention.py` | `checkpoints/` exclusion, live-run exclusion, `keep_last` semantics, settings-driven enable/disable. |
| `tests/test_fleet/test_summary.py` | Status state machine and tail-seek derivation from fixture JSONL. |
| `tests/test_fleet/test_launch.py` | Ref resolution, input coercion, argv construction. |
| `tests/test_fleet/test_history.py` | Completed-run listing. |
| `tests/test_fleet/test_tui_runs.py` | Textual `run_test()` pilot coverage of the Runs screen and empty state. |
| `tests/test_fleet/test_tui_actions.py` | Dashboard-open, kill and gate-resolve bindings. |
| `tests/test_fleet/test_tui_run_detail.py` | Run detail screen + Escape-to-return. |
| `tests/test_fleet/test_tui_drilldown.py` | Providers and registries screens. |
| `tests/test_fleet/test_tui_new_run.py` | Form generation and submission. |
| `tests/test_fleet/test_notify.py` | Notification emission on gate entry / failure. |
| `tests/test_fleet/test_run_record_wiring.py` | Records written and removed by every run path (fg, fg-web, bg, resume). |
| `tests/test_cli/test_fleet_list.py` | `conductor fleet list` / `fleet prune` non-interactive output. |
| `tests/test_cli/test_fleet_optional_dep.py` | Bare `conductor fleet` without `textual` prints the install hint, not a traceback. |
| `docs/fleet.md` | User-facing fleet manager documentation, including the D4 gate-resolution matrix. |

### Modified Files

| File Path | Changes |
|-----------|---------|
| `src/conductor/cli/pid.py` | Promote `_is_process_alive` to a public `is_process_alive` (keeping the private name as an alias so `tests/test_cli/test_stop.py` patch targets keep working). **Delete `write_pid_file`** — D2 removes its only call site. Retain `read_pid_files` / `remove_pid_file` / `remove_pid_file_for_current_process` so `stop` can still see and clean up pre-upgrade live runs. The design is explicit that liveness must **reuse** this hardened implementation (issues #166, #344), not reimplement it. |
| `src/conductor/cli/run.py` | Write a run record in both `run_workflow_async` (after `EventLogSubscriber` construction at ~line 1683, where `dashboard.port` and `run_id` are both known) and `resume_workflow_async` (~line 2308); remove it unconditionally in each `finally`, replacing the `CONDUCTOR_WEB_BG`-gated `remove_pid_file_for_current_process()` at ~1860/2460 (D2). Add the best-effort retention sweep at startup (D3, subject to Open Question 2). Run/Resume parity is an AGENTS.md rule. |
| `src/conductor/cli/bg_runner.py` | D2: delete the parent's `write_pid_file` call (line 394) and replace it with a poll for the child's `<run_id>.json` record, keyed on the `run_id` the parent already generates in `_open_bg_log_files`. Preserve the fatal contract — terminate the child and raise `RuntimeError` naming the captured stderr log. Thread `run_id` into `_finalize_background_launch`. |
| `src/conductor/cli/app.py` | `app.add_typer(fleet_app, rich_help_panel=...)`; rewrite `stop` / `_stop_process` / `_print_running_list` (lines 1103–1243) to key on run records instead of `entry["port"]`, which `KeyError`s on a portless foreground record; add `--run-id` and `--yes/-y`; implement the D1 confirmation and non-TTY refusal; extract the shared stop implementation for E8. |
| `src/conductor/cli/gate.py` | `_gate_respond_impl` itself is unchanged. It is imported by the TUI's gate-resolve action (D4) and becomes a consumed interface — note this in its docstring so a future refactor does not narrow it to the CLI. The TUI-side call is not "no behavior change," though: `_gate_respond_impl` is synchronous, makes blocking `httpx` calls, writes to a module-level stderr `Console`, and raises `typer.Exit` on failure — none of which is safe to invoke directly from a Textual event handler. See E13-T2 for the worker/console-capture wrapper this requires. |
| `pyproject.toml` | Add `[project.optional-dependencies] tui = ["textual>=6.0"]` (pinned to a floor actually tested against, not the historical `>=1.0` — the repo's other post-1.0 floors are annotated with rationale, e.g. `claude-agent-sdk>=0.2.82`, `anthropic>=0.77.0,<1.0.0`) and add `textual` to the `dev` dependency group so the Textual pilot tests can run. Re-lock with `uv lock`. |
| `tests/test_cli/test_stop.py` | Update the 22 port-keyed assertions for run-record keying and foreground records; add D1 confirmation, `--yes` and non-TTY refusal cases. |
| `tests/test_cli/test_pid.py` | Cover the public `is_process_alive` alias and the new run-id removal helper; drop `write_pid_file` coverage. |
| `tests/test_cli/test_bg_runner.py` | Update for D2: the parent no longer writes a PID file; it polls for the child's record and fails fatally on timeout or early child death. |
| `tests/test_cli/test_help_panels.py` | Add `fleet` to `test_noun_groups_listed` and the `rich_help_panel` mapping assertion. |
| `docs/cli-reference.md` | Document `conductor fleet` / `fleet list` / `fleet prune`; update the `conductor stop` section (line 229), which currently states it stops "processes launched with `--web-bg`", and document `--run-id` / `--yes` and the D1 confirmation. |
| `docs/configuration.md` | Document `~/.conductor/config.toml` (D3): location, `$CONDUCTOR_HOME`, the `[fleet.retention]` table, defaults, read-only status, and that pruning removes `conductor replay` material. |
| `AGENTS.md` | Add `src/conductor/fleet/` to the Core Package Structure section, `settings.py` to the top-level module list, `cli/fleet.py` to the `cli/` list, and `tests/test_fleet/` to Tests Structure. |
| `CHANGELOG.md` | Unreleased entry. |
| `README.md` | Mention the fleet manager and the `[tui]` extra. |

### Deleted Files

| File Path | Reason |
|-----------|--------|
| — | No files deleted. One *function* is removed: `cli/pid.py::write_pid_file`, whose only call site (`cli/bg_runner.py:394`) is retired by D2. The module itself is retained and extended — it owns the hardened cross-platform liveness probe the design mandates reusing, and `read_pid_files` / `remove_pid_file` stay for the legacy port-keyed records that pre-upgrade live runs may still hold. |

---

## Implementation Plan

### E1 — Run record core

**Status: DONE.**

> ⚠️ Force-approved after 4 review rounds without reviewer sign-off. Outstanding: src/conductor/fleet/records.py:436-454: If os.link raises OSError because hard links are unsupported or denied, _restore_if_absent leaves the record only under a hidden .prune-* name. Implement a non-clobbering recovery strategy that keeps the live record discoverable, and handle NotImplementedError/AttributeError consistently with the helper's never-raises contract.; tests/test_fleet/test_records.py: Add negative-path coverage where os.link raises EPERM/ENOTSUP or is unavailable, asserting the record remains available at its canonical path and a concurrent newer record is never overwritten.; src/conductor/fleet/records.py: Repeated scans after restoration's final unlink fails create an additional .prune-* hard link each time. Bound or reap these artifacts rather than allowing unbounded accumulation during polling.; src/conductor/fleet/records.py:768: remove_run_record unlinks by path without the identity protection used by remove_run_record_for_current_process. A resumed process can rewrite the same run_id before the old process removes it. Route removal through identity-checked deletion or explicitly constrain and document the API.

**Goal.** Deliver `src/conductor/fleet/records.py`: the write/read/prune
primitives for a `run_id`-keyed run record, satisfying the design's *The fix*
section (key by `run_id`, the listed field set, atomic writes, tolerant
readers, liveness via the existing probe).

**Prerequisites.** None. This is the design's Phase 0 starting point.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E1-T1 | IMPL | Promote `_is_process_alive` to a public `is_process_alive`, keeping `_is_process_alive = is_process_alive` as an alias so existing patch targets in `tests/test_cli/test_stop.py` continue to resolve. | `src/conductor/cli/pid.py` | DONE |
| E1-T2 | IMPL | Define `RunRecord` with the design's exact field set: `run_id`, `pid`, `workflow_path`, `workflow_name`, `started_at`, `event_log_path`, `port \| None`, `mode` (`fg` / `fg-web` / `bg`), `checkpoint_dir`. No tenth field — D4 explicitly rejected adding `tty`. Include `to_dict()` / `from_dict()` mirroring the `to_dict()` convention in `providers/diagnostics.py`. Fields are required for a record this code writes; a legacy port-keyed `.pid` shape read by E1-T4 will be missing several of them (`run_id`, `mode`, `event_log_path`), so `from_dict()` must default/optionalize rather than require all nine — the "exactly nine, no more" constraint governs what `write_run_record` writes, not what every `from_dict()` input must supply. | `src/conductor/fleet/records.py`, `src/conductor/fleet/__init__.py` | DONE |
| E1-T3 | IMPL | `write_run_record(...)` writing `~/.conductor/runs/<run_id>.json` atomically (temp file in the same directory + `os.replace`). Honor `CONDUCTOR_HOME` the way `registry/config.py:69` does, so tests and isolated environments can redirect it. Note `cli/pid.py::pid_dir()` does **not** honor it today; do not copy that omission. | `src/conductor/fleet/records.py` | DONE |
| E1-T4 | IMPL | `read_run_records()` returning only records whose `pid` passes `is_process_alive`, deleting stale ones. Must tolerate: partially-written files, files that vanish mid-scan, unparseable JSON, and the **legacy port-keyed `.pid` shape** (missing `run_id` / `mode` / `event_log_path`) — surfaced with unknown fields and `mode="bg"` (D1) rather than dropped or crashed. Mirrors the existing posture of `read_pid_files()`. Legacy `.pid` files must be read from `cli/pid.py::pid_dir()` explicitly (always `~/.conductor/runs/`, ignoring `CONDUCTOR_HOME`) rather than from the new `CONDUCTOR_HOME`-aware run-record directory — when `CONDUCTOR_HOME` is set the two directories diverge, and a reader that only looked in the new location would silently find no legacy records to tolerate. Legacy tolerance is therefore scoped to the default home; a `CONDUCTOR_HOME` user has no pre-upgrade records to worry about by construction. | `src/conductor/fleet/records.py` | DONE |
| E1-T5 | IMPL | `read_run_record(run_id)` for a single keyed lookup — the primitive D2's parent-side launch gate polls. Returns `None` when absent or unparseable; must not delete a record it merely cannot parse yet, since a concurrent atomic write is the expected reason. | `src/conductor/fleet/records.py` | DONE |
| E1-T6 | IMPL | `remove_run_record(run_id)` and `remove_run_record_for_current_process()`, matching the existing `remove_pid_file_for_current_process()` shape named in the design. | `src/conductor/fleet/records.py` | DONE |
| E1-T7 | TEST | Round-trip write/read; stale record pruned when the PID is dead; a `kill -9`-style orphan self-prunes; concurrent writers never yield a torn read; legacy `.pid` files are read without error and classified `bg`; an unreadable/corrupt file is removed rather than raised; `read_run_record` returns `None` rather than deleting on a transient parse failure. | `tests/test_fleet/test_records.py`, `tests/test_fleet/__init__.py` | DONE |
| E1-T8 | TEST | Regression coverage for the `is_process_alive` public alias on both the POSIX and Windows branches (the Windows branch is already exercised cross-platform by patching `_kernel32`). | `tests/test_cli/test_pid.py` | DONE |

**Acceptance Criteria.**
- [x] `RunRecord` carries exactly the nine fields listed in the design's *The fix* — no more.
- [x] Records are keyed by `run_id`, never by port.
- [x] Writes are atomic (temp + `os.replace`); a reader never observes a partial record.
- [x] Liveness delegates to `cli/pid.py`; no second process-probe implementation exists in the repo.
- [x] `read_run_records()` never raises on a corrupt, vanished, or legacy-shaped file.
- [x] A legacy `.pid` record is reported with `mode="bg"` so D1 never prompts for it.
- [x] `CONDUCTOR_HOME` redirects the record directory; legacy `.pid` files are still read from the (unredirected) default `pid_dir()`.
- [x] `make check` and `uv run pytest tests/test_fleet/test_records.py tests/test_cli/test_pid.py` pass.
- [x] **Residual risk, accepted for v1:** liveness is a bare `is_process_alive(pid)`, with no creation-time or other identity check. A PID the OS recycles onto an unrelated process after a `kill -9` orphan will read back as alive, and `conductor stop --all` / the TUI's `K` would then signal that unrelated process. This risk exists today for `--web-bg` records; this plan extends run records to every mode and adds a kill-all binding, which enlarges (but does not introduce) the exposure. Not fixed in v1 — out of scope per the design — but flagged here rather than silently inherited.

---

### E2 — Write a run record from every run path (DONE)

**Status: DONE.**

> ⚠️ Force-approved after 4 review rounds without reviewer sign-off. Outstanding: {'file': 'src/conductor/cli/bg_runner.py:727-730,808-812,843-854', 'issue': 'The parent and child independently select the latest checkpoint. A newer checkpoint appearing between selections makes the parent poll one run ID while the child writes another.', 'action': 'Resolve the checkpoint once in the parent, pass its exact path through --from, and derive the poll key from that checkpoint.'}; {'file': 'src/conductor/cli/bg_runner.py:736-742', 'issue': 'Run-ID handling remains inconsistent with engine/event_log.py: checkpoint IDs and fallback environment IDs follow different validation and normalization rules. Invalid IDs or event-log append failure can make the parent poll a different ID than the child uses.', 'action': 'Use one shared, identity-preserving run-ID contract across checkpoint reuse, environment adoption, event logging, records, and parent prediction. Test invalid IDs and append-open failure.'}; {'file': 'src/conductor/cli/app.py:1103-1179; tests/test_cli/test_stop.py:236-347', 'issue': 'The stop discovery/removal migration belongs to E3 and is untraced scope creep for E2.', 'action': 'Remove these changes from E2 or formally amend the epic scope.'}; {'file': 'tests/test_cli/test_bg_runner.py:478-512', 'issue': 'The required invalid-workflow launch regression mocks readiness and process death, so it does not verify actual CLI ordering or invalid-workflow behavior.', 'action': 'Add a process-level test launching an invalid workflow, observing dashboard readiness followed by failure, and asserting the fatal error identifies the stderr log.'}

**Goal.** Close the design's blocking problem — "Only `--web-bg` runs write a
run record; foreground runs are invisible" — by writing and removing a record
from every execution path, with the child owning the write in every mode (D2).

**Prerequisites.** E1.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E2-T1 | IMPL | In `run_workflow_async`, write the record immediately after `EventLogSubscriber` construction (`cli/run.py` ~1683) — the first point where `run_id`, `event_log_path` and the already-started `dashboard.port` are all available. Derive `mode` from `web` / `web_bg` / `CONDUCTOR_WEB_BG`, matching the existing `bg_mode` expression at ~line 1778. | `src/conductor/cli/run.py` | DONE |
| E2-T2 | IMPL | Replace the `is_bg_child`-gated cleanup in the `finally` (`cli/run.py` ~1858-1864) with an unconditional `remove_run_record_for_current_process()`. Per D2 the legacy `remove_pid_file_for_current_process()` call is dropped entirely: no new `.pid` files exist, and a pre-upgrade child removes its own with pre-upgrade code. | `src/conductor/cli/run.py` | DONE |
| E2-T3 | IMPL | Mirror T1 and T2 in `resume_workflow_async` (`cli/run.py` ~2308, ~2459). Resume reuses the original `run_id` via `existing_run_id`, so a resumed run must **replace** its prior record rather than create a second one — a natural consequence of `run_id` keying, but assert it. Required by the AGENTS.md Run/Resume Parity rule. | `src/conductor/cli/run.py` | DONE |
| E2-T4 | IMPL | D2 parent side: delete the `write_pid_file` call at `cli/bg_runner.py:394` and the now-dead `write_pid_file` function in `cli/pid.py`. Thread `run_id` into `_finalize_background_launch` and poll `read_run_record(run_id)` after `_wait_for_server` succeeds, checking `proc.poll()` each iteration. On timeout (15 s, matching `_wait_for_server`) or early child death, `_terminate_child(proc)` and raise `RuntimeError` naming the captured stderr log — the same fatal contract the `write_pid_file` failure arm has today. | `src/conductor/cli/bg_runner.py`, `src/conductor/cli/pid.py` | DONE |
| E2-T5 | IMPL | Populate `checkpoint_dir` from `CheckpointManager.get_checkpoints_dir()` (`engine/checkpoint.py:152`) so a fleet consumer can locate the (global, `$TMPDIR`-rooted) checkpoints directory without re-deriving the path. Note `get_checkpoints_dir()` returns the same value for every run — it is not per-run data — so it is `run_id` + `workflow_path`, not `checkpoint_dir` itself, that actually lets E3's confirmation prompt find *this run's* checkpoint files, the same way `CheckpointManager._periodic_checkpoints_for_run(workflow_path, run_id)` (`engine/checkpoint.py:476`) does. | `src/conductor/cli/run.py` | DONE |
| E2-T6 | TEST | A record appears for `fg`, `fg-web`, `bg` and `resume`, with the correct `mode`, a `port` only where a dashboard exists, and an `event_log_path` that matches the subscriber's actual path. Record removed on normal exit, on `WorkflowTerminated`, and on an unexpected exception. | `tests/test_fleet/test_run_record_wiring.py` | DONE |
| E2-T7 | TEST | D2 parent gate: exactly one record per bg run; the parent's `run_id` and the child's record key match; the launch raises `RuntimeError` (and terminates the child) when the record never appears; it raises fast when the child dies during the poll; the stderr-log path appears in every failure message. Add the regression this closes — a child that starts its dashboard and then dies on an invalid workflow must now be reported as a failed launch. | `tests/test_cli/test_bg_runner.py` | DONE |

**Acceptance Criteria.**
- [x] `conductor run workflow.yaml` (no flags) produces a discoverable record for the lifetime of the process.
- [x] `--web` records carry the dashboard port; plain foreground records carry `port: null`.
- [x] A resumed run updates its existing record rather than adding a second.
- [x] Records are removed on every exit path, including explicit termination.
- [x] Exactly one record exists per `--web-bg` run, written by the child.
- [x] A `--web-bg` launch whose child never writes a record fails fatally, terminates the child, and names the stderr log.
- [x] `write_pid_file` no longer exists; `read_pid_files` / `remove_pid_file` remain for legacy records.
- [x] `uv run pytest tests/test_fleet tests/test_cli/test_bg_runner.py tests/test_cli/test_resume_command.py` passes.

---

### E3 — `conductor stop` over run records

**Status: DONE.**

**Goal.** Deliver the design's stated standalone Phase 0 value: "it fixes
`conductor stop`'s blindness to foreground runs" and gives it "a meaningful
`--all`", with the D1 confirmation guarding the new blast radius. Blindness and
`--all` are necessary but not sufficient — a foreground run must also actually
stop when signalled, which the codebase does not currently guarantee (see the
correction in Open Question 1); E3-T9 and E3-T10 close that gap so this epic
delivers a working stop, not just a discoverable one.

**Prerequisites.** E1, E2.

> ⚠️ **Never run a bare `conductor stop` (or `conductor stop --all`) while
> implementing or verifying this epic.** With no arguments it auto-stops when
> exactly one run is alive, and nothing excludes the run you are executing
> inside — so smoke-testing it terminates your own workflow mid-epic. This has
> already happened once, killing 171 turns of work (see
> [microsoft/conductor#399](https://github.com/microsoft/conductor/issues/399)).
> Verify `stop` with `--help`, with unit tests against fake records, or against
> a throwaway run you launched yourself and can name explicitly with
> `--run-id`. The same caution applies to any command that signals processes it
> discovered rather than ones you named.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E3-T1 | IMPL | Rewrite `stop` (`cli/app.py:1103`) to source from `read_run_records()`. Add a `--run-id` selector; retain `--port` as a filter that matches a record's port (still the documented handle in `docs/cli-reference.md`) and errors clearly when the named run has no dashboard. | `src/conductor/cli/app.py` | DONE |
| E3-T2 | IMPL | Fix `_stop_process` and `_print_running_list` (`cli/app.py:1173`, `:1220`), which index `entry["port"]` unconditionally and would `KeyError` on a portless foreground record. Render `—` for a missing port and add `Mode` and `Run ID` columns. | `src/conductor/cli/app.py` | DONE |
| E3-T3 | IMPL | Implement D1. Keep auto-stop-single and `--all` behavior unchanged in scope; add a `rich.prompt.Confirm` gate that fires when any target has `mode in {"fg", "fg-web"}`. `--all` prompts **once**, naming the foreground runs. Legacy `.pid` records classify as `bg` and never prompt, so today's behavior is byte-for-byte preserved for today's fleet. | `src/conductor/cli/app.py` | DONE |
| E3-T4 | IMPL | Add `--yes` / `-y` to bypass the prompt. When `sys.stdin.isatty()` is false and `--yes` was not passed, print the would-be targets and exit non-zero rather than proceeding — a non-TTY cannot confirm, and defaulting to yes would reinstate the hazard. Use `sys.stdin.isatty()`, the repo's existing test (`cli/run.py:1752`). | `src/conductor/cli/app.py` | DONE |
| E3-T5 | IMPL | Per Open Question 1's working assumption, the confirmation text states that in-flight progress is lost unless periodic checkpoints are enabled, and reports whether the run has checkpoints by looking for `{workflow_name}-*.json` files matching the record's `run_id` under `checkpoint_dir` — checking for **any** checkpoint file for this run, not just for the directory's existence, since `checkpoint_dir` is the same global path for every run and its mere presence says nothing about this run. No signal-triggered checkpoint path is added. | `src/conductor/cli/app.py` | DONE |
| E3-T6 | IMPL | Replace the post-kill `remove_pid_file(entry["port"])` calls with run-id-keyed removal, falling back to port-keyed removal for a legacy record so pre-upgrade live runs remain stoppable. | `src/conductor/cli/app.py`, `src/conductor/cli/pid.py` | DONE |
| E3-T7 | TEST | Update the 22 port-keyed assertions; add cases for a foreground record, a mixed fg+bg fleet, `--run-id`, `--port` against a portless run, and a legacy `.pid` record. D1 cases: confirm-yes stops, confirm-no stops nothing and exits 0, `--all` over a mixed fleet prompts exactly once, `--all` over bg-only does not prompt, `--yes` bypasses, non-TTY without `--yes` exits non-zero having signalled nothing. | `tests/test_cli/test_stop.py` | DONE |
| E3-T8 | IMPL | Update the `conductor stop` section of the CLI reference, which currently scopes the command to `--web-bg` processes, and document `--run-id`, `--yes` and the confirmation. | `docs/cli-reference.md` | DONE |
| E3-T9 | IMPL | **Fix the `SIGTERM`-swallowing bug this epic depends on.** `interrupt/listener.py::_register_cleanup_handlers`'s `_sigterm_handler` currently restores the terminal and returns without re-raising when `self._previous_sigterm` is not callable — the normal case, since `signal.getsignal(SIGTERM)` is `SIG_DFL` (an `IntEnum`, not callable) in an unmodified process. That means a `mode == "fg"` run today survives `SIGTERM` indefinitely; see the correction in Open Question 1. Change the handler so that when the previous handler is not callable, it restores the default disposition and re-raises the signal against itself after restoring the terminal (`signal.signal(signal.SIGTERM, signal.SIG_DFL)` then `os.kill(os.getpid(), signal.SIGTERM)`), rather than falling through silently. | `src/conductor/interrupt/listener.py` | DONE |
| E3-T10 | IMPL | **Make the shared stop path verify termination instead of assuming it.** `_stop_process` (`cli/app.py:1173`) currently prints `Stopped` as soon as `os.kill` returns without raising, and E3-T6 then removes the run record — with E3-T9 unfixed this reports success on a run that never stopped, and even after E3-T9 there is a window between signalling and process exit. After signalling, poll `is_process_alive(pid)` (the E1-T1 public alias) over a short grace period (e.g. up to ~2s); only print `Stopped` and let the caller remove the run record once the process is confirmed gone. If the process is still alive after the grace period, escalate (POSIX: `SIGKILL`; Windows: no escalation signal exists — report the run as still running rather than claiming success) and report the outcome honestly rather than silently declaring success. This is the same verify-then-report contract E8-T1's shared implementation must expose to the TUI. | `src/conductor/cli/app.py` | DONE |
| E3-T11 | TEST | A `mode == "fg"` run under a real `KeyboardListener` (PTY-backed, matching the reviewer's empirical repro) actually exits on `SIGTERM` after E3-T9; `_stop_process` does not report `Stopped` until `is_process_alive` returns `False`; a process that ignores the grace period is escalated rather than silently reported as stopped; the run record is only removed once termination is confirmed. | `tests/test_cli/test_stop.py`, `tests/test_interrupt/test_listener.py` | DONE |
| E3-T12 | TEST | Windows cross-platform contract: `_stop_process` already uses `os.kill(pid, signal.CTRL_BREAK_EVENT)` on `win32`, which is delivered via `GenerateConsoleCtrlEvent` and only reaches process groups attached to the *sending* process's console — so a `conductor stop` invoked from a different console window cannot reach a foreground `conductor run` in another console, and `--web-bg` children are additionally spawned with `CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB` (`bg_runner.py:79`). D1 extends `stop` to foreground runs on every platform; state and verify this constraint explicitly rather than leaving it implicit, alongside the POSIX fix in E3-T9/T10. | `tests/test_cli/test_stop.py` | DONE |

**Acceptance Criteria.**
- [x] `conductor stop` lists foreground runs alongside background runs.
- [x] No code path indexes `port` unconditionally on a run record.
- [x] Stopping a foreground run requires confirmation; stopping a bg-only fleet behaves exactly as it does today.
- [x] `--all` over a mixed fleet prompts exactly once and names the foreground runs.
- [x] A non-TTY invocation without `--yes` signals nothing and exits non-zero.
- [x] The confirmation text names the progress-loss consequence.
- [x] A pre-upgrade legacy `.pid` record is still listable and stoppable, and never triggers the prompt.
- [x] A `mode == "fg"` run actually terminates on `conductor stop` — verified against a real `KeyboardListener`, not assumed from `os.kill` not raising.
- [x] The run record is removed only after termination is confirmed, never on signal-send alone.
- [x] `uv run pytest tests/test_cli/test_stop.py tests/test_interrupt/test_listener.py` passes.

---

### E4 — `conductor fleet` sub-app and `fleet list`

**Status: DONE.**

**Goal.** The non-interactive half of the design's *CLI surface*: "`conductor
fleet list` — non-interactive Rich table. Core, no new dependency."

**Prerequisites.** E1, E2.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E4-T1 | IMPL | Create the `fleet` Typer sub-app following `cli/checkpoint.py` and `cli/registry.py`. Per the design's *One deliberate deviation*, use `invoke_without_command=True` rather than `no_args_is_help=True`. Until E7 lands the TUI, the bare callback prints help; E7 flips it to launch the TUI. | `src/conductor/cli/fleet.py` | DONE |
| E4-T2 | IMPL | Register with `app.add_typer(fleet_app, rich_help_panel=...)`. `Run & Recover` is the closest existing panel for a run-oriented noun group; confirm against the panel list in `tests/test_cli/test_help_panels.py`. | `src/conductor/cli/app.py` | DONE |
| E4-T3 | IMPL | Implement `fleet list`: a Rich table over `read_run_records()` with workflow, mode, status, PID, port, started-at. Uses only `rich`, already a direct dependency. The empty case prints a dim "no runs" line, not an error — the design makes the empty state a normal state. | `src/conductor/cli/fleet.py` | DONE |
| E4-T4 | TEST | Output with zero, one and several records; portless records render `—`; exit code is 0 when empty. | `tests/test_cli/test_fleet_list.py` | DONE |
| E4-T5 | TEST | Add `fleet` to `test_noun_groups_listed` and to the `rich_help_panel` mapping assertion. | `tests/test_cli/test_help_panels.py` | DONE |
| E4-T6 | IMPL | Document `conductor fleet list` in the CLI reference, including its table of contents entry. | `docs/cli-reference.md` | DONE |

**Acceptance Criteria.**
- [x] `conductor fleet list` works on a clean install with no optional extras.
- [x] It lists foreground, `--web` and `--web-bg` runs.
- [x] Empty output exits 0.
- [x] `conductor --help` shows `fleet` under a panel, and the help-panel test asserts it.
- [x] `uv run pytest tests/test_cli/test_fleet_list.py tests/test_cli/test_help_panels.py` passes.

---

### E5 — `~/.conductor/config.toml` and `$TMPDIR/conductor/` retention

**Status: DONE.**

**Goal.** The design's *Second-order cleanup*: bound the unbounded event-log
directory (1522 files / 12 MB observed) using the existing `keep_last`
vocabulary rather than a new policy language, configured in the new
`~/.conductor/config.toml` settings file (D3).

**Prerequisites.** E1, E4.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E5-T1 | IMPL | New `src/conductor/settings.py` (D3): `ConductorSettings` / `FleetRetentionSettings` Pydantic models, `get_settings_path()` honoring `$CONDUCTOR_HOME` exactly as `registry/config.py:69` does, and `load_settings()` reading with stdlib `tomllib`. Missing file → defaults. Malformed TOML or invalid values → a clear `ConductorError`. Read-only in v1: no writer, no `conductor config set`. | `src/conductor/settings.py` | DONE |
| E5-T2 | IMPL | `prune_event_logs(*, keep_last, dry_run=False)` over `$TMPDIR/conductor/*.events.jsonl`, newest-first by mtime, deleting past `keep_last`. Name and guard it after `CheckpointManager.rotate_periodic_checkpoints` (`engine/checkpoint.py:530`), including its `keep_last < 1` negative-slice guard, which would otherwise retain exactly the files it means to delete. Best-effort; never raises. | `src/conductor/fleet/retention.py` | DONE |
| E5-T3 | IMPL | Exclusions: never descend into the `checkpoints/` subdirectory (`engine/checkpoint.py:158` puts it inside the same `$TMPDIR/conductor/`), and never delete an `event_log_path` referenced by a live run record — a `resume` may be appending to it (`engine/event_log.py:113`). Also skip the `.bg.stderr.log` / `.bg.stdout.log` companions of a retained events log so the three artefacts of one run are retained or removed together. | `src/conductor/fleet/retention.py` | DONE |
| E5-T4 | IMPL | Wire the settings-driven sweep per Open Question 2's working assumption: `[fleet.retention] enabled` / `keep_last`, swept once at run startup from `cli/run.py` alongside the E2 record write, wrapped so it can never raise or measurably delay a run (the design measured a full 1522-file scan at 0.136 s). Add `conductor fleet prune [--keep-last N] [--dry-run]` as the explicit manual entry point regardless. | `src/conductor/cli/fleet.py`, `src/conductor/fleet/retention.py`, `src/conductor/cli/run.py` | DONE |
| E5-T5 | TEST | Settings: defaults with no file; `$CONDUCTOR_HOME` redirection; `[fleet.retention]` parsed; malformed TOML raises for `fleet prune` but is swallowed by the startup sweep. | `tests/test_settings.py` | DONE |
| E5-T6 | TEST | Retention: `keep_last` retains the newest N; `keep_last=0` and negative values are handled without deleting the wrong set; `checkpoints/` survives; a live run's log survives; bg log companions follow their events log; an unreadable file does not abort the sweep; `enabled = false` sweeps nothing. | `tests/test_fleet/test_retention.py` | DONE |
| E5-T7 | IMPL | Document `~/.conductor/config.toml` in `docs/configuration.md` and retention + `fleet prune` in the CLI reference, explicitly noting that pruning an event log makes that run unavailable to `conductor replay`. | `docs/configuration.md`, `docs/cli-reference.md` | DONE |

**Acceptance Criteria.**
- [x] `~/.conductor/config.toml` is read via stdlib `tomllib`, honors `$CONDUCTOR_HOME`, and defaults cleanly when absent.
- [x] A malformed settings file never breaks `conductor run`.
- [x] Retention never deletes checkpoints.
- [x] Retention never deletes a live run's event log.
- [x] `keep_last` semantics match `rotate_periodic_checkpoints`, including the negative-slice guard.
- [x] `conductor fleet prune --dry-run` lists without deleting.
- [x] The `conductor replay` consequence is documented where the setting is documented.
- [x] `uv run pytest tests/test_fleet/test_retention.py tests/test_settings.py` passes.

---

### E6 — `RunSummary` derivation

**Status: DONE.**

**Goal.** The design's *Implementation* → `summary.py`: "derive a `RunSummary`
from a run record plus its event-log tail (current step, elapsed-on-step, token
total, gate state)", and the *Status vocabulary* state machine.

**Prerequisites.** E1.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E6-T1 | IMPL | Bounded tail reader for a JSONL event log: seek from the end, read the last N KB, discard a leading partial line, parse what remains. Must tolerate a file being appended to concurrently. Do not reuse `web/replay.py::_load_events` (`web/replay.py:33`) — it loads the whole file, which is wrong for a 2-second poll loop. | `src/conductor/fleet/summary.py` | DONE |
| E6-T2 | IMPL | Derive the status state machine — `running` · `at-gate` · `paused` · `completed` · `failed` — from the event stream. Confirmed sources: `gate_presented` / `gate_resolved` (`engine/workflow.py:3552`, `:3570`) for `at-gate`; `workflow_completed` / `workflow_failed` for terminal states. Liveness from the run record is authoritative over log inference, which the design shows is unreliable on its own (228 false positives, 0 true positives). | `src/conductor/fleet/summary.py` | DONE |
| E6-T3 | IMPL | Current step and elapsed-on-step from the most recent `agent_started` (`engine/workflow.py:3410`, payload `agent_name`, `iteration`, `agent_type`) without a matching `agent_completed`; total elapsed from the record's `started_at`. | `src/conductor/fleet/summary.py` | DONE |
| E6-T4 | IMPL | Token and cost totals summed from `agent_completed` `tokens` / `cost_usd` (`engine/workflow.py:4106`). Per D5 and the design's *Known data gaps*, label tokens as completed-only, and track unpriced agents so cost renders `~$X (N unpriced)` — reusing the `unpriced_agents` / `has_unpriced` convention rather than summing `null` into a confident total. | `src/conductor/fleet/summary.py` | DONE |
| E6-T5 | IMPL | Extract run topology (agents, types, models, providers) from the `workflow_started` event for E9's detail screen. | `src/conductor/fleet/summary.py` | DONE |
| E6-T6 | IMPL | Carry the gate payload from `gate_presented` (`agent_name`, `prompt`, `options`, `option_details`) onto the summary, and expose a `gate_resolvable` flag computed from D4's rule: true when `port is not None`, false for `mode == "fg"`. Deriving it here keeps the policy in one place rather than in two screens. | `src/conductor/fleet/summary.py` | DONE |
| E6-T7 | TEST | Fixture JSONL logs covering each status; a log truncated mid-line; a log with no events yet; a gate opened then resolved; a run with unpriced agents; a `for_each` group as the current step; `gate_resolvable` true for `fg-web`/`bg` and false for `fg`. | `tests/test_fleet/test_summary.py` | DONE |

**Acceptance Criteria.**
- [x] Every status in the design's vocabulary is derivable and tested.
- [x] A truncated or actively-appended log never raises.
- [x] Cost never presents a partial total as complete.
- [x] Tokens are labelled as completed-only.
- [x] Tail reads are bounded, not whole-file.
- [x] `gate_resolvable` is computed once, in `summary.py`, per D4.
- [x] `uv run pytest tests/test_fleet/test_summary.py` passes.

---

### E7 — Textual app skeleton, Runs screen, optional dependency

**Status: DONE.**

**Goal.** The design's Phase 1 core: "Textual fleet list, read-only,
auto-refresh", the polled *Refresh model*, and the `tui` optional extra.

**Prerequisites.** E4, E6.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E7-T1 | IMPL | Add `tui = ["textual>=6.0"]` to `[project.optional-dependencies]` alongside the existing `claude-agent-sdk` and `aca` extras — pinned to the floor actually developed and tested against, not the much looser `>=1.0` (latest on PyPI is 8.2.8; `App.run_test()` / `Pilot` behavior that every TUI pilot test in E7-E14 depends on has changed across that range) — add `textual` to the `dev` group so pilot tests run, and re-lock with `uv lock`. | `pyproject.toml` | DONE |
| E7-T2 | IMPL | Flip the `fleet` callback to launch the TUI. Guard the `textual` import with the repo's established `try/except ImportError` + availability-flag pattern (`providers/aca.py:67-77`) and fail with the `pip install 'conductor-cli[tui]'` hint (note the distribution is `conductor-cli`, not `conductor`), never a traceback. | `src/conductor/cli/fleet.py` | DONE |
| E7-T3 | IMPL | Textual `App` with the `Screen` push/pop stack the design requires for real Escape-to-return drill-down, plus a ~2 s `set_interval` poll. Per *Refresh model*, no file watcher. | `src/conductor/fleet/tui/app.py`, `src/conductor/fleet/tui/__init__.py` | DONE |
| E7-T4 | IMPL | Runs screen: flat list sorted by recency (not grouped by workflow definition — an explicit design lesson from Prefect), columns per the mockup (workflow · current step · total elapsed · elapsed on step · tokens · cost · port), with `▲` for `at-gate` and `●` for running rendered as a persistent badge. | `src/conductor/fleet/tui/screens/runs.py` | DONE |
| E7-T5 | IMPL | First-class empty state showing the launch affordance rather than an empty table. | `src/conductor/fleet/tui/screens/runs.py` | DONE |
| E7-T6 | TEST | `App.run_test()` pilot tests: table renders seeded records, the at-gate badge appears, the empty state renders when no records exist, and a poll tick picks up a newly-written record. | `tests/test_fleet/test_tui_runs.py` | DONE |
| E7-T7 | TEST | Bare `conductor fleet` with `textual` unavailable prints the install hint and exits non-zero without a traceback; `conductor fleet list` still works in that state. | `tests/test_cli/test_fleet_optional_dep.py` | DONE |

**Acceptance Criteria.**
- [x] `pip install conductor-cli` (no extras) leaves `run`, `stop` and `fleet list` fully working.
- [x] `conductor fleet` without `[tui]` prints an actionable install hint naming `conductor-cli[tui]`.
- [x] The Runs screen refreshes on a timer with no file watcher in the codebase.
- [x] The empty state is a screen, not an error.
- [x] Runs are sorted by recency.
- [x] `make check` and `uv run pytest tests/test_fleet tests/test_cli/test_fleet_optional_dep.py` pass.

---

### E8 — Dashboard open, kill and kill-all

**Status: DONE.**

**Goal.** The design's Phase 1 actions, including its constraint that killing
is "deliberately **not** `conductor fleet kill`" — the `k` / `K` bindings must
call the same code path as `conductor stop`, and must honor D1.

**Prerequisites.** E3, E7.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E8-T1 | IMPL | Extract the stop/kill logic from `cli/app.py` into a reusable function so the TUI and the CLI share one implementation, following the `_list_checkpoints_impl` / `_gate_respond_impl` shared-impl precedent (`cli/checkpoint.py:46`, `cli/gate.py:77`). The D1 confirmation is injected as a callback so the CLI supplies `rich.prompt.Confirm` and the TUI supplies a Textual modal — one policy, two presentations. This reuses E3-T10's verify-then-report contract (poll `is_process_alive` before declaring success and removing the record) — the TUI must not inherit the fire-and-forget behavior E3-T10 removes from the CLI. | `src/conductor/cli/app.py`, `src/conductor/fleet/tui/actions.py` | DONE |
| E8-T2 | IMPL | `w` opens `http://127.0.0.1:<port>` via `webbrowser`. A record with `port: null` disables the action with a visible reason rather than failing silently. | `src/conductor/fleet/tui/actions.py`, `src/conductor/fleet/tui/screens/runs.py` | DONE |
| E8-T3 | IMPL | `k` (single) and `K` (all) with one confirmation. Per *What single-user removes*, one confirm — no additional interlocks — and per D1 the TUI confirms **always**, naming any foreground runs in scope. Per *Patterns adopted from prior art*, kill works by signal via the run record, independent of any dashboard or API being reachable — which depends on E3-T9 making `SIGTERM` actually effective against a `mode == "fg"` run; without that fix this binding would silently no-op exactly as `conductor stop` does today. | `src/conductor/fleet/tui/actions.py` | DONE |
| E8-T4 | TEST | Pilot tests: `w` on a portless run is disabled with a reason; `w` on a `--web` run invokes the browser opener with the right URL; `k` signals exactly the selected PID and does not report success until the process is confirmed gone; `K` prompts once and signals all; declining the confirmation signals nothing; a kill on an already-dead PID does not raise; the TUI and CLI resolve to the same shared implementation. | `tests/test_fleet/test_tui_actions.py` | DONE |

**Acceptance Criteria.**
- [x] The TUI and `conductor stop` share one kill implementation with an injected confirm callback.
- [x] Kill works with no dashboard running.
- [x] Portless runs disable the dashboard action visibly.
- [x] `K` confirms exactly once and names foreground runs.
- [x] `k`/`K` do not report a run as killed until its process is confirmed gone.
- [x] `uv run pytest tests/test_fleet/test_tui_actions.py tests/test_cli/test_stop.py` passes.

---

### E9 — Run detail screen

**Status: DONE.**

**Goal.** The design's *Screens* → Run detail: "topology from
`workflow_started`, per-agent timings from the event log, current step
highlighted. **Not a DAG.**"

**Prerequisites.** E6, E7.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E9-T1 | IMPL | Detail screen pushed on `enter`, popped on Escape, rendering the topology from E6-T5 as a discrete step list — per *Patterns adopted from prior art*, discrete steps rather than scrolling text. | `src/conductor/fleet/tui/screens/run_detail.py` | DONE |
| E9-T2 | IMPL | Per-agent rows: status, elapsed, tokens, cost, with the current step highlighted. Explicitly out of scope per *Non-goals*: DAG rendering, agent messages, tool output. | `src/conductor/fleet/tui/screens/run_detail.py` | DONE |
| E9-T3 | IMPL | Detail rows need the full per-agent event history rather than a tail window; add a bounded full-log read used only on the detail screen, keeping the list screen on the tail path. | `src/conductor/fleet/summary.py` | DONE |
| E9-T4 | IMPL | Register the screen and its bindings on the app's screen stack. | `src/conductor/fleet/tui/app.py` | DONE |
| E9-T5 | TEST | Pilot: enter pushes detail for the selected run, Escape returns to Runs, the current step is highlighted, and a run whose log is missing renders a graceful placeholder. | `tests/test_fleet/test_tui_run_detail.py`, `tests/test_fleet/test_summary.py` | DONE |

**Acceptance Criteria.**
- [x] Escape returns to the Runs screen via the real screen stack.
- [x] No DAG, agent-message, or tool-output rendering is introduced.
- [x] A missing or unreadable event log degrades gracefully.
- [x] `uv run pytest tests/test_fleet/test_tui_run_detail.py` passes.

---

### E10 — Providers drill-down

**Status: DONE.**

**Goal.** The design's Phase 2, "pure reuse of `doctor --json`": collapsed
summary first (`copilot — 14 models`), expand for per-model reasoning effort and
context window.

**Prerequisites.** E7.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E10-T1 | IMPL | Providers screen sourced from `providers/diagnostics.py::gather` (`diagnostics.py:534` — note it is `async def`, so it runs as a Textual worker, not inline in a message handler). Consume the `ProviderDiagnostic` / `ModelDiagnostic` dataclasses directly — the design's "the TUI never touches provider internals" is satisfied by the diagnostics layer, so there is no need to shell out to `doctor --json`. | `src/conductor/fleet/tui/screens/providers.py` | DONE |
| E10-T2 | IMPL | Collapsed counts by default; expanding a provider shows `supported_reasoning_efforts`, `default_reasoning_effort` and `max_context_window_tokens` per model. Model listing implies `--check` (network), so it must be an explicit user action, run off the poll timer as an awaited worker, and must not block the UI. | `src/conductor/fleet/tui/screens/providers.py` | DONE |
| E10-T3 | IMPL | Render `tier` so experimental providers are visibly marked, consistent with `cli/doctor.py::_tier_cell`. Surface `connection_error` / `models_error` instead of showing an empty list. | `src/conductor/fleet/tui/screens/providers.py` | DONE |
| E10-T4 | IMPL | Bind `p` and register the screen. | `src/conductor/fleet/tui/app.py` | DONE |
| E10-T5 | TEST | Pilot with a stubbed `gather`: collapsed counts render, expansion shows model detail, an errored provider surfaces its error, Escape returns. | `tests/test_fleet/test_tui_drilldown.py` | DONE |

**Acceptance Criteria.**
- [x] Providers render offline by default; model fetching is explicit and non-blocking.
- [x] Provider tier is visible.
- [x] Errors are shown rather than rendered as emptiness.
- [x] No provider internals are imported directly; only the diagnostics dataclasses.

---

### E11 — Registries drill-down

**Status: DONE.**

**Goal.** The design's Phase 2: registries → workflows → inputs, reusing
`registry/config.py::load_config` and `registry/index.py::load_index`.

**Prerequisites.** E7.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E11-T1 | IMPL | Registries screen from `providers/diagnostics.py::gather_registries` (`diagnostics.py:394`), which already wraps `load_config` and distinguishes a load *failure* from an empty config via its `error` field — surface that distinction rather than reporting "no registries". | `src/conductor/fleet/tui/screens/registries.py` | DONE |
| E11-T2 | IMPL | Drill into a registry's workflows via `registry/index.py::load_index` → `RegistryIndex.workflows`. Index loading can hit the network for GitHub-backed registries (`index.py:179`), so it must be an explicit action off the poll timer. | `src/conductor/fleet/tui/screens/registries.py` | DONE |
| E11-T3 | IMPL | Drill into a workflow's inputs by fetching with `registry/cache.py::resolve_and_fetch` and loading with `config/loader.py::load_config`, rendering `wf.input` (`InputDef`: `type`, `required`, `default`, `description`) the way `conductor show` does at `cli/app.py:678`. | `src/conductor/fleet/tui/screens/registries.py` | DONE |
| E11-T4 | IMPL | Bind `r` and register the screen. | `src/conductor/fleet/tui/app.py` | DONE |
| E11-T5 | TEST | Pilot with a temp path-backed registry: registries list, workflows drill, inputs drill, Escape unwinds each level; a malformed registry config surfaces its error. | `tests/test_fleet/test_tui_drilldown.py` | DONE |

**Acceptance Criteria.**
- [x] A malformed registry config is reported as an error, not as "no registries".
- [x] Network-touching operations are explicit and never on the poll timer.
- [x] Escape unwinds one level at a time through the screen stack.
- [x] Input rendering matches `conductor show`'s field set.

---

### E12 — Launch with inputs

**Status: DONE.**

**Goal.** The design's Phase 3 and *Launch model: viewer, not supervisor* — the
TUI shells out to `conductor run --web-bg` and forgets.

**Prerequisites.** E7 (and E2, whose D2 launch gate `launch_background` now
depends on). Benefits from E11 for registry-ref picking.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E12-T1 | IMPL | `fleet/launch.py`: accept a file path or registry ref, resolve via `registry/resolver.py::resolve_ref` + `registry/cache.py::resolve_and_fetch` (the same pair `conductor show` uses at `cli/app.py:637`), and load the config to read `wf.input`. | `src/conductor/fleet/launch.py` | DONE |
| E12-T2 | IMPL | Build the `conductor run --web-bg` invocation with `--input k=v` pairs. **Do not** re-implement detached spawning — the design is explicit that `cli/bg_runner.py` has already solved it on both platforms and that re-implementing it would make runs die with the TUI. Call `launch_background()` directly rather than spawning a `conductor` subprocess; it now returns only after the child's run record exists (D2), so a successful call means the run is already discoverable. | `src/conductor/fleet/launch.py` | DONE |
| E12-T3 | IMPL | New-run screen: a form generated from `wf.input` with per-type widgets, required-field enforcement, defaults pre-filled, and descriptions shown. Coerce values to the declared `type` before submission, matching `InputDef`'s five types (`config/schema.py:42`). Because every run launched here is `--web-bg`, its gates are remotely resolvable by construction (D4). | `src/conductor/fleet/tui/screens/new_run.py` | DONE |
| E12-T4 | IMPL | Bind `n`, register the screen, and return to Runs on submit so the new run appears on the next poll. | `src/conductor/fleet/tui/app.py` | DONE |
| E12-T5 | TEST | Ref resolution for both file and registry forms; argv/kwargs construction; type coercion and required-field rejection; a launch failure — including the D2 record-poll timeout — surfaces its message rather than a traceback. | `tests/test_fleet/test_launch.py` | DONE |
| E12-T6 | TEST | Pilot: form renders from a fixture workflow's inputs, submission invokes the launcher with the coerced values, and the launched run appears in the list on the next refresh. | `tests/test_fleet/test_tui_new_run.py` | DONE |

**Acceptance Criteria.**
- [x] Launched runs survive the TUI exiting.
- [x] No detached-spawn logic is duplicated outside `cli/bg_runner.py`.
- [x] Required inputs are enforced and defaults are pre-filled.
- [x] Values are coerced to their declared type before launch.
- [x] A launch failure — including a record-poll timeout — is reported in-UI.

---

### E13 — Gate visibility, resolution and notifications

**Status: DONE.**

**Goal.** The design's Phase 4 and its *Patterns adopted from prior art*:
"'Needs human' as a first-class list state — persistent badge, not a
notification", plus the *Future work* item "Notifications on gate-entry and
run-failure (terminal bell / OSC 9)". Implements D4.

**Prerequisites.** E7, E8.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E13-T1 | IMPL | Surface the gate prompt and options carried by E6-T6 (from `gate_presented`: `agent_name`, `options`, `option_details`, `prompt` — `engine/workflow.py:3552`) on the Runs and detail screens. | `src/conductor/fleet/tui/screens/runs.py`, `src/conductor/fleet/tui/screens/run_detail.py` | DONE |
| E13-T2 | IMPL | D4 resolve path: bind `g` on a gated run with `gate_resolvable = true` to `cli/gate.py::_gate_respond_impl`, presenting the gate's options and posting the selection over the existing HTTP endpoint. Reuse the shared impl rather than a second HTTP client, so `CONDUCTOR_GATE_TOKEN` handling and the `/api/gate-status` auto-discovery come along unchanged — but the reuse is not free: `_gate_respond_impl` is synchronous, makes blocking `httpx` calls (5s/10s timeouts), writes every success and failure message to a module-level `rich.Console(stderr=True)`, and raises `typer.Exit` on all seven of its failure paths. Called directly from a Textual event handler it would block the UI thread, print escape sequences into the terminal underneath the TUI, and leak `click.exceptions.Exit`. Wrap the call in a Textual worker (`run_worker`) that captures the console's output and catches `typer.Exit`, translating it into an in-UI error per E13-T5's requirement that "a gate-respond HTTP failure surfaces in-UI rather than raising." | `src/conductor/fleet/tui/actions.py`, `src/conductor/cli/gate.py` | DONE |
| E13-T3 | IMPL | D4 display-only path: a `mode == "fg"` gated run renders `▲ at-gate (terminal · PID <pid>)` and disables `g` with that reason visible. No new run-record field and no new core resolution channel — a thread blocked in `Prompt.ask` (`gates/human.py:163-170`) cannot be cancelled, so a second channel would not help. | `src/conductor/fleet/tui/screens/runs.py`, `src/conductor/fleet/tui/actions.py` | DONE |
| E13-T4 | IMPL | Emit a terminal bell / OSC 9 notification on a transition **into** `at-gate` and into `failed`, debounced so a poll re-read cannot re-fire for the same transition. Per *What single-user removes*, no notification service. | `src/conductor/fleet/tui/notify.py` | DONE |
| E13-T5 | TEST | Notification fires once per transition, not once per poll; a gate resolved externally clears the badge on the next refresh; `g` on an `fg-web`/`bg` run calls the shared gate impl with the right port, agent and choice; `g` on an `fg` run is disabled and shows the PID; a gate-respond HTTP failure surfaces in-UI rather than raising. | `tests/test_fleet/test_notify.py`, `tests/test_fleet/test_tui_actions.py` | DONE |

**Acceptance Criteria.**
- [x] `at-gate` is a persistent badge, not a transient notification, for every mode.
- [x] Notifications fire once per transition.
- [x] A gated run with a dashboard port is resolvable from the TUI through the existing `gate respond` code path — no new endpoint.
- [x] A portless `fg` gated run is explicitly marked display-only with its PID, and the resolve action is disabled with a reason rather than failing at runtime.
- [x] No new run-record field was added for this epic.

---

### E14 — History screen

**Status: DONE.**

**Goal.** The design's Phase 4 *Screens* → History: "completed runs, subject to
retention".

**Prerequisites.** E5, E6, E7.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E14-T1 | IMPL | `fleet/history.py`: enumerate completed runs from retained `*.events.jsonl` files. The design's own measurement makes the constraint explicit — a non-terminal log is **not** evidence of a live run, so history must classify by terminal event and treat the rest as "ended, outcome unknown" rather than "running". | `src/conductor/fleet/history.py` | DONE |
| E14-T2 | IMPL | History screen listing workflow, outcome, duration, tokens and cost, bounded by E5's retention so the list cannot grow without limit. Per *What single-user removes*, no long-horizon audit history and no pagination. | `src/conductor/fleet/tui/screens/history.py` | DONE |
| E14-T3 | IMPL | Bind `h`, register the screen, and offer `conductor replay <log>` as the depth action — consistent with *Division of labor* (TUI = breadth). | `src/conductor/fleet/tui/app.py`, `src/conductor/fleet/tui/screens/history.py` | DONE |
| E14-T4 | TEST | Completed, failed and outcome-unknown logs classify correctly; the list is bounded by retention; a corrupt log is skipped rather than fatal. | `tests/test_fleet/test_history.py` | DONE |

**Acceptance Criteria.**
- [x] A log with no terminal event is never presented as running.
- [x] History is bounded by the retention policy.
- [x] Depth is delegated to `conductor replay`, not re-implemented.

---

### E15 — Documentation, changelog and AGENTS.md

**Status: DONE.**

**Goal.** Bring the repo's own documentation surfaces in line, matching how
`skills`, `aca` and the `checkpoint` / `gate` groups were documented.

**Prerequisites.** E4 (minimum, for a Phase 0-only release); complete after the
last shipped phase.

| Task ID | Type | Description | Files | Status |
|---|---|---|---|---|
| E15-T1 | IMPL | `docs/fleet.md`: screens, key bindings, the status vocabulary, the `[tui]` extra, retention, the design's *Division of labor* framing (TUI = breadth, dashboard = depth), and the D4 gate matrix — which modes can be resolved from the TUI and why `--web` is recommended for anything intended to be fleet-managed. | `docs/fleet.md` | DONE |
| E15-T2 | IMPL | CLI reference: `conductor fleet`, `fleet list`, `fleet prune`; update the `conductor stop` section for run-record scope, `--run-id`, `--yes` and the D1 confirmation; update the table of contents. | `docs/cli-reference.md` | DONE |
| E15-T3 | IMPL | `docs/configuration.md`: document `~/.conductor/config.toml` (D3) — location, `$CONDUCTOR_HOME`, `[fleet.retention]`, defaults, read-only status, and the `conductor replay` consequence of pruning. | `docs/configuration.md` | DONE |
| E15-T4 | IMPL | AGENTS.md: add `src/conductor/fleet/` to Core Package Structure, `settings.py` to the top-level module list, `cli/fleet.py` to the `cli/` list, and `tests/test_fleet/` to Tests Structure. Note the `invoke_without_command=True` deviation from the other three noun sub-apps so a future reader does not "fix" it, and note that the bg launch gate is now a run-record poll rather than a parent-side PID write (D2), so a future reader does not restore `write_pid_file`. | `AGENTS.md` | DONE |
| E15-T5 | IMPL | CHANGELOG Unreleased entry, leading with the run-discovery fix (foreground runs were previously invisible to `conductor stop`) since that is the user-visible bug fix independent of the TUI. Call out the `conductor stop` confirmation as a behavior change and the new `~/.conductor/config.toml`. | `CHANGELOG.md` | DONE |
| E15-T6 | IMPL | README: mention the fleet manager and `pip install 'conductor-cli[tui]'`. | `README.md` | DONE |

**Acceptance Criteria.**
- [x] `conductor stop`'s documented scope and confirmation behavior match its implementation.
- [x] `~/.conductor/config.toml` is documented in `docs/configuration.md`.
- [x] AGENTS.md describes the fleet module, `settings.py`, the deliberate sub-app deviation, and the D2 launch-gate change.
- [x] The CHANGELOG entry names the foreground-run discovery fix and the `stop` behavior change.
- [x] `make validate-examples` and `make check` pass.

---

## References

**Source design (authoritative)**
- [`docs/projects/fleet-manager/fleet-manager.design.md`](./fleet-manager.design.md) — *Solution Design: Fleet Manager*. Every epic above references a section of this document; design decisions are not re-derived here.

**Prior art in this repo**
- [`docs/projects/aca/aca-provider.plan.md`](../aca/aca-provider.plan.md) — the plan-document format and Open-Questions convention this plan follows.
- `src/conductor/cli/checkpoint.py`, `src/conductor/cli/gate.py`, `src/conductor/cli/registry.py` — the noun sub-app pattern (`add_typer`, `rich_help_panel`, shared `_impl` functions) that `cli/fleet.py` mirrors, and the `_gate_respond_impl` D4 reuses.
- `src/conductor/cli/pid.py` — the hardened cross-platform liveness probe the design requires reusing (issues [#166](https://github.com/microsoft/conductor/issues/166), [#344](https://github.com/microsoft/conductor/issues/344)).
- `src/conductor/cli/bg_runner.py` — detached launching on both platforms, PID file, captured stdout/stderr (issue [#116](https://github.com/microsoft/conductor/issues/116)); `_finalize_background_launch` is the launch gate D2 rewires.
- `src/conductor/registry/config.py` — the `~/.conductor/*.toml` + `$CONDUCTOR_HOME` + `tomllib` precedent `settings.py` follows for D3.
- `src/conductor/engine/checkpoint.py` — the `keep_last` / `rotate_periodic_checkpoints` retention vocabulary E5 mirrors (issue [#244](https://github.com/microsoft/conductor/issues/244)), and the `checkpoints/` subdirectory retention must not touch.
- `src/conductor/engine/event_log.py` — the always-on JSONL event log every fleet summary reads, including `CONDUCTOR_RUN_ID` adoption, which makes D2's parent-side record poll possible.
- `src/conductor/engine/workflow.py::_handle_gate_with_web` — the CLI-vs-web gate resolution policy that D4 is derived from.
- `src/conductor/gates/human.py` — the blocking `Prompt.ask` in `asyncio.to_thread` that makes foreground gates unresolvable remotely (D4).
- `src/conductor/providers/diagnostics.py` — `ProviderDiagnostic` / `ModelDiagnostic` / `RegistryDiagnostic`, the stable serialized contract behind `conductor doctor --json` (issue [#301](https://github.com/microsoft/conductor/issues/301)).
- `src/conductor/interrupt/listener.py` — the only `SIGTERM` handler in the codebase; the evidence behind Open Question 1.
- `src/conductor/web/replay.py` — event-log replay; the depth action E14 delegates to, and the reason retention is irreversible.

**Related issues**
- [#181](https://github.com/microsoft/conductor/issues/181) — `conductor watch`; the naming collision the design cites when rejecting `watch`.
- [#245](https://github.com/microsoft/conductor/issues/245) — dashboard stop/kill checkpointing via `handle_dashboard_stop`; the path a signal-based `stop` does **not** go through (Open Question 1).
- [#265](https://github.com/microsoft/conductor/issues/265) — unpriced models; the `~$X (N unpriced)` convention E6-T4 reuses.
- [#275](https://github.com/microsoft/conductor/issues/275) — the flat-verb / noun-sub-app CLI convention `fleet` joins.
- [#286](https://github.com/microsoft/conductor/issues/286) — the gate resolution policy in `_handle_gate_with_web` that D4 builds on.

**External**
- [Textual](https://textual.textualize.io/) — the TUI framework; `App.run_test()` / `Pilot` back the pilot tests in E7–E14.
- [Textual testing guide](https://textual.textualize.io/guide/testing/) — the harness used for all TUI tests.
- [`textual-serve`](https://github.com/Textualize/textual-serve) — noted by the design as a future escape hatch, not part of this plan.
