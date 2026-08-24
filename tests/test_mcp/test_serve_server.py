"""Tests for wiring the frozen catalogue onto the low-level MCP ``Server``
and running it over stdio, plus the FR10 startup summary
(FR1, FR10, DD3, DD9, E8-T4, E8-T5, E8-T6).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from conductor import __version__
from conductor.cli.bg_runner import BackgroundLaunch
from conductor.fleet.records import RunRecord, write_run_record
from conductor.mcp.serve.catalogue import build_catalogue
from conductor.mcp.serve.options import ServeOptions
from conductor.mcp.serve.server import build_server, log_startup_summary
from conductor.registry.config import RegistriesConfig
from tests.test_mcp.conftest import write_path_registry

_REVIEW_PR_YAML = """\
workflow:
  name: review-pr
  description: Reviews a pull request across correctness, tests, and security.
  entry_point: worker
  input:
    pr_number:
      type: number
      required: true
      description: The PR number to review
agents:
  - name: worker
    prompt: "Review PR {{ pr_number }}"
    output:
      result:
        type: string
output:
  result: "{{ worker.output.result }}"
"""

_DEPLOY_YAML = """\
workflow:
  name: deploy
  description: Deploys the service to production.
  entry_point: worker
agents:
  - name: worker
    prompt: "Deploy it."
    output:
      result:
        type: string
output:
  result: "{{ worker.output.result }}"
"""


def _two_tool_catalogue(tmp_path: Path):
    entry = write_path_registry(
        tmp_path,
        name="official",
        workflows={"review-pr": _REVIEW_PR_YAML, "deploy": _DEPLOY_YAML},
    )
    config = RegistriesConfig(registries={"official": entry})
    return build_catalogue(ServeOptions(), registries_config=config, allow_network=False)


def _two_tool_catalogue_with_config(tmp_path: Path) -> tuple[Any, RegistriesConfig]:
    """Like :func:`_two_tool_catalogue`, but also returns the
    :class:`RegistriesConfig` used to build it, needed by tests that
    dispatch a direct-mode workflow tool through the live server (which
    resolves the workflow path via ``invoke.py``'s own registries lookup)."""
    entry = write_path_registry(
        tmp_path,
        name="official",
        workflows={"review-pr": _REVIEW_PR_YAML, "deploy": _DEPLOY_YAML},
    )
    config = RegistriesConfig(registries={"official": entry})
    catalogue = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)
    return catalogue, config


def _patch_registries_config(monkeypatch: pytest.MonkeyPatch, config: RegistriesConfig) -> None:
    """Mirrors ``test_serve_discovery.py``'s identical helper: points
    ``invoke.py``'s default ``load_registries_config()`` fallback at a
    test's in-memory :class:`RegistriesConfig`, needed by any path that
    reaches ``invoke_workflow_tool`` through the server's own ``call_tool``
    dispatch rather than passing ``registries_config`` directly."""
    monkeypatch.setattr("conductor.mcp.serve.invoke.load_registries_config", lambda: config)


def _make_fake_launch_background(calls: list[dict[str, Any]]) -> Any:
    """Mirrors ``test_serve_invoke.py``/``test_serve_discovery.py``'s
    identical stand-in: records the kwargs ``launch_background`` was
    called with and writes a real, discoverable :class:`RunRecord`."""

    def _fake(
        *,
        workflow_path: Path,
        inputs: dict[str, Any],
        provider_override: str | None = None,
        skip_gates: bool = False,
        web_port: int = 0,
        metadata: dict[str, str] | None = None,
        **_ignored: Any,
    ) -> BackgroundLaunch:
        calls.append(
            {
                "workflow_path": workflow_path,
                "inputs": inputs,
                "skip_gates": skip_gates,
                "metadata": metadata,
            }
        )
        run_id = f"run{len(calls):05d}"
        port = 9000 + len(calls)
        write_run_record(
            RunRecord(
                run_id=run_id,
                pid=os.getpid(),
                workflow_path=str(workflow_path),
                workflow_name=workflow_path.stem,
                started_at="2026-01-01T00:00:00+00:00",
                event_log_path="",
                port=port,
                mode="bg",
                checkpoint_dir=None,
            )
        )
        return BackgroundLaunch(
            url=f"http://127.0.0.1:{port}",
            stderr_log=Path(f"/tmp/conductor-x-{run_id}.bg.stderr.log"),
            stdout_log=Path(f"/tmp/conductor-x-{run_id}.bg.stdout.log"),
            run_id=run_id,
            workflow_started=True,
            still_running=True,
        )

    return _fake


# ---------------------------------------------------------------------------
# E8-T6: in-memory stream pair, initialize + tools/list, DD3 stability
# ---------------------------------------------------------------------------


class TestServerToolsList:
    @pytest.mark.asyncio
    async def test_initialize_succeeds_and_tools_list_returns_expected_names(
        self, tmp_path: Path
    ) -> None:
        catalogue = _two_tool_catalogue(tmp_path)
        server = build_server(catalogue, ServeOptions(toolsets=("workflows",)))

        async with create_connected_server_and_client_session(server) as client:
            # `create_connected_server_and_client_session` already drove
            # `initialize` to completion before yielding -- if it failed, the
            # context manager itself would raise.
            result = await client.list_tools()

        assert sorted(tool.name for tool in result.tools) == ["deploy", "review_pr"]

    @pytest.mark.asyncio
    async def test_tools_list_schemas_match_the_catalogue(self, tmp_path: Path) -> None:
        catalogue = _two_tool_catalogue(tmp_path)
        server = build_server(catalogue, ServeOptions())

        async with create_connected_server_and_client_session(server) as client:
            result = await client.list_tools()

        by_name = {tool.name: tool for tool in result.tools}
        for entry in catalogue.entries:
            assert by_name[entry.tool_name].inputSchema == entry.tool.inputSchema
            assert by_name[entry.tool_name].description == entry.tool.description

    @pytest.mark.asyncio
    async def test_two_sequential_connections_return_an_identical_list(
        self, tmp_path: Path
    ) -> None:
        """DD3: the tool list "MUST NOT vary per-connection"."""
        catalogue = _two_tool_catalogue(tmp_path)
        server = build_server(catalogue, ServeOptions())

        async with create_connected_server_and_client_session(server) as first_client:
            first = await first_client.list_tools()

        async with create_connected_server_and_client_session(server) as second_client:
            second = await second_client.list_tools()

        assert first.tools == second.tools

    @pytest.mark.asyncio
    async def test_repeated_calls_on_one_connection_are_also_identical(
        self, tmp_path: Path
    ) -> None:
        catalogue = _two_tool_catalogue(tmp_path)
        server = build_server(catalogue, ServeOptions())

        async with create_connected_server_and_client_session(server) as client:
            first = await client.list_tools()
            second = await client.list_tools()

        assert first.tools == second.tools

    @pytest.mark.asyncio
    async def test_empty_catalogue_serves_an_empty_tool_list(self) -> None:
        empty = build_catalogue(
            ServeOptions(), registries_config=RegistriesConfig(), allow_network=False
        )
        server = build_server(empty, ServeOptions(toolsets=("workflows",)))

        async with create_connected_server_and_client_session(server) as client:
            result = await client.list_tools()

        assert result.tools == []

    @pytest.mark.asyncio
    async def test_initialize_reports_conductors_own_version(self, tmp_path: Path) -> None:
        """The `initialize` response's `serverInfo.version` must be Conductor's
        own version, not the MCP SDK's (the SDK falls back to the latter when
        no version is passed to `Server(...)`)."""
        catalogue = _two_tool_catalogue(tmp_path)
        server = build_server(catalogue, ServeOptions())

        async with create_connected_server_and_client_session(server) as client:
            result = await client.initialize()

        assert result.serverInfo.version == __version__


# ---------------------------------------------------------------------------
# E8-T5: the FR10 startup summary
# ---------------------------------------------------------------------------


class TestStartupSummary:
    def test_reports_exposed_count_and_mode_on_stderr_only(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        catalogue = _two_tool_catalogue(tmp_path)

        log_startup_summary(catalogue, ServeOptions())

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "2" in captured.err
        assert "direct" in captured.err

    def test_lists_each_tool_with_its_registry_and_pin(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        catalogue = _two_tool_catalogue(tmp_path)

        log_startup_summary(catalogue, ServeOptions())

        err = capsys.readouterr().err
        for entry in catalogue.entries:
            assert entry.tool_name in err
            assert entry.registry in err
            assert entry.pin.as_str() in err

    def test_discovery_mode_is_named_in_the_summary(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        entry = write_path_registry(
            tmp_path,
            name="official",
            workflows={"review-pr": _REVIEW_PR_YAML, "deploy": _DEPLOY_YAML},
        )
        config = RegistriesConfig(registries={"official": entry})
        options = ServeOptions(max_direct_tools=1)
        catalogue = build_catalogue(options, registries_config=config, allow_network=False)
        assert catalogue.mode == "discovery"

        log_startup_summary(catalogue, options)

        err = capsys.readouterr().err
        assert "discovery" in err

    def test_degraded_entry_is_reported_with_its_reason(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        directory = tmp_path / "adhoc"
        directory.mkdir()
        (directory / "broken.yaml").write_text("not: [valid", encoding="utf-8")
        options = ServeOptions(workflow_dirs=(directory,))
        catalogue = build_catalogue(
            options, registries_config=RegistriesConfig(), allow_network=False
        )
        assert catalogue.entries[0].resolution_tier == "degraded"

        with caplog.at_level(logging.WARNING):
            log_startup_summary(catalogue, options)

        err = capsys.readouterr().err
        assert "broken" in err
        assert "degraded" in err
        assert any("degraded" in record.message for record in caplog.records)

    def test_collision_is_reported_naming_both_registries(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        official = write_path_registry(
            tmp_path, name="official", workflows={"review-pr": _REVIEW_PR_YAML}
        )
        team = write_path_registry(tmp_path, name="team", workflows={"review-pr": _REVIEW_PR_YAML})
        config = RegistriesConfig(registries={"official": official, "team": team})
        catalogue = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)
        assert len(catalogue.collisions) == 1

        caplog.clear()
        with caplog.at_level(logging.WARNING):
            log_startup_summary(catalogue, ServeOptions())

        messages = "\n".join(record.message for record in caplog.records)
        assert "official/review-pr" in messages
        assert "team/review-pr" in messages

    def test_same_registry_collision_names_both_workflows(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Two workflows in the SAME registry that slugify to the same base
        name must still be told apart in the FR10 summary -- deduplicating
        by registry alone collapses both identities into one entry."""
        official = write_path_registry(
            tmp_path,
            name="official",
            workflows={"review-pr-a": _REVIEW_PR_YAML, "review-pr-b": _REVIEW_PR_YAML},
        )
        config = RegistriesConfig(registries={"official": official})
        catalogue = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)
        assert len(catalogue.collisions) == 1
        assert len({identity.workflow for identity in catalogue.collisions[0].identities}) == 2

        caplog.clear()
        with caplog.at_level(logging.WARNING):
            log_startup_summary(catalogue, ServeOptions())

        messages = "\n".join(record.message for record in caplog.records)
        assert "official/review-pr-a" in messages
        assert "official/review-pr-b" in messages

    def test_empty_catalogue_reports_zero_exposed(self, capsys: pytest.CaptureFixture[str]) -> None:
        empty = build_catalogue(
            ServeOptions(), registries_config=RegistriesConfig(), allow_network=False
        )

        log_startup_summary(empty, ServeOptions())

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "0" in captured.err


# ---------------------------------------------------------------------------
# E9/E10 (review round 3): a direct-mode generated workflow tool must
# actually be callable, and the default `runs` toolset must be both
# published and dispatched -- not just listed.
# ---------------------------------------------------------------------------


class TestDirectModeWorkflowToolsAreCallable:
    @pytest.mark.asyncio
    async def test_a_direct_mode_workflow_tool_is_dispatched_not_unknown(
        self, tmp_path: Path, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalogue, config = _two_tool_catalogue_with_config(tmp_path)
        assert catalogue.mode == "direct"
        server = build_server(catalogue, ServeOptions())
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "conductor.mcp.serve.invoke.launch_background", _make_fake_launch_background(calls)
        )
        _patch_registries_config(monkeypatch, config)

        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool("deploy", {})

        assert result.isError is not True
        assert len(calls) == 1
        assert result.structuredContent is not None
        assert result.structuredContent["run_id"] == "run00001"
        assert result.structuredContent["status"] == "running"

    @pytest.mark.asyncio
    async def test_an_unknown_direct_mode_tool_name_is_still_refused(self, tmp_path: Path) -> None:
        catalogue, _ = _two_tool_catalogue_with_config(tmp_path)
        server = build_server(catalogue, ServeOptions())

        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool("not_a_real_tool", {})

        assert result.isError is True


class TestRunsToolsetIsPublishedAndDispatched:
    @pytest.mark.asyncio
    async def test_runs_toolset_is_in_the_default_tool_list(self, tmp_path: Path) -> None:
        catalogue = _two_tool_catalogue(tmp_path)
        server = build_server(catalogue, ServeOptions())

        async with create_connected_server_and_client_session(server) as client:
            result = await client.list_tools()

        names = {tool.name for tool in result.tools}
        assert {
            "conductor_run_status",
            "conductor_await_run",
            "conductor_cancel_run",
            "conductor_list_runs",
        } <= names

    @pytest.mark.asyncio
    async def test_runs_toolset_is_absent_when_disabled(self, tmp_path: Path) -> None:
        catalogue = _two_tool_catalogue(tmp_path)
        server = build_server(catalogue, ServeOptions(toolsets=("workflows",)))

        async with create_connected_server_and_client_session(server) as client:
            result = await client.list_tools()

        names = {tool.name for tool in result.tools}
        assert "conductor_run_status" not in names

    @pytest.mark.asyncio
    async def test_conductor_run_status_is_dispatched_for_a_live_run(
        self, tmp_path: Path, conductor_home: Path
    ) -> None:
        catalogue = _two_tool_catalogue(tmp_path)
        server = build_server(catalogue, ServeOptions())
        write_run_record(
            RunRecord(
                run_id="run00001",
                pid=os.getpid(),
                workflow_path="/tmp/deploy.yaml",
                workflow_name="deploy",
                started_at="2026-01-01T00:00:00+00:00",
                event_log_path="",
                port=9001,
                mode="bg",
                checkpoint_dir=None,
            )
        )

        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool("conductor_run_status", {"run_id": "run00001"})

        assert result.isError is not True
        assert result.structuredContent is not None
        assert result.structuredContent["run_id"] == "run00001"
        assert result.structuredContent["source"] == "live"

    @pytest.mark.asyncio
    async def test_conductor_list_runs_is_dispatched(
        self, tmp_path: Path, conductor_home: Path
    ) -> None:
        catalogue = _two_tool_catalogue(tmp_path)
        server = build_server(catalogue, ServeOptions())
        write_run_record(
            RunRecord(
                run_id="run00002",
                pid=os.getpid(),
                workflow_path="/tmp/deploy.yaml",
                workflow_name="deploy",
                started_at="2026-01-01T00:00:00+00:00",
                event_log_path="",
                port=9002,
                mode="bg",
                checkpoint_dir=None,
            )
        )

        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool("conductor_list_runs", {})

        assert result.isError is not True
        assert result.structuredContent is not None
        run_ids = {entry["run_id"] for entry in result.structuredContent["runs"]}
        assert "run00002" in run_ids

    @pytest.mark.asyncio
    async def test_conductor_run_status_unknown_run_id_is_not_an_error(
        self, tmp_path: Path, conductor_home: Path
    ) -> None:
        """An unresolvable run_id is a normal ``not_found`` payload, not a
        protocol-level tool error -- distinct from an unrecognized tool
        *name*, which is refused before dispatch."""
        catalogue = _two_tool_catalogue(tmp_path)
        server = build_server(catalogue, ServeOptions())

        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool("conductor_run_status", {"run_id": "nonexistent"})

        assert result.isError is not True
        assert result.structuredContent["source"] == "not_found"
