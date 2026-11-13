"""Stdlib native argv/EOF recorder and hermetic SDK wire peers; no vendor CLI."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path
from types import CodeType

type Json = None | bool | int | float | str | list[Json] | dict[str, Json]


def install_audit() -> None:
    """Observe actual module execution in fresh helpers, including stale-bytecode mismatches."""
    root = Path(os.environ["WINDOWS_PEER_AUDIT"])

    def audit(event: str, args: tuple[CodeType, ...]) -> None:
        if event != "exec":
            return
        code = args[0]
        path = Path(code.co_filename)
        if path.name not in {
            "local_process_scope.py",
            "local_scope_launcher.py",
            "local_windows_launch.py",
        }:
            return
        source = path.read_bytes()
        entrypoint = Path(sys.argv[0])
        value = {
            "pid": os.getpid(),
            "starttime": Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]
            if sys.platform == "linux"
            else None,
            "python": sys.version,
            "platform": sys.platform,
            "windows_version": tuple(sys.getwindowsversion()) if sys.platform == "win32" else None,
            "sdk_versions": {
                name: version(name) for name in ("claude-agent-sdk", "github-copilot-sdk", "mcp")
            },
            "file": str(path),
            "sha256": hashlib.sha256(source).hexdigest(),
            "source_code_equal": code
            == compile(source, code.co_filename, "exec", dont_inherit=True),
            "entrypoint": str(entrypoint),
            "entrypoint_hash": hashlib.sha256(entrypoint.read_bytes()).hexdigest()
            if entrypoint.is_file()
            else None,
        }
        (root / f"{os.getpid()}-{path.stem}.json").write_text(json.dumps(value), encoding="utf-8")

    sys.addaudithook(audit)


def record(event: str, **fields: Json) -> dict[str, Json]:
    """Write only fixture inputs and observed process state, never an ambient environment dump."""
    arguments: list[Json] = list(sys.argv[1:])
    value: dict[str, Json] = {
        "event": event,
        "pid": os.getpid(),
        "starttime": Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]
        if sys.platform == "linux"
        else None,
        "ppid": os.getppid(),
        "argv": arguments,
        "argc": len(sys.argv) - 1,
        "script": sys.argv[0],
        "executable": sys.executable,
        "cwd": os.getcwd(),
        "marker": os.environ.get("WINDOWS_PEER_MARKER"),
        "ambient": os.environ.get("WINDOWS_PEER_AMBIENT"),
        "path": os.environ.get("PATH"),
        "home": os.environ.get("HOME"),
        "profile": os.environ.get("USERPROFILE"),
        "pythonutf8": os.environ.get("PYTHONUTF8"),
        "long_value_length": len(os.environ.get("LONG_VALUE", "")),
        "sdk_token": os.environ.get("COPILOT_SDK_AUTH_TOKEN"),
        **fields,
    }
    if sys.platform == "win32":
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.IsProcessInJob.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        ]
        member = wintypes.BOOL()
        assert kernel.IsProcessInJob(kernel.GetCurrentProcess(), None, ctypes.byref(member))
        value["in_job"] = bool(member.value)
        if handle := os.environ.get("WINDOWS_PEER_EXTRA_HANDLE"):
            kernel.GetFinalPathNameByHandleW.argtypes = [
                wintypes.HANDLE,
                wintypes.LPWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
            ]
            buffer = ctypes.create_unicode_buffer(32768)
            count = kernel.GetFinalPathNameByHandleW(int(handle), buffer, len(buffer), 0)
            value["extra_file"] = buffer.value if count else None
    path = Path(os.environ["WINDOWS_PEER_RECEIPTS"]) / f"{os.getpid()}-{event}.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return value


def send(payload: dict[str, Json], *, framed: bool = False) -> None:
    data = json.dumps(payload).encode()
    sys.stdout.buffer.write(
        f"Content-Length: {len(data)}\r\n\r\n".encode() + data if framed else data + b"\n"
    )
    sys.stdout.buffer.flush()


def claude() -> None:
    if sys.argv[1:] == ["-v"]:
        record("version")
        print("2.1.263 (hermetic fixture)", flush=True)
        return
    if "auth" in sys.argv[1:]:
        record("auth")
        send({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "pro"})
        return
    record("main")
    for line in sys.stdin.buffer:
        request = json.loads(line)
        match request["type"]:
            case "control_request":
                send(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": request["request_id"],
                            "response": {},
                        },
                    }
                )
            case "user":
                send(
                    {
                        "type": "result",
                        "subtype": "success",
                        "duration_ms": 1,
                        "duration_api_ms": 1,
                        "is_error": False,
                        "num_turns": 1,
                        "session_id": "fixture-session",
                        "result": "fixture response",
                        "usage": {"input_tokens": 3, "output_tokens": 2},
                    }
                )
            case _:
                raise AssertionError("Unexpected hermetic Claude request")
    record("eof")


def copilot() -> None:
    record("copilot")
    while True:
        length = 0
        while line := sys.stdin.buffer.readline():
            if line == b"\r\n":
                break
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
        if not line:
            record("eof")
            return
        request = json.loads(sys.stdin.buffer.read(length))
        if "id" not in request:
            continue
        result: dict[str, Json] = {}
        match request["method"]:
            case "connect":
                result = {"ok": True, "protocolVersion": 3, "version": "local-fixture"}
            case "ping":
                result = {
                    "message": request.get("params", {}).get("message"),
                    "timestamp": 1,
                    "protocolVersion": 3,
                }
            case "status.get":
                result = {"version": "local-fixture", "protocolVersion": 3}
            case "runtime.shutdown":
                record("shutdown")
            case _:
                raise AssertionError("Unexpected hermetic Copilot request")
        send({"jsonrpc": "2.0", "id": request["id"], "result": result}, framed=True)


def mcp() -> None:
    observed = record("mcp")
    for line in sys.stdin.buffer:
        request = json.loads(line)
        if "id" not in request:
            continue
        response: dict[str, Json] = {"jsonrpc": "2.0", "id": request["id"]}
        match request["method"]:
            case "initialize":
                response["result"] = {
                    "protocolVersion": request["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "windows-fixture", "version": "1"},
                }
            case "tools/list":
                response["result"] = {
                    "tools": [
                        {
                            "name": "inspect",
                            "description": "Read the fixture workspace",
                            "inputSchema": {"type": "object"},
                        }
                    ]
                }
            case "tools/call":
                response["result"] = {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    **observed,
                                    "file": Path("shared.txt").read_text(encoding="utf-8"),
                                }
                            ),
                        }
                    ]
                }
            case _:
                raise AssertionError("Unexpected hermetic MCP request")
        send(response)
    record("eof")


def main() -> None:
    match os.environ["WINDOWS_PEER_MODE"]:
        case "claude":
            claude()
        case "copilot":
            copilot()
        case "mcp":
            mcp()
        case "raw":
            record("raw")
            sys.stdout.buffer.write(b"ready\n")
            sys.stdout.buffer.flush()
            data = sys.stdin.buffer.read()
            record("eof", stdin_hex=data.hex())
            sys.stdout.buffer.write(b"final:" + data)
            sys.stderr.buffer.write(b"err:\0\r\n")
        case "descendant":
            data = sys.stdin.buffer.read()
            with subprocess.Popen(
                [sys.executable, "-B", __file__],
                env={**os.environ, "WINDOWS_PEER_MODE": "leaf"},
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ) as child:
                child.communicate(data)
                assert child.returncode == 0
            record("descendant", stdin_hex=data.hex())
            sys.stdout.buffer.write(data)
            sys.stderr.buffer.write(b"descendant:\0\r\n")
        case "leaf":
            record("leaf", stdin_hex=sys.stdin.buffer.read().hex())
        case "record":
            value = record("record", stdin_hex=sys.stdin.buffer.read().hex())
            send(value)
            sys.exit(int(os.environ.get("WINDOWS_PEER_EXIT", "0")))
        case _:
            raise AssertionError("Unknown hermetic peer mode")


if __name__ == "__main__":
    main()
