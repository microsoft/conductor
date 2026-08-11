"""Tests for the ``conductor fleet prune`` CLI command (Fleet Manager E5).

Covers:
- ``--dry-run`` lists what would be pruned without deleting anything.
- ``--keep-last`` overrides ``~/.conductor/config.toml``.
- With no ``--keep-last``, the configured (or default) ``keep_last`` is used
  regardless of ``[fleet.retention].enabled`` -- `fleet prune` is the
  explicit manual entry point and runs "regardless" per the design.
- A malformed settings file surfaces as a CLI error (exit 1) when no
  ``--keep-last`` override is given.
- The empty/no-file-to-prune case prints a message and exits 0.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app

runner = CliRunner()


@pytest.fixture()
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the event-log root to an isolated directory.

    See ``tests/test_fleet/test_retention.py``'s identical fixture for why
    ``tempfile.gettempdir`` is patched directly rather than via the
    ``TMPDIR`` env var.
    """
    from conductor.fleet.retention import event_log_root

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    return event_log_root()


@pytest.fixture()
def settings_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "conductor_home"
    home.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))
    return home


def _make_event_log(root: Path, name: str, *, age_seconds: float = 0.0) -> Path:
    import os

    path = root / name
    path.write_text("{}\n")
    if age_seconds:
        now = time.time()
        os.utime(path, (now - age_seconds, now - age_seconds))
    return path


class TestFleetPruneCommand:
    def test_help(self) -> None:
        result = runner.invoke(app, ["fleet", "prune", "--help"])
        assert result.exit_code == 0
        assert "Prune old event logs" in result.output

    def test_dry_run_lists_without_deleting(self, temp_root: Path, settings_home: Path) -> None:
        old_log = _make_event_log(temp_root, "conductor-old.events.jsonl", age_seconds=1000)
        newest = _make_event_log(temp_root, "conductor-newest.events.jsonl", age_seconds=0)

        result = runner.invoke(app, ["fleet", "prune", "--keep-last", "1", "--dry-run"])

        assert result.exit_code == 0
        assert "Would delete" in result.output
        # Collapse Rich's line-wrapping before matching the (possibly long) path.
        normalized = "".join(result.output.split())
        assert old_log.name in normalized
        # Nothing was actually deleted.
        assert old_log.exists()
        assert newest.exists()

    def test_keep_last_override_actually_prunes(self, temp_root: Path, settings_home: Path) -> None:
        old_log = _make_event_log(temp_root, "conductor-old.events.jsonl", age_seconds=1000)
        newest = _make_event_log(temp_root, "conductor-newest.events.jsonl", age_seconds=0)

        result = runner.invoke(app, ["fleet", "prune", "--keep-last", "1"])

        assert result.exit_code == 0
        assert "Deleted" in result.output
        assert not old_log.exists()
        assert newest.exists()

    def test_no_keep_last_uses_settings_default(self, temp_root: Path, settings_home: Path) -> None:
        """With no --keep-last, the configured (default 200) keep_last is
        used -- `fleet prune` always runs regardless of
        [fleet.retention].enabled, since it's the explicit manual entry
        point rather than the opportunistic auto-sweep."""
        old_log = _make_event_log(temp_root, "conductor-old.events.jsonl", age_seconds=1000)

        result = runner.invoke(app, ["fleet", "prune"])

        assert result.exit_code == 0
        # keep_last defaults to 200, so this single file is retained.
        assert old_log.exists()
        assert "Nothing to prune" in result.output

    def test_nothing_to_prune(self, temp_root: Path, settings_home: Path) -> None:
        result = runner.invoke(app, ["fleet", "prune", "--keep-last", "100"])
        assert result.exit_code == 0
        assert "Nothing to prune" in result.output

    def test_malformed_settings_raises_without_override(
        self, temp_root: Path, settings_home: Path
    ) -> None:
        (settings_home / "config.toml").write_text("not valid [[[toml")

        result = runner.invoke(app, ["fleet", "prune"])

        assert result.exit_code == 1
        assert "Error" in result.output

    def test_malformed_settings_ignored_with_explicit_keep_last(
        self, temp_root: Path, settings_home: Path
    ) -> None:
        """An explicit --keep-last override bypasses settings entirely, so a
        broken config.toml does not block the manual override path."""
        (settings_home / "config.toml").write_text("not valid [[[toml")
        old_log = _make_event_log(temp_root, "conductor-old.events.jsonl", age_seconds=1000)

        result = runner.invoke(app, ["fleet", "prune", "--keep-last", "0"])

        assert result.exit_code == 0
        # keep_last=0 means "prune nothing" (mirrors the checkpoint guard).
        assert old_log.exists()
