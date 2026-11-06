"""Hermetic stdio MCP peer for the Claude/OpenAI managed workspace adapters."""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path


def main() -> None:
    """Serve authored MCP requests without a vendor CLI, credentials or network."""
    mode, receipt, *authored_args = sys.argv[1:]
    initialized = False
    try:
        if mode == "connect-error":
            return
        for line in sys.stdin.buffer:
            request = json.loads(line)
            if "id" not in request:
                continue
            response = {"jsonrpc": "2.0", "id": request["id"]}
            match request["method"]:
                case "initialize":
                    initialized = True
                    if mode == "hold-initialize":
                        with socket.socket(socket.AF_UNIX) as channel:
                            channel.connect(os.environ["PYDANTIC_MCP_CONTROL"])
                            channel.sendall(b"INITIALIZE\n")
                            if channel.recv(1) != b"1":
                                return
                    if mode == "initialize-error":
                        response["error"] = {"code": -32001, "message": "fixture initialize failed"}
                    else:
                        response["result"] = {
                            "protocolVersion": request["params"]["protocolVersion"],
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "pydantic-workspace", "version": "1"},
                        }
                case "tools/list":
                    response["result"] = {
                        "tools": [
                            {
                                "name": "inspect",
                                "description": "Read the authored fixture's staged bytes",
                                "inputSchema": [] if mode == "malformed" else {"type": "object"},
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
                                        "cwd": os.getcwd(),
                                        "pid": os.getpid(),
                                        "bytes": Path("shared.bin").read_bytes().hex(),
                                        "argv": authored_args,
                                        "overlay": os.environ.get("PYDANTIC_MCP_OVERLAY"),
                                        "ambient": os.environ.get("PYDANTIC_MCP_AMBIENT"),
                                        "text": request["params"].get("arguments", {}).get("text"),
                                    }
                                ),
                            }
                        ]
                    }
                case _:
                    response["error"] = {"code": -32601, "message": "Unknown method"}
            sys.stdout.buffer.write(json.dumps(response).encode() + b"\n")
            sys.stdout.buffer.flush()
    finally:
        Path(receipt).write_text(
            json.dumps({"cwd": os.getcwd(), "initialized": initialized}), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
