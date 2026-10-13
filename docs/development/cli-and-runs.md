# CLI and Run Lifecycle

How the Typer CLI is organized, how `run` and `resume` stay in parity, how a `--web-bg` launch
is gated and torn down, and how every run is made discoverable (run records, terminal records,
legacy PID files, self-exclusion, retention). Also covers the small leaf modules the run path
depends on (`src/conductor/run_id.py`, `src/conductor/rundir.py`,
`src/conductor/install_hint.py`, `src/conductor/settings.py`) and `conductor update`.

## When to read

- Editing anything under `src/conductor/cli/` (new command, new flag, help panels, aliases).
- Adding a flag to `conductor run` or `conductor resume`, or touching `run_workflow_async` /
  `resume_workflow_async` in `src/conductor/cli/run.py`.
- Changing `--web-bg` launch, readiness, or termination in `src/conductor/cli/bg_runner.py`.
- Changing run records, terminal records, `conductor stop` / `status` / `fleet list` scope, or
  event-log retention (`src/conductor/fleet/records.py`, `src/conductor/fleet/retention.py`).
- Changing the run-id format, the runs directory, install hints, or `~/.conductor/config.toml`.
- Debugging a background launch that failed or a run that is not discoverable (start with
  [debugging background launches](../cli-reference.md#debugging-background-launches)).

## Required invariants

### CLI organization and import cost

- Hot-path verbs stay flat (`run`, `resume`, `validate`, `show`, `stop`, `status`, `replay`,
  `update`, `doctor`, `guide`); the long tail lives in noun sub-apps registered with
  `app.add_typer(...)` (`registry`, `plugin`, `mcp`, `checkpoint`, `gate`, `bundle`, `fleet`).
  Group placement in root `--help` uses `rich_help_panel=`; panel order is the first-occurrence
  order of commands. Pinned by `tests/test_cli/test_help_panels.py`.
- Sub-apps without a bare-invocation default use `no_args_is_help=True`. The ONE deliberate
  exception is `fleet` (`src/conductor/cli/fleet.py`): `invoke_without_command=True`, because bare
  `conductor fleet` launches the TUI. NEVER "fix" it to match the others.
- The flat `checkpoints` and `gate-respond` commands are hidden deprecated aliases that print a
  stderr deprecation warning and forward to `checkpoint list` / `gate respond` through a shared
  impl (`src/conductor/cli/checkpoint.py::_list_checkpoints_impl`,
  `src/conductor/cli/gate.py::_gate_respond_impl`) (#275). User-facing list:
  [deprecated aliases](../cli-reference.md#deprecated-command-aliases).
- `src/conductor/cli/app.py` imports every `cli/*.py` sub-app module on every invocation. Any heavy
  dependency of a sub-app MUST be imported lazily inside the command body:
  `src/conductor/cli/mcp.py::serve` (the `mcp` SDK and `psutil`, because
  `conductor.mcp.__init__` eagerly imports the SDK) and `src/conductor/cli/bundle.py`
  (`conductor.bundle.*`, which pulls in Jinja2 and the plugin/skill registries). Type-only imports
  go under `TYPE_CHECKING`. Pinned by
  `tests/test_cli/test_mcp_serve.py::TestLazyImportBoundary`.
- `fleet` checks `TEXTUAL_AVAILABLE` only in the bare-invocation callback and prints
  `src/conductor/install_hint.py::install_command` with `soft_wrap=True`; NEVER hardcode
  `pip install 'conductor-cli[tui]'` (#441). Pinned by `tests/test_cli/test_fleet_optional_dep.py`.
- `plugin` has `list` and `fetch` and deliberately no `update`: a floating ref self-updates, a
  pinned one must not. `fetch` being its own verb is what keeps `conductor validate` offline.
  See [plugins and skills](plugins-and-skills.md).
- `conductor doctor` (`src/conductor/cli/doctor.py`) is presentation only; diagnostics live in
  `src/conductor/providers/diagnostics.py`.
- Read-only commands MUST NOT prune. `conductor status` reads with
  `src/conductor/fleet/records.py::scan_run_records` (never `read_run_records`, which deletes stale
  and corrupt files as it reads). Pinned by `tests/test_cli/test_status.py::TestStatusNeverStops`.
- `conductor status` and `conductor fleet list` also list recently completed runs from terminal
  records (R1). `--live` MUST restore the exact pre-change scope; `status --json` keeps the
  `running` array byte-compatible and adds a sibling `completed` array, which `--live` omits.
  Pinned by `tests/test_cli/test_status.py::TestStatusJsonCompletedRuns` and
  `tests/test_cli/test_fleet_list.py::TestFleetListCompletedRuns`.
- `conductor gate respond`, `conductor guide`, and the graceful rung of `conductor stop` resolve
  the dashboard token only through `src/conductor/web/auth.py::resolve_cli_token`
  (`--token` > `CONDUCTOR_GATE_TOKEN` > token file). See [dashboard](dashboard.md).
- Live-run discovery MUST use run records (`src/conductor/fleet/records.py::scan_run_records`
  for read-only callers). Do not copy `src/conductor/cli/guide.py::guide_impl`'s port
  auto-discovery: it still calls the legacy `src/conductor/cli/pid.py::scan_pid_files`, which
  sees only pre-upgrade `.pid` files, so it cannot find a current run (known gap; its tests mock
  `scan_pid_files`).
- All console output follows [console output](console-output.md) (`make_console`, `styled`).

### Run / resume parity

`run` and `resume` MUST accept the same flag wherever it is meaningful for a resumed run. When you
add a flag to `run`, add it to `resume` unless one of the documented exclusions applies.

| Flag | `run` | `resume` | Note |
|------|-------|----------|------|
| `--provider` / `-p` | yes | yes | runtime provider override |
| `--metadata` / `-m` | yes | yes | merged over YAML metadata |
| `--skip-gates` | yes | yes | auto-select first gate option |
| `--log-file` / `-l` | yes | yes | `auto` or explicit path |
| `--no-interactive` | yes | yes | disables Esc/Ctrl+G listener |
| `--web`, `--web-port`, `--web-bg` | yes | yes | dashboard and background launch |
| `--environment` | yes | yes | execution environment name or file |
| `--input` / `-i` | yes | no | inputs are restored from the checkpoint |
| `--workspace-instructions`, `--instructions`, `--print-loaded-instructions` | yes | no | the instructions preamble is persisted in the checkpoint |
| `--dry-run` | yes | no | resume executes from a saved point |
| `--input-json` (hidden) | yes | no | internal: background launcher round-trips typed inputs |
| `--guidance` | no | yes | resume-only (#400): no "mid-run" moment on a fresh run |
| `--from` | no | yes | explicit checkpoint file |

Implementation parity:

- `src/conductor/cli/run.py::run_workflow_async` and `::resume_workflow_async` MUST wire the same
  event emitter, `EventLogSubscriber`, `ConsoleEventSubscriber`, telemetry subscriber, and
  `WebDashboard` start/stop lifecycle, and construct `WorkflowEngine` with the same keyword set
  (today: `registry`, `skip_gates`, `workflow_path`, `interrupt_event`, `event_emitter`,
  `keyboard_listener`, `web_dashboard`, `instructions_preamble`, `plugin_marketplaces`,
  `execution_environment`, `secret_cache`, `redactor`, `run_context`, `run_id`, `log_file`,
  `dashboard_port`, `bg_mode`).
- Both paths MUST write the run's own record via
  `src/conductor/cli/run.py::_write_run_record_for_current_process`, write a terminal record via
  `::_write_terminal_record_for_current_process`, remove the live record in `finally` via
  `::_remove_run_record_for_current_process_safe`, run the retention sweep
  (`src/conductor/fleet/retention.py::maybe_prune_event_logs`), and prefetch plugin sources
  (`::_prefetch_plugin_sources`). Pinned by `tests/test_fleet/test_run_record_wiring.py`.
- Both paths call `src/conductor/cli/run.py::_prepare_run_secrets` early (before MCP servers are
  built or plugin sources fetched), scrub results and caught exceptions with the run redactor at
  the CLI boundary, and reset the context redactor only after the final sinks (terminal record,
  telemetry) drain. Secret semantics live in [execution backends](execution-backends.md).
  Pinned by `tests/test_cli/test_run_secrets_wiring.py`.
- Background forking lives in `src/conductor/cli/bg_runner.py`: `run --web-bg` calls
  `launch_background()` and `resume --web-bg` calls `launch_background_resume()`; both MUST forward
  equivalent options (including `--environment`, via `_extend_cmd_with_environment`). Neither
  writes a PID file: the detached child writes its own run record and the parent's
  `_finalize_background_launch` polls for it. Pinned by
  `tests/test_cli/test_bg_runner.py::TestLaunchedArgvIsAcceptedByTheChildCLI`.
- A resumed run reuses the checkpoint's `run_id` and appends to the original events JSONL when
  the checkpoint's `event_log_path` still exists
  (`src/conductor/engine/event_log.py::EventLogSubscriber`, `existing_path`/`existing_run_id`),
  so a multi-resume session has one continuous log and one `run_id`.
- Dashboard seeding and replay filtering on resume are specified in [dashboard](dashboard.md).

### Background launch (`--web-bg`)

- The child's stdout and stderr MUST be captured to
  `$TMPDIR/conductor/conductor-<name>-<ts>-<run_id>.bg.{stderr,stdout}.log`, never `DEVNULL`, so a
  silent crash or `faulthandler` dump leaves evidence (#116). The child receives
  `CONDUCTOR_WEB_BG`, `CONDUCTOR_WEB_PORT`, `CONDUCTOR_RUN_ID`, `CONDUCTOR_BG_STDERR_LOG`, and
  `CONDUCTOR_BG_STDOUT_LOG` (`src/conductor/cli/bg_runner.py::_build_bg_env`); the engine stamps
  the log paths into `workflow_started` system metadata.
- The child command MUST start with `sys.executable -P -m conductor` (both `launch_background` and
  `launch_background_resume`) so a chosen `cwd` containing a stray `conductor/` package cannot
  shadow the installed one. Use `-P`, not `PYTHONSAFEPATH`, which would leak into `type: script`
  steps.
- `cwd` (keyword-only, default `None`) threads to `Popen(cwd=...)` / `CreateProcess`; only the
  Fleet TUI (New Run and Resume) and `conductor mcp serve` pass one. `_spawn_bg_child` MUST
  validate it is an existing directory before opening log files and raise a named
  `RuntimeError`. Once a caller sets `cwd`, the workflow path MUST already be absolute (see
  `src/conductor/fleet/launch.py`).
  Pinned by `tests/test_cli/test_bg_runner.py::TestLaunchBackgroundCwd`.
- Readiness is staged in `_finalize_background_launch`:
  1. `_wait_for_server` polls the port and checks `proc.poll()` every iteration, so a child that
     dies before binding fails in well under a second.
  2. The run-record poll waits (15s) for the child's own record, matched on `mode == "bg"` and
     `port`, plus identity. It is a readiness signal, NOT a kill switch (#435): if its deadline
     passes while the child is alive and its dashboard still answers a fresh 1s re-probe, the
     launch proceeds with `BackgroundLaunch.run_record_written=False` and a warning, surfaced by
     `src/conductor/cli/app.py::_print_web_bg_no_run_record_notice` and the New Run screen. Only a
     dead child or an unreachable dashboard fails the launch.
  3. `_wait_for_workflow_start` polls `GET /api/info` for up to `CONDUCTOR_WEB_BG_START_TIMEOUT`
     seconds (default 30, `0` disables) for the presence of the `started_at` KEY, not its
     truthiness (#410). Outcomes are the `StartProbe` enum. `TIMED_OUT` with a live child returns
     normally (`workflow_started=False`) and is shown as "still initializing", not a failure.
  4. `still_running` is re-polled after the gate so a sub-second run is never advertised with a
     live dashboard URL.
- NEVER restore parent-side PID writing; a future readiness change belongs in the poll gate.
- Identity is the run record's `pid`, not `Popen.pid` (#444). A trampoline `sys.executable` (for
  example a Windows `uv tool install`) re-execs into a different process.
  `_confirmed_pid_from_record` accepts a record whose `pid` equals `proc.pid` OR that is fresh
  (`_record_is_fresh`: timezone-aware `started_at` at or after `launched_at`, captured just
  before spawning). The confirmed pid, not `proc.pid`, is what stage 3 compares `/api/info`
  against via `_classify_dashboard_identity`.
- `PORT_CONFLICT` is fatal ONLY when an identity was confirmed. An unconfirmed mismatch keeps
  polling and degrades at worst to `TIMED_OUT`. The error message MUST name the foreign pid
  captured from the probe loop's last payload, before termination.
- Termination MUST reach the whole tree (#447). Windows: the child is created with
  `CREATE_SUSPENDED`, assigned to a fresh job object (`_create_job_object`,
  `JOB_OBJECT_LIMIT_BREAKAWAY_OK` set, `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` deliberately NOT set so
  the tree outlives the launcher), then resumed in a `finally` so a failed assignment never
  leaves it suspended; `_WindowsDetachedProcess.terminate_tree` kills the job. POSIX:
  `start_new_session=True` makes the child a group leader and `os.killpg` is used ONLY for pids in
  `_SPAWNED_GROUP_LEADERS`, never an arbitrary pid.
- `_terminate_child` returns `_TerminationOutcome(confirmed, surviving_pids)` after an
  identity-checked liveness sweep over `proc.pid` and the confirmed pid. `_termination_note`
  says "The background process was terminated." only when `confirmed`; otherwise it names the
  survivors and points at `conductor status` / `conductor stop --port`.
- Of the five terminate-and-raise branches, only the three with a pid (dashboard unreachable,
  `CHILD_EXITED` non-zero, `PORT_CONFLICT`) call `_cleanup_record_after_termination`, which
  removes the record only once that pid is confirmed dead; a surviving orphan keeps the record
  that is `conductor stop`'s only handle on it. The two run-record-poll branches (read failure,
  fatal deadline) never had a confirmed pid and remove nothing.
- The user-facing troubleshooting checklist,
  [debugging background launches](../cli-reference.md#debugging-background-launches), quotes
  these failure messages, log-file names, and `system.bg_stderr_log`. Keep it in sync when you
  change a message, a log path, or `CONDUCTOR_WEB_BG_START_TIMEOUT`. `faulthandler` is enabled at
  import time in `src/conductor/__init__.py` so a native crash lands in the stderr log (#116);
  `workflow_failed` carries `is_base_exception` for failures that escaped `Exception` handling.
- `launch_background_resume` predicts the child's `run_id` from the checkpoint
  (`_peek_resume_run_id`) using the same rule as `EventLogSubscriber`, so the parent polls the key
  the child will actually write. Pinned by
  `tests/test_cli/test_bg_runner.py::TestResumeLaunchPollsCheckpointRunId`.

### Run records, terminal records, and self-exclusion

- Every run (foreground, `--web`, `--web-bg`, resumed) writes a `run_id`-keyed JSON
  `src/conductor/fleet/records.py::RunRecord` into `~/.conductor/runs/`. It has exactly nine
  fields; do not add more (a `tty` field was rejected as POSIX-only).
- `RunRecord.mode` is `RunMode = Literal["fg", "fg-web", "bg"]`. A missing `mode` defaults to
  `"bg"`; a non-string or explicit `null` is rejected. An unrecognised string MUST normalise, never
  raise: an unknown `fg*` value fails closed to `"fg"` (keeping the stop-confirmation prompt) and
  anything else becomes `"bg"`. A raise would route to the corrupt path, which deletes without
  checking liveness. Pinned by `tests/test_fleet/test_records.py::TestFromDictModeCoercion`.
- Every Windows sharing-violation-prone operation in records (`os.replace`, `os.unlink`, the
  quarantine `os.rename` and `os.link`) MUST go through `_retry_on_windows_sharing_violation`
  (#486). A violation that outlasts the budget is logged and reported as `False`, never as success.
- `TerminalRunRecord` tombstones live in `terminal_records_dir()`, a `terminal/` SUBDIRECTORY of
  the runs directory, never beside live records: the live-record globs are non-recursive, and a
  sibling file would be read as a corrupt live record (deleted regardless of liveness) or race the
  record it replaces. They carry status, rendered output, error type/message, usage totals, and
  log paths. Pinned by `tests/test_fleet/test_terminal_records.py`.
- `src/conductor/cli/pid.py` is read-only legacy support. It writes nothing; it exists so a
  still-running pre-upgrade background process with a `.pid` file stays discoverable and
  stoppable (`read_run_records()` surfaces those as `mode="bg"` records). It also owns
  `is_process_alive`, the repository's single liveness probe.
- `conductor stop` excludes the run it is executing inside unless `--allow-self` is passed
  (#399). `src/conductor/cli/self_run.py::partition_own_run` uses three signals, first match wins:
  (1) `CONDUCTOR_RUN_ID` or `CONDUCTOR_SELF_RUN_ID` equals the record's `run_id`; (2)
  `CONDUCTOR_WEB_BG` + `CONDUCTOR_WEB_PORT` match the record's port ONLY when the record has no
  `run_id`; (3) POSIX process ancestry (`/proc` `PPid:` walk plus `os.getsid(0)`). Every run
  exports `CONDUCTOR_SELF_RUN_ID` for its descendants; it is a different name from
  `CONDUCTOR_RUN_ID` because the event log treats `CONDUCTOR_RUN_ID` as "adopt this id", which a
  nested `conductor run` must not do. Pinned by `tests/test_cli/test_self_run.py`.

### Leaf modules and the run-id contract

- `src/conductor/run_id.py` is the single `run_id` contract (#435): `RUN_ID_PATTERN_SOURCE`
  (`[A-Za-z0-9_-]{1,64}`), `is_valid_run_id`, `new_run_id` (8 lowercase hex by default). NEVER
  restate the charset; filename parsers interpolate `RUN_ID_PATTERN_SOURCE`. NEVER normalise
  (for example lowercase) an id: a resumed child that writes a different key than the parent polls
  for gets killed. `src/conductor/fleet/records.py` re-exports `is_valid_run_id` for compatibility.
- `src/conductor/run_id.py`, `src/conductor/rundir.py`, and `src/conductor/install_hint.py` MUST
  stay stdlib-only with no `conductor` imports; `src/conductor/settings.py` imports only pydantic
  and `src/conductor/exceptions.py`. Reason: `conductor.cli.__init__` imports `conductor.cli.app`,
  which reaches `conductor.web.server`, so importing anything under `cli/` or
  `src/conductor/fleet/records.py` from an always-on module closes an import cycle.
  `src/conductor/install_hint.py` is top-level, not under `cli/`, because `providers/` needs it.
- `src/conductor/rundir.py::runs_dir` is the one definition of `~/.conductor/runs/` (it does not
  read `CONDUCTOR_HOME`). Callers MUST write `from conductor import rundir; rundir.runs_dir()`,
  not `from conductor.rundir import runs_dir`, so the autouse isolation fixture in
  `tests/conftest.py` that patches `conductor.rundir.runs_dir` reaches every caller.

### Install hints and update

- `src/conductor/install_hint.py::install_command` MUST never raise (it runs inside error
  construction; on failure it falls back to the install-script one-liner). Branch logic stays in
  the pure `render_install_command`; `detect_environment` is the impure half.
- Detection order: uv receipt FIRST (a uv tool install writes both files), then editable
  `direct_url.json` (`uv sync --inexact --extra ...`), else `<sys.executable> -m pip install`
  with the recorded git URL when present. Already-recorded extras MUST be unioned with the
  requested one, and the recorded install source reused. An unreadable receipt is
  `readable=False` (rendered with a `# WARNING` comment), never an empty extras set. PEP 610
  `archive_info` is not a source.
- `install.sh` (`receipt_extras`) and `install.ps1` (`Get-ReceiptExtras`) parse the same receipt
  and warn-and-continue on a broken one; agreement is asserted by
  `tests/test_integration/test_install_script_extras.py`, which executes the real helpers.
  Python side: `tests/test_install_hint.py`.
- `src/conductor/cli/update.py` delegates upgrades to the install scripts; in-process self-upgrade
  is gone (on Windows the running interpreter sits in the venv being replaced). `--apply` spawns
  the installer detached and exits. `_print_extras_note` reports the installed extras, because
  `uv tool install --force` rewrites the whole requirement set (#441). The startup hint is
  suppressed by `CONDUCTOR_NO_UPDATE_CHECK=1`, `--silent`, `--help`/`--version`, and `update`
  itself. Pinned by `tests/test_cli/test_update.py`.

### Settings and retention

- `src/conductor/settings.py::load_settings` reads `~/.conductor/config.toml` (honors
  `CONDUCTOR_HOME`) with `tomllib` and is read-only in v1. A missing file yields defaults; a
  malformed file raises for an explicit reader (`conductor fleet prune` without `--keep-last`)
  but MUST be swallowed by the startup sweep.
- `src/conductor/fleet/retention.py::maybe_prune_event_logs` is the opportunistic sweep
  `src/conductor/cli/run.py` calls; it is gated by `[fleet.retention].enabled` and MUST never raise.
  `prune_event_logs` never deletes the `checkpoints/` subdirectory or a log a live/resuming run
  references, keeps or prunes `.bg.*.log` companions and the matching terminal record with their
  log, and separately bounds orphaned terminal records. Pinned by
  `tests/test_fleet/test_retention.py`.

## Change checklist

- [ ] New command: choose flat vs sub-app, set `rich_help_panel`, update
      `tests/test_cli/test_help_panels.py` and [CLI reference](../cli-reference.md).
- [ ] New sub-app with heavy imports: import them inside the command body; extend the lazy-import
      test if a new heavy dependency appears.
- [ ] New `run` flag: add it to `resume` (and to both `launch_background*` argv builders when it
      matters to a background child) or add the reason to the parity table above.
- [ ] New engine kwarg: pass it in both `run_workflow_async` and `resume_workflow_async`.
- [ ] Bg-launch change: keep every stage's test in `tests/test_cli/test_bg_runner.py` passing on
      both POSIX and Windows code paths; never reintroduce `DEVNULL` or parent-side PID writes.
- [ ] Record-format change: keep `RunRecord` at nine fields, keep mode normalisation total, and
      keep terminal records in `terminal/`.
- [ ] Any new filesystem probe of runs/ uses `rundir.runs_dir()` via the module attribute.
- [ ] Any change to install extras logic: update both `src/conductor/install_hint.py` and both
      install scripts.
- [ ] User-facing change: add a `changelog.d/` fragment.

## Rationale and evidence

**Lazy sub-app imports.** Typer builds the whole command tree on every invocation; a top-level
import in one sub-app is paid by every `conductor` command, including `--help`.

**Read-only status.** A reader that prunes turns a diagnostic command into the one that loses the
run it was asked about, so observe-only callers use the `scan_*` readers.

**Run-record poll is not a kill switch (#435).** Killing a healthy workflow because a diagnostic
write failed is worse than an undiscoverable run; the warning tells the user it will not appear
in `status`.

**Identity, not `Popen.pid` (#444).** On a trampoline interpreter, comparing against `proc.pid`
produced a false `PORT_CONFLICT` on every port and a spurious "no run record" note. Freshness is
what a stale-record check actually needs.

**Whole-tree termination (#447).** Killing only `proc.pid` left the re-exec'd workflow running
while the message claimed it was terminated. CPython's `Popen` closes the thread handle before
returning, so suspend-then-assign requires the custom Windows wrapper. The killpg registry exists
because `os.killpg` on a stale or reused pid could kill an unrelated process group.

**Mode normalisation.** A newer Conductor's mode value would otherwise make an older one delete a
live run's record; folding an unknown foreground variant into `bg` would silently drop the
foreground stop confirmation.

**Extras preservation (#441).** `--force` rewrites the uv tool's requirement set and `uv sync` is
exact by default, so a hint or upgrade naming only one extra uninstalled the others; and pip
cannot resolve `conductor-cli` on the documented install path.

## Related references

- [Debugging background launches](../cli-reference.md#debugging-background-launches): the
  user-facing troubleshooting checklist for `--web-bg` failures
- [CLI reference](../cli-reference.md): user-facing commands, including
  [`conductor status`](../cli-reference.md#conductor-status) and
  [`conductor stop`](../cli-reference.md#conductor-stop)
- [Fleet Manager user guide](../fleet.md): terminal records, retention, launch directory
- [Configuration](../configuration.md): machine-wide settings (`~/.conductor/config.toml`)
- [Fleet Manager design](../projects/fleet-manager/fleet-manager.design.md)
- [Fleet TUI](fleet-tui.md), [dashboard](dashboard.md), [MCP server](mcp-server.md),
  [testing](testing.md), [console output](console-output.md)
