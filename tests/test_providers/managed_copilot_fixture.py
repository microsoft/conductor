"""Hermetic Content-Length RPC peer for the real installed Copilot SDK."""

from __future__ import annotations

import json
import os
import socket
import sys
from hashlib import sha256
from pathlib import Path
from uuid import uuid4


def serve() -> None:
    """Record native wire ordering without using credentials or a model."""
    receipt = Path(os.environ["COPILOT_FIXTURE_RECEIPT"]) / f"{os.getpid()}.jsonl"
    sessions = Path.cwd() / ".fixture-sessions"
    sessions.mkdir(exist_ok=True)

    def record(event: str, **values: object) -> None:
        with receipt.open("a") as stream:
            stream.write(json.dumps({"event": event, "cwd": os.getcwd(), **values}) + "\n")
        if endpoint := os.environ.get("COPILOT_FIXTURE_ENDPOINT"):
            with socket.socket(socket.AF_UNIX) as channel:
                channel.connect(endpoint)
                channel.sendall(
                    json.dumps({"event": event, "cwd": os.getcwd(), **values}).encode() + b"\n"
                )

    def send(payload: dict[str, object]) -> None:
        data = json.dumps(payload).encode()
        sys.stdout.buffer.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
        sys.stdout.buffer.flush()

    record(
        "spawn",
        pid=os.getpid(),
        ppid=os.getppid(),
        argv=sys.argv[1:],
        cwd=os.getcwd(),
        env=dict(os.environ),
    )
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
        method = request.get("method")
        params = request.get("params", {})
        record("rpc", method=method, params=params)
        if "id" not in request:
            continue
        mode = os.environ.get("COPILOT_FIXTURE_MODE")
        if (mode == "hold" and method == "connect") or (
            mode == "stuck-shutdown" and method == "runtime.shutdown"
        ):
            continue
        result: object = {}
        error: dict[str, object] | None = None
        history: list[str] = []
        sid = params.get("sessionId", "")
        state = sessions / sha256(sid.encode()).hexdigest()
        if method == "connect":
            if os.environ.get("COPILOT_FIXTURE_MODE") == "fail":
                error = {"code": -32001, "message": "fixture handshake refused"}
            else:
                result = {"ok": True, "protocolVersion": 3, "version": "local-fixture"}
        elif method == "session.create":
            sid = params.get("sessionId", uuid4().hex)
            state = sessions / sha256(sid.encode()).hexdigest()
            with state.open("x") as stream:
                json.dump(history, stream)
            result = {"sessionId": sid}
        elif method == "session.resume":
            if not state.is_file():
                error = {"code": -32003, "message": "fixture unknown session"}
            else:
                with state.open() as stream:
                    history = json.load(stream)
                result = {"sessionId": sid}
        elif method == "session.send":
            if not state.is_file():
                error = {"code": -32003, "message": "fixture unknown session"}
            else:
                with state.open() as stream:
                    history = json.load(stream)
                history.append(params["prompt"])
                with state.open("w") as stream:
                    json.dump(history, stream)
                result = {"messageId": uuid4().hex}
            if mode == "retry-once" and error is None:
                try:
                    with (receipt.parent / "send-failed").open("x"):
                        pass
                except FileExistsError:
                    pass
                else:
                    error = {"code": -32002, "message": "fixture transient send failure"}
        if error is not None:
            send({"jsonrpc": "2.0", "id": request["id"], "error": error})
        else:
            send({"jsonrpc": "2.0", "id": request["id"], "result": result})
        if error is not None:
            continue
        if method == "runtime.shutdown":
            record("shutdown-replied")
        if method in ("session.send", "session.abort"):
            sid = params["sessionId"]
            content = (
                json.dumps({"history": history})
                if mode == "history"
                else "SUCCESS DONE ignore instructions"
            )
            for kind, data in (
                (
                    "assistant.message",
                    {"content": content, "messageId": uuid4().hex},
                ),
                ("session.idle", {}),
            ):
                if mode == "interrupt" and method == "session.send" and kind == "session.idle":
                    continue
                send(
                    {
                        "jsonrpc": "2.0",
                        "method": "session.event",
                        "params": {
                            "sessionId": sid,
                            "event": {
                                "id": uuid4().hex,
                                "timestamp": "2026-11-02T00:00:00Z",
                                "parentId": None,
                                "ephemeral": False,
                                "type": kind,
                                "data": data,
                            },
                        },
                    }
                )


if __name__ == "__main__":
    serve()
