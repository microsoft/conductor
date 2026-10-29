"""HTTP loopback contract for the in-image stdlib bridge."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest


@pytest.mark.asyncio
async def test_bridge_streams_http_event_before_terminal_frame() -> None:
    # Requirement: the bridge forwards each HTTP NDJSON line without buffering the result.
    release = threading.Event()
    request_body: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            request_body.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            self.wfile.write(b'{"type":"agent_message","data":{"text":"first"}}\n')
            self.wfile.flush()
            assert release.wait(20)
            self.wfile.write(b'{"type":"result","data":{"content":{"answer":"ok"}}}\n')
            self.wfile.flush()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "conductor.runner.bridge",
        "--port",
        str(server.server_port),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "http_proxy": "http://127.0.0.1:1", "no_proxy": ""},
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(b'{"agent":{"name":"agent"}}')
        await process.stdin.drain()
        process.stdin.close()
        assert json.loads(await asyncio.wait_for(process.stdout.readline(), 20)) == {
            "type": "bridge_ready",
            "data": {},
        }
        event = json.loads(await asyncio.wait_for(process.stdout.readline(), 20))
        assert event == {"type": "agent_message", "data": {"text": "first"}}
        assert process.returncode is None
        release.set()
        result = json.loads(await asyncio.wait_for(process.stdout.readline(), 20))
        assert result["data"]["content"] == {"answer": "ok"}
        assert await asyncio.wait_for(process.wait(), 20) == 0
        assert request_body == [{"agent": {"name": "agent"}}]
    finally:
        release.set()
        if process.returncode is None:
            process.kill()
            await process.wait()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_bridge_preserves_runner_preflight_error() -> None:
    # Requirement: an HTTP 400 error survives the bridge as a terminal error frame.
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            payload = b'{"error":{"message":"stdio binary not installed"}}'
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "conductor.runner.bridge",
            "--port",
            str(server.server_port),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, stderr = await asyncio.wait_for(process.communicate(b"{}"), 20)
        assert process.returncode == 0 and stderr == b""
        assert json.loads(output) == {
            "type": "error",
            "data": {"message": "stdio binary not installed"},
        }
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_bridge_ready_follows_execute_registration_before_targeted_interrupt() -> None:
    # Requirement: an early 404 cannot lose an interrupt once the execute bridge reports ready.
    registered = threading.Event()
    release = threading.Event()
    interrupted: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/interrupt":
                if not registered.is_set():
                    self.send_error(404)
                    return
                interrupted.append(json.loads(body)["execution_id"])
                self.send_response(200)
                self.end_headers()
                release.set()
                return
            assert self.path == "/execute"
            assert json.loads(body) == {"execution_id": "exec-1"}
            registered.set()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            assert release.wait(20)
            self.wfile.write(b'{"type":"result","data":{"content":{}}}\n')
            self.wfile.flush()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    execute: asyncio.subprocess.Process | None = None

    async def interrupt() -> tuple[int | None, bytes, bytes]:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "conductor.runner.bridge",
            "--port",
            str(server.server_port),
            "--interrupt",
            "exec-1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, stderr = await asyncio.wait_for(process.communicate(), 20)
        return process.returncode, output, stderr

    try:
        assert await interrupt() == (1, b"", f"runner interrupt HTTP 404{os.linesep}".encode())
        execute = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "conductor.runner.bridge",
            "--port",
            str(server.server_port),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert execute.stdin is not None and execute.stdout is not None
        execute.stdin.write(b'{"execution_id":"exec-1"}')
        await execute.stdin.drain()
        execute.stdin.close()
        assert json.loads(await asyncio.wait_for(execute.stdout.readline(), 20)) == {
            "type": "bridge_ready",
            "data": {},
        }
        assert await interrupt() == (0, b"", b"")
        assert json.loads(await asyncio.wait_for(execute.stdout.readline(), 20))["type"] == "result"
        assert await asyncio.wait_for(execute.wait(), 20) == 0
        assert interrupted == ["exec-1"]
    finally:
        release.set()
        if execute is not None and execute.returncode is None:
            execute.kill()
            await execute.wait()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(("status", "expected_exit"), [(409, 3), (401, 1), (404, 1), (500, 1)])
async def test_bridge_interrupt_exit_distinguishes_conflict_from_failure(
    status: int, expected_exit: int
) -> None:
    # Requirement: the host recognizes terminal conflicts by exit code, never stderr prose.
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_error(status)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "conductor.runner.bridge",
            "--port",
            str(server.server_port),
            "--interrupt",
            "completed",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
        assert process.returncode == expected_exit
        assert stdout == b""
        assert str(status).encode() in stderr
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=5)
