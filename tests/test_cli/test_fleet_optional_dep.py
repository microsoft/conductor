"""Tests for the ``conductor fleet`` optional ``textual`` dependency (Fleet Manager E7).

Covers E7-T7: bare ``conductor fleet`` with ``textual`` unavailable prints
an actionable install hint and exits non-zero without a traceback;
``conductor fleet list`` (core, no optional dependency) still works in
that state.

Also covers issue #441: the hint is resolved from the detected install
context rather than hardcoding ``pip install 'conductor-cli[tui]'``, which
cannot work for anyone (``conductor-cli`` is not published to PyPI).
"""

from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from conductor.cli.app import app
from conductor.install_hint import InstallContext, InstallEnvironment, render_install_command

runner = CliRunner()


class TestFleetWithoutTextual:
    """Simulates a clean install with no ``[tui]`` extra."""

    def test_bare_invocation_prints_the_resolved_install_command(self) -> None:
        with (
            patch("conductor.cli.fleet.TEXTUAL_AVAILABLE", False),
            patch("conductor.cli.fleet.install_command", return_value="INSTALL-ME-NOW"),
        ):
            result = runner.invoke(app, ["fleet"])

        assert result.exit_code != 0
        assert "tui" in result.output
        assert "INSTALL-ME-NOW" in result.output

    def test_the_hint_asks_the_resolver_for_the_tui_extra(self) -> None:
        with (
            patch("conductor.cli.fleet.TEXTUAL_AVAILABLE", False),
            patch("conductor.cli.fleet.install_command", return_value="x") as resolver,
        ):
            runner.invoke(app, ["fleet"])

        resolver.assert_called_once_with("tui")

    def test_a_uv_tool_install_is_never_told_to_use_pip(self) -> None:
        """The regression issue #441 is about: `pip install
        'conductor-cli[tui]'` cannot work on the documented install path —
        `conductor-cli` is not on PyPI and a uv tool venv is not
        pip-managed."""
        env = InstallEnvironment(InstallContext.UV_TOOL, frozenset(), "v0.1.30")
        with (
            patch("conductor.cli.fleet.TEXTUAL_AVAILABLE", False),
            patch(
                "conductor.cli.fleet.install_command",
                return_value=render_install_command("tui", env),
            ),
        ):
            result = runner.invoke(app, ["fleet"])

        assert "pip install" not in result.output
        assert "uv tool install --force" in result.output

    def test_brackets_in_the_resolved_command_survive_rich_markup(self) -> None:
        """The command is a runtime value containing `[tui]`, and rich would
        silently delete a lowercase bracketed token from a plain string. It
        must go through `styled()`, which never hands values to the parser."""
        with (
            patch("conductor.cli.fleet.TEXTUAL_AVAILABLE", False),
            patch(
                "conductor.cli.fleet.install_command",
                return_value="uv sync --extra tui  # [tui] [aca]",
            ),
        ):
            result = runner.invoke(app, ["fleet"])

        assert "[tui] [aca]" in result.output
        assert "Traceback" not in result.output

    def test_the_command_is_not_wrapped_across_lines(self) -> None:
        """The resolved uv spec is longer than a default 80-column terminal.
        Rich would word-wrap it, inserting a real newline that turns a
        copy-paste into two broken commands."""
        long_command = (
            "uv tool install --force 'conductor-cli[aca,tui] @ "
            "git+https://github.com/microsoft/conductor.git@v0.1.30'"
        )
        assert len(long_command) > 80
        with (
            patch("conductor.cli.fleet.TEXTUAL_AVAILABLE", False),
            patch("conductor.cli.fleet.install_command", return_value=long_command),
        ):
            result = runner.invoke(app, ["fleet"])

        assert any(long_command in line for line in result.output.splitlines())

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
