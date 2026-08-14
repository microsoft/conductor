"""Tests for ``conductor.cli.self_run`` (issue #399).

Covers:
- ``own_run_pids()`` identity signals (self, ancestry, session)
- ``_read_ppid`` ancestry-walk driving via ``own_run_pids``
- ``partition_own_run`` classification against each of the three signals
- ``describe_own_run`` identity formatting
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from conductor.cli.self_run import (
    _MAX_ANCESTRY_HOPS,
    RUN_ID_ENV,
    WEB_BG_ENV,
    WEB_PORT_ENV,
    describe_own_run,
    own_run_pids,
    partition_own_run,
)
from conductor.fleet.records import RunRecord


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the bg-launch identity env vars don't leak in from the real environment."""
    monkeypatch.delenv(RUN_ID_ENV, raising=False)
    monkeypatch.delenv(WEB_BG_ENV, raising=False)
    monkeypatch.delenv(WEB_PORT_ENV, raising=False)


class TestOwnRunPids:
    """Tests for the real (unmocked) ``own_run_pids()``."""

    def test_contains_own_pid_on_every_platform(self) -> None:
        assert os.getpid() in own_run_pids()

    @pytest.mark.skipif(
        not sys.platform.startswith("linux"), reason="ancestry walk is /proc-based (Linux only)"
    )
    def test_contains_parent_pid_on_linux(self) -> None:
        assert os.getppid() in own_run_pids()

    @pytest.mark.skipif(sys.platform == "win32", reason="os.getsid is POSIX-only")
    def test_contains_session_id_on_posix(self) -> None:
        assert os.getsid(0) in own_run_pids()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="ancestry walk is /proc-based (Linux only)"
)
class TestAncestryWalk:
    """Drives ``own_run_pids()``'s ``/proc`` walk via a monkeypatched ``_read_ppid``."""

    def test_multi_hop_chain_fully_collected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        me = os.getpid()
        chain = {me: 100_001, 100_001: 100_002, 100_002: 100_003, 100_003: None}
        monkeypatch.setattr("conductor.cli.self_run._read_ppid", lambda pid: chain.get(pid))

        pids = own_run_pids()

        assert {me, 100_001, 100_002, 100_003}.issubset(pids)

    def test_ppid_cycle_terminates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        me = os.getpid()
        chain = {me: 200_001, 200_001: 200_002, 200_002: 200_001}
        monkeypatch.setattr("conductor.cli.self_run._read_ppid", lambda pid: chain.get(pid))

        # Must return promptly rather than looping forever.
        pids = own_run_pids()

        assert {me, 200_001, 200_002}.issubset(pids)

    def test_max_ancestry_hops_cap_holds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        me = os.getpid()
        # Build a chain longer than the hop cap, all distinct pids.
        chain_len = _MAX_ANCESTRY_HOPS + 20
        chain: dict[int, int | None] = {}
        prev = me
        for i in range(chain_len):
            nxt = 300_000 + i
            chain[prev] = nxt
            prev = nxt
        chain[prev] = None
        monkeypatch.setattr("conductor.cli.self_run._read_ppid", lambda pid: chain.get(pid))

        pids = own_run_pids()

        # The walk is bounded, so it cannot have collected the entire chain.
        assert len(pids) <= _MAX_ANCESTRY_HOPS + 2  # +1 for self, +1 slack for getsid

    def test_unreadable_proc_stops_without_raising(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("conductor.cli.self_run._read_ppid", lambda pid: None)

        pids = own_run_pids()

        assert os.getpid() in pids


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="ancestry walk is /proc-based (Linux only)"
)
class TestReadPpid:
    """Direct tests of ``_read_ppid``'s real error handling (not monkeypatched away)."""

    def test_returns_none_for_nonexistent_pid(self) -> None:
        from conductor.cli.self_run import _read_ppid

        assert _read_ppid(999_999_999) is None

    def test_returns_none_for_malformed_ppid_value(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from pathlib import Path as _Path

        from conductor.cli import self_run

        fake_status = tmp_path / "status"
        fake_status.write_text("Name:\ttest\nPPid:\tnotanumber\n")
        monkeypatch.setattr(
            self_run,
            "Path",
            lambda p: fake_status if str(p).startswith("/proc/") else _Path(p),
        )

        assert self_run._read_ppid(12345) is None

    def test_returns_none_when_no_ppid_line_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from pathlib import Path as _Path

        from conductor.cli import self_run

        fake_status = tmp_path / "status"
        fake_status.write_text("Name:\ttest\nState:\tS (sleeping)\n")
        monkeypatch.setattr(
            self_run,
            "Path",
            lambda p: fake_status if str(p).startswith("/proc/") else _Path(p),
        )

        assert self_run._read_ppid(12345) is None

    def test_non_utf8_status_file_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """A process with a non-UTF-8 ``Name:`` (e.g. via ``prctl(PR_SET_NAME)``) must
        not crash the ancestry walk -- only the ``PPid:`` line is ever parsed."""
        from pathlib import Path as _Path

        from conductor.cli import self_run

        fake_status = tmp_path / "status"
        fake_status.write_bytes(b"Name:\tx\xff\xfey\nPPid:\t42\n")
        monkeypatch.setattr(
            self_run,
            "Path",
            lambda p: fake_status if str(p).startswith("/proc/") else _Path(p),
        )

        assert self_run._read_ppid(12345) == 42


def _entry(
    pid: int, port: int | None, run_id: str = "", workflow: str = "/tmp/wf.yaml"
) -> RunRecord:
    """Build a run record for the partitioner.

    ``partition_own_run`` operates on :class:`RunRecord` since the Fleet
    Manager -- ``read_run_records`` surfaces legacy ``.pid`` files in that
    same shape, so there is no dict form left to model.
    """
    return RunRecord(
        run_id=run_id,
        pid=pid,
        workflow_path=workflow,
        workflow_name=Path(workflow).stem,
        started_at="2026-03-03T00:00:00+00:00",
        event_log_path="",
        port=port,
        mode="bg" if port is not None else "fg",
        checkpoint_dir=None,
    )


class TestPartitionOwnRun:
    """Tests for ``partition_own_run``'s three-signal classification."""

    def test_run_id_match_is_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RUN_ID_ENV, "AbC123")
        monkeypatch.setattr("conductor.cli.self_run.own_run_pids", lambda: frozenset())

        entries = [_entry(111, 8080, run_id="abc123")]
        partition = partition_own_run(entries)

        assert partition.own == entries
        assert partition.others == []
        assert partition.reasons[111] == "run id"

    def test_different_run_id_is_not_self_even_with_matching_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(RUN_ID_ENV, "self-run-id")
        monkeypatch.setenv(WEB_BG_ENV, "1")
        monkeypatch.setenv(WEB_PORT_ENV, "8080")
        monkeypatch.setattr("conductor.cli.self_run.own_run_pids", lambda: frozenset())

        # Same port as our own web-bg port, but a *different* recorded run_id
        # -- the compatibility (port) signal must not fire here.
        entries = [_entry(111, 8080, run_id="someone-elses-run-id")]
        partition = partition_own_run(entries)

        assert partition.own == []
        assert partition.others == entries

    def test_legacy_port_signal_fires_only_without_a_recorded_run_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(WEB_BG_ENV, "1")
        monkeypatch.setenv(WEB_PORT_ENV, "8080")
        monkeypatch.setattr("conductor.cli.self_run.own_run_pids", lambda: frozenset())

        entries = [_entry(111, 8080, run_id="")]
        partition = partition_own_run(entries)

        assert partition.own == entries
        assert partition.others == []
        assert partition.reasons[111] == "dashboard port"

    def test_ancestry_pid_is_self(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("conductor.cli.self_run.own_run_pids", lambda: frozenset({111}))

        entries = [_entry(111, 8080)]
        partition = partition_own_run(entries)

        assert partition.own == entries
        assert partition.others == []
        assert partition.reasons[111] == "process ancestry"

    def test_no_signal_leaves_own_empty_and_preserves_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("conductor.cli.self_run.own_run_pids", lambda: frozenset())

        entries = [_entry(111, 8080, run_id="a"), _entry(222, 9090, run_id="b")]
        partition = partition_own_run(entries)

        assert partition.own == []
        assert partition.others == entries

    def test_mixed_entries_partition_correctly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A genuinely mixed self+other input classifies each entry independently.

        This is the precise, cheap unit-level check for the exact scenario
        issue #399 exists to fix: proven (during review) to catch an
        own/others swap bug that CLI-level substring assertions missed.
        """
        monkeypatch.setenv(RUN_ID_ENV, "mine")
        monkeypatch.setattr("conductor.cli.self_run.own_run_pids", lambda: frozenset())

        entries = [_entry(111, 8080, run_id="mine"), _entry(222, 9090, run_id="other")]
        partition = partition_own_run(entries)

        assert partition.own == [entries[0]]
        assert partition.others == [entries[1]]

    def test_non_string_run_id_is_ignored_rather_than_crashing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A malformed ``run_id`` must not crash classification.

        The partitioner no longer needs its own type guard: it takes
        :class:`RunRecord`, and a hand-edited file with an integer id is
        rejected by ``RunRecord.from_dict`` before it can ever reach here --
        ``_load_record_file`` then skips that file. The guard moved down a
        layer rather than disappearing.
        """
        monkeypatch.setenv(RUN_ID_ENV, "mine")
        monkeypatch.setattr("conductor.cli.self_run.own_run_pids", lambda: frozenset())

        with pytest.raises(ValueError, match="run_id must be a string"):
            RunRecord.from_dict(
                {"pid": 111, "port": 8080, "run_id": 12345, "workflow": "/tmp/wf.yaml"}
            )

        # And a well-formed record still classifies normally afterwards, which
        # is the property that actually matters: one bad file on disk is
        # skipped by `_load_record_file` (see tests/test_fleet/test_records.py)
        # rather than taking down classification for every other run.
        other = _entry(222, 9090, run_id="theirs")
        partition = partition_own_run([other])
        assert partition.own == []
        assert partition.others == [other]


class TestOwnRunPartitionInvariant:
    """Tests for ``OwnRunPartition``'s constructor-enforced own/others disjointness."""

    def test_disjoint_own_and_others_construct_cleanly(self) -> None:
        from conductor.cli.self_run import OwnRunPartition

        OwnRunPartition(own=[_entry(111, 8080)], others=[_entry(222, 9090)], reasons={})

    def test_overlapping_own_and_others_raises(self) -> None:
        from conductor.cli.self_run import OwnRunPartition

        entry = _entry(111, 8080)
        with pytest.raises(ValueError, match="both own and other"):
            OwnRunPartition(own=[entry], others=[entry], reasons={})

    def test_entries_missing_port_do_not_falsely_collide(self) -> None:
        """Multiple entries with no ``port`` key must not be treated as an overlap."""
        from conductor.cli.self_run import OwnRunPartition

        OwnRunPartition(
            own=[_entry(1, 8080)],
            others=[_entry(2, 8081)],
            reasons={},
        )


class TestDescribeOwnRun:
    """Tests for ``describe_own_run``'s identity formatting."""

    def test_prefers_run_id(self) -> None:
        entry = _entry(111, 8080, run_id="abc123", workflow="/tmp/my-workflow.yaml")
        assert describe_own_run(entry) == "abc123"

    def test_falls_back_to_workflow_stem_and_port(self) -> None:
        entry = _entry(111, 8080, run_id="", workflow="/tmp/my-workflow.yaml")
        assert describe_own_run(entry) == "my-workflow (port 8080)"

    def test_returns_plain_str_not_text(self) -> None:
        entry = _entry(111, 8080, run_id="abc123")
        assert type(describe_own_run(entry)) is str

    def test_null_workflow_falls_back_to_unknown_rather_than_crashing(self) -> None:
        """A PID file with ``"workflow": null`` must not crash ``Path(None)``.

        ``dict.get(key, default)`` only substitutes the default when the key
        is *absent*, not when it's present with value ``None`` -- so this
        exercises that exact gotcha.
        """
        entry = _entry(111, 8080, workflow="")
        assert describe_own_run(entry) == "unknown (port 8080)"


class TestForegroundSelfExclusionOffLinux:
    """A foreground run must be able to recognise itself without ``/proc``.

    A fg run has no port (so signal 2 cannot fire) and, off Linux, no
    ``/proc`` ancestry walk (so signal 3 cannot either). ``cli/run.py``
    therefore exports ``CONDUCTOR_SELF_RUN_ID`` from every run into its own
    environment, so a descendant -- an agent's shell, and the ``conductor
    stop --all`` it spawns -- inherits it and signal 1 works everywhere.
    Without it, that command terminates its own workflow (issue #399).
    """

    def test_self_run_id_identifies_a_portless_foreground_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(RUN_ID_ENV, raising=False)
        monkeypatch.delenv(WEB_BG_ENV, raising=False)
        monkeypatch.delenv(WEB_PORT_ENV, raising=False)
        monkeypatch.setenv("CONDUCTOR_SELF_RUN_ID", "fgrun01")
        # No ancestry signal available -- the Windows/macOS situation.
        monkeypatch.setattr("conductor.cli.self_run.own_run_pids", lambda: frozenset())

        entries = [_entry(111, None, run_id="fgrun01")]
        partition = partition_own_run(entries)

        assert partition.own == entries
        assert partition.others == []
        assert partition.reasons[111] == "run id"

    def test_a_different_runs_record_is_still_targetable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(RUN_ID_ENV, raising=False)
        monkeypatch.setenv("CONDUCTOR_SELF_RUN_ID", "fgrun01")
        monkeypatch.setattr("conductor.cli.self_run.own_run_pids", lambda: frozenset())

        entries = [_entry(222, None, run_id="someoneelse")]
        partition = partition_own_run(entries)

        assert partition.own == []
        assert partition.others == entries

    def test_bg_run_id_env_still_wins_when_both_are_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``CONDUCTOR_RUN_ID`` is the bg child's own id; a stale inherited
        ``CONDUCTOR_SELF_RUN_ID`` from an outer run must not displace it."""
        monkeypatch.setenv(RUN_ID_ENV, "bgchild")
        monkeypatch.setenv("CONDUCTOR_SELF_RUN_ID", "outerrun")
        monkeypatch.setattr("conductor.cli.self_run.own_run_pids", lambda: frozenset())

        entries = [_entry(111, 8080, run_id="bgchild"), _entry(222, 8081, run_id="outerrun")]
        partition = partition_own_run(entries)

        assert [e.pid for e in partition.own] == [111]
        assert [e.pid for e in partition.others] == [222]
