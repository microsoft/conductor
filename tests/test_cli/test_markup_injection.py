"""End-to-end markup safety across the CLI and gates (issue #406).

``tests/test_console.py`` covers the primitives. This file drives the real
commands and handlers, because the primitives being correct says nothing about
whether a given call site actually uses them — which is precisely how #387
fixed ``cli/run.py`` and still left ``title=`` broken in the function it
changed, and how two commands written the same week reintroduced the pattern.

Every value here is one a user or a fetched third-party repository can supply:
a workflow ``name:``, a for-each key derived from agent output, a PID file
stem, a plugin or marketplace name from a cloned checkout.

Both failure modes are asserted throughout. A crash-only suite cannot tell a
correct fix from one that silently deletes the value, and the silent half is
the one that ships unnoticed.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app

runner = CliRunner()

# ``/``-leading tokens raise MarkupError; the rest are silently deleted.
CRASHING = "[/etc/x]"
DELETED = "[dim]"


def _write_workflow(path: Path, name: str) -> Path:
    path.write_text(
        "workflow:\n"
        f'  name: "{name}"\n'
        '  entry_point: "start"\n'
        "agents:\n"
        '  - name: "start"\n'
        "    type: script\n"
        '    command: "echo"\n'
        '    args: ["hi"]\n',
        encoding="utf-8",
    )
    return path


class TestValidateRendersWorkflowNames:
    """The lead repro: ``conductor validate`` died on a bracketed name."""

    @pytest.mark.parametrize("name", [f"probe {CRASHING} name", f"probe {DELETED} name"])
    def test_bracketed_workflow_name_survives(self, tmp_path: Path, name: str) -> None:
        wf = _write_workflow(tmp_path / "wf.yaml", name)
        result = runner.invoke(app, ["validate", str(wf)])
        assert result.exit_code == 0, result.output
        assert name in result.output

    def test_bracketed_description_survives(self, tmp_path: Path) -> None:
        wf = tmp_path / "wf.yaml"
        wf.write_text(
            "workflow:\n"
            '  name: "wf"\n'
            f'  description: "handles {CRASHING} paths"\n'
            '  entry_point: "start"\n'
            "agents:\n"
            '  - name: "start"\n'
            "    type: script\n"
            '    command: "echo"\n'
            '    args: ["hi"]\n',
            encoding="utf-8",
        )
        result = runner.invoke(app, ["validate", str(wf)])
        assert result.exit_code == 0, result.output
        assert f"handles {CRASHING} paths" in result.output

    @pytest.mark.parametrize("agent_name", [f"step{CRASHING}", f"step{DELETED}"])
    def test_bracketed_agent_name_survives_the_agents_table(
        self, tmp_path: Path, agent_name: str
    ) -> None:
        wf = tmp_path / "wf.yaml"
        wf.write_text(
            "workflow:\n"
            '  name: "wf"\n'
            f'  entry_point: "{agent_name}"\n'
            "agents:\n"
            f'  - name: "{agent_name}"\n'
            "    type: script\n"
            '    command: "echo"\n'
            '    args: ["hi"]\n',
            encoding="utf-8",
        )
        result = runner.invoke(app, ["validate", str(wf)])
        assert result.exit_code == 0, result.output
        assert agent_name in result.output


class TestShowRendersWorkflowNames:
    """``conductor show`` prints the same fields through ``output_console``."""

    @pytest.mark.parametrize("name", [f"probe {CRASHING} name", f"probe {DELETED} name"])
    def test_bracketed_workflow_name_survives(self, tmp_path: Path, name: str) -> None:
        wf = _write_workflow(tmp_path / "wf.yaml", name)
        result = runner.invoke(app, ["show", str(wf)])
        assert result.exit_code == 0, result.output
        assert name in result.output


class TestStatusAndStopRenderWorkflowNames:
    """``conductor status`` (#389) was written against the unfixed pattern.

    The crash mode is unreachable here — the value is a path *stem*, which
    cannot contain ``/`` — but corruption still damages the name a user reads
    to pick a port for ``conductor stop``.
    """

    @pytest.fixture()
    def pid_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        runs = tmp_path / "runs"
        runs.mkdir()
        monkeypatch.setattr("conductor.cli.pid.pid_dir", lambda: runs)
        return runs

    def _write_pid(self, pid_dir: Path, stem: str) -> None:
        (pid_dir / f"{stem}-8124.pid").write_text(
            json.dumps(
                {
                    "pid": 610745,
                    "port": 8124,
                    "workflow": f"/tmp/{stem}.yaml",
                    "url": "http://127.0.0.1:8124",
                    "started_at": "2026-01-01T00:00:00Z",
                    "run_id": "a1b2c3d4",
                }
            ),
            encoding="utf-8",
        )

    def test_status_shows_the_whole_workflow_name(self, pid_dir: Path) -> None:
        self._write_pid(pid_dir, f"report {DELETED} run")
        with patch("conductor.cli.pid._is_process_alive", return_value=True):
            result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        assert f"report {DELETED} run" in result.output

    def test_stop_listing_shows_the_whole_workflow_name(self, pid_dir: Path) -> None:
        self._write_pid(pid_dir, f"report {DELETED} run")
        self._write_pid(pid_dir, "other")
        with patch("conductor.cli.pid._is_process_alive", return_value=True):
            result = runner.invoke(app, ["stop"])
        assert f"report {DELETED} run" in result.output


class TestForEachIterationIdentitySurvives:
    """``verbose_log_section``'s panel title — corrupted on every run today.

    The engine rewrites a for-each member's name to ``<agent>[<key>]`` so
    interleaved verbose output can be attributed to one iteration. A key from
    ``key_by:`` is agent-derived, so a lowercase key erased exactly the
    identity the qualified name exists to carry, and a ``/`` key killed the run
    from a logging call. ``is_verbose()``/``is_full()`` both default true, so
    this is a plain ``conductor run``.
    """

    KEYS = ["0", "task1", "kpi-7", "docs/a.md", "/etc/x", "#42", "user@host"]

    @pytest.mark.parametrize("key", KEYS)
    def test_console_panel_title_keeps_the_key(self, key: str) -> None:
        from conductor.cli import run as run_module

        title = f"Prompt for 'review[{key}]'"
        with (
            patch("conductor.cli.app.is_verbose", return_value=True),
            patch("conductor.cli.app.is_full", return_value=True),
            run_module._verbose_console.capture() as captured,
        ):
            run_module.verbose_log_section(title, "body")
        assert "".join(title.split()) in "".join(captured.get().split())

    @pytest.mark.parametrize("key", KEYS)
    def test_file_panel_title_keeps_the_key(self, tmp_path: Path, key: str) -> None:
        """The file sink has no verbosity gate, so it is live on ``--log-file``."""
        from conductor.cli import run as run_module

        log = tmp_path / "run.log"
        title = f"Prompt for 'review[{key}]'"
        try:
            run_module.init_file_logging(log)
            with (
                patch("conductor.cli.app.is_verbose", return_value=False),
                patch("conductor.cli.app.is_full", return_value=False),
            ):
                run_module.verbose_log_section(title, "body")
        finally:
            run_module.close_file_logging()
        written = "".join(log.read_text(encoding="utf-8").split())
        assert "".join(title.split()) in written

    def test_every_iteration_renders_a_distinct_title(self) -> None:
        """The regression in one assertion: all titles used to read the same."""
        from conductor.cli import run as run_module

        rendered = []
        for key in ["task1", "task2", "task3"]:
            with (
                patch("conductor.cli.app.is_verbose", return_value=True),
                patch("conductor.cli.app.is_full", return_value=True),
                run_module._verbose_console.capture() as captured,
            ):
                run_module.verbose_log_section(f"Prompt for 'review[{key}]'", "body")
            rendered.append("".join(captured.get().split()))
        assert len(set(rendered)) == 3


class TestDialogRendersAgentNames:
    """``gates/dialog.py`` builds a Panel title from the same qualified name."""

    @pytest.mark.parametrize("key", ["task1", "/etc/x"])
    def test_dialog_opening_title_keeps_the_agent_name(self, key: str) -> None:
        from rich.panel import Panel

        from conductor.config.schema import AgentDef
        from conductor.gates.dialog import DialogHandler

        console = MagicMock()
        handler = DialogHandler(console=console)
        agent = AgentDef(name=f"review[{key}]", prompt="p")

        handler._display_dialog_start(agent, {"out": 1}, "question?")

        titles = [
            call.args[0].title
            for call in console.print.call_args_list
            if call.args and isinstance(call.args[0], Panel) and call.args[0].title is not None
        ]
        assert any(f"review[{key}]" in str(title) for title in titles), titles


class TestErrorPanelsRenderExceptionText:
    """``print_error`` titles and bodies carry arbitrary exception text."""

    @pytest.mark.parametrize("message", [f"cannot read {CRASHING}", f"cannot read {DELETED}"])
    def test_exception_message_survives(self, message: str) -> None:
        from conductor.cli.app import console, print_error

        with console.capture() as captured:
            print_error(RuntimeError(message))
        assert "".join(message.split()) in "".join(captured.get().split())

    def test_error_type_survives_in_the_title(self) -> None:
        """``error_type`` defaults to the class name, which is the title."""
        from conductor.cli.app import console, print_error
        from conductor.exceptions import ConductorError

        class Weird(ConductorError):
            @property
            def error_type(self) -> str:
                return f"Weird{CRASHING}Type"

        with console.capture() as captured:
            print_error(Weird("boom"))
        assert f"Weird{CRASHING}Type" in "".join(captured.get().split())


class TestFetchedPluginMetadataRenders:
    """#398 made these strings third-party rather than the author's own YAML.

    ``conductor plugin list`` and ``conductor validate`` render plugin,
    marketplace, skill and subagent names read out of a cloned repository.
    """

    @pytest.mark.parametrize("marker", [CRASHING, DELETED])
    def test_plugin_path_survives_the_report(self, tmp_path: Path, marker: str) -> None:
        """The plugin *root* is rendered, and a path can contain brackets.

        The plugin *name* cannot — it is charset-validated against
        ``[A-Za-z0-9_.-]+`` because it is joined into the CLI's
        delimiter-separated tool list — so the path and the MCP server name
        are the reachable surfaces here.
        """
        root = tmp_path / f"plug{marker}"
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "acme", "description": "d", "version": "1.0.0"}),
            encoding="utf-8",
        )
        (root / "agents").mkdir()
        (root / "agents" / "review.agent.md").write_text(
            "---\nname: review\ndescription: reviews things\n---\n\nBody.\n",
            encoding="utf-8",
        )

        wf = tmp_path / "wf.yaml"
        wf.write_text(
            "workflow:\n"
            '  name: "wf"\n'
            '  entry_point: "start"\n'
            "  runtime:\n"
            "    provider: copilot\n"
            f'    plugins: ["{root}"]\n'
            "agents:\n"
            '  - name: "start"\n'
            '    prompt: "hi"\n'
            "    output:\n"
            "      answer:\n"
            "        type: string\n",
            encoding="utf-8",
        )

        result = runner.invoke(app, ["validate", str(wf)])
        assert result.exit_code == 0, result.output
        rendered = "".join(result.output.split())
        assert f"plug{marker}" in rendered
        assert "acme:review" in rendered

    @pytest.mark.parametrize("message", [f"bad manifest {CRASHING}", f"bad manifest {DELETED}"])
    def test_plugin_error_text_survives_the_report(self, message: str) -> None:
        """``str(exc)`` from the plugin layer reaches a markup-parsing sink."""
        from conductor.cli.app import console, print_error
        from conductor.plugins.errors import PluginManifestError

        with console.capture() as captured:
            print_error(PluginManifestError(message))
        assert "".join(message.split()) in "".join(captured.get().split())


class TestGateResponseRendersServerPayloads:
    """``conductor gate respond`` prints a ``detail`` string from the API."""

    @pytest.mark.parametrize("detail", [f"bad {CRASHING}", f"bad {DELETED}"])
    def test_server_detail_survives(self, detail: str) -> None:
        import httpx

        from conductor.cli.gate import console

        response = httpx.Response(409, json={"error": detail})
        with patch("httpx.post", return_value=response), console.capture() as captured:
            result = runner.invoke(
                app, ["gate", "respond", "--port", "8080", "--choice", "ok", "--agent", "a"]
            )
        assert result.exit_code == 1
        assert detail in captured.get()
