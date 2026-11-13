#!/usr/bin/env python3
"""Hermetic stream-json CLI fixture for the real installed Claude SDK transport."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict
from uuid import uuid4

if TYPE_CHECKING:
    import pytest

    from conductor.config.schema import AgentDef
    from conductor.execution.local_process_scope import LocalProcessScopeController
    from conductor.execution.local_scope_launcher import Json
    from conductor.execution.types import ManagedWorkspaceContext

_NATIVE_SPAWN = asyncio.create_subprocess_exec


class FixtureEvent(TypedDict):
    event: str
    pid: int
    argv: list[str]
    cwd: str
    home: str
    prompt: str


class NativeHarness:
    """Own fixture IPC, helper processes, isolated homes and event subscriptions."""

    def __init__(self, directory: Path) -> None:
        from tests.test_execution.test_local_process_scope import ControlledClock

        self.directory = directory
        self.cli = directory / "fixture.py"
        self.work = directory / "work"
        self.clock = ControlledClock()
        self.events: list[FixtureEvent] = []
        self.waiters: list[tuple[str, asyncio.Future[FixtureEvent]]] = []
        self.handlers: set[asyncio.Task[None]] = set()
        self.processes: list[asyncio.subprocess.Process] = []
        self.controller: LocalProcessScopeController
        self.context: ManagedWorkspaceContext
        self.auth_gate = asyncio.Event()

    def expect(self, name: str) -> asyncio.Future[FixtureEvent]:
        future: asyncio.Future[FixtureEvent] = asyncio.get_running_loop().create_future()
        for event in self.events:
            if event["event"] == name:
                future.set_result(event)
                return future
        self.waiters.append((name, future))
        return future

    async def receive(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.handlers.add(task)
        try:
            event = json.loads(await reader.readline())
            self.events.append(event)
            for name, future in self.waiters:
                if name == event["event"] and not future.done():
                    future.set_result(event)
            if event["event"] == "auth":
                await self.auth_gate.wait()
            writer.write(b"1")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            self.handlers.discard(task)

    def agent(self, *, keyed: bool = False) -> AgentDef:
        from conductor.config.schema import AgentDef

        return AgentDef(
            name="native",
            prompt="hello",
            working_dir=str(self.work),
            session_key="conversation" if keyed else None,
            timeout_seconds=None,
            max_session_seconds=None,
            max_agent_iterations=None,
        )


@contextlib.asynccontextmanager
async def native_harness(
    directory: Path, monkeypatch: pytest.MonkeyPatch, mode: str = "success"
) -> AsyncIterator[NativeHarness]:
    from conductor.execution.local_process_scope import LocalProcessScopeController
    from conductor.execution.types import ManagedWorkspaceContext
    from conductor.providers import claude_agent_sdk as adapter

    h = NativeHarness(directory)
    if mode != "hang_auth":
        h.auth_gate.set()
    directory.mkdir(parents=True)
    h.work.mkdir()
    shutil.copyfile(Path(__file__), h.cli)
    h.cli.chmod(0o700)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    endpoint = f"/proc/{os.getpid()}/fd/{fd}/events"
    server = await asyncio.start_unix_server(h.receive, path=endpoint)
    (directory / "config.json").write_text(
        json.dumps({"endpoint": endpoint, "mode": mode, "sid": str(uuid4())})
    )
    for name in tuple(os.environ):
        monkeypatch.delenv(name)
    for name, value in {
        "PATH": f"{Path(sys.executable).parent}:/usr/bin",
        "HOME": str(directory / "home"),
        "USERPROFILE": str(directory / "home"),
        "XDG_CONFIG_HOME": str(directory / "home/config"),
        "XDG_CACHE_HOME": str(directory / "home/cache"),
        "CLAUDE_CONFIG_DIR": str(directory / "claude"),
        "TMPDIR": str(directory),
        "PYTHONPYCACHEPREFIX": str(directory / "pycache"),
        "COPILOT_SKIP_CLI_DOWNLOAD": "1",
    }.items():
        monkeypatch.setenv(name, value)

    async def fixture_only(*args, **kwargs) -> asyncio.subprocess.Process:
        command = Path(args[0])
        assert command.name == "conductor-local-scope"
        assert ".launchers" in command.parts or ".scopes" in command.parts
        assert command.is_relative_to(directory.parent)
        process = await _NATIVE_SPAWN(*args, **kwargs)
        h.processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fixture_only)
    monkeypatch.setattr(adapter, "_find_claude_cli", lambda: h.cli)
    h.controller = LocalProcessScopeController(str(directory), "native-inc", _scheduler=h.clock)
    h.context = ManagedWorkspaceContext(str(directory), "native-inc", h.controller)
    test_task = asyncio.current_task()
    assert test_task is not None

    def expire() -> None:
        h.controller.begin_teardown()
        h.clock.advance(31)
        test_task.cancel()

    guard = asyncio.get_running_loop().call_later(15, expire)
    try:
        yield h
    finally:
        guard.cancel()
        h.auth_gate.set()
        h.controller.begin_teardown()
        h.clock.advance(20)
        result = await h.controller.await_release()
        if result.status == "unconfirmed":
            await h.controller.force_stop(tuple(proof.scope_id for proof in h.controller.proofs))
        async with asyncio.timeout(10):
            # Queueing force is not a join. Reap stable native handles before
            # closing IPC, even when the terminal decision is already uncertain.
            for process in h.processes:
                await process.wait()
            await h.controller.close()
            if h.handlers:
                await asyncio.gather(*h.handlers)
        server.close()
        await server.wait_closed()
        os.close(fd)
        for _, future in h.waiters:
            future.cancel()


def main() -> None:
    directory = Path(__file__).parent
    config = json.loads((directory / "config.json").read_text())

    def event(name: str, **fields: str | int | list[str]) -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.connect(config["endpoint"])
            channel.sendall(
                json.dumps(
                    {"event": name, "pid": os.getpid(), "argv": sys.argv[1:], **fields}
                ).encode()
                + b"\n"
            )
            channel.recv(1)

    def output(value: dict[str, Json]) -> None:
        print(json.dumps(value), flush=True)

    if sys.argv[1:] == ["-v"]:
        event("version", cwd=os.getcwd(), home=os.environ["HOME"])
        print("2.1.263 (Hermetic Claude fixture)", flush=True)
        if config["mode"] == "probe_eof":
            # SDK facade termination must not kill this payload or custodian.
            sys.stdin.read()
            (directory / f"version-eof-{os.getpid()}").write_bytes(b"version-final-bytes")
            event("version_eof")
        return
    if "auth" in sys.argv[1:]:
        event("auth", cwd=os.getcwd(), home=os.environ["HOME"])
        if config["mode"] == "malformed_auth":
            print("malformed auth status", flush=True)
            return
        output({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "pro"})
        return

    event("main", cwd=os.getcwd(), home=os.environ["HOME"])
    sid = config["sid"]
    for argument in sys.argv[1:]:
        if argument.startswith("--resume="):
            sid = argument.removeprefix("--resume=")
    for line in sys.stdin:
        message = json.loads(line)
        match message["type"]:
            case "control_request":
                event("initialize")
                if config["mode"] == "hang_connect":
                    continue
                response = {
                    "subtype": "success",
                    "request_id": message["request_id"],
                    "response": {},
                }
                if config["mode"] == "fail_connect":
                    response = {
                        "subtype": "error",
                        "request_id": message["request_id"],
                        "error": "fixture initialize refused",
                    }
                output({"type": "control_response", "response": response})
            case "user":
                event("prompt", prompt=json.dumps(message["message"]))
                if config["mode"] == "hang_response":
                    continue
                if config["mode"] == "malformed":
                    output({"type": "result", "session_id": sid})
                    continue
                output(
                    {
                        "type": "result",
                        "subtype": "success",
                        "duration_ms": 1,
                        "duration_api_ms": 1,
                        "is_error": False,
                        "num_turns": 1,
                        "session_id": sid,
                        "result": "fixture response",
                        "usage": {"input_tokens": 3, "output_tokens": 2},
                    }
                )
            case _:
                raise AssertionError(f"Unexpected fixture message: {message['type']}")

    # These bytes exist only if native disconnect delivered real stdin EOF.
    (directory / f"eof-{os.getpid()}").write_bytes(b"final-session-bytes")
    from claude_agent_sdk import project_key_for_directory

    transcript = (
        Path(os.environ["CLAUDE_CONFIG_DIR"])
        / "projects"
        / project_key_for_directory(os.getcwd())
        / f"{sid}.jsonl"
    )
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(json.dumps({"sessionId": sid, "cwd": os.getcwd()}) + "\n")
    print('{"type":"system","subtype":"fixture_eof"}', flush=True)
    event("eof")


if __name__ == "__main__":
    main()
