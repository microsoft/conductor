"""The Fleet Manager Textual ``App`` skeleton (Fleet Manager E7-T3).

Establishes the ``Screen`` push/pop stack the design requires for real
Escape-to-return drill-down (E9's run-detail screen and later screens push
onto this same stack rather than reinventing navigation) and pushes the
Runs (home) screen at startup. The Runs screen owns its own ~2s poll timer
(``RunsScreen.POLL_INTERVAL_SECONDS``); this module does not layer a second
one on top.
"""

from __future__ import annotations

from textual.app import App

from conductor.fleet.records import RunRecord
from conductor.fleet.tui.screens.history import HistoryScreen
from conductor.fleet.tui.screens.new_run import NewRunScreen
from conductor.fleet.tui.screens.providers import ProvidersScreen
from conductor.fleet.tui.screens.registries import RegistriesScreen
from conductor.fleet.tui.screens.run_detail import RunDetailScreen
from conductor.fleet.tui.screens.runs import RunsScreen


class FleetApp(App):
    """Conductor Fleet Manager TUI application (``conductor fleet``)."""

    TITLE = "Conductor Fleet"

    def on_mount(self) -> None:
        """Push the Runs (home) screen onto the app's screen stack at startup."""
        self.push_screen(RunsScreen())

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
