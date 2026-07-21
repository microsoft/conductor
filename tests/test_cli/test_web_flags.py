"""Tests for --web, --web-port, and --web-bg CLI flags.

This module tests:
- CLI flag acceptance and parameter passing
- Missing web dependency detection with actionable error
- Dashboard startup failure handling (non-fatal)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app
from conductor.config.schema import ProviderSettings

runner = CliRunner()

# Minimal workflow YAML for test fixtures
_WORKFLOW_YAML = """\
workflow:
  name: test-workflow
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: "Hello"
    routes:
      - to: $end

output:
  result: "done"
"""


@pytest.fixture()
def workflow_file(tmp_path: Path) -> Path:
    """Create a minimal workflow file for testing."""
    f = tmp_path / "test.yaml"
    f.write_text(_WORKFLOW_YAML)
    return f


class TestWebFlagAcceptance:
    """Test that --web, --web-port, and --web-bg flags are accepted by the CLI."""

    def test_web_flag_passed_to_run_workflow_async(self, workflow_file: Path) -> None:
        """Test --web flag is passed through to run_workflow_async."""
        with patch("conductor.cli.run.run_workflow_async") as mock_run:
            mock_run.return_value = {"result": "done"}

            runner.invoke(app, ["run", str(workflow_file), "--web"])

            assert mock_run.called
            _, kwargs = mock_run.call_args
            assert kwargs["web"] is True

    def test_web_port_flag_passed(self, workflow_file: Path) -> None:
        """Test --web-port value is passed through."""
        with patch("conductor.cli.run.run_workflow_async") as mock_run:
            mock_run.return_value = {"result": "done"}

            runner.invoke(app, ["run", str(workflow_file), "--web", "--web-port", "8080"])

            assert mock_run.called
            _, kwargs = mock_run.call_args
            assert kwargs["web"] is True
            assert kwargs["web_port"] == 8080

    def test_web_bg_flag_passed(self, workflow_file: Path) -> None:
        """Test --web-bg flag forks a background process (does not call run_workflow_async)."""
        from pathlib import Path as _Path

        from conductor.cli.bg_runner import BackgroundLaunch

        with patch("conductor.cli.bg_runner.launch_background") as mock_launch:
            mock_launch.return_value = BackgroundLaunch(
                url="http://127.0.0.1:9999",
                stderr_log=_Path("/tmp/conductor-test-deadbeef.bg.stderr.log"),
                stdout_log=_Path("/tmp/conductor-test-deadbeef.bg.stdout.log"),
                run_id="deadbeef",
            )

            result = runner.invoke(app, ["run", str(workflow_file), "--web-bg"])

            assert result.exit_code == 0
            assert mock_launch.called
            _, kwargs = mock_launch.call_args
            assert kwargs["workflow_path"] == workflow_file
            assert "http://127.0.0.1:9999" in result.output

    def test_silent_web_bg_suppresses_dashboard_output(self, workflow_file: Path) -> None:
        """Test --silent suppresses --web-bg parent-process dashboard output."""
        from pathlib import Path as _Path

        from conductor.cli.bg_runner import BackgroundLaunch

        with patch("conductor.cli.bg_runner.launch_background") as mock_launch:
            mock_launch.return_value = BackgroundLaunch(
                url="http://127.0.0.1:9999",
                stderr_log=_Path("/tmp/conductor-test-deadbeef.bg.stderr.log"),
                stdout_log=_Path("/tmp/conductor-test-deadbeef.bg.stdout.log"),
                run_id="deadbeef",
            )

            result = runner.invoke(app, ["--silent", "run", str(workflow_file), "--web-bg"])

            assert result.exit_code == 0
            assert mock_launch.called
            assert "http://127.0.0.1:9999" not in result.output
            assert "Dashboard" not in result.output
            assert "Workflow running in background" not in result.output
            assert "Child stderr log" not in result.output

    def test_web_flags_default_values(self, workflow_file: Path) -> None:
        """Test that web flags default to False/0 when not specified."""
        with patch("conductor.cli.run.run_workflow_async") as mock_run:
            mock_run.return_value = {"result": "done"}

            runner.invoke(app, ["run", str(workflow_file)])

            assert mock_run.called
            _, kwargs = mock_run.call_args
            assert kwargs["web"] is False
            assert kwargs["web_port"] == 0
            assert kwargs["web_bg"] is False

    def test_web_compatible_with_existing_flags(self, workflow_file: Path) -> None:
        """Test --web works alongside existing flags like --skip-gates."""
        with patch("conductor.cli.run.run_workflow_async") as mock_run:
            mock_run.return_value = {"result": "done"}

            runner.invoke(
                app,
                ["run", str(workflow_file), "--web", "--skip-gates", "--no-interactive"],
            )

            assert mock_run.called
            call_args = mock_run.call_args
            # skip_gates is the 4th positional arg
            assert call_args[0][3] is True
            _, kwargs = call_args
            assert kwargs["web"] is True


class TestWebBgMutualExclusion:
    """Test that --web and --web-bg are mutually exclusive."""

    def test_web_and_web_bg_mutually_exclusive(self, workflow_file: Path) -> None:
        """Test that --web and --web-bg together produce an error."""
        result = runner.invoke(app, ["run", str(workflow_file), "--web", "--web-bg"])
        assert result.exit_code != 0


class TestLaunchBackgroundSilentFlag:
    """Regression tests for issue #196 — bg_runner must not pass --silent.

    The child's ``stdout``/``stderr`` are already redirected to ``DEVNULL``, so
    ``--silent`` adds nothing for the user. Worse, it sets
    ``verbose_mode=False`` in the child, which gates provider-side SDK event
    logging that ``--log-file`` would otherwise capture.
    """

    def test_launch_background_does_not_pass_silent(self, tmp_path: Path) -> None:
        """``launch_background`` must not inject ``--silent`` into the cmd.

        Asserts both the negative contract (no ``--silent``) and the positive
        contract (expected flags still present) so that an accidental
        regression that drops both ``--silent`` *and* another flag would
        still be caught. Mirrors ``test_builds_resume_subcommand_with_workflow``
        on the resume side.
        """
        from conductor.cli import bg_runner

        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text("workflow: {name: x, entry_point: a}\nagents: []\n")

        captured: dict[str, list[str]] = {}

        def _fake_popen(cmd: list[str], **_kwargs: object) -> MagicMock:
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
            launch = bg_runner.launch_background(
                workflow_path=wf_path,
                inputs={"question": "hello"},
                provider_override="copilot",
                skip_gates=True,
                metadata={"tracker": "ado"},
                web_port=9099,
            )

        assert launch.url == "http://127.0.0.1:9099"
        cmd = captured["cmd"]
        # Issue #196: ``--silent`` must NOT be injected — see class docstring.
        assert "--silent" not in cmd
        # Positive contract: expected flags must still be present.
        assert "run" in cmd
        assert str(wf_path) in cmd
        assert "--web" in cmd
        assert "--web-port" in cmd
        assert "9099" in cmd
        assert "--no-interactive" in cmd
        assert "--input" in cmd
        assert "question=hello" in cmd
        assert "--provider" in cmd and "copilot" in cmd
        assert "--skip-gates" in cmd
        assert "--metadata" in cmd
        assert "tracker=ado" in cmd


class TestDashboardStartupFailure:
    """Test that dashboard startup failure is non-fatal."""

    def test_dashboard_start_failure_continues_workflow(self, workflow_file: Path) -> None:
        """Test that when dashboard fails, CLI still succeeds."""
        with patch("conductor.cli.run.run_workflow_async") as mock_run:
            mock_run.return_value = {"result": "done"}
            result = runner.invoke(app, ["run", str(workflow_file), "--web"])
            assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_dashboard_start_oserror_is_non_fatal(self) -> None:
        """Test the actual code path: dashboard.start() OSError is caught.

        Mocks WebDashboard so start() raises OSError, verifies the
        workflow result is still returned (dashboard=None after failure).
        """
        from conductor.cli.run import run_workflow_async

        mock_dashboard = MagicMock()
        mock_dashboard.start = AsyncMock(side_effect=OSError("Address already in use"))
        mock_dashboard.stop = AsyncMock()

        mock_web_module = MagicMock()
        mock_web_module.WebDashboard.return_value = mock_dashboard

        # Mock config loading and the full engine flow
        mock_config = MagicMock()
        mock_config.workflow.name = "test"
        mock_config.workflow.entry_point = "agent1"
        mock_config.agents = []
        mock_config.workflow.runtime.provider = ProviderSettings(name="copilot")
        mock_config.workflow.limits.max_iterations = 50
        mock_config.workflow.limits.timeout_seconds = None
        mock_config.workflow.cost.show_summary = False
        mock_config.tools = None
        mock_config.mcp_servers = []

        mock_engine = MagicMock()
        mock_engine.run = AsyncMock(return_value={"result": "done"})
        mock_engine._last_checkpoint_path = None
        mock_engine.get_execution_summary.return_value = {}

        with (
            patch("conductor.cli.run.load_config", return_value=mock_config),
            patch.dict(sys.modules, {"conductor.web.server": mock_web_module}),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
            patch("conductor.cli.run.ProviderRegistry") as mock_registry,
            patch(
                "conductor.cli.run._build_mcp_servers",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = False
            mock_registry.return_value.__aenter__ = AsyncMock(return_value=mock_registry)
            mock_registry.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await run_workflow_async(
                Path("/tmp/fake.yaml"),
                {},
                web=True,
            )
            assert result == {"result": "done"}


# ---------------------------------------------------------------------------
# --web-bg + human_gate validation (external-workflow-friction Item 4)
# ---------------------------------------------------------------------------

_GATE_WORKFLOW_YAML = """\
workflow:
  name: gate-workflow
  entry_point: ask

agents:
  - name: ask
    type: human_gate
    prompt: "Continue?"
    options:
      - label: "Yes"
        value: yes
        route: $end
      - label: "No"
        value: no
        route: $end

output:
  result: "done"
"""


@pytest.fixture()
def gate_workflow_file(tmp_path: Path) -> Path:
    """Workflow containing a ``human_gate`` agent."""
    f = tmp_path / "gate.yaml"
    f.write_text(_GATE_WORKFLOW_YAML)
    return f


def _fake_bg_launch(url: str = "http://127.0.0.1:9999") -> object:
    """Build a ``BackgroundLaunch`` stand-in for a mocked ``--web-bg`` fork."""
    from conductor.cli.bg_runner import BackgroundLaunch

    return BackgroundLaunch(
        url=url,
        stderr_log=Path("/tmp/conductor-test-deadbeef.bg.stderr.log"),
        stdout_log=Path("/tmp/conductor-test-deadbeef.bg.stdout.log"),
        run_id="deadbeef",
    )


# Pin a wide console so the Rich-rendered gate notice never wraps mid-token on
# CI's narrow non-TTY width (see repo memory on width-sensitive CLI tests).
_WIDE = {"COLUMNS": "200"}


class TestWebBgHumanGateNotice:
    """``--web-bg`` + ``human_gate`` now forks and points at gate resolution.

    Background human gates are resolvable from the dashboard modal or the
    ``conductor gate-respond`` CLI (issue #286), so ``--web-bg`` no longer
    aborts pre-fork. Instead the launch proceeds and prints a notice naming
    the dashboard URL / port and ``conductor gate-respond``. ``--skip-gates``
    still auto-selects the first option, so no notice is shown in that mode.
    """

    def test_run_web_bg_with_human_gate_proceeds_with_notice(
        self, gate_workflow_file: Path
    ) -> None:
        """``run --web-bg`` + ``human_gate`` (no ``--skip-gates``) → fork + notice."""
        with patch("conductor.cli.bg_runner.launch_background") as mock_launch:
            mock_launch.return_value = _fake_bg_launch()
            result = runner.invoke(app, ["run", str(gate_workflow_file), "--web-bg"], env=_WIDE)

        assert result.exit_code == 0
        assert mock_launch.called
        # The notice must name the problem and how to resolve it. It is emitted
        # to stderr (Console(stderr=True)); Click 8.3+ separates the streams.
        combined = (result.output or "") + (result.stderr or "")
        assert "human_gate" in combined
        assert "gate-respond" in combined
        assert "9999" in combined  # port extracted from the dashboard URL

    def test_run_web_bg_with_human_gate_and_skip_gates_proceeds_without_notice(
        self, gate_workflow_file: Path
    ) -> None:
        """``--skip-gates`` auto-selects; fork proceeds and no gate notice is shown."""
        with patch("conductor.cli.bg_runner.launch_background") as mock_launch:
            mock_launch.return_value = _fake_bg_launch()
            result = runner.invoke(
                app, ["run", str(gate_workflow_file), "--web-bg", "--skip-gates"], env=_WIDE
            )

        assert result.exit_code == 0
        assert mock_launch.called
        combined = (result.output or "") + (result.stderr or "")
        assert "gate-respond" not in combined

    def test_run_web_bg_without_gate_shows_no_notice(self, tmp_path: Path) -> None:
        """A gate-free workflow forks without any gate notice."""
        f = tmp_path / "plain.yaml"
        f.write_text(_WORKFLOW_YAML)
        with patch("conductor.cli.bg_runner.launch_background") as mock_launch:
            mock_launch.return_value = _fake_bg_launch()
            result = runner.invoke(app, ["run", str(f), "--web-bg"], env=_WIDE)

        assert result.exit_code == 0
        assert mock_launch.called
        combined = (result.output or "") + (result.stderr or "")
        assert "gate-respond" not in combined

    def test_resume_web_bg_with_human_gate_proceeds_with_notice(
        self, gate_workflow_file: Path
    ) -> None:
        """Same behavior applies to ``resume --web-bg`` (run/resume parity)."""
        with patch("conductor.cli.bg_runner.launch_background_resume") as mock_launch:
            mock_launch.return_value = _fake_bg_launch()
            result = runner.invoke(app, ["resume", str(gate_workflow_file), "--web-bg"], env=_WIDE)

        assert result.exit_code == 0
        assert mock_launch.called
        combined = (result.output or "") + (result.stderr or "")
        assert "human_gate" in combined
        assert "gate-respond" in combined

    def test_run_web_bg_with_human_gate_inside_for_each_proceeds_with_notice(
        self, tmp_path: Path
    ) -> None:
        """``human_gate`` nested in a ``for_each.agent`` also triggers the notice.

        The top-level walk over ``config.agents`` misses inline agents declared
        inside ``for_each`` groups, so the notice check must scan those too.
        """
        for_each_yaml = """\
workflow:
  name: gate-in-foreach
  entry_point: source

agents:
  - name: source
    type: agent
    prompt: "List items"
    output:
      items:
        type: array
        items: { type: string }
    routes:
      - to: loop

for_each:
  - name: loop
    type: for_each
    source: source.output.items
    as: item
    agent:
      name: inner
      type: human_gate
      prompt: "Approve {{ item }}?"
      options:
        - label: "Yes"
          value: yes
          route: $end
        - label: "No"
          value: no
          route: $end

output:
  result: "done"
"""
        f = tmp_path / "gate_in_foreach.yaml"
        f.write_text(for_each_yaml)

        with patch("conductor.cli.bg_runner.launch_background") as mock_launch:
            mock_launch.return_value = _fake_bg_launch()
            result = runner.invoke(app, ["run", str(f), "--web-bg"], env=_WIDE)

        assert result.exit_code == 0
        assert mock_launch.called
        combined = (result.output or "") + (result.stderr or "")
        assert "human_gate" in combined
        assert "gate-respond" in combined

    def test_resume_web_bg_from_checkpoint_only_proceeds_with_notice(
        self, gate_workflow_file: Path, tmp_path: Path
    ) -> None:
        """``resume --from <checkpoint> --web-bg`` (no workflow arg) still notices.

        ``resolved_workflow`` is ``None`` in this code path; the workflow path
        is recovered from the checkpoint JSON so the gate notice still fires.
        """
        import json as _json

        checkpoint = tmp_path / "ckpt.json"
        checkpoint.write_text(
            _json.dumps(
                {
                    "workflow_path": str(gate_workflow_file.resolve()),
                    "workflow_hash": "deadbeef",
                    "workflow_name": "gate-workflow",
                    "failed_agent": "ask",
                    "completed_agents": [],
                    "context": {},
                    "timestamp": "2026-05-27T00:00:00",
                    "error_message": "test",
                }
            )
        )

        with patch("conductor.cli.bg_runner.launch_background_resume") as mock_launch:
            mock_launch.return_value = _fake_bg_launch()
            result = runner.invoke(
                app, ["resume", "--from", str(checkpoint), "--web-bg"], env=_WIDE
            )

        assert result.exit_code == 0
        assert mock_launch.called
        combined = (result.output or "") + (result.stderr or "")
        assert "human_gate" in combined
        assert "gate-respond" in combined
