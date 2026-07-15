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

    def test_honours_conductor_run_id_env_var(self, tmp_path, monkeypatch):
        """``CONDUCTOR_RUN_ID`` (set by bg_runner) is used in the filename and run_id.

        This is what cross-correlates the bg ``.bg.stderr.log`` file with
        the child's ``.events.jsonl`` file. See issue #116.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CONDUCTOR_RUN_ID", "abcdef01")

        sub = EventLogSubscriber("env-runid-test")
        try:
            assert sub.run_id == "abcdef01"
            assert "abcdef01" in sub.path.name
        finally:
            sub.close()

    def test_lowercases_hex_run_id_from_env(self, tmp_path, monkeypatch):
        """Mixed-case hex run ids are normalised to lowercase for filename consistency."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CONDUCTOR_RUN_ID", "ABCDEF01")

        sub = EventLogSubscriber("env-runid-case")
        try:
            assert sub.run_id == "abcdef01"
        finally:
            sub.close()

    def test_rejects_invalid_run_id_env(self, tmp_path, monkeypatch):
        """Non-hex / overlong env values are rejected; a fresh random id is used.

        This keeps the filename safe from accidental injection via the env
        var (e.g. path separators, control characters, very long values).
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CONDUCTOR_RUN_ID", "../etc/passwd")

        sub = EventLogSubscriber("env-runid-bad")
        try:
            assert sub.run_id != "../etc/passwd"
            # Falls back to a fresh 8-hex random id.
            import re

            assert re.fullmatch(r"[0-9a-f]{8}", sub.run_id), sub.run_id
        finally:
            sub.close()

    def test_empty_run_id_env_falls_back_to_random(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CONDUCTOR_RUN_ID", "")

        sub = EventLogSubscriber("env-runid-empty")
        try:
            import re

            assert re.fullmatch(r"[0-9a-f]{8}", sub.run_id), sub.run_id
        finally:
            sub.close()

    def test_accepts_32_char_hex_run_id(self, tmp_path, monkeypatch):
        """Upper boundary: a 32-char hex string is the longest accepted run id."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CONDUCTOR_RUN_ID", "a" * 32)

        sub = EventLogSubscriber("env-runid-32")
        try:
            assert sub.run_id == "a" * 32
            assert "a" * 32 in sub.path.name
        finally:
            sub.close()

    def test_rejects_33_char_hex_run_id(self, tmp_path, monkeypatch):
        """One past the upper boundary: 33-char hex is rejected, falls back to random.

        Guards against an accidental relaxation of the regex to ``{1,}``
        or removal of the upper bound, which would let arbitrary-length
        env values into the filename.
        """
        import re

        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CONDUCTOR_RUN_ID", "a" * 33)

        sub = EventLogSubscriber("env-runid-33")
        try:
            assert sub.run_id != "a" * 33
            assert re.fullmatch(r"[0-9a-f]{8}", sub.run_id), sub.run_id
        finally:
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

    def test_appends_to_existing_log(self, tmp_path, monkeypatch):
        """Resume mode: reuse an existing path + run_id and append.

        Regression coverage for issue #167 — a resumed run must continue
        writing to the original JSONL log so a multi-resume session
        produces one continuous log file.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))

        # Seed an "original" log with one event
        original = EventLogSubscriber("appending")
        original.on_event(WorkflowEvent(type="agent_started", timestamp=1.0, data={"a": 1}))
        original.close()

        seeded_path = original.path
        seeded_run_id = original.run_id

        # Resumed subscriber: reuse path + run_id
        resumed = EventLogSubscriber(
            "appending",
            existing_path=seeded_path,
            existing_run_id=seeded_run_id,
        )
        assert resumed.path == seeded_path
        assert resumed.run_id == seeded_run_id

        resumed.on_event(WorkflowEvent(type="agent_completed", timestamp=2.0, data={"a": 1}))
        resumed.close()

        lines = seeded_path.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["type"] == "agent_started"
        assert json.loads(lines[1])["type"] == "agent_completed"

    def test_falls_back_to_new_log_when_existing_path_missing(self, tmp_path, monkeypatch):
        """If the existing path is missing, create a fresh log."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        missing = tmp_path / "does-not-exist.events.jsonl"

        sub = EventLogSubscriber("fallback", existing_path=missing, existing_run_id="abc12345")
        try:
            assert sub.path != missing
            assert sub.path.exists()
            # When falling back, a fresh random run_id is used (not the supplied one)
            assert sub.run_id != "abc12345"
        finally:
            sub.close()

    def test_falls_back_when_no_existing_run_id(self, tmp_path, monkeypatch):
        """Without a paired run_id, ignore the existing path."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        seed = tmp_path / "seed.events.jsonl"
        seed.write_text('{"type":"x","timestamp":0,"data":{}}\n')

        sub = EventLogSubscriber("no-id", existing_path=seed, existing_run_id=None)
        try:
            assert sub.path != seed
            assert sub.path.exists()
        finally:
            sub.close()

    def test_records_working_dir_on_agent_start_events(self, tmp_path, monkeypatch):
        """Requirement: the JSONL log preserves the resolved ``working_dir``
        carried by the additive ``parallel_agent_started`` /
        ``for_each_agent_started`` events, including an explicit ``None`` when
        no working directory was configured."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        sub = EventLogSubscriber("working-dir-events")

        sub.on_event(
            WorkflowEvent(
                type="parallel_agent_started",
                timestamp=time.time(),
                data={
                    "group_name": "fan",
                    "agent_name": "explicit_a",
                    "working_dir": "/repo/a",
                },
            )
        )
        sub.on_event(
            WorkflowEvent(
                type="for_each_agent_started",
                timestamp=time.time(),
                data={
                    "group_name": "fans",
                    "agent_name": "fan_agent[0]",
                    "item_key": "0",
                    "working_dir": None,
                },
            )
        )
        sub.close()

        lines = sub.path.read_text().strip().split("\n")
        assert len(lines) == 2
        parallel = json.loads(lines[0])
        assert parallel["type"] == "parallel_agent_started"
        assert parallel["data"]["working_dir"] == "/repo/a"
        for_each = json.loads(lines[1])
        assert for_each["type"] == "for_each_agent_started"
        assert for_each["data"]["agent_name"] == "fan_agent[0]"
        assert for_each["data"]["item_key"] == "0"
        assert for_each["data"]["working_dir"] is None
