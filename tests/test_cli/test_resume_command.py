"""Tests for the resume and checkpoints CLI commands.

Tests cover:
- resume command with --from checkpoint path
- resume command with workflow path (finds latest checkpoint)
- resume command missing arguments error
- resume command with nonexistent checkpoint error
- checkpoints command with no checkpoints
- checkpoints command with multiple checkpoints
- checkpoints command filtered by workflow path
- Workflow hash mismatch warning on resume
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app
from conductor.engine.checkpoint import CheckpointData, CheckpointManager

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _write_checkpoint(
    tmp_path: Path,
    workflow_path: Path,
    *,
    current_agent: str = "greeter",
    error_type: str = "ProviderError",
    error_message: str = "Network error",
    timestamp: str = "20260224-153000",
    workflow_hash: str | None = None,
) -> Path:
    """Write a checkpoint JSON file and return its path."""
    if workflow_hash is None:
        workflow_hash = CheckpointManager.compute_workflow_hash(workflow_path)

    checkpoint = {
        "version": 1,
        "workflow_path": str(workflow_path.resolve()),
        "workflow_hash": workflow_hash,
        "created_at": "2026-02-24T15:30:00+00:00",
        "failure": {
            "error_type": error_type,
            "message": error_message,
            "agent": current_agent,
            "iteration": 1,
        },
        "inputs": {"name": "World"},
        "current_agent": current_agent,
        "context": {
            "workflow_inputs": {"name": "World"},
            "agent_outputs": {},
            "current_iteration": 0,
            "execution_history": [],
        },
        "limits": {
            "current_iteration": 0,
            "max_iterations": 10,
            "execution_history": [],
        },
        "copilot_session_ids": {},
    }

    workflow_name = workflow_path.stem
    cp_path = tmp_path / f"{workflow_name}-{timestamp}.json"
    cp_path.write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")
    return cp_path


# ---------------------------------------------------------------------------
# Resume command tests
# ---------------------------------------------------------------------------


class TestResumeCommand:
    """Tests for the 'conductor resume' CLI command."""

    def test_resume_help(self) -> None:
        """Test that resume --help works."""
        result = runner.invoke(app, ["resume", "--help"])
        assert result.exit_code == 0
        assert "Resume a workflow from a checkpoint" in result.output

    def test_resume_missing_arguments(self) -> None:
        """Test error when neither workflow nor --from is provided."""
        result = runner.invoke(app, ["resume"])
        assert result.exit_code == 1
        assert "Provide a workflow file" in result.output

    def test_resume_nonexistent_checkpoint(self, tmp_path: Path) -> None:
        """Test error when --from points to a nonexistent file."""
        fake_path = tmp_path / "nonexistent.json"
        result = runner.invoke(app, ["resume", "--from", str(fake_path)])
        assert result.exit_code == 1
        assert "Checkpoint file not found" in result.output

    def test_resume_nonexistent_workflow(self, tmp_path: Path) -> None:
        """Test error when workflow file doesn't exist."""
        fake_path = tmp_path / "nonexistent.yaml"
        result = runner.invoke(app, ["resume", str(fake_path)])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_resume_from_checkpoint_path(self, tmp_path: Path) -> None:
        """Test resume with explicit --from checkpoint path."""
        wf_path = _write_workflow(tmp_path)
        cp_path = _write_checkpoint(tmp_path, wf_path)

        mock_result = {"message": "Hello, World!"}

        with patch(
            "conductor.cli.run.resume_workflow_async", new_callable=AsyncMock
        ) as mock_resume:
            mock_resume.return_value = mock_result
            runner.invoke(app, ["resume", "--from", str(cp_path)])

        assert mock_resume.called
        call_kwargs = mock_resume.call_args
        assert call_kwargs[1]["checkpoint_path"] == cp_path.resolve()

    def test_resume_with_workflow_path(self, tmp_path: Path) -> None:
        """Test resume with workflow path (finds latest checkpoint)."""
        wf_path = _write_workflow(tmp_path)

        mock_result = {"message": "Hello!"}

        with patch(
            "conductor.cli.run.resume_workflow_async", new_callable=AsyncMock
        ) as mock_resume:
            mock_resume.return_value = mock_result
            runner.invoke(app, ["resume", str(wf_path)])

        assert mock_resume.called
        call_kwargs = mock_resume.call_args
        assert call_kwargs[1]["workflow_path"] == wf_path.resolve()

    def test_resume_outputs_json_on_success(self, tmp_path: Path) -> None:
        """Test that successful resume outputs JSON to stdout."""
        wf_path = _write_workflow(tmp_path)
        cp_path = _write_checkpoint(tmp_path, wf_path)

        mock_result = {"message": "Resumed output"}

        with patch(
            "conductor.cli.run.resume_workflow_async", new_callable=AsyncMock
        ) as mock_resume:
            mock_resume.return_value = mock_result
            result = runner.invoke(app, ["resume", "--from", str(cp_path)])

        assert result.exit_code == 0
        assert "Resumed output" in result.output

    def test_resume_with_skip_gates(self, tmp_path: Path) -> None:
        """Test resume passes --skip-gates through."""
        wf_path = _write_workflow(tmp_path)

        with patch(
            "conductor.cli.run.resume_workflow_async", new_callable=AsyncMock
        ) as mock_resume:
            mock_resume.return_value = {"result": "ok"}
            runner.invoke(app, ["resume", str(wf_path), "--skip-gates"])

        call_kwargs = mock_resume.call_args
        assert call_kwargs[1]["skip_gates"] is True

    def test_resume_handles_execution_error(self, tmp_path: Path) -> None:
        """Test that execution errors are displayed properly."""
        wf_path = _write_workflow(tmp_path)
        cp_path = _write_checkpoint(tmp_path, wf_path)

        from conductor.exceptions import ExecutionError

        with patch(
            "conductor.cli.run.resume_workflow_async", new_callable=AsyncMock
        ) as mock_resume:
            mock_resume.side_effect = ExecutionError("Agent failed")
            result = runner.invoke(app, ["resume", "--from", str(cp_path)])

        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Checkpoints command tests
# ---------------------------------------------------------------------------


class TestCheckpointsCommand:
    """Tests for the 'conductor checkpoints' CLI command."""

    def test_checkpoints_help(self) -> None:
        """Test that checkpoints --help works."""
        result = runner.invoke(app, ["checkpoints", "--help"])
        assert result.exit_code == 0
        assert "List available workflow checkpoints" in result.output

    def test_checkpoints_no_checkpoints(self, tmp_path: Path) -> None:
        """Test output when no checkpoints exist."""
        with patch.object(CheckpointManager, "list_checkpoints", return_value=[]):
            result = runner.invoke(app, ["checkpoints"])

        assert result.exit_code == 0
        assert "No checkpoints found" in result.output

    def test_checkpoints_with_multiple(self, tmp_path: Path) -> None:
        """Test listing multiple checkpoints."""
        checkpoints = [
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

        with patch.object(CheckpointManager, "list_checkpoints", return_value=checkpoints):
            result = runner.invoke(app, ["checkpoints"])

        assert result.exit_code == 0
        assert "workflow-a" in result.output
        assert "workflow-b" in result.output
        assert "researcher" in result.output
        assert "synthesizer" in result.output
        assert "ProviderError" in result.output
        assert "TimeoutError" in result.output
        assert "2 checkpoint(s)" in result.output

    def test_checkpoints_filtered_by_workflow(self, tmp_path: Path) -> None:
        """Test filtering checkpoints by workflow path."""
        wf_path = _write_workflow(tmp_path, "my-workflow")

        with patch.object(CheckpointManager, "list_checkpoints", return_value=[]) as mock_list:
            result = runner.invoke(app, ["checkpoints", str(wf_path)])

        assert result.exit_code == 0
        # Verify list_checkpoints was called with the resolved path
        mock_list.assert_called_once()
        call_arg = mock_list.call_args[0][0]
        assert call_arg == wf_path.resolve()

    def test_checkpoints_nonexistent_workflow(self, tmp_path: Path) -> None:
        """Test error when filtering by nonexistent workflow file."""
        fake_path = tmp_path / "nonexistent.yaml"
        result = runner.invoke(app, ["checkpoints", str(fake_path)])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_checkpoints_no_checkpoints_for_workflow(self, tmp_path: Path) -> None:
        """Test message when no checkpoints exist for a specific workflow."""
        wf_path = _write_workflow(tmp_path, "specific-workflow")

        with patch.object(CheckpointManager, "list_checkpoints", return_value=[]):
            result = runner.invoke(app, ["checkpoints", str(wf_path)])

        assert result.exit_code == 0
        assert "No checkpoints found for workflow" in result.output


# ---------------------------------------------------------------------------
# Hash mismatch warning tests
# ---------------------------------------------------------------------------


class TestHashMismatchWarning:
    """Test workflow hash mismatch warning on resume."""

    @pytest.mark.asyncio
    async def test_hash_mismatch_warning_in_resume_async(self, tmp_path: Path) -> None:
        """Test that resume_workflow_async warns on hash mismatch."""
        from unittest.mock import MagicMock

        from conductor.cli.run import _verbose_console, resume_workflow_async

        wf_path = _write_workflow(tmp_path)
        cp_path = _write_checkpoint(tmp_path, wf_path, workflow_hash="sha256:different")

        # We need to mock the ProviderRegistry and engine since we can't
        # actually create providers in tests
        with (
            patch("conductor.cli.run.ProviderRegistry") as mock_registry_cls,
            patch("conductor.cli.run.WorkflowEngine") as mock_engine_cls,
            patch.object(_verbose_console, "print") as mock_print,
        ):
            # Set up async context manager
            mock_registry = AsyncMock()
            mock_registry_cls.return_value = mock_registry
            mock_registry.__aenter__ = AsyncMock(return_value=mock_registry)
            mock_registry.__aexit__ = AsyncMock(return_value=False)

            # Set up engine mock
            mock_engine = MagicMock()
            mock_engine.resume = AsyncMock(return_value={"result": "ok"})
            mock_engine.config = MagicMock()
            mock_engine.config.workflow.cost.show_summary = False
            mock_engine_cls.return_value = mock_engine

            await resume_workflow_async(
                checkpoint_path=cp_path,
            )

            # Verify warning was printed
            warning_printed = any(
                "changed since checkpoint" in str(call) for call in mock_print.call_args_list
            )
            assert warning_printed, "Expected hash mismatch warning. Prints: " + str(
                [str(c) for c in mock_print.call_args_list]
            )


# ---------------------------------------------------------------------------
# Resume workflow async unit tests
# ---------------------------------------------------------------------------


class TestResumeWorkflowAsync:
    """Tests for the resume_workflow_async function."""

    @pytest.mark.asyncio
    async def test_no_checkpoint_found_for_workflow(self, tmp_path: Path) -> None:
        """Test error when no checkpoints exist for the given workflow."""
        from conductor.cli.run import resume_workflow_async
        from conductor.exceptions import CheckpointError

        wf_path = _write_workflow(tmp_path)

        with (
            patch.object(CheckpointManager, "find_latest_checkpoint", return_value=None),
            pytest.raises(CheckpointError, match="No checkpoints found"),
        ):
            await resume_workflow_async(workflow_path=wf_path)

    @pytest.mark.asyncio
    async def test_neither_workflow_nor_checkpoint(self) -> None:
        """Test error when neither argument is provided."""
        from conductor.cli.run import resume_workflow_async
        from conductor.exceptions import CheckpointError

        with pytest.raises(CheckpointError, match="Either workflow path or --from"):
            await resume_workflow_async()

    @pytest.mark.asyncio
    async def test_agent_not_in_workflow(self, tmp_path: Path) -> None:
        """Test error when checkpoint agent doesn't exist in workflow."""
        from conductor.cli.run import resume_workflow_async
        from conductor.exceptions import CheckpointError

        wf_path = _write_workflow(tmp_path)
        cp_path = _write_checkpoint(tmp_path, wf_path, current_agent="nonexistent_agent")

        with pytest.raises(CheckpointError, match="not found in workflow"):
            await resume_workflow_async(checkpoint_path=cp_path)

    @pytest.mark.asyncio
    async def test_workflow_file_not_found(self, tmp_path: Path) -> None:
        """Test error when workflow file referenced in checkpoint doesn't exist."""
        from conductor.cli.run import resume_workflow_async
        from conductor.exceptions import CheckpointError

        # Create a checkpoint pointing to a non-existent workflow
        fake_wf = tmp_path / "deleted-workflow.yaml"
        fake_wf.write_text("name: deleted\n")
        cp_path = _write_checkpoint(tmp_path, fake_wf, current_agent="greeter")
        fake_wf.unlink()  # Delete the workflow file

        with pytest.raises(CheckpointError, match="Workflow file not found"):
            await resume_workflow_async(checkpoint_path=cp_path)
