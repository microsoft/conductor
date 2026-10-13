# Fleet Manager TUI

Contributor rules for the `conductor fleet` Textual app (`src/conductor/fleet/tui/`) and the
event-log readers it renders (`src/conductor/fleet/summary.py`, `src/conductor/fleet/history.py`,
`src/conductor/fleet/resume.py`, `src/conductor/fleet/launch.py`). Run records, terminal
records, and retention are in [CLI and runs](cli-and-runs.md); user-facing behavior is in the
[Fleet Manager guide](../fleet.md).

## When to read

- Editing anything under `src/conductor/fleet/tui/`: the screens in
  `src/conductor/fleet/tui/screens/`, or `src/conductor/fleet/tui/app.py`,
  `src/conductor/fleet/tui/actions.py`, `src/conductor/fleet/tui/theme.py`,
  `src/conductor/fleet/tui/anim.py`, `src/conductor/fleet/tui/widgets.py`,
  `src/conductor/fleet/tui/notify.py`, `src/conductor/fleet/tui/dag.py`.
- Changing how status, current step, tokens, cost, gates, or history are derived from event logs.
- Changing New Run / Resume launching or the launch directory.
- Writing or fixing tests under `tests/test_fleet/`, especially Textual pilot tests.

## Required invariants

### Packaging

- Textual is the optional `tui` extra with floor `textual>=8.0` (`pyproject.toml`);
  `src/conductor/fleet/tui/widgets.py::BlockFooter` is written against 8.x `Footer` group rendering.
- Only bare `conductor fleet` imports the TUI. A missing Textual prints
  `src/conductor/install_hint.py::install_command` and exits non-zero (see
  [CLI and runs](cli-and-runs.md)); `fleet list` / `fleet prune` need no extra.

### No blocking I/O on the event loop (#437)

- Every screen that reads the filesystem on a timer, or on any path that can be slow, MUST do it in
  a worker: an `@work async def` that `await asyncio.to_thread(<collector>)`, then renders on the
  event loop after the `await`. Examples:
  `src/conductor/fleet/tui/screens/runs.py::RunsScreen._refresh_worker`,
  `src/conductor/fleet/tui/screens/run_detail.py` `_refresh_worker`,
  `src/conductor/fleet/tui/screens/history.py` `load_history`,
  `src/conductor/fleet/tui/screens/step_detail.py` `load_step`,
  `src/conductor/fleet/tui/screens/new_run.py` `action_resolve` / `action_launch`, and
  `src/conductor/fleet/tui/actions.py::kill_runs`.
- Documented exceptions: `src/conductor/fleet/tui/screens/registries.py` `load_registries` reads one
  local TOML inline, and `src/conductor/fleet/tui/screens/providers.py` `load_providers` awaits a
  natively async `gather()` (whose SDK availability imports are still synchronous, so it is not yet
  an instance of the pattern).
- Every screen shows `src/conductor/fleet/tui/theme.py::loading_text` from `compose()` until its
  first result lands.

### Polled refresh: drop, coalesce, always release

- The two polled screens (`src/conductor/fleet/tui/screens/runs.py`,
  `src/conductor/fleet/tui/screens/run_detail.py`) keep `refresh_runs()` / `refresh_detail()` as a
  SYNCHRONOUS dispatcher (callable from `on_mount`, `set_interval`, and action handlers) guarded by
  a plain `_refreshing` boolean. A tick that arrives mid-scan is DROPPED. NEVER replace this with
  `@work(exclusive=True)`, which cancels the in-flight scan instead of skipping the new tick.
- Set `_refreshing` in the dispatcher, not as the worker's first line (a `@work` body does not
  start until Textual schedules it), and release it in a `finally` (one transient error would
  otherwise stop refreshing for the session, indistinguishable from a quiet fleet).
- An explicit refresh (after a kill in `_kill_and_refresh`, in `action_resolve_gate`'s `finally`)
  passes `explicit=True` and is COALESCED via `_refresh_pending`, not dropped: those callers promise
  the table reflects what they just did. Load-once screens (`load_history`) need no guard. Pinned by
  `tests/test_fleet/test_tui_runs.py` (`test_a_tick_is_skipped_while_a_refresh_is_in_flight`,
  `test_a_dropped_poll_tick_is_not_remembered`).

### A failed load is never an empty state

- NEVER render "No runs" or "No run history yet" for a directory that could not be read; that is a
  false claim of absence. `src/conductor/fleet/tui/screens/runs.py::RunScan` distinguishes an empty
  fleet from one whose summaries all failed (`failed`, `seen_run_ids`);
  `src/conductor/fleet/tui/screens/history.py` shows a red error line like
  `src/conductor/fleet/tui/screens/step_detail.py` and
  `src/conductor/fleet/tui/screens/registries.py`.
- `RunScan.seen_run_ids` MUST carry every record read, not just rows that rendered, because the
  notifier (`src/conductor/fleet/tui/notify.py`) is pruned against it. Pruning against rendered
  rows made a briefly failing run look new and re-fire its gate notification. Notifications
  (terminal bell / OSC 9) fire once per status transition.

### Frame timer repaints only what moves (#462)

- The ~10 fps frame timer (`src/conductor/fleet/tui/screens/runs.py::RunsScreen._tick`) may repaint
  only the animated table cells and the preview pane's `#run-preview-score` widget. The gate
  section, the `Progress N/M` header, and the footer's `refresh_bindings()` belong to the ~2 s data
  poll (`_update_gate_detail`) and to selection changes. NEVER call `_update_gate_detail()` from
  `_tick`.

### Animation policy

- Precedence (`src/conductor/fleet/tui/anim.py::animations_enabled`): `CONDUCTOR_FLEET_NO_ANIM`
  always wins (off); `CONDUCTOR_FLEET_ANIM` forces on; otherwise off on a detected RDP session
  (`is_remote_session`: `SESSIONNAME` starting `RDP-Tcp`); otherwise on.
- SSH MUST NOT be detected. Pinned by `tests/test_fleet/test_tui_anim.py`
  (`test_ssh_signals_are_not_detected`, `test_an_ssh_session_alone_does_not_disable`).
- Any path that disables animation also sets Textual's `App.animation_level = "none"` in
  `src/conductor/fleet/tui/app.py` `on_mount`. Pinned by `tests/test_fleet/test_tui_app.py`.

### Launch directory (#477, #486)

- `src/conductor/fleet/tui/app.py::FleetApp.launch_dir` is a process-lifetime `reactive[Path]`:
  no config key, no state file, reset to the process cwd on the next `conductor fleet`. Mutate it
  only through `FleetApp.set_launch_dir`.
- The only UI that changes it is `src/conductor/fleet/tui/actions.py::DirectoryPickerModal`,
  opened via `change_launch_directory` (bound to `d` on Runs and `ctrl+d` with `priority=True` on
  New Run, because `Input` binds `ctrl+d` itself). A bad path is rejected in place with a red
  message; the modal stays open.
- The tree-to-input mirror (`DirectoryPickerModal.on_tree_node_highlighted`) MUST fire only while
  the tree has focus: `DirectoryTree` posts a `NodeHighlighted` for its root as soon as its
  background load lands, which otherwise replaces the prefilled directory with its parent.
  Pinned by `tests/test_fleet/test_tui_actions.py`
  (`test_tree_highlight_while_unfocused_leaves_the_input_alone`).
- `src/conductor/fleet/launch.py` stays a pure function of its arguments: `resolve_workflow` takes
  `base_dir`, `launch_workflow` / `launch_resume` take `cwd`; none read the process cwd. A relative
  file reference joins onto `base_dir`; registry and absolute references ignore it.
  `ResolvedWorkflow.path` MUST be absolute via `Path(os.path.abspath(...))`, not `.resolve()` (keeps
  a symlinked project path as typed; a relative path in a detached child's argv would resolve
  against the wrong cwd). Pinned by `tests/test_fleet/test_launch.py::TestResolveWorkflowBaseDir`.
- New Run and Resume MUST launch through `src/conductor/cli/bg_runner.py::launch_background` /
  `launch_background_resume`, never a re-implemented spawn (a TUI-spawned run must not die with
  the TUI). `launch_resume` passes `workflow_path=None` and the checkpoint path.

### Footer and theme

- `BlockFooter` passes `show_command_palette=False` to `Footer.__init__` to hide the docked
  `^p palette` key (the `ctrl+p` binding still works), reclaiming columns for `d Dir`.
- `src/conductor/fleet/tui/theme.py` owns the single status glyph/label/colour vocabulary; screens
  MUST NOT define local glyph maps. `src/conductor/fleet/tui/theme.py::format_cost_text` is the one
  Fleet cost formatter and appends the compact billing label only to a cell that shows a dollar
  figure.
- Shared stop/kill/gate logic in `src/conductor/fleet/tui/actions.py` reuses
  `src/conductor/cli/app.py::stop_records` and `src/conductor/cli/gate.py::_gate_respond_impl`; do
  not duplicate either.
- `RunSummary.gate_resolvable` is computed once in `src/conductor/fleet/summary.py` (true iff the
  record has a dashboard port). A gate on a plain foreground (`fg`) run is displayed but cannot be
  resolved from the TUI; screens read the flag rather than re-deriving it from `mode`.

### Event-log derived state

- `src/conductor/fleet/summary.py::stream_event_log` is a streamed, uncapped reader bounded only
  by the longest line (#485). NEVER reintroduce head/tail/size windows; a long or resumed run
  outgrew all three. The Runs poll passes `keep_types=_SUMMARY_EVENT_TYPES` (a cheap regex
  prefilter that skips JSON parsing of uninteresting lines); detail screens read unfiltered. Add
  any event type `_scan_events` starts branching on to `_SUMMARY_EVENT_TYPES`
  (`tests/test_fleet/test_summary.py::TestScanAgentDetailsPrefilterEquivalence`).
- `src/conductor/fleet/summary.py::_scan_events` is generation-aware: each root `workflow_started`
  (a resume always writes another) resets status, gate, and open steps and replaces topology, name,
  cwd, and inputs, but does NOT reset token/cost totals, which accumulate across generations
  (`tests/test_fleet/test_summary.py::TestResumedRunGenerations`).
- Status comes only from explicit markers (`gate_presented`, `gate_resolved`,
  `workflow_completed`, `workflow_failed`), never from timing.
- Usage accounting (#599): only root-context completions in
  `src/conductor/fleet/summary.py::_USAGE_COMPLETION_EVENT_TYPES` (`agent_completed`,
  `parallel_agent_completed`, `subworkflow_completed`) add tokens and cost. An event carrying a
  nonempty `subworkflow_path` NEVER contributes directly, and neither does the duplicate
  `for_each_item_completed` envelope. The parent-context `subworkflow_completed` carries that
  child's own subtotal (grandchildren included), its `unpriced_agent_count` and its aggregate
  billing provenance, so a nested run's cost, unpriced warning and label stay on one basis. A
  child payload without a valid aggregate is counted as unstated, never labeled with the
  envelope's single `billing_mode`. `src/conductor/fleet/history.py` applies the same rule; both
  scanners stay single-pass (`tests/test_fleet/test_summary.py::TestSubworkflowUsageInRunSummary`,
  `tests/test_fleet/test_history.py::TestSubworkflowUsageInHistory`,
  `tests/test_engine/test_billing_provenance.py::TestSubworkflowRollup`).
- Billing labels (`RunSummary.billing`, `AgentDetail.billing`, `HistoryEntry.billing`) are counted
  in the same scanner branch, on the same events, under the same predicate
  (`src/conductor/fleet/summary.py` `_count_billing` / `_contributes_usage`, shared by
  `src/conductor/fleet/history.py`) as the cost beside them; never take them from a terminal record
  or the engine aggregate.
- `src/conductor/fleet/history.py::build_history_entries` enumerates
  `$TMPDIR/conductor/*.events.jsonl` directly (run records are gone by then). A log with no terminal
  event is `"unknown"`, NEVER `"running"`. The list is bounded by `[fleet.retention].keep_last` and
  independently by `_MAX_HISTORY_ENTRIES` (200). `_scan_history_events` MUST remain a single forward
  pass and use the LATEST root `workflow_started` as `started_at`. Entries are enriched with output
  and error from the terminal record when one exists; a missing terminal record is not corruption.
- `src/conductor/fleet/resume.py::correlate_checkpoints` joins history rows to checkpoints by
  normalized (`realpath`) event-log path, with `run_id` as a fallback refused when ambiguous. Resume
  availability is checkpoint existence plus the recorded `workflow_path` existing, NEVER `outcome`.
  Keep this out of `src/conductor/fleet/history.py`, which is derived from event logs alone. History
  gates its `r` binding via `check_action` and refreshes bindings on row highlight.

### Test cautions

- `tests/test_fleet/conftest.py` sets `CONDUCTOR_FLEET_NO_ANIM=1` for every test (autouse); a test
  of animation must delete it deliberately.
- Use `settle(pilot)` to wait for every worker to finish and the result to paint; a single
  `pilot.pause()` only turns the loop once and may land before the thread hop. Its leading
  `pause()` is load-bearing. Use `wait_for(pilot, predicate, message=...)` instead of fixed sleeps
  for anything timer-driven.
- A test that presses a key opening a modal (kill confirm, gate options), or that suspends a
  worker on a `threading.Event`, and then needs a second keypress, MUST use a plain
  `await pilot.pause()` between the two, NEVER `settle`: `settle` awaits
  `app.workers.wait_for_complete()`, which cannot finish until the second keypress. `settle` is
  bounded at 30 s so the mistake fails instead of hanging the run, but the test still fails.

## Change checklist

- [ ] New filesystem read in a screen: worker + `asyncio.to_thread`, `loading_text()` first,
      explicit error rendering on failure.
- [ ] New polled screen: copy the dispatcher + `_refreshing` + `finally` pattern from
      `src/conductor/fleet/tui/screens/runs.py`.
- [ ] New animated element: repaint it from `_tick` only if it actually moves; everything else
      on the data poll.
- [ ] New event type the summary scanner reads: add it to `_SUMMARY_EVENT_TYPES` and cover it in
      `tests/test_fleet/test_summary.py`.
- [ ] New binding: check footer width on an 80-column terminal and update `docs/fleet.md`
      key bindings.
- [ ] Pilot tests: follow the test cautions above; run `uv run pytest tests/test_fleet`.

## Rationale and evidence

**Workers and the drop guard (#437).** Synchronous scans of a large event-log directory froze the
UI; a newest-wins worker would cancel a scan that was nearly done on every tick of a slow machine.

**Failed load vs empty.** "No runs" reads as success; a user acting on it could miss a running or
gated workflow.

**Frame timer (#462).** Rebuilding the preview pane and footer bindings ten times a second was
costly over remote sessions. RDP is detected because it diffs, encodes, and ships pixel regions;
SSH ships a few hundred bytes of ANSI per frame and the costly case (a slow link) has no signal,
so `CONDUCTOR_FLEET_NO_ANIM` is the remedy there and for VNC/Citrix/xrdp.

**Streaming reader (#485).** A real 9.72 MB / 20,361-line log lost its current step, totals,
and topology under the old windows; the prefiltered stream reads it in about 12 ms.

## Related references

- [Fleet Manager user guide](../fleet.md) (screens, key bindings, launch directory, animation)
- [Fleet Manager design](../projects/fleet-manager/fleet-manager.design.md)
- [CLI and runs](cli-and-runs.md) (run records, terminal records, retention, bg launch)
- [Testing](testing.md), [console output](console-output.md)
