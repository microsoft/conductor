"""Native contracts for the synthetic Docker control-command bootstrap."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final, assert_never

import psutil
import pytest

from tests.test_engine.mcp_placement import preserve_primary
from tests.test_mcp.fixtures.docker_realm import _PEER, _Realm, create_realm

_ENTRY: Final = """
import json, os, runpy, sys
from pathlib import Path
script = sys.argv.pop(1)
receipt = Path(sys.argv.pop(1))
sys.argv[0] = script
receipt.write_text(json.dumps({"pid": os.getpid()}))
try:
    runpy.run_path(script, run_name="__main__")
finally:
    receipt.write_text(json.dumps({
        "pid": os.getpid(),
        "peer_imports": [name for name in ("pydantic", "psutil", "mcp") if name in sys.modules],
    }))
"""
_NL: Final = os.linesep.encode()
_WINDOWS: Final = sys.platform == "win32"


async def _assert_cancelled_control(
    owner: psutil.Process,
    cancel_and_drain: Callable[[], Awaitable[None]],
    receive: Callable[[], Awaitable[bytes]],
) -> None:
    try:
        await cancel_and_drain()
    finally:
        with preserve_primary():
            await asyncio.to_thread(owner.wait, timeout=5)
            assert not owner.is_running()
    try:
        assert await receive() == b""
    except ConnectionResetError as error:
        if not (_WINDOWS and getattr(error, "winerror", None) == 10054):
            raise


@pytest.fixture
def realm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Realm:
    return create_realm(tmp_path, monkeypatch)


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (("image", "inspect", "peer:image"), (0, b"{}" + _NL, b"")),
        (
            ("volume", "create", "--label", "key=literal & $HOME", "volume with spaces"),
            (0, b"volume with spaces" + _NL, b""),
        ),
        (("inspect", "missing container"), (1, b"", b"no such container: missing container" + _NL)),
        (
            ("unsupported", "literal & $HOME"),
            (2, b"", b"unsupported fake docker argv: ['unsupported', 'literal & $HOME']" + _NL),
        ),
    ],
)
def test_control_preserves_argv_environment_empty_stdin_and_result(
    realm: _Realm, argv: tuple[str, ...], expected: tuple[int, bytes, bytes]
) -> None:
    # Requirement: real control execution retains exact delivery and success/failure bytes.
    environment = {**realm.backend._cli_env, "CONTROL_WITNESS": "literal & $HOME \u03bb"}
    result = subprocess.run(
        [realm.backend._docker_binary, *argv],
        input=b"must not reach the daemon\x00\n",
        capture_output=True,
        env=environment,
        timeout=15,
        check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == expected
    record = json.loads((realm.root / "docker.jsonl").read_text())
    assert record["argv"] == list(argv) and record["stdin"] == ""
    assert record["env"] == environment
    if argv[:2] == ("volume", "create"):
        state = json.loads((realm.root / "state.json").read_text())
        assert state["volumes"] == {"volume with spaces": {"key": "literal & $HOME"}}


def test_control_uses_outer_interpreter_without_peer_imports(realm: _Realm) -> None:
    # Requirement: the actual daemon operation shares the outer CLI process, without SDK imports.
    receipt = realm.root / "outer.json"
    with socket.create_server(("127.0.0.1", 0)) as listener:
        listener.settimeout(5)
        environment = {
            **realm.backend._cli_env,
            "FAKE_DOCKER_DELAY_COMMAND": "image inspect",
            "FAKE_DOCKER_DELAY_READY_PORT": str(listener.getsockname()[1]),
        }
        with subprocess.Popen(
            [
                sys.executable,
                "-c",
                _ENTRY,
                str(_PEER),
                str(receipt),
                "image",
                "inspect",
                "peer:image",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        ) as child:
            try:
                connection, _address = listener.accept()
                with connection:
                    connection.settimeout(5)
                    with connection.makefile("rb") as stream:
                        identity = stream.readline().split()
                    daemon_pid = int(identity[0])
                    outer_pid = json.loads(receipt.read_text())["pid"]
                    connection.sendall(b"continue")
                out, err = child.communicate(input=b"ignored\n", timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                child.kill()
                child.communicate(timeout=5)
                raise
        assert child.returncode == 0 and (out, err) == (b"{}" + _NL, b"")
    snapshot = json.loads(receipt.read_text())
    assert daemon_pid == outer_pid == snapshot["pid"]
    assert snapshot["peer_imports"] == []
    print({"outer_pid": outer_pid, "daemon_pid": daemon_pid, "joined": True})


async def test_control_cancellation_joins_native_owner_and_closes_pipe(realm: _Realm) -> None:
    # Requirement: backend cancellation reaches the real held control owner and drains its pipes.
    loop = asyncio.get_running_loop()
    with socket.create_server(("127.0.0.1", 0)) as listener:
        listener.setblocking(False)
        realm.backend._cli_env.update(
            FAKE_DOCKER_DELAY_COMMAND="image inspect",
            FAKE_DOCKER_DELAY_READY_PORT=str(listener.getsockname()[1]),
        )
        opening = asyncio.create_task(realm.backend._run_docker(("image", "inspect", "peer:image")))
        try:
            connection, _address = await asyncio.wait_for(loop.sock_accept(listener), 5)
            with connection:
                connection.setblocking(False)
                identity = b""
                while not identity.endswith(b"\n"):
                    chunk = await asyncio.wait_for(loop.sock_recv(connection, 4096), 5)
                    assert chunk
                    identity += chunk
                owner = psutil.Process(int(identity.split()[0]))

                async def cancel_and_drain() -> None:
                    opening.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(opening, 5)

                async def receive() -> bytes:
                    return await asyncio.wait_for(loop.sock_recv(connection, 1), 5)

                await _assert_cancelled_control(owner, cancel_and_drain, receive)
                print({"cancelled_owner": owner.pid, "terminal": True})
        finally:
            with preserve_primary():
                if not opening.done():
                    opening.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await opening


def test_control_preserves_inner_ten_second_failure_bound(realm: _Realm) -> None:
    # Requirement: removing the helper process retains its failure status and bounded lifetime.
    environment = {
        **realm.backend._cli_env,
        "FAKE_DOCKER_DELAY_COMMAND": "image inspect",
        "FAKE_DOCKER_DELAY": "30",
    }
    result = subprocess.run(
        [sys.executable, str(_PEER), "image", "inspect", "peer:image"],
        input=b"ignored",
        capture_output=True,
        env=environment,
        timeout=15,
        check=False,
    )
    assert result.returncode == 1 and result.stdout == b""
    assert b"subprocess.TimeoutExpired" in result.stderr
    assert b"timed out after 10 seconds" in result.stderr


class _WindowsReset(ConnectionResetError):
    def __init__(self, winerror: int) -> None:
        super().__init__(winerror, "synthetic socket reset")
        self.winerror = winerror


type _SocketCase = tuple[bool, bytes | OSError, bool, bool, type[Exception] | None]


@pytest.mark.parametrize(
    "case",
    [
        (False, b"", False, False, None),
        (True, b"", False, False, None),
        (True, _WindowsReset(10054), False, False, None),
        (False, _WindowsReset(10054), False, False, ConnectionResetError),
        (True, _WindowsReset(64), False, False, ConnectionResetError),
        (True, _WindowsReset(10053), False, False, ConnectionResetError),
        (True, ConnectionResetError(10054, "no winerror"), False, False, ConnectionResetError),
        (True, b"unexpected", False, False, AssertionError),
        (True, OSError("unrelated socket failure"), False, False, OSError),
        (True, _WindowsReset(10054), True, False, psutil.TimeoutExpired),
        (True, _WindowsReset(10054), True, True, RuntimeError),
    ],
)
async def test_cancelled_control_socket_guards(
    monkeypatch: pytest.MonkeyPatch, case: _SocketCase
) -> None:
    # Requirement: only terminal Windows10054 is accepted; every row uses a genuine native owner.
    windows, witness, live, drain_failure, expected = case
    monkeypatch.setattr(sys.modules[__name__], "_WINDOWS", windows)
    primary = RuntimeError("cancellation/drain failed")
    code = "import os, sys; print(os.getpid(), flush=True); sys.stdin.buffer.read()"
    with subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        try:
            assert child.stdout is not None
            identity = await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 5)
            owner = psutil.Process(int(identity))

            async def cancel_and_drain() -> None:
                if drain_failure:
                    raise primary
                if not live:
                    await asyncio.to_thread(child.communicate, timeout=5)

            async def receive() -> bytes:
                match witness:
                    case bytes():
                        return witness
                    case OSError():
                        raise witness
                    case unreachable:
                        assert_never(unreachable)

            if expected is None:
                await _assert_cancelled_control(owner, cancel_and_drain, receive)
            else:
                with pytest.raises(expected) as caught:
                    await _assert_cancelled_control(owner, cancel_and_drain, receive)
                if drain_failure:
                    assert caught.value is primary
                    assert isinstance(caught.value.__cause__, psutil.TimeoutExpired)
        finally:
            with preserve_primary():
                try:
                    child.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate(timeout=5)
                    raise
    assert child.returncode == 0
