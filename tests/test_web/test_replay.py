"""Tests for the ReplayDashboard server.

Tests cover:
- Loading JSON array format
- Loading JSONL format
- GET /api/state returns all events
- GET /api/replay/info returns correct metadata
- GET /api/logs returns events with download header
- Invalid file handling
- Empty file handling
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from conductor.web.replay import ReplayDashboard, _load_events

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_events(count: int = 3) -> list[dict]:
    """Create a list of sample event dicts."""
    base_ts = 1700000000.0
    events = [
        {
            "type": "workflow_started",
            "timestamp": base_ts,
            "data": {"name": "test-workflow", "entry_point": "agent1"},
        },
    ]
    for i in range(1, count):
        events.append(
            {
                "type": "agent_started",
                "timestamp": base_ts + i * 0.5,
                "data": {"agent_name": f"agent{i}"},
            }
        )
    return events


def _write_json(path: Path, events: list[dict]) -> Path:
    """Write events as a JSON array file."""
    path.write_text(json.dumps(events))
    return path


def _write_jsonl(path: Path, events: list[dict]) -> Path:
    """Write events as a JSONL file."""
    lines = [json.dumps(e) for e in events]
    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# _load_events tests
# ---------------------------------------------------------------------------


class TestLoadEvents:
    """Tests for the _load_events function."""

    def test_load_json_array(self, tmp_path: Path) -> None:
        """Loads a JSON array file correctly."""
        events = _make_events()
        path = _write_json(tmp_path / "log.json", events)
        loaded = _load_events(path)
        assert len(loaded) == len(events)
        assert loaded[0]["type"] == "workflow_started"

    def test_load_jsonl(self, tmp_path: Path) -> None:
        """Loads a JSONL file correctly."""
        events = _make_events()
        path = _write_jsonl(tmp_path / "log.jsonl", events)
        loaded = _load_events(path)
        assert len(loaded) == len(events)
        assert loaded[0]["type"] == "workflow_started"

    def test_load_jsonl_with_blank_lines(self, tmp_path: Path) -> None:
        """Skips blank lines in JSONL files."""
        events = _make_events(2)
        content = json.dumps(events[0]) + "\n\n" + json.dumps(events[1]) + "\n\n"
        path = tmp_path / "log.jsonl"
        path.write_text(content)
        loaded = _load_events(path)
        assert len(loaded) == 2

    def test_load_empty_file_raises(self, tmp_path: Path) -> None:
        """Raises ValueError for empty files."""
        path = tmp_path / "empty.json"
        path.write_text("")
        with pytest.raises(ValueError, match="empty"):
            _load_events(path)

    def test_load_invalid_content_raises(self, tmp_path: Path) -> None:
        """Raises ValueError for completely invalid content."""
        path = tmp_path / "bad.json"
        path.write_text("this is not json at all")
        with pytest.raises(ValueError, match="Cannot parse"):
            _load_events(path)

    def test_load_json_non_array_falls_to_jsonl(self, tmp_path: Path) -> None:
        """A JSON object (not array) falls through to JSONL parsing."""
        path = tmp_path / "obj.json"
        path.write_text('{"type": "workflow_started", "timestamp": 1.0, "data": {}}')
        loaded = _load_events(path)
        assert len(loaded) == 1
        assert loaded[0]["type"] == "workflow_started"


# ---------------------------------------------------------------------------
# ReplayDashboard API tests
# ---------------------------------------------------------------------------


class TestReplayDashboardApi:
    """Tests for the ReplayDashboard API endpoints."""

    def _make_dashboard(self, tmp_path: Path, events: list[dict] | None = None) -> ReplayDashboard:
        """Create a ReplayDashboard from a temp log file."""
        if events is None:
            events = _make_events()
        path = _write_json(tmp_path / "log.json", events)
        return ReplayDashboard(path)

    def test_get_state_returns_all_events(self, tmp_path: Path) -> None:
        """GET /api/state returns all loaded events."""
        events = _make_events(5)
        dashboard = self._make_dashboard(tmp_path, events)
        with TestClient(dashboard.app) as client:
            resp = client.get("/api/state")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 5
            assert data[0]["type"] == "workflow_started"

    def test_replay_info(self, tmp_path: Path) -> None:
        """GET /api/replay/info returns correct metadata."""
        events = _make_events(4)
        dashboard = self._make_dashboard(tmp_path, events)
        with TestClient(dashboard.app) as client:
            resp = client.get("/api/replay/info")
            assert resp.status_code == 200
            info = resp.json()
            assert info["mode"] == "replay"
            assert info["totalEvents"] == 4
            assert info["workflowName"] == "test-workflow"
            assert info["startTime"] == events[0]["timestamp"]
            assert info["endTime"] == events[-1]["timestamp"]

    def test_replay_info_no_workflow_started(self, tmp_path: Path) -> None:
        """GET /api/replay/info works when no workflow_started event exists."""
        events = [
            {"type": "agent_started", "timestamp": 1.0, "data": {"agent_name": "a1"}},
        ]
        dashboard = self._make_dashboard(tmp_path, events)
        with TestClient(dashboard.app) as client:
            resp = client.get("/api/replay/info")
            info = resp.json()
            assert info["workflowName"] is None
            assert info["totalEvents"] == 1

    def test_download_logs(self, tmp_path: Path) -> None:
        """GET /api/logs returns events with Content-Disposition header."""
        dashboard = self._make_dashboard(tmp_path)
        with TestClient(dashboard.app) as client:
            resp = client.get("/api/logs")
            assert resp.status_code == 200
            assert "attachment" in resp.headers.get("content-disposition", "")
            assert len(resp.json()) == 3

    def test_index_returns_html(self, tmp_path: Path) -> None:
        """GET / returns the frontend HTML."""
        dashboard = self._make_dashboard(tmp_path)
        with TestClient(dashboard.app) as client:
            resp = client.get("/")
            assert resp.status_code == 200
            assert "text/html" in resp.headers.get("content-type", "")

    def test_invalid_file_raises(self, tmp_path: Path) -> None:
        """ReplayDashboard raises ValueError for invalid log files."""
        path = tmp_path / "bad.txt"
        path.write_text("not json")
        with pytest.raises(ValueError):
            ReplayDashboard(path)

    def test_jsonl_format_works(self, tmp_path: Path) -> None:
        """ReplayDashboard works with JSONL format files."""
        events = _make_events(3)
        path = _write_jsonl(tmp_path / "log.jsonl", events)
        dashboard = ReplayDashboard(path)
        with TestClient(dashboard.app) as client:
            resp = client.get("/api/state")
            assert resp.status_code == 200
            assert len(resp.json()) == 3
