"""Tests for the ``diagnose`` toolset: ``conductor_run_logs`` (links, never
bytes; ``exists: false`` for a pruned path; error type/message from the
terminal record), ``conductor_doctor``, and ``conductor_validate_workflow``
(FR8, DD12, NFR6, E11-T3, E11-T4, E11-T6).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from mcp.types import ResourceLink

from conductor.fleet.records import RunRecord, TerminalRunRecord, write_run_record
from conductor.mcp.serve.catalogue import build_catalogue
from conductor.mcp.serve.diagnose import (
    conductor_doctor,
    conductor_run_logs,
    conductor_validate_workflow,
)
from conductor.mcp.serve.invoke import UnknownToolError
from conductor.mcp.serve.options import ServeOptions
from conductor.registry.config import RegistriesConfig

# ---------------------------------------------------------------------------
# Shared fixtures / builders
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
    run_id: str,
    *,
    event_log_path: str = "",
    bg_stderr_log: str | None = None,
    bg_stdout_log: str | None = None,
    status: str = "success",
    error_type: str | None = None,
    error_message: str | None = None,
    workflow_name: str = "wf",
) -> TerminalRunRecord:
    return TerminalRunRecord(
        run_id=run_id,
        workflow_path=f"/tmp/{workflow_name}.yaml",
        workflow_name=workflow_name,
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:05:00+00:00",
        status=status,
        output={"result": "ok"} if status == "success" else {},
        error_type=error_type,
        error_message=error_message,
        total_tokens=10,
        total_cost_usd=0.01,
        unpriced_agent_count=0,
        event_log_path=event_log_path,
        bg_stderr_log=bg_stderr_log,
        bg_stdout_log=bg_stdout_log,
    )


def _event(etype: str, data: dict[str, Any] | None = None) -> str:
    return json.dumps({"type": etype, "timestamp": time.time(), "data": data or {}})


def _write_log(path: Path, contents: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    return path


_SECRET_LOG_LINE = "Traceback: super-secret-stack-trace-detail"


# ---------------------------------------------------------------------------
# E11-T4 / E11-T6: conductor_run_logs
# ---------------------------------------------------------------------------


class TestConductorRunLogs:
    def test_returns_resource_links_and_never_log_bytes(
        self, conductor_home: Path, tmp_path: Path
    ) -> None:
        events_log = _write_log(
            tmp_path / "conductor-wf-20260101-120000-runlog01.events.jsonl",
            _event("workflow_started", {"name": "wf"}) + "\n",
        )
        stderr_log = _write_log(
            tmp_path / "conductor-wf-20260101-120000-runlog01.bg.stderr.log",
            _SECRET_LOG_LINE + "\n",
        )
        stdout_log = _write_log(
            tmp_path / "conductor-wf-20260101-120000-runlog01.bg.stdout.log",
            "some stdout output\n",
        )
        write_run_record(_live_record("runlog01", event_log_path=str(events_log)))

        links, structured = conductor_run_logs("runlog01")

        # Never bytes -- assert the actual log line appears nowhere in the
        # serialized result, not merely that a "contents" key is absent.
        serialized = json.dumps(structured) + "".join(
            f"{link.uri}{link.name}{link.description}" for link in links
        )
        assert _SECRET_LOG_LINE not in serialized
        assert "some stdout output" not in serialized

        assert len(links) == 3
        assert all(isinstance(link, ResourceLink) for link in links)
        uris = {str(link.uri) for link in links}
        assert Path(events_log).resolve().as_uri() in uris
        assert Path(stderr_log).resolve().as_uri() in uris
        assert Path(stdout_log).resolve().as_uri() in uris

    def test_structured_content_carries_bounded_metadata_per_file(
        self, conductor_home: Path, tmp_path: Path
    ) -> None:
        events_log = _write_log(
            tmp_path / "conductor-wf-20260101-120000-runlog02.events.jsonl",
            _event("workflow_started", {"name": "wf"}) + "\n",
        )
        write_run_record(_live_record("runlog02", event_log_path=str(events_log)))

        _, structured = conductor_run_logs("runlog02")

        info = structured["files"]["events_log"]
        assert info["exists"] is True
        assert info["size"] == events_log.stat().st_size
        assert info["modified_at"] is not None
        assert info["path"] == str(events_log)

    def test_pruned_path_reports_exists_false_with_the_same_path(
        self, conductor_home: Path, tmp_path: Path
    ) -> None:
        from conductor.fleet.records import write_terminal_record

        pruned_path = str(tmp_path / "conductor-wf-20260101-120000-runlog03.bg.stderr.log")
        write_terminal_record(
            _terminal_record(
                "runlog03",
                event_log_path=str(tmp_path / "already-gone.events.jsonl"),
                bg_stderr_log=pruned_path,
            )
        )

        links, structured = conductor_run_logs("runlog03")

        info = structured["files"]["bg_stderr_log"]
        assert info["exists"] is False
        assert info["path"] == pruned_path
        assert all(link.name != "runlog03-bg.stderr.log" for link in links)

    def test_error_type_and_message_come_from_the_terminal_record(
        self, conductor_home: Path, tmp_path: Path
    ) -> None:
        from conductor.fleet.records import write_terminal_record

        write_terminal_record(
            _terminal_record(
                "runlog04",
                status="failed",
                error_type="ProviderError",
                error_message="the provider blew up",
            )
        )

        _, structured = conductor_run_logs("runlog04")

        assert structured["source"] == "terminal"
        assert structured["error_type"] == "ProviderError"
        assert structured["error_message"] == "the provider blew up"
        assert structured["status"] == "failed"

    def test_unknown_run_id_still_returns_a_well_formed_result(
        self, conductor_home: Path, event_log_dir: Path
    ) -> None:
        links, structured = conductor_run_logs("ghost0009")

        assert links == []
        assert structured["source"] == "not_found"
        for info in structured["files"].values():
            assert info["exists"] is False

    def test_includes_the_read_with_your_own_tools_note(
        self, conductor_home: Path, tmp_path: Path
    ) -> None:
        events_log = _write_log(
            tmp_path / "conductor-wf-20260101-120000-runlog05.events.jsonl", "\n"
        )
        write_run_record(_live_record("runlog05", event_log_path=str(events_log)))

        _, structured = conductor_run_logs("runlog05")

        assert "own file-reading tool" in structured["note"]

    def test_wildcard_run_id_does_not_expose_another_runs_logs(
        self, conductor_home: Path, event_log_dir: Path
    ) -> None:
        """A ``run_id`` such as ``"*"`` must never be interpolated into a
        glob pattern -- it would otherwise match every run's files."""
        _write_log(event_log_dir / "conductor-wf-20260101-120000-victim001.events.jsonl", "\n")
        _write_log(
            event_log_dir / "conductor-wf-20260101-120000-victim001.bg.stderr.log",
            "victim secret\n",
        )

        links, structured = conductor_run_logs("*")

        assert links == []
        assert structured["source"] == "not_found"
        for info in structured["files"].values():
            assert info["exists"] is False

    def test_bg_logs_found_even_when_no_record_or_event_log_exists(
        self, conductor_home: Path, event_log_dir: Path
    ) -> None:
        """The primary failure this tool exists to diagnose: a child that
        died before writing its own run record or event log still left the
        parent-created ``.bg.stderr.log`` behind, in the well-known
        ``$TMPDIR/conductor`` directory."""
        stderr_log = _write_log(
            event_log_dir / "conductor-wf-20260101-120000-diedearly1.bg.stderr.log",
            "startup failure\n",
        )

        links, structured = conductor_run_logs("diedearly1")

        assert structured["source"] == "not_found"
        info = structured["files"]["bg_stderr_log"]
        assert info["exists"] is True
        assert info["path"] == str(stderr_log)
        assert any(link.name == "diedearly1-bg.stderr.log" for link in links)

    def test_bg_log_picks_the_newest_match_not_the_oldest(
        self, conductor_home: Path, tmp_path: Path
    ) -> None:
        """When a resumed run reuses its predecessor's ``run_id`` and both
        left a bg log behind, the newest one is this run's diagnostics --
        the oldest is a previous attempt's stale capture."""
        events_log = _write_log(
            tmp_path / "conductor-wf-20260101-120000-resumed01.events.jsonl", "\n"
        )
        _write_log(
            tmp_path / "conductor-wf-20260101-120000-resumed01.bg.stderr.log",
            "first attempt\n",
        )
        newer = _write_log(
            tmp_path / "conductor-wf-20260101-130000-resumed01.bg.stderr.log",
            "resumed attempt\n",
        )
        write_run_record(_live_record("resumed01", event_log_path=str(events_log)))

        _, structured = conductor_run_logs("resumed01")

        assert structured["files"]["bg_stderr_log"]["path"] == str(newer)


# ---------------------------------------------------------------------------
# E11-T3 / E11-T6: conductor_doctor
# ---------------------------------------------------------------------------


class TestConductorDoctor:
    @pytest.mark.asyncio
    async def test_returns_a_structured_report(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONDUCTOR_NO_UPDATE_CHECK", "1")

        report = await conductor_doctor()

        assert isinstance(report, dict)
        assert "env" in report
        assert report["env"]["conductor_version"]


# ---------------------------------------------------------------------------
# E11-T3 / E11-T6: conductor_validate_workflow
# ---------------------------------------------------------------------------

_VALID_YAML = """\
workflow:
  name: valid-demo
  description: A valid workflow.
  entry_point: worker
agents:
  - name: worker
    prompt: "Do the thing."
    output:
      result:
        type: string
output:
  result: "{{ worker.output.result }}"
"""

_INVALID_YAML = """\
workflow:
  name: invalid-demo
  description: References a nonexistent entry point.
  entry_point: nope
agents:
  - name: worker
    prompt: "Do the thing."
    output:
      result:
        type: string
output:
  result: "{{ worker.output.result }}"
"""


class TestConductorValidateWorkflow:
    def _catalogue(self, tmp_path: Path, *, yaml_text: str, filename: str):
        directory = tmp_path / "wfdir"
        directory.mkdir(exist_ok=True)
        (directory / filename).write_text(yaml_text, encoding="utf-8")
        options = ServeOptions(workflow_dirs=(directory,))
        catalogue = build_catalogue(
            options, registries_config=RegistriesConfig(), allow_network=False
        )
        return catalogue, options

    def test_valid_workflow_returns_a_structured_report(self, tmp_path: Path) -> None:
        catalogue, options = self._catalogue(
            tmp_path, yaml_text=_VALID_YAML, filename="valid-demo.yaml"
        )
        tool_name = catalogue.entries[0].tool_name

        result = conductor_validate_workflow(tool_name, catalogue=catalogue, options=options)

        assert result["is_valid"] is True
        assert result["entry_point"] == "worker"
        assert result["agents"] == ["worker"]

    def test_invalid_workflow_is_reported_as_such(self, tmp_path: Path) -> None:
        # A workflow with a broken `entry_point` reference fails to parse
        # (`WorkflowConfig.validate_references`), so it is exposed by the
        # catalogue with a *degraded* schema rather than the real one --
        # the tool name is still derivable from the filename stem.
        catalogue, options = self._catalogue(
            tmp_path, yaml_text=_INVALID_YAML, filename="invalid-demo.yaml"
        )
        tool_name = catalogue.entries[0].tool_name
        assert catalogue.entries[0].resolution_tier == "degraded"

        result = conductor_validate_workflow(tool_name, catalogue=catalogue, options=options)

        assert result["is_valid"] is False
        assert result["entry_point"] is None
        assert result["agents"] == []

    def test_refuses_an_unknown_tool_name(self, tmp_path: Path) -> None:
        catalogue, options = self._catalogue(
            tmp_path, yaml_text=_VALID_YAML, filename="valid-demo.yaml"
        )

        with pytest.raises(UnknownToolError):
            conductor_validate_workflow("not-a-tool", catalogue=catalogue, options=options)

    def test_refuses_a_path_shaped_argument(self, tmp_path: Path) -> None:
        """NFR3: `conductor_validate_workflow` takes a catalogue tool name,
        never a path -- a path-shaped string is never a catalogue key, so
        it is refused the same way any other unrecognized name is."""
        catalogue, options = self._catalogue(
            tmp_path, yaml_text=_VALID_YAML, filename="valid-demo.yaml"
        )

        with pytest.raises(UnknownToolError):
            conductor_validate_workflow(
                str(tmp_path / "wfdir" / "valid-demo.yaml"), catalogue=catalogue, options=options
            )
