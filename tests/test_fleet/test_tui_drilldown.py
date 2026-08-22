"""Pilot tests for the Fleet Manager TUI's Providers drill-down screen
(Fleet Manager E10).

Uses Textual's ``App.run_test()`` to drive a real (headless) instance of
:class:`~conductor.fleet.tui.app.FleetApp` with a stubbed
``conductor.providers.diagnostics.gather`` / ``gather_provider``, covering
E10-T5:

- ``p`` from the Runs screen pushes the Providers screen.
- Collapsed provider rows render offline (tier, credentials, install
  status) without any model count until explicitly checked.
- Expanding a provider triggers an explicit (network-implying) model check
  and then renders per-model reasoning-effort/context-window detail.
- An errored provider (``connection_error``/``models_error``) surfaces its
  error text instead of an empty list.
- ``escape`` returns to the Runs screen via the real screen stack.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from textual.widgets import DataTable, Input, Static

from conductor.fleet.tui.app import FleetApp
from conductor.fleet.tui.screens.new_run import NewRunScreen
from conductor.fleet.tui.screens.providers import ProvidersScreen
from conductor.fleet.tui.screens.registries import (
    RegistriesScreen,
    RegistryWorkflowsScreen,
    WorkflowInputsScreen,
)
from conductor.fleet.tui.screens.runs import RunsScreen
from conductor.providers.diagnostics import CredentialEnvVar, ModelDiagnostic, ProviderDiagnostic
from tests.test_fleet.conftest import settle

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def fleet_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect both the run-record directory and the legacy ``.pid`` directory.

    Mirrors the ``fleet_env`` fixture used across the other TUI pilot test
    modules -- the Providers screen itself never touches run records, but
    the app still mounts the Runs screen first, which does.
    """
    home = tmp_path / "conductor_home"
    home.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))

    legacy_dir = tmp_path / "legacy_runs"
    legacy_dir.mkdir()
    monkeypatch.setattr("conductor.cli.pid.pid_dir", lambda: legacy_dir)

    return home


def _diag(
    name: str,
    *,
    installed: bool = True,
    implemented: bool = True,
    tier: str | None = "stable",
    credential_env_vars: list[CredentialEnvVar] | None = None,
    credentials_optional: bool = False,
    checked: bool = False,
    connection_ok: bool | None = None,
    connection_error: str | None = None,
    models: list[ModelDiagnostic] | None = None,
    models_error: str | None = None,
) -> ProviderDiagnostic:
    return ProviderDiagnostic(
        name=name,
        installed=installed,
        implemented=implemented,
        tier=tier,
        credential_env_vars=credential_env_vars or [],
        credentials_optional=credentials_optional,
        checked=checked,
        connection_ok=connection_ok,
        connection_error=connection_error,
        models=models,
        models_error=models_error,
    )


class _FakeReport:
    """A minimal stand-in for ``DoctorReport`` -- only ``.providers`` is read."""

    def __init__(self, providers: list[ProviderDiagnostic]) -> None:
        self.providers = providers


async def _goto_providers(pilot) -> None:
    """Navigate from the (already-mounted) Runs screen to Providers."""
    await settle(pilot)
    await pilot.press("p")
    await settle(pilot)


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


class TestProvidersNavigation:
    async def test_p_pushes_providers_screen(self, fleet_env: Path) -> None:
        with patch(
            "conductor.fleet.tui.screens.providers.gather",
            new=AsyncMock(return_value=_FakeReport([_diag("copilot")])),
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                assert isinstance(app.screen, RunsScreen)
                await _goto_providers(pilot)

                assert isinstance(app.screen, ProvidersScreen)

    async def test_escape_returns_to_runs(self, fleet_env: Path) -> None:
        with patch(
            "conductor.fleet.tui.screens.providers.gather",
            new=AsyncMock(return_value=_FakeReport([_diag("copilot")])),
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_providers(pilot)
                assert isinstance(app.screen, ProvidersScreen)

                await pilot.press("escape")
                await settle(pilot)

                assert isinstance(app.screen, RunsScreen)


# ---------------------------------------------------------------------------
# Collapsed summary (offline by default)
# ---------------------------------------------------------------------------


class TestProvidersCollapsedSummary:
    async def test_offline_load_renders_without_checking_models(self, fleet_env: Path) -> None:
        """On mount, only the offline gather() (check=False) runs -- no
        model count is shown until the user explicitly expands."""
        fake_gather = AsyncMock(
            return_value=_FakeReport(
                [_diag("copilot", tier="stable"), _diag("claude", tier="stable")]
            )
        )
        with (
            patch("conductor.fleet.tui.screens.providers.gather", new=fake_gather),
            patch("conductor.fleet.tui.screens.providers.gather_provider") as fake_gather_provider,
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_providers(pilot)

                table = app.screen.query_one(DataTable)
                assert table.row_count == 2

                fake_gather.assert_called_once()
                # gather() was called with check=False, list_models=False.
                _args, kwargs = fake_gather.call_args
                assert kwargs.get("check") is False
                assert kwargs.get("list_models") is False
                # No per-provider model check happened yet.
                fake_gather_provider.assert_not_called()

                rows = [table.get_row_at(i) for i in range(2)]
                names = [r[0] for r in rows]
                assert any("copilot" in n for n in names)
                assert any("claude" in n for n in names)
                # Collapsed, unchecked models cell shows an action hint,
                # not a model count.
                for row in rows:
                    assert "check" in str(row[-1]).lower()

    async def test_tier_is_visible_and_experimental_marked(self, fleet_env: Path) -> None:
        with patch(
            "conductor.fleet.tui.screens.providers.gather",
            new=AsyncMock(
                return_value=_FakeReport(
                    [
                        _diag("copilot", tier="stable"),
                        _diag("aca", tier="experimental"),
                    ]
                )
            ),
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_providers(pilot)

                table = app.screen.query_one(DataTable)
                rows = [table.get_row_at(i) for i in range(table.row_count)]
                stable_row = next(r for r in rows if "copilot" in r[0])
                experimental_row = next(r for r in rows if "aca" in r[0])

                assert "stable" in stable_row[2]
                assert "experimental" in str(experimental_row[2]).lower()


# ---------------------------------------------------------------------------
# Expansion (explicit, non-blocking model check)
# ---------------------------------------------------------------------------


class TestProvidersExpansion:
    async def test_expanding_unchecked_provider_triggers_explicit_check(
        self, fleet_env: Path
    ) -> None:
        checked_diag = _diag(
            "copilot",
            checked=True,
            connection_ok=True,
            models=[
                ModelDiagnostic(
                    id="gpt-5",
                    supported_reasoning_efforts=["low", "medium", "high"],
                    default_reasoning_effort="medium",
                    max_context_window_tokens=200000,
                )
            ],
        )
        with (
            patch(
                "conductor.fleet.tui.screens.providers.gather",
                new=AsyncMock(return_value=_FakeReport([_diag("copilot", checked=False)])),
            ),
            patch(
                "conductor.fleet.tui.screens.providers.gather_provider",
                new=AsyncMock(return_value=checked_diag),
            ) as fake_gather_provider,
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_providers(pilot)

                table = app.screen.query_one(DataTable)
                table.move_cursor(row=0)
                await pilot.press("enter")
                await settle(pilot)

                # An explicit per-provider check was triggered (implies
                # network -- check=True, list_models=True).
                fake_gather_provider.assert_called_once_with(
                    "copilot", check=True, list_models=True
                )

                # Expanded rows now show the model's detail.
                rows = [table.get_row_at(i) for i in range(table.row_count)]
                assert any("gpt-5" in r[0] for r in rows)
                model_row = next(r for r in rows if "gpt-5" in r[0])
                assert "low" in model_row[2] and "medium" in model_row[2] and "high" in model_row[2]
                assert str(model_row[3]) == "medium"
                assert "200,000" in model_row[4]

    async def test_collapsing_an_expanded_provider_hides_model_rows(self, fleet_env: Path) -> None:
        checked_diag = _diag(
            "copilot",
            checked=True,
            connection_ok=True,
            models=[ModelDiagnostic(id="gpt-5")],
        )
        with (
            patch(
                "conductor.fleet.tui.screens.providers.gather",
                new=AsyncMock(return_value=_FakeReport([checked_diag])),
            ),
            patch("conductor.fleet.tui.screens.providers.gather_provider"),
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_providers(pilot)

                table = app.screen.query_one(DataTable)
                table.move_cursor(row=0)
                await pilot.press("enter")
                await settle(pilot)
                assert table.row_count == 2  # provider + 1 model row

                table.move_cursor(row=0)
                await pilot.press("enter")
                await settle(pilot)
                assert table.row_count == 1  # collapsed again

    async def test_expanding_already_checked_provider_does_not_recheck(
        self, fleet_env: Path
    ) -> None:
        checked_diag = _diag(
            "copilot", checked=True, connection_ok=True, models=[ModelDiagnostic(id="gpt-5")]
        )
        with (
            patch(
                "conductor.fleet.tui.screens.providers.gather",
                new=AsyncMock(return_value=_FakeReport([checked_diag])),
            ),
            patch("conductor.fleet.tui.screens.providers.gather_provider") as fake_gather_provider,
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_providers(pilot)

                table = app.screen.query_one(DataTable)
                table.move_cursor(row=0)
                await pilot.press("enter")
                await settle(pilot)

                fake_gather_provider.assert_not_called()

    async def test_expanding_unimplemented_provider_shows_terminal_state(
        self, fleet_env: Path
    ) -> None:
        """A provider with ``implemented=False`` can never become
        ``checked`` (``diagnostics.py`` returns it unchanged regardless of
        ``check``/``list_models``), so expanding it must render a terminal
        "not implemented" state rather than "enter to check"/"not checked
        yet" -- which would never resolve -- and must never launch the
        (network-implying) check worker."""
        with (
            patch(
                "conductor.fleet.tui.screens.providers.gather",
                new=AsyncMock(
                    return_value=_FakeReport([_diag("openai", implemented=False, installed=False)])
                ),
            ),
            patch("conductor.fleet.tui.screens.providers.gather_provider") as fake_gather_provider,
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_providers(pilot)

                table = app.screen.query_one(DataTable)
                collapsed_row = table.get_row_at(0)
                assert "not implemented" in collapsed_row[4]

                table.move_cursor(row=0)
                await pilot.press("enter")
                await settle(pilot)

                fake_gather_provider.assert_not_called()

                rows = [table.get_row_at(i) for i in range(table.row_count)]
                assert any("not implemented" in r[0] for r in rows)
                assert not any("not checked yet" in r[0] for r in rows)
                assert not any("enter to check" in r[4] for r in rows)

    async def test_model_listing_is_non_blocking(self, fleet_env: Path) -> None:
        """Expanding a provider awaits the check as a worker rather than
        blocking the event loop -- the app must stay responsive (able to
        process a subsequent key) while the check is in flight."""
        import asyncio

        check_started = asyncio.Event()
        release_check = asyncio.Event()

        async def _slow_check(name: str, *, check: bool, list_models: bool) -> ProviderDiagnostic:
            check_started.set()
            await release_check.wait()
            return _diag(name, checked=True, connection_ok=True, models=[])

        with (
            patch(
                "conductor.fleet.tui.screens.providers.gather",
                new=AsyncMock(return_value=_FakeReport([_diag("copilot", checked=False)])),
            ),
            patch("conductor.fleet.tui.screens.providers.gather_provider", new=_slow_check),
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_providers(pilot)

                table = app.screen.query_one(DataTable)
                table.move_cursor(row=0)
                await pilot.press("enter")
                # Not `settle`: the check worker is genuinely blocked on
                # `release_check` until line below, so waiting for all
                # workers here would deadlock.
                await pilot.pause()

                # The check is now in flight (not yet resolved) -- the app
                # must still process further input (e.g. escape) instead
                # of hanging.
                await asyncio.wait_for(check_started.wait(), timeout=2.0)
                await pilot.press("escape")
                await settle(pilot)
                assert isinstance(app.screen, RunsScreen)

                release_check.set()
                await settle(pilot)


# ---------------------------------------------------------------------------
# Errors surfaced, not rendered as emptiness
# ---------------------------------------------------------------------------


class TestProvidersErrors:
    async def test_connection_error_surfaced_in_collapsed_row(self, fleet_env: Path) -> None:
        with patch(
            "conductor.fleet.tui.screens.providers.gather",
            new=AsyncMock(
                return_value=_FakeReport(
                    [
                        _diag(
                            "copilot",
                            checked=True,
                            connection_ok=False,
                            connection_error="Network unreachable",
                        )
                    ]
                )
            ),
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_providers(pilot)

                table = app.screen.query_one(DataTable)
                row = table.get_row_at(0)
                assert "network unreachable" in str(row[-1]).lower()

    async def test_models_error_surfaced_when_expanded(self, fleet_env: Path) -> None:
        with (
            patch(
                "conductor.fleet.tui.screens.providers.gather",
                new=AsyncMock(
                    return_value=_FakeReport(
                        [
                            _diag(
                                "copilot",
                                checked=True,
                                connection_ok=True,
                                models_error="Failed to list models: 500",
                            )
                        ]
                    )
                ),
            ),
            patch("conductor.fleet.tui.screens.providers.gather_provider"),
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_providers(pilot)

                table = app.screen.query_one(DataTable)
                # Collapsed row already surfaces the error.
                assert "failed to list models" in str(table.get_row_at(0)[-1]).lower()

                table.move_cursor(row=0)
                await pilot.press("enter")
                await settle(pilot)

                rows = [table.get_row_at(i) for i in range(table.row_count)]
                assert any("failed to list models" in str(r[0]).lower() for r in rows)


# ---------------------------------------------------------------------------
# Terminal states (checked, no error message) are not misrendered as
# "unchecked" -- distinguish failed/unavailable/genuinely-unchecked (E10-T2).
# ---------------------------------------------------------------------------


class TestProvidersTerminalStates:
    async def test_checked_connection_failed_without_error_shows_failed(
        self, fleet_env: Path
    ) -> None:
        """``checked=True`` with ``connection_ok=False`` and no
        ``connection_error`` is a normal failed-connection outcome -- it
        must not render as "enter to check"/"not checked yet", which would
        wrongly imply the check never ran (and could invite a retry)."""
        with patch(
            "conductor.fleet.tui.screens.providers.gather",
            new=AsyncMock(
                return_value=_FakeReport([_diag("copilot", checked=True, connection_ok=False)])
            ),
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_providers(pilot)

                table = app.screen.query_one(DataTable)
                cell = str(table.get_row_at(0)[-1]).lower()
                assert "connection failed" in cell
                assert "enter to check" not in cell
                assert "not checked yet" not in cell

                table.move_cursor(row=0)
                await pilot.press("enter")
                await settle(pilot)
                rows = [table.get_row_at(i) for i in range(table.row_count)]
                assert any("connection failed" in str(r[0]).lower() for r in rows)
                assert not any("not checked yet" in str(r[0]).lower() for r in rows)

    async def test_checked_models_none_renders_n_a(self, fleet_env: Path) -> None:
        """A checked provider whose ``models`` is ``None`` (listing not
        enumerated) renders ``n/a``, not the unchecked hint."""
        with patch(
            "conductor.fleet.tui.screens.providers.gather",
            new=AsyncMock(
                return_value=_FakeReport(
                    [_diag("copilot", checked=True, connection_ok=True, models=None)]
                )
            ),
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_providers(pilot)

                table = app.screen.query_one(DataTable)
                cell = str(table.get_row_at(0)[-1]).lower()
                assert "n/a" in cell
                assert "enter to check" not in cell

                table.move_cursor(row=0)
                await pilot.press("enter")
                await settle(pilot)
                rows = [table.get_row_at(i) for i in range(table.row_count)]
                assert any("n/a" in str(r[0]).lower() for r in rows)
                assert not any("not checked yet" in str(r[0]).lower() for r in rows)


# ---------------------------------------------------------------------------
# Collapsed model count (E10-T5): a checked-then-collapsed provider still
# shows its model count in the collapsed row.
# ---------------------------------------------------------------------------


class TestProvidersCollapsedCount:
    async def test_collapsed_row_shows_model_count_after_check(self, fleet_env: Path) -> None:
        checked_diag = _diag(
            "copilot",
            checked=True,
            connection_ok=True,
            models=[ModelDiagnostic(id="gpt-5"), ModelDiagnostic(id="gpt-4")],
        )
        with (
            patch(
                "conductor.fleet.tui.screens.providers.gather",
                new=AsyncMock(return_value=_FakeReport([_diag("copilot", checked=False)])),
            ),
            patch(
                "conductor.fleet.tui.screens.providers.gather_provider",
                new=AsyncMock(return_value=checked_diag),
            ),
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_providers(pilot)

                table = app.screen.query_one(DataTable)
                table.move_cursor(row=0)
                await pilot.press("enter")  # expand -> triggers the check
                await settle(pilot)
                table.move_cursor(row=0)
                await pilot.press("enter")  # collapse again
                await settle(pilot)

                assert table.row_count == 1
                row = table.get_row_at(0)
                assert "2 models" in row[-1]


# ---------------------------------------------------------------------------
# `enter` footer advertisement (issue #459)
# ---------------------------------------------------------------------------


class TestProvidersFooter:
    """`enter` must both toggle expand/collapse *and* actually appear in
    the footer -- `DataTable` binds `enter` itself (`show=False`), so
    without `priority=True` the screen's own binding is shadowed and the
    key silently vanishes from the footer while it still works (see
    `runs.py`'s identical `Detail` binding, which this mirrors)."""

    async def test_expand_collapse_binding_survives_datatables_own_enter(
        self, fleet_env: Path
    ) -> None:
        with patch(
            "conductor.fleet.tui.screens.providers.gather",
            new=AsyncMock(return_value=_FakeReport([_diag("copilot")])),
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_providers(pilot)

                shown = [
                    ab.binding.description
                    for ab in app.screen.active_bindings.values()
                    if ab.binding.show and ab.enabled
                ]
                assert "Expand/Collapse" in shown

    async def test_enter_expands_exactly_once(self, fleet_env: Path) -> None:
        """`enter` is bound twice over -- `DataTable`'s own hidden binding
        and the screen's visible one -- so prove the two paths stay
        mutually exclusive and don't fire the check twice."""
        checked_diag = _diag(
            "copilot",
            checked=True,
            connection_ok=True,
            models=[ModelDiagnostic(id="gpt-5")],
        )
        with (
            patch(
                "conductor.fleet.tui.screens.providers.gather",
                new=AsyncMock(return_value=_FakeReport([_diag("copilot", checked=False)])),
            ),
            patch(
                "conductor.fleet.tui.screens.providers.gather_provider",
                new=AsyncMock(return_value=checked_diag),
            ) as fake_gather_provider,
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_providers(pilot)

                table = app.screen.query_one(DataTable)
                table.move_cursor(row=0)
                await pilot.press("enter")
                await settle(pilot)

                fake_gather_provider.assert_called_once_with(
                    "copilot", check=True, list_models=True
                )
                assert table.row_count == 2  # provider + 1 model row

    async def test_mouse_click_still_expands(self, fleet_env: Path) -> None:
        """A mouse click posts `RowSelected` directly (rather than going
        through the `priority` screen binding), so this exercises
        `on_data_table_row_selected` -- the other of the two paths that
        must both funnel into the same toggle."""
        checked_diag = _diag(
            "copilot",
            checked=True,
            connection_ok=True,
            models=[ModelDiagnostic(id="gpt-5")],
        )
        with (
            patch(
                "conductor.fleet.tui.screens.providers.gather",
                new=AsyncMock(return_value=_FakeReport([_diag("copilot", checked=False)])),
            ),
            patch(
                "conductor.fleet.tui.screens.providers.gather_provider",
                new=AsyncMock(return_value=checked_diag),
            ) as fake_gather_provider,
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_providers(pilot)

                table = app.screen.query_one(DataTable)
                table.move_cursor(row=0)
                row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
                app.screen.post_message(DataTable.RowSelected(table, 0, row_key))
                await settle(pilot)

                fake_gather_provider.assert_called_once_with(
                    "copilot", check=True, list_models=True
                )
                assert table.row_count == 2  # provider + 1 model row

    async def test_hidden_with_a_model_sub_row_highlighted(self, fleet_env: Path) -> None:
        """The acceptance criterion: `enter` is not offered while a model
        sub-row is highlighted, since a sub-row has no expand/collapse
        action of its own."""
        checked_diag = _diag(
            "copilot",
            checked=True,
            connection_ok=True,
            models=[ModelDiagnostic(id="gpt-5")],
        )
        with (
            patch(
                "conductor.fleet.tui.screens.providers.gather",
                new=AsyncMock(return_value=_FakeReport([checked_diag])),
            ),
            patch("conductor.fleet.tui.screens.providers.gather_provider"),
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_providers(pilot)

                table = app.screen.query_one(DataTable)
                table.move_cursor(row=0)
                await pilot.press("enter")  # expand
                await settle(pilot)
                assert table.row_count == 2  # provider + 1 model row

                table.move_cursor(row=1)  # the model sub-row
                await pilot.pause()

                assert app.screen.check_action("toggle_provider", ()) is False
                shown = [
                    ab.binding.description
                    for ab in app.screen.active_bindings.values()
                    if ab.binding.show and ab.enabled
                ]
                assert "Expand/Collapse" not in shown

    async def test_second_enter_collapses_the_same_provider_just_expanded(
        self, fleet_env: Path
    ) -> None:
        """Regression test for the `_render_table` cursor-reset bug:
        `DataTable.clear()` unconditionally resets `cursor_coordinate` to
        row 0, so without restoring the cursor by row KEY after a rebuild,
        expanding a non-first provider (`charlie`, row 2) would leave the
        cursor on row 0 (`alpha`) -- and a second `enter` would collapse
        the WRONG provider instead of the one just expanded."""
        checked_diag = _diag(
            "charlie", checked=True, connection_ok=True, models=[ModelDiagnostic(id="m1")]
        )
        diags = [_diag("alpha", checked=True), _diag("bravo", checked=True), checked_diag]
        with (
            patch(
                "conductor.fleet.tui.screens.providers.gather",
                new=AsyncMock(return_value=_FakeReport(diags)),
            ),
            patch(
                "conductor.fleet.tui.screens.providers.gather_provider",
                new=AsyncMock(return_value=checked_diag),
            ),
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_providers(pilot)

                table = app.screen.query_one(DataTable)
                assert table.row_count == 3
                table.move_cursor(row=2)  # charlie

                await pilot.press("enter")  # expand charlie
                await settle(pilot)

                assert app.screen._expanded == {"charlie"}
                # The cursor must follow the row it was on, not snap back
                # to row 0 -- `clear()`'s own default behaviour.
                key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
                assert key == "charlie"

                await pilot.press("enter")  # collapse -- must still be charlie
                await settle(pilot)

                assert app.screen._expanded == set()


# ---------------------------------------------------------------------------
# Registries drill-down (Fleet Manager E11)
# ---------------------------------------------------------------------------
#
# Uses a real, temp path-backed registry (not a stubbed diagnostics layer)
# per E11-T5 -- registries → workflows → inputs is exercised through the
# actual reuse targets (``registry/config.py``, ``registry/index.py``,
# ``registry/cache.py``, ``config/loader.py``), only ``CONDUCTOR_HOME`` is
# redirected.

_SIMPLE_WORKFLOW_YAML = """\
workflow:
  name: test-workflow
  description: A minimal workflow for drill-down tests
  entry_point: helper
  input:
    question:
      type: string
      required: true
      description: The question to answer
    verbose:
      type: boolean
      required: false
      default: false
      description: Enable verbose output

agents:
  - name: helper
    model: copilot
    prompt: Say hello
"""

_NO_INPUT_WORKFLOW_YAML = """\
workflow:
  name: no-input-workflow
  entry_point: helper

agents:
  - name: helper
    model: copilot
    prompt: Say hello
"""


def _write_local_registry(root: Path, *, with_inputs: bool = True) -> Path:
    """Build a minimal local (path-type) registry directory with one workflow."""
    from ruamel.yaml import YAML

    registry_dir = root / "registry"
    registry_dir.mkdir(parents=True, exist_ok=True)

    workflow_content = _SIMPLE_WORKFLOW_YAML if with_inputs else _NO_INPUT_WORKFLOW_YAML
    (registry_dir / "workflow.yaml").write_text(workflow_content)

    yaml = YAML()
    index_data = {
        "workflows": {
            "test-workflow": {"description": "A test workflow", "path": "workflow.yaml"},
        }
    }
    with open(registry_dir / "index.yaml", "w") as f:
        yaml.dump(index_data, f)

    return registry_dir


def _configure_registry(registry_dir: Path, *, name: str = "my-reg") -> None:
    from conductor.registry.config import RegistryType, add_registry

    add_registry(name, str(registry_dir), registry_type=RegistryType.path, set_default=True)


async def _goto_registries(pilot) -> None:
    """Navigate from the (already-mounted) Runs screen to Registries."""
    await settle(pilot)
    await pilot.press("r")
    await settle(pilot)


# ---------------------------------------------------------------------------
# Registries list
# ---------------------------------------------------------------------------


class TestRegistriesNavigation:
    async def test_r_pushes_registries_screen(self, fleet_env: Path) -> None:
        _configure_registry(_write_local_registry(fleet_env))

        app = FleetApp()
        async with app.run_test() as pilot:
            assert isinstance(app.screen, RunsScreen)
            await _goto_registries(pilot)

            assert isinstance(app.screen, RegistriesScreen)

    async def test_escape_returns_to_runs(self, fleet_env: Path) -> None:
        _configure_registry(_write_local_registry(fleet_env))

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_registries(pilot)
            assert isinstance(app.screen, RegistriesScreen)

            await pilot.press("escape")
            await settle(pilot)

            assert isinstance(app.screen, RunsScreen)


class TestRegistriesList:
    async def test_lists_configured_registry(self, fleet_env: Path) -> None:
        _configure_registry(_write_local_registry(fleet_env), name="my-reg")

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_registries(pilot)

            table = app.screen.query_one(DataTable)
            assert table.row_count == 1
            row = table.get_row_at(0)
            assert str(row[0]) == "my-reg"
            assert str(row[1]) == "path"
            assert row[3] == "✓"  # default marker

    async def test_no_registries_configured_is_not_an_error(self, fleet_env: Path) -> None:
        """A genuinely empty config renders a dim message, not an error."""
        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_registries(pilot)

            table = app.screen.query_one(DataTable)
            assert table.display is False
            message = app.screen.query_one("#registries-message", Static)
            text = str(message.render())
            assert "no registries" in text.lower()
            assert "error" not in text.lower()

    async def test_malformed_registry_config_surfaces_error(self, fleet_env: Path) -> None:
        """A malformed ``registries.toml`` is reported as an error, not as
        "no registries" (E11-T1 acceptance criterion)."""
        (fleet_env / "registries.toml").write_text("this is not [ valid toml")

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_registries(pilot)

            table = app.screen.query_one(DataTable)
            assert table.display is False
            message = app.screen.query_one("#registries-message", Static)
            text = str(message.render()).lower()
            assert "failed to load registries" in text
            assert "no registries" not in text


# ---------------------------------------------------------------------------
# Workflows drill-down
# ---------------------------------------------------------------------------


class TestRegistryWorkflowsDrilldown:
    async def test_selecting_registry_pushes_workflows_screen(self, fleet_env: Path) -> None:
        _configure_registry(_write_local_registry(fleet_env))

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_registries(pilot)

            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)
            await pilot.press("enter")
            await settle(pilot)

            assert isinstance(app.screen, RegistryWorkflowsScreen)

            wf_table = app.screen.query_one(DataTable)
            assert wf_table.row_count == 1
            row = wf_table.get_row_at(0)
            assert str(row[0]) == "test-workflow"
            assert str(row[1]) == "A test workflow"

    async def test_escape_unwinds_one_level_to_registries(self, fleet_env: Path) -> None:
        _configure_registry(_write_local_registry(fleet_env))

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_registries(pilot)
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)
            await pilot.press("enter")
            await settle(pilot)
            assert isinstance(app.screen, RegistryWorkflowsScreen)

            await pilot.press("escape")
            await settle(pilot)

            assert isinstance(app.screen, RegistriesScreen)

    async def test_malformed_index_surfaces_error_not_empty(self, fleet_env: Path) -> None:
        registry_dir = fleet_env / "registry"
        registry_dir.mkdir(parents=True, exist_ok=True)
        (registry_dir / "index.yaml").write_text("not: valid: yaml: [")
        _configure_registry(registry_dir)

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_registries(pilot)
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)
            await pilot.press("enter")
            await settle(pilot)

            assert isinstance(app.screen, RegistryWorkflowsScreen)
            wf_table = app.screen.query_one(DataTable)
            assert wf_table.display is False
            message = app.screen.query_one("#workflows-message", Static)
            assert "failed to load workflows" in str(message.render()).lower()


# ---------------------------------------------------------------------------
# Inputs drill-down
# ---------------------------------------------------------------------------


class TestWorkflowInputsDrilldown:
    async def test_selecting_workflow_pushes_inputs_screen(self, fleet_env: Path) -> None:
        _configure_registry(_write_local_registry(fleet_env))

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_registries(pilot)
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)
            await pilot.press("enter")
            await settle(pilot)

            wf_table = app.screen.query_one(DataTable)
            wf_table.move_cursor(row=0)
            await pilot.press("enter")
            await settle(pilot)

            assert isinstance(app.screen, WorkflowInputsScreen)

            inputs_table = app.screen.query_one(DataTable)
            assert inputs_table.row_count == 2
            rows = {str(r[0]): r for r in (inputs_table.get_row_at(i) for i in range(2))}
            question_row = rows["question"]
            assert str(question_row[1]) == "string"
            assert str(question_row[2]) == "✓"
            assert str(question_row[3]) == "-"
            assert str(question_row[4]) == "The question to answer"

            verbose_row = rows["verbose"]
            assert str(verbose_row[1]) == "boolean"
            assert str(verbose_row[2]) == ""
            assert str(verbose_row[3]) == "False"

    async def test_workflow_with_no_inputs_shows_normal_message(self, fleet_env: Path) -> None:
        _configure_registry(_write_local_registry(fleet_env, with_inputs=False))

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_registries(pilot)
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)
            await pilot.press("enter")
            await settle(pilot)

            wf_table = app.screen.query_one(DataTable)
            wf_table.move_cursor(row=0)
            await pilot.press("enter")
            await settle(pilot)

            assert isinstance(app.screen, WorkflowInputsScreen)
            inputs_table = app.screen.query_one(DataTable)
            assert inputs_table.display is False
            message = app.screen.query_one("#inputs-message", Static)
            text = str(message.render()).lower()
            assert "no inputs" in text
            assert "error" not in text

    async def test_escape_unwinds_one_level_at_a_time(self, fleet_env: Path) -> None:
        """Escape steps back through the full stack: inputs -> workflows ->
        registries -> runs, one level per press (E11 acceptance criterion)."""
        _configure_registry(_write_local_registry(fleet_env))

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_registries(pilot)
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)
            await pilot.press("enter")
            await settle(pilot)

            wf_table = app.screen.query_one(DataTable)
            wf_table.move_cursor(row=0)
            await pilot.press("enter")
            await settle(pilot)
            assert isinstance(app.screen, WorkflowInputsScreen)

            await pilot.press("escape")
            await settle(pilot)
            assert isinstance(app.screen, RegistryWorkflowsScreen)

            await pilot.press("escape")
            await settle(pilot)
            assert isinstance(app.screen, RegistriesScreen)

            await pilot.press("escape")
            await settle(pilot)
            assert isinstance(app.screen, RunsScreen)


# ---------------------------------------------------------------------------
# Markup-safety regression coverage
# ---------------------------------------------------------------------------
#
# Registry/workflow/input values are data, not authored Rich markup -- a
# name/description/source/default containing e.g. "[/bold]" must render as
# literal text, never raise ``rich.errors.MarkupError`` and crash the
# screen. Row *keys* stay the raw, unescaped value (only the row keys are
# used to look up state, never rendered as markup).


class TestRegistriesMarkupSafety:
    async def test_registry_row_escapes_markup_like_values(self, fleet_env: Path) -> None:
        from conductor.providers.diagnostics import RegistryDiagnostic, RegistryInfo

        malicious = RegistryInfo(
            name="my-reg", type="path", source="[/bold]evil[/red]", is_default=True
        )
        with patch(
            "conductor.fleet.tui.screens.registries.gather_registries",
            return_value=RegistryDiagnostic(default="my-reg", registries=[malicious]),
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await _goto_registries(pilot)

                table = app.screen.query_one(DataTable)
                assert table.row_count == 1
                row = table.get_row_at(0)
                # Raised no MarkupError, and the escaped cell renders back to
                # the literal source text rather than being interpreted as markup.
                assert row[2].plain == "[/bold]evil[/red]"

    async def test_workflow_row_escapes_markup_like_description(self, fleet_env: Path) -> None:
        from ruamel.yaml import YAML

        registry_dir = fleet_env / "registry"
        registry_dir.mkdir(parents=True, exist_ok=True)
        (registry_dir / "workflow.yaml").write_text(_SIMPLE_WORKFLOW_YAML)
        yaml = YAML()
        index_data = {
            "workflows": {
                "test-workflow": {"description": "[/bold]evil[/red]", "path": "workflow.yaml"},
            }
        }
        with open(registry_dir / "index.yaml", "w") as f:
            yaml.dump(index_data, f)
        _configure_registry(registry_dir)

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_registries(pilot)
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)
            await pilot.press("enter")
            await settle(pilot)

            assert isinstance(app.screen, RegistryWorkflowsScreen)
            wf_table = app.screen.query_one(DataTable)
            assert wf_table.row_count == 1
            row = wf_table.get_row_at(0)

            assert row[1].plain == "[/bold]evil[/red]"

    async def test_empty_workflows_message_escapes_markup_like_registry_name(
        self, fleet_env: Path
    ) -> None:
        """The 'No workflows found' message interpolates the registry name --
        a name containing markup-like text must render literally rather than
        raising MarkupError (E11 review round 2)."""
        from conductor.registry.config import RegistryEntry, RegistryType

        registry_dir = fleet_env / "registry"
        registry_dir.mkdir(parents=True, exist_ok=True)
        (registry_dir / "index.yaml").write_text("workflows: {}\n")

        registry_name = "evil[bold]name"
        with patch(
            "conductor.fleet.tui.screens.registries.get_registry",
            return_value=RegistryEntry(type=RegistryType.path, source=str(registry_dir)),
        ):
            app = FleetApp()
            async with app.run_test() as pilot:
                await settle(pilot)
                await app.push_screen(RegistryWorkflowsScreen(registry_name))
                await settle(pilot)

                assert isinstance(app.screen, RegistryWorkflowsScreen)
                wf_table = app.screen.query_one(DataTable)
                assert wf_table.display is False
                message = app.screen.query_one("#workflows-message", Static)
                text = str(message.render())
                assert "evil[bold]name" in text
                assert "no workflows found" in text.lower()

    async def test_input_row_escapes_markup_like_description_and_default(
        self, fleet_env: Path
    ) -> None:
        workflow_yaml = """\
workflow:
  name: test-workflow
  entry_point: helper
  input:
    question:
      type: string
      required: true
      default: "[/bold]evil default[/red]"
      description: "[/bold]evil description[/red]"

agents:
  - name: helper
    model: copilot
    prompt: Say hello
"""
        from ruamel.yaml import YAML

        registry_dir = fleet_env / "registry"
        registry_dir.mkdir(parents=True, exist_ok=True)
        (registry_dir / "workflow.yaml").write_text(workflow_yaml)
        yaml = YAML()
        index_data = {
            "workflows": {
                "test-workflow": {"description": "A test workflow", "path": "workflow.yaml"},
            }
        }
        with open(registry_dir / "index.yaml", "w") as f:
            yaml.dump(index_data, f)
        _configure_registry(registry_dir)

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_registries(pilot)
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)
            await pilot.press("enter")
            await settle(pilot)

            wf_table = app.screen.query_one(DataTable)
            wf_table.move_cursor(row=0)
            await pilot.press("enter")
            await settle(pilot)

            assert isinstance(app.screen, WorkflowInputsScreen)
            inputs_table = app.screen.query_one(DataTable)
            assert inputs_table.row_count == 1
            row = inputs_table.get_row_at(0)

            assert row[3].plain == "[/bold]evil default[/red]"
            assert row[4].plain == "[/bold]evil description[/red]"


# ---------------------------------------------------------------------------
# Launching a registry workflow with `n`
# ---------------------------------------------------------------------------


class TestRunFromRegistryDrilldown:
    """`n` launches the workflow you are looking at, from either drill-down
    level, instead of making you escape out and retype a reference you just
    navigated through."""

    async def test_n_on_workflows_list_opens_prefilled_new_run(self, fleet_env: Path) -> None:
        _configure_registry(_write_local_registry(fleet_env))

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_registries(pilot)
            app.screen.query_one(DataTable).move_cursor(row=0)
            await pilot.press("enter")
            await settle(pilot)

            app.screen.query_one(DataTable).move_cursor(row=0)
            await pilot.press("n")
            await settle(pilot)

            assert isinstance(app.screen, NewRunScreen)
            assert app.screen.query_one("#workflow-ref", Input).value == "test-workflow@my-reg"

    async def test_n_on_inputs_screen_opens_prefilled_new_run(self, fleet_env: Path) -> None:
        _configure_registry(_write_local_registry(fleet_env))

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_registries(pilot)
            app.screen.query_one(DataTable).move_cursor(row=0)
            await pilot.press("enter")
            await settle(pilot)
            app.screen.query_one(DataTable).move_cursor(row=0)
            await pilot.press("enter")
            await settle(pilot)

            assert isinstance(app.screen, WorkflowInputsScreen)

            await pilot.press("n")
            await settle(pilot)

            assert isinstance(app.screen, NewRunScreen)
            assert app.screen.query_one("#workflow-ref", Input).value == "test-workflow@my-reg"

    async def test_prefilled_reference_resolves_into_a_form(self, fleet_env: Path) -> None:
        """The pre-filled reference is resolved on mount, so the user lands on
        a usable form rather than on a filled box they still have to submit."""
        _configure_registry(_write_local_registry(fleet_env))

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_registries(pilot)
            app.screen.query_one(DataTable).move_cursor(row=0)
            await pilot.press("enter")
            await settle(pilot)
            app.screen.query_one(DataTable).move_cursor(row=0)
            await pilot.press("n")

            for _ in range(50):
                await settle(pilot)
                if isinstance(app.screen, NewRunScreen) and app.screen._input_widgets:
                    break
                await asyncio.sleep(0.05)

            assert isinstance(app.screen, NewRunScreen)
            assert set(app.screen._input_widgets) == {"question", "verbose"}
            assert app.screen._resolved is not None  # launchable

    async def test_return_to_runs_unwinds_the_whole_drilldown(self, fleet_env: Path) -> None:
        """A launch started three levels deep hands back to Runs, where the new
        run is actually visible -- popping one screen would land on a
        workflow's inputs instead."""
        _configure_registry(_write_local_registry(fleet_env))

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_registries(pilot)
            app.screen.query_one(DataTable).move_cursor(row=0)
            await pilot.press("enter")
            await settle(pilot)
            app.screen.query_one(DataTable).move_cursor(row=0)
            await pilot.press("n")
            await settle(pilot)

            assert isinstance(app.screen, NewRunScreen)

            app.return_to_runs()
            await settle(pilot)

            assert isinstance(app.screen, RunsScreen)


# ---------------------------------------------------------------------------
# `enter` footer advertisement (issue #459)
# ---------------------------------------------------------------------------


class TestRegistriesFooter:
    """`enter` must both open the highlighted registry's workflows *and*
    actually appear in the footer -- `DataTable` binds `enter` itself
    (`show=False`), so without `priority=True` the screen's own binding is
    shadowed and the key silently vanishes from the footer while it still
    works (see `runs.py`'s identical `Detail` binding, which this
    mirrors)."""

    async def test_workflows_binding_survives_datatables_own_enter(self, fleet_env: Path) -> None:
        _configure_registry(_write_local_registry(fleet_env))

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_registries(pilot)

            shown = [
                ab.binding.description
                for ab in app.screen.active_bindings.values()
                if ab.binding.show and ab.enabled
            ]
            assert "Workflows" in shown

    async def test_enter_pushes_exactly_one_workflows_screen(self, fleet_env: Path) -> None:
        _configure_registry(_write_local_registry(fleet_env))

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_registries(pilot)

            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)
            before = len(app.screen_stack)
            await pilot.press("enter")
            await settle(pilot)

            assert len(app.screen_stack) == before + 1
            assert isinstance(app.screen, RegistryWorkflowsScreen)

    async def test_mouse_click_still_pushes_workflows_screen(self, fleet_env: Path) -> None:
        """A mouse click posts `RowSelected` directly (rather than going
        through the `priority` screen binding), so this exercises
        `on_data_table_row_selected` -- the other of the two paths that
        must both funnel into the same push."""
        _configure_registry(_write_local_registry(fleet_env))

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_registries(pilot)

            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
            before = len(app.screen_stack)
            app.screen.post_message(DataTable.RowSelected(table, 0, row_key))
            await settle(pilot)

            assert len(app.screen_stack) == before + 1
            assert isinstance(app.screen, RegistryWorkflowsScreen)

    async def test_hidden_and_harmless_with_no_registries_configured(self, fleet_env: Path) -> None:
        """With no registries configured the table is empty, so `enter`
        must not be advertised (previously a new footer inaccuracy
        introduced alongside the priority binding) -- and pressing it
        anyway must not crash the app (the empty-table guard in
        `action_open_workflows` is the only thing preventing that today)."""
        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_registries(pilot)

            table = app.screen.query_one(DataTable)
            assert table.row_count == 0
            assert app.screen.check_action("open_workflows", ()) is False
            shown = [
                ab.binding.description
                for ab in app.screen.active_bindings.values()
                if ab.binding.show and ab.enabled
            ]
            assert "Workflows" not in shown

            before = len(app.screen_stack)
            await pilot.press("enter")
            await settle(pilot)

            assert app.is_running
            assert len(app.screen_stack) == before


class TestRegistryWorkflowsFooter:
    """`enter` must both open the highlighted workflow's inputs *and*
    actually appear in the footer -- same shadowing hazard as
    ``TestRegistriesFooter`` above."""

    async def test_inputs_binding_survives_datatables_own_enter(self, fleet_env: Path) -> None:
        _configure_registry(_write_local_registry(fleet_env))

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_registries(pilot)
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)
            await pilot.press("enter")
            await settle(pilot)
            assert isinstance(app.screen, RegistryWorkflowsScreen)

            shown = [
                ab.binding.description
                for ab in app.screen.active_bindings.values()
                if ab.binding.show and ab.enabled
            ]
            assert "Inputs" in shown

    async def test_enter_pushes_exactly_one_inputs_screen(self, fleet_env: Path) -> None:
        _configure_registry(_write_local_registry(fleet_env))

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_registries(pilot)
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)
            await pilot.press("enter")
            await settle(pilot)
            assert isinstance(app.screen, RegistryWorkflowsScreen)

            wf_table = app.screen.query_one(DataTable)
            wf_table.move_cursor(row=0)
            before = len(app.screen_stack)
            await pilot.press("enter")
            await settle(pilot)

            assert len(app.screen_stack) == before + 1
            assert isinstance(app.screen, WorkflowInputsScreen)

    async def test_mouse_click_still_pushes_inputs_screen(self, fleet_env: Path) -> None:
        """A mouse click posts `RowSelected` directly (rather than going
        through the `priority` screen binding), so this exercises
        `on_data_table_row_selected` -- the other of the two paths that
        must both funnel into the same push."""
        _configure_registry(_write_local_registry(fleet_env))

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_registries(pilot)
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)
            await pilot.press("enter")
            await settle(pilot)
            assert isinstance(app.screen, RegistryWorkflowsScreen)

            wf_table = app.screen.query_one(DataTable)
            wf_table.move_cursor(row=0)
            row_key = wf_table.coordinate_to_cell_key(wf_table.cursor_coordinate).row_key
            before = len(app.screen_stack)
            app.screen.post_message(DataTable.RowSelected(wf_table, 0, row_key))
            await settle(pilot)

            assert len(app.screen_stack) == before + 1
            assert isinstance(app.screen, WorkflowInputsScreen)

    async def test_hidden_and_harmless_with_no_workflows_in_registry(self, fleet_env: Path) -> None:
        """A registry with no workflows in its index leaves the table
        empty, so `enter`/`n` must not be advertised, and pressing either
        anyway must not crash the app (registries.py's "no workflows found"
        state, not an error)."""
        from ruamel.yaml import YAML

        registry_dir = fleet_env / "empty-registry"
        registry_dir.mkdir(parents=True, exist_ok=True)
        yaml = YAML()
        with open(registry_dir / "index.yaml", "w") as f:
            yaml.dump({"workflows": {}}, f)
        _configure_registry(registry_dir)

        app = FleetApp()
        async with app.run_test() as pilot:
            await _goto_registries(pilot)
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)
            await pilot.press("enter")
            await settle(pilot)
            assert isinstance(app.screen, RegistryWorkflowsScreen)

            wf_table = app.screen.query_one(DataTable)
            assert wf_table.row_count == 0
            assert app.screen.check_action("open_inputs", ()) is False
            assert app.screen.check_action("new_run", ()) is False
            shown = [
                ab.binding.description
                for ab in app.screen.active_bindings.values()
                if ab.binding.show and ab.enabled
            ]
            assert "Inputs" not in shown
            assert "Run" not in shown

            before = len(app.screen_stack)
            await pilot.press("enter")
            await pilot.press("n")
            await settle(pilot)

            assert app.is_running
            assert len(app.screen_stack) == before
