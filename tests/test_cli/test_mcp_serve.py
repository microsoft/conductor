"""Tests for the ``conductor mcp serve`` CLI command
(FR1, FR10, DD3, DD9, E8-T1, E8-T2, E8-T3, E8-T7).
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from typer.testing import CliRunner

import conductor.cli.mcp as mcp_module
from conductor.cli.app import app
from conductor.mcp.serve.options import ServeOptions

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
"""Matches the SGR escapes Rich emits, mirroring ``test_replay_command.py``.

Load-bearing for the help assertions: ``typer.rich_utils`` sets
``FORCE_TERMINAL`` at *import* time when ``GITHUB_ACTIONS`` is set, so on CI
Rich styles the help panel even though nothing is a TTY -- and its option
highlighter emits the leading dash as its own span
(``\\x1b[1;36m-\\x1b[0m\\x1b[1;36m-registry\\x1b[0m``), so the literal
``--registry`` never appears in the raw output. Stripping is the only
reliable fix: the flag is set before any test can patch the environment, and
pinning ``COLUMNS`` does not disable colour.
"""

_WIDE = {"COLUMNS": "200"}
"""Pinned width so a long flag such as ``--max-concurrent-runs`` is not
wrapped mid-token by Rich's option column, mirroring ``test_help_panels.py``.
Stripping ANSI alone would not survive that wrap."""

_REVIEW_PR_YAML = """\
workflow:
  name: review-pr
  description: Reviews a pull request.
  entry_point: worker
agents:
  - name: worker
    prompt: "Review it."
    output:
      result:
        type: string
output:
  result: "{{ worker.output.result }}"
"""


@asynccontextmanager
async def _fake_stdio_server():
    """A ``stdio_server()`` stand-in whose read side is already closed, so
    ``Server.run()`` returns almost immediately instead of blocking on this
    test process's real stdin.

    Safe to use for asserting the FR10 startup summary lands on stderr:
    ``serve_stdio`` prints that summary *before* it ever enters
    ``stdio_server()``, so replacing only the transport does not affect it.
    """
    send_stream, receive_stream = anyio.create_memory_object_stream(0)
    out_send, out_receive = anyio.create_memory_object_stream(0)
    await send_stream.aclose()
    try:
        yield receive_stream, out_send
    finally:
        await receive_stream.aclose()
        await out_send.aclose()
        await out_receive.aclose()


class TestServeHelp:
    """``conductor mcp --help`` / ``conductor mcp serve --help`` render."""

    def test_serve_help_renders(self) -> None:
        result = runner.invoke(app, ["mcp", "serve", "--help"], env=_WIDE)
        assert result.exit_code == 0
        rendered = _ANSI_RE.sub("", result.output)
        for flag in (
            "--registry",
            "--allow",
            "--deny",
            "--workflow-dir",
            "--toolsets",
            "--max-direct-tools",
            "--max-wait-seconds",
            "--tool-prefix",
            "--max-concurrent-runs",
            "--introspect-full",
            "--launch-dir",
        ):
            assert flag in rendered

    def test_mcp_group_help_renders(self) -> None:
        result = runner.invoke(app, ["mcp", "--help"])
        assert result.exit_code == 0
        assert "serve" in result.output

    def test_bare_mcp_invocation_shows_usage(self) -> None:
        result = runner.invoke(app, ["mcp"])
        assert result.exit_code == 2
        assert "Usage" in result.output


class TestServeFlagsReachOptions:
    """Flags parse and reach a correctly-populated ``ServeOptions``."""

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Isolate this test from whatever process ancestry it happens to
        # run under (issue #544): no Copilot host to detect, so the
        # default resolves to this process's own cwd, deterministically.
        monkeypatch.setattr(
            "conductor.mcp.serve.launch_dir.detect_copilot_ancestor_cwd", lambda: None
        )
        captured: dict[str, ServeOptions] = {}
        monkeypatch.setattr(
            mcp_module, "_serve_impl", lambda options: captured.__setitem__("options", options)
        )

        result = runner.invoke(app, ["mcp", "serve"])
        assert result.exit_code == 0, result.output

        options = captured["options"]
        assert options.registries is None
        assert options.allow == ()
        assert options.deny == ()
        assert options.workflow_dirs == ()
        assert options.toolsets == ("workflows", "runs")
        assert options.max_direct_tools == 25
        assert options.max_wait_seconds == 300
        assert options.tool_prefix is None
        assert options.max_concurrent_runs == 0
        assert options.introspect_full is False
        assert options.launch_dir == Path.cwd()

    def test_repeatable_and_scalar_flags(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, ServeOptions] = {}
        monkeypatch.setattr(
            mcp_module, "_serve_impl", lambda options: captured.__setitem__("options", options)
        )

        workflow_dir_a = tmp_path / "a"
        workflow_dir_b = tmp_path / "b"
        launch_dir = tmp_path / "launch"
        workflow_dir_a.mkdir()
        workflow_dir_b.mkdir()
        launch_dir.mkdir()

        result = runner.invoke(
            app,
            [
                "mcp",
                "serve",
                "--registry",
                "official",
                "--registry",
                "team",
                "--allow",
                "release-*",
                "--deny",
                "internal-*",
                "--workflow-dir",
                str(workflow_dir_a),
                "--workflow-dir",
                str(workflow_dir_b),
                "--toolsets",
                "workflows",
                "--toolsets",
                "introspect",
                "--max-direct-tools",
                "10",
                "--max-wait-seconds",
                "60",
                "--tool-prefix",
                "acme",
                "--max-concurrent-runs",
                "3",
                "--introspect-full",
                "--launch-dir",
                str(launch_dir),
            ],
        )
        assert result.exit_code == 0, result.output

        options = captured["options"]
        assert options.registries == ("official", "team")
        assert options.allow == ("release-*",)
        assert options.deny == ("internal-*",)
        assert options.workflow_dirs == (workflow_dir_a, workflow_dir_b)
        assert options.toolsets == ("workflows", "introspect")
        assert options.max_direct_tools == 10
        assert options.max_wait_seconds == 60
        assert options.tool_prefix == "acme"
        assert options.max_concurrent_runs == 3
        assert options.introspect_full is True
        assert options.launch_dir == launch_dir


class TestServeStdoutIsProtocolPure:
    """The startup summary lands on stderr; stdout carries nothing else."""

    def test_startup_summary_on_stderr_stdout_stays_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "conductor_home"))
        monkeypatch.setattr("conductor.mcp.serve.server.stdio_server", _fake_stdio_server)

        workflow_dir = tmp_path / "workflows"
        workflow_dir.mkdir()
        (workflow_dir / "review-pr.yaml").write_text(_REVIEW_PR_YAML, encoding="utf-8")

        result = runner.invoke(app, ["mcp", "serve", "--workflow-dir", str(workflow_dir)])

        assert result.exit_code == 0, result.output
        assert result.stdout == ""
        assert "review_pr" in result.stderr
        assert "exposing 1" in result.stderr
        assert "direct" in result.stderr

    def test_startup_summary_names_an_explicit_launch_dir_stdout_stays_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "conductor_home"))
        monkeypatch.setattr("conductor.mcp.serve.server.stdio_server", _fake_stdio_server)

        workflow_dir = tmp_path / "workflows"
        workflow_dir.mkdir()
        (workflow_dir / "review-pr.yaml").write_text(_REVIEW_PR_YAML, encoding="utf-8")
        launch_dir = tmp_path / "launch"
        launch_dir.mkdir()

        result = runner.invoke(
            app,
            [
                "mcp",
                "serve",
                "--workflow-dir",
                str(workflow_dir),
                "--launch-dir",
                str(launch_dir),
            ],
        )

        assert result.exit_code == 0, result.output
        assert result.stdout == ""
        assert str(launch_dir) in result.stderr


class TestServeLaunchDirValidation:
    """An invalid `--launch-dir` fails before the catalogue is built or
    stdio is opened (issue #544)."""

    def test_nonexistent_directory_fails_before_catalogue_is_built(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalogue_built = False

        def _boom(*args: object, **kwargs: object) -> None:
            nonlocal catalogue_built
            catalogue_built = True
            raise AssertionError("build_catalogue must not run for an invalid --launch-dir")

        monkeypatch.setattr("conductor.mcp.serve.catalogue.build_catalogue", _boom)

        missing = tmp_path / "does-not-exist"
        result = runner.invoke(app, ["mcp", "serve", "--launch-dir", str(missing)])

        assert result.exit_code == 1
        assert catalogue_built is False
        assert str(missing) in result.stderr
        assert "does not exist" in result.stderr
        assert "--launch-dir" in result.stderr
        assert result.stdout == ""

    def test_a_file_as_launch_dir_is_rejected_naming_the_remedy(self, tmp_path: Path) -> None:
        file_path = tmp_path / "not-a-dir.txt"
        file_path.write_text("hello", encoding="utf-8")

        result = runner.invoke(app, ["mcp", "serve", "--launch-dir", str(file_path)])

        assert result.exit_code == 1
        assert str(file_path) in result.stderr
        assert "not a directory" in result.stderr
        assert "--launch-dir" in result.stderr


class TestServeSimulatedPluginHostStartup:
    """A Copilot host process (identified by process ancestry) launching
    this server from its own install directory must resolve the launch
    directory to the *host's* cwd -- the motivating scenario for issue
    #544 -- rather than this process's own inherited (server-cwd)
    default."""

    def test_detected_host_cwd_wins_over_the_servers_own_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plugin_cwd = tmp_path / "plugin-install-dir"
        host_cwd = tmp_path / "actual-project"
        plugin_cwd.mkdir()
        host_cwd.mkdir()
        monkeypatch.chdir(plugin_cwd)
        monkeypatch.setattr(
            "conductor.mcp.serve.launch_dir.detect_copilot_ancestor_cwd", lambda: host_cwd
        )

        captured: dict[str, ServeOptions] = {}
        monkeypatch.setattr(
            mcp_module, "_serve_impl", lambda options: captured.__setitem__("options", options)
        )

        result = runner.invoke(app, ["mcp", "serve"])

        assert result.exit_code == 0, result.output
        assert captured["options"].launch_dir == host_cwd


class TestLazyImportBoundary:
    """``conductor.mcp.*`` (including the new process-inspection module) is
    imported lazily, only inside ``serve()``'s body -- an ordinary
    ``conductor`` invocation must never pay the ``mcp`` SDK or ``psutil``
    import cost (issue #544)."""

    def test_ordinary_help_does_not_import_mcp_sdk_or_psutil(self) -> None:
        import os
        import subprocess
        import sys

        script = (
            "import sys\n"
            "from conductor.cli.app import app\n"
            "assert 'psutil' not in sys.modules, 'psutil imported by importing the CLI app'\n"
            "assert 'mcp' not in sys.modules, 'mcp SDK imported by importing the CLI app'\n"
            "from typer.testing import CliRunner\n"
            "result = CliRunner().invoke(app, ['--help'])\n"
            "assert result.exit_code == 0, result.output\n"
            "assert 'psutil' not in sys.modules, 'psutil imported by --help'\n"
            "assert 'mcp' not in sys.modules, 'mcp SDK imported by --help'\n"
            "result = CliRunner().invoke(app, ['mcp', 'serve', '--help'])\n"
            "assert result.exit_code == 0, result.output\n"
            "assert 'psutil' not in sys.modules, 'psutil imported by mcp serve --help'\n"
            "assert 'mcp' not in sys.modules, 'mcp SDK imported by mcp serve --help'\n"
            "print('OK')\n"
        )
        env = dict(os.environ)
        env["CONDUCTOR_NO_UPDATE_CHECK"] = "1"
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "OK" in result.stdout
