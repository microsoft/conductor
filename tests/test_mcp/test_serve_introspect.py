"""Tests for the ``introspect`` toolset: bounded/filtered event query with
R4's tool-payload reduction, per-step detail, and plan tree (FR8, DD3, R4,
E11-T2, E11-T5).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from conductor.fleet.records import RunRecord, TerminalRunRecord, write_run_record
from conductor.mcp.serve.catalogue import build_catalogue
from conductor.mcp.serve.introspect import (
    conductor_node_detail,
    conductor_plan_tree,
    conductor_run_events,
)
from conductor.mcp.serve.invoke import UnknownToolError
from conductor.mcp.serve.options import ALL_TOOLSETS, ServeOptions, is_toolset_enabled
from conductor.mcp.serve.server import build_server
from conductor.registry.config import RegistriesConfig

# ---------------------------------------------------------------------------
# Shared fixtures / builders (mirrors tests/test_mcp/test_serve_runs.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def conductor_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "conductor_home"
    home.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))
    return home


@pytest.fixture
def event_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "tmp"
    d.mkdir()
    (d / "conductor").mkdir()
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(d))
    return d / "conductor"


def _live_record(
    run_id: str, *, pid: int | None = None, event_log_path: str = "", workflow_name: str = "wf"
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        pid=pid if pid is not None else os.getpid(),
        workflow_path=f"/tmp/{workflow_name}.yaml",
        workflow_name=workflow_name,
        started_at="2026-01-01T00:00:00+00:00",
        event_log_path=event_log_path,
        port=9101,
        mode="bg",
        checkpoint_dir=None,
    )


def _terminal_record(
    run_id: str, *, event_log_path: str = "", workflow_name: str = "wf"
) -> TerminalRunRecord:
    return TerminalRunRecord(
        run_id=run_id,
        workflow_path=f"/tmp/{workflow_name}.yaml",
        workflow_name=workflow_name,
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:05:00+00:00",
        status="success",
        output={"result": "ok"},
        error_type=None,
        error_message=None,
        total_tokens=10,
        total_cost_usd=0.01,
        unpriced_agent_count=0,
        event_log_path=event_log_path,
        bg_stderr_log=None,
        bg_stdout_log=None,
    )


def _event(etype: str, data: dict[str, Any] | None = None) -> str:
    return json.dumps({"type": etype, "timestamp": time.time(), "data": data or {}})


def _write_event_log(root: Path, *, name: str, ts: str, run_id: str, lines: list[str]) -> Path:
    path = root / f"conductor-{name}-{ts}-{run_id}.events.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


_SECRET_ARGUMENT = "super-secret-argument-value"
_SECRET_RESULT = "super-secret-result-value"


def _tool_call_events(agent_name: str = "worker") -> list[str]:
    return [
        _event("agent_started", {"agent_name": agent_name}),
        _event("agent_prompt_rendered", {"agent_name": agent_name, "rendered_prompt": "Do it."}),
        _event(
            "agent_tool_start",
            {"agent_name": agent_name, "tool_name": "search", "arguments": _SECRET_ARGUMENT},
        ),
        _event(
            "agent_tool_complete",
            {"agent_name": agent_name, "tool_name": "search", "result": _SECRET_RESULT},
        ),
        _event(
            "agent_completed",
            {"agent_name": agent_name, "output": {"result": "done"}},
        ),
    ]


# ---------------------------------------------------------------------------
# E11-T1: toolset gating
# ---------------------------------------------------------------------------


class TestToolsetGating:
    def test_introspect_and_diagnose_are_off_by_default(self) -> None:
        options = ServeOptions()
        assert is_toolset_enabled(options, "introspect") is False
        assert is_toolset_enabled(options, "diagnose") is False

    def test_introspect_can_be_explicitly_enabled(self) -> None:
        options = ServeOptions(toolsets=("workflows", "runs", "introspect"))
        assert is_toolset_enabled(options, "introspect") is True
        assert is_toolset_enabled(options, "diagnose") is False

    def test_all_toolsets_omits_discovery(self) -> None:
        """`discovery` is decided by the catalogue builder at startup (FR9),
        never operator-selectable -- it must not appear in the recognized
        vocabulary (DD3)."""
        assert "discovery" not in ALL_TOOLSETS

    def test_unknown_toolset_name_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="bogus"):
            ServeOptions(toolsets=("workflows", "bogus"))


# ---------------------------------------------------------------------------
# E11-T1 / E11-T5: the actual protocol boundary -- `is_toolset_enabled` alone
# (TestToolsetGating above) cannot tell whether enabling a toolset makes its
# tools reachable through `tools/list` and `tools/call`; these tests drive a
# real MCP session against `build_server` to prove it.
# ---------------------------------------------------------------------------


def _empty_catalogue():
    return build_catalogue(
        ServeOptions(), registries_config=RegistriesConfig(), allow_network=False
    )


class TestIntrospectProtocolDispatch:
    @pytest.mark.asyncio
    async def test_introspect_tools_absent_from_tools_list_by_default(self) -> None:
        server = build_server(_empty_catalogue(), ServeOptions())

        async with create_connected_server_and_client_session(server) as client:
            result = await client.list_tools()

        names = {tool.name for tool in result.tools}
        assert "conductor_run_events" not in names
        assert "conductor_node_detail" not in names
        assert "conductor_plan_tree" not in names

    @pytest.mark.asyncio
    async def test_introspect_tools_listed_and_callable_when_enabled(
        self, conductor_home: Path, tmp_path: Path
    ) -> None:
        log = _write_event_log(
            tmp_path,
            name="wf",
            ts="20260101-120000",
            run_id="proto0001",
            lines=_tool_call_events(),
        )
        write_run_record(_live_record("proto0001", event_log_path=str(log)))
        options = ServeOptions(toolsets=("workflows", "runs", "introspect"))
        server = build_server(_empty_catalogue(), options)

        async with create_connected_server_and_client_session(server) as client:
            listed = await client.list_tools()
            assert "conductor_run_events" in {tool.name for tool in listed.tools}

            result = await client.call_tool("conductor_run_events", {"run_id": "proto0001"})

        assert result.isError is not True
        assert result.structuredContent is not None
        assert result.structuredContent["run_id"] == "proto0001"
        # R4's default redaction must survive the real protocol round trip,
        # not just a direct function call.
        assert _SECRET_ARGUMENT not in json.dumps(result.structuredContent)

    @pytest.mark.asyncio
    async def test_introspect_full_propagates_through_call_tool(
        self, conductor_home: Path, tmp_path: Path
    ) -> None:
        log = _write_event_log(
            tmp_path,
            name="wf",
            ts="20260101-120000",
            run_id="proto0002",
            lines=_tool_call_events(),
        )
        write_run_record(_live_record("proto0002", event_log_path=str(log)))
        options = ServeOptions(toolsets=("workflows", "runs", "introspect"), introspect_full=True)
        server = build_server(_empty_catalogue(), options)

        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool("conductor_run_events", {"run_id": "proto0002"})

        assert _SECRET_ARGUMENT in json.dumps(result.structuredContent)

    @pytest.mark.asyncio
    async def test_introspect_tool_call_refused_when_toolset_disabled(self) -> None:
        """A disabled toolset's tool must be refused even if a caller
        invokes it directly, without going through ``tools/list`` first
        (DD3: the gate must hold regardless of what a client cached)."""
        server = build_server(_empty_catalogue(), ServeOptions())

        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool("conductor_run_events", {"run_id": "anything"})

        assert result.isError is True


class TestDiagnoseProtocolDispatch:
    @pytest.mark.asyncio
    async def test_diagnose_tools_absent_from_tools_list_by_default(self) -> None:
        server = build_server(_empty_catalogue(), ServeOptions())

        async with create_connected_server_and_client_session(server) as client:
            result = await client.list_tools()

        names = {tool.name for tool in result.tools}
        assert "conductor_doctor" not in names
        assert "conductor_validate_workflow" not in names
        assert "conductor_run_logs" not in names

    @pytest.mark.asyncio
    async def test_conductor_doctor_listed_and_callable_when_enabled(self) -> None:
        options = ServeOptions(toolsets=("workflows", "runs", "diagnose"))
        server = build_server(_empty_catalogue(), options)

        async with create_connected_server_and_client_session(server) as client:
            listed = await client.list_tools()
            assert "conductor_doctor" in {tool.name for tool in listed.tools}

            result = await client.call_tool("conductor_doctor", {})

        assert result.isError is not True
        assert result.structuredContent is not None

    @pytest.mark.asyncio
    async def test_diagnose_tool_call_refused_when_toolset_disabled(self) -> None:
        server = build_server(_empty_catalogue(), ServeOptions())

        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool("conductor_doctor", {})

        assert result.isError is True


# ---------------------------------------------------------------------------
# E11-T2 / E11-T5: conductor_run_events
# ---------------------------------------------------------------------------


class TestConductorRunEvents:
    def test_bounded_by_limit(self, conductor_home: Path, tmp_path: Path) -> None:
        log = _write_event_log(
            tmp_path,
            name="wf",
            ts="20260101-120000",
            run_id="ev00001",
            lines=[_event("agent_started", {"agent_name": f"a{i}"}) for i in range(10)],
        )
        write_run_record(_live_record("ev00001", event_log_path=str(log)))

        result = conductor_run_events("ev00001", limit=3)

        assert result["total"] == 10
        assert result["returned"] == 3
        assert len(result["events"]) == 3
        assert result["truncated"] is True

    def test_filtered_by_event_type(self, conductor_home: Path, tmp_path: Path) -> None:
        log = _write_event_log(
            tmp_path,
            name="wf",
            ts="20260101-120000",
            run_id="ev00002",
            lines=[
                _event("agent_started", {"agent_name": "a"}),
                _event("agent_message", {"agent_name": "a", "content": "hi"}),
                _event("agent_completed", {"agent_name": "a", "output": {}}),
            ],
        )
        write_run_record(_live_record("ev00002", event_log_path=str(log)))

        result = conductor_run_events("ev00002", event_types=("agent_message",))

        assert result["total"] == 1
        assert [e["type"] for e in result["events"]] == ["agent_message"]

    def test_unknown_run_id_reports_no_event_log(
        self, conductor_home: Path, event_log_dir: Path
    ) -> None:
        result = conductor_run_events("ghost0001")

        assert result["events"] == []
        assert result["source"] == "not_found"
        assert "error" in result

    def test_tool_payload_redacted_by_default_with_byte_size_reported(
        self, conductor_home: Path, tmp_path: Path
    ) -> None:
        log = _write_event_log(
            tmp_path,
            name="wf",
            ts="20260101-120000",
            run_id="ev00003",
            lines=_tool_call_events(),
        )
        write_run_record(_live_record("ev00003", event_log_path=str(log)))

        result = conductor_run_events("ev00003")
        serialized = json.dumps(result)

        assert _SECRET_ARGUMENT not in serialized
        assert _SECRET_RESULT not in serialized

        by_type = {e["type"]: e for e in result["events"]}
        start_data = by_type["agent_tool_start"]["data"]
        complete_data = by_type["agent_tool_complete"]["data"]

        assert start_data["arguments"] == {
            "name": "search",
            "status": "redacted",
            "byte_size": len(json.dumps(_SECRET_ARGUMENT).encode("utf-8")),
        }
        assert start_data["arguments_byte_size"] == len(
            json.dumps(_SECRET_ARGUMENT).encode("utf-8")
        )
        assert complete_data["result"] == {
            "name": "search",
            "status": "redacted",
            "byte_size": len(json.dumps(_SECRET_RESULT).encode("utf-8")),
        }
        assert complete_data["result_byte_size"] == len(json.dumps(_SECRET_RESULT).encode("utf-8"))

    def test_tool_payload_restored_under_introspect_full_with_byte_size_still_reported(
        self, conductor_home: Path, tmp_path: Path
    ) -> None:
        log = _write_event_log(
            tmp_path,
            name="wf",
            ts="20260101-120000",
            run_id="ev00004",
            lines=_tool_call_events(),
        )
        write_run_record(_live_record("ev00004", event_log_path=str(log)))

        result = conductor_run_events("ev00004", introspect_full=True)
        serialized = json.dumps(result)

        assert _SECRET_ARGUMENT in serialized
        assert _SECRET_RESULT in serialized

        by_type = {e["type"]: e for e in result["events"]}
        start_data = by_type["agent_tool_start"]["data"]
        complete_data = by_type["agent_tool_complete"]["data"]

        assert start_data["arguments"] == _SECRET_ARGUMENT
        assert start_data["arguments_byte_size"] == len(
            json.dumps(_SECRET_ARGUMENT).encode("utf-8")
        )
        assert complete_data["result"] == _SECRET_RESULT
        assert complete_data["result_byte_size"] == len(json.dumps(_SECRET_RESULT).encode("utf-8"))

    def test_non_tool_events_are_untouched(self, conductor_home: Path, tmp_path: Path) -> None:
        log = _write_event_log(
            tmp_path,
            name="wf",
            ts="20260101-120000",
            run_id="ev00005",
            lines=[_event("agent_message", {"agent_name": "a", "content": "hello"})],
        )
        write_run_record(_live_record("ev00005", event_log_path=str(log)))

        result = conductor_run_events("ev00005")

        assert result["events"][0]["data"]["content"] == "hello"

    def test_resolves_a_crashed_run_via_the_event_log_fallback(
        self, conductor_home: Path, event_log_dir: Path
    ) -> None:
        _write_event_log(
            event_log_dir,
            name="wf",
            ts="20260101-120000",
            run_id="crash001",
            lines=[_event("workflow_started", {"name": "wf"})],
        )

        result = conductor_run_events("crash001")

        assert result["source"] == "event_log"
        assert result["total"] == 1

    def test_byte_size_matches_the_event_logs_own_json_encoding(
        self, conductor_home: Path, tmp_path: Path
    ) -> None:
        """E11-T2: ``byte_size`` must report the same number of bytes
        ``engine/event_log.py`` actually wrote for the field -- a
        structured or non-ASCII payload previously reported a different
        size under different ``json.dumps`` settings."""
        structured_argument = {"query": "café", "nested": {"a": [1, 2, 3]}}
        log = _write_event_log(
            tmp_path,
            name="wf",
            ts="20260101-120000",
            run_id="ev00006",
            lines=[
                _event(
                    "agent_tool_start",
                    {
                        "agent_name": "worker",
                        "tool_name": "search",
                        "arguments": structured_argument,
                    },
                ),
            ],
        )
        write_run_record(_live_record("ev00006", event_log_path=str(log)))

        result = conductor_run_events("ev00006")

        expected = len(json.dumps(structured_argument, separators=(",", ":")).encode("utf-8"))
        start_data = result["events"][0]["data"]
        assert start_data["arguments_byte_size"] == expected
        assert start_data["arguments"]["byte_size"] == expected

    def test_a_single_oversized_event_is_withheld_regardless_of_limit(
        self, conductor_home: Path, tmp_path: Path
    ) -> None:
        """NFR6: limiting event *count* does not bound size when a single
        event can itself be arbitrarily large (e.g. a huge
        ``agent_message``)."""
        from conductor.mcp.serve.introspect import MAX_FIELD_BYTES

        huge_content = "x" * (MAX_FIELD_BYTES + 1)
        log = _write_event_log(
            tmp_path,
            name="wf",
            ts="20260101-120000",
            run_id="ev00007",
            lines=[_event("agent_message", {"agent_name": "a", "content": huge_content})],
        )
        write_run_record(_live_record("ev00007", event_log_path=str(log)))

        result = conductor_run_events("ev00007")

        evt = result["events"][0]
        assert evt["truncated"] is True
        assert evt["type"] == "agent_message"
        assert evt["byte_size"] > MAX_FIELD_BYTES
        assert huge_content not in json.dumps(result)


# ---------------------------------------------------------------------------
# E11-T2 / E11-T5: conductor_node_detail
# ---------------------------------------------------------------------------


class TestConductorNodeDetail:
    def test_returns_prompt_and_output_in_full(self, conductor_home: Path, tmp_path: Path) -> None:
        log = _write_event_log(
            tmp_path,
            name="wf",
            ts="20260101-120000",
            run_id="nd00001",
            lines=_tool_call_events("worker"),
        )
        write_run_record(_live_record("nd00001", event_log_path=str(log)))

        detail = conductor_node_detail("nd00001", "worker")

        assert detail["prompt"] == "Do it."
        assert detail["output"] == {"result": "done"}
        assert detail["status"] == "completed"

    def test_activity_lines_never_carry_a_tool_payload_either_way(
        self, conductor_home: Path, tmp_path: Path
    ) -> None:
        """R4, asserted rather than assumed: `derive_step_detail` already
        discards `arguments`/`result` when it builds its activity stream,
        so a future change there that starts leaking them must fail this
        test."""
        log = _write_event_log(
            tmp_path,
            name="wf",
            ts="20260101-120000",
            run_id="nd00002",
            lines=_tool_call_events("worker"),
        )
        write_run_record(_live_record("nd00002", event_log_path=str(log)))

        detail = conductor_node_detail("nd00002", "worker")
        serialized = json.dumps(detail["activity"])

        assert _SECRET_ARGUMENT not in serialized
        assert _SECRET_RESULT not in serialized
        kinds = [line["kind"] for line in detail["activity"]]
        assert "tool" in kinds
        assert "tool_result" in kinds

    def test_works_for_a_terminal_run(self, conductor_home: Path, tmp_path: Path) -> None:
        from conductor.fleet.records import write_terminal_record

        log = _write_event_log(
            tmp_path,
            name="wf",
            ts="20260101-120000",
            run_id="nd00003",
            lines=_tool_call_events("worker"),
        )
        write_terminal_record(_terminal_record("nd00003", event_log_path=str(log)))

        detail = conductor_node_detail("nd00003", "worker")

        assert detail["output"] == {"result": "done"}

    def test_unknown_run_id_reports_an_error(
        self, conductor_home: Path, event_log_dir: Path
    ) -> None:
        detail = conductor_node_detail("ghost0002", "worker")

        assert detail["status"] == "unknown"
        assert "error" in detail

    def test_an_oversized_prompt_is_withheld_regardless_of_output_size(
        self, conductor_home: Path, tmp_path: Path
    ) -> None:
        """NFR6: no event-count limit applies to `conductor_node_detail` at
        all, so a huge prompt must be bounded directly."""
        from conductor.mcp.serve.introspect import MAX_FIELD_BYTES

        huge_prompt = "x" * (MAX_FIELD_BYTES + 1)
        log = _write_event_log(
            tmp_path,
            name="wf",
            ts="20260101-120000",
            run_id="nd00004",
            lines=[
                _event("agent_started", {"agent_name": "worker"}),
                _event(
                    "agent_prompt_rendered",
                    {"agent_name": "worker", "rendered_prompt": huge_prompt},
                ),
                _event("agent_completed", {"agent_name": "worker", "output": {"result": "ok"}}),
            ],
        )
        write_run_record(_live_record("nd00004", event_log_path=str(log)))

        detail = conductor_node_detail("nd00004", "worker")

        assert detail["prompt"]["truncated"] is True
        assert detail["prompt"]["byte_size"] > MAX_FIELD_BYTES
        assert huge_prompt not in json.dumps(detail)
        assert detail["output"] == {"result": "ok"}


# ---------------------------------------------------------------------------
# E11-T2 / E11-T5: conductor_plan_tree
# ---------------------------------------------------------------------------

_PLAN_YAML = """\
workflow:
  name: plan-demo
  description: A small workflow to exercise plan tree extraction.
  entry_point: first
agents:
  - name: first
    prompt: "Step one"
    output:
      result:
        type: string
    routes:
      - to: second
        when: "first.output.result == 'go'"
      - to: $end
  - name: second
    prompt: "Step two {{ item }}"
    output:
      result:
        type: string
parallel:
  - name: fan_out
    agents:
      - first
      - second
    routes:
      - to: $end
for_each:
  - name: analyzers
    type: for_each
    source: first.output.result
    as: item
    agent:
      name: analyzer
      prompt: "Analyze {{ item }}"
      output:
        ok:
          type: boolean
    routes:
      - to: $end
output:
  result: "{{ second.output.result }}"
"""


class TestConductorPlanTree:
    def _catalogue(self, tmp_path: Path):
        directory = tmp_path / "wfdir"
        directory.mkdir()
        (directory / "plan-demo.yaml").write_text(_PLAN_YAML, encoding="utf-8")
        options = ServeOptions(workflow_dirs=(directory,))
        catalogue = build_catalogue(
            options, registries_config=RegistriesConfig(), allow_network=False
        )
        return catalogue, options

    def test_plan_tree_matches_the_yaml(self, tmp_path: Path) -> None:
        catalogue, options = self._catalogue(tmp_path)
        tool_name = catalogue.entries[0].tool_name

        tree = conductor_plan_tree(tool_name, catalogue=catalogue, options=options)

        assert tree["workflow_name"] == "plan-demo"
        assert tree["entry_point"] == "first"
        by_name = {node["name"]: node for node in tree["nodes"]}
        assert by_name["first"]["type"] == "agent"
        assert by_name["first"]["routes"] == [
            {"to": "second", "when": "first.output.result == 'go'"},
            {"to": "$end", "when": None},
        ]
        assert by_name["fan_out"]["type"] == "parallel"
        assert by_name["fan_out"]["agents"] == ["first", "second"]
        assert by_name["analyzers"]["type"] == "for_each"
        assert by_name["analyzers"]["agent"] == "analyzer"

    def test_unknown_tool_name_is_refused(self, tmp_path: Path) -> None:
        catalogue, options = self._catalogue(tmp_path)

        with pytest.raises(UnknownToolError):
            conductor_plan_tree("not-a-real-tool", catalogue=catalogue, options=options)

    def test_path_shaped_name_is_refused(self, tmp_path: Path) -> None:
        """NFR3: no tool accepts a filesystem path -- a path-shaped string
        is never a catalogue key, so it is refused like any other unknown
        name."""
        catalogue, options = self._catalogue(tmp_path)

        with pytest.raises(UnknownToolError):
            conductor_plan_tree(
                str(tmp_path / "wfdir" / "plan-demo.yaml"), catalogue=catalogue, options=options
            )


class TestPlanTreeCatalogueIdentityDisambiguation:
    def test_same_basename_workflow_dirs_resolve_to_their_own_distinct_file(
        self, tmp_path: Path
    ) -> None:
        """``catalogue.reverse``'s ``(registry, workflow)`` pair alone is
        shared by two ``--workflow-dir`` directories with the same
        basename and a same-named file in each (see
        ``test_serve_catalogue.py::TestDuplicateSourceIdentity``); this
        must not make ``conductor_plan_tree`` inspect the wrong workflow
        for either of the two published tool names."""

        def _yaml(name: str, agent_name: str) -> str:
            return (
                "workflow:\n"
                f"  name: {name}\n"
                f"  entry_point: {agent_name}\n"
                "agents:\n"
                f"  - name: {agent_name}\n"
                '    prompt: "Do it."\n'
                "    output:\n"
                "      result:\n"
                "        type: string\n"
                "output:\n"
                f'  result: "{{{{ {agent_name}.output.result }}}}"\n'
            )

        root_a = tmp_path / "a"
        root_b = tmp_path / "b"
        adhoc_a = root_a / "adhoc"
        adhoc_b = root_b / "adhoc"
        adhoc_a.mkdir(parents=True)
        adhoc_b.mkdir(parents=True)
        (adhoc_a / "review-pr.yaml").write_text(_yaml("review-pr-a", "worker_a"), encoding="utf-8")
        (adhoc_b / "review-pr.yaml").write_text(_yaml("review-pr-b", "worker_b"), encoding="utf-8")

        options = ServeOptions(workflow_dirs=(adhoc_a, adhoc_b))
        catalogue = build_catalogue(
            options, registries_config=RegistriesConfig(), allow_network=False
        )
        assert len(catalogue.entries) == 2

        expected_agent_by_source = {
            str(adhoc_a / "review-pr.yaml"): "worker_a",
            str(adhoc_b / "review-pr.yaml"): "worker_b",
        }
        for entry in catalogue.entries:
            tree = conductor_plan_tree(entry.tool_name, catalogue=catalogue, options=options)
            expected_agent = expected_agent_by_source[entry.source]
            assert tree["nodes"][0]["name"] == expected_agent
