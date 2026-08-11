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


def _entry(pid: int, port: int, run_id: str = "", workflow: str = "/tmp/wf.yaml") -> dict:
    return {"pid": pid, "port": port, "run_id": run_id, "workflow": workflow}


class TestPartitionOwnRun:
    """Tests for ``partition_own_run``'s three-signal classification."""

    def test_run_id_match_is_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RUN_ID_ENV, "AbC123")
        monkeypatch.setattr("conductor.cli.self_run.own_run_pids", lambda: frozenset())

        entries = [_entry(111, 8080, run_id="abc123")]
        partition = partition_own_run(entries)

        assert partition.own == entries
        assert partition.others == []
        assert partition.reasons[8080] == "run id"

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
        assert partition.reasons[8080] == "dashboard port"

    def test_ancestry_pid_is_self(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("conductor.cli.self_run.own_run_pids", lambda: frozenset({111}))

        entries = [_entry(111, 8080)]
        partition = partition_own_run(entries)

        assert partition.own == entries
        assert partition.others == []
        assert partition.reasons[8080] == "process ancestry"

    def test_no_signal_leaves_own_empty_and_preserves_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("conductor.cli.self_run.own_run_pids", lambda: frozenset())

        entries = [_entry(111, 8080, run_id="a"), _entry(222, 9090, run_id="b")]
        partition = partition_own_run(entries)

        assert partition.own == []
        assert partition.others == entries


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
