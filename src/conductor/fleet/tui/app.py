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

    def push_new_run(self) -> None:
        """Push the New-run screen (E12) onto the screen stack.

        Mirrors :meth:`push_registries`'s centralization rationale (E12-T4)
        -- the Runs screen's ``n`` binding calls this rather than
        constructing :class:`NewRunScreen` itself. ``NewRunScreen`` pops
        back to Runs itself on both ``escape`` and a successful launch,
        reusing the same stack; the newly-launched run then appears via
        the Runs screen's own next poll tick.
        """
        self.push_screen(NewRunScreen())

    def push_history(self) -> None:
        """Push the History screen (E14) onto the screen stack.

        Mirrors :meth:`push_new_run`'s centralization rationale (E14-T3)
        -- the Runs screen's ``h`` binding calls this rather than
        constructing :class:`HistoryScreen` itself. ``HistoryScreen`` pops
        back to Runs via its own ``escape`` binding, reusing the same
        stack.
        """
        self.push_screen(HistoryScreen())
