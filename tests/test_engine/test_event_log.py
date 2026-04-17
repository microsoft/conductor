"""Tests for the JSONL event log subscriber."""

import json
import time

from conductor.engine.event_log import EventLogSubscriber
from conductor.events import WorkflowEvent


class TestEventLogSubscriber:
    """Tests for EventLogSubscriber."""

    def test_creates_file_in_temp_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        sub = EventLogSubscriber("test-workflow")
        assert sub.path.exists()
        assert sub.path.suffix == ".jsonl"
        assert "test-workflow" in sub.path.name
        sub.close()

    def test_writes_events_as_jsonl(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        sub = EventLogSubscriber("test-workflow")

        event = WorkflowEvent(
            type="agent_started",
            timestamp=time.time(),
            data={"agent_name": "researcher", "iteration": 1},
        )
        sub.on_event(event)
        sub.close()

        lines = sub.path.read_text().strip().split("\n")
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["type"] == "agent_started"
        assert parsed["data"]["agent_name"] == "researcher"

    def test_writes_multiple_events(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        sub = EventLogSubscriber("multi")

        for i in range(5):
            sub.on_event(WorkflowEvent(type=f"event_{i}", timestamp=time.time(), data={"i": i}))
        sub.close()

        lines = sub.path.read_text().strip().split("\n")
        assert len(lines) == 5
        for i, line in enumerate(lines):
            assert json.loads(line)["type"] == f"event_{i}"

    def test_handles_non_serializable_data(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        sub = EventLogSubscriber("serialization")

        from pathlib import Path

        event = WorkflowEvent(
            type="test",
            timestamp=time.time(),
            data={"path": Path("/some/path"), "raw": b"bytes-data"},
        )
        sub.on_event(event)
        sub.close()

        parsed = json.loads(sub.path.read_text().strip())
        assert parsed["data"]["path"] == "/some/path"
        assert parsed["data"]["raw"] == "bytes-data"

    def test_safe_after_close(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        sub = EventLogSubscriber("close-test")
        sub.close()

        # Should not raise
        sub.on_event(WorkflowEvent(type="late", timestamp=time.time(), data={}))
        sub.close()  # Double close should be safe

    def test_filenames_unique_for_simultaneous_starts(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        subs = [EventLogSubscriber("same-workflow") for _ in range(3)]
        paths = [s.path for s in subs]
        # All paths must be distinct even when created in rapid succession
        assert len(set(paths)) == len(paths), f"Expected unique paths, got {paths}"
        for s in subs:
            s.close()

    def test_filename_contains_random_suffix(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        sub = EventLogSubscriber("ts-test")
        # Filename should match pattern: conductor-<name>-YYYYMMDD-HHMMSS-<8 hex chars>.events.jsonl
        import re

        assert re.search(r"\d{8}-\d{6}-[0-9a-f]{8}\.events\.jsonl$", sub.path.name), (
            f"Filename lacks random suffix: {sub.path.name}"
        )
        sub.close()

    def test_integrates_with_emitter(self, tmp_path, monkeypatch):
        from conductor.events import WorkflowEventEmitter

        monkeypatch.setenv("TMPDIR", str(tmp_path))
        emitter = WorkflowEventEmitter()
        sub = EventLogSubscriber("integration")
        emitter.subscribe(sub.on_event)

        emitter.emit(
            WorkflowEvent(type="workflow_started", timestamp=time.time(), data={"name": "test"})
        )
        emitter.emit(
            WorkflowEvent(type="workflow_completed", timestamp=time.time(), data={"elapsed": 1.5})
        )
        sub.close()

        lines = sub.path.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["type"] == "workflow_started"
        assert json.loads(lines[1])["type"] == "workflow_completed"
