"""Requirements for local-machine workspace claim ownership."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch

import pytest

from conductor.engine.workspace_claim import (
    WorkspaceClaimError,
    acquire_workspace_claim,
)


def test_claim_acquire_release_preserves_lock_file(tmp_path, monkeypatch) -> None:
    # Requirement: release clears ownership but leaves the reusable advisory lock.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))
    claim = acquire_workspace_claim("run-1")
    assert json.loads(claim.path.read_text())["token"] == claim.token
    claim.release()
    assert not claim.path.exists()
    assert claim.path.with_suffix(".lock").exists()
    claim.release()
    another = acquire_workspace_claim("run-1")
    another.release()


def test_live_owner_is_refused(tmp_path, monkeypatch) -> None:
    # Requirement: a process that still exists keeps the workspace reservation.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))
    claim = acquire_workspace_claim("run-2")
    with pytest.raises(WorkspaceClaimError, match=str(os.getpid())):
        acquire_workspace_claim("run-2")
    claim.release()


@pytest.mark.asyncio
async def test_live_child_claim_blocks_parent_until_child_exits(tmp_path, monkeypatch) -> None:
    # Requirement: a different process cannot claim a live owner, then can reap its stale claim.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))
    child_code = (
        "import sys\n"
        "from conductor.engine.workspace_claim import acquire_workspace_claim\n"
        "acquire_workspace_claim('cross-process')\n"
        "print('READY', flush=True)\n"
        "sys.stdin.buffer.read(1)\n"
    )
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child_code,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert child.stdout is not None
        assert await asyncio.wait_for(child.stdout.readline(), timeout=10) == b"READY\n"
        with pytest.raises(WorkspaceClaimError, match=str(child.pid)):
            acquire_workspace_claim("cross-process")
        assert child.stdin is not None
        child.stdin.write(b"x")
        await child.stdin.drain()
        assert await asyncio.wait_for(child.wait(), timeout=10) == 0
        claim = acquire_workspace_claim("cross-process")
        assert claim.path.exists()
        claim.release()
    finally:
        if child.returncode is None:
            child.kill()
            await asyncio.wait_for(child.wait(), timeout=10)


def test_eperm_owner_is_treated_as_alive(tmp_path, monkeypatch) -> None:
    # Requirement: EPERM never permits reclaiming a possibly live process.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))
    claim = acquire_workspace_claim("run-3")
    with (
        patch(
            "conductor.engine.workspace_claim.os.kill",
            side_effect=PermissionError(errno.EPERM, "denied"),
        ),
        pytest.raises(WorkspaceClaimError, match=str(os.getpid())),
    ):
        acquire_workspace_claim("run-3")
    claim.release()


def test_stale_owner_two_racers_only_one_acquires(tmp_path, monkeypatch) -> None:
    # Requirement: serialized stale reclaim gives exactly one racer ownership.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))
    stale = acquire_workspace_claim("run-4")
    data = json.loads(stale.path.read_text())
    dead_pid = 9999999
    data["pid"] = dead_pid
    stale.path.write_text(json.dumps(data))
    barrier = threading.Barrier(2)
    real_kill = os.kill

    def probe(pid: int, signal: int) -> None:
        if pid == dead_pid:
            raise ProcessLookupError(errno.ESRCH, "gone")
        real_kill(pid, signal)

    monkeypatch.setattr("conductor.engine.workspace_claim.os.kill", probe)

    def race():
        barrier.wait()
        try:
            return acquire_workspace_claim("run-4")
        except WorkspaceClaimError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in (pool.submit(race), pool.submit(race))]
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    winners[0].release()


def test_old_token_cannot_release_new_owner(tmp_path, monkeypatch) -> None:
    # Requirement: token fencing prevents an old owner removing a replacement.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))
    claim = acquire_workspace_claim("run-5")
    wrong = replace(claim, token="wrong")
    with pytest.raises(WorkspaceClaimError, match="token"):
        wrong.release()
    assert claim.path.exists()
    claim.release()


def test_invalid_run_id_is_refused(tmp_path, monkeypatch) -> None:
    # Requirement: empty and path-shaped identifiers never become claim paths.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))
    for run_id in ("", "../other"):
        with pytest.raises(WorkspaceClaimError):
            acquire_workspace_claim(run_id)
