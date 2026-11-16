"""Native process controls for the synthetic Docker peer ownership receipt."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_mcp.fixtures.docker_realm import _PEER
from tests.test_mcp.fixtures.docker_stdio import PeerReceipt


@pytest.mark.parametrize("intermediary", [False, True], ids=["direct", "intermediary"])
def test_peer_identity_requires_owned_native_ancestry(tmp_path: Path, intermediary: bool) -> None:
    # Requirement: a peer belongs to its actual child/launcher chain, never a sibling's.
    receipt = tmp_path / "peer.json"
    args = [str(_PEER), "--peer", str(receipt)]
    if intermediary:
        args.insert(0, str(_PEER.with_name("peer_intermediary.py")))
    runtime_env = {key: value for key, value in os.environ.items() if key.upper() == "SYSTEMROOT"}
    with (
        subprocess.Popen(
            [sys.executable, *args],
            env=runtime_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as child,
        subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
            env=runtime_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as foreign,
    ):
        try:
            _out, err = child.communicate(timeout=20)
            assert child.returncode == 0, err.decode()
            peer = PeerReceipt.model_validate_json(receipt.read_text())
            assert peer.closed and not peer.called
            assert peer.is_owned_by(os.getpid(), child.pid)
            assert not peer.is_owned_by(os.getpid(), foreign.pid)
            assert not peer.is_owned_by(foreign.pid, child.pid)
            assert not peer.is_owned_by(peer.pid, child.pid)
            if intermediary:
                assert peer.pid != child.pid
            print(
                {
                    "intermediary": intermediary,
                    "launcher_pid": os.getpid(),
                    "child_pid": child.pid,
                    "foreign_pid": foreign.pid,
                    "peer_pid": peer.pid,
                    "parent_pids": peer.parent_pids,
                    "closed": peer.closed,
                }
            )
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
            foreign.communicate(timeout=5)
        assert foreign.returncode == 0
