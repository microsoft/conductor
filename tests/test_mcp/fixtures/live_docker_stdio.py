"""Dependency-free stdio peer for real Docker workspace and lifecycle tests."""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path
from typing import Final

TRACE: Final = Path("/workspace/peer-history.jsonl")


def record(event: str) -> None:
    """Persist progress independently of the controller's protocol diagnostics."""
    with TRACE.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps({"event": event, "container": os.environ["HOSTNAME"], "cwd": os.getcwd()})
            + "\n"
        )


def main() -> int:
    """Answer JSON-RPC requests and expose deterministic lifecycle failure modes."""
    mode = os.environ.get("PEER_MODE", "cooperative")
    if mode == "uncooperative":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        for line in sys.stdin:
            request = json.loads(line)
            match request.get("method"):
                case "initialize":
                    record("initialize")
                    if mode == "startup-secret":
                        Path("/workspace/initialize-mutated.txt").write_text(
                            "initialize-mutated\n", encoding="utf-8"
                        )
                        record("startup-stderr-emitted")
                        print(os.environ["PAYLOAD_TOKEN"], file=sys.stderr, flush=True)
                        return 23
                    result = {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "live-docker-peer", "version": "1"},
                    }
                case "tools/list":
                    result = {
                        "tools": [
                            {
                                "name": "mutate",
                                "description": "Read and append to a relative workspace file.",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"path": {"type": "string"}},
                                },
                            }
                        ]
                    }
                case "tools/call":
                    record("call")
                    arguments = request["params"]["arguments"]
                    once = Path("/workspace/call-refused-once")
                    if arguments.get("fail_once") and not once.exists():
                        once.touch()
                        print(
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": request["id"],
                                    "error": {"code": -32603, "message": "first call refused"},
                                }
                            ),
                            flush=True,
                        )
                        continue
                    match mode:
                        case "transport":
                            return 24
                        case "call-secret":
                            record("call-stderr-emitted")
                            print(os.environ["PAYLOAD_TOKEN"], file=sys.stderr, flush=True)
                            print(
                                json.dumps(
                                    {
                                        "jsonrpc": "2.0",
                                        "id": request["id"],
                                        "error": {"code": -32603, "message": "peer refused call"},
                                    }
                                ),
                                flush=True,
                            )
                            continue
                        case "timeout" | "uncooperative":
                            # A signal, rather than a timed sleep, holds the real server call.
                            signal.pause()
                            continue
                        case "cooperative" | "is-error":
                            path = Path(arguments["path"])
                            previous = path.read_text(encoding="utf-8")
                            path.write_text(previous + "mcp\n", encoding="utf-8")
                            result = {
                                "content": [{"type": "text", "text": previous + "mcp\n"}],
                                "isError": mode == "is-error",
                            }
                        case _:
                            raise RuntimeError("Unknown peer fixture mode")
                case _:
                    # Notifications have no response, including initialized and cancelled.
                    continue
            print(
                json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}),
                flush=True,
            )
        if mode == "uncooperative":
            signal.pause()
    finally:
        record("closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
