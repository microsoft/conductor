"""Tests for the ``conductor checkpoint`` CLI subcommand group.

Covers the ``checkpoint list`` command and its hidden ``checkpoints``
deprecated alias (still works, warns, and forwards to the shared impl).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from conductor.cli.app import app
from conductor.engine.checkpoint import CheckpointData, CheckpointManager

runner = CliRunner()


def _write_workflow(tmp_path: Path, name: str = "test-workflow") -> Path:
    """Write a minimal workflow YAML file and return its path."""
    wf = tmp_path / f"{name}.yaml"
    wf.write_text(
        f"""\
workflow:
  name: {name}
  entry_point: greeter

agents:
  - name: greeter
    model: gpt-4
    prompt: "Hello"
    output:
      greeting:
        type: string
    routes:
      - to: $end

output:
  message: "{{{{ greeter.output.greeting }}}}"
"""
    )
    return wf


def _sample_checkpoints() -> list[CheckpointData]:
    """Two failure checkpoints for list-rendering assertions."""
    return [
        CheckpointData(
            version=1,
            workflow_path="/path/to/workflow-a.yaml",
            workflow_hash="sha256:abc",
            created_at="2026-02-24T15:30:00+00:00",
            failure={
                "error_type": "ProviderError",
                "message": "Network error",
                "agent": "researcher",
                "iteration": 2,
            },
            inputs={"topic": "AI"},
            current_agent="researcher",
            context={},
            limits={},
            file_path=Path("/tmp/conductor/checkpoints/workflow-a-20260224-153000.json"),
        ),
        CheckpointData(
            version=1,
            workflow_path="/path/to/workflow-b.yaml",
            workflow_hash="sha256:def",
            created_at="2026-02-24T16:00:00+00:00",
            failure={
                "error_type": "TimeoutError",
                "message": "Timed out",
                "agent": "synthesizer",
                "iteration": 5,
            },
            inputs={},
            current_agent="synthesizer",
            context={},
            limits={},
            file_path=Path("/tmp/conductor/checkpoints/workflow-b-20260224-160000.json"),
        ),
    ]


class TestCheckpointListCommand:
    """Tests for the 'conductor checkpoint list' CLI command."""

    def test_help(self) -> None:
        """`checkpoint list --help` works."""
        result = runner.invoke(app, ["checkpoint", "list", "--help"])
        assert result.exit_code == 0
        assert "List available workflow checkpoints" in result.output

    def test_no_checkpoints(self, tmp_path: Path) -> None:
        """Output when no checkpoints exist."""
        with patch.object(CheckpointManager, "list_checkpoints", return_value=[]):
            result = runner.invoke(app, ["checkpoint", "list"])

        assert result.exit_code == 0
        assert "No checkpoints found" in result.output

    def test_with_multiple(self, tmp_path: Path) -> None:
        """List multiple checkpoints."""
        with patch.object(
            CheckpointManager, "list_checkpoints", return_value=_sample_checkpoints()
        ):
            result = runner.invoke(app, ["checkpoint", "list"])

        assert result.exit_code == 0
        assert "workflow-a" in result.output
        assert "workflow-b" in result.output
        assert "researcher" in result.output
        assert "synthesizer" in result.output
        assert "ProviderError" in result.output
        assert "TimeoutError" in result.output
        assert "2 checkpoint(s)" in result.output

    def test_shows_periodic_trigger(self, tmp_path: Path) -> None:
        """Periodic checkpoints render a 'periodic' trigger and no error type."""
        checkpoints = [
            CheckpointData(
                version=1,
                workflow_path="/path/to/workflow-a.yaml",
                workflow_hash="sha256:abc",
                created_at="2026-02-24T15:30:00+00:00",
                failure={
                    "error_type": None,
                    "message": None,
                    "agent": "researcher",
                    "iteration": 2,
                },
                inputs={},
                current_agent="researcher",
                context={},
                limits={},
                file_path=Path("/tmp/conductor/checkpoints/workflow-a-20260224-153000.json"),
                trigger="periodic",
            ),
        ]

        with patch.object(CheckpointManager, "list_checkpoints", return_value=checkpoints):
            result = runner.invoke(app, ["checkpoint", "list"])

        assert result.exit_code == 0
        assert "periodic" in result.output
        assert "researcher" in result.output
        # No error type for a periodic checkpoint — rendered as an em dash.
        assert "—" in result.output

    def test_filtered_by_workflow(self, tmp_path: Path) -> None:
        """Filter checkpoints by workflow path."""
        wf_path = _write_workflow(tmp_path, "my-workflow")

        with patch.object(CheckpointManager, "list_checkpoints", return_value=[]) as mock_list:
            result = runner.invoke(app, ["checkpoint", "list", str(wf_path)])

        assert result.exit_code == 0
        mock_list.assert_called_once()
        call_arg = mock_list.call_args[0][0]
        assert call_arg == wf_path.resolve()

    def test_nonexistent_workflow(self, tmp_path: Path) -> None:
        """Error when filtering by a nonexistent workflow file."""
        fake_path = tmp_path / "nonexistent.yaml"
        result = runner.invoke(app, ["checkpoint", "list", str(fake_path)])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_no_checkpoints_for_workflow(self, tmp_path: Path) -> None:
        """Message when no checkpoints exist for a specific workflow."""
        wf_path = _write_workflow(tmp_path, "specific-workflow")

        with patch.object(CheckpointManager, "list_checkpoints", return_value=[]):
            result = runner.invoke(app, ["checkpoint", "list", str(wf_path)])

        assert result.exit_code == 0
        assert "No checkpoints found for workflow" in result.output


class TestCheckpointsDeprecatedAlias:
    """The hidden ``checkpoints`` alias still works, warns, and forwards."""

    def test_alias_warns_and_forwards(self) -> None:
        """`checkpoints` emits a deprecation notice and forwards to the impl."""
        with patch.object(CheckpointManager, "list_checkpoints", return_value=[]):
            result = runner.invoke(app, ["checkpoints"])

        assert result.exit_code == 0
        # Collapse Rich line-wrapping before matching the message.
        normalized = " ".join(result.output.split())
        assert "deprecated" in normalized
        assert "removed in a future release" in normalized
        assert "conductor checkpoint list" in normalized
        # Forwarded to the shared impl.
        assert "No checkpoints found" in normalized

    def test_alias_matches_new_command(self, tmp_path: Path) -> None:
        """Alias renders the same table as ``checkpoint list`` — forwarding parity."""
        with patch.object(
            CheckpointManager, "list_checkpoints", return_value=_sample_checkpoints()
        ):
            alias = runner.invoke(app, ["checkpoints"])
            canonical = runner.invoke(app, ["checkpoint", "list"])

        assert alias.exit_code == canonical.exit_code == 0
        for token in ("workflow-a", "workflow-b", "ProviderError", "2 checkpoint(s)"):
            assert token in alias.output
            assert token in canonical.output
        # The canonical command does not print the deprecation notice.
        assert "deprecated" not in canonical.output

    def test_alias_hidden_from_help(self) -> None:
        """The deprecated alias is registered and invokable, but marked hidden."""
        import click
        import typer

        group = typer.main.get_command(app)
        ctx = click.Context(group)
        alias = group.get_command(ctx, "checkpoints")
        assert alias is not None  # still invokable
        assert alias.hidden is True  # but out of --help
        # The canonical group stays visible.
        assert group.get_command(ctx, "checkpoint").hidden is False
