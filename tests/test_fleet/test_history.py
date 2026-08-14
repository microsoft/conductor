"""Tests for ``conductor.fleet.history`` (Fleet Manager E14).

Covers:
- A log ending in ``workflow_completed`` classifies as ``"completed"``.
- A log ending in ``workflow_failed`` classifies as ``"failed"``.
- A log with no terminal event at all classifies as ``"unknown"`` --
  **never** as ``"running"`` (the core constraint this epic exists to
  enforce: a non-terminal log is not evidence of a live run).
- Token/cost totals are summed the same way ``fleet.summary`` does,
  including the unpriced-agent count.
- The returned list is bounded by the retention ``keep_last`` setting
  (both the explicit override and the settings-driven default), sorted
  newest-first by mtime.
- A corrupt/unreadable log is skipped rather than aborting the whole scan.
- The workflow name and run id are recovered from the standard
  ``conductor-<name>-<ts>-<run_id>.events.jsonl`` filename shape.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from conductor.fleet.history import HistoryEntry, build_history_entries
from conductor.fleet.retention import event_log_root

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``event_log_root()`` to an isolated directory.

    Mirrors ``tests/test_fleet/test_retention.py``'s own fixture -- patches
    ``tempfile.gettempdir`` directly rather than the ``TMPDIR`` env var,
    since ``tempfile`` caches its resolved directory per-process.
    """
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    return event_log_root()


def _event(etype: str, data: dict[str, Any] | None = None, *, ts: float | None = None) -> str:
    """Serialize a single JSONL event line matching WorkflowEvent.to_dict()'s shape."""
    return json.dumps(
        {"type": etype, "timestamp": ts if ts is not None else time.time(), "data": data or {}}
    )


def _write_log(
    root: Path,
    *,
    name: str = "my-workflow",
    ts: str = "20260101-120000",
    run_id: str = "deadbeef",
    lines: list[str] | None = None,
) -> Path:
    """Write a ``conductor-<name>-<ts>-<run_id>.events.jsonl`` file under ``root``."""
    path = root / f"conductor-{name}-{ts}-{run_id}.events.jsonl"
    path.write_text("\n".join(lines or []) + ("\n" if lines else ""))
    return path


# ---------------------------------------------------------------------------
# Terminal-event classification (E14-T1)
# ---------------------------------------------------------------------------


class TestOutcomeClassification:
    def test_completed_log_classifies_as_completed(self, temp_root: Path) -> None:
        _write_log(
            temp_root,
            lines=[
                _event("workflow_started", {"name": "my-workflow"}, ts=1000.0),
                _event("workflow_completed", {"elapsed": 42.0}, ts=1042.0),
            ],
        )

        entries = build_history_entries()

        assert len(entries) == 1
        assert entries[0].outcome == "completed"

    def test_failed_log_classifies_as_failed(self, temp_root: Path) -> None:
        _write_log(
            temp_root,
            lines=[
                _event("workflow_started", {"name": "my-workflow"}, ts=1000.0),
                _event("workflow_failed", {"error_type": "ValueError"}, ts=1010.0),
            ],
        )

        entries = build_history_entries()

        assert len(entries) == 1
        assert entries[0].outcome == "failed"

    def test_log_with_no_terminal_event_is_unknown_never_running(self, temp_root: Path) -> None:
        """The core constraint (acceptance criterion): a log with no
        terminal event must never be presented as "running" -- it is
        "unknown", regardless of whether the process behind it happens to
        still be alive."""
        _write_log(
            temp_root,
            lines=[
                _event("workflow_started", {"name": "my-workflow"}, ts=1000.0),
                _event("agent_started", {"agent_name": "helper"}, ts=1001.0),
            ],
        )

        entries = build_history_entries()

        assert len(entries) == 1
        assert entries[0].outcome == "unknown"
        assert entries[0].outcome != "running"

    def test_empty_log_is_unknown(self, temp_root: Path) -> None:
        _write_log(temp_root, lines=[])

        entries = build_history_entries()

        assert len(entries) == 1
        assert entries[0].outcome == "unknown"

    def test_garbled_content_is_skipped_not_shown_as_unknown(self, temp_root: Path) -> None:
        """A non-empty log containing only malformed (non-JSON) line
        content is corrupt, not "legitimately empty" -- E14-T4 requires a
        corrupt log to be skipped from the returned list entirely, never
        presented as an ordinary "unknown" entry (E14 review round 2)."""
        path = temp_root / "conductor-garbled-20260101-120000-cafef00d.events.jsonl"
        path.write_bytes(b"not json at all\n\x00\x01binary garbage\n")

        entries = build_history_entries()

        assert entries == []


# ---------------------------------------------------------------------------
# Token/cost totals (mirrors fleet.summary's own accounting)
# ---------------------------------------------------------------------------


class TestTokenCostTotals:
    def test_sums_tokens_and_cost_across_completed_agents(self, temp_root: Path) -> None:
        _write_log(
            temp_root,
            lines=[
                _event("workflow_started", {"name": "my-workflow"}, ts=1000.0),
                _event(
                    "agent_completed",
                    {"agent_name": "a", "tokens": 100, "cost_usd": 0.05},
                    ts=1010.0,
                ),
                _event(
                    "agent_completed",
                    {"agent_name": "b", "tokens": 200, "cost_usd": 0.10},
                    ts=1020.0,
                ),
                _event("workflow_completed", {"elapsed": 42.0}, ts=1042.0),
            ],
        )

        entries = build_history_entries()

        assert entries[0].total_tokens == 300
        assert entries[0].total_cost_usd == pytest.approx(0.15)
        assert entries[0].unpriced_agent_count == 0
        assert entries[0].has_unpriced is False

    def test_unpriced_completed_agent_is_counted_not_summed_as_zero(self, temp_root: Path) -> None:
        _write_log(
            temp_root,
            lines=[
                _event("workflow_started", {"name": "my-workflow"}, ts=1000.0),
                _event(
                    "agent_completed",
                    {"agent_name": "a", "tokens": 100, "cost_usd": None},
                    ts=1010.0,
                ),
                _event("workflow_completed", {"elapsed": 42.0}, ts=1042.0),
            ],
        )

        entries = build_history_entries()

        assert entries[0].total_tokens == 100
        assert entries[0].total_cost_usd is None
        assert entries[0].unpriced_agent_count == 1
        assert entries[0].has_unpriced is True


# ---------------------------------------------------------------------------
# Duration
# ---------------------------------------------------------------------------


class TestDuration:
    def test_prefers_the_engine_reported_elapsed(self, temp_root: Path) -> None:
        _write_log(
            temp_root,
            lines=[
                _event("workflow_started", {"name": "my-workflow"}, ts=1000.0),
                _event("workflow_completed", {"elapsed": 42.0}, ts=1999.0),
            ],
        )

        entries = build_history_entries()

        assert entries[0].duration_seconds == pytest.approx(42.0)

    def test_falls_back_to_started_minus_ended_when_no_elapsed_field(self, temp_root: Path) -> None:
        _write_log(
            temp_root,
            lines=[
                _event("workflow_started", {"name": "my-workflow"}, ts=1000.0),
                _event("workflow_failed", {"error_type": "ValueError"}, ts=1010.0),
            ],
        )

        entries = build_history_entries()

        assert entries[0].duration_seconds == pytest.approx(10.0)

    def test_none_when_neither_is_available(self, temp_root: Path) -> None:
        _write_log(temp_root, lines=[_event("agent_started", {"agent_name": "a"}, ts=1000.0)])

        entries = build_history_entries()

        assert entries[0].duration_seconds is None


# ---------------------------------------------------------------------------
# Filename parsing (workflow name / run id)
# ---------------------------------------------------------------------------


class TestFilenameParsing:
    def test_recovers_workflow_name_and_run_id_from_filename(self, temp_root: Path) -> None:
        _write_log(temp_root, name="my-workflow", run_id="deadbeef", lines=[])

        entries = build_history_entries()

        assert entries[0].workflow_name == "my-workflow"
        assert entries[0].run_id == "deadbeef"

    def test_workflow_name_with_hyphens_recovered_correctly(self, temp_root: Path) -> None:
        """The workflow-name segment itself may contain hyphens (a common
        workflow-file-stem convention); only the fixed-format timestamp and
        hex run-id segments anchor the parse from the right."""
        _write_log(temp_root, name="simple-qa-bot", run_id="cafe1234", lines=[])

        entries = build_history_entries()

        assert entries[0].workflow_name == "simple-qa-bot"
        assert entries[0].run_id == "cafe1234"

    def test_unrecognized_filename_shape_falls_back_to_stem(self, temp_root: Path) -> None:
        """An unrelated file that happens to match the glob (e.g. a
        hand-crafted or legacy-shaped file) still produces a usable entry
        rather than being dropped outright."""
        path = temp_root / "conductor-not-the-expected-shape.events.jsonl"
        path.write_text("")

        entries = build_history_entries()

        assert len(entries) == 1
        assert entries[0].run_id is None

    def test_hyphenated_run_id_recovered_correctly(self, temp_root: Path) -> None:
        """A run id containing ``-``/``_`` (issue #435's broadened
        ``conductor.run_id`` contract) still round-trips through the
        filename parser -- without anchoring on the fixed-format timestamp,
        a hyphenated run id could otherwise be split apart and lost."""
        _write_log(temp_root, name="my-workflow", run_id="nightly-run_7", lines=[])

        entries = build_history_entries()

        assert entries[0].workflow_name == "my-workflow"
        assert entries[0].run_id == "nightly-run_7"


# ---------------------------------------------------------------------------
# Bounded by retention (E14-T2's acceptance criterion)
# ---------------------------------------------------------------------------


class TestBoundedByRetention:
    def test_explicit_keep_last_bounds_the_list(self, temp_root: Path) -> None:
        now = time.time()
        for i in range(5):
            path = _write_log(temp_root, name=f"wf{i}", run_id=f"{i:08x}", lines=[])
            # Stagger mtimes so sort order is deterministic (newest last
            # created -> should sort first).
            import os

            os.utime(path, (now + i, now + i))

        entries = build_history_entries(keep_last=2)

        assert len(entries) == 2
        assert entries[0].workflow_name == "wf4"
        assert entries[1].workflow_name == "wf3"

    def test_keep_last_less_than_one_means_unbounded(self, temp_root: Path) -> None:
        """Mirrors ``prune_event_logs``'s own ``keep_last < 1`` semantics:
        a non-positive value means "don't bound", not "show nothing"."""
        for i in range(3):
            _write_log(temp_root, name=f"wf{i}", run_id=f"{i:08x}", lines=[])

        entries = build_history_entries(keep_last=0)

        assert len(entries) == 3

    def test_default_keep_last_reads_from_settings(self, temp_root: Path) -> None:
        for i in range(5):
            _write_log(temp_root, name=f"wf{i}", run_id=f"{i:08x}", lines=[])

        with patch("conductor.fleet.history._resolve_keep_last", return_value=3):
            entries = build_history_entries()

        assert len(entries) == 3

    def test_malformed_settings_falls_back_to_a_default_bound(self, temp_root: Path) -> None:
        """A broken ``~/.conductor/config.toml`` must not crash the History
        screen -- mirrors ``maybe_prune_event_logs``'s own
        never-break-on-bad-settings contract."""
        for i in range(3):
            _write_log(temp_root, name=f"wf{i}", run_id=f"{i:08x}", lines=[])

        with patch("conductor.settings.load_settings", side_effect=Exception("boom")):
            entries = build_history_entries()

        assert len(entries) == 3


# ---------------------------------------------------------------------------
# Corrupt / unreadable logs are skipped, not fatal (E14-T4)
# ---------------------------------------------------------------------------


class TestCorruptLogSkipped:
    def test_one_bad_file_does_not_abort_the_whole_scan(self, temp_root: Path) -> None:
        _write_log(
            temp_root,
            name="good-one",
            run_id="00000001",
            lines=[
                _event("workflow_started", {"name": "good-one"}, ts=1000.0),
                _event("workflow_completed", {"elapsed": 5.0}, ts=1005.0),
            ],
        )
        _write_log(temp_root, name="bad-one", run_id="00000002", lines=[])

        # Monkeypatch the module-level per-file builder to raise only for
        # the "bad-one" path, proving that specific failure is contained
        # and does not prevent the other (good) file from being returned.
        import conductor.fleet.history as history_module

        original_build_entry = history_module._build_entry

        def _flaky_build_entry(path: Path):
            if "bad-one" in path.name:
                raise RuntimeError("simulated corruption")
            return original_build_entry(path)

        with patch.object(history_module, "_build_entry", side_effect=_flaky_build_entry):
            entries = build_history_entries()

        assert len(entries) == 1
        assert entries[0].workflow_name == "good-one"

    def test_unreadable_directory_root_never_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even a total failure resolving/scanning the event-log root
        degrades to an empty list rather than propagating."""
        monkeypatch.setattr(
            "conductor.fleet.history.event_log_root",
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        entries = build_history_entries()

        assert entries == []

    def test_unreadable_individual_file_is_skipped_not_shown_as_unknown(
        self, temp_root: Path
    ) -> None:
        """A log that cannot even be opened (permission denied, vanished
        mid-scan, any other OSError) must be omitted from the returned
        list entirely -- never presented as a fabricated "unknown" entry
        with zero totals, which would misrepresent a read failure as
        legitimate (if inconclusive) retrospective data (E14 review round 1)."""
        _write_log(
            temp_root,
            name="good-one",
            run_id="00000001",
            lines=[
                _event("workflow_started", {"name": "good-one"}, ts=1000.0),
                _event("workflow_completed", {"elapsed": 5.0}, ts=1005.0),
            ],
        )
        unreadable = _write_log(temp_root, name="unreadable-one", run_id="00000002", lines=[])

        import builtins

        real_open = builtins.open

        def _flaky_open(file, *args, **kwargs):
            if str(file) == str(unreadable):
                raise PermissionError("simulated permission denied")
            return real_open(file, *args, **kwargs)

        with patch("builtins.open", side_effect=_flaky_open):
            entries = build_history_entries()

        assert len(entries) == 1
        assert entries[0].workflow_name == "good-one"

    def test_empty_but_readable_log_is_unknown_not_skipped(self, temp_root: Path) -> None:
        """A genuinely empty log (no non-blank lines at all) is
        legitimate data -- readable, with nothing recorded yet -- and
        must still produce a normal "unknown" entry, distinct from both
        an unreadable file and a non-empty-but-corrupt one (E14 review
        round 1 / round 2)."""
        _write_log(temp_root, name="empty-one", lines=[])

        entries = build_history_entries()

        assert len(entries) == 1
        assert entries[0].outcome == "unknown"

    def test_non_empty_corrupt_log_is_skipped(self, temp_root: Path) -> None:
        """A non-empty log whose lines all fail to parse as JSON is
        corrupt, not empty -- it must be skipped from the returned list
        entirely, distinguishing it from the genuinely-empty case above
        (E14 review round 2)."""
        path = temp_root / "conductor-corrupt-20260101-120000-cafef00d.events.jsonl"
        path.write_bytes(b"not json at all\n\x00\x01binary garbage\n")

        entries = build_history_entries()

        assert entries == []


# ---------------------------------------------------------------------------
# Non-finite numeric data is rejected, not allowed to crash rendering
# (E14 review round 1)
# ---------------------------------------------------------------------------


class TestNonFiniteNumericDataRejected:
    def test_nan_infinity_timestamps_are_ignored(self, temp_root: Path) -> None:
        """A NaN/Infinity timestamp -- valid JSON, never a legitimate Unix
        time -- must not become ``started_at``/``ended_at``, which would
        later crash duration formatting (``int(nan)``/``int(inf)`` both
        raise)."""
        _write_log(
            temp_root,
            lines=[
                _event("workflow_started", {"name": "my-workflow"}, ts=float("nan")),
                _event("workflow_completed", {"elapsed": 5.0}, ts=float("inf")),
            ],
        )

        entries = build_history_entries()

        assert len(entries) == 1
        assert entries[0].started_at is None
        assert entries[0].ended_at is None
        # The engine-reported elapsed is still finite and used directly.
        assert entries[0].duration_seconds == 5.0

    def test_non_finite_elapsed_falls_back_to_none(self, temp_root: Path) -> None:
        _write_log(
            temp_root,
            lines=[
                _event("workflow_started", {"name": "my-workflow"}, ts=1000.0),
                _event("workflow_completed", {"elapsed": float("nan")}, ts=1005.0),
            ],
        )

        entries = build_history_entries()

        assert len(entries) == 1
        # No finite elapsed, but started_at/ended_at are both known and
        # finite, so duration falls back to their difference.
        assert entries[0].duration_seconds == 5.0

    def test_non_finite_tokens_and_cost_are_ignored(self, temp_root: Path) -> None:
        _write_log(
            temp_root,
            lines=[
                _event("workflow_started", {"name": "my-workflow"}, ts=1000.0),
                _event(
                    "agent_completed",
                    {"agent_name": "a", "tokens": float("inf"), "cost_usd": float("nan")},
                    ts=1001.0,
                ),
                _event("workflow_completed", {"elapsed": 5.0}, ts=1005.0),
            ],
        )

        entries = build_history_entries()

        assert len(entries) == 1
        assert entries[0].total_tokens == 0
        assert entries[0].total_cost_usd is None


# ---------------------------------------------------------------------------
# Full stream-scan (not a byte-bounded tail read) (E14 review round 1)
# ---------------------------------------------------------------------------


class TestFullStreamScan:
    def test_token_cost_event_beyond_512kib_from_the_start_is_not_omitted(
        self, temp_root: Path
    ) -> None:
        """A token/cost-bearing ``agent_completed`` near the *start* of a
        log larger than the old 512 KiB tail-read window must still be
        counted -- a byte-capped tail read would have silently dropped
        it, presenting a genuinely completed run with an incomplete
        total (E14 review round 1)."""
        padding_event = _event("agent_message", {"content": "x" * 2000})
        lines = [
            _event("workflow_started", {"name": "my-workflow"}, ts=1000.0),
            _event(
                "agent_completed",
                {"agent_name": "early-agent", "tokens": 500, "cost_usd": 0.05},
                ts=1001.0,
            ),
        ]
        # Pad well past 512 KiB so the old tail-bounded reader would have
        # discarded the early agent_completed event above.
        while sum(len(line) + 1 for line in lines) < 600 * 1024:
            lines.append(padding_event)
        lines.append(_event("workflow_completed", {"elapsed": 42.0}, ts=1042.0))

        _write_log(temp_root, lines=lines)

        entries = build_history_entries()

        assert len(entries) == 1
        assert entries[0].outcome == "completed"
        assert entries[0].total_tokens == 500
        assert entries[0].total_cost_usd == 0.05

    def test_oversized_terminal_event_is_not_discarded(self, temp_root: Path) -> None:
        """A ``workflow_completed`` whose own line -- or whose position in
        the file -- would have fallen outside a 512 KiB tail window (e.g.
        because a huge earlier payload pushed the file past that size)
        must still be found and classified correctly."""
        padding_event = _event("agent_message", {"content": "x" * 2000})
        lines = [_event("workflow_started", {"name": "my-workflow"}, ts=1000.0)]
        while sum(len(line) + 1 for line in lines) < 520 * 1024:
            lines.append(padding_event)
        lines.append(_event("workflow_completed", {"elapsed": 42.0}, ts=1042.0))

        _write_log(temp_root, lines=lines)

        entries = build_history_entries()

        assert len(entries) == 1
        assert entries[0].outcome == "completed"
        assert entries[0].duration_seconds == 42.0


# ---------------------------------------------------------------------------
# Display cap is independent of the retention keep_last bound
# (E14 review round 1)
# ---------------------------------------------------------------------------


class TestDisplayCapIndependentOfRetentionSetting:
    def test_keep_last_less_than_one_still_bounds_the_display(self, temp_root: Path) -> None:
        """``keep_last < 1`` means "don't prune" to the retention sweep,
        but the History screen itself must never grow without limit --
        the display cap applies independently of that setting (E14
        review round 1)."""
        import conductor.fleet.history as history_module

        with patch.object(history_module, "_MAX_HISTORY_ENTRIES", 3):
            for i in range(10):
                _write_log(temp_root, name=f"wf{i}", run_id=f"{i:08x}", lines=[])

            entries = build_history_entries(keep_last=0)

        assert len(entries) == 3


# ---------------------------------------------------------------------------
# Resumed runs: a new root workflow_started resets stale terminal state
# (E14 review round 1)
# ---------------------------------------------------------------------------


class TestResumedRunResetsTerminalState:
    def test_activity_after_an_earlier_terminal_event_resets_to_unknown(
        self, temp_root: Path
    ) -> None:
        """A resumed run (no dashboard attached) appends a fresh
        ``workflow_started`` after an earlier ``workflow_failed`` without
        the engine's own re-emit being suppressed -- the scanner must
        treat that as a new execution attempt and stop reporting the
        stale prior outcome until this attempt's own terminal event (if
        any) appears."""
        _write_log(
            temp_root,
            lines=[
                _event("workflow_started", {"name": "my-workflow"}, ts=1000.0),
                _event("workflow_failed", {"error_type": "ValueError"}, ts=1010.0),
                # Resume: a second root-level workflow_started, then more
                # activity with no terminal event of its own yet.
                _event("workflow_started", {"name": "my-workflow"}, ts=2000.0),
                _event("agent_started", {"agent_name": "helper"}, ts=2001.0),
            ],
        )

        entries = build_history_entries()

        assert len(entries) == 1
        assert entries[0].outcome == "unknown"
        assert entries[0].ended_at is None

    def test_a_resumed_attempts_own_terminal_event_is_used(self, temp_root: Path) -> None:
        """Once the resumed attempt reaches its own terminal event, that
        (not the earlier, superseded one) is what the entry reports."""
        _write_log(
            temp_root,
            lines=[
                _event("workflow_started", {"name": "my-workflow"}, ts=1000.0),
                _event("workflow_failed", {"error_type": "ValueError"}, ts=1010.0),
                _event("workflow_started", {"name": "my-workflow"}, ts=2000.0),
                _event("workflow_completed", {"elapsed": 55.0}, ts=2055.0),
            ],
        )

        entries = build_history_entries()

        assert len(entries) == 1
        assert entries[0].outcome == "completed"
        assert entries[0].duration_seconds == 55.0


# ---------------------------------------------------------------------------
# HistoryEntry shape
# ---------------------------------------------------------------------------


class TestHistoryEntryShape:
    def test_entry_carries_the_path(self, temp_root: Path) -> None:
        path = _write_log(temp_root, lines=[])

        entries = build_history_entries()

        assert isinstance(entries[0], HistoryEntry)
        assert entries[0].path == path
