"""Executable Docker stand-in with a separate, environment-isolated MCP peer.

Run through the temporary docker/docker.cmd launcher made by test_docker_transport.
Control commands reuse the existing fake daemon; only ``run`` owns stdio.
"""

from __future__ import annotations

import io
import json
import os
import runpy
import subprocess
import sys
import threading
import traceback
from pathlib import Path

sys.dont_write_bytecode = True


# Receipt schemas are a host-side API; executable control and peer paths use plain snapshots.
if __name__ != "__main__":
    from itertools import pairwise

    from pydantic import BaseModel, ConfigDict, Field

    class PeerReceipt(BaseModel):
        """The server's environment, separating the explicit host runtime carrier."""

        model_config = ConfigDict(frozen=True)
        pid: int
        parent_pids: list[int]
        env: dict[str, str]
        runtime_env: dict[str, str] = Field(default_factory=dict)
        called: bool
        closed: bool

        def is_owned_by(self, launcher_pid: int, child_pid: int) -> bool:
            """Require the actual Popen child-to-launcher edge in the peer's native ancestry."""
            chain = [self.pid, *self.parent_pids]
            return self.pid != launcher_pid and any(
                child == child_pid and parent == launcher_pid for child, parent in pairwise(chain)
            )

    class LaunchReceipt(BaseModel):
        """The fake CLI's observation of delivery and the joined peer."""

        model_config = ConfigDict(frozen=True)
        pid: int
        child_pid: int
        env: dict[str, str]
        argv: list[str]
        env_file: str
        payload: dict[str, str]
        mode: int
        joined: bool
        exit_code: int


def peer(receipt: Path, *, system_root_carrier: bool = False) -> int:
    """Record the actual server environment and acknowledge a real tool request."""
    import psutil

    if os.environ.get("PEER_FAILURE", "").startswith("diagnostic-"):
        from diagnostic_stdio import diagnostic_peer

        return diagnostic_peer(receipt)
    observed_env = dict(os.environ)
    runtime_env: dict[str, str] = {}
    if system_root_carrier:
        # Keep the logical payload exact while recording the actual runtime-only addition.
        runtime_env["SYSTEMROOT"] = observed_env.pop("SYSTEMROOT")
    snapshot = {
        "pid": os.getpid(),
        "parent_pids": [parent.pid for parent in psutil.Process().parents()],
        "env": observed_env,
        "runtime_env": runtime_env,
        "called": False,
        "closed": False,
    }
    receipt.write_text(json.dumps(snapshot), encoding="utf-8")
    if os.environ.get("PEER_INITIALIZE_FILE"):
        # This is a disk-backed daemon model, not evidence of a native Docker mount.
        request = json.loads(sys.stdin.buffer.readline())
        assert request["method"] == "initialize"
        snapshot["initialize_seen"] = True
        workspace = Path(os.environ["PEER_WORKSPACE"])
        (workspace / os.environ["PEER_INITIALIZE_FILE"]).write_bytes(b"initialize-mutated\n")
    if os.environ.get("PEER_FAILURE") == "startup":
        snapshot["closed"] = True
        receipt.write_text(json.dumps(snapshot), encoding="utf-8")
        return 23

    from mcp.server.fastmcp import FastMCP

    server = FastMCP("environment-witness")

    @server.tool()
    def receive() -> str:
        """Acknowledge delivery without returning secret values through MCP."""
        snapshot["called"] = True
        receipt.write_text(json.dumps(snapshot), encoding="utf-8")
        if os.environ.get("PEER_FAILURE") == "transport":
            snapshot["closed"] = True
            receipt.write_text(json.dumps(snapshot), encoding="utf-8")
            os._exit(24)
        if os.environ.get("PEER_FAILURE") == "call":
            raise RuntimeError("fixture request rejected")
        return "payload received"

    try:
        server.run(transport="stdio")
    finally:
        snapshot["closed"] = True
        receipt.write_text(json.dumps(snapshot), encoding="utf-8")
    return 0


def main() -> int:
    """Keep Docker control traffic out of the SDK's protocol channel."""
    argv = sys.argv[1:]
    if argv[0] == "--peer":
        return peer(Path(argv[1]), system_root_carrier="--system-root-carrier" in argv[2:])
    helper = Path(__file__).resolve().parents[2] / "test_execution" / "docker_fake.py"
    if argv[0] != "run":
        # Preserve the old helper's empty input and ten-second failure bound in this process.
        def expire_control() -> None:
            traceback.print_exception(
                subprocess.TimeoutExpired([sys.executable, str(helper), *argv], 10)
            )
            sys.stderr.flush()
            os._exit(1)

        deadline = threading.Timer(10, expire_control)
        deadline.daemon = True
        previous_stdin = sys.stdin
        with io.TextIOWrapper(io.BytesIO()) as empty_stdin:
            sys.stdin = empty_stdin
            deadline.start()
            try:
                # The existing executable state machine terminates through SystemExit.
                runpy.run_path(str(helper), run_name="__main__")
            finally:
                sys.stdin = previous_stdin
                deadline.cancel()
                deadline.join()
    control = subprocess.run(
        [sys.executable, str(helper), *argv],
        input=b"",
        capture_output=True,
        timeout=10,
        check=False,
    )
    if argv[0] != "run" or control.returncode:
        sys.stdout.buffer.write(control.stdout)
        sys.stderr.buffer.write(control.stderr)
        return control.returncode

    env_file = Path(argv[argv.index("--env-file") + 1])
    payload = dict(line.split("=", 1) for line in env_file.read_text(encoding="utf-8").splitlines())
    if payload.get("PEER_INITIALIZE_FILE"):
        state = json.loads(Path(os.environ["FAKE_DOCKER_STATE"]).read_text())
        name = argv[argv.index("--name") + 1]
        payload["PEER_WORKSPACE"] = str(
            Path(os.environ["FAKE_DOCKER_WORKSPACE_ROOT"]) / state["containers"][name]["Volume"]
        )
    receipt = Path(os.environ["MCP_PEER_RECEIPT"]).with_name(
        argv[argv.index("--name") + 1] + ".json"
    )
    launcher = receipt.with_suffix(".launcher.json")
    peer_env = dict(payload)
    peer_args = [sys.executable, str(Path(__file__).resolve()), "--peer", str(receipt)]
    if sys.platform == "win32" and not any(key.upper() == "SYSTEMROOT" for key in payload):
        # Requirement: Windows Python needs SystemRoot for native SDK imports, not ambient access.
        # https://docs.python.org/3.12/library/subprocess.html#subprocess.Popen
        peer_env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        peer_args.append("--system-root-carrier")
    with subprocess.Popen(
        peer_args,
        env=peer_env,
    ) as child:
        launch = {
            "pid": os.getpid(),
            "child_pid": child.pid,
            "env": dict(os.environ),
            "argv": argv,
            "env_file": str(env_file),
            "payload": payload,
            "mode": env_file.stat().st_mode & 0o777,
            "joined": False,
        }
        launcher.write_text(json.dumps(launch), encoding="utf-8")
        try:
            code = child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
            raise
        launch["joined"] = True
        launch["exit_code"] = code
        launcher.write_text(json.dumps(launch), encoding="utf-8")
        return code


if __name__ == "__main__":
    raise SystemExit(main())
