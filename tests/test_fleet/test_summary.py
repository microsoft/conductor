"""Tests for ``conductor.fleet.summary`` (Fleet Manager E6/E9 — RunSummary and
RunDetail derivation).

Covers:
- Bounded JSONL tail reading: whole small file, seek-and-discard-partial-line
  for a file larger than the tail window, tolerance of a mid-line truncation,
  an empty file, and a missing file.
- Every status in the design's vocabulary (`running`, `at-gate`, `paused`,
  `completed`, `failed`), including a gate opened then resolved returning to
  `running`.
- Current step / elapsed-on-step derivation for a plain agent, a for_each
  group, and non-LLM step types (script/wait/set/human_gate) that close via
  their own distinct completion event rather than `agent_completed`.
- Token/cost totals, completed-only labelling, and the unpriced-agent
  tracking convention (mirroring `engine.usage.WorkflowUsage`).
- Topology extraction from `workflow_started`.
- The gate payload carried onto the summary, and `gate_resolvable` per D4
  (true for `fg-web`/`bg`, false for `fg`).
- (E9-T3) The bounded *full*-log reader and per-agent `RunDetail` derivation
  used only by the run-detail screen: per-agent pending/running/completed/
  failed status, elapsed/tokens/cost per agent, and graceful degradation
  when the log or its `workflow_started` event is missing.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from conductor.fleet.records import RunRecord
from conductor.fleet.summary import (
    RunDetail,
    RunSummary,
    derive_run_detail,
    derive_run_summary,
    read_event_log_full,
    read_event_log_tail,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(etype: str, data: dict[str, Any] | None = None, *, ts: float | None = None) -> str:
    """Serialize a single JSONL event line matching WorkflowEvent.to_dict()'s shape."""
    return json.dumps(
        {"type": etype, "timestamp": ts if ts is not None else time.time(), "data": data or {}}
    )


def _write_jsonl(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n")


def _make_record(tmp_path: Path, **overrides: object) -> RunRecord:
    defaults: dict[str, object] = {
        "run_id": "abc123",
        "pid": os.getpid(),
        "workflow_path": "/tmp/workflow.yaml",
        "workflow_name": "workflow",
        "started_at": "2026-01-01T00:00:00+00:00",
        "event_log_path": str(tmp_path / "run.events.jsonl"),
        "port": 8080,
        "mode": "bg",
        "checkpoint_dir": "/tmp/conductor/checkpoints",
    }
    defaults.update(overrides)
    return RunRecord(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# read_event_log_tail (E6-T1)
# ---------------------------------------------------------------------------


class TestReadEventLogTail:
    def test_reads_all_events_when_file_smaller_than_window(self, tmp_path: Path) -> None:
        path = tmp_path / "small.events.jsonl"
        _write_jsonl(
            path,
            [
                _event("agent_started", {"agent_name": "a"}),
                _event("agent_completed", {"agent_name": "a"}),
            ],
        )

        events = read_event_log_tail(path, tail_bytes=1024)

        assert len(events) == 2
        assert events[0]["type"] == "agent_started"
        assert events[1]["type"] == "agent_completed"

    def test_bounded_read_discards_older_events(self, tmp_path: Path) -> None:
        """A file much larger than the tail window only yields the newest events --
        confirming the read is bounded, not whole-file."""
        path = tmp_path / "large.events.jsonl"
        lines = [_event("agent_started", {"agent_name": f"agent-{i}"}) for i in range(500)]
        _write_jsonl(path, lines)

        events = read_event_log_tail(path, tail_bytes=200)

        assert 0 < len(events) < 500
        # Only the newest (highest-index) agents survive in the tail window.
        names = [e["data"]["agent_name"] for e in events]
        assert names[-1] == "agent-499"
        assert "agent-0" not in names

    def test_discards_partial_leading_line_at_seek_boundary(self, tmp_path: Path) -> None:
        """The line straddling the seek point must never surface as a garbled event."""
        path = tmp_path / "boundary.events.jsonl"
        lines = [_event("agent_started", {"agent_name": f"agent-{i}"}) for i in range(50)]
        _write_jsonl(path, lines)

        # Pick a range of tail_bytes that lands mid-line at various boundaries.
        saw_at_least_one_event = False
        for tail_bytes in range(10, 200, 7):
            events = read_event_log_tail(path, tail_bytes=tail_bytes)
            if events:
                saw_at_least_one_event = True
            for e in events:
                assert e["type"] == "agent_started"
                assert e["data"]["agent_name"].startswith("agent-")
        # Confirms the loop above actually exercised parsed events, not just
        # a vacuously-true "no garbled events" over consistently-empty results.
        assert saw_at_least_one_event

    def test_tolerates_truncated_mid_line(self, tmp_path: Path) -> None:
        path = tmp_path / "truncated.events.jsonl"
        good1 = _event("agent_started", {"agent_name": "a"})
        bad = '{"type": "agent_completed", "data": {"agent_nam'  # cut mid-object
        good2 = _event("workflow_completed", {})
        path.write_text(f"{good1}\n{bad}\n{good2}\n")

        events = read_event_log_tail(path, tail_bytes=4096)

        types = [e["type"] for e in events]
        assert types == ["agent_started", "workflow_completed"]

    def test_empty_file_yields_no_events(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.events.jsonl"
        path.write_text("")

        events = read_event_log_tail(path)

        assert events == []

    def test_missing_file_yields_no_events(self, tmp_path: Path) -> None:
        path = tmp_path / "does-not-exist.events.jsonl"

        events = read_event_log_tail(path)

        assert events == []


# ---------------------------------------------------------------------------
# Status vocabulary (E6-T2)
# ---------------------------------------------------------------------------


class TestStatusVocabulary:
    def test_running_is_the_default(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path, [_event("workflow_started", {}), _event("agent_started", {"agent_name": "a"})]
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        summary = derive_run_summary(record)

        assert summary.status == "running"

    def test_at_gate_from_gate_presented(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _event("agent_started", {"agent_name": "review"}),
                _event(
                    "gate_presented",
                    {
                        "agent_name": "review",
                        "prompt": "OK?",
                        "options": ["yes", "no"],
                        "option_details": [],
                    },
                ),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        summary = derive_run_summary(record)

        assert summary.status == "at-gate"
        assert summary.gate is not None

    def test_gate_opened_then_resolved_returns_to_running(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _event("agent_started", {"agent_name": "review"}),
                _event(
                    "gate_presented",
                    {
                        "agent_name": "review",
                        "prompt": "OK?",
                        "options": ["yes"],
                        "option_details": [],
                    },
                ),
                _event("gate_resolved", {"agent_name": "review", "selected_option": "yes"}),
                _event("agent_started", {"agent_name": "next"}),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        summary = derive_run_summary(record)

        assert summary.status == "running"
        assert summary.gate is None
        assert summary.current_step == "next"

    def test_paused_from_agent_paused(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _event("agent_started", {"agent_name": "a"}),
                _event("agent_paused", {"agent_name": "a", "partial_content": "..."}),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        summary = derive_run_summary(record)

        assert summary.status == "paused"

    def test_paused_then_resumed_returns_to_running(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _event("agent_started", {"agent_name": "a"}),
                _event("agent_paused", {"agent_name": "a", "partial_content": "..."}),
                _event("agent_started", {"agent_name": "a"}),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        summary = derive_run_summary(record)

        assert summary.status == "running"

    def test_completed_from_workflow_completed(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _event("agent_started", {"agent_name": "a"}),
                _event("agent_completed", {"agent_name": "a", "tokens": 10, "cost_usd": 0.01}),
                _event("workflow_completed", {"elapsed": 5.0}),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        summary = derive_run_summary(record)

        assert summary.status == "completed"

    def test_failed_from_workflow_failed(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _event("agent_started", {"agent_name": "a"}),
                _event("workflow_failed", {"error_type": "ProviderError", "message": "boom"}),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        summary = derive_run_summary(record)

        assert summary.status == "failed"

    def test_no_events_yet_is_running(self, tmp_path: Path) -> None:
        """A log with no events yet (freshly created, empty) is still a normal
        'running' state -- the record itself is known-live."""
        path = tmp_path / "run.events.jsonl"
        path.write_text("")
        record = _make_record(tmp_path, event_log_path=str(path))

        summary = derive_run_summary(record)

        assert summary.status == "running"
        assert summary.current_step is None
        assert summary.total_tokens == 0
        assert summary.total_cost_usd is None
        assert summary.gate is None

    def test_missing_event_log_path_never_raises(self, tmp_path: Path) -> None:
        record = _make_record(tmp_path, event_log_path="")

        summary = derive_run_summary(record)

        assert summary.status == "running"
        assert summary.current_step is None


# ---------------------------------------------------------------------------
# Current step / elapsed-on-step (E6-T3)
# ---------------------------------------------------------------------------


class TestCurrentStep:
    def test_agent_started_without_completed_is_current_step(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        start_ts = 1000.0
        _write_jsonl(path, [_event("agent_started", {"agent_name": "researcher"}, ts=start_ts)])
        record = _make_record(tmp_path, event_log_path=str(path))

        summary = derive_run_summary(record, now=start_ts + 42.0)

        assert summary.current_step == "researcher"
        assert summary.current_step_type == "agent"
        assert summary.elapsed_on_step_seconds(now=start_ts + 42.0) == 42.0

    def test_agent_completed_closes_the_step(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _event("agent_started", {"agent_name": "a"}),
                _event("agent_completed", {"agent_name": "a", "tokens": 5, "cost_usd": 0.001}),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        summary = derive_run_summary(record)

        assert summary.current_step is None
        assert summary.elapsed_on_step_seconds() is None

    def test_for_each_group_as_current_step(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _event(
                    "for_each_started",
                    {
                        "group_name": "triage",
                        "item_count": 7,
                        "max_concurrent": 3,
                        "failure_mode": "fail_fast",
                    },
                ),
                _event(
                    "for_each_item_started", {"group_name": "triage", "item_key": "0", "index": 0}
                ),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        summary = derive_run_summary(record)

        assert summary.current_step == "triage"
        assert summary.current_step_type == "for_each"

    def test_for_each_completed_closes_the_group_step(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _event("for_each_started", {"group_name": "triage", "item_count": 2}),
                _event("for_each_completed", {"group_name": "triage"}),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        summary = derive_run_summary(record)

        assert summary.current_step is None

    def test_parallel_group_as_current_step(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path, [_event("parallel_started", {"group_name": "fanout", "agents": ["a", "b"]})]
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        summary = derive_run_summary(record)

        assert summary.current_step == "fanout"
        assert summary.current_step_type == "parallel"

    def test_script_step_closes_via_script_completed_not_agent_completed(
        self, tmp_path: Path
    ) -> None:
        """script/wait/set/human_gate steps get agent_started but never
        agent_completed -- their own *_completed event must close the step
        so it doesn't appear stuck open forever."""
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _event("agent_started", {"agent_name": "build", "agent_type": "script"}),
                _event("script_started", {"agent_name": "build", "command": "make"}),
                _event("script_completed", {"agent_name": "build", "exit_code": 0}),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        summary = derive_run_summary(record)

        assert summary.current_step is None

    def test_wait_and_set_steps_close_the_open_agent_step(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _event("agent_started", {"agent_name": "cooldown", "agent_type": "wait"}),
                _event("wait_started", {"agent_name": "cooldown"}),
                _event("wait_completed", {"agent_name": "cooldown", "waited_seconds": 1.0}),
                _event("agent_started", {"agent_name": "bind", "agent_type": "set"}),
                _event("set_started", {"agent_name": "bind"}),
                _event("set_completed", {"agent_name": "bind"}),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        summary = derive_run_summary(record)

        assert summary.current_step is None

    def test_human_gate_step_closes_via_gate_resolved(self, tmp_path: Path) -> None:
        """A human_gate step's `agent_started` never gets an `agent_completed` --
        `gate_resolved` must close it, or current_step would show the gate's
        agent forever after the workflow has moved on."""
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _event("agent_started", {"agent_name": "review", "agent_type": "human_gate"}),
                _event(
                    "gate_presented",
                    {
                        "agent_name": "review",
                        "prompt": "OK?",
                        "options": ["yes"],
                        "option_details": [],
                    },
                ),
                _event("gate_resolved", {"agent_name": "review", "selected_option": "yes"}),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        summary = derive_run_summary(record)

        assert summary.current_step is None
        assert summary.status == "running"

    def test_total_elapsed_from_record_started_at(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        path.write_text("")
        record = _make_record(
            tmp_path, event_log_path=str(path), started_at="2026-01-01T00:00:00+00:00"
        )

        summary = derive_run_summary(record)

        from datetime import UTC, datetime

        start_epoch = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
        elapsed = summary.total_elapsed_seconds(now=start_epoch + 100.0)
        assert elapsed == 100.0

    def test_total_elapsed_none_for_unparseable_started_at(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        path.write_text("")
        record = _make_record(tmp_path, event_log_path=str(path), started_at="?")

        summary = derive_run_summary(record)

        assert summary.total_elapsed_seconds() is None


# ---------------------------------------------------------------------------
# Token / cost totals (E6-T4)
# ---------------------------------------------------------------------------


class TestTokenAndCostTotals:
    def test_sums_tokens_and_cost_across_completed_agents(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _event("agent_completed", {"agent_name": "a", "tokens": 100, "cost_usd": 0.01}),
                _event("agent_completed", {"agent_name": "b", "tokens": 200, "cost_usd": 0.02}),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        summary = derive_run_summary(record)

        assert summary.total_tokens == 300
        assert summary.total_cost_usd is not None
        assert abs(summary.total_cost_usd - 0.03) < 1e-9
        assert summary.has_unpriced is False

    def test_unpriced_agent_tracked_not_summed_as_zero(self, tmp_path: Path) -> None:
        """An agent with tokens but null cost_usd must be excluded from the
        cost total and counted as unpriced -- never silently treated as $0."""
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _event(
                    "agent_completed", {"agent_name": "priced", "tokens": 100, "cost_usd": 0.05}
                ),
                _event(
                    "agent_completed", {"agent_name": "unpriced", "tokens": 50, "cost_usd": None}
                ),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        summary = derive_run_summary(record)

        assert summary.total_tokens == 150
        assert summary.total_cost_usd == 0.05
        assert summary.unpriced_agent_count == 1
        assert summary.has_unpriced is True

    def test_all_unpriced_yields_none_cost_not_zero(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [_event("agent_completed", {"agent_name": "a", "tokens": 10, "cost_usd": None})],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        summary = derive_run_summary(record)

        assert summary.total_cost_usd is None
        assert summary.has_unpriced is True
        assert summary.unpriced_agent_count == 1

    def test_zero_token_null_cost_agent_not_counted_unpriced(self, tmp_path: Path) -> None:
        """An agent that consumed no tokens (e.g. a free/no-op path) with a
        null cost is not "unpriced" -- there was nothing to price."""
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [_event("agent_completed", {"agent_name": "a", "tokens": 0, "cost_usd": None})],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        summary = derive_run_summary(record)

        assert summary.unpriced_agent_count == 0
        assert summary.has_unpriced is False

    def test_no_completed_agents_yields_zero_tokens_none_cost(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(path, [_event("agent_started", {"agent_name": "a"})])
        record = _make_record(tmp_path, event_log_path=str(path))

        summary = derive_run_summary(record)

        assert summary.total_tokens == 0
        assert summary.total_cost_usd is None
        assert summary.has_unpriced is False


# ---------------------------------------------------------------------------
# Topology extraction (E6-T5)
# ---------------------------------------------------------------------------


class TestTopologyExtraction:
    def test_extracts_agents_and_entry_point(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _event(
                    "workflow_started",
                    {
                        "name": "my-workflow",
                        "entry_point": "researcher",
                        "agents": [
                            {
                                "name": "researcher",
                                "type": "agent",
                                "model": "gpt-5",
                                "provider_name": "copilot",
                            },
                            {
                                "name": "writer",
                                "type": "agent",
                                "model": "claude-sonnet",
                                "provider_name": "claude",
                            },
                        ],
                    },
                )
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        summary = derive_run_summary(record)

        assert summary.topology is not None
        assert summary.topology.entry_point == "researcher"
        assert len(summary.topology.agents) == 2
        assert summary.topology.agents[0].name == "researcher"
        assert summary.topology.agents[0].model == "gpt-5"
        assert summary.topology.agents[0].provider_name == "copilot"
        assert summary.topology.agents[1].name == "writer"

    def test_no_workflow_started_in_window_yields_none_topology(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(path, [_event("agent_started", {"agent_name": "a"})])
        record = _make_record(tmp_path, event_log_path=str(path))

        summary = derive_run_summary(record)

        assert summary.topology is None


# ---------------------------------------------------------------------------
# Gate payload + gate_resolvable (E6-T6)
# ---------------------------------------------------------------------------


class TestGatePayloadAndResolvable:
    def test_gate_payload_carries_prompt_and_options(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        option_details = [
            {"label": "Approve", "value": "approve", "route": "$end", "prompt_for": None}
        ]
        _write_jsonl(
            path,
            [
                _event(
                    "gate_presented",
                    {
                        "agent_name": "review",
                        "prompt": "Ship it?",
                        "options": ["approve", "reject"],
                        "option_details": option_details,
                    },
                )
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        summary = derive_run_summary(record)

        assert summary.gate is not None
        assert summary.gate.agent_name == "review"
        assert summary.gate.prompt == "Ship it?"
        assert summary.gate.options == ["approve", "reject"]
        assert summary.gate.option_details == option_details

    def test_gate_resolvable_true_for_fg_web(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        path.write_text("")
        record = _make_record(tmp_path, event_log_path=str(path), mode="fg-web", port=9001)

        summary = derive_run_summary(record)

        assert summary.gate_resolvable is True

    def test_gate_resolvable_true_for_bg(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        path.write_text("")
        record = _make_record(tmp_path, event_log_path=str(path), mode="bg", port=9002)

        summary = derive_run_summary(record)

        assert summary.gate_resolvable is True

    def test_gate_resolvable_false_for_fg(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        path.write_text("")
        record = _make_record(tmp_path, event_log_path=str(path), mode="fg", port=None)

        summary = derive_run_summary(record)

        assert summary.gate_resolvable is False


# ---------------------------------------------------------------------------
# RunSummary identity fields pass through from the record
# ---------------------------------------------------------------------------


class TestRunSummaryIdentityFields:
    def test_passes_through_record_fields(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        path.write_text("")
        record = _make_record(
            tmp_path,
            run_id="run-xyz",
            workflow_name="my-flow",
            mode="fg-web",
            port=1234,
            started_at="2026-02-02T00:00:00+00:00",
            event_log_path=str(path),
        )

        summary = derive_run_summary(record)

        assert isinstance(summary, RunSummary)
        assert summary.run_id == "run-xyz"
        assert summary.workflow_name == "my-flow"
        assert summary.mode == "fg-web"
        assert summary.port == 1234
        assert summary.started_at == "2026-02-02T00:00:00+00:00"


# ---------------------------------------------------------------------------
# read_event_log_full (E9-T3)
# ---------------------------------------------------------------------------


def _workflow_started_event(agent_names: list[str], *, ts: float | None = None) -> str:
    return _event(
        "workflow_started",
        {
            "name": "wf",
            "entry_point": agent_names[0] if agent_names else None,
            "agents": [
                {"name": n, "type": "agent", "model": "gpt-5", "provider_name": "copilot"}
                for n in agent_names
            ],
        },
        ts=ts,
    )


class TestReadEventLogFull:
    def test_reads_all_events_when_file_smaller_than_bound(self, tmp_path: Path) -> None:
        path = tmp_path / "small.events.jsonl"
        _write_jsonl(
            path,
            [
                _workflow_started_event(["a"]),
                _event("agent_started", {"agent_name": "a"}),
                _event("agent_completed", {"agent_name": "a", "tokens": 5, "cost_usd": 0.01}),
            ],
        )

        events = read_event_log_full(path)

        assert len(events) == 3
        assert events[0]["type"] == "workflow_started"
        assert events[-1]["type"] == "agent_completed"

    def test_bounded_read_truncates_a_pathologically_large_log(self, tmp_path: Path) -> None:
        """A log far larger than max_bytes is truncated (from the start
        forward, unlike the tail reader) rather than loaded in full."""
        path = tmp_path / "huge.events.jsonl"
        lines = [_workflow_started_event(["a"])]
        lines += [_event("agent_started", {"agent_name": f"agent-{i}"}) for i in range(5000)]
        _write_jsonl(path, lines)
        full_size = path.stat().st_size

        events = read_event_log_full(path, max_bytes=200)

        assert 0 < len(events) < len(lines)
        # First event (workflow_started) still present -- confirms this
        # reads from the start, the opposite end from read_event_log_tail.
        assert events[0]["type"] == "workflow_started"
        assert full_size > 200

    def test_empty_file_yields_no_events(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.events.jsonl"
        path.write_text("")

        assert read_event_log_full(path) == []

    def test_missing_file_yields_no_events(self, tmp_path: Path) -> None:
        path = tmp_path / "does-not-exist.events.jsonl"

        assert read_event_log_full(path) == []

    def test_tolerates_truncated_mid_line(self, tmp_path: Path) -> None:
        path = tmp_path / "truncated.events.jsonl"
        good = _workflow_started_event(["a"])
        bad = '{"type": "agent_started", "data": {"agent_nam'
        path.write_text(f"{good}\n{bad}\n")

        events = read_event_log_full(path)

        assert len(events) == 1
        assert events[0]["type"] == "workflow_started"


# ---------------------------------------------------------------------------
# derive_run_detail (E9-T3)
# ---------------------------------------------------------------------------


class TestDeriveRunDetailTopologyAndOrder:
    def test_agents_rendered_in_topology_order(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(path, [_workflow_started_event(["researcher", "writer", "reviewer"])])
        record = _make_record(tmp_path, event_log_path=str(path))

        detail = derive_run_detail(record)

        assert isinstance(detail, RunDetail)
        assert detail.topology is not None
        assert [a.name for a in detail.agents] == ["researcher", "writer", "reviewer"]

    def test_agent_fields_carried_from_topology(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(path, [_workflow_started_event(["researcher"])])
        record = _make_record(tmp_path, event_log_path=str(path))

        detail = derive_run_detail(record)

        agent = detail.agents[0]
        assert agent.type == "agent"
        assert agent.model == "gpt-5"
        assert agent.provider_name == "copilot"


class TestDeriveRunDetailPerAgentStatus:
    def test_agent_never_started_is_pending(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _workflow_started_event(["researcher", "writer"]),
                _event("agent_started", {"agent_name": "researcher"}, ts=1000.0),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        detail = derive_run_detail(record)

        writer = next(a for a in detail.agents if a.name == "writer")
        assert writer.status == "pending"
        assert writer.elapsed_seconds() is None
        assert writer.tokens is None
        assert writer.cost_usd is None

    def test_agent_started_without_close_is_running(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        start_ts = 1000.0
        _write_jsonl(
            path,
            [
                _workflow_started_event(["researcher"]),
                _event("agent_started", {"agent_name": "researcher"}, ts=start_ts),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        detail = derive_run_detail(record)

        researcher = detail.agents[0]
        assert researcher.status == "running"
        assert researcher.elapsed_seconds(now=start_ts + 30.0) == 30.0
        assert detail.current_step == "researcher"

    def test_agent_completed_reports_elapsed_tokens_cost(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _workflow_started_event(["researcher"]),
                _event("agent_started", {"agent_name": "researcher"}, ts=1000.0),
                _event(
                    "agent_completed",
                    {
                        "agent_name": "researcher",
                        "elapsed": 12.5,
                        "tokens": 200,
                        "cost_usd": 0.03,
                    },
                    ts=1012.5,
                ),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        detail = derive_run_detail(record)

        researcher = detail.agents[0]
        assert researcher.status == "completed"
        assert researcher.elapsed_seconds() == 12.5
        assert researcher.tokens == 200
        assert researcher.cost_usd == 0.03
        assert detail.current_step is None

    def test_agent_failed_reports_failed_status(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _workflow_started_event(["reviewer"]),
                _event("agent_started", {"agent_name": "reviewer"}, ts=1000.0),
                _event(
                    "agent_failed",
                    {"agent_name": "reviewer", "elapsed": 3.0, "error_type": "ValueError"},
                    ts=1003.0,
                ),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        detail = derive_run_detail(record)

        reviewer = detail.agents[0]
        assert reviewer.status == "failed"
        assert reviewer.elapsed_seconds() == 3.0
        assert reviewer.tokens is None
        assert reviewer.cost_usd is None

    def test_parallel_agent_started_and_completed_reports_usage(self, tmp_path: Path) -> None:
        """Production-shaped sequence: parallel-group members never get a
        plain agent_started/agent_completed -- only parallel_agent_started/
        parallel_agent_completed (engine/workflow.py:5138,5181)."""
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _workflow_started_event(["fanout-agent"]),
                _event("parallel_started", {"group_name": "fanout"}, ts=999.0),
                _event(
                    "parallel_agent_started",
                    {"group_name": "fanout", "agent_name": "fanout-agent"},
                    ts=1000.0,
                ),
                _event(
                    "parallel_agent_completed",
                    {
                        "group_name": "fanout",
                        "agent_name": "fanout-agent",
                        "elapsed": 5.0,
                        "tokens": 150,
                        "cost_usd": 0.02,
                    },
                    ts=1005.0,
                ),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        detail = derive_run_detail(record)

        agent = detail.agents[0]
        assert agent.status == "completed"
        assert agent.elapsed_seconds() == 5.0
        assert agent.tokens == 150
        assert agent.cost_usd == 0.02

    def test_parallel_agent_started_without_close_is_running(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _workflow_started_event(["fanout-agent"]),
                _event("parallel_started", {"group_name": "fanout"}, ts=999.0),
                _event(
                    "parallel_agent_started",
                    {"group_name": "fanout", "agent_name": "fanout-agent"},
                    ts=1000.0,
                ),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        detail = derive_run_detail(record)

        agent = detail.agents[0]
        assert agent.status == "running"
        assert agent.elapsed_seconds(now=1030.0) == 30.0

    def test_parallel_agent_failed_reports_failed_status(self, tmp_path: Path) -> None:
        """Production-shaped sequence: parallel-group members open via
        parallel_agent_started, not a plain agent_started."""
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _workflow_started_event(["fanout-agent"]),
                _event("parallel_started", {"group_name": "fanout"}, ts=999.0),
                _event(
                    "parallel_agent_started",
                    {"group_name": "fanout", "agent_name": "fanout-agent"},
                    ts=1000.0,
                ),
                _event(
                    "parallel_agent_failed",
                    {
                        "group_name": "fanout",
                        "agent_name": "fanout-agent",
                        "elapsed": 2.0,
                        "error_type": "RuntimeError",
                    },
                    ts=1002.0,
                ),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        detail = derive_run_detail(record)

        agent = detail.agents[0]
        assert agent.status == "failed"
        assert agent.elapsed_seconds() == 2.0

    def test_subworkflow_completed_closes_the_step(self, tmp_path: Path) -> None:
        """A `type: workflow` step closes via subworkflow_completed, not
        agent_completed (engine/workflow.py:3969)."""
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _workflow_started_event(["subflow"]),
                _event(
                    "agent_started", {"agent_name": "subflow", "agent_type": "workflow"}, ts=1000.0
                ),
                _event(
                    "subworkflow_started",
                    {"agent_name": "subflow", "iteration": 1, "workflow": "child.yaml"},
                    ts=1000.0,
                ),
                _event(
                    "subworkflow_completed",
                    {"agent_name": "subflow", "elapsed": 8.0, "output": {}},
                    ts=1008.0,
                ),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        detail = derive_run_detail(record)

        subflow = detail.agents[0]
        assert subflow.status == "completed"
        assert subflow.elapsed_seconds() == 8.0
        assert detail.current_step is None

    def test_script_failed_marks_step_failed(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _workflow_started_event(["build"]),
                _event("agent_started", {"agent_name": "build", "agent_type": "script"}, ts=1000.0),
                _event("script_started", {"agent_name": "build"}, ts=1000.0),
                _event(
                    "script_failed",
                    {"agent_name": "build", "elapsed": 1.0, "error_type": "RuntimeError"},
                    ts=1001.0,
                ),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        detail = derive_run_detail(record)

        build = detail.agents[0]
        assert build.status == "failed"
        assert build.elapsed_seconds() == 1.0
        assert detail.current_step is None

    def test_wait_failed_marks_step_failed(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _workflow_started_event(["pause"]),
                _event("agent_started", {"agent_name": "pause", "agent_type": "wait"}, ts=1000.0),
                _event(
                    "wait_failed",
                    {"agent_name": "pause", "elapsed": 0.5, "error_type": "ValueError"},
                    ts=1000.5,
                ),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        detail = derive_run_detail(record)

        pause = detail.agents[0]
        assert pause.status == "failed"

    def test_set_failed_marks_step_failed(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _workflow_started_event(["assign"]),
                _event("agent_started", {"agent_name": "assign", "agent_type": "set"}, ts=1000.0),
                _event(
                    "set_failed",
                    {"agent_name": "assign", "elapsed": 0.1, "error_type": "ValueError"},
                    ts=1000.1,
                ),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        detail = derive_run_detail(record)

        assign = detail.agents[0]
        assert assign.status == "failed"

    def test_subworkflow_failed_marks_step_failed(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _workflow_started_event(["subflow"]),
                _event(
                    "agent_started", {"agent_name": "subflow", "agent_type": "workflow"}, ts=1000.0
                ),
                _event(
                    "subworkflow_started",
                    {"agent_name": "subflow", "iteration": 1, "workflow": "child.yaml"},
                    ts=1000.0,
                ),
                _event(
                    "subworkflow_failed",
                    {"agent_name": "subflow", "elapsed": 3.0, "error_type": "RuntimeError"},
                    ts=1003.0,
                ),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        detail = derive_run_detail(record)

        subflow = detail.agents[0]
        assert subflow.status == "failed"
        assert subflow.elapsed_seconds() == 3.0

    def test_workflow_failed_agent_name_marks_running_agent_failed(self, tmp_path: Path) -> None:
        """A plain agent's unhandled exception has no per-agent failure
        event -- it propagates straight to workflow_failed, which still
        carries the agent_name (engine/workflow.py:4208)."""
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _workflow_started_event(["researcher"]),
                _event("agent_started", {"agent_name": "researcher"}, ts=1000.0),
                _event(
                    "workflow_failed",
                    {"error_type": "ValueError", "message": "boom", "agent_name": "researcher"},
                    ts=1002.0,
                ),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        detail = derive_run_detail(record)

        researcher = detail.agents[0]
        assert researcher.status == "failed"
        assert detail.current_step is None

    def test_workflow_failed_preserves_prior_specific_failure_elapsed(self, tmp_path: Path) -> None:
        """Real script/wait/set/subworkflow failures emit their own specific
        *_failed event (with elapsed) before workflow_failed. workflow_failed
        must not clobber that recorded elapsed with None."""
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _workflow_started_event(["build"]),
                _event("agent_started", {"agent_name": "build", "agent_type": "script"}, ts=1000.0),
                _event(
                    "script_failed",
                    {"agent_name": "build", "elapsed": 4.5, "error_type": "RuntimeError"},
                    ts=1004.5,
                ),
                _event(
                    "workflow_failed",
                    {"error_type": "RuntimeError", "message": "boom", "agent_name": "build"},
                    ts=1004.6,
                ),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        detail = derive_run_detail(record)

        build = detail.agents[0]
        assert build.status == "failed"
        assert build.elapsed_seconds() == 4.5

    def test_restarted_agent_sums_cumulative_tokens_and_cost(self, tmp_path: Path) -> None:
        """A loop-back restart's later completion adds to, not overwrites,
        an earlier attempt's usage."""
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _workflow_started_event(["reviewer"]),
                _event("agent_started", {"agent_name": "reviewer"}, ts=1000.0),
                _event(
                    "agent_completed",
                    {"agent_name": "reviewer", "elapsed": 5.0, "tokens": 10, "cost_usd": 0.001},
                    ts=1005.0,
                ),
                _event("agent_started", {"agent_name": "reviewer"}, ts=1010.0),
                _event(
                    "agent_completed",
                    {"agent_name": "reviewer", "elapsed": 4.0, "tokens": 20, "cost_usd": 0.002},
                    ts=1014.0,
                ),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        detail = derive_run_detail(record)

        reviewer = detail.agents[0]
        assert reviewer.status == "completed"
        assert reviewer.tokens == 30
        assert reviewer.cost_usd == 0.003

    def test_nested_subworkflow_agent_does_not_corrupt_root_row(self, tmp_path: Path) -> None:
        """A nested agent sharing a root agent's name (subworkflow_path
        stamped, engine/workflow.py:582) must not corrupt the root row."""
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _workflow_started_event(["researcher"]),
                _event("agent_started", {"agent_name": "researcher"}, ts=1000.0),
                # A nested sub-workflow happens to reuse the name
                # "researcher" for one of its own inner agents.
                _event(
                    "agent_started",
                    {"agent_name": "researcher", "subworkflow_path": ["subflow"]},
                    ts=1001.0,
                ),
                _event(
                    "agent_completed",
                    {
                        "agent_name": "researcher",
                        "elapsed": 1.0,
                        "tokens": 999,
                        "cost_usd": 9.99,
                        "subworkflow_path": ["subflow"],
                    },
                    ts=1002.0,
                ),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        detail = derive_run_detail(record)

        researcher = detail.agents[0]
        # Still "running" (from the root's own, unstamped agent_started) --
        # the nested completion must not close it or contribute usage.
        assert researcher.status == "running"
        assert researcher.tokens is None
        assert researcher.cost_usd is None
        assert detail.current_step == "researcher"

    def test_non_llm_step_completes_via_its_own_event_not_agent_completed(
        self, tmp_path: Path
    ) -> None:
        """A script step's agent_started never gets agent_completed --
        script_completed must still mark it "completed", matching the list
        screen's current-step tracking (E6-T3)."""
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _workflow_started_event(["build"]),
                _event("agent_started", {"agent_name": "build", "agent_type": "script"}, ts=1000.0),
                _event("script_started", {"agent_name": "build"}, ts=1000.0),
                _event("script_completed", {"agent_name": "build", "exit_code": 0}, ts=1001.0),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        detail = derive_run_detail(record)

        build = detail.agents[0]
        assert build.status == "completed"
        assert detail.current_step is None

    def test_restarted_agent_reflects_latest_attempt(self, tmp_path: Path) -> None:
        """An agent that completed once, then restarted (e.g. a route
        loop-back) and is now running again must show "running", not a
        stale "completed" from its first pass."""
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _workflow_started_event(["reviewer"]),
                _event("agent_started", {"agent_name": "reviewer"}, ts=1000.0),
                _event(
                    "agent_completed",
                    {"agent_name": "reviewer", "elapsed": 5.0, "tokens": 10, "cost_usd": 0.001},
                    ts=1005.0,
                ),
                _event("agent_started", {"agent_name": "reviewer"}, ts=1010.0),
            ],
        )
        record = _make_record(tmp_path, event_log_path=str(path))

        detail = derive_run_detail(record)

        reviewer = detail.agents[0]
        assert reviewer.status == "running"
        assert reviewer.elapsed_seconds(now=1010.0 + 4.0) == 4.0


class TestDeriveRunDetailGracefulDegradation:
    def test_missing_event_log_path_yields_empty_detail(self, tmp_path: Path) -> None:
        record = _make_record(tmp_path, event_log_path="")

        detail = derive_run_detail(record)

        assert detail.topology is None
        assert detail.agents == []
        assert detail.current_step is None

    def test_nonexistent_log_file_yields_empty_detail(self, tmp_path: Path) -> None:
        record = _make_record(
            tmp_path, event_log_path=str(tmp_path / "does-not-exist.events.jsonl")
        )

        detail = derive_run_detail(record)

        assert detail.topology is None
        assert detail.agents == []

    def test_no_workflow_started_yet_yields_empty_detail(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        path.write_text("")
        record = _make_record(tmp_path, event_log_path=str(path))

        detail = derive_run_detail(record)

        assert detail.topology is None
        assert detail.agents == []

    def test_identity_fields_pass_through(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        path.write_text("")
        record = _make_record(
            tmp_path, run_id="run-detail-1", workflow_name="detail-flow", event_log_path=str(path)
        )

        detail = derive_run_detail(record)

        assert detail.run_id == "run-detail-1"
        assert detail.workflow_name == "detail-flow"


class TestStaleGateClosing:
    """A gate must not latch on forever when no ``gate_resolved`` arrives.

    A ``questions`` node emitted only ``gate_presented`` until the engine was
    fixed to pair the two, and every log written before that fix still has
    the unpaired events in it -- so the summary has to close a gate the run
    has visibly moved past, not just one it was told about.
    """

    def test_gate_closes_when_a_later_step_starts(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _event("workflow_started", {"workflow_name": "wf"}),
                _event("agent_started", {"agent_name": "ask_questions"}),
                _event("gate_presented", {"agent_name": "ask_questions", "prompt": "Q?"}),
                # No gate_resolved -- the run simply moves on.
                _event("agent_started", {"agent_name": "planner"}),
            ],
        )
        summary = derive_run_summary(_make_record(tmp_path))
        assert summary.gate is None
        assert summary.status == "running"

    def test_gate_closes_when_its_own_agent_completes(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _event("workflow_started", {"workflow_name": "wf"}),
                _event("agent_started", {"agent_name": "ask_questions"}),
                _event("gate_presented", {"agent_name": "ask_questions", "prompt": "Q?"}),
                _event("agent_completed", {"agent_name": "ask_questions", "tokens": 10}),
            ],
        )
        summary = derive_run_summary(_make_record(tmp_path))
        assert summary.gate is None
        assert summary.status == "running"

    def test_an_open_gate_still_reads_as_open(self, tmp_path: Path) -> None:
        """The closing rules must not swallow a gate that is genuinely open --
        the presenting agent stays started while it waits."""
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _event("workflow_started", {"workflow_name": "wf"}),
                _event("agent_started", {"agent_name": "ask_questions"}),
                _event("gate_presented", {"agent_name": "ask_questions", "prompt": "Q?"}),
            ],
        )
        summary = derive_run_summary(_make_record(tmp_path))
        assert summary.gate is not None
        assert summary.gate.agent_name == "ask_questions"
        assert summary.status == "at-gate"

    def test_repeated_prompts_from_one_node_stay_open(self, tmp_path: Path) -> None:
        """A questions node presents repeatedly under one name; each new
        prompt replaces the last rather than closing the gate."""
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _event("workflow_started", {"workflow_name": "wf"}),
                _event("agent_started", {"agent_name": "ask_questions"}),
                _event("gate_presented", {"agent_name": "ask_questions", "prompt": "Q1?"}),
                _event("gate_presented", {"agent_name": "ask_questions", "prompt": "Q2?"}),
            ],
        )
        summary = derive_run_summary(_make_record(tmp_path))
        assert summary.gate is not None
        assert summary.gate.prompt == "Q2?"
        assert summary.status == "at-gate"

    def test_explicit_gate_resolved_still_closes(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _event("workflow_started", {"workflow_name": "wf"}),
                _event("agent_started", {"agent_name": "plan_approval"}),
                _event("gate_presented", {"agent_name": "plan_approval", "prompt": "OK?"}),
                _event("gate_resolved", {"agent_name": "plan_approval", "selected_option": "yes"}),
            ],
        )
        summary = derive_run_summary(_make_record(tmp_path))
        assert summary.gate is None
        assert summary.status == "running"


class TestQuestionsNodeCloses:
    """A `questions` node closes with `questions_completed`, not
    `agent_completed` -- so it showed as "running" for the rest of the run,
    visibly wrong once the workflow had moved on to the next step."""

    def test_questions_completed_closes_the_step(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _event(
                    "workflow_started",
                    {"workflow_name": "wf", "agents": [{"name": "ask", "type": "questions"}]},
                ),
                _event("agent_started", {"agent_name": "ask"}),
                _event("questions_completed", {"agent_name": "ask", "outcome": "completed"}),
            ],
        )
        detail = derive_run_detail(_make_record(tmp_path))
        statuses = {a.name: a.status for a in detail.agents}
        assert statuses.get("ask") == "completed"

    def test_is_at_gate_before_it_completes(self, tmp_path: Path) -> None:
        """An unanswered question is *waiting on a person*, which is a
        sharper statement than "running" and the only one the run-detail
        screen makes now that it no longer repeats the gate prompt."""
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _event(
                    "workflow_started",
                    {"workflow_name": "wf", "agents": [{"name": "ask", "type": "questions"}]},
                ),
                _event("agent_started", {"agent_name": "ask"}),
                _event("gate_presented", {"agent_name": "ask", "prompt": "Q?"}),
            ],
        )
        detail = derive_run_detail(_make_record(tmp_path))
        statuses = {a.name: a.status for a in detail.agents}
        assert statuses.get("ask") == "at-gate"

    def test_it_also_closes_the_current_step(self, tmp_path: Path) -> None:
        """The Runs screen's current-step tracking reads the same set."""
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(
            path,
            [
                _event("workflow_started", {"workflow_name": "wf"}),
                _event("agent_started", {"agent_name": "ask"}),
                _event("questions_completed", {"agent_name": "ask"}),
            ],
        )
        summary = derive_run_summary(_make_record(tmp_path))
        assert summary.current_step is None


class TestDeclaredWorkflowName:
    """A repo that stores each workflow as `<name>/workflow.yaml` made every
    run show up as "workflow" on the Runs screen, while History (which reads
    the log filename) showed the real name."""

    def test_declared_name_wins_over_the_file_stem(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(path, [_event("workflow_started", {"name": "ship"})])
        summary = derive_run_summary(_make_record(tmp_path, workflow_name="workflow"))
        assert summary.workflow_name == "ship"

    def test_falls_back_to_the_record_when_undeclared(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        _write_jsonl(path, [_event("agent_started", {"agent_name": "a"})])
        summary = derive_run_summary(_make_record(tmp_path, workflow_name="from-record"))
        assert summary.workflow_name == "from-record"


class TestTopologySurvivesALongLog:
    """`workflow_started` is the log's first event, so on any run long enough
    to outgrow the tail window it is the one event guaranteed to be outside
    it -- the step list disappeared exactly when a run got interesting."""

    def test_topology_read_from_the_head_when_the_tail_misses_it(self, tmp_path: Path) -> None:
        path = tmp_path / "run.events.jsonl"
        lines = [
            _event(
                "workflow_started",
                {"workflow_name": "wf", "agents": [{"name": "first", "type": "agent"}]},
            )
        ]
        # Push it well outside a deliberately tiny tail window.
        lines += [
            _event("agent_message", {"agent_name": "a", "text": "x" * 200}) for _ in range(80)
        ]
        _write_jsonl(path, lines)

        summary = derive_run_summary(_make_record(tmp_path), tail_bytes=2048)
        assert summary.topology is not None
        assert [a.name for a in summary.topology.agents] == ["first"]
