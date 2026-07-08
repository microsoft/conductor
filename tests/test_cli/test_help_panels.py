"""Tests for the top-level CLI help-panel grouping (issue #275).

Verifies the hybrid command surface: hot-path verbs stay flat while the long
tail is grouped under noun sub-apps, all organised into ``rich_help_panel``
sections. Help output is rendered at a pinned width so panel titles do not
wrap on narrow CI terminals.
"""

from __future__ import annotations

import click
import typer
from typer.testing import CliRunner

from conductor.cli.app import app

runner = CliRunner()

# Pinned width so Rich renders panel titles on a single header line.
_WIDE = {"COLUMNS": "200"}


class TestHelpPanels:
    """The root ``--help`` groups commands into the expected panels."""

    def test_all_panel_titles_present(self) -> None:
        result = runner.invoke(app, ["--help"], env=_WIDE)
        assert result.exit_code == 0
        for panel in (
            "Run & Recover",
            "Author & Inspect",
            "Interact",
            "State",
            "Environment",
        ):
            assert panel in result.output

    def test_flat_commands_listed(self) -> None:
        result = runner.invoke(app, ["--help"], env=_WIDE)
        assert result.exit_code == 0
        for cmd in ("run", "resume", "stop", "replay", "validate", "show", "update", "doctor"):
            assert cmd in result.output

    def test_noun_groups_listed(self) -> None:
        result = runner.invoke(app, ["--help"], env=_WIDE)
        assert result.exit_code == 0
        for group in ("gate", "checkpoint", "registry"):
            assert group in result.output

    def test_commands_mapped_to_correct_panels(self) -> None:
        """Each command sits under its designated panel (structural check).

        Asserting the ``rich_help_panel`` mapping directly catches a
        miscategorised command that a presence-only check would miss, and is
        immune to help-output width wrapping.
        """
        group = typer.main.get_command(app)
        ctx = click.Context(group)
        expected = {
            "run": "Run & Recover",
            "resume": "Run & Recover",
            "stop": "Run & Recover",
            "replay": "Run & Recover",
            "validate": "Author & Inspect",
            "show": "Author & Inspect",
            "gate": "Interact",
            "checkpoint": "State",
            "registry": "Environment",
            "update": "Environment",
            "doctor": "Environment",
        }
        for name, panel in expected.items():
            cmd = group.get_command(ctx, name)
            assert cmd is not None, f"{name} should be registered"
            assert cmd.rich_help_panel == panel, f"{name} should be under {panel!r}"


class TestDeprecatedAliasesHidden:
    """The deprecated aliases are still invokable but hidden from ``--help``."""

    def test_aliases_registered_but_hidden(self) -> None:
        group = typer.main.get_command(app)
        ctx = click.Context(group)
        for alias in ("checkpoints", "gate-respond"):
            cmd = group.get_command(ctx, alias)
            assert cmd is not None, f"{alias} should still be invokable"
            assert cmd.hidden is True, f"{alias} should be hidden from --help"

    def test_canonical_targets_visible(self) -> None:
        group = typer.main.get_command(app)
        ctx = click.Context(group)
        for name in ("checkpoint", "gate"):
            cmd = group.get_command(ctx, name)
            assert cmd is not None
            assert cmd.hidden is False
