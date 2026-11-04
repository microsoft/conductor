"""Local JSON-RPC peer for native MCP stdio and installed custody helper tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> None:
    """Serve MCP without credentials, model calls or network access."""
    mode, receipt = sys.argv[1:3]
    initialized = False
    try:
        if mode == "connect-error":
            return
        for line in sys.stdin:
            request = json.loads(line)
            if "id" not in request:
                continue
            response = {"jsonrpc": "2.0", "id": request["id"]}
            match request["method"]:
                case "initialize":
                    initialized = True
                    if mode in ("initialize-error", "secret-error"):
                        response["error"] = {
                            "code": -32001,
                            "message": (
                                os.environ["MCP_MANAGED_OVERLAY"]
                                if mode == "secret-error"
                                else "fixture initialize failed"
                            ),
                        }
                    else:
                        response["result"] = {
                            "protocolVersion": request["params"]["protocolVersion"],
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "managed-fixture", "version": "1"},
                        }
                case "tools/list":
                    response["result"] = {
                        "tools": [
                            {
                                "name": "inspect",
                                "description": "Inspect this fixture's local workspace",
                                "inputSchema": ([] if mode == "malformed" else {"type": "object"}),
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
                                        "file": Path("shared.txt").read_text(),
                                        "argv": sys.argv[3:],
                                        "overlay": os.environ.get("MCP_MANAGED_OVERLAY"),
                                        "ambient": os.environ.get("MCP_MANAGED_AMBIENT"),
                                        "home": str(Path.home()),
                                        "text": request["params"].get("arguments", {}).get("text"),
                                    }
                                ),
                            }
                        ]
                    }
                case _:
                    response["error"] = {"code": -32601, "message": "Unknown method"}
            print(json.dumps(response), flush=True)
    finally:
        Path(receipt).write_text(
            json.dumps({"pid": os.getpid(), "cwd": os.getcwd(), "initialized": initialized}),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
