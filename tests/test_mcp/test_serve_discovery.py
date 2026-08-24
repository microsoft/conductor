"""Tests for the discovery fallback: the ``conductor_find_workflow`` /
``conductor_run_workflow`` pair, and the server's direct-vs-discovery mode
gating (FR9, DD3, G7, E12-T3).

Above ``--max-direct-tools`` the per-workflow tools must be absent and the
discovery pair present; below it, the reverse; the mode is decided once at
catalogue-build time and never varies within (or across) a connection
(DD3); ``conductor_run_workflow`` refuses a path-shaped ``name`` the same
way any other unrecognized tool name is refused (NFR3); and the startup log
names the exposed count and the configured threshold (FR10).
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from conductor.cli.bg_runner import BackgroundLaunch
from conductor.fleet.records import RunRecord, write_run_record
from conductor.mcp.serve.catalogue import build_catalogue
from conductor.mcp.serve.discovery import conductor_find_workflow, conductor_run_workflow
from conductor.mcp.serve.invoke import LaunchTracker, UnknownToolError
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


def _simple_yaml(name: str) -> str:
    return textwrap.dedent(
        f"""\
        workflow:
          name: {name}
          description: A simple workflow named {name}.
          entry_point: worker
        agents:
          - name: worker
            prompt: "Do the thing."
            output:
              result:
                type: string
        output:
          result: "{{{{ worker.output.result }}}}"
        """
    )


def _two_tool_catalogue(tmp_path: Path) -> tuple[Any, RegistriesConfig]:
    entry = write_path_registry(
        tmp_path,
        name="official",
        workflows={"review-pr": _REVIEW_PR_YAML, "deploy": _DEPLOY_YAML},
    )
    config = RegistriesConfig(registries={"official": entry})
    catalogue = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)
    return catalogue, config


def _over_cap_catalogue(
    tmp_path: Path, *, count: int = 5, max_direct_tools: int = 3
) -> tuple[Any, ServeOptions, RegistriesConfig]:
    workflows = {f"workflow-{i}": _simple_yaml(f"workflow-{i}") for i in range(count)}
    entry = write_path_registry(tmp_path, name="official", workflows=workflows)
    config = RegistriesConfig(registries={"official": entry})
    options = ServeOptions(max_direct_tools=max_direct_tools)
    catalogue = build_catalogue(options, registries_config=config, allow_network=False)
    assert catalogue.mode == "discovery"
    return catalogue, options, config


def _patch_registries_config(monkeypatch: pytest.MonkeyPatch, config: RegistriesConfig) -> None:
    """Point ``invoke.py``'s default ``load_registries_config()`` fallback at
    a test's in-memory :class:`RegistriesConfig` instead of the real
    ``~/.conductor/registries.toml`` -- needed by every path that reaches
    ``invoke_workflow_tool`` without itself threading ``registries_config``
    through (the server's own ``call_tool`` dispatch, exactly as a real
    ``conductor mcp serve`` invocation would)."""
    monkeypatch.setattr("conductor.mcp.serve.invoke.load_registries_config", lambda: config)


def _make_fake_launch_background(calls: list[dict[str, Any]]) -> Any:
    """Mirrors ``test_serve_invoke.py``'s own stand-in: records the kwargs
    ``launch_background`` was called with and writes a real, discoverable
    :class:`RunRecord`, matching D2's guarantee that a successful launch is
    already discoverable before it returns."""

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
# E12-T1: conductor_find_workflow
# ---------------------------------------------------------------------------


class TestConductorFindWorkflow:
    def test_empty_query_lists_every_published_workflow(self, tmp_path: Path) -> None:
        catalogue, _ = _two_tool_catalogue(tmp_path)

        result = conductor_find_workflow("", catalogue=catalogue)

        assert result["count"] == 2
        assert sorted(w["name"] for w in result["workflows"]) == ["deploy", "review_pr"]

    def test_omitted_query_defaults_to_listing_everything(self, tmp_path: Path) -> None:
        catalogue, _ = _two_tool_catalogue(tmp_path)

        result = conductor_find_workflow(catalogue=catalogue)

        assert result["count"] == 2

    def test_query_filters_by_name_substring(self, tmp_path: Path) -> None:
        catalogue, _ = _two_tool_catalogue(tmp_path)

        result = conductor_find_workflow("review", catalogue=catalogue)

        assert result["count"] == 1
        assert result["workflows"][0]["name"] == "review_pr"

    def test_query_is_case_insensitive(self, tmp_path: Path) -> None:
        catalogue, _ = _two_tool_catalogue(tmp_path)

        result = conductor_find_workflow("REVIEW", catalogue=catalogue)

        assert result["count"] == 1

    def test_query_also_matches_the_description(self, tmp_path: Path) -> None:
        catalogue, _ = _two_tool_catalogue(tmp_path)

        result = conductor_find_workflow("production", catalogue=catalogue)

        assert result["count"] == 1
        assert result["workflows"][0]["name"] == "deploy"

    def test_no_match_returns_an_empty_list(self, tmp_path: Path) -> None:
        catalogue, _ = _two_tool_catalogue(tmp_path)

        result = conductor_find_workflow("no-such-workflow", catalogue=catalogue)

        assert result == {"query": "no-such-workflow", "count": 0, "workflows": []}

    def test_result_carries_description_and_input_schema(self, tmp_path: Path) -> None:
        catalogue, _ = _two_tool_catalogue(tmp_path)
        entry = next(e for e in catalogue.entries if e.tool_name == "review_pr")

        result = conductor_find_workflow("review", catalogue=catalogue)

        found = result["workflows"][0]
        assert found["description"] == entry.tool.description
        assert found["input_schema"] == entry.tool.inputSchema
        assert found["registry"] == entry.registry
        assert found["workflow"] == entry.workflow

    def test_empty_catalogue_returns_no_matches(self) -> None:
        empty = build_catalogue(
            ServeOptions(), registries_config=RegistriesConfig(), allow_network=False
        )

        result = conductor_find_workflow("anything", catalogue=empty)

        assert result["count"] == 0
        assert result["workflows"] == []


# ---------------------------------------------------------------------------
# E12-T1: conductor_run_workflow
# ---------------------------------------------------------------------------


class TestConductorRunWorkflow:
    async def test_dispatches_through_the_same_invocation_layer_as_a_generated_tool(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalogue, registries_config = _two_tool_catalogue(conductor_home)
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "conductor.mcp.serve.invoke.launch_background", _make_fake_launch_background(calls)
        )

        content, structured = await conductor_run_workflow(
            "review_pr",
            {"pr_number": 7},
            catalogue=catalogue,
            options=ServeOptions(),
            tracker=LaunchTracker(),
            registries_config=registries_config,
        )

        assert content and structured
        assert len(calls) == 1
        # DD11: never skip a human gate on the caller's behalf, exactly
        # like a generated tool's own `invoke_workflow_tool` call.
        assert calls[0]["skip_gates"] is False
        assert calls[0]["inputs"] == {"pr_number": 7}
        assert structured["run_id"] == "run00001"

    async def test_wait_seconds_is_forwarded_as_the_reserved_parameter(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_wait_seconds` must reach `invoke_workflow_tool` as the reserved
        parameter, not as a workflow input -- `launch_background`'s own
        `inputs` kwarg must never contain it."""
        catalogue, registries_config = _two_tool_catalogue(conductor_home)
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "conductor.mcp.serve.invoke.launch_background", _make_fake_launch_background(calls)
        )

        await conductor_run_workflow(
            "review_pr",
            {"pr_number": 7},
            0,
            catalogue=catalogue,
            options=ServeOptions(),
            tracker=LaunchTracker(),
            registries_config=registries_config,
        )

        assert calls[0]["inputs"] == {"pr_number": 7}
        assert "_wait_seconds" not in calls[0]["inputs"]

    async def test_refuses_an_unknown_tool_name(self, tmp_path: Path) -> None:
        catalogue, registries_config = _two_tool_catalogue(tmp_path)

        with pytest.raises(UnknownToolError):
            await conductor_run_workflow(
                "not-a-tool",
                {},
                catalogue=catalogue,
                options=ServeOptions(),
                tracker=LaunchTracker(),
                registries_config=registries_config,
            )

    @pytest.mark.parametrize(
        "path_shaped_name",
        [
            "/etc/passwd",
            "../../etc/passwd",
            "https://example.com/workflow.yaml",
            "official/review-pr",
        ],
    )
    async def test_refuses_a_path_shaped_name(self, tmp_path: Path, path_shaped_name: str) -> None:
        """NFR3: `name` is a catalogue key, never a path, URL, or registry
        source -- none of those shapes is ever a key in `catalogue.reverse`,
        so each is refused exactly like any other unrecognized name."""
        catalogue, registries_config = _two_tool_catalogue(tmp_path)

        with pytest.raises(UnknownToolError):
            await conductor_run_workflow(
                path_shaped_name,
                {},
                catalogue=catalogue,
                options=ServeOptions(),
                tracker=LaunchTracker(),
                registries_config=registries_config,
            )

    async def test_refuses_an_actual_workflow_file_path(self, tmp_path: Path) -> None:
        """Even the *real*, on-disk path of a published workflow's file --
        not just an arbitrary path-shaped string -- must be refused; only
        the catalogue tool name itself is accepted."""
        registry_dir = tmp_path / "official"
        catalogue, registries_config = _two_tool_catalogue(tmp_path)
        real_file = registry_dir / "workflows" / "review-pr.yaml"
        assert real_file.is_file()

        with pytest.raises(UnknownToolError):
            await conductor_run_workflow(
                str(real_file),
                {},
                catalogue=catalogue,
                options=ServeOptions(),
                tracker=LaunchTracker(),
                registries_config=registries_config,
            )


# ---------------------------------------------------------------------------
# E12-T2/T3: mode gates `tools/list` and `tools/call`
# ---------------------------------------------------------------------------


class TestModeGatesTheToolList:
    @pytest.mark.asyncio
    async def test_above_the_cap_serves_the_discovery_pair_and_not_workflow_tools(
        self, tmp_path: Path
    ) -> None:
        catalogue, options, _ = _over_cap_catalogue(tmp_path)
        server = build_server(catalogue, options)

        async with create_connected_server_and_client_session(server) as client:
            result = await client.list_tools()

        names = sorted(tool.name for tool in result.tools)
        assert names == ["conductor_find_workflow", "conductor_run_workflow"]
        for entry in catalogue.entries:
            assert entry.tool_name not in names

    @pytest.mark.asyncio
    async def test_below_the_cap_serves_workflow_tools_and_not_the_discovery_pair(
        self, tmp_path: Path
    ) -> None:
        catalogue, _ = _two_tool_catalogue(tmp_path)
        assert catalogue.mode == "direct"
        server = build_server(catalogue, ServeOptions())

        async with create_connected_server_and_client_session(server) as client:
            result = await client.list_tools()

        names = sorted(tool.name for tool in result.tools)
        assert names == ["deploy", "review_pr"]
        assert "conductor_find_workflow" not in names
        assert "conductor_run_workflow" not in names

    @pytest.mark.asyncio
    async def test_mode_does_not_change_across_repeated_calls_or_connections(
        self, tmp_path: Path
    ) -> None:
        """DD3: the tool list -- including which mode produced it -- "MUST
        NOT vary per-connection or as a side effect of other requests"."""
        catalogue, options, _ = _over_cap_catalogue(tmp_path)
        server = build_server(catalogue, options)

        async with create_connected_server_and_client_session(server) as client:
            first = await client.list_tools()
            second = await client.list_tools()

        async with create_connected_server_and_client_session(server) as another_client:
            third = await another_client.list_tools()

        assert first.tools == second.tools == third.tools

    @pytest.mark.asyncio
    async def test_a_direct_mode_server_never_produces_the_discovery_pair(
        self, tmp_path: Path
    ) -> None:
        """Building a fresh server from a direct-mode catalogue must never
        yield the discovery pair, regardless of how many times it is
        queried -- there is no runtime switch to flip (DD3)."""
        catalogue, _ = _two_tool_catalogue(tmp_path)
        server = build_server(catalogue, ServeOptions())

        async with create_connected_server_and_client_session(server) as client:
            for _ in range(3):
                result = await client.list_tools()
                names = {tool.name for tool in result.tools}
                assert "conductor_find_workflow" not in names
                assert "conductor_run_workflow" not in names


class TestDiscoveryToolsAreCallableOnlyInDiscoveryMode:
    @pytest.mark.asyncio
    async def test_conductor_find_workflow_is_callable_in_discovery_mode(
        self, tmp_path: Path
    ) -> None:
        catalogue, options, _ = _over_cap_catalogue(tmp_path)
        server = build_server(catalogue, options)

        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool("conductor_find_workflow", {"query": ""})

        assert result.structuredContent is not None
        assert result.structuredContent["count"] == len(catalogue.entries)

    @pytest.mark.asyncio
    async def test_conductor_run_workflow_is_callable_in_discovery_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalogue, options, registries_config = _over_cap_catalogue(tmp_path)
        server = build_server(catalogue, options)
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "conductor.mcp.serve.invoke.launch_background", _make_fake_launch_background(calls)
        )
        _patch_registries_config(monkeypatch, registries_config)
        tool_name = catalogue.entries[0].tool_name

        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool("conductor_run_workflow", {"name": tool_name})

        assert result.isError is not True
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_a_per_workflow_tool_name_is_not_callable_in_discovery_mode(
        self, tmp_path: Path
    ) -> None:
        """The per-workflow tools are not merely unlisted above the cap --
        they are unreachable through `tools/call` too."""
        catalogue, options, _ = _over_cap_catalogue(tmp_path)
        server = build_server(catalogue, options)
        tool_name = catalogue.entries[0].tool_name

        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool(tool_name, {})

        assert result.isError is True


# ---------------------------------------------------------------------------
# E12-T3: the startup log names the count and threshold
# ---------------------------------------------------------------------------


class TestStartupLogNamesCountAndThreshold:
    def test_discovery_mode_names_the_exposed_count_and_the_configured_threshold(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        catalogue, options, _ = _over_cap_catalogue(tmp_path, count=5, max_direct_tools=3)

        log_startup_summary(catalogue, options)

        err = capsys.readouterr().err
        assert "5" in err
        assert "--max-direct-tools=3" in err
        assert "discovery" in err

    def test_direct_mode_names_the_exposed_count_and_the_configured_threshold(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        catalogue, _ = _two_tool_catalogue(tmp_path)
        options = ServeOptions()

        log_startup_summary(catalogue, options)

        err = capsys.readouterr().err
        assert "2" in err
        assert f"--max-direct-tools={options.max_direct_tools}" in err
        assert "direct" in err
