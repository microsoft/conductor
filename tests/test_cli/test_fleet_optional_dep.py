"""Tests for the ``conductor fleet`` optional ``textual`` dependency (Fleet Manager E7).

Covers E7-T7: bare ``conductor fleet`` with ``textual`` unavailable prints
an actionable install hint and exits non-zero without a traceback;
``conductor fleet list`` (core, no optional dependency) still works in
that state.
"""

from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from conductor.cli.app import app

runner = CliRunner()


class TestFleetWithoutTextual:
    """Simulates a clean install with no ``[tui]`` extra."""

    def test_bare_invocation_prints_install_hint(self) -> None:
        with patch("conductor.cli.fleet.TEXTUAL_AVAILABLE", False):
            result = runner.invoke(app, ["fleet"])

        assert result.exit_code != 0
        assert "conductor-cli[tui]" in result.output
        assert "pip install" in result.output

    def test_bare_invocation_never_raises_a_traceback(self) -> None:
        """A missing optional dependency must surface as a clean CLI error,
        never an uncaught ImportError/traceback."""
        with patch("conductor.cli.fleet.TEXTUAL_AVAILABLE", False):
            result = runner.invoke(app, ["fleet"])

        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Traceback" not in result.output

    def test_fleet_list_still_works_without_textual(self, tmp_path) -> None:
        """The core, non-interactive `fleet list` command has no optional
        dependency and must be entirely unaffected by textual's absence."""
        with (
            patch("conductor.cli.fleet.TEXTUAL_AVAILABLE", False),
            patch.dict("os.environ", {"CONDUCTOR_HOME": str(tmp_path)}),
        ):
            result = runner.invoke(app, ["fleet", "list"])

        assert result.exit_code == 0

    def test_fleet_prune_still_works_without_textual(self, tmp_path) -> None:
        """`fleet prune` is likewise core functionality, unaffected by
        textual's absence."""
        with (
            patch("conductor.cli.fleet.TEXTUAL_AVAILABLE", False),
            patch.dict("os.environ", {"CONDUCTOR_HOME": str(tmp_path)}),
        ):
            result = runner.invoke(app, ["fleet", "prune", "--dry-run"])

        assert result.exit_code == 0

    def test_subcommand_invocation_does_not_require_textual(self) -> None:
        """Invoking a subcommand (as opposed to the bare group) never even
        looks at TEXTUAL_AVAILABLE -- the check only gates the bare,
        no-subcommand branch."""
        with patch("conductor.cli.fleet.TEXTUAL_AVAILABLE", False):
            result = runner.invoke(app, ["fleet", "list", "--help"])

        assert result.exit_code == 0
        assert "List every live Conductor run" in result.output


class TestFleetWithTextual:
    """The counterpart, real-dependency-available cases."""

    def test_textual_is_actually_importable(self) -> None:
        """Sanity check that this environment (the `tui` extra is in the
        `dev` dependency group per E7-T1) genuinely has `textual` available,
        so `TEXTUAL_AVAILABLE` reflects a real import rather than being
        hardcoded True."""
        import conductor.cli.fleet as fleet_module

        assert fleet_module.TEXTUAL_AVAILABLE is True

    def test_bare_invocation_launches_tui_when_available(self) -> None:
        with (
            patch("conductor.cli.fleet.TEXTUAL_AVAILABLE", True),
            patch("conductor.fleet.tui.app.FleetApp") as mock_app_cls,
        ):
            result = runner.invoke(app, ["fleet"])

        assert result.exit_code == 0
        mock_app_cls.return_value.run.assert_called_once()
