# Fleet Manager

`conductor fleet` gives you one place to see, manage, and launch every
Conductor run on the machine — foreground, foreground+web, and
`--web-bg` alike. It fixes a specific bug (foreground runs were invisible
to `conductor stop`) and layers an optional interactive TUI on top of the
same run-discovery mechanism.

## Table of Contents

- [The problem this solves](#the-problem-this-solves)
- [Installing the TUI](#installing-the-tui)
- [Animation and remote sessions](#animation-and-remote-sessions)
- [Screens](#screens)
- [Key bindings](#key-bindings)
- [Launch directory](#launch-directory)
- [Status vocabulary](#status-vocabulary)
- [Gates: display vs. resolve](#gates-display-vs-resolve)
- [Division of labor: TUI vs. dashboard](#division-of-labor-tui-vs-dashboard)
- [Terminal records](#terminal-records)
- [Retention](#retention)
- [See Also](#see-also)

## The problem this solves

Before the Fleet Manager, only `--web-bg` runs were discoverable: `conductor
stop` read a port-keyed `.pid` file that only `--web-bg`'s launcher ever
wrote. A plain `conductor run` or `conductor run --web` was invisible to
it — the only way to stop one was to find its PID yourself and signal it
directly. (`conductor fleet list` is itself new, introduced by the Fleet
Manager alongside the run-record mechanism below — it never had the old
PID-file limitation to begin with.)

Every `conductor run` (and `resume`) invocation now writes a small JSON
**run record** to `~/.conductor/runs/<run_id>.json` (or `$CONDUCTOR_HOME/runs/`), keyed by run ID rather
than port (a foreground run has no port), describing its mode (`fg` /
`fg-web` / `bg`), PID, workflow path, and — when it has a dashboard — port.
`conductor stop`, `conductor fleet list`, and the TUI all read from this
same record store, so every run is discoverable and stoppable the same way
regardless of how it was started. See the
[CLI reference](cli-reference.md#conductor-stop) for `stop`'s full behavior,
including the confirmation prompt a foreground stop now shows.

This run-record mechanism is core functionality with no optional
dependency — it works on a clean install. The interactive TUI described
below is a separate, optional layer on top of it.

## Installing the TUI

`conductor fleet list` and `conductor fleet prune` (documented in the
[CLI reference](cli-reference.md)) need nothing beyond a normal Conductor
install. The interactive TUI (`conductor fleet`, invoked with no
subcommand) additionally requires the `tui` extra.

The command depends on how Conductor itself was installed, so
`conductor fleet` prints the right one for your machine rather than
guessing:

| How you installed | Command |
| --- | --- |
| The install script (`curl -sSfL https://aka.ms/conductor/install.sh \| sh`) | `uv tool install --force 'conductor-cli[tui] @ git+https://github.com/microsoft/conductor.git@v<version>'` |
| A source checkout (`uv sync`) | `uv sync --extra tui` |
| Anything else — a wheel, `pip`/`pipx` from git, a system package | `pip install 'conductor-cli[tui]'` (with the git URL appended when there is one) |

`conductor-cli` is **not** published to PyPI, so the `pip` form resolves
only where pip can already see an installed `conductor-cli` — never inside a
uv tool venv, which is what the install script creates. That is why the hint
is resolved rather than hardcoded (issue #441); for a `pip`/`pipx`-from-git
install it also appends the git URL you installed from, so the command
actually resolves.

Without the extra, `conductor fleet` prints that command and exits non-zero
rather than raising an `ImportError` traceback:

```bash
$ conductor fleet
Error: the interactive fleet manager requires the 'tui' extra.
Install with: uv tool install --force 'conductor-cli[tui] @ git+https://github.com/microsoft/conductor.git@v<version>'
```

The suggested command pins the version already running and carries any
extras you already have, because `uv tool install --force` replaces the
tool's whole requirement set — installing `[tui]` on a machine that had
`[aca]` would otherwise remove it.

For the same reason, `conductor update` (and the install scripts it drives)
preserve the extras recorded in the existing install, so an upgrade never
silently uninstalls the TUI. To install an extra as part of an upgrade, or
to drop back to a bare install:

```bash
curl -sSfL https://aka.ms/conductor/install.sh | sh -s -- --extras tui
curl -sSfL https://aka.ms/conductor/install.sh | sh -s -- --no-preserve-extras
```

`conductor fleet list` and `conductor fleet prune` are unaffected either
way — only the bare, no-subcommand invocation needs `textual`.

## Animation and remote sessions

The TUI animates three things: the status badge of a running or at-gate
row (a spinner or a breathing glyph), the Runs screen's preview pane —
specifically the live step in its flowed score of chips, which is the
one part of the pane that moves — and the launch splash. All three are
driven by the same ~10fps interval and gate on the same check, so they
turn on and off together.

That clock only ever repaints what actually moves: the animated table
cells and the preview's score line. Rebuilding the whole preview pane and
re-evaluating the footer's key bindings ten times a second — which the
clock used to do — is what made the TUI feel laggy over a slow link
(issue #462); those now update at the ~2s data-poll rate and on cursor
moves instead.

A repaint ten times a second is also genuinely costly over some
connections, so Conductor detects one kind of remote session and turns
animation off automatically for it:

- **RDP**, detected from `SESSIONNAME` starting with `RDP-Tcp` (Windows
  names every Remote Desktop session that way; the physical console
  session is named plain `Console` and is not a match).

**SSH is deliberately not detected**, even though a slow SSH link is a
real reason to want animation off. The two transports are not comparable:
RDP renders on the remote machine and then diffs, encodes and ships
*changed pixel regions*, so its cost scales with pixels changed per second
and a churning text region is the worst case for it. SSH ships the ANSI
byte stream and your local terminal does the rendering, so its cost is a
few hundred bytes per frame — negligible, and measured as such. What
actually hurts is a *slow* link, and there is no signal for slow, only for
"SSH at all", which is usually a fast one. Set `CONDUCTOR_FLEET_NO_ANIM=1`
when your link genuinely is slow — that is also the answer for remote
transports with no reliable signal at all, such as VNC, Citrix, or xrdp.

Any path that disables animation — explicit `CONDUCTOR_FLEET_NO_ANIM` or
detection — also sets Textual's own `App.animation_level` to `"none"`,
which additionally stops Textual's built-in widget animations (for example
the tables' smooth-scroll easing) — a broader effect than the Fleet-specific
clock alone, and worth knowing if you were relying on that easing.

Two environment variables override this, and the *off* switch always
wins if both are set:

| Variable | Effect |
| --- | --- |
| `CONDUCTOR_FLEET_NO_ANIM` | Force animation off, regardless of session detection. Wins over `CONDUCTOR_FLEET_ANIM` if both are set. |
| `CONDUCTOR_FLEET_ANIM` | Force animation back on over a detected RDP session. |

```bash
# Force animation off (e.g. a slow SSH link, recording a terminal
# session, or on battery):
CONDUCTOR_FLEET_NO_ANIM=1 conductor fleet

# Force animation on over RDP, once you know the link can take it:
CONDUCTOR_FLEET_ANIM=1 conductor fleet
```

A detected remote session shows a one-time notification naming the
detected session type and the `CONDUCTOR_FLEET_ANIM=1` override; an
explicit `CONDUCTOR_FLEET_NO_ANIM` does not, since that path is already
the reader's own choice.

Three more Textual-level knobs — `TEXTUAL_FPS`, `TEXTUAL_SMOOTH_SCROLL`,
and `TEXTUAL_ANIMATIONS` — tune the framework's own rendering and are
deliberately **not** set by Conductor for you. They are read once, at
import time, as `Final` module constants (`textual/constants.py`), so
honoring them would mean setting them before `textual` is imported —
i.e. before every `conductor` invocation, not just `conductor fleet`.
Set them yourself in your shell environment if you want to tune them.

## Screens

The TUI uses Textual's `Screen` push/pop stack, so every drill-down has a
real Escape-to-return path. Screens push onto (and pop off) that same
stack rather than each managing its own navigation state.

- **Runs** (home) — every live run, sorted by recency, refreshed on a ~2s
  poll. Deliberately a **flat list**, not grouped by workflow definition:
  per the design's own prior-art lesson (Prefect), operators triage by
  which run needs attention, not by which file it came from. When nothing
  is running, this screen shows the launch affordance (`n` → New Run)
  rather than an empty table — the empty state is a first-class screen,
  not an afterthought. The poll's scan runs off the UI thread, and any
  tick arriving while the previous scan is still running is skipped rather
  than started alongside it, so the UI stays responsive even against a
  large fleet. If the scan itself fails, the screen says so rather than
  silently showing you a stale table or an empty-looking fleet.
- **Run detail** (`enter` on a Runs row) — topology from `workflow_started`,
  per-agent status/elapsed/tokens/cost from the event log, with the
  currently-running agent highlighted. Deliberately **not a DAG**: no agent
  messages, no tool output, no graph rendering — see
  [Division of labor](#division-of-labor-tui-vs-dashboard) for why that's
  the dashboard's job.
- **Step detail** (`enter` on a Run detail row) — what a single step
  actually did: its input, its output, and its activity stream (messages,
  reasoning, tool calls and their results). Loaded once on open and
  reloaded only on `r`, never on a timer — a drill-down that refreshed
  underneath the reader would move the text they were mid-way through.
- **Providers** (`p`) — a collapsed summary per provider (installed, tier,
  credential presence); expand a row to run an explicit, on-demand
  connection check and see its models with reasoning-effort and
  context-window limits. Consumes the same `providers/diagnostics.py`
  dataclasses `conductor doctor` does — the TUI never imports provider
  internals directly. Offline by default; a network-touching model check
  only ever happens on explicit expand, never on the poll timer.
- **Registries** (`r`) — configured registries → a registry's workflows →
  a workflow's inputs, three separately-pushed screens so Escape unwinds
  one level at a time. A malformed registry config is reported as an
  error, not silently presented as "no registries." Index/workflow loading
  can hit the network for a GitHub-backed registry, so it is always an
  explicit action, never on the poll timer. Pressing `n` on either of the
  two deeper screens opens **New run** with that workflow's reference
  already filled in and resolved, so launching what you are looking at
  does not mean escaping back out and retyping it.
- **New run** (`n`) — enter a file path or registry reference, resolve it,
  and fill in a form generated from the workflow's declared `input:`
  block (required fields marked, defaults pre-filled, descriptions shown).
  A relative reference resolves against the current
  [launch directory](#launch-directory) (`ctrl+d` to change it, from either
  this screen or Runs). Submitting shells out to `conductor run --web-bg`
  via the same `launch_background()` the CLI itself uses, so a launched run
  outlives the TUI rather than dying with it. Once the launch succeeds, this screen pops
  back to Runs, where the new run appears on the next poll tick; the TUI
  never tracks a launched run's lifecycle beyond that (**viewer, not
  supervisor**).
- **History** (`h`) — every retained run, subject to
  [retention](#retention), regardless of outcome. Enumerated directly
  from retained event logs under `$TMPDIR/conductor/`, not from run
  records (a completed run's record has already been removed). A log
  with no `workflow_completed`/`workflow_failed` terminal event is listed
  too, shown as **unknown**, never as "running" — a non-terminal log is
  not evidence of a live run, and a **currently-live** run is always
  excluded from Resume regardless of what checkpoint would otherwise
  correlate to it, since resuming it would make the new process adopt
  the live run's `run_id`, overwrite its run record, and interleave two
  processes' events into one log. Selecting a row surfaces the exact
  `conductor replay <log>` command rather than opening a viewer inside
  the TUI — depth, again, belongs to `replay`/the dashboard, not this
  screen. A row whose event log correlates to a checkpoint on disk also
  offers `r`/Resume: pressing it resumes that run in the background
  through the same `launch_background_resume` path `conductor resume
  --web-bg` uses, then returns to Runs — the one action this screen
  performs itself rather than delegating (replay stays a viewer;
  resuming a run is an action, like Runs killing a process or resolving
  a gate). The key is offered only when a checkpoint correlates to the
  row **and** its recorded workflow file still exists; availability is
  entirely **checkpoint-driven, never outcome-driven** — an `unknown` row
  with a periodic checkpoint offers Resume, while a `failed` row from an
  explicit `type: terminate` does not, because that step writes no
  checkpoint by design. A `completed` row can offer it too, which would
  re-execute already-finished (possibly billable) work; the notification
  shown before resuming names the checkpoint's save time and step so that
  choice is informed rather than hidden. In practice, Resume shows up
  mostly on `failed` rows, which always carry a failure checkpoint — an
  `unknown` row only offers it when the workflow opted into
  [periodic checkpoints](workflow-syntax.md#periodic-checkpoints)
  (`runtime.checkpoint`), which are off by default.

## Key bindings

Bindings shown are the Runs (home) screen's; each drill-down screen binds
`escape` to pop back to whatever pushed it.

| Key | Action |
|-----|--------|
| `enter` | Open run detail for the selected row |
| `w` | Open the selected run's dashboard in a browser |
| `k` | Kill the selected run (confirms first) |
| `K` | Kill every displayed run (confirms once) |
| `g` | Resolve the selected run's open gate (see [below](#gates-display-vs-resolve)) |
| `n` | New run |
| `d` | Change the [launch directory](#launch-directory) |
| `p` | Providers |
| `r` | Registries |
| `h` | History |
| `q` | Quit |

The Runs footer also hides the docked `^p palette` key to make room for the
above (`ctrl+p` still opens the command palette — only the footer key is
hidden, not the palette itself).

Screens with a row-scoped `enter` advertise it in their own footer:

| Screen | `enter` |
|--------|---------|
| Runs | Open run detail for the selected row |
| Run detail | Open step detail for the highlighted step |
| History | Surface `conductor replay <log>` for the selected row |
| Providers | Expand/collapse the highlighted provider |
| Registries | Open that registry's workflows |
| Registry workflows | Open that workflow's inputs |

On Providers, `enter` is not offered while a model/status sub-row is
highlighted — only a provider row itself can be expanded or collapsed.
The Step-detail screen that `enter` opens from Run detail binds `r` to
reload and `tab` to switch panes. On History, `r` resumes the
highlighted row's checkpoint in the background when a checkpoint is
available (see [above](#screens)).

Inside the Registries drill-down, `n` runs the highlighted (or currently
displayed) workflow rather than starting from an empty form. A launch
started from there returns to Runs — not to the screen it was launched
from — since that is where the new run actually shows up.

Kill always confirms — even a single `k` — per the design's *What
single-user removes*: "Kill-all safety interlocks | No risk of killing
another user's run; one confirm." A foreground run in scope is named
specifically in the confirmation, since a plain `SIGTERM` on a foreground
run discards in-flight progress unless periodic checkpoints are enabled
for it (the same warning `conductor stop`'s own confirmation shows — one
policy, two presentations, sharing the same underlying implementation).

## Launch directory

`d` (Runs) / `ctrl+d` (New run) opens a directory picker — type a path, or
browse a tree rooted at the current directory's parent (so a sibling
checkout is one keypress away) — and sets the TUI's **launch directory**
for the rest of this `conductor fleet` session. The input is pre-filled
with the current launch directory; browsing the tree updates it as you go,
and either pressing Enter in the input, or pressing Enter or clicking a
directory in the tree, accepts it.

The launch directory affects two things:

- A **relative** workflow reference on the New Run screen resolves against
  it, not against wherever `conductor fleet` happened to be started.
- A launched run's detached child inherits it as its working directory,
  which is what its Directory column shows and what a `type: script` step
  without an explicit `working_dir:` defaults to.

It does **not** affect `runtime.working_dir` / `agent.working_dir` or a
sub-workflow's own workflow-file reference — those always resolve against
the *workflow file's* directory, unrelated to where the TUI itself was
launched from or later pointed at. It is also not a filter: the Runs and
History screens always show the whole fleet, regardless of the current
launch directory.

**Process-lifetime only** — there is no `config.toml` key and no state file
behind it. It starts at `conductor fleet`'s own working directory every
time, and resets the moment this process exits.

## Status vocabulary

Status is a small explicit state machine, not a boolean:

`running` · `at-gate` · `paused` · `completed` · `failed`

`at-gate` is the highest-value cell in the Runs table — it's the reason to
look at the screen at all — and is rendered as a **persistent badge**
(`▲`), never a transient notification, for every run mode. A terminal bell
/ OSC 9 notification additionally fires once, on the transition *into*
`at-gate` or `failed` — debounced so a run that stays in that status
across multiple ~2s poll ticks doesn't re-notify on every tick. There is no
notification service beyond that: per the design's *What single-user
removes*, "Terminal bell / OSC 9 is the whole feature."

Token/cost totals shown across the Runs, run-detail, and History screens
are **completed-agent totals only** — there is no mid-flight usage event,
so a currently-running agent contributes nothing to the total until it
finishes. The columns are simply labelled `Tokens`/`Cost`, with no
in-column caveat, so a total mid-run may look lower than expected; this is
a deliberate v1 scope decision (no live token streaming), not a bug.

## Gates: display vs. resolve

A human gate is derived from `gate_presented`/`gate_resolved` in the JSONL
event log, which **every** run writes regardless of mode — so `at-gate` is
displayed for every run, with no exceptions. Whether it can be *resolved*
from the TUI depends on how the run was started:

| Mode | Dashboard port? | `g` behavior |
|------|:---:|--------------|
| `bg` (`--web-bg`) | Yes | Resolves via HTTP, same as `conductor gate respond` |
| `fg-web` (`conductor run --web`) | Yes | Resolves via HTTP, same as `conductor gate respond` |
| `fg` (plain `conductor run`) | No | Display-only: `▲ at-gate (terminal · PID <pid>)`, `g` disabled |

For any run with a dashboard port, `g` presents the gate's options and
posts the selection over the **existing** HTTP gate-respond endpoint —
the exact same code path `conductor gate respond` uses, including its
`CONDUCTOR_GATE_TOKEN` handling. No new endpoint, no new core resolution
code.

A plain foreground run's gate cannot be resolved remotely, and that is a
hard property of how it works today, not a gap this feature could have
closed: the gate blocks in a background thread around a synchronous
`Prompt.ask()`, and a thread blocked on a blocking prompt call cannot be
cancelled or redirected. Even a hypothetical new file-drop or socket
channel would leave that terminal prompt sitting there, still consuming
the next keystroke typed at it. The TUI marks such a row's PID so you know
which terminal to go answer the prompt in, and disables `g` with that
reason visible, rather than letting you press it and watch nothing happen.

**Because of this, `--web` is the recommended way to start anything you
intend to manage from the fleet view.** A run launched from the TUI's own
New Run screen is always `--web-bg`, so it is gate-resolvable by
construction — only a run you start yourself as a plain `conductor run`
loses that ability.

## Division of labor: TUI vs. dashboard

> **TUI = breadth. Web dashboard = depth.**
>
> The TUI answers *"what is happening across my fleet, and what needs
> me?"* The dashboard answers *"what exactly is this one run doing?"*

They compose: the TUI's per-run dashboard action (`w`) opens the browser at
that run's dashboard. No rendering logic is duplicated, and neither
surface grows into the other's job — this is also what keeps the TUI
cheap: it never renders a DAG, streams agent messages, or displays tool
output. The run-detail screen shows discrete per-agent steps (status,
elapsed, tokens, cost), never a graph; the History screen delegates replay
to `conductor replay <log>` rather than embedding a viewer.

## Terminal records

A run record (the JSON file backing `conductor stop` / `fleet list`
discovery above) is removed the moment its process exits — that's what
makes it a *live*-run primitive. To let a completed run's outcome still be
looked up by `run_id` afterward, `cli/run.py`'s `finally` block writes one
more small JSON file, a **terminal record**, to
`~/.conductor/runs/terminal/<run_id>.json` (or `$CONDUCTOR_HOME/runs/terminal/`
when that's set) immediately before removing the live record. It carries
the run's identifying fields plus its terminal status (`success` /
`failed`), rendered output, error type/message on failure, usage totals,
and the paths to its event log and (for a `--web-bg` run) captured
stderr/stdout logs — enough for `conductor status`, `conductor fleet
list`, and a future MCP `conductor_run_status` tool to answer "how did
that run finish" without the process still being alive to ask.

The `terminal/` subdirectory placement is deliberate, not cosmetic: the
live-record listing/removal functions in `fleet/records.py` glob
`run_records_dir()/*.json` non-recursively, so nothing filed a directory
down is ever mistaken for, or raced against, a live record.

⚠️ **A crashed or `kill -9`'d run produces no terminal record.** The
tombstone is written by the same graceful-shutdown path that would also
have written a final `workflow_completed`/`workflow_failed` event — a
process that never reaches its `finally` block leaves nothing behind.
`conductor_run_status`-style lookups for such a run will report it as
simply unknown, the same as a `run_id` that never existed.

Terminal records are pruned by the exact same `[fleet.retention]` sweep
described below, in the same pass as the event log they point at — see
[Retention](#retention) for the mechanics and `keep_last` semantics.

## Retention

`$TMPDIR/conductor/` accumulates one JSONL event log per run indefinitely
unless bounded. `~/.conductor/config.toml`'s `[fleet.retention]` table
controls an opportunistic sweep that runs at the start of every
`conductor run`/`resume`, keeping only the most-recent `keep_last` event
logs (default `200`) and never touching a log a live (or currently
resuming) run still references, nor the `checkpoints/` subdirectory that
also lives under the same root. The same `keep_last` also bounds
[terminal records](#terminal-records): a run's terminal record is pruned
or kept in the same pass as its event log, matched by `run_id`, so a
`run_id` resolves completely or not at all rather than a stale record
outliving the log it points at. A terminal record whose event log has
*already* disappeared (pruned by an earlier sweep, or reaped
independently) is bounded by a second pass over `terminal/` using that
same `keep_last`, newest-first by when the run actually ended. See
[Machine-Wide Settings](configuration.md#machine-wide-settings-conductorconfigtoml)
for the full settings reference, and `conductor fleet prune`
(documented in the [CLI reference](cli-reference.md#conductor-fleet-prune))
for the explicit manual entry point, which always works regardless of
whether the automatic sweep is enabled.

The History screen's list is bounded by this exact same setting — and,
independently, is never shown beyond a fixed 200-entry display cap even
when `keep_last` is configured below 1 ("unbounded" for the pruning sweep
itself, not for this display). There is no pagination: per the design's
*What single-user removes*, this deliberately omits long-horizon audit
history in favor of staying simple. Pruning an event log makes that
run's history permanently unavailable to both the History screen and
`conductor replay`, since both read the JSONL file directly.

The sweep never descends into the `checkpoints/` subdirectory, and
checkpoint rotation (`keep_last` on `runtime.checkpoint`) runs entirely
independently of event-log retention — the two are on separate clocks.
So a History row can outlive its checkpoint (an event log survives the
`keep_last` window longer than the checkpoint it once correlated to,
and Resume disappears from that row) and a checkpoint can just as
easily outlive its row (the event log gets pruned first, leaving a
checkpoint on disk with no History entry to resume it from).

## See Also

- [CLI Reference](cli-reference.md) — `conductor stop`, `conductor fleet
  list`, `conductor fleet prune`, `conductor gate respond`
- [Configuration](configuration.md#machine-wide-settings-conductorconfigtoml)
  — `~/.conductor/config.toml` and `[fleet.retention]`
- [Web Dashboard](../README.md#web-dashboard) — the "depth" half of the
  division of labor above
