# CLI Reference

Complete command-line reference for Conductor.

## Table of Contents

- [Root-Level Options](#root-level-options)
- [`conductor run`](#conductor-run)
- [`conductor status`](#conductor-status)
- [`conductor stop`](#conductor-stop)
- [`conductor fleet`](#conductor-fleet)
- [`conductor fleet list`](#conductor-fleet-list)
- [`conductor fleet prune`](#conductor-fleet-prune)
- [`conductor gate respond`](#conductor-gate-respond)
- [`conductor guide`](#conductor-guide)
- [`conductor checkpoint list`](#conductor-checkpoint-list)
- [`conductor validate`](#conductor-validate)
- [`conductor doctor`](#conductor-doctor)
- [`conductor registry`](#conductor-registry)
- [Deprecated command aliases](#deprecated-command-aliases)

## Root-Level Options

The following root-level options **must appear before the
subcommand name**:

```bash
conductor [ROOT OPTIONS] <command> [ARGS] [OPTIONS]
```

| Option | Short | Description |
|--------|-------|-------------|
| `--quiet` | `-q` | Minimal output (agent lifecycle and routing only) |
| `--silent` | `-s` | No progress output (JSON result only) |
| `--version` | `-v` | Show version and exit |

`--quiet` and `--silent` are mutually exclusive. They control workflow
progress output (`run` / `resume`); other commands may not be affected.

```bash
# Correct: root-level option before the subcommand
conductor --quiet run workflow.yaml

# Incorrect: rejected with "No such option: --quiet"
conductor run workflow.yaml --quiet
```

## `conductor run`

Execute a workflow from a YAML file.

```bash
conductor run <workflow.yaml> [OPTIONS]
```

### Options

| Option | Short | Description |
|--------|-------|-------------|
| `--input NAME=VALUE` | `-i` | Workflow input (repeatable) |
| `--input.NAME=VALUE` | | Alternative input syntax |
| `--metadata KEY=VALUE` | `-m` | Workflow metadata (repeatable). Merged on top of YAML `metadata:` and surfaced in the `workflow_started` event. |
| `--workspace-instructions` | | Auto-discover convention files (`AGENTS.md`, `CLAUDE.md`, `.github/copilot-instructions.md`, and `.github/instructions/**/*.instructions.md`) by walking from CWD up to the git root. Concatenated and prepended to every agent's prompt. See [Workspace Instructions](#workspace-instructions) below for details on the `.github/instructions/` directory convention. |
| `--instructions PATH` | | Explicit path to an instructions file (repeatable). Combines with auto-discovered files when both flags are used. |
| `--provider PROVIDER` | `-p` | Override provider (copilot, claude, claude-agent-sdk, hermes) |
| `--dry-run` | | Show execution plan without running |
| `--skip-gates` | | Auto-select first option at human gates |
| `--log-file <auto\|PATH>` | `-l` | Write full debug output to a file |
| `--web` | | Start a real-time web dashboard |
| `--web-bg` | | Run in background, print dashboard URL, exit |
| `--web-port PORT` | | Port for web dashboard (0 = auto-select) |
| `--no-interactive` | | Disable Esc-to-interrupt capability |

> **Note:** Output verbosity (`--quiet`/`-q`, `--silent`/`-s`) is controlled by
> [root-level options](#root-level-options), which must appear *before* the
> `run` subcommand: `conductor --quiet run workflow.yaml`.

### Examples

#### Basic Execution

```bash
# Run with a single input
conductor run workflow.yaml --input question="What is AI?"

# Run with multiple inputs
conductor run workflow.yaml -i question="Hello" -i context="Greeting"

# Alternative input syntax
conductor run workflow.yaml --input.question="What is AI?"
```

#### Provider Override

```bash
# Override the workflow's default provider
conductor run workflow.yaml --provider claude

# Use Copilot instead of Claude
conductor run workflow.yaml -p copilot
```

#### Dry Run and Debugging

```bash
# Preview execution plan without running
conductor run workflow.yaml --dry-run

# Quiet output (agent lifecycle only) — note: --quiet is a root-level option
# and must come before the run subcommand
conductor --quiet run workflow.yaml --input question="Test"

# Write full debug log to a file
conductor run workflow.yaml --log-file debug.log
```

#### Web Dashboard

```bash
# Start dashboard in foreground (keeps running after workflow completes)
conductor run workflow.yaml --web --input question="Test"

# Start dashboard on a specific port
conductor run workflow.yaml --web --web-port 8080 --input question="Test"

# Background mode: prints URL and exits immediately
conductor run workflow.yaml --web-bg --input question="Test"
# Dashboard auto-shuts down after workflow completes and clients disconnect
```

The `--web` flag starts a real-time browser dashboard showing:
- DAG visualization of the workflow graph with live node state updates
- Agent detail panel with rendered prompt, reasoning, tool calls, and output
- Streaming activity as agents execute (reasoning chunks, tool invocations)

The `--web-bg` flag is a convenience shortcut: it forks a background process running the workflow with the dashboard, prints the URL, and exits the CLI immediately. The background process shuts down automatically after the workflow completes and all browser clients disconnect.

`--web` and `--web-bg` are mutually exclusive.

**Security:** the dashboard is origin/host-restricted and token-protected by
default (issue #397). Requests must present a `Host` header naming the bound
machine (loopback aliases or the configured bind host); a present `Origin`
header must match too, though most non-browser clients (curl, `httpx`,
`conductor gate respond`) send none and are unaffected. Mutating routes
(`/api/stop`, `/api/kill`, `/api/resume`, `/api/gate-respond`,
`/api/guidance`) and the `/ws` WebSocket handshake additionally require a
bearer token — see [Authentication](#conductor-gate-respond) under
`conductor gate respond` for the token precedence order and discovery. Set
`CONDUCTOR_WEB_ALLOW_ORIGINS` (comma-separated full origins) to admit an
extra development origin, e.g. Vite's `http://localhost:5173`, without
disabling the check for anything else.

**Readiness contract** (issue #410) — the launcher confirms the workflow
actually started before it prints a URL and exits 0, rather than trusting a
bare TCP connection. This happens in two stages:

1. **Port reachability** — the launcher waits (up to 15s) for the child's
   dashboard port to accept connections, checking the child's exit status on
   every iteration. If the child dies before the port opens, the launcher
   exits 1 immediately (typically well under a second) with the exit code
   and a bounded tail of the child's captured stderr log — e.g. a
   `ConfigurationError` from a workflow that fails to even parse. A clean
   (exit code 0) sub-second run is not treated as a failure.
2. **Workflow start** — once the port is reachable and the PID file is
   written, the launcher polls `GET /api/info` (the same identity endpoint
   `conductor stop` uses) for up to 30s, waiting for it to report the
   workflow has actually started (not just that the dashboard's HTTP server
   is up). If the child dies during this wait, the launcher removes the PID
   file it just wrote and exits 1 with the exit code and stderr tail. If a
   *different* process already holds the requested port, the launcher
   terminates the child, removes the PID file, and exits 1 naming the
   conflicting PID and suggesting `--web-port`.

If the 30s workflow-start wait elapses with the child still alive and
listening, that is **not** treated as a failure: the URL is still printed
and the CLI still exits 0, since the workflow may simply be slow to start
(plugin fetch, MCP server startup, provider connection). In that case a
note is printed alongside the URL suggesting the dashboard or stderr log be
checked. Tune the wait (or disable it entirely) via
`CONDUCTOR_WEB_BG_START_TIMEOUT` (seconds; default `30`; `0` disables the
stage-two probe, restoring the pre-#410 behavior of trusting the port alone).

**`--web-bg` and `human_gate`** — background runs support human gates through
the dashboard. When the workflow reaches a `human_gate`, the detached process
waits for a response from the web dashboard (the gate modal) or the
`conductor gate respond` CLI, rather than trying to prompt on the (absent)
stdin. At launch, when the workflow contains a `human_gate` (and `--skip-gates`
is not set), Conductor prints a notice with the dashboard URL and the
`conductor gate respond` command so a parked run doesn't look stuck:

- Resolve each gate from the dashboard's decision modal, or
- Run `conductor gate respond --port <port> --choice <value>` (optionally
  `--agent <name>` and `--input "<text>"`), or
- Pass `--skip-gates` to auto-select the first option at every gate.

The same behavior applies to `conductor resume --web-bg`.

Background workflows can be stopped with `conductor stop` (see below) or via the stop button in the web dashboard.

#### Automation Mode

```bash
# Skip human gates (auto-select first option)
conductor run workflow.yaml --skip-gates

# CI/CD pattern: silent console + full file log
# (--silent is a root-level option and must come before the run subcommand)
conductor --silent run workflow.yaml --log-file auto --skip-gates --input question="Automated test"
```

#### Metadata and Instructions

```bash
# Inject runtime metadata (visible in the workflow_started event)
conductor run twig-sdlc.yaml --metadata work_item_id=1814 --metadata env=staging

# Auto-discover and inject convention instruction files (see "Workspace Instructions" below)
conductor run workflow.yaml --workspace-instructions

# Combine auto-discovery with an explicit extra file
conductor run workflow.yaml --workspace-instructions --instructions ./style-guide.md
```

##### Workspace Instructions

When `--workspace-instructions` is set, conductor walks from the current
working directory up to the git root and discovers four conventions, in this
order:

| Convention | Type | Discovery |
|---|---|---|
| `AGENTS.md` | File | Closest-to-CWD wins |
| `.github/copilot-instructions.md` | File | Closest-to-CWD wins |
| `CLAUDE.md` | File | Closest-to-CWD wins |
| `.github/instructions/**/*.instructions.md` | Directory (recursive) | Closest-to-CWD wins per relative path within the directory |

The directory convention follows GitHub Copilot's documented format
([GitHub docs](https://docs.github.com/en/copilot/customizing-copilot/about-customizing-github-copilot-chat-responses),
[VS Code docs](https://code.visualstudio.com/docs/copilot/customization/custom-instructions)):

- Files must use the double `*.instructions.md` extension.
- A YAML frontmatter block with `applyTo` controls activation:

  ```markdown
  ---
  description: 'Coding conventions for the API layer'
  applyTo: '**'
  ---
  Use four-space indentation.
  ```

- `applyTo: "**"` → loaded as always-on (matches Copilot's "always applied").
- `applyTo: "<other glob>"` → **skipped** (the convention scopes these per-file
  in the chat; conductor has no equivalent per-agent file scoping).
- `applyTo` absent → **skipped** (the convention says these are manual-attach
  only, never auto-applied).

This conservative interpretation matches the documented semantics exactly. To
include unscoped instructions today, use the explicit `--instructions PATH`
flag.

#### Complex Inputs

```bash
# JSON array input
conductor run workflow.yaml --input items='["item1", "item2", "item3"]'

# JSON object input
conductor run workflow.yaml --input config='{"key": "value", "count": 5}'

# Multi-line input (use quotes)
conductor run workflow.yaml --input text="Line 1
Line 2
Line 3"
```

## `conductor status`

List background workflows launched with `--web-bg`, without stopping any of them.

```bash
conductor status [OPTIONS]
```

### Options

| Option | Description |
|--------|-------------|
| `--json` | Emit machine-readable output instead of a table |

### Why This Exists

`conductor stop` with no arguments also lists running workflows — but it *stops* one when exactly one is running, so the natural "what's running?" reflex is destructive precisely when there is a single run to lose. `conductor status` never terminates anything.

It is also read-only on disk: unlike `stop`, it never removes a run record, so a run stays discoverable even if its liveness cannot be confirmed at that moment.

The dashboard URL is included because there is otherwise no supported way to recover it once the launching terminal is gone. The table renders `Started` to minute precision in UTC, and the dashboard URL wraps onto a second line rather than being cropped on a narrow terminal. `--json` reports the exact recorded `started_at` value regardless.

### `--json` Payload

```json
{
  "running": [
    {
      "pid": 12345,
      "port": 8080,
      "workflow": "my-workflow.yaml",
      "run_id": "a1b2c3d4",
      "started_at": "2026-03-03T12:00:00+00:00",
      "stderr_log": "/tmp/conductor/conductor-my-workflow-20260303-120000-a1b2c3d4.bg.stderr.log",
      "stdout_log": "/tmp/conductor/conductor-my-workflow-20260303-120000-a1b2c3d4.bg.stdout.log",
      "url": "http://127.0.0.1:8080"
    }
  ]
}
```

`run_id` is the join key to the run's events JSONL
(`conductor-<name>-<ts>-<run_id>.events.jsonl` under `$TMPDIR/conductor/`);
`stderr_log`/`stdout_log` are the paths to the child's captured console
output (see [Debugging `--web-bg` failures](../AGENTS.md#debugging---web-bg-failures)).
All three are `null` — never `""` — for a PID file written before this field
existed. A resumed run whose checkpoint carried no usable run id still gets
a freshly-minted `run_id` (and matching log paths), not `null`.

### Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Listed successfully, including when nothing is running |

### Examples

```bash
# What is running right now?
conductor status

# Machine-readable, for scripts
conductor status --json
```

## `conductor stop`

Stop running workflow processes — foreground, foreground+web, or background
(`--web-bg`). Discovers every run via its Fleet Manager run record
(`~/.conductor/runs/`, keyed by run ID), not just `--web-bg` processes.

```bash
conductor stop [OPTIONS]
```

### Options

| Option | Description |
|--------|-------------|
| `--port PORT` | Stop the workflow whose dashboard is on this specific port |
| `--run-id RUN_ID` | Stop the workflow with this run ID (the only selector that can target a foreground run, which has no dashboard port) |
| `--all` | Stop all *other* running conductor workflows (see Self-Exclusion) |
| `--allow-self` | Include the run this command is executing inside (refused by default) |
| `--yes`, `-y` | Skip the confirmation prompt when stopping a foreground run |
| `--force` | Proceed when the run's identity cannot be confirmed (see [Identity and `--force`](#identity-and---force)) |
| `--json` | Emit a machine-readable result per workflow on stdout instead of prose |

With no options, `conductor stop` lists running workflows. If exactly one is found, it stops automatically. If multiple are running, it prints the list and asks you to specify `--run-id`, `--port`, or `--all`. That listing shares its rendering with `conductor status`, so `Started` is shown at the same minute precision in UTC.

### Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Every targeted workflow is confirmed stopped, or was already gone — including a self-only refusal (see Self-Exclusion) |
| `1` | `--port`/`--run-id` matched no running workflow, the target was ambiguous, or the selector matched only your own run |
| `2` | At least one workflow survived, or could not be confirmed stopped |

Exit `2` is deliberately not a synonym for failure to signal — it means Conductor could not *prove* the process is gone. A run that ignored every rung and a run whose liveness could not be probed both land here, because both leave you with something you should look at by hand.

Listing without stopping anything (the ambiguous case) is exit `1`, not `0`: it is a failure to act, and must not report success to a script.

### Foreground confirmation

Stopping a **foreground** run (no dashboard, or a dashboard started without
`--web-bg`) requires confirmation, because — unlike a `--web-bg` process,
where the dashboard's Stop/Kill controls checkpoint before terminating — a
plain `SIGTERM` discards in-flight progress unless
[periodic checkpoints](workflow-syntax.md#periodic-checkpoints) are enabled
for that run. The prompt names every foreground run in scope and reports,
per run, whether a periodic checkpoint was actually found for it. `--all`
over a mixed fleet prompts **once**, naming all the foreground runs it would
stop; a background-only fleet is never prompted.

Use `--yes`/`-y` to skip the prompt (e.g. in scripts or CI). If stdin is not
a terminal and `--yes` was not passed, `conductor stop` refuses to
proceed — printing the runs it would have stopped and exiting non-zero —
rather than silently defaulting to "yes".

A pre-upgrade, legacy port-keyed `.pid` record (written by versions of
Conductor before the Fleet Manager run-record system) is always treated as
a background run and never triggers this prompt.

### How It Works

Every `conductor run` (and `resume`) writes a run record to
`~/.conductor/runs/<run_id>.json` describing its mode (`fg`, `fg-web`, or
`bg`), PID, workflow path, and (for a run with a dashboard) port. Records are
written atomically, so a concurrent `stop` can never read a half-written one,
and they are removed automatically when a workflow completes normally.

`stop` reads these records, escalates until the target is confirmed gone (a
graceful cancel via the dashboard, which lets the run checkpoint, then a
platform signal, then forceful termination), and only removes a run record
once its process is confirmed dead — so a workflow that survives stays
discoverable instead of becoming an untracked orphan. On Windows there is no
`SIGKILL` equivalent, so a still-running process is reported as such rather
than declared stopped.

A handful of pre-upgrade port-keyed `.pid` files may still exist under the
same directory; they are read and cleaned up the same way, so a background
run started before upgrading stays stoppable.

> **Windows note:** `CTRL_BREAK_EVENT` is delivered via
> `GenerateConsoleCtrlEvent`, which only reaches process groups attached to
> the *sending* process's own console. A `conductor stop` invoked from a
> different console window cannot reach a foreground `conductor run` (or a
> `--web-bg` child, which is spawned in its own detached process group) in
> another console.

The escalation ladder, confirming each rung before moving to the next:

1. **Ask the dashboard to cancel** (`POST /api/kill`) — the graceful rung, which lets the run write a checkpoint so you can `conductor resume` later.
2. **Send a platform signal** — `SIGTERM` on POSIX, `CTRL_BREAK_EVENT` on Windows.
3. **Force-terminate** — `SIGKILL` / `TerminateProcess`.

### Identity and `--force`

Between a run record being written and `stop` reading it, the OS may have recycled that PID onto an unrelated process. Every PID-directed rung is therefore gated on the dashboard confirming its own PID first. Three outcomes:

| Identity | Meaning | Behavior |
|----------|---------|----------|
| **confirmed** | The dashboard reports the PID we recorded | Proceed |
| **unconfirmed** | The dashboard could not be reached, or is too old to report a PID | Refuse, unless `--force` |
| **mismatched** | The dashboard reports a *different* PID | Refuse — `--force` does **not** override this |

`--force` overrides *uncertainty* only. A positive mismatch means the PID demonstrably belongs to something else, so signalling it would be signalling a stranger; no flag lifts that.

`--force` has one further effect. If a run's liveness cannot be probed at all, its entry would otherwise be permanent — bare `stop` stays ambiguous and `stop --all` exits `2` forever, which wedges CI teardown ([#166](https://github.com/microsoft/conductor/issues/166)). `--force` clears such an entry, printing a warning: if that process is still alive it is now untracked and must be stopped by hand. This is deliberately narrow, and does not fire for a mismatch or for a process demonstrably still alive.

The web dashboard also exposes these run-time controls:

- **Stop** (`POST /api/stop`) interrupts the current agent and pauses it, then
  offers **Resume** (re-run the agent) or **Kill**. If clicked during the brief
  startup window before the engine is ready, the Stop is queued and honored as
  soon as the engine binds its interrupt event (rather than hard-cancelling).
  This and **Kill** always preserve progress.
- **Kill** (`POST /api/kill`) stops the workflow entirely. A best-effort
  checkpoint is written so you can `conductor resume` later, and the dashboard
  shows a **"Workflow Stopped"** banner with the checkpoint path (or a clear
  explanation if no checkpoint could be saved).
- **Guide** sends mid-run guidance text (`POST /api/guidance`) to the running
  workflow — applied at the next step boundary, or immediately if an agent is
  currently paused (in which case it resumes with the guidance applied).
  Unlike Stop/Kill, this does not pause or terminate anything — it corrects
  the run's course without interrupting it. See
  [`conductor guide`](#conductor-guide) below for the CLI equivalent.

### Self-Exclusion

`conductor stop` never targets the run it is executing inside by default — an
agent smoke-testing this command must not terminate its own workflow (issue
#399). This matters because nothing about a PID-file entry says "this is the
workflow driving you" — an agent's `bash` tool, and any `conductor stop` it
spawns, sits inside that very run's process tree, so a naive `stop` treats it
as fair game just like any other run.

The caller's own run is identified by three signals, tried in order (first
match wins):

1. **`CONDUCTOR_RUN_ID` / `CONDUCTOR_SELF_RUN_ID`** env var matching the
   record's `run_id` (case-insensitively, since a manually-exported env var
   could differ in case from the minted lowercase id). The first is set on
   every `--web-bg` child; the second is exported by *every* run into its own
   environment, so a foreground run — which has no port for signal 2 and no
   `/proc` for signal 3 on Windows or macOS — can still recognise itself.
   Both are inherited by descendants, including a spawned `conductor stop`.
2. **`CONDUCTOR_WEB_BG=1` + `CONDUCTOR_WEB_PORT`** matching the entry's port —
   a compatibility signal used only for records written before `run_id`
   existed (empty `run_id`).
3. **Process ancestry** — a `/proc/<pid>/status` `PPid:` walk plus a session-id
   check, so any descendant of the background process (however it was
   re-parented) still resolves to it.

Effects:

- `conductor stop` (no flags) and `conductor stop --all` never stop your own
  run; `--all` means "stop all *other* runs." If only your own run is alive,
  both print a refusal and exit `0` (nothing was requested by name and
  nothing failed).
- `conductor stop --port <your own port>` is refused and exits `1`, naming
  `--allow-self` as the remedy — here a specific target was named and
  declined.
- `conductor stop --allow-self [...]` restores the pre-#399 targeting exactly
  (same processes, same counts), but now prints a yellow warning when the run
  being stopped is your own.

**Windows caveat**: process ancestry (signal 3) is POSIX-only. On Windows,
self-identification relies solely on the `CONDUCTOR_RUN_ID` /
`CONDUCTOR_WEB_BG`+`CONDUCTOR_WEB_PORT` env vars — an agent whose tool runner
strips `CONDUCTOR_*` env vars before spawning its shell is still exposed.

See [`conductor status`](#conductor-status) for a non-destructive way to see
the full list of running workflows, including your own.

### Examples

```bash
# Stop the only running workflow
conductor stop

# Stop a specific workflow by dashboard port
conductor stop --port 8080

# Stop a specific workflow by run ID (works even with no dashboard)
conductor stop --run-id a1b2c3d4

# Stop all other running workflows, confirming once if any are foreground
conductor stop --all

# Include the run this command is executing inside
conductor stop --allow-self --port 8080

# Stop all running workflows non-interactively (e.g. in a script)
conductor stop --all --yes
```

## `conductor fleet`

Monitor and manage the fleet of running Conductor workflows. With no
subcommand, launches the interactive Textual TUI — see
[`docs/fleet.md`](fleet.md) for the full guide to its screens, key
bindings, and status vocabulary.

```bash
conductor fleet
```

The TUI requires the `tui` extra. The install command depends on how
Conductor itself was installed, so the bare invocation prints the one that
works on your machine rather than guessing:

| How you installed | Command |
| --- | --- |
| The install script | `uv tool install --force 'conductor-cli[tui] @ git+https://github.com/microsoft/conductor.git@v<version>'` |
| A source checkout (`uv sync`) | `uv sync --extra tui` |
| Anything else — a wheel, `pip`/`pipx` from git, a system package | `pip install 'conductor-cli[tui]'` (with the git URL appended when there is one) |

`conductor-cli` is not on PyPI, so the `pip` form resolves only where pip
can already see an installed `conductor-cli` — never inside the uv tool venv
the install script creates. Without the extra, the bare invocation prints the
resolved command and exits non-zero rather than raising an `ImportError`
traceback:

```bash
$ conductor fleet
Error: the interactive fleet manager requires the 'tui' extra.
Install with: uv tool install --force 'conductor-cli[tui] @ git+https://github.com/microsoft/conductor.git@v<version>'
```

The suggested command pins the running version and includes any extras
already installed, since `uv tool install --force` replaces the tool's
entire requirement set. `conductor update` and the install scripts preserve
them for the same reason — see
[Updating](../README.md#updating).

`conductor fleet list` and `conductor fleet prune` (below) need nothing
beyond a normal Conductor install — only the bare, no-subcommand
invocation needs `textual`.

### Examples

```bash
# Launch the interactive TUI
conductor fleet
```

## `conductor fleet list`

List every live Conductor run — foreground, foreground+web, or
`--web-bg` — as a non-interactive Rich table. Discovers runs the same way
`conductor stop` does, via the Fleet Manager run record
(`~/.conductor/runs/`), so foreground runs show up here too, not just
`--web-bg` ones. This is core functionality with no optional dependency —
unlike the interactive `conductor fleet` TUI above, which requires the
`tui` extra.

```bash
conductor fleet list
```

Each row shows the workflow name, mode (`fg`, `fg-web`, or `bg`), status,
PID, dashboard port (`—` for a foreground run with no dashboard), and start
time. When no runs are active, it prints a dim "No runs found." line and
exits `0` — an empty fleet is a normal state, not an error.

### Examples

```bash
# List every live run
conductor fleet list
```

## `conductor fleet prune`

Prune old event logs under `$TMPDIR/conductor/`, keeping only the
most-recent `keep_last` (see
[`~/.conductor/config.toml`](configuration.md#machine-wide-settings-conductorconfigtoml)'s
`[fleet.retention]` table). This is the explicit manual entry point for
retention and always works — regardless of whether the opportunistic
startup sweep is enabled via `[fleet.retention].enabled`.

```bash
conductor fleet prune [OPTIONS]
```

### Options

| Option | Description |
|--------|-------------|
| `--keep-last N` | Number of most-recent event logs to retain, overriding `[fleet.retention].keep_last` from the settings file for this invocation only |
| `--dry-run` | List what would be pruned without deleting anything |

Never deletes the `checkpoints/` subdirectory or an event log still
referenced by a live (or currently-resuming) run. A retained or live run's
`.bg.stderr.log` / `.bg.stdout.log` companion files are always kept
alongside its event log.

> **Warning:** pruning an event log makes that run's history unavailable to
> `conductor replay` — `replay` reads the JSONL event log
> directly, so once it's deleted there is nothing left to replay.

With no `--keep-last`, the configured value from
`~/.conductor/config.toml` is used (`200` if the file doesn't set one). A
malformed settings file is reported as an error and exits non-zero in this
case; passing `--keep-last` explicitly bypasses the settings file entirely,
so a broken `config.toml` never blocks a manual override.

### Examples

```bash
# Prune using the configured (or default) keep_last
conductor fleet prune

# Preview what would be pruned without deleting anything
conductor fleet prune --dry-run

# Override keep_last for this invocation only
conductor fleet prune --keep-last 50
```

## `conductor gate respond`

Resolve a parked `human_gate` step from the command line without opening a browser. Sends a gate response to a running workflow's web dashboard via HTTP — useful for SSH sessions or headless environments where the dashboard UI is unreachable.

```bash
conductor gate respond [OPTIONS]
```

### Options

| Option | Short | Description |
|--------|-------|-------------|
| `--port PORT` | `-p` | Dashboard port of the running workflow (**required**) |
| `--choice VALUE` | `-c` | Selected gate option value (**required**) |
| `--agent NAME` | `-a` | Gate agent name (auto-discovered via `/api/gate-status` when omitted) |
| `--input TEXT` | | Additional free-text input for the gate response |
| `--token SECRET` | | Auth token (also reads from `CONDUCTOR_GATE_TOKEN` env var) |

### Authentication

Every request requires a valid token (issue #397): a per-run token is minted
automatically for every dashboard, so the protected configuration is the
default rather than something you must opt into. Requests without a
matching token are rejected with HTTP 403. The token is resolved in this
order:

1. `--token SECRET`
2. the `CONDUCTOR_GATE_TOKEN` environment variable
3. the per-run token file at `~/.conductor/runs/dashboard-<port>.token`
   (written with mode `0600` on POSIX by the dashboard on startup, removed
   on shutdown; on Windows the mode bits are not honoured, so the file's
   protection comes from the inherited ACL of its parent directory
   (`%USERPROFILE%\.conductor\runs` by default) rather than per-owner
   permission bits), auto-discovered by port so most invocations need
   neither flag nor env var

The first source found wins; a later source in the list is never consulted
once an earlier one supplies a value.

### Auto-Discovery

When `--agent` is omitted, `conductor gate respond` queries `GET /api/gate-status` on the specified port. If a gate is currently waiting, its agent name is used automatically and printed to the console. If no gate is waiting, the command exits with code 1.

### Examples

```bash
# Resolve the only waiting gate (agent auto-discovered)
conductor gate respond --port 8080 --choice approve

# Resolve a specific named gate
conductor gate respond -p 8080 -c reject --agent review-gate

# Pass additional free-text input
conductor gate respond -p 8080 -c approve --input "Looks good, ship it"

# Provide auth token via flag
conductor gate respond -p 8080 -c approve --token my-secret

# Provide auth token via environment variable
CONDUCTOR_GATE_TOKEN=my-secret conductor gate respond -p 8080 -c approve
```

> **Deprecated alias:** `conductor gate-respond` still works but prints a
> deprecation warning and will be removed in a future release. Use
> `conductor gate respond` instead.

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Gate resolved successfully |
| 1 | Connection error, auth failure, validation error, or no gate waiting |

## `conductor guide`

Send mid-run guidance text to a workflow running with `--web` or `--web-bg`,
without stopping it first. Useful for correcting course from an SSH session
or any headless environment where the dashboard UI is unreachable.

The guidance is applied at the next step boundary (before the next agent,
parallel group, for-each group, script, set, or wait step), or immediately if
an agent is currently paused (a dashboard **Stop**, or an Esc/Ctrl+G TTY
interrupt) — in which case the agent resumes with the guidance applied.

```bash
conductor guide [OPTIONS]
```

### Options

| Option | Short | Description |
|--------|-------|-------------|
| `--text TEXT` | `-t` | Guidance text to send to the running workflow (**required**) |
| `--port PORT` | `-p` | Dashboard port of the running workflow (auto-discovered via `~/.conductor/runs/` if omitted) |
| `--token SECRET` | | Auth token (also reads from `CONDUCTOR_GATE_TOKEN` env var) |

### Auto-Discovery

When `--port` is omitted, `conductor guide` scans `~/.conductor/runs/` for
running background workflows (the same read-only mechanism `conductor
status` uses). If exactly one is running, its port is used automatically. If
none are running, or more than one is, the command prints the list and exits
with code 1.

### Authentication

Same token precedence and auto-discovery as `conductor gate respond`: `--token` >
`CONDUCTOR_GATE_TOKEN` > the per-run token file in `~/.conductor/runs/`.

### Examples

```bash
# Auto-discover the running workflow's port
conductor guide --text "Prefer Python 3.12 examples"

# Target a specific dashboard port
conductor guide --port 8080 --text "Skip the benchmark step"

# Provide auth token via flag or environment variable
conductor guide --text "Use the staging endpoint" --token my-secret
CONDUCTOR_GATE_TOKEN=my-secret conductor guide --text "Use the staging endpoint"
```

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Guidance accepted |
| 1 | Connection error, auth failure, validation error, no workflow running, or the workflow has already completed |

## `conductor checkpoint list`

List saved workflow checkpoints (failure and periodic), newest first. Each row shows the workflow name, timestamp, trigger, the agent that was running (or about to run), the error type for failure checkpoints, and the checkpoint file path.

```bash
conductor checkpoint list [WORKFLOW]
```

### Arguments

| Argument | Description |
|----------|-------------|
| `WORKFLOW` | Optional workflow YAML path; filters the list to that workflow only |

### Examples

```bash
# List all checkpoints across every workflow
conductor checkpoint list

# List checkpoints for a single workflow
conductor checkpoint list workflow.yaml
```

Resume a failed run from its latest checkpoint with `conductor resume`. Pass
`--guidance "correction text"` (repeatable) to apply mid-run guidance to the
restored context before the resumed agent runs — see
[Mid-run guidance](#conductor-guide) above.

> **Deprecated alias:** `conductor checkpoints` still works but prints a
> deprecation warning and will be removed in a future release. Use
> `conductor checkpoint list` instead.

## `conductor validate`

Validate a workflow file without executing it. Checks YAML syntax, schema compliance, cross-references (agent names, routes, parallel groups), and Jinja2 template references throughout the workflow.

```bash
conductor validate <workflow.yaml>
```

### Examples

```bash
# Validate a single workflow
conductor validate my-workflow.yaml

# Validate with full path
conductor validate ./workflows/production/main.yaml

# Validate all examples (using shell expansion)
for f in examples/*.yaml; do conductor validate "$f"; done
```

### Validation Checks

**Errors** (validation fails):
- YAML syntax errors
- Schema compliance (required fields, types)
- Agent name references in routes
- Parallel group agent references
- For-each source references
- Circular dependency detection
- Input/output schema validation
- **Stale agent references in templates** — `{{ old_agent.output.field }}` where `old_agent` doesn't exist
- **Missing workflow input references** — `{{ workflow.input.x }}` where `x` isn't declared in `input:`
- Stale references checked across `prompt`, `system_prompt`, `command`, `args`, `working_dir`, `input_mapping`, parallel-group inputs, and workflow `output:` templates

**Warnings** (validation passes with notes):
- **Undeclared dependencies in explicit mode** — agent prompt references `{{ a.output.val }}` but doesn't declare `a.output` in its `input:` list

## `conductor doctor`

Report provider and environment diagnostics — a safe, read-only health check
for your Conductor setup. Answers "is my setup healthy?" without running a
workflow: which providers are installed, their stability tier, which
credential environment variables are detected, plus Conductor version /
update status and configured registries.

```bash
conductor doctor [SECTION] [OPTIONS]
```

`SECTION` (optional positional) limits output to one of `providers`,
`registries`, or `env`. When omitted, all three sections are shown.

**Offline by default** — no providers are instantiated and no credentials are
required. The only default network access is the GitHub-releases update check
in the `env` section (cache-first, short timeout, silent, and skipped when
`CONDUCTOR_NO_UPDATE_CHECK` is set).

### Options

| Option | Short | Description |
|--------|-------|-------------|
| `--check` | | Instantiate each provider and test its connection via `validate_connection()` (performs network I/O). |
| `--models` | | List each provider's available models (implies `--check`). |
| `--provider NAME` | `-p` | Scope the providers section to a single provider. |
| `--json` | | Emit machine-readable JSON instead of Rich tables (for CI). |

### Sections

- **env** — Conductor version, Python version, OS/platform, and update
  availability.
- **providers** — for each known provider (`copilot`, `claude`,
  `claude-agent-sdk`, `hermes`, `openai-agents`): whether the SDK is
  installed, the capability tier (`stable` / `experimental`), which
  credential environment variables are **present** (presence only — values
  are never printed), and — with `--check` / `--models` — connection status
  and a model count. `openai-agents` is surfaced as "not yet implemented".
- **registries** — configured workflow registries and which is the default
  (see [`conductor registry`](#conductor-registry)).

### Per-model capabilities (`--models`)

Beyond a model count in the Providers table, `--models` renders a separate
**Models** detail table per provider with each model's reasoning-effort
support and context-window limits:

| Column | Description |
|--------|--------------|
| Model | The model identifier. |
| Reasoning efforts | `reasoning.effort` levels the model accepts (e.g. `low, medium, high, xhigh`), `none` when the model definitively supports none (e.g. a non-thinking Claude model), or `n/a` when the provider can't determine support. |
| Default | The model's default reasoning-effort level, or `—` when unknown/not applicable. |
| Prompt / Output / Context | Maximum prompt (input), output (completion), and total context-window tokens, or `—` when the provider doesn't expose that limit. |
| Input $/Mtok / Output $/Mtok | Resolved per-million-token input/output rate (USD), or `—` when unpriced; a displayed `0.00` means a genuinely free rate reported by the provider, never "unknown". |
| Pricing | Where the rate came from (see #386): `provider` (live rate from the provider's `get_model_pricing` hook), `table` (static `DEFAULT_PRICING` fallback), `none` (unpriced — the run's cost summary will show `~$X (N unpriced)`), or `error` when pricing resolution itself failed. |

Coverage varies by provider — every field degrades independently to `n/a` /
`—` rather than failing the command:

- **Copilot** reports reasoning-effort levels + default, and prompt/context
  token limits, from the SDK's per-model metadata (`Output` is frequently
  `—` — the live API does not currently populate it for most models). It is
  also currently the only provider that implements the `get_model_pricing`
  hook, so `Pricing` shows `provider` for models the SDK prices live; other
  providers legitimately show `table` or `none`.
- **Claude** derives reasoning-effort support from a static heuristic
  (Claude 3.7+ / 4.x models support all five levels; older models support
  none) and reports only `Prompt` (via the Anthropic API's
  `max_input_tokens`) — `Output` and `Context` are always `—` and `Default`
  is always `—` (Anthropic has no per-model default-effort concept).
- **`claude-agent-sdk`**, **`hermes`**, and **`openai-agents`** don't
  implement model enumeration (`list_models`) at all, so `--models` shows
  `n/a` for them in the Providers table and they get **no** Models detail
  table — there is nothing to detail.

In `--json`, each provider's `models` field is a list of objects (not plain
id strings):

```json
{
  "id": "gpt-5.5",
  "supported_reasoning_efforts": ["low", "medium", "high", "xhigh"],
  "default_reasoning_effort": "medium",
  "max_prompt_tokens": 128000,
  "max_output_tokens": 64000,
  "max_context_window_tokens": 192000,
  "input_per_mtok": 2.00,
  "output_per_mtok": 8.00,
  "pricing_source": "table"
}
```

### Credential detection

Only the **presence** of credential environment variables is reported —
values are never read or printed. Detected variables per provider:

| Provider | Environment variables | Requirement |
|----------|-----------------------|-------------|
| `copilot` | `GITHUB_TOKEN`, `GH_TOKEN`, `COPILOT_PROVIDER_API_KEY`, `COPILOT_PROVIDER_BEARER_TOKEN`, `COPILOT_PROVIDER_RUNTIME_TOKEN` | optional overrides — authenticates via the GitHub/Copilot CLI login on disk |
| `claude` | `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` | required (direct Anthropic API) |
| `claude-agent-sdk` | `ANTHROPIC_API_KEY` | optional override — authenticates via `claude login` |
| `hermes` | *(none — endpoint / API key are passed explicitly)* | — |
| `openai-agents` | *(none — not yet implemented)* | — |

For **`copilot`** and **`claude-agent-sdk`**, these env vars are *optional
overrides*: both providers authenticate primarily via an on-disk CLI login
(the GitHub/Copilot CLI and `claude login`, respectively), so an all-absent
credentials cell for them is expected — **not** a misconfiguration. Each
absent optional variable renders as a neutral `○` (with a short note in the
**Notes** column) rather than the red `✗` used for a genuinely missing
*required* credential. The offline view only ever reports env-var
*presence*, never validity, for **any** provider — run a live connection
probe with `--check` to confirm a provider is actually ready.

### Exit codes

- `0` — success (the default for offline runs; missing credentials for an
  *optional* provider never fail the command).
- `1` — an invalid `SECTION`/`--provider` was given, **or** `--check` was set
  and the **scoped** provider failed to connect. The scoped provider is the
  one named by `--provider`, or `copilot` (the default) when `--provider` is
  omitted.

### Examples

```bash
# Full offline report (all sections)
conductor doctor

# Just the providers section
conductor doctor providers

# Actually test provider connections (network)
conductor doctor --check

# List available models for a single provider
conductor doctor --models --provider claude

# Machine-readable output for CI
conductor doctor --json
```

## `conductor registry`

Manage workflow registries — named sources (GitHub repos or local directories) for shared workflows.

```bash
conductor registry <subcommand> [OPTIONS]
```

### Subcommands

| Subcommand | Description |
|------------|-------------|
| `list [NAME]` | List configured registries, or list workflows in a specific registry. For GitHub registries, the per-registry listing also prints a "Latest tags:" footer with up to 5 newest tags. |
| `add <NAME> <SOURCE>` | Add a new registry (GitHub `owner/repo` or local path) |
| `remove <NAME>` | Remove a registry |
| `set-default <NAME>` | Set the default registry |
| `update [NAME]` | Refresh the cached index for one or all registries. For GitHub registries, the index is re-fetched via a SHA-pinned raw URL that bypasses Fastly's CDN, so updates always reflect the current state of the registry repo. |
| `show <NAME>` | Show details for a single configured registry: type, source, default status, and (for GitHub registries) a "Latest tags:" footer listing up to 5 newest tags discovered on the registry repo. Use `list <NAME>` to inspect the workflows it contains. |

### Options

| Option | Description |
|--------|-------------|
| `--default` | Mark as the default registry (with `add`) |

### Examples

```bash
# Add a GitHub-hosted registry and set it as default
conductor registry add official myorg/conductor-workflows --default

# Add a local directory registry
conductor registry add local ./my-workflows

# List all configured registries
conductor registry list

# List workflows in a specific registry
conductor registry list official

# Show registry details
conductor registry show official

# Set a different default
conductor registry set-default local

# Update cached registry index
conductor registry update

# Remove a registry
conductor registry remove local
```

### Running Workflows from a Registry

Once a registry is configured, `conductor run` accepts short workflow names
of the form `<workflow>[@<registry>][#<ref>]`. `@` selects the registry;
`#` selects a git ref (tag, branch, or commit SHA). Quote the reference in
shell commands so `#` isn't treated as a comment.

```bash
# Run from default registry (default-branch HEAD)
conductor run qa-bot

# Run from a specific registry (latest)
conductor run qa-bot@official

# Pin a specific tag
conductor run 'qa-bot@official#v1.2.3'

# Pin the default-branch HEAD or any other branch
conductor run 'qa-bot@official#main'

# Pin a specific commit SHA
conductor run 'qa-bot@official#a1b2c3d'

# Pin a tag in the default registry (empty registry segment)
conductor run 'qa-bot@#v1.2.3'
```

Path-type registries do not support `#<ref>` and will reject any reference
that includes one.

See [design/registry.md](./design/registry.md) for the full design.

## Deprecated command aliases

The command surface groups related subcommands under nouns (`checkpoint`,
`gate`, `registry`). Two older flat commands are retained as **deprecated
aliases** so existing scripts keep working. Each still runs, but prints a
one-line deprecation warning to stderr and forwards to its replacement. They
are hidden from `--help` and are slated for removal in a future release.

| Deprecated alias | Use instead |
|------------------|-------------|
| `conductor checkpoints [WORKFLOW]` | `conductor checkpoint list [WORKFLOW]` |
| `conductor gate-respond [OPTIONS]` | `conductor gate respond [OPTIONS]` |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | API key for Claude provider |
| `GITHUB_TOKEN` | Token for Copilot provider (if not using GitHub CLI auth) |
| `CONDUCTOR_LOG_LEVEL` | Logging level: DEBUG, INFO, WARNING, ERROR |
| `CONDUCTOR_GATE_TOKEN` | Overrides the dashboard's per-run minted auth token. Checked by `POST /api/stop`, `/api/kill`, `/api/resume`, `/api/gate-respond`, and `/api/guidance`, and by the `/ws` WebSocket handshake; also read by `conductor gate respond` and `conductor guide` |
| `CONDUCTOR_WEB_ALLOW_ORIGINS` | Comma-separated list of additional origins (`scheme://host:port`) the dashboard's `OriginHostGuard` accepts, on top of the loopback aliases and configured bind host. Dev-server escape hatch (e.g. Vite's `http://localhost:5173`); nothing else is disabled by setting it |
| `CONDUCTOR_HOME` | Overrides `~/.conductor/` as the location of run records, the registry config, and `config.toml` |
| `CONDUCTOR_FLEET_NO_ANIM` | Set to any non-empty value to disable Fleet Manager TUI animation (spinners, sparkline motion, splash). Useful over slow SSH links, in recorded terminals, and where movement is distracting |

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Workflow execution error |
| 2 | Validation error |
| 3 | Configuration error |
| 130 | User interrupt (Ctrl+C) |

## See Also

- [Fleet Manager](./fleet.md) - The `conductor fleet` TUI: screens, key bindings, gate resolvability, retention
- [Workflow Syntax Reference](./workflow-syntax.md) - Complete YAML syntax
- [Examples](../examples/) - Example workflows
- [Providers](./providers/) - Provider-specific documentation
