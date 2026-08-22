"""The Fleet Manager Textual ``App`` skeleton (Fleet Manager E7-T3).

Establishes the ``Screen`` push/pop stack the design requires for real
Escape-to-return drill-down (E9's run-detail screen and later screens push
onto this same stack rather than reinventing navigation) and pushes the
Runs (home) screen at startup. The Runs screen owns its own ~2s poll timer
(``RunsScreen.POLL_INTERVAL_SECONDS``); this module does not layer a second
one on top.
"""

from __future__ import annotations

import os
from pathlib import Path

from textual.app import App
from textual.reactive import reactive

from conductor.fleet.records import RunRecord
from conductor.fleet.tui.anim import animations_enabled, disabled_reason
from conductor.fleet.tui.screens.history import HistoryScreen
from conductor.fleet.tui.screens.new_run import NewRunScreen
from conductor.fleet.tui.screens.providers import ProvidersScreen
from conductor.fleet.tui.screens.registries import RegistriesScreen
from conductor.fleet.tui.screens.run_detail import RunDetailScreen
from conductor.fleet.tui.screens.runs import RunsScreen
from conductor.fleet.tui.screens.splash import SplashScreen
from conductor.fleet.tui.screens.step_detail import StepDetailScreen


def _initial_launch_dir() -> Path:
    """Return the process's current working directory at app startup.

    Falls back to the home directory on ``OSError`` -- the process cwd can
    be deleted out from under a running process, and ``engine/workflow.py``
    guards ``os.getcwd()`` the same way for the same reason.
    """
    try:
        return Path(os.getcwd())
    except OSError:
        return Path.home()


class FleetApp(App):
    """Conductor Fleet Manager TUI application (``conductor fleet``)."""

    TITLE = "Conductor Fleet"

    DEFAULT_THEME = "tokyo-night"
    """Applied in :meth:`on_mount`, **not** as a ``theme = ...`` class
    attribute: ``App.theme`` is a Textual ``reactive``, and assigning a
    plain string in the class body replaces that descriptor outright. The
    app still starts up looking themed, but nothing watches the attribute
    any more -- so picking a theme from the command palette silently does
    nothing. Set it on the instance instead, which goes *through* the
    reactive and leaves the palette working."""

    launch_dir: reactive[Path] = reactive[Path](_initial_launch_dir, init=False)
    """The directory new runs are launched from (issue #477): the base a
    relative workflow reference on the New Run screen resolves against, and
    the working directory a launched run's detached child inherits (hence
    its Directory column and any ``type: script`` step's default cwd).

    **Process-lifetime only.** There is no ``config.toml`` key and no state
    file backing this -- it starts at the process's cwd
    (:func:`_initial_launch_dir`) every time ``conductor fleet`` runs, and a
    change made via the footer's ``d``/``ctrl+d`` picker (see
    ``fleet/tui/actions.py::DirectoryPickerModal``) is gone the moment this
    process exits. It does **not** affect ``runtime.working_dir`` /
    ``agent.working_dir`` or sub-workflow references, which always resolve
    against the *workflow file's* directory, and it is not a filter on what
    Runs/History display -- the Fleet Manager always shows the whole fleet.

    A Textual ``reactive`` (not a plain attribute) so screens can
    ``self.watch(self.app, "launch_dir", callback)`` the same way
    ``textual.widgets._header.Header`` watches ``app.title``/
    ``screen.sub_title``. ``init=False`` because the callable default is
    evaluated once regardless, and re-running every registered watcher at
    startup (Textual's ``init=True`` default) would fire before a screen's
    own ``on_mount`` has registered anything to receive it."""

    CSS = """
    /* ------------------------------------------------------------------
       App-level design system.

       Styling lives here, not per-screen, so every table, heading and
       notice reads the same on every screen. A screen that needs a local
       rule is the exception; adding one is a decision, not a default.
       ------------------------------------------------------------------ */

    /* No `background:` here on purpose. Setting `$surface` paints a screen
       in the theme's *elevated* colour rather than its base background,
       which reads as a flat mid-grey haze under dim text. Screens default
       to `$background`; the surface colour is for things that sit on top. */

    /* Tables carry the app's data, so they get the padding the raw default
       lacks, and inherit the screen background rather than imposing their
       own lighter one. */
    DataTable {
        background: transparent;
        scrollbar-size-vertical: 1;
    }

    DataTable > .datatable--header {
        background: transparent;
        color: $text-accent;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: $primary 30%;
        color: $text;
        text-style: bold;
    }

    /* A screen's title bar: where you are, in one line, styled once here
       rather than re-invented per drill-down. */
    .screen-title {
        width: 100%;
        padding: 0 2;
        background: $panel;
        color: $text;
        text-style: bold;
    }

    /* The summary strip beneath a title (counts, totals). Muted, so it
       reads as context rather than as another row of data. */
    .summary-bar {
        width: 100%;
        height: auto;
        padding: 0 2;
        color: $text-muted;
    }

    /* A bordered region that groups related content -- the treatment that
       makes a table read as a panel rather than as loose text. */
    .panel {
        border: round $primary 40%;
        padding: 0 1;
        margin: 0 1;
    }

    /* Body text that is context rather than content. */
    .muted {
        color: $text-muted;
    }

    /* Empty states: centred and padded, rather than a paragraph abandoned
       in the top-left corner of an otherwise blank screen. */
    .empty-state {
        width: 100%;
        height: 1fr;
        content-align: center middle;
        padding: 2 4;
        color: $text-muted;
    }

    /* An inline error/notice line under a table or form. */
    .notice {
        width: 100%;
        height: auto;
        padding: 0 2;
    }
    """

    def on_mount(self) -> None:
        """Apply the default theme and push the Runs (home) screen (E7-T3)."""
        self.theme = self.DEFAULT_THEME
        self.push_screen(RunsScreen())
        # Pushed *on top of* Runs rather than before it, so the fleet scan
        # is already underway behind the splash and the home screen is fully
        # drawn the moment it pops -- the splash covers that work instead of
        # adding to it.
        if animations_enabled():
            self.push_screen(SplashScreen())
        else:
            # `animations_enabled()` already gates every spinner and the
            # splash above; this additionally stops Textual's *own*
            # widget animations (e.g. the tables' smooth-scroll easing),
            # which is not something the app's own frame clock owns.
            self.animation_level = "none"

        reason = disabled_reason()
        if reason is not None:
            # Only fires when *detection* is what disabled animation, not
            # an explicit `CONDUCTOR_FLEET_NO_ANIM` -- see
            # `anim.disabled_reason`'s docstring for why that distinction
            # matters. `markup=False`: `reason` is a runtime value and
            # Textual's `notify()` defaults to parsing it as markup (rule I).
            self.notify(
                f"Animation disabled: {reason} session detected. "
                "Set CONDUCTOR_FLEET_ANIM=1 to force it back on.",
                title="Fleet",
                markup=False,
            )

    def set_launch_dir(self, path: Path) -> None:
        """Set :attr:`launch_dir` -- the one named mutation site (issue #477).

        Called by ``fleet/tui/actions.py::change_launch_directory`` once the
        directory picker modal dismisses with a chosen, validated directory.
        Matches this module's ``push_*``/``return_to_runs`` convention of
        centralizing state mutation in one named method rather than having
        screens assign ``self.app.launch_dir`` directly.
        """
        self.launch_dir = path

    def push_run_detail(self, record: RunRecord) -> None:
        """Push the run-detail screen (E9) for ``record`` onto the screen stack.

        Kept as a method on the App -- rather than having the Runs screen
        import and construct :class:`RunDetailScreen` directly -- so screen
        construction and stack management for this drill-down stay
        centralized here (E9-T4), matching how :meth:`on_mount` already
        owns the initial ``RunsScreen`` push (E7-T3). ``RunDetailScreen``
        itself pops back to Runs via its own ``escape`` binding
        (``self.app.pop_screen()``), reusing the same stack.
        """
        self.push_screen(RunDetailScreen(record))

    def push_providers(self) -> None:
        """Push the Providers drill-down screen (E10) onto the screen stack.

        Mirrors :meth:`push_run_detail`'s centralization rationale (E10-T4)
        -- the Runs screen's ``p`` binding calls this rather than
        constructing :class:`ProvidersScreen` itself. ``ProvidersScreen``
        pops back to Runs via its own ``escape`` binding, reusing the same
        stack.
        """
        self.push_screen(ProvidersScreen())

    def push_registries(self) -> None:
        """Push the Registries drill-down screen (E11) onto the screen stack.

        Mirrors :meth:`push_providers`'s centralization rationale (E11-T4)
        -- the Runs screen's ``r`` binding calls this rather than
        constructing :class:`RegistriesScreen` itself. Deeper drill-down
        levels (a registry's workflows, a workflow's inputs) are pushed
        directly by ``registries.py`` itself rather than routed back
        through the app, since those transitions only ever originate from
        within that same screen module's own row-selection handlers.
        """
        self.push_screen(RegistriesScreen())

    def push_new_run(self, initial_ref: str | None = None) -> None:
        """Push the New-run screen (E12) onto the screen stack.

        Mirrors :meth:`push_registries`'s centralization rationale (E12-T4)
        -- the Runs screen's ``n`` binding calls this rather than
        constructing :class:`NewRunScreen` itself. ``NewRunScreen`` pops
        back to Runs itself on both ``escape`` and a successful launch,
        reusing the same stack; the newly-launched run then appears via
        the Runs screen's own next poll tick.

        Args:
            initial_ref: A workflow reference to pre-fill and resolve on
                mount. Passed by the Registries drill-down's own ``n``
                binding, so launching the workflow you are already looking
                at does not mean retyping a reference you just navigated
                through. Omitted (``None``) for the Runs screen's ``n``,
                which starts from an empty form.
        """
        self.push_screen(NewRunScreen(initial_ref))

    def push_step_detail(self, record: RunRecord, agent_name: str) -> None:
        """Push the step drill-down for one agent of ``record``.

        Mirrors the other push helpers' centralization rationale -- the
        run-detail screen's row selection calls this rather than
        constructing :class:`StepDetailScreen` itself.
        """
        self.push_screen(StepDetailScreen(record, agent_name))

    def push_history(self) -> None:
        """Push the History screen (E14) onto the screen stack.

        Mirrors :meth:`push_new_run`'s centralization rationale (E14-T3)
        -- the Runs screen's ``h`` binding calls this rather than
        constructing :class:`HistoryScreen` itself. ``HistoryScreen`` pops
        back to Runs via its own ``escape`` binding, reusing the same
        stack.
        """
        self.push_screen(HistoryScreen())

    def return_to_runs(self) -> None:
        """Unwind the screen stack back to the Runs (home) screen.

        A successful launch pops *to Runs* rather than popping one level,
        because the New-run screen can be reached from more than one depth:
        directly from Runs (``n``), or from two levels inside the Registries
        drill-down (its own ``n``). Popping a single screen from the latter
        lands back on a workflow's inputs, where the run that was just
        started is nowhere to be seen -- the opposite of the hand-off this
        is supposed to make ("the new run appears on the Runs screen's next
        poll tick").

        Pops by count rather than looping on ``screen_stack[-1]`` so a
        stack that somehow contains no ``RunsScreen`` cannot spin forever;
        in that case nothing is popped.
        """
        stack = self.screen_stack
        for depth, screen in enumerate(stack):
            if isinstance(screen, RunsScreen):
                for _ in range(len(stack) - depth - 1):
                    self.pop_screen()
                return
