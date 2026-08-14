# Fleet Manager

`conductor fleet` gives you one place to see, manage, and launch every
Conductor run on the machine — foreground, foreground+web, and
`--web-bg` alike. It fixes a specific bug (foreground runs were invisible
to `conductor stop`) and layers an optional interactive TUI on top of the
same run-discovery mechanism.

## Table of Contents

- [The problem this solves](#the-problem-this-solves)
- [Installing the TUI](#installing-the-tui)
- [Screens](#screens)
- [Key bindings](#key-bindings)
- [Status vocabulary](#status-vocabulary)
- [Gates: display vs. resolve](#gates-display-vs-resolve)
- [Division of labor: TUI vs. dashboard](#division-of-labor-tui-vs-dashboard)
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
  not an afterthought.
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
  Submitting shells out to `conductor run --web-bg` via the same
  `launch_background()` the CLI itself uses, so a launched run outlives
  the TUI rather than dying with it. Once the launch succeeds, this screen pops
  back to Runs, where the new run appears on the next poll tick; the TUI
  never tracks a launched run's lifecycle beyond that (**viewer, not
  supervisor**).
- **History** (`h`) — every retained run, subject to
  [retention](#retention), regardless of outcome. Enumerated directly
  from retained event logs under `$TMPDIR/conductor/`, not from run
  records (a completed run's record has already been removed). A log
  with no `workflow_completed`/`workflow_failed` terminal event is listed
  too, shown as **unknown**, never as "running" — a non-terminal log is
  not evidence of a live run. Selecting a row surfaces the exact
  `conductor replay <log>` command rather than opening a viewer inside
  the TUI — depth, again, belongs to `replay`/the dashboard, not this
  screen.

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
| `p` | Providers |
| `r` | Registries |
| `n` | New run |
| `h` | History |
| `q` | Quit |

On Run detail, `enter` opens the highlighted step; that screen binds `r`
to reload and `tab` to switch panes.

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

## Retention

`$TMPDIR/conductor/` accumulates one JSONL event log per run indefinitely
unless bounded. `~/.conductor/config.toml`'s `[fleet.retention]` table
controls an opportunistic sweep that runs at the start of every
`conductor run`/`resume`, keeping only the most-recent `keep_last` event
logs (default `200`) and never touching a log a live (or currently
resuming) run still references, nor the `checkpoints/` subdirectory that
also lives under the same root. See
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

## See Also

- [CLI Reference](cli-reference.md) — `conductor stop`, `conductor fleet
  list`, `conductor fleet prune`, `conductor gate respond`
- [Configuration](configuration.md#machine-wide-settings-conductorconfigtoml)
  — `~/.conductor/config.toml` and `[fleet.retention]`
- [Web Dashboard](../README.md#web-dashboard) — the "depth" half of the
  division of labor above
