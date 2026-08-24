"""Tests for the run lifecycle tools: the three-source resolver,
``conductor_run_status``, ``conductor_await_run``, ``conductor_cancel_run``,
and ``conductor_list_runs`` (FR6, FR7, DD11, E10).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from conductor.cli.app import StopOutcome
from conductor.fleet.records import (
    RunRecord,
    TerminalRunRecord,
    write_run_record,
)
from conductor.fleet.summary import GateInfo, RunSummary
from conductor.mcp.serve.runs import (
    conductor_await_run,
    conductor_cancel_run,
    conductor_list_runs,
    conductor_run_status,
    resolve_run,
)

# ---------------------------------------------------------------------------
# Shared fixtures / builders
# ---------------------------------------------------------------------------


@pytest.fixture
def conductor_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate ``CONDUCTOR_HOME`` (run/terminal records live under it)."""
    home = tmp_path / "conductor_home"
    home.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))
    return home


@pytest.fixture
def event_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``tempfile.gettempdir()`` so ``find_event_log_for_run`` (used
    by the crash fallback) searches a throwaway directory rather than the
    real machine's ``$TMPDIR/conductor``."""
    d = tmp_path / "tmp"
    d.mkdir()
    (d / "conductor").mkdir()
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(d))
    return d / "conductor"


def _live_record(
    run_id: str = "live0001",
    *,
    pid: int | None = None,
    port: int | None = 9101,
    workflow_name: str = "review-pr",
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        pid=pid if pid is not None else os.getpid(),
        workflow_path=f"/tmp/{workflow_name}.yaml",
        workflow_name=workflow_name,
        started_at="2026-01-01T00:00:00+00:00",
        event_log_path="",
        port=port,
        mode="bg",
        checkpoint_dir=None,
    )


def _summary(
    run_id: str,
    *,
    status: str,
    workflow_name: str = "review-pr",
    port: int | None = 9101,
    gate: GateInfo | None = None,
    gate_resolvable: bool = True,
) -> RunSummary:
    return RunSummary(
        run_id=run_id,
        workflow_name=workflow_name,
        mode="bg",
        port=port,
        started_at="2026-01-01T00:00:00+00:00",
        status=status,  # type: ignore[arg-type]
        current_step="worker",
        current_step_type="agent",
        current_step_started_at=time.time(),
        total_tokens=42,
        total_cost_usd=0.01,
        unpriced_agent_count=0,
        gate=gate,
        gate_resolvable=gate_resolvable,
        topology=None,
    )


def _terminal_record(
    run_id: str = "term0001",
    *,
    status: str = "success",
    workflow_name: str = "review-pr",
    error_type: str | None = None,
    error_message: str | None = None,
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
        total_tokens=100,
        total_cost_usd=0.05,
        unpriced_agent_count=0,
        event_log_path="",
        bg_stderr_log=None,
        bg_stdout_log=None,
    )


def _write_event_log(
    root: Path,
    *,
    name: str = "review-pr",
    ts: str = "20260101-120000",
    run_id: str = "crash001",
    lines: list[str] | None = None,
) -> Path:
    path = root / f"conductor-{name}-{ts}-{run_id}.events.jsonl"
    path.write_text("\n".join(lines or []) + ("\n" if lines else ""))
    return path


def _event(etype: str, data: dict[str, Any] | None = None) -> str:
    import json

    return json.dumps({"type": etype, "timestamp": time.time(), "data": data or {}})


# ---------------------------------------------------------------------------
# E10-T1: resolve_run
# ---------------------------------------------------------------------------


class TestResolveRun:
    def test_live_record_resolves_via_summary(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_run_record(_live_record("live0001"))
        monkeypatch.setattr(
            "conductor.mcp.serve.runs.derive_run_summary",
            lambda record: _summary(record.run_id, status="running"),
        )

        lookup = resolve_run("live0001")

        assert lookup.source == "live"
        assert lookup.record is not None
        assert lookup.record.run_id == "live0001"
        assert lookup.summary is not None
        assert lookup.summary.status == "running"
        assert lookup.terminal is None
        assert lookup.event_log_path is None

    def test_dead_process_falls_through_past_a_stale_live_record(
        self, conductor_home: Path
    ) -> None:
        """A record whose process has already exited (but has not yet been
        pruned) must not be reported live -- it falls through to whichever
        of the remaining two sources answers next."""
        write_run_record(_live_record("stale001", pid=99999999))
        write_terminal = _terminal_record("stale001")
        from conductor.fleet.records import write_terminal_record

        write_terminal_record(write_terminal)

        lookup = resolve_run("stale001")

        assert lookup.source == "terminal"
        assert lookup.terminal is not None
        assert lookup.terminal.run_id == "stale001"

    def test_terminal_record_when_no_live_record_exists(self, conductor_home: Path) -> None:
        from conductor.fleet.records import write_terminal_record

        write_terminal_record(_terminal_record("done0001"))

        lookup = resolve_run("done0001")

        assert lookup.source == "terminal"
        assert lookup.record is None
        assert lookup.summary is None
        assert lookup.terminal is not None
        assert lookup.terminal.run_id == "done0001"

    def test_crash_falls_back_to_event_log(self, conductor_home: Path, event_log_dir: Path) -> None:
        """No live record, no terminal record: the process crashed before
        its ``finally`` block ran. The event log is still located by
        filename."""
        _write_event_log(
            event_log_dir,
            run_id="crash001",
            lines=[_event("workflow_started", {"name": "review-pr"})],
        )

        lookup = resolve_run("crash001")

        assert lookup.source == "event_log"
        assert lookup.record is None
        assert lookup.summary is None
        assert lookup.terminal is None
        assert lookup.event_log_path is not None
        assert lookup.event_log_path.name.endswith("crash001.events.jsonl")

    def test_not_found_when_nothing_matches(
        self, conductor_home: Path, event_log_dir: Path
    ) -> None:
        lookup = resolve_run("nope0001")

        assert lookup.source == "not_found"
        assert lookup.record is None
        assert lookup.summary is None
        assert lookup.terminal is None
        assert lookup.event_log_path is None

    def test_wildcard_run_id_is_rejected_before_any_lookup(
        self, conductor_home: Path, event_log_dir: Path
    ) -> None:
        """A ``run_id`` such as ``"*"`` must never be interpolated into a
        glob pattern -- rejected up front rather than reaching
        :func:`conductor.fleet.records.find_event_log_for_run`."""
        (event_log_dir / "conductor-wf-20260101-120000-victim002.events.jsonl").write_text("\n")

        lookup = resolve_run("*")

        assert lookup.source == "not_found"
        assert lookup.event_log_path is None

    def test_path_traversal_run_id_is_rejected_before_any_lookup(
        self, conductor_home: Path, event_log_dir: Path
    ) -> None:
        lookup = resolve_run("../../etc/passwd")

        assert lookup.source == "not_found"
        assert lookup.event_log_path is None


# ---------------------------------------------------------------------------
# E10-T2: conductor_run_status
# ---------------------------------------------------------------------------


class TestConductorRunStatus:
    def test_status_for_a_live_run(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_run_record(_live_record("live0002"))
        monkeypatch.setattr(
            "conductor.mcp.serve.runs.derive_run_summary",
            lambda record: _summary(record.run_id, status="running"),
        )

        result = conductor_run_status("live0002")

        assert result["run_id"] == "live0002"
        assert result["source"] == "live"
        assert result["status"] == "running"
        assert result["workflow_name"] == "review-pr"
        assert result["url"] == "http://127.0.0.1:9101"
        assert "gate" not in result

    def test_status_at_a_gate_includes_prompt_options_and_url(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_run_record(_live_record("gate0001"))
        gate = GateInfo(
            agent_name="reviewer",
            prompt="Approve this change?",
            options=["approve", "reject"],
            option_details=[{"value": "approve", "label": "Approve"}],
        )
        monkeypatch.setattr(
            "conductor.mcp.serve.runs.derive_run_summary",
            lambda record: _summary(record.run_id, status="at-gate", gate=gate),
        )

        result = conductor_run_status("gate0001")

        assert result["status"] == "at-gate"
        assert result["gate"] == {
            "agent_name": "reviewer",
            "prompt": "Approve this change?",
            "options": ["approve", "reject"],
            "option_details": [{"value": "approve", "label": "Approve"}],
        }
        assert result["gate_resolvable"] is True
        assert result["approval_url"] == "http://127.0.0.1:9101"
        assert "http://127.0.0.1:9101" in result["next"]

    def test_status_for_a_cleanly_finished_run_needs_no_process_port_or_log(
        self, conductor_home: Path
    ) -> None:
        from conductor.fleet.records import write_terminal_record

        write_terminal_record(
            _terminal_record("done0002", status="success", workflow_name="review-pr")
        )

        result = conductor_run_status("done0002")

        assert result["source"] == "terminal"
        assert result["status"] == "completed"
        assert result["workflow_name"] == "review-pr"
        assert result["output"] == {"result": "ok"}
        assert result["error_type"] is None

    def test_status_for_a_failed_finished_run(self, conductor_home: Path) -> None:
        from conductor.fleet.records import write_terminal_record

        write_terminal_record(
            _terminal_record(
                "failed001",
                status="failed",
                error_type="ProviderError",
                error_message="boom",
            )
        )

        result = conductor_run_status("failed001")

        assert result["status"] == "failed"
        assert result["error_type"] == "ProviderError"
        assert result["error_message"] == "boom"

    def test_status_for_a_crashed_run_uses_event_log_fallback_and_names_the_source(
        self, conductor_home: Path, event_log_dir: Path
    ) -> None:
        _write_event_log(
            event_log_dir,
            run_id="crash002",
            lines=[_event("workflow_started", {"name": "review-pr"})],
        )

        result = conductor_run_status("crash002")

        assert result["source"] == "event_log"
        assert result["status"] == "unknown"
        assert result["workflow_name"] == "review-pr"
        assert "event_log_path" in result

    def test_status_for_an_unknown_run_id(self, conductor_home: Path, event_log_dir: Path) -> None:
        result = conductor_run_status("ghost001")

        assert result["source"] == "not_found"
        assert result["status"] == "unknown"
        assert "error" in result


# ---------------------------------------------------------------------------
# E10-T3: conductor_await_run
# ---------------------------------------------------------------------------


class TestConductorAwaitRun:
    async def test_returns_early_when_reaching_a_gate(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_run_record(_live_record("await001"))
        gate = GateInfo(
            agent_name="reviewer", prompt="Approve?", options=["yes"], option_details=[]
        )
        monkeypatch.setattr(
            "conductor.mcp.serve.runs.derive_run_summary",
            lambda record: _summary(record.run_id, status="at-gate", gate=gate),
        )

        start = time.monotonic()
        result = await conductor_await_run("await001", wait_seconds=100.0, max_wait_seconds=300)
        elapsed = time.monotonic() - start

        assert elapsed < 2.0
        assert result["status"] == "at-gate"
        assert result["source"] == "live"
        assert "http://127.0.0.1:9101" in result["next"]

    async def test_returns_early_on_terminal_status(self, conductor_home: Path) -> None:
        from conductor.fleet.records import write_terminal_record

        write_terminal_record(_terminal_record("await002", status="success"))

        start = time.monotonic()
        result = await conductor_await_run("await002", wait_seconds=100.0, max_wait_seconds=300)
        elapsed = time.monotonic() - start

        assert elapsed < 2.0
        assert result["source"] == "terminal"
        assert result["status"] == "completed"

    async def test_timeout_while_still_running_names_a_next_action(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_run_record(_live_record("await003"))
        monkeypatch.setattr(
            "conductor.mcp.serve.runs.derive_run_summary",
            lambda record: _summary(record.run_id, status="running"),
        )

        result = await conductor_await_run("await003", wait_seconds=0.1, max_wait_seconds=300)

        assert result["status"] == "running"
        assert "conductor_await_run" in result["next"]

    async def test_wait_seconds_is_bounded_by_max_wait_seconds(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A caller requesting a huge `wait_seconds` is still bounded by the
        server's `--max-wait-seconds` ceiling."""
        write_run_record(_live_record("await004"))
        monkeypatch.setattr(
            "conductor.mcp.serve.runs.derive_run_summary",
            lambda record: _summary(record.run_id, status="running"),
        )

        start = time.monotonic()
        await conductor_await_run("await004", wait_seconds=99_999.0, max_wait_seconds=0)
        elapsed = time.monotonic() - start

        assert elapsed < 2.0

    async def test_progress_emitted_only_when_token_and_sender_supplied(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_run_record(_live_record("await005"))
        monkeypatch.setattr(
            "conductor.mcp.serve.runs.derive_run_summary",
            lambda record: _summary(record.run_id, status="running"),
        )

        progress_calls: list[tuple[Any, ...]] = []

        async def _record_progress(
            token: Any, progress: float, total: float | None, message: str | None
        ) -> None:
            progress_calls.append((token, progress, total, message))

        await conductor_await_run(
            "await005",
            wait_seconds=0.1,
            max_wait_seconds=300,
            progress_token="tok-1",
            send_progress=_record_progress,
        )
        assert len(progress_calls) >= 1
        assert progress_calls[0][0] == "tok-1"

        progress_calls.clear()
        await conductor_await_run(
            "await005",
            wait_seconds=0.1,
            max_wait_seconds=300,
            progress_token=None,
            send_progress=_record_progress,
        )
        assert progress_calls == []


# ---------------------------------------------------------------------------
# E10-T4: conductor_cancel_run
# ---------------------------------------------------------------------------


class TestConductorCancelRun:
    def test_cancel_routes_through_stop_records_and_reports_stopped(
        self, conductor_home: Path
    ) -> None:
        record = _live_record("cancel01")
        write_run_record(record)

        calls: list[dict[str, Any]] = []

        def _fake_stop_records(targets, con, *, confirm=None):
            calls.append({"targets": targets, "confirm": confirm})
            return StopOutcome(declined=False, stopped=list(targets), failed=[])

        # `unittest.mock.patch` (not `monkeypatch.setattr("conductor.cli.app...")`):
        # `conductor.cli.__init__` does `from conductor.cli.app import app`, which
        # rebinds the *attribute* `conductor.cli.app` to the Typer instance --
        # monkeypatch's string-path resolver walks attributes and finds that
        # Typer object instead of the module, while `mock.patch` resolves via
        # `importlib.import_module` and reaches the real module.
        with patch("conductor.cli.app.stop_records", _fake_stop_records):
            result = conductor_cancel_run("cancel01")

        assert result["status"] == "stopped"
        assert result["run_id"] == "cancel01"
        assert len(calls) == 1
        assert calls[0]["confirm"] is None
        assert [r.run_id for r in calls[0]["targets"]] == ["cancel01"]

    def test_cancel_reports_a_failure_honestly(self, conductor_home: Path) -> None:
        record = _live_record("cancel02")
        write_run_record(record)

        def _fake_stop_records(targets, con, *, confirm=None):
            return StopOutcome(declined=False, stopped=[], failed=[(targets[0], "survived")])

        with patch("conductor.cli.app.stop_records", _fake_stop_records):
            result = conductor_cancel_run("cancel02")

        assert result["status"] == "failed"
        assert result["reason"] == "survived"

    def test_cancel_on_already_terminal_run_is_a_distinct_non_error_outcome(
        self, conductor_home: Path
    ) -> None:
        from conductor.fleet.records import write_terminal_record

        write_terminal_record(_terminal_record("cancel03", status="success"))

        calls: list[Any] = []
        with patch("conductor.cli.app.stop_records", lambda *a, **k: calls.append((a, k))):
            result = conductor_cancel_run("cancel03")

        assert result["status"] == "already_terminal"
        assert result["source"] == "terminal"
        assert result["run_status"] == "completed"
        assert calls == []  # stop_records must never be called for a dead run

    def test_force_true_notes_that_the_ladder_is_not_escalated(self, conductor_home: Path) -> None:
        record = _live_record("cancel04")
        write_run_record(record)

        def _fake_stop_records(targets, con, *, confirm=None):
            return StopOutcome(declined=False, stopped=[], failed=[(targets[0], "unconfirmed")])

        with patch("conductor.cli.app.stop_records", _fake_stop_records):
            result = conductor_cancel_run("cancel04", force=True)

        assert result["status"] == "failed"
        assert "note" in result
        assert "force" in result["note"].lower()


# ---------------------------------------------------------------------------
# E10-T5: conductor_list_runs
# ---------------------------------------------------------------------------


class TestConductorListRuns:
    def test_lists_live_and_terminal_runs(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from conductor.fleet.records import write_terminal_record

        write_run_record(_live_record("list0001", workflow_name="review-pr"))
        monkeypatch.setattr(
            "conductor.mcp.serve.runs.derive_run_summary",
            lambda record: _summary(
                record.run_id, status="running", workflow_name=record.workflow_name
            ),
        )
        write_terminal_record(_terminal_record("list0002", workflow_name="deploy"))

        entries = conductor_list_runs()

        by_id = {e["run_id"]: e for e in entries}
        assert by_id["list0001"]["source"] == "live"
        assert by_id["list0001"]["status"] == "running"
        assert by_id["list0002"]["source"] == "terminal"
        assert by_id["list0002"]["status"] == "completed"

    def test_filters_by_status(self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from conductor.fleet.records import write_terminal_record

        write_run_record(_live_record("list0003", workflow_name="a"))
        write_run_record(_live_record("list0004", workflow_name="b"))

        def _fake_summary(record: RunRecord) -> RunSummary:
            status = "at-gate" if record.run_id == "list0003" else "running"
            gate = (
                GateInfo(agent_name="x", prompt="p", options=[], option_details=[])
                if status == "at-gate"
                else None
            )
            return _summary(
                record.run_id, status=status, workflow_name=record.workflow_name, gate=gate
            )

        monkeypatch.setattr("conductor.mcp.serve.runs.derive_run_summary", _fake_summary)
        write_terminal_record(_terminal_record("list0005", workflow_name="c"))

        parked = conductor_list_runs(status="at-gate")

        assert [e["run_id"] for e in parked] == ["list0003"]

    def test_filters_by_workflow(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from conductor.fleet.records import write_terminal_record

        write_run_record(_live_record("list0006", workflow_name="review-pr"))
        monkeypatch.setattr(
            "conductor.mcp.serve.runs.derive_run_summary",
            lambda record: _summary(
                record.run_id, status="running", workflow_name=record.workflow_name
            ),
        )
        write_terminal_record(_terminal_record("list0007", workflow_name="deploy"))

        entries = conductor_list_runs(workflow="deploy")

        assert [e["run_id"] for e in entries] == ["list0007"]

    def test_dedupes_a_run_id_present_in_both_sources_with_live_winning(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from conductor.fleet.records import write_terminal_record

        write_run_record(_live_record("dup00001", workflow_name="review-pr"))
        monkeypatch.setattr(
            "conductor.mcp.serve.runs.derive_run_summary",
            lambda record: _summary(
                record.run_id, status="running", workflow_name=record.workflow_name
            ),
        )
        # The narrow race: the process is still exiting but has already
        # written its tombstone.
        write_terminal_record(_terminal_record("dup00001", status="success"))

        entries = conductor_list_runs()

        matches = [e for e in entries if e["run_id"] == "dup00001"]
        assert len(matches) == 1
        assert matches[0]["source"] == "live"

    def test_respects_limit(self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from conductor.fleet.records import write_terminal_record

        for i in range(5):
            write_terminal_record(_terminal_record(f"lim{i:05d}", workflow_name="w"))

        entries = conductor_list_runs(limit=2)

        assert len(entries) == 2
