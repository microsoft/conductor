"""Shared TUI actions: open dashboard, kill and kill-all (Fleet Manager E8),
plus gate resolution (Fleet Manager E13, D4).

Killing is deliberately **not** ``conductor fleet kill`` (per the design):
this module reuses the exact same stop/kill implementation
``conductor stop`` uses (:func:`conductor.cli.app.stop_records`) rather
than re-implementing signal-send and liveness-polling here. Per D1, the
TUI always confirms before killing anything -- unlike the CLI, which only
confirms when a foreground run is in scope -- via a Textual modal rather
than ``rich.prompt.Confirm``. The underlying kill mechanics
(:func:`~conductor.cli.app.stop_records`, including its E3-T10
verify-then-report contract) and the per-run checkpoint-status warning
text (:func:`~conductor.cli.app._foreground_stop_warning_lines`) are
shared with the CLI: one policy, two presentations.

Gate resolution follows the same "reuse the CLI's shared implementation"
principle (D4): :func:`resolve_gate` presents a gated run's options via
:class:`GateOptionsModal`, then posts the selection through the exact same
:func:`conductor.cli.gate._gate_respond_impl` ``conductor gate respond``
uses -- so ``CONDUCTOR_GATE_TOKEN`` handling and the ``/api/gate-status``
auto-discovery come along unchanged, with no second HTTP client. That
function is synchronous and blocks on ``httpx`` (5s/10s timeouts), so the
call runs in a Textual worker thread (``App.run_worker(..., thread=True)``)
rather than on the UI thread; its module-level ``stderr`` console is
temporarily redirected during the call so its progress/error text is
captured instead of being printed into the terminal underneath the TUI's
alternate screen buffer; and its ``typer.Exit`` (raised on every failure
path) is caught and translated into a :class:`GateResolveOutcome` rather
than propagating -- so a gate-respond HTTP failure surfaces in-UI, never
as an unhandled exception.
"""

from __future__ import annotations

import io
import logging
import os
import platform
import shutil
import subprocess
import threading
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from conductor.cli.app import StopOutcome, _foreground_stop_warning_lines, stop_records
from conductor.console import make_console
from conductor.fleet.records import RunRecord
from conductor.fleet.summary import GateInfo

if TYPE_CHECKING:
    from textual.app import App, ComposeResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dashboard (E8-T2)
# ---------------------------------------------------------------------------


def dashboard_url(record: RunRecord) -> str | None:
    """Return ``record``'s dashboard URL, or ``None`` if it has no dashboard.

    A ``mode == "fg"`` record has no port and therefore no dashboard to
    open -- see :func:`dashboard_disabled_reason` for the user-visible
    explanation of why the action is unavailable.
    """
    if record.port is None:
        return None
    return f"http://127.0.0.1:{record.port}"


def dashboard_disabled_reason(record: RunRecord) -> str | None:
    """Return why the dashboard action is unavailable for ``record``.

    Returns ``None`` when the action is available. Currently the only
    reason it wouldn't be: the run has no dashboard port (E8-T2).
    """
    if record.port is None:
        return "no dashboard for this run (foreground, no --web/--web-bg)"
    return None


def open_dashboard(record: RunRecord) -> bool:
    """Open ``record``'s dashboard in the default web browser (``w``, E8-T2).

    Returns ``False`` without opening anything for a portless record (a
    foreground run with no dashboard) rather than failing silently or
    raising -- the caller is responsible for surfacing *why* via
    :func:`dashboard_disabled_reason`.

    Args:
        record: The run whose dashboard to open.

    Returns:
        True if a browser open was attempted, False if this record has no
        dashboard.
    """
    url = dashboard_url(record)
    if url is None:
        return False
    return open_url(url)


def _is_wsl() -> bool:
    """Detect WSL, where a Linux browser opener usually cannot work.

    WSL has no X display and typically no real browser installed, so
    Python's ``webbrowser`` falls through to whatever generic handler it
    can find -- on a stock WSL2 image that is ``gio``, which answers
    ``Operation not supported`` and opens nothing. The browser that should
    receive the URL is on the Windows side.
    """
    if platform.system() != "Linux":
        return False
    if "microsoft" in platform.release().lower():
        return True
    # WSL1 kernels do not always carry "microsoft" in the release string,
    # but the interop env var is set in both.
    return bool(os.environ.get("WSL_DISTRO_NAME"))


def _wsl_open(url: str) -> bool:
    """Hand ``url`` to the Windows side from WSL. True if a handler ran.

    Tries ``wslview`` (from ``wslu``) first because it is the purpose-built
    tool and respects the user's configured browser, then falls back to
    PowerShell's ``Start-Process``, which is present on every WSL host.

    ``explorer.exe`` is deliberately not used even though it also works:
    it exits non-zero on success, so its return code cannot distinguish
    "opened" from "failed" -- which is the whole thing this function needs
    to report.
    """
    if shutil.which("wslview"):
        candidates = [["wslview", url]]
    else:
        candidates = [["powershell.exe", "-NoProfile", "-Command", "Start-Process", url]]

    for argv in candidates:
        try:
            # No shell: the URL is built from an int port, but a list-form
            # call keeps it that way regardless of what a record contains.
            result = subprocess.run(argv, capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError):
            logger.debug("WSL browser opener %s failed", argv[0], exc_info=True)
            continue
        if result.returncode == 0:
            return True
        logger.debug(
            "WSL browser opener %s exited %s: %s",
            argv[0],
            result.returncode,
            result.stderr.decode(errors="replace").strip(),
        )
    return False


def open_url(url: str) -> bool:
    """Open ``url`` in the user's browser. True if an opener reported success.

    Exists because ``webbrowser.open`` is not enough on WSL (see
    :func:`_is_wsl`) and because its own return value was previously
    discarded -- so a failed open reported success to the caller and the
    user was told the dashboard had been opened when nothing had happened.
    """
    if _is_wsl():
        return _wsl_open(url)
    try:
        return webbrowser.open(url)
    except Exception:  # noqa: BLE001 - a failed open must not kill the TUI
        logger.debug("webbrowser.open failed for %s", url, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Kill / kill-all confirmation (E8-T3, D1)
# ---------------------------------------------------------------------------


def build_kill_confirmation_message(targets: list[RunRecord]) -> str:
    """Build the kill-confirmation modal's message text for ``targets``.

    Per D1, the TUI confirms **always** before killing anything -- unlike
    the CLI, which only confirms when a foreground run is in scope -- so
    this is built and shown unconditionally by :func:`kill_runs`. Any
    foreground run in ``targets`` is specifically named with its
    checkpoint-recoverability status, reusing
    ``conductor.cli.app._foreground_stop_warning_lines`` -- the exact same
    per-run text ``conductor stop``'s own confirmation prompt shows (one
    policy, two presentations).

    Args:
        targets: The runs about to be killed.

    Returns:
        Plain-text message for the confirmation modal.
    """
    names = ", ".join(Path(r.workflow_path or "unknown").stem for r in targets)
    lines = [f"Kill {len(targets)} run(s): {names}?"]

    foreground = [r for r in targets if r.mode in {"fg", "fg-web"}]
    if foreground:
        lines.append("")
        lines.append("In-flight progress will be lost unless periodic checkpoints are enabled:")
        lines.extend(f"  - {line}" for line in _foreground_stop_warning_lines(foreground))

    return "\n".join(lines)


class ConfirmKillModal(ModalScreen[bool]):
    """A confirmation modal for killing one or more runs (D1, E8-T3).

    Dismisses with ``True`` on confirm (``y`` or the Confirm button),
    ``False`` on cancel (``n``, Escape, or the Cancel button).
    """

    DEFAULT_CSS = """
    ConfirmKillModal {
        align: center middle;
    }
    #confirm-dialog {
        /* Fixed, not `auto`. Two reasons, and both are load-bearing:
           `Static.DEFAULT_CSS` sets only `height: auto`, so its width falls
           back to `1fr` -- and an `auto`-width container whose children are
           all `1fr` resolves to 0, which is why this dialog rendered as an
           empty bordered box (#449). And a foreground run's warning line runs
           to ~100 characters, so an auto width would size to the longest line
           rather than wrapping. `max-height` for the same reason
           `GateOptionsModal` is bounded: a kill-all naming several foreground
           runs is routinely taller than the terminal. */
        width: 60;
        max-width: 90%;
        height: auto;
        max-height: 90%;
        border: thick $error;
        padding: 1 2;
    }
    #confirm-message-scroll {
        /* `auto` so a one-line confirm stays a small box, `max-height: 100%`
           so a long one scrolls inside the capped dialog instead of
           overflowing it -- without the cap the container grows past its
           parent and the tail is silently truncated with no scrollbar. */
        height: auto;
        max-height: 100%;
        scrollbar-size-vertical: 1;
    }
    #confirm-hint {
        /* Docked so the keys a user needs are the one thing that can never be
           pushed off the bottom by a long message. */
        dock: bottom;
        height: 1;
    }
    """

    BINDINGS = [
        ("y", "confirm", "Confirm"),
        ("n", "cancel", "Cancel"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        yield Vertical(
            VerticalScroll(
                # `Text`, not a plain str: `Static` defaults to markup=True,
                # so a workflow named `plan[wip].yaml` would be silently
                # rendered as `plan.yaml` -- in the dialog whose whole job is
                # naming what is about to be killed. The hint below is
                # conductor's own literal, so it stays markup.
                Static(Text(self._message), id="confirm-message"),
                id="confirm-message-scroll",
            ),
            Static("[bold]\\[y][/bold] Confirm   [bold]\\[n/esc][/bold] Cancel", id="confirm-hint"),
            id="confirm-dialog",
        )

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


def _silent_console() -> Console:
    """A Rich Console that discards its output.

    ``stop_records``'s underlying ``_stop_process`` prints CLI-style
    progress messages via Rich; writing those to the real terminal would
    corrupt Textual's alternate-screen rendering. The TUI surfaces its own
    feedback via ``Screen.notify`` instead, so this output is simply
    discarded.
    """
    return make_console(file=io.StringIO(), width=200)


async def kill_runs(app: App, targets: list[RunRecord]) -> StopOutcome:
    """Kill (stop) every run in ``targets``, always confirming first (D1, E8-T3).

    Confirms via :class:`ConfirmKillModal` -- awaited with
    ``app.push_screen_wait`` -- rather than ``rich.prompt.Confirm``, then
    delegates to the exact same :func:`conductor.cli.app.stop_records`
    ``conductor stop`` uses (E8-T1), with no ``confirm`` argument since
    confirmation has already happened here. Because ``stop_records``
    reuses :func:`conductor.cli.app._stop_process`'s verify-then-report
    contract (E3-T10), a run is only reported/counted as killed once its
    process is actually confirmed gone -- never on signal-send alone. Kill
    works purely by signal via the run record (E3-T9 makes ``SIGTERM``
    actually effective against a ``mode == "fg"`` run), independent of any
    dashboard or API being reachable.

    Args:
        app: The running Textual app (used to push the confirmation modal).
        targets: The run(s) to kill. An empty list is a no-op.

    Returns:
        A :class:`conductor.cli.app.StopOutcome`. ``declined=True`` means
        the user cancelled the confirmation modal; nothing was touched.
    """
    if not targets:
        return StopOutcome(declined=True, stopped=[])

    message = build_kill_confirmation_message(targets)
    confirmed = await app.push_screen_wait(ConfirmKillModal(message))
    if not confirmed:
        return StopOutcome(declined=True, stopped=[])

    return stop_records(targets, _silent_console())


# ---------------------------------------------------------------------------
# Gate resolution (Fleet Manager E13, D4)
# ---------------------------------------------------------------------------


def gate_resolve_disabled_reason(record: RunRecord) -> str | None:
    """Return why the gate-resolve action is unavailable for ``record``.

    Returns ``None`` when the action is available -- mirrors
    :func:`dashboard_disabled_reason`'s contract. Per D4, the only reason
    it wouldn't be: a plain foreground run (``mode == "fg"``) has no HTTP
    channel to reach it. Its gate blocks in ``asyncio.to_thread`` around a
    synchronous ``Prompt.ask`` (``gates/human.py:163-170``) -- a thread
    blocked in a blocking prompt call cannot be cancelled, so that gate can
    only be resolved at the terminal that owns it, identified by
    ``record.pid`` (D4 explicitly rejected adding a tenth, ``tty``, field
    to the run record -- the PID is sufficient to find the terminal).
    """
    if record.port is None:
        return f"foreground run, terminal-only -- resolve at PID {record.pid}"
    return None


def _option_label(gate: GateInfo, value: str) -> str:
    """Return the display label for one of ``gate``'s option values.

    Falls back to the raw value itself when ``option_details`` doesn't
    carry a ``label`` for it (e.g. an older event predating that field, or
    a value present only in the plain ``options`` list) -- a gate is
    always presentable even with a partial payload.
    """
    for detail in gate.option_details:
        if detail.get("value") == value:
            label = detail.get("label")
            if label:
                return str(label)
    return value


class GateOptionsModal(ModalScreen[str | None]):
    """Presents an open gate's prompt and options; dismisses with the
    selected option's value, or ``None`` if cancelled (D4, E13-T2).

    Mirrors :class:`ConfirmKillModal`'s presentation convention (a
    centered, bordered modal) but for a variable-length option list rather
    than a fixed yes/no confirm.
    """

    DEFAULT_CSS = """
    GateOptionsModal {
        align: center middle;
    }
    #gate-modal {
        /* Bounded, not `auto`: a real gate prompt is routinely longer than
           the terminal is tall (a plan-approval gate carries the whole
           plan), and an auto-sized modal simply grew past the screen --
           taking its own option list off the bottom with it. */
        width: 90%;
        max-width: 120;
        height: 80%;
        border: thick $warning;
        padding: 1 2;
    }
    #gate-prompt-scroll {
        /* 1fr: the prompt absorbs the modal's spare height, so the option
           list and hint below it stay visible no matter how long it is. */
        height: 1fr;
        scrollbar-size-vertical: 1;
    }
    #gate-option-list {
        height: auto;
        max-height: 40%;
        margin-top: 1;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        # Plain up/down belong to the OptionList (which holds focus, and is
        # how an option gets chosen), so scrolling the prompt needs its own
        # keys rather than stealing those.
        # Deliberately not pageup/pagedown: the OptionList holds focus and
        # binds those for its own option navigation, so a screen-level
        # binding never fires. ctrl+shift+arrows page instead.
        ("ctrl+down", "scroll_prompt_down", "Scroll"),
        ("ctrl+up", "scroll_prompt_up", "Scroll up"),
        ("ctrl+shift+down", "prompt_page_down", "Page down"),
        ("ctrl+shift+up", "prompt_page_up", "Page up"),
    ]

    def __init__(self, gate: GateInfo) -> None:
        super().__init__()
        self._gate = gate
        # OptionList requires unique widget ids; a gate's option *values*
        # are schema-valid but not guaranteed unique (and may not even be
        # legal Textual identifiers), so an opaque, index-based id is used
        # for each Option instead, mapped back to its real value here.
        self._option_values: dict[str, str] = {
            f"gate-option-{index}": value for index, value in enumerate(self._gate.options)
        }

    def compose(self) -> ComposeResult:
        yield Vertical(
            VerticalScroll(
                Static(self._gate.prompt, markup=False, id="gate-prompt"),
                id="gate-prompt-scroll",
                # A scroll container is focusable by default and would take
                # focus ahead of the option list, sending arrows and Enter to
                # the prompt instead of to the choices. The prompt is scrolled
                # by its own bindings (ctrl+arrows / page keys) precisely so it
                # never has to hold focus.
                can_focus=False,
            ),
            OptionList(
                *[
                    Option(Text(_option_label(self._gate, value)), id=option_id)
                    for option_id, value in self._option_values.items()
                ],
                id="gate-option-list",
            ),
            Static(
                Text.from_markup(
                    "[dim]↑↓ choose · enter select · ctrl+↑/↓ scroll · esc cancel[/dim]"
                ),
                id="gate-hint",
            ),
            id="gate-modal",
        )

    def on_mount(self) -> None:
        """Put focus on the options, which is what the user is here to pick."""
        self.query_one("#gate-option-list", OptionList).focus()

    def _prompt_scroll(self) -> VerticalScroll:
        return self.query_one("#gate-prompt-scroll", VerticalScroll)

    def action_scroll_prompt_down(self) -> None:
        """Scroll the prompt down a line -- bound to ctrl+down."""
        self._prompt_scroll().scroll_down()

    def action_scroll_prompt_up(self) -> None:
        """Scroll the prompt up a line -- bound to ctrl+up."""
        self._prompt_scroll().scroll_up()

    def action_prompt_page_down(self) -> None:
        """Scroll the prompt a page down -- bound to pagedown."""
        self._prompt_scroll().scroll_page_down()

    def action_prompt_page_up(self) -> None:
        """Scroll the prompt a page up -- bound to pageup."""
        self._prompt_scroll().scroll_page_up()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option_id
        value = self._option_values.get(option_id) if option_id is not None else None
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


@dataclass(frozen=True, slots=True)
class GateResolveOutcome:
    """Outcome of a :func:`resolve_gate` HTTP call.

    Attributes:
        success: True if ``_gate_respond_impl`` completed without raising
            ``typer.Exit``.
        message: The captured, markup-stripped text
            ``_gate_respond_impl`` printed (its own success or error
            message) -- never empty; a fallback is substituted if
            ``_gate_respond_impl`` printed nothing for some reason.
    """

    success: bool
    message: str


# ``_gate_respond_impl``'s module-level ``console`` is a shared, mutable
# global -- this lock serializes the swap/call/restore region in
# ``_resolve_gate_sync`` so two concurrent gate-resolve calls (e.g. a
# second ``g`` press racing an in-flight worker) can never interleave
# their console swaps, which would otherwise cross their captured output
# or leave the global console permanently pointed at a discarded
# ``io.StringIO`` (E13 review round 1).
_gate_console_lock = threading.Lock()


def _resolve_gate_sync(port: int, choice: str, agent: str) -> GateResolveOutcome:
    """Call :func:`conductor.cli.gate._gate_respond_impl` synchronously,
    capturing its console output and translating any failure -- a raised
    ``typer.Exit``, or any other unexpected exception from the HTTP path
    (e.g. a malformed 409/422 response body) -- into a
    :class:`GateResolveOutcome` instead of letting it propagate out of the
    worker thread (E13-T2).

    Must be called from a worker thread (:func:`resolve_gate` dispatches
    it via ``App.run_worker(..., thread=True)``) -- ``_gate_respond_impl``
    is synchronous and blocks on ``httpx`` calls (5s/10s timeouts), which
    would otherwise stall the Textual event loop.

    The swap/call/restore of ``_gate_respond_impl``'s module-level
    ``console`` global is serialized by :data:`_gate_console_lock` so a
    second, concurrent gate-resolve call blocks until this one has
    restored the original console, rather than racing it.

    Args:
        port: The gated run's dashboard port.
        choice: The selected option's value.
        agent: The gate's agent name (from ``GateInfo.agent_name``).

    Returns:
        A :class:`GateResolveOutcome` -- never raises.
    """
    import conductor.cli.gate as gate_module

    captured = make_console(file=io.StringIO(), width=200, no_color=True)
    with _gate_console_lock:
        original_console = gate_module.console
        gate_module.console = captured
        try:
            gate_module._gate_respond_impl(port, choice, agent, None, None)
            success = True
        except typer.Exit:
            success = False
        except Exception:
            # Any other exception from the HTTP path (a malformed
            # response body, a connection error not already translated
            # to typer.Exit, etc.) must still surface as an in-UI failure
            # outcome rather than escaping the worker thread.
            logger.warning("Unexpected error resolving gate on port %s", port, exc_info=True)
            success = False
        finally:
            gate_module.console = original_console

    message = captured.file.getvalue().strip()  # type: ignore[attr-defined]
    if not message:
        message = "Gate resolved." if success else "Gate resolution failed."
    return GateResolveOutcome(success=success, message=message)


async def resolve_gate(app: App, record: RunRecord, gate: GateInfo) -> GateResolveOutcome | None:
    """Resolve ``record``'s open gate via the shared ``conductor gate
    respond`` HTTP path (D4, E13-T2).

    Presents ``gate``'s options via :class:`GateOptionsModal` -- awaited
    with ``app.push_screen_wait`` -- then posts the selection through
    :func:`_resolve_gate_sync`, dispatched to a worker thread
    (``App.run_worker(..., thread=True)``) and awaited so the blocking
    ``httpx`` calls never stall the UI thread.

    Args:
        app: The running Textual app (used to push the options modal and
            run the worker).
        record: The gated run. Must have ``record.port is not None`` --
            callers check :func:`gate_resolve_disabled_reason` first (a
            ``mode == "fg"`` run has no HTTP channel to reach).
        gate: The gate's prompt/options, as carried on ``RunSummary.gate``.

    Returns:
        A :class:`GateResolveOutcome`, or ``None`` if the user cancelled
        the options modal (no HTTP call was attempted).
    """
    assert record.port is not None, "resolve_gate requires a run with a dashboard port"
    choice = await app.push_screen_wait(GateOptionsModal(gate))
    if choice is None:
        return None

    port = record.port
    worker = app.run_worker(lambda: _resolve_gate_sync(port, choice, gate.agent_name), thread=True)
    return await worker.wait()
