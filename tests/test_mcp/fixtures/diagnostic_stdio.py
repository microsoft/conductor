"""JSON-RPC peer emitting adversarial stdout/stderr diagnostics."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import psutil

CANARY = "docker-diagnostic-canary-5bf7"


def diagnostic_peer(receipt: Path) -> int:
    """Record protocol progress without storing the emitted diagnostic bytes."""
    mode = os.environ["PEER_FAILURE"]
    canary = os.environ.get("DIAGNOSTIC_CANARY", CANARY)
    snapshot = {
        "pid": os.getpid(),
        "parent_pids": [parent.pid for parent in psutil.Process().parents()],
        "env": dict(os.environ),
        "called": False,
        "closed": False,
    }
    receipt.write_text(json.dumps(snapshot), encoding="utf-8")
    try:
        if mode == "diagnostic-stderr-startup":
            print(canary, file=sys.stderr, flush=True)
            return 23
        if mode == "diagnostic-stdout-startup":
            print("invalid-json-" + canary, flush=True)
            return 23
        for line in sys.stdin:
            request = json.loads(line)
            method = request.get("method")
            if method == "initialize":
                if mode == "diagnostic-root":
                    print(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": "server-request",
                                "method": "unrecognized-" + canary,
                                "params": {"payload": canary},
                            }
                        ),
                        flush=True,
                    )
                result = {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "diagnostic-peer", "version": "1"},
                }
            elif method == "tools/list":
                result = {
                    "tools": []
                    if mode == "diagnostic-client"
                    else [
                        {
                            "name": "receive",
                            "inputSchema": {"type": "object"},
                        }
                    ]
                }
            elif method == "tools/call":
                snapshot["called"] = True
                receipt.write_text(json.dumps(snapshot), encoding="utf-8")
                if mode == "diagnostic-stderr-call":
                    print(canary, file=sys.stderr, flush=True)
                if mode == "diagnostic-stdout-call":
                    print("invalid-json-" + canary, flush=True)
                    return 23
                if mode == "diagnostic-stderr-call":
                    print(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": request["id"],
                                "error": {"code": -32603, "message": canary},
                            }
                        ),
                        flush=True,
                    )
                    continue
                result = {"content": [{"type": "text", "text": "received"}], "isError": False}
            else:
                continue
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": result,
                    }
                ),
                flush=True,
            )
    finally:
        snapshot["closed"] = True
        receipt.write_text(json.dumps(snapshot), encoding="utf-8")
    return 0
