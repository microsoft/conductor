"""Tests for configurable event log output directory."""

import tempfile
import time
from pathlib import Path

from conductor.engine.event_log import EventLogSubscriber
from conductor.events import WorkflowEvent


def test_runtime_config_event_log_dir():
    """RuntimeConfig accepts event_log_dir field, defaults to None."""
    from conductor.config.schema import RuntimeConfig

    assert RuntimeConfig().event_log_dir is None
    assert RuntimeConfig(event_log_dir="./logs").event_log_dir == "./logs"
    assert RuntimeConfig(event_log_dir="/var/log/conductor").event_log_dir == ("/var/log/conductor")


def test_default_writes_to_tmpdir():
    """Without event_log_dir, writes to $TMPDIR/conductor/ (existing behavior)."""
    sub = EventLogSubscriber("test_wf")
    try:
        assert sub.path.parent == Path(tempfile.gettempdir()) / "conductor"
    finally:
        sub.close()


def test_custom_event_log_dir(tmp_path):
    """With event_log_dir, writes to the specified directory."""
    sub = EventLogSubscriber(
        "test_wf",
        event_log_dir=tmp_path / "logs",
    )
    try:
        assert sub.path.parent == tmp_path / "logs"
        assert sub.path.exists()

        sub.on_event(
            WorkflowEvent(
                type="test",
                timestamp=time.time(),
                data={},
            )
        )
        sub.close()

        assert sub.path.read_text().strip()
    finally:
        if not sub._handle.closed:
            sub.close()


def test_existing_path_overrides_event_log_dir(tmp_path):
    """On resume, existing_path takes precedence over event_log_dir."""
    existing = tmp_path / "existing.events.jsonl"
    existing.write_text("")

    sub = EventLogSubscriber(
        "test_wf",
        existing_path=existing,
        existing_run_id="abcd1234",
        event_log_dir=tmp_path / "custom",
    )

    try:
        assert sub.path == existing
    finally:
        sub.close()
