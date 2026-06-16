"""Unit tests for CheckpointManager.

Tests cover:
- save/load round-trip
- file format validation (version, required fields)
- hash computation
- find_latest_checkpoint with multiple files
- list_checkpoints with filtering
- cleanup idempotent
- atomic write (no partial files on error)
- file permissions (0o600)
- non-serializable value handling via _make_json_serializable
- save_checkpoint doesn't raise on failure
"""

from __future__ import annotations

import json
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from unittest.mock import patch

import pytest

from conductor.engine.checkpoint import (
    CheckpointData,
    CheckpointManager,
    _make_json_serializable,
)
from conductor.engine.context import WorkflowContext
from conductor.engine.limits import LimitEnforcer
from conductor.exceptions import CheckpointError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(
    inputs: dict[str, Any] | None = None,
    agents: dict[str, dict[str, Any]] | None = None,
) -> WorkflowContext:
    """Build a WorkflowContext with optional inputs and agent outputs."""
    ctx = WorkflowContext()
    if inputs:
        ctx.set_workflow_inputs(inputs)
    if agents:
        for name, output in agents.items():
            ctx.store(name, output)
    return ctx


def _make_limits(
    iterations: int = 0,
    max_iter: int = 10,
    history: list[str] | None = None,
) -> LimitEnforcer:
    """Build a LimitEnforcer with iteration state."""
    enforcer = LimitEnforcer(max_iterations=max_iter, timeout_seconds=300)
    enforcer.start()
    enforcer.current_iteration = iterations
    enforcer.execution_history = list(history or [])
    return enforcer


def _write_workflow(tmp_path: Path, content: str = "name: test-workflow\n") -> Path:
    """Write a dummy workflow YAML and return its path."""
    wf = tmp_path / "workflow.yaml"
    wf.write_text(content)
    return wf


# ---------------------------------------------------------------------------
# _make_json_serializable tests
# ---------------------------------------------------------------------------


class TestMakeJsonSerializable:
    """Tests for the _make_json_serializable helper."""

    def test_primitives_unchanged(self) -> None:
        assert _make_json_serializable(None) is None
        assert _make_json_serializable(True) is True
        assert _make_json_serializable(42) == 42
        assert _make_json_serializable(3.14) == 3.14
        assert _make_json_serializable("hello") == "hello"

    def test_bytes_utf8(self) -> None:
        assert _make_json_serializable(b"hello") == "hello"

    def test_bytes_non_utf8(self) -> None:
        result = _make_json_serializable(b"\xff\xfe")
        assert result.startswith("<bytes len=")

    def test_datetime(self) -> None:
        dt = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        result = _make_json_serializable(dt)
        assert "2026-01-15" in result

    def test_path(self) -> None:
        p = Path("/tmp/test.yaml")
        assert _make_json_serializable(p) == str(p)

    def test_dict_recursive(self) -> None:
        d = {"path": PurePosixPath("/a"), "nested": {"b": b"data"}}
        result = _make_json_serializable(d)
        assert result["path"] == "/a"
        assert result["nested"]["b"] == "data"

    def test_list_recursive(self) -> None:
        result = _make_json_serializable([PurePosixPath("/a"), 42, [b"x"]])
        assert result == ["/a", 42, ["x"]]

    def test_set_converted_to_sorted_list(self) -> None:
        result = _make_json_serializable({"b", "a", "c"})
        assert result == ["a", "b", "c"]

    def test_custom_object_to_str(self) -> None:
        class Foo:
            def __str__(self) -> str:
                return "foo-repr"

        result = _make_json_serializable(Foo())
        assert result == "foo-repr"

    def test_entire_result_is_json_serializable(self) -> None:
        data = {
            "path": Path("/tmp/x"),
            "when": datetime.now(UTC),
            "raw": b"\x00\x01",
            "items": [1, "two", None],
        }
        result = _make_json_serializable(data)
        # Should not raise
        json.dumps(result)


# ---------------------------------------------------------------------------
# CheckpointManager.compute_workflow_hash tests
# ---------------------------------------------------------------------------


class TestComputeWorkflowHash:
    def test_returns_sha256_prefixed_hash(self, tmp_path: Path) -> None:
        wf = _write_workflow(tmp_path, "name: demo\n")
        h = CheckpointManager.compute_workflow_hash(wf)
        assert h.startswith("sha256:")
        assert len(h.split(":")[1]) == 64

    def test_deterministic(self, tmp_path: Path) -> None:
        wf = _write_workflow(tmp_path, "name: stable\n")
        h1 = CheckpointManager.compute_workflow_hash(wf)
        h2 = CheckpointManager.compute_workflow_hash(wf)
        assert h1 == h2

    def test_changes_with_content(self, tmp_path: Path) -> None:
        wf = _write_workflow(tmp_path, "v1")
        h1 = CheckpointManager.compute_workflow_hash(wf)
        wf.write_text("v2")
        h2 = CheckpointManager.compute_workflow_hash(wf)
        assert h1 != h2


# ---------------------------------------------------------------------------
# CheckpointManager.get_checkpoints_dir tests
# ---------------------------------------------------------------------------


class TestGetCheckpointsDir:
    def test_returns_path_under_tmpdir(self) -> None:
        d = CheckpointManager.get_checkpoints_dir()
        assert d.parts[-2:] == ("conductor", "checkpoints")
        assert d.exists()

    def test_idempotent(self) -> None:
        d1 = CheckpointManager.get_checkpoints_dir()
        d2 = CheckpointManager.get_checkpoints_dir()
        assert d1 == d2


# ---------------------------------------------------------------------------
# CheckpointManager.save_checkpoint tests
# ---------------------------------------------------------------------------


class TestSaveCheckpoint:
    def test_creates_json_file(self, tmp_path: Path) -> None:
        wf = _write_workflow(tmp_path)
        ctx = _make_context({"q": "hi"}, {"agent_a": {"answer": "yes"}})
        limits = _make_limits(1, 10, ["agent_a"])
        error = RuntimeError("boom")

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            path = CheckpointManager.save_checkpoint(wf, ctx, limits, "agent_b", error, {"q": "hi"})

        assert path is not None
        assert path.exists()
        assert path.suffix == ".json"

        data = json.loads(path.read_text())
        assert data["version"] == 1
        assert data["current_agent"] == "agent_b"
        assert data["failure"]["error_type"] == "RuntimeError"
        assert data["failure"]["message"] == "boom"
        assert data["context"]["workflow_inputs"]["q"] == "hi"
        assert data["limits"]["current_iteration"] == 1

    def test_file_permissions(self, tmp_path: Path) -> None:
        if sys.platform == "win32":
            pytest.skip("File permissions test not applicable on Windows")

        wf = _write_workflow(tmp_path)
        ctx = _make_context()
        limits = _make_limits()
        error = RuntimeError("err")

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            path = CheckpointManager.save_checkpoint(wf, ctx, limits, "a", error, {})

        assert path is not None
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600

    def test_never_raises_on_failure(self, tmp_path: Path) -> None:
        wf = _write_workflow(tmp_path)
        ctx = _make_context()
        limits = _make_limits()
        error = RuntimeError("err")

        # Point to a non-existent directory that cannot be created
        fake_dir = tmp_path / "no" / "such" / "dir"
        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=fake_dir):
            result = CheckpointManager.save_checkpoint(wf, ctx, limits, "a", error, {})

        assert result is None

    def test_handles_non_serializable_inputs(self, tmp_path: Path) -> None:
        wf = _write_workflow(tmp_path)
        ctx = _make_context()
        limits = _make_limits()
        error = RuntimeError("err")

        inputs_with_path: dict[str, Any] = {"file": PurePosixPath("/tmp/x"), "data": b"bytes"}

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            path = CheckpointManager.save_checkpoint(wf, ctx, limits, "a", error, inputs_with_path)

        assert path is not None
        data = json.loads(path.read_text())
        assert data["inputs"]["file"] == "/tmp/x"
        assert data["inputs"]["data"] == "bytes"

    def test_copilot_session_ids_included(self, tmp_path: Path) -> None:
        wf = _write_workflow(tmp_path)
        ctx = _make_context()
        limits = _make_limits()
        error = RuntimeError("err")

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            path = CheckpointManager.save_checkpoint(
                wf,
                ctx,
                limits,
                "a",
                error,
                {},
                copilot_session_ids={"agent_a": "sid-123"},
            )

        assert path is not None
        data = json.loads(path.read_text())
        assert data["copilot_session_ids"] == {"agent_a": "sid-123"}

    def test_run_id_and_event_log_path_included(self, tmp_path: Path) -> None:
        """``run_id`` and ``event_log_path`` round-trip through save+load.

        Regression coverage for issue #167: the resume web dashboard needs
        these fields to replay the original timeline.
        """
        wf = _write_workflow(tmp_path)
        ctx = _make_context()
        limits = _make_limits()
        error = RuntimeError("err")
        log_path = tmp_path / "conductor-x.events.jsonl"
        log_path.write_text("")

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            saved_path = CheckpointManager.save_checkpoint(
                wf,
                ctx,
                limits,
                "a",
                error,
                {},
                run_id="abc12345",
                event_log_path=str(log_path),
            )

        assert saved_path is not None
        data = json.loads(saved_path.read_text())
        assert data["run_id"] == "abc12345"
        assert data["event_log_path"] == str(log_path)

        cp = CheckpointManager.load_checkpoint(saved_path)
        assert cp.run_id == "abc12345"
        assert cp.event_log_path == str(log_path)

    def test_run_id_and_event_log_path_default_empty(self, tmp_path: Path) -> None:
        """Defaults are empty strings when not supplied (backward compat)."""
        wf = _write_workflow(tmp_path)
        ctx = _make_context()
        limits = _make_limits()
        error = RuntimeError("err")

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            saved_path = CheckpointManager.save_checkpoint(wf, ctx, limits, "a", error, {})

        assert saved_path is not None
        cp = CheckpointManager.load_checkpoint(saved_path)
        assert cp.run_id == ""
        assert cp.event_log_path == ""

    def test_no_leftover_tmp_file(self, tmp_path: Path) -> None:
        """After a successful save, no .tmp file should remain."""
        wf = _write_workflow(tmp_path)
        ctx = _make_context()
        limits = _make_limits()
        error = RuntimeError("err")

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            CheckpointManager.save_checkpoint(wf, ctx, limits, "a", error, {})

        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []


# ---------------------------------------------------------------------------
# CheckpointManager.load_checkpoint tests
# ---------------------------------------------------------------------------


class TestLoadCheckpoint:
    def test_load_valid_checkpoint(self, tmp_path: Path) -> None:
        wf = _write_workflow(tmp_path)
        ctx = _make_context({"q": "hi"}, {"planner": {"plan": "go"}})
        limits = _make_limits(1, 15, ["planner"])
        error = ValueError("bad value")

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            saved_path = CheckpointManager.save_checkpoint(
                wf, ctx, limits, "synthesizer", error, {"q": "hi"}
            )

        assert saved_path is not None
        cp = CheckpointManager.load_checkpoint(saved_path)

        assert isinstance(cp, CheckpointData)
        assert cp.version == 1
        assert cp.current_agent == "synthesizer"
        assert cp.failure["error_type"] == "ValueError"
        assert cp.context["agent_outputs"]["planner"]["plan"] == "go"
        assert cp.limits["current_iteration"] == 1
        assert cp.file_path == saved_path

    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(CheckpointError, match="not found"):
            CheckpointManager.load_checkpoint(tmp_path / "missing.json")

    def test_invalid_json(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("not json {{{")

        with pytest.raises(CheckpointError, match="Invalid JSON"):
            CheckpointManager.load_checkpoint(bad)

    def test_missing_version(self, tmp_path: Path) -> None:
        f = tmp_path / "no-version.json"
        f.write_text(json.dumps({"workflow_path": "/x"}))

        with pytest.raises(CheckpointError, match="missing 'version'"):
            CheckpointManager.load_checkpoint(f)

    def test_unsupported_version(self, tmp_path: Path) -> None:
        f = tmp_path / "v99.json"
        f.write_text(json.dumps({"version": 99}))

        with pytest.raises(CheckpointError, match="Unsupported checkpoint version"):
            CheckpointManager.load_checkpoint(f)

    def test_missing_required_field(self, tmp_path: Path) -> None:
        f = tmp_path / "incomplete.json"
        f.write_text(json.dumps({"version": 1, "workflow_path": "/x"}))

        with pytest.raises(CheckpointError, match="missing required field"):
            CheckpointManager.load_checkpoint(f)

    def test_loads_legacy_checkpoint_without_run_id_or_event_log_path(self, tmp_path: Path) -> None:
        """Old checkpoints written before this PR have neither field.

        Backward compatibility: ``load_checkpoint`` must default both
        fields to ``""`` without raising.
        """
        legacy = tmp_path / "legacy.json"
        legacy.write_text(
            json.dumps(
                {
                    "version": 1,
                    "workflow_path": "/x.yaml",
                    "workflow_hash": "sha256:abc",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "failure": {"error_type": "X", "message": "m", "agent": "a", "iteration": 0},
                    "inputs": {},
                    "current_agent": "a",
                    "context": {"workflow_inputs": {}, "agent_outputs": {}},
                    "limits": {"current_iteration": 0, "max_iterations": 10},
                    "copilot_session_ids": {},
                    # No run_id, no event_log_path — pre-PR shape.
                }
            )
        )

        cp = CheckpointManager.load_checkpoint(legacy)
        assert cp.run_id == ""
        assert cp.event_log_path == ""


# ---------------------------------------------------------------------------
# CheckpointManager.find_latest_checkpoint tests
# ---------------------------------------------------------------------------


class TestFindLatestCheckpoint:
    def test_no_checkpoints(self, tmp_path: Path) -> None:
        wf = _write_workflow(tmp_path)
        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            assert CheckpointManager.find_latest_checkpoint(wf) is None

    def test_single_checkpoint(self, tmp_path: Path) -> None:
        wf = _write_workflow(tmp_path)
        ctx = _make_context()
        limits = _make_limits()
        error = RuntimeError("err")

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            saved = CheckpointManager.save_checkpoint(wf, ctx, limits, "a", error, {})
            latest = CheckpointManager.find_latest_checkpoint(wf)

        assert latest == saved

    def test_returns_most_recent(self, tmp_path: Path) -> None:
        wf = _write_workflow(tmp_path)
        ctx = _make_context()
        limits = _make_limits()

        # Create two checkpoints with distinct timestamps in filenames
        cp1 = tmp_path / "workflow-20260101-120000.json"
        cp2 = tmp_path / "workflow-20260201-120000.json"

        checkpoint_data = {
            "version": 1,
            "workflow_path": str(wf),
            "workflow_hash": "sha256:abc",
            "created_at": "2026-01-01T12:00:00Z",
            "failure": {"error_type": "E", "message": "m", "agent": "a", "iteration": 0},
            "current_agent": "a",
            "context": ctx.to_dict(),
            "limits": limits.to_dict(),
            "inputs": {},
            "copilot_session_ids": {},
        }

        cp1.write_text(json.dumps(checkpoint_data))
        checkpoint_data["created_at"] = "2026-02-01T12:00:00Z"
        cp2.write_text(json.dumps(checkpoint_data))

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            latest = CheckpointManager.find_latest_checkpoint(wf)

        assert latest == cp2

    def test_ignores_other_workflow_checkpoints(self, tmp_path: Path) -> None:
        wf = _write_workflow(tmp_path)
        # Create a checkpoint for a different workflow
        other = tmp_path / "other-20260101-120000.json"
        other.write_text(json.dumps({"version": 1}))

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            assert CheckpointManager.find_latest_checkpoint(wf) is None


# ---------------------------------------------------------------------------
# CheckpointManager.list_checkpoints tests
# ---------------------------------------------------------------------------


class TestListCheckpoints:
    def _create_checkpoint_file(
        self,
        directory: Path,
        name: str,
        created_at: str,
        workflow_path: str = "/wf.yaml",
    ) -> Path:
        """Write a valid checkpoint JSON file."""
        data = {
            "version": 1,
            "workflow_path": workflow_path,
            "workflow_hash": "sha256:abc",
            "created_at": created_at,
            "failure": {"error_type": "E", "message": "m", "agent": "a", "iteration": 0},
            "current_agent": "a",
            "context": {
                "workflow_inputs": {},
                "agent_outputs": {},
                "current_iteration": 0,
                "execution_history": [],
            },
            "limits": {"current_iteration": 0, "max_iterations": 10, "execution_history": []},
            "inputs": {},
            "copilot_session_ids": {},
        }
        f = directory / name
        f.write_text(json.dumps(data))
        return f

    def test_empty_dir(self, tmp_path: Path) -> None:
        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            result = CheckpointManager.list_checkpoints()
        assert result == []

    def test_returns_sorted_descending(self, tmp_path: Path) -> None:
        self._create_checkpoint_file(tmp_path, "wf-20260101-100000.json", "2026-01-01T10:00:00Z")
        self._create_checkpoint_file(tmp_path, "wf-20260301-100000.json", "2026-03-01T10:00:00Z")
        self._create_checkpoint_file(tmp_path, "wf-20260201-100000.json", "2026-02-01T10:00:00Z")

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            result = CheckpointManager.list_checkpoints()

        assert len(result) == 3
        assert result[0].created_at == "2026-03-01T10:00:00Z"
        assert result[1].created_at == "2026-02-01T10:00:00Z"
        assert result[2].created_at == "2026-01-01T10:00:00Z"

    def test_filter_by_workflow(self, tmp_path: Path) -> None:
        self._create_checkpoint_file(tmp_path, "alpha-20260101-100000.json", "2026-01-01T10:00:00Z")
        self._create_checkpoint_file(tmp_path, "beta-20260101-100000.json", "2026-01-01T10:00:00Z")
        self._create_checkpoint_file(tmp_path, "alpha-20260201-100000.json", "2026-02-01T10:00:00Z")

        wf = tmp_path / "alpha.yaml"
        wf.write_text("name: alpha\n")

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            result = CheckpointManager.list_checkpoints(wf)

        assert len(result) == 2
        for cp in result:
            assert cp.file_path.name.startswith("alpha-")

    def test_skips_invalid_files(self, tmp_path: Path) -> None:
        self._create_checkpoint_file(tmp_path, "wf-20260101-100000.json", "2026-01-01T10:00:00Z")
        bad = tmp_path / "wf-20260201-100000.json"
        bad.write_text("not valid json")

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            result = CheckpointManager.list_checkpoints()

        assert len(result) == 1


# ---------------------------------------------------------------------------
# CheckpointManager.cleanup tests
# ---------------------------------------------------------------------------


class TestCleanup:
    def test_deletes_file(self, tmp_path: Path) -> None:
        f = tmp_path / "checkpoint.json"
        f.write_text("{}")

        CheckpointManager.cleanup(f)
        assert not f.exists()

    def test_idempotent(self, tmp_path: Path) -> None:
        f = tmp_path / "checkpoint.json"
        # File does not exist — should not raise
        CheckpointManager.cleanup(f)


# ---------------------------------------------------------------------------
# Round-trip tests
# ---------------------------------------------------------------------------


class TestSaveLoadRoundTrip:
    def test_basic_round_trip(self, tmp_path: Path) -> None:
        wf = _write_workflow(tmp_path, "name: my-wf\nagents: []\n")
        ctx = _make_context(
            {"topic": "AI"},
            {"planner": {"plan": "research"}, "researcher": {"findings": ["a", "b"]}},
        )
        limits = _make_limits(2, 20, ["planner", "researcher"])
        error = RuntimeError("network error")

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            saved = CheckpointManager.save_checkpoint(
                wf, ctx, limits, "synthesizer", error, {"topic": "AI"}
            )

        assert saved is not None
        cp = CheckpointManager.load_checkpoint(saved)

        assert cp.version == 1
        assert cp.current_agent == "synthesizer"
        assert cp.inputs == {"topic": "AI"}
        assert cp.context["agent_outputs"]["planner"]["plan"] == "research"
        assert cp.limits["current_iteration"] == 2
        assert cp.limits["max_iterations"] == 20
        assert cp.failure["error_type"] == "RuntimeError"

    def test_context_reconstructable(self, tmp_path: Path) -> None:
        """Saved context can be reconstructed via WorkflowContext.from_dict."""
        wf = _write_workflow(tmp_path)
        ctx = _make_context({"q": "hi"}, {"a": {"x": 1}})
        limits = _make_limits(1, 10, ["a"])
        error = RuntimeError("err")

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            saved = CheckpointManager.save_checkpoint(wf, ctx, limits, "b", error, {"q": "hi"})

        assert saved is not None
        cp = CheckpointManager.load_checkpoint(saved)

        restored_ctx = WorkflowContext.from_dict(cp.context)
        assert restored_ctx.workflow_inputs == {"q": "hi"}
        assert restored_ctx.agent_outputs["a"]["x"] == 1

    def test_limits_reconstructable(self, tmp_path: Path) -> None:
        """Saved limits can be reconstructed via LimitEnforcer.from_dict."""
        wf = _write_workflow(tmp_path)
        ctx = _make_context()
        limits = _make_limits(5, 25, ["a", "b", "c", "d", "e"])
        error = RuntimeError("err")

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            saved = CheckpointManager.save_checkpoint(wf, ctx, limits, "f", error, {})

        assert saved is not None
        cp = CheckpointManager.load_checkpoint(saved)

        restored = LimitEnforcer.from_dict(cp.limits, timeout_seconds=120)
        assert restored.current_iteration == 5
        assert restored.max_iterations == 25
        assert restored.execution_history == ["a", "b", "c", "d", "e"]

    def test_parallel_group_output_round_trip(self, tmp_path: Path) -> None:
        """Parallel group outputs survive checkpoint round-trip."""
        wf = _write_workflow(tmp_path)
        ctx = _make_context()
        ctx.store(
            "parallel_group",
            {
                "type": "parallel",
                "outputs": {"r1": {"data": "x"}, "r2": {"data": "y"}},
                "errors": {},
            },
        )
        limits = _make_limits(2, 10, ["parallel_group"])
        error = RuntimeError("err")

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            saved = CheckpointManager.save_checkpoint(wf, ctx, limits, "next", error, {})

        assert saved is not None
        cp = CheckpointManager.load_checkpoint(saved)

        restored_ctx = WorkflowContext.from_dict(cp.context)
        assert restored_ctx.agent_outputs["parallel_group"]["type"] == "parallel"
        assert restored_ctx.agent_outputs["parallel_group"]["outputs"]["r1"]["data"] == "x"

    def test_workflow_hash_matches(self, tmp_path: Path) -> None:
        """Workflow hash in checkpoint matches direct computation."""
        wf = _write_workflow(tmp_path, "name: hashtest\n")
        ctx = _make_context()
        limits = _make_limits()
        error = RuntimeError("err")

        expected_hash = CheckpointManager.compute_workflow_hash(wf)

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            saved = CheckpointManager.save_checkpoint(wf, ctx, limits, "a", error, {})

        assert saved is not None
        cp = CheckpointManager.load_checkpoint(saved)
        assert cp.workflow_hash == expected_hash


# ---------------------------------------------------------------------------
# Periodic checkpoint trigger tests (issue #244)
# ---------------------------------------------------------------------------


class TestPeriodicCheckpointTrigger:
    """save_checkpoint with error=None and trigger='periodic'."""

    def test_periodic_save_has_null_failure_fields(self, tmp_path: Path) -> None:
        wf = _write_workflow(tmp_path)
        ctx = _make_context({"q": "hi"}, {"a": {"x": 1}})
        limits = _make_limits(2, 10, ["a", "b"])

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            path = CheckpointManager.save_checkpoint(
                wf, ctx, limits, "c", None, {"q": "hi"}, trigger="periodic"
            )

        assert path is not None
        data = json.loads(path.read_text())
        assert data["trigger"] == "periodic"
        assert data["failure"]["error_type"] is None
        assert data["failure"]["message"] is None
        # The about-to-run agent is still recorded so resume can continue.
        assert data["failure"]["agent"] == "c"
        assert data["current_agent"] == "c"

    def test_default_trigger_is_failure(self, tmp_path: Path) -> None:
        wf = _write_workflow(tmp_path)
        ctx = _make_context()
        limits = _make_limits()
        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            path = CheckpointManager.save_checkpoint(wf, ctx, limits, "a", RuntimeError("boom"), {})
        assert path is not None
        assert json.loads(path.read_text())["trigger"] == "failure"

    def test_periodic_trigger_round_trips(self, tmp_path: Path) -> None:
        wf = _write_workflow(tmp_path)
        ctx = _make_context()
        limits = _make_limits()
        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            path = CheckpointManager.save_checkpoint(
                wf, ctx, limits, "a", None, {}, trigger="periodic"
            )
        assert path is not None
        assert CheckpointManager.load_checkpoint(path).trigger == "periodic"

    def test_load_defaults_trigger_for_legacy_checkpoint(self, tmp_path: Path) -> None:
        """Checkpoints written before the trigger field load as 'failure'."""
        wf = _write_workflow(tmp_path)
        ctx = _make_context()
        limits = _make_limits()
        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            path = CheckpointManager.save_checkpoint(wf, ctx, limits, "a", RuntimeError("x"), {})
        assert path is not None
        data = json.loads(path.read_text())
        del data["trigger"]
        path.write_text(json.dumps(data))
        assert CheckpointManager.load_checkpoint(path).trigger == "failure"


# ---------------------------------------------------------------------------
# Rotation / per-run cleanup tests (issue #244)
# ---------------------------------------------------------------------------


def _write_checkpoint_file(
    directory: Path,
    name: str,
    created_at: str,
    *,
    run_id: str,
    trigger: str,
    workflow_path: str = "/wf.yaml",
) -> Path:
    """Write a valid checkpoint JSON with controllable trigger/run_id/created_at."""
    is_failure = trigger == "failure"
    data = {
        "version": 1,
        "workflow_path": workflow_path,
        "workflow_hash": "sha256:abc",
        "created_at": created_at,
        "trigger": trigger,
        "failure": {
            "error_type": "E" if is_failure else None,
            "message": "m" if is_failure else None,
            "agent": "a",
            "iteration": 0,
        },
        "current_agent": "a",
        "context": {
            "workflow_inputs": {},
            "agent_outputs": {},
            "current_iteration": 0,
            "execution_history": [],
        },
        "limits": {"current_iteration": 0, "max_iterations": 10, "execution_history": []},
        "inputs": {},
        "copilot_session_ids": {},
        "run_id": run_id,
    }
    f = directory / name
    f.write_text(json.dumps(data))
    return f


class TestRotatePeriodicCheckpoints:
    def _seed(self, directory: Path, *, run_id: str, count: int, trigger: str = "periodic") -> None:
        for i in range(count):
            _write_checkpoint_file(
                directory,
                f"workflow-2026010{i}-100000-{i:02d}.json",
                f"2026-01-0{i}T10:00:00.{i:06d}Z",
                run_id=run_id,
                trigger=trigger,
            )

    def test_keeps_newest_keep_last(self, tmp_path: Path) -> None:
        wf = _write_workflow(tmp_path, "name: wf\n")
        self._seed(tmp_path, run_id="r1", count=5)

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            CheckpointManager.rotate_periodic_checkpoints(wf, "r1", keep_last=2)
            remaining = CheckpointManager.list_checkpoints(wf)

        assert len(remaining) == 2
        # Newest two by created_at are kept.
        kept = sorted(c.created_at for c in remaining)
        assert kept == ["2026-01-03T10:00:00.000003Z", "2026-01-04T10:00:00.000004Z"]

    def test_never_deletes_failure_checkpoints(self, tmp_path: Path) -> None:
        wf = _write_workflow(tmp_path, "name: wf\n")
        self._seed(tmp_path, run_id="r1", count=4)
        _write_checkpoint_file(
            tmp_path,
            "workflow-20260105-100000-ff.json",
            "2026-01-05T10:00:00Z",
            run_id="r1",
            trigger="failure",
        )

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            CheckpointManager.rotate_periodic_checkpoints(wf, "r1", keep_last=1)
            remaining = CheckpointManager.list_checkpoints(wf)

        triggers = sorted(c.trigger for c in remaining)
        assert triggers == ["failure", "periodic"]  # 1 periodic kept + the failure

    def test_does_not_touch_other_runs(self, tmp_path: Path) -> None:
        wf = _write_workflow(tmp_path, "name: wf\n")
        self._seed(tmp_path, run_id="r1", count=3)
        _write_checkpoint_file(
            tmp_path,
            "workflow-20260109-100000-o1.json",
            "2026-01-09T10:00:00Z",
            run_id="other",
            trigger="periodic",
        )

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            CheckpointManager.rotate_periodic_checkpoints(wf, "r1", keep_last=1)
            remaining = CheckpointManager.list_checkpoints(wf)

        run_ids = sorted(c.run_id for c in remaining)
        assert run_ids == ["other", "r1"]  # other run untouched, 1 r1 kept

    def test_keep_last_zero_is_noop(self, tmp_path: Path) -> None:
        wf = _write_workflow(tmp_path, "name: wf\n")
        self._seed(tmp_path, run_id="r1", count=3)

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            CheckpointManager.rotate_periodic_checkpoints(wf, "r1", keep_last=0)
            remaining = CheckpointManager.list_checkpoints(wf)

        assert len(remaining) == 3


class TestCleanupPeriodicForRun:
    def test_removes_all_periodic_for_run_keeps_failure(self, tmp_path: Path) -> None:
        wf = _write_workflow(tmp_path, "name: wf\n")
        for i in range(3):
            _write_checkpoint_file(
                tmp_path,
                f"workflow-2026020{i}-100000-{i:02d}.json",
                f"2026-02-0{i}T10:00:00.{i:06d}Z",
                run_id="r1",
                trigger="periodic",
            )
        _write_checkpoint_file(
            tmp_path,
            "workflow-20260205-100000-ff.json",
            "2026-02-05T10:00:00Z",
            run_id="r1",
            trigger="failure",
        )

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            CheckpointManager.cleanup_periodic_for_run(wf, "r1")
            remaining = CheckpointManager.list_checkpoints(wf)

        assert len(remaining) == 1
        assert remaining[0].trigger == "failure"

    def test_other_run_periodic_untouched(self, tmp_path: Path) -> None:
        wf = _write_workflow(tmp_path, "name: wf\n")
        _write_checkpoint_file(
            tmp_path,
            "workflow-20260201-100000-a.json",
            "2026-02-01T10:00:00Z",
            run_id="r1",
            trigger="periodic",
        )
        _write_checkpoint_file(
            tmp_path,
            "workflow-20260202-100000-b.json",
            "2026-02-02T10:00:00Z",
            run_id="r2",
            trigger="periodic",
        )

        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            CheckpointManager.cleanup_periodic_for_run(wf, "r1")
            remaining = CheckpointManager.list_checkpoints(wf)

        assert len(remaining) == 1
        assert remaining[0].run_id == "r2"
