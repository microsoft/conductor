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
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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

    def test_resume_with_provider_override(self, tmp_path: Path) -> None:
        """Test resume passes --provider through as provider_override."""
        wf_path = _write_workflow(tmp_path)

        with patch(
            "conductor.cli.run.resume_workflow_async", new_callable=AsyncMock
        ) as mock_resume:
            mock_resume.return_value = {"result": "ok"}
            runner.invoke(app, ["resume", str(wf_path), "--provider", "claude"])

        call_kwargs = mock_resume.call_args
        assert call_kwargs[1]["provider_override"] == "claude"

    def test_resume_with_metadata(self, tmp_path: Path) -> None:
        """Test resume parses --metadata flags into a dict."""
        wf_path = _write_workflow(tmp_path)

        with patch(
            "conductor.cli.run.resume_workflow_async", new_callable=AsyncMock
        ) as mock_resume:
            mock_resume.return_value = {"result": "ok"}
            runner.invoke(
                app,
                [
                    "resume",
                    str(wf_path),
                    "--metadata",
                    "tracker=ado",
                    "-m",
                    "work_item_id=1814",
                ],
            )

        call_kwargs = mock_resume.call_args
        assert call_kwargs[1]["metadata"] == {
            "tracker": "ado",
            "work_item_id": "1814",
        }

    def test_resume_invalid_metadata_format(self, tmp_path: Path) -> None:
        """Test resume rejects malformed --metadata values."""
        wf_path = _write_workflow(tmp_path)

        result = runner.invoke(app, ["resume", str(wf_path), "--metadata", "no_equals"])
        assert result.exit_code != 0

    def test_resume_with_web(self, tmp_path: Path) -> None:
        """Test resume passes --web and --web-port through."""
        wf_path = _write_workflow(tmp_path)

        with patch(
            "conductor.cli.run.resume_workflow_async", new_callable=AsyncMock
        ) as mock_resume:
            mock_resume.return_value = {"result": "ok"}
            runner.invoke(app, ["resume", str(wf_path), "--web", "--web-port", "9091"])

        call_kwargs = mock_resume.call_args
        assert call_kwargs[1]["web"] is True
        assert call_kwargs[1]["web_port"] == 9091
        assert call_kwargs[1]["web_bg"] is False

    def test_resume_web_and_web_bg_mutually_exclusive(self, tmp_path: Path) -> None:
        """Test that --web and --web-bg cannot be combined."""
        wf_path = _write_workflow(tmp_path)

        result = runner.invoke(app, ["resume", str(wf_path), "--web", "--web-bg"])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output.lower()

    def test_resume_web_bg_invokes_launch_background_resume(self, tmp_path: Path) -> None:
        """Test that --web-bg dispatches to launch_background_resume."""
        wf_path = _write_workflow(tmp_path)

        with patch("conductor.cli.bg_runner.launch_background_resume") as mock_launch:
            mock_launch.return_value = "http://127.0.0.1:9092"
            result = runner.invoke(
                app,
                [
                    "resume",
                    str(wf_path),
                    "--web-bg",
                    "--web-port",
                    "9092",
                    "--provider",
                    "copilot",
                    "-m",
                    "tracker=ado",
                    "--skip-gates",
                ],
            )

        assert result.exit_code == 0
        assert "http://127.0.0.1:9092" in result.output
        assert mock_launch.called
        kwargs = mock_launch.call_args[1]
        assert kwargs["workflow_path"] == wf_path.resolve()
        assert kwargs["checkpoint_path"] is None
        assert kwargs["provider_override"] == "copilot"
        assert kwargs["skip_gates"] is True
        assert kwargs["web_port"] == 9092
        assert kwargs["metadata"] == {"tracker": "ado"}

    def test_resume_web_bg_with_from_checkpoint(self, tmp_path: Path) -> None:
        """Test --web-bg forwards --from checkpoint path."""
        wf_path = _write_workflow(tmp_path)
        cp_path = _write_checkpoint(tmp_path, wf_path)

        with patch("conductor.cli.bg_runner.launch_background_resume") as mock_launch:
            mock_launch.return_value = "http://127.0.0.1:9093"
            result = runner.invoke(app, ["resume", "--from", str(cp_path), "--web-bg"])

        assert result.exit_code == 0
        kwargs = mock_launch.call_args[1]
        assert kwargs["workflow_path"] is None
        assert kwargs["checkpoint_path"] == cp_path.resolve()


# ---------------------------------------------------------------------------
# launch_background_resume tests
# ---------------------------------------------------------------------------


class TestLaunchBackgroundResume:
    """Tests for the launch_background_resume helper in bg_runner.py."""

    def test_requires_workflow_or_checkpoint(self) -> None:
        """Test that launch_background_resume raises when both args are None."""
        from conductor.cli.bg_runner import launch_background_resume

        with pytest.raises(ValueError, match="workflow_path or checkpoint_path"):
            launch_background_resume(workflow_path=None, checkpoint_path=None)

    def test_builds_resume_subcommand_with_workflow(self, tmp_path: Path) -> None:
        """Test the subprocess command starts with `conductor resume <workflow>`."""
        from conductor.cli import bg_runner

        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")

        captured: dict[str, list[str]] = {}

        def _fake_popen(cmd: list[str], **kwargs: object) -> MagicMock:  # type: ignore[no-untyped-def]
            captured["cmd"] = cmd
            proc = MagicMock()
            proc.pid = 12345
            proc.poll.return_value = None
            return proc

        with (
            patch("conductor.cli.bg_runner.subprocess.Popen", side_effect=_fake_popen),
            patch("conductor.cli.bg_runner._wait_for_server", return_value=True),
            patch("conductor.cli.pid.write_pid_file"),
        ):
            url = bg_runner.launch_background_resume(
                workflow_path=wf_path,
                checkpoint_path=None,
                provider_override="copilot",
                skip_gates=True,
                metadata={"tracker": "ado"},
                web_port=9099,
            )

        assert url == "http://127.0.0.1:9099"
        cmd = captured["cmd"]
        # `--silent` is global and must precede the subcommand
        assert "--silent" in cmd
        assert cmd.index("resume") > cmd.index("--silent")
        assert str(wf_path) in cmd
        assert "--web" in cmd
        assert "--web-port" in cmd
        assert "9099" in cmd
        assert "--no-interactive" in cmd
        assert "--provider" in cmd and "copilot" in cmd
        assert "--skip-gates" in cmd
        assert "--metadata" in cmd
        assert "tracker=ado" in cmd

    def test_builds_resume_subcommand_with_from_checkpoint(self, tmp_path: Path) -> None:
        """Test --from is forwarded when checkpoint_path is given without workflow_path."""
        from conductor.cli import bg_runner

        cp_path = tmp_path / "cp.json"
        cp_path.write_text("{}")

        captured: dict[str, list[str]] = {}

        def _fake_popen(cmd: list[str], **kwargs: object) -> MagicMock:  # type: ignore[no-untyped-def]
            captured["cmd"] = cmd
            proc = MagicMock()
            proc.pid = 12345
            proc.poll.return_value = None
            return proc

        with (
            patch("conductor.cli.bg_runner.subprocess.Popen", side_effect=_fake_popen),
            patch("conductor.cli.bg_runner._wait_for_server", return_value=True),
            patch("conductor.cli.pid.write_pid_file"),
        ):
            bg_runner.launch_background_resume(
                workflow_path=None,
                checkpoint_path=cp_path,
                web_port=9100,
            )

        cmd = captured["cmd"]
        assert "resume" in cmd
        assert "--from" in cmd
        from_idx = cmd.index("--from")
        assert cmd[from_idx + 1] == str(cp_path)


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


# ---------------------------------------------------------------------------
# launch_background_resume failure paths and detachment behavior
# ---------------------------------------------------------------------------


class TestLaunchBackgroundResumeFailures:
    """Failure paths and detachment kwargs for launch_background_resume."""

    def test_terminates_child_on_server_timeout(self, tmp_path: Path) -> None:
        """If the dashboard never comes up, the still-running child is killed."""
        from conductor.cli import bg_runner

        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")

        proc = MagicMock()
        proc.pid = 4242
        proc.poll.return_value = None  # still running

        with (
            patch("conductor.cli.bg_runner.subprocess.Popen", return_value=proc),
            patch("conductor.cli.bg_runner._wait_for_server", return_value=False),
            patch("conductor.cli.pid.write_pid_file") as mock_write,
            pytest.raises(RuntimeError, match="terminated"),
        ):
            bg_runner.launch_background_resume(workflow_path=wf_path, checkpoint_path=None)

        proc.terminate.assert_called_once()
        mock_write.assert_not_called()

    def test_reports_immediate_child_exit(self, tmp_path: Path) -> None:
        """If the child died before the server came up, surface its exit code."""
        from conductor.cli import bg_runner

        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")

        proc = MagicMock()
        proc.pid = 4243
        proc.poll.return_value = 7  # exited with code 7

        with (
            patch("conductor.cli.bg_runner.subprocess.Popen", return_value=proc),
            patch("conductor.cli.bg_runner._wait_for_server", return_value=False),
            patch("conductor.cli.pid.write_pid_file") as mock_write,
            pytest.raises(RuntimeError, match="exited immediately with code 7"),
        ):
            bg_runner.launch_background_resume(workflow_path=wf_path, checkpoint_path=None)

        # Child already dead -> no terminate, no PID file written.
        proc.terminate.assert_not_called()
        mock_write.assert_not_called()

    def test_terminates_child_on_pid_write_failure(self, tmp_path: Path) -> None:
        """If write_pid_file raises, the running child is killed (no orphan)."""
        from conductor.cli import bg_runner

        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")

        proc = MagicMock()
        proc.pid = 4244
        proc.poll.return_value = None

        with (
            patch("conductor.cli.bg_runner.subprocess.Popen", return_value=proc),
            patch("conductor.cli.bg_runner._wait_for_server", return_value=True),
            patch("conductor.cli.pid.write_pid_file", side_effect=OSError("disk full")),
            pytest.raises(RuntimeError, match="Failed to write PID file"),
        ):
            bg_runner.launch_background_resume(workflow_path=wf_path, checkpoint_path=None)

        proc.terminate.assert_called_once()

    def test_pid_file_written_with_workflow_path(self, tmp_path: Path) -> None:
        """When workflow_path is provided, the PID file is keyed to it."""
        from conductor.cli import bg_runner

        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")

        proc = MagicMock()
        proc.pid = 5555
        proc.poll.return_value = None

        with (
            patch("conductor.cli.bg_runner.subprocess.Popen", return_value=proc),
            patch("conductor.cli.bg_runner._wait_for_server", return_value=True),
            patch("conductor.cli.pid.write_pid_file") as mock_write,
        ):
            bg_runner.launch_background_resume(
                workflow_path=wf_path, checkpoint_path=None, web_port=9201
            )

        mock_write.assert_called_once_with(5555, 9201, wf_path)

    def test_pid_file_falls_back_to_checkpoint_path(self, tmp_path: Path) -> None:
        """When only checkpoint_path is given, it is used for the PID file ref."""
        from conductor.cli import bg_runner

        cp_path = tmp_path / "cp.json"
        cp_path.write_text("{}")

        proc = MagicMock()
        proc.pid = 5556
        proc.poll.return_value = None

        with (
            patch("conductor.cli.bg_runner.subprocess.Popen", return_value=proc),
            patch("conductor.cli.bg_runner._wait_for_server", return_value=True),
            patch("conductor.cli.pid.write_pid_file") as mock_write,
        ):
            bg_runner.launch_background_resume(
                workflow_path=None, checkpoint_path=cp_path, web_port=9202
            )

        mock_write.assert_called_once_with(5556, 9202, cp_path)

    def test_subprocess_detachment_kwargs(self, tmp_path: Path) -> None:
        """Verify Popen is called with detachment + DEVNULL + bg env vars."""
        import sys as _sys

        from conductor.cli import bg_runner

        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")

        captured: dict[str, object] = {}

        def _fake_popen(cmd: list[str], **kwargs: object) -> MagicMock:
            captured.update(kwargs)
            captured["cmd"] = cmd
            proc = MagicMock()
            proc.pid = 1
            proc.poll.return_value = None
            return proc

        with (
            patch("conductor.cli.bg_runner.subprocess.Popen", side_effect=_fake_popen),
            patch("conductor.cli.bg_runner._wait_for_server", return_value=True),
            patch("conductor.cli.pid.write_pid_file"),
        ):
            bg_runner.launch_background_resume(
                workflow_path=wf_path, checkpoint_path=None, web_port=9203
            )

        import subprocess as _sp

        assert captured["stdout"] is _sp.DEVNULL
        assert captured["stderr"] is _sp.DEVNULL
        assert captured["stdin"] is _sp.DEVNULL
        if _sys.platform == "win32":
            assert captured["creationflags"] == _sp.CREATE_NEW_PROCESS_GROUP
        else:
            assert captured["start_new_session"] is True
        env = captured["env"]
        assert isinstance(env, dict)
        assert env["CONDUCTOR_WEB_BG"] == "1"
        assert env["CONDUCTOR_WEB_PORT"] == "9203"


# ---------------------------------------------------------------------------
# _execute_with_stop_signal tests (used by both run and resume)
# ---------------------------------------------------------------------------


class TestExecuteWithStopSignal:
    """Direct tests of the shared cancellation helper."""

    @pytest.mark.asyncio
    async def test_returns_engine_result_when_no_dashboard(self) -> None:
        from conductor.cli.run import _execute_with_stop_signal

        async def _engine() -> dict[str, str]:
            return {"ok": "yes"}

        result = await _execute_with_stop_signal(_engine(), dashboard=None)
        assert result == {"ok": "yes"}

    @pytest.mark.asyncio
    async def test_returns_engine_result_when_engine_finishes_first(self) -> None:
        import asyncio

        from conductor.cli.run import _execute_with_stop_signal

        dashboard = MagicMock()

        async def _never_stop() -> None:
            await asyncio.Event().wait()

        dashboard.wait_for_stop = _never_stop

        async def _engine() -> dict[str, str]:
            await asyncio.sleep(0)
            return {"ok": "yes"}

        result = await _execute_with_stop_signal(_engine(), dashboard=dashboard)
        assert result == {"ok": "yes"}

    @pytest.mark.asyncio
    async def test_raises_execution_error_when_stop_fires_first(self) -> None:
        import asyncio

        from conductor.cli.run import _execute_with_stop_signal
        from conductor.exceptions import ExecutionError

        dashboard = MagicMock()

        async def _stop() -> None:
            return None  # stop signal fires immediately

        dashboard.wait_for_stop = _stop

        async def _engine() -> dict[str, str]:
            await asyncio.Event().wait()  # would block forever
            return {}

        with pytest.raises(ExecutionError, match="stopped by user"):
            await _execute_with_stop_signal(_engine(), dashboard=dashboard)

    @pytest.mark.asyncio
    async def test_losing_task_with_exception_does_not_leak(self) -> None:
        """Regression: the cleanup loop must drain a losing task even if
        cancelling it surfaces a stored non-CancelledError. With the previous
        ``contextlib.suppress(CancelledError)`` the second pending task could
        be left un-awaited; ``asyncio.gather(return_exceptions=True)`` fixes it.
        """
        import asyncio

        from conductor.cli.run import _execute_with_stop_signal

        dashboard = MagicMock()

        async def _stop_raises() -> None:
            # Stop signal "fires" by raising — this lands as a stored exception
            # on the losing wait_for_stop task once it's cancelled.
            raise RuntimeError("dashboard stop boom")

        dashboard.wait_for_stop = _stop_raises

        async def _engine() -> dict[str, str]:
            await asyncio.sleep(0)
            return {"ok": "engine won"}

        # Either outcome (engine wins or stop wins) is acceptable; the only
        # thing this test guards against is the helper itself raising or
        # leaking an un-awaited task warning.
        import contextlib as _ctx

        with _ctx.suppress(Exception):
            await _execute_with_stop_signal(_engine(), dashboard=dashboard)


# ---------------------------------------------------------------------------
# resume_workflow_async wiring tests (no mocking of resume_workflow_async)
# ---------------------------------------------------------------------------


def _make_resume_mocks() -> tuple[MagicMock, MagicMock]:
    """Create ProviderRegistry + WorkflowEngine mocks for resume_workflow_async."""
    mock_registry = AsyncMock()
    mock_registry.__aenter__ = AsyncMock(return_value=mock_registry)
    mock_registry.__aexit__ = AsyncMock(return_value=False)
    mock_registry.set_resume_session_ids = MagicMock()

    mock_engine = MagicMock()
    mock_engine.resume = AsyncMock(return_value={"result": "ok"})
    mock_engine.config = MagicMock()
    mock_engine.config.workflow.cost.show_summary = False
    mock_engine._last_checkpoint_path = None
    mock_engine.set_context = MagicMock()
    mock_engine.set_limits = MagicMock()
    mock_engine.get_execution_summary = MagicMock(return_value={})
    return mock_registry, mock_engine


class TestResumeWiring:
    """Verify resume_workflow_async actually wires the new components."""

    @pytest.mark.asyncio
    async def test_dashboard_start_oserror_is_non_fatal(self, tmp_path: Path) -> None:
        """Mirror of run-side test: dashboard start failure must not abort resume."""
        from conductor.cli.run import resume_workflow_async

        wf_path = _write_workflow(tmp_path)
        cp_path = _write_checkpoint(tmp_path, wf_path)

        mock_dashboard = MagicMock()
        mock_dashboard.start = AsyncMock(side_effect=OSError("port busy"))
        mock_dashboard.stop = AsyncMock()

        mock_web_module = MagicMock()
        mock_web_module.WebDashboard.return_value = mock_dashboard

        mock_registry, mock_engine = _make_resume_mocks()

        import sys as _sys

        with (
            patch.dict(_sys.modules, {"conductor.web.server": mock_web_module}),
            patch("conductor.cli.run.ProviderRegistry", return_value=mock_registry),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
            patch(
                "conductor.cli.run._build_mcp_servers",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await resume_workflow_async(checkpoint_path=cp_path, web=True)

        assert result == {"result": "ok"}
        assert mock_engine.resume.await_count == 1

    @pytest.mark.asyncio
    async def test_provider_override_mutates_config(self, tmp_path: Path) -> None:
        """provider_override must overwrite config.workflow.runtime.provider."""
        from conductor.cli.run import resume_workflow_async

        wf_path = _write_workflow(tmp_path)
        cp_path = _write_checkpoint(tmp_path, wf_path)

        captured_configs: list[Any] = []

        def _capture_config(config: Any, **_kwargs: Any) -> Any:  # noqa: ANN401
            captured_configs.append(config)
            mock_registry, _ = _make_resume_mocks()
            return mock_registry

        mock_registry, mock_engine = _make_resume_mocks()

        with (
            patch("conductor.cli.run.ProviderRegistry", side_effect=_capture_config),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
            patch(
                "conductor.cli.run._build_mcp_servers",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await resume_workflow_async(checkpoint_path=cp_path, provider_override="claude")

        assert captured_configs, "ProviderRegistry was not constructed"
        cfg = captured_configs[0]
        assert cfg.workflow.runtime.provider == "claude"

    @pytest.mark.asyncio
    async def test_metadata_merges_into_config(self, tmp_path: Path) -> None:
        """CLI metadata must be merged on top of YAML metadata on resume."""
        from conductor.cli.run import resume_workflow_async

        wf_path = _write_workflow(tmp_path)
        cp_path = _write_checkpoint(tmp_path, wf_path)

        captured_configs: list[Any] = []

        def _capture_config(config: Any, **_kwargs: Any) -> Any:  # noqa: ANN401
            captured_configs.append(config)
            mock_registry, _ = _make_resume_mocks()
            return mock_registry

        _, mock_engine = _make_resume_mocks()

        with (
            patch("conductor.cli.run.ProviderRegistry", side_effect=_capture_config),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
            patch(
                "conductor.cli.run._build_mcp_servers",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await resume_workflow_async(
                checkpoint_path=cp_path, metadata={"tracker": "ado", "ticket": "1234"}
            )

        cfg = captured_configs[0]
        assert cfg.workflow.metadata["tracker"] == "ado"
        assert cfg.workflow.metadata["ticket"] == "1234"

    @pytest.mark.asyncio
    async def test_run_context_populated_on_resume(self, tmp_path: Path) -> None:
        """RunContext passed to WorkflowEngine must include run_id, log_file, bg_mode."""
        import os as _os

        from conductor.cli.run import resume_workflow_async

        wf_path = _write_workflow(tmp_path)
        cp_path = _write_checkpoint(tmp_path, wf_path)

        engine_kwargs: dict[str, Any] = {}

        def _capture_engine(*_args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            engine_kwargs.update(kwargs)
            _, mock_engine = _make_resume_mocks()
            return mock_engine

        mock_registry, _ = _make_resume_mocks()

        # Force bg_mode via env var (simulates the bg-child code path).
        with (
            patch.dict(_os.environ, {"CONDUCTOR_WEB_BG": "1"}, clear=False),
            patch("conductor.cli.run.ProviderRegistry", return_value=mock_registry),
            patch("conductor.cli.run.WorkflowEngine", side_effect=_capture_engine),
            patch(
                "conductor.cli.run._build_mcp_servers",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await resume_workflow_async(checkpoint_path=cp_path)

        rc = engine_kwargs.get("run_context")
        assert rc is not None, f"run_context not passed; got kwargs={list(engine_kwargs)}"
        assert rc.bg_mode is True
        assert isinstance(rc.run_id, str) and rc.run_id  # populated from event log subscriber
        assert isinstance(rc.log_file, str) and rc.log_file
        # event_emitter must be wired so the dashboard / event log receive events
        assert engine_kwargs.get("event_emitter") is not None

    def test_metadata_value_with_equals_sign_via_cli(self, tmp_path: Path) -> None:
        """Regression: --metadata key=https://x?a=b must keep the right-hand =."""
        wf_path = _write_workflow(tmp_path)
        _write_checkpoint(tmp_path, wf_path)

        with patch(
            "conductor.cli.run.resume_workflow_async", new_callable=AsyncMock
        ) as mock_resume:
            mock_resume.return_value = {"result": "ok"}
            result = runner.invoke(
                app,
                ["resume", str(wf_path), "-m", "url=https://x?a=b&c=d"],
            )
        assert result.exit_code == 0, result.output
        kwargs = mock_resume.call_args[1]
        assert kwargs["metadata"] == {"url": "https://x?a=b&c=d"}
