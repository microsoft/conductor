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

    def test_repeatable_and_scalar_flags(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, ServeOptions] = {}
        monkeypatch.setattr(
            mcp_module, "_serve_impl", lambda options: captured.__setitem__("options", options)
        )

        workflow_dir_a = tmp_path / "a"
        workflow_dir_b = tmp_path / "b"
        workflow_dir_a.mkdir()
        workflow_dir_b.mkdir()

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
