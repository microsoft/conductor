"""Daemon-free Docker agent realm contract tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from conductor.bundle.collector import collect_bundle
from conductor.exceptions import ConfigurationError, ProviderError
from conductor.execution import docker_agent
from conductor.execution.docker import DockerRunnerBackend
from conductor.execution.docker_paths import map_agent_paths
from conductor.execution.types import (
    AgentResult,
    AgentSpec,
    BundleRef,
    ResolvedExecutionSpec,
    RunSpec,
)


def _bundle(tmp_path: Path) -> BundleRef:
    store = tmp_path / "bundle"
    (store / "tree/main").mkdir(parents=True)
    (store / "bundle.json").write_text(
        json.dumps({"entries": [], "skills_topology": {}, "plugins_topology": {}}),
        encoding="utf-8",
    )
    return BundleRef("sha256:test", str(store), source_roots=((str(tmp_path), "main"),))


def _spec(image: str = "runner:2") -> AgentSpec:
    return AgentSpec(
        name="agent",
        execution_id="exec-1",
        model_provider="copilot",
        model=None,
        rendered_prompt="work",
        execution=ResolvedExecutionSpec(image=image),
        provider_credentials={"api_key": "never-in-argv"},
        env_overlay={"TOKEN": "secret-overlay"},
    )


def _rows(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_missing_bundle_fails_before_docker_io(
    fake_realm: tuple[DockerRunnerBackend, Path, Path],
) -> None:
    # Requirement: a Docker agent cannot start an unstaged runner container.
    backend, log, _state = fake_realm
    lease = await backend.prepare_run(RunSpec(run_id="run_ABC-1"))
    with pytest.raises(ConfigurationError, match="published run bundle"):
        await backend.run_agent(_spec(), lease)
    assert not log.exists()


@pytest.mark.asyncio
async def test_runner_reuses_container_and_cleans_all_resources(
    fake_realm: tuple[DockerRunnerBackend, Path, Path], tmp_path: Path
) -> None:
    # Requirement: calls share a lease-owned runner; finalize leaves no labeled resources.
    backend, log, state = fake_realm
    lease = await backend.prepare_run(RunSpec(run_id="run_ABC-1", bundle=_bundle(tmp_path)))
    assert backend.capabilities().interrupt is False
    assert (await backend.run_agent(_spec(), lease)).content == {"answer": "agent"}
    assert backend.capabilities().interrupt is True
    assert (await backend.run_agent(_spec(), lease)).content == {"answer": "agent"}
    creates = [
        row
        for row in _rows(log)
        if row["argv"][0] == "create" and "io.conductor.resource=runner" in row["argv"]
    ]
    assert len(creates) == 1
    argv = creates[0]["argv"]
    assert argv[-5:] == ["--entrypoint", "python", "runner:2", "-m", "conductor.aca_runner"]
    assert "ACA_RUNNER_HOST=127.0.0.1" in argv
    assert "--publish" not in argv and "-p" not in argv and "--env-file" not in argv
    assert "secret-overlay" not in json.dumps(argv) and "never-in-argv" not in json.dumps(argv)
    assert any(
        row["argv"][:2] == ["exec", "-i"] and "--health" in row["argv"] for row in _rows(log)
    )
    await backend.finalize_run(lease, "succeeded")
    await backend.finalize_run(lease, "succeeded")
    assert backend.capabilities().interrupt is False
    assert json.loads(state.read_text(encoding="utf-8"))["containers"] == {}


@pytest.mark.asyncio
async def test_realm_handshake_rejects_old_image_before_execute(
    fake_realm: tuple[DockerRunnerBackend, Path, Path], tmp_path: Path
) -> None:
    # Requirement: a v1 image is refused at startup and its container removed.
    backend, log, state = fake_realm
    backend._cli_env["FAKE_DOCKER_PROTOCOL_VERSION"] = "1"
    lease = await backend.prepare_run(RunSpec(run_id="run_ABC-1", bundle=_bundle(tmp_path)))
    with pytest.raises(ConfigurationError, match="runner:2.*protocol 1.*Rebuild"):
        await backend.run_agent(_spec(), lease)
    assert not any(
        row["argv"][:2] == ["exec", "-i"] and "--health" not in row["argv"] for row in _rows(log)
    )
    assert json.loads(state.read_text(encoding="utf-8"))["containers"] == {}


@pytest.mark.asyncio
async def test_realm_handshake_names_bridge_less_image_and_rebuild(
    fake_realm: tuple[DockerRunnerBackend, Path, Path], tmp_path: Path
) -> None:
    # Requirement: a pre-bridge image fails with its image name and rebuild remedy.
    backend, log, state = fake_realm
    backend._cli_env["FAKE_DOCKER_SCENARIO"] = "realm-no-bridge"
    lease = await backend.prepare_run(RunSpec(run_id="run_ABC-1", bundle=_bundle(tmp_path)))
    with pytest.raises(ConfigurationError, match="runner:2.*bridge.*Rebuild"):
        await backend.run_agent(_spec(), lease)
    assert not any("--interrupt" in row["argv"] for row in _rows(log))
    assert json.loads(state.read_text(encoding="utf-8"))["containers"] == {}


@pytest.mark.asyncio
async def test_realm_handshake_keeps_daemon_failure_distinct(
    fake_realm: tuple[DockerRunnerBackend, Path, Path], tmp_path: Path
) -> None:
    # Requirement: a failed Docker daemon connection is not diagnosed as an old image.
    backend, _log, state = fake_realm
    backend._cli_env["FAKE_DOCKER_SCENARIO"] = "realm-daemon-error"
    lease = await backend.prepare_run(RunSpec(run_id="run_ABC-1", bundle=_bundle(tmp_path)))
    with pytest.raises(ConfigurationError, match="Docker daemon") as failure:
        await backend.run_agent(_spec(), lease)
    assert "Rebuild" not in str(failure.value)
    assert json.loads(state.read_text(encoding="utf-8"))["containers"] == {}


@pytest.mark.asyncio
async def test_realm_interrupt_capability_requires_health_feature(
    fake_realm: tuple[DockerRunnerBackend, Path, Path],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Requirement: a feature-less v2 runner never claims graceful interrupt support.
    backend, _log, _state = fake_realm
    backend._cli_env["FAKE_DOCKER_FEATURES"] = ""
    assert backend.capabilities().interrupt is False
    lease = await backend.prepare_run(RunSpec(run_id="run_ABC-1", bundle=_bundle(tmp_path)))
    signal = asyncio.Event()
    for _ in range(2):
        assert (await backend.run_agent(_spec(), lease, interrupt_signal=signal)).content == {
            "answer": "agent"
        }
    assert backend.capabilities().interrupt is False
    assert sum("does not advertise interrupt" in row.message for row in caplog.records) == 1
    await backend.finalize_run(lease, "succeeded")


@pytest.mark.asyncio
async def test_realm_without_interrupt_feature_aborts_read_without_watcher(
    fake_realm: tuple[DockerRunnerBackend, Path, Path],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Requirement: feature-less runners locally abort an interrupt without an unsupported exec.
    backend, log, _state = fake_realm
    backend._cli_env["FAKE_DOCKER_FEATURES"] = ""
    connected = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connected.set()
        await reader.read(1)
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    task: asyncio.Task[AgentResult] | None = None
    try:
        backend._cli_env["FAKE_DOCKER_RELEASE_PORT"] = str(server.sockets[0].getsockname()[1])
        lease = await backend.prepare_run(RunSpec(run_id="run_ABC-1", bundle=_bundle(tmp_path)))
        signal = asyncio.Event()
        task = asyncio.create_task(backend.run_agent(_spec(), lease, interrupt_signal=signal))
        await asyncio.wait_for(connected.wait(), 20)
        assert not task.done()
        signal.set()
        result = await asyncio.wait_for(task, 20)
        assert result.partial is True and result.content == {}
        assert backend.capabilities().interrupt is False
        assert not any("--interrupt" in row["argv"] for row in _rows(log))
        assert sum("does not advertise interrupt" in row.message for row in caplog.records) == 1
        await backend.finalize_run(lease, "succeeded")
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_agent_error_frame_scrubs_credentials(
    fake_realm: tuple[DockerRunnerBackend, Path, Path], tmp_path: Path
) -> None:
    # Requirement: no wire credential value may surface in host diagnostics.
    backend, _log, _state = fake_realm
    backend._cli_env["FAKE_DOCKER_SCENARIO"] = "realm-error"
    lease = await backend.prepare_run(RunSpec(run_id="run_ABC-1", bundle=_bundle(tmp_path)))
    with pytest.raises(ProviderError) as failure:
        await backend.run_agent(_spec(), lease)
    assert "secret-overlay" not in str(failure.value)
    assert "[REDACTED]" in str(failure.value)
    await backend.finalize_run(lease, "failed")


@pytest.mark.asyncio
async def test_malformed_ndjson_is_rejected(
    fake_realm: tuple[DockerRunnerBackend, Path, Path], tmp_path: Path
) -> None:
    # Requirement: an unparseable runner frame fails before a fabricated result can escape.
    backend, _log, _state = fake_realm
    backend._cli_env["FAKE_DOCKER_SCENARIO"] = "realm-malformed"
    lease = await backend.prepare_run(RunSpec(run_id="run_ABC-1", bundle=_bundle(tmp_path)))
    with pytest.raises(ConfigurationError, match="malformed NDJSON"):
        await backend.run_agent(_spec(), lease)
    await backend.finalize_run(lease, "failed")


@pytest.mark.asyncio
async def test_truncated_runner_stream_is_not_a_result(
    fake_realm: tuple[DockerRunnerBackend, Path, Path], tmp_path: Path
) -> None:
    # Requirement: an event stream without one terminal frame cannot report success.
    backend, _log, _state = fake_realm
    backend._cli_env["FAKE_DOCKER_SCENARIO"] = "realm-truncated"
    lease = await backend.prepare_run(RunSpec(run_id="run_ABC-1", bundle=_bundle(tmp_path)))
    with pytest.raises(ConfigurationError, match="without a terminal frame"):
        await backend.run_agent(_spec(), lease)
    await backend.finalize_run(lease, "failed")


@pytest.mark.asyncio
async def test_runner_identity_includes_security_and_resource_options(
    fake_realm: tuple[DockerRunnerBackend, Path, Path], tmp_path: Path
) -> None:
    # Requirement: distinct container options get distinct lease runners, each fully configured.
    backend, log, state = fake_realm
    lease = await backend.prepare_run(RunSpec(run_id="run_ABC-1", bundle=_bundle(tmp_path)))
    assert (await backend.run_agent(_spec(), lease)).content == {"answer": "agent"}
    hardened = replace(
        _spec(),
        execution=ResolvedExecutionSpec(
            image="runner:2",
            network="none",
            user="1000",
            init=True,
            read_only=True,
            cap_drop_all=True,
            no_new_privileges=True,
            tmpfs="128m",
            cpu=1.5,
            memory="512m",
            pids=100,
        ),
    )
    assert (await backend.run_agent(hardened, lease)).content == {"answer": "agent"}
    runners = [
        row["argv"]
        for row in _rows(log)
        if row["argv"][0] == "create" and "io.conductor.resource=runner" in row["argv"]
    ]
    assert len(runners) == 2
    argv = runners[1]
    for flag in ("--init", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges"):
        assert flag in argv
    for flag, value in (
        ("--network", "none"),
        ("--user", "1000"),
        ("--tmpfs", "/tmp:size=128m"),
        ("--cpus", "1.5"),
        ("--memory", "512m"),
        ("--pids-limit", "100"),
    ):
        assert argv[argv.index(flag) + 1] == value
    await backend.finalize_run(lease, "succeeded")
    assert json.loads(state.read_text(encoding="utf-8"))["containers"] == {}


@pytest.mark.asyncio
async def test_concurrent_agent_calls_use_exclusive_runners(
    fake_realm: tuple[DockerRunnerBackend, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: overlapping calls own separate runners and neither blocks the other.
    backend, log, _state = fake_realm
    original = backend._run_docker
    state_lock = asyncio.Lock()

    async def serialized_docker(argv, **kwargs):
        # The file-backed fake must serialize writes as a real daemon does.
        async with state_lock:
            return await original(argv, **kwargs)

    monkeypatch.setattr(backend, "_run_docker", serialized_docker)
    both_events = asyncio.Event()
    released = asyncio.Event()
    received: list[str] = []

    async def handle(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await released.wait()
        writer.write(b"1")
        await writer.drain()
        writer.close()

    def on_event(kind: str, _data: Mapping[str, Any]) -> None:
        received.append(kind)
        if len(received) == 2:
            both_events.set()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    tasks: list[asyncio.Task[AgentResult]] = []
    try:
        backend._cli_env["FAKE_DOCKER_RELEASE_PORT"] = str(server.sockets[0].getsockname()[1])
        lease = await backend.prepare_run(RunSpec(run_id="run_ABC-1", bundle=_bundle(tmp_path)))
        tasks = [
            asyncio.create_task(
                backend.run_agent(
                    replace(_spec(), execution_id=f"exec-{index}"), lease, on_event=on_event
                )
            )
            for index in (1, 2)
        ]
        await asyncio.wait_for(both_events.wait(), 20)
        assert all(not task.done() for task in tasks)
        released.set()
        assert [result.content for result in await asyncio.gather(*tasks)] == [
            {"answer": "agent"},
            {"answer": "agent"},
        ]
        assert (
            len(
                [
                    row
                    for row in _rows(log)
                    if row["argv"][0] == "create" and "io.conductor.resource=runner" in row["argv"]
                ]
            )
            == 2
        )
        await backend.finalize_run(lease, "succeeded")
    finally:
        released.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_cancelling_one_call_preserves_concurrent_call_and_its_runner(
    fake_realm: tuple[DockerRunnerBackend, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: cancellation retires its runner, not the concurrently active sibling's.
    backend, log, state = fake_realm
    original = backend._run_docker
    state_lock = asyncio.Lock()

    async def serialized_docker(argv, **kwargs):
        # The file-backed fake must serialize writes as a real daemon does.
        async with state_lock:
            return await original(argv, **kwargs)

    monkeypatch.setattr(backend, "_run_docker", serialized_docker)
    received: set[str] = set()
    both_streaming = asyncio.Event()
    release = asyncio.Event()

    async def handle(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await release.wait()
        writer.write(b"1")
        await writer.drain()
        writer.close()

    def event_for(execution_id: str):
        def record(_kind: str, _data: Mapping[str, Any]) -> None:
            received.add(execution_id)
            if len(received) == 2:
                both_streaming.set()

        return record

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    tasks: list[asyncio.Task[AgentResult]] = []
    try:
        backend._cli_env["FAKE_DOCKER_RELEASE_PORT"] = str(server.sockets[0].getsockname()[1])
        lease = await backend.prepare_run(RunSpec(run_id="run_ABC-1", bundle=_bundle(tmp_path)))
        tasks = [
            asyncio.create_task(
                backend.run_agent(
                    replace(_spec(), execution_id=execution_id),
                    lease,
                    on_event=event_for(execution_id),
                )
            )
            for execution_id in ("exec-1", "exec-2")
        ]
        await asyncio.wait_for(both_streaming.wait(), 20)
        assert len(json.loads(state.read_text(encoding="utf-8"))["containers"]) == 2
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(tasks[0], 20)
        assert not tasks[1].done()
        assert len(json.loads(state.read_text(encoding="utf-8"))["containers"]) == 1
        release.set()
        assert (await asyncio.wait_for(tasks[1], 20)).content == {"answer": "agent"}
        assert (await backend.run_agent(_spec(), lease)).content == {"answer": "agent"}
        assert (
            sum(
                row["argv"][0] == "create" and "io.conductor.resource=runner" in row["argv"]
                for row in _rows(log)
            )
            == 2
        )
        await backend.finalize_run(lease, "cancelled")
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_realm_cancel_preserves_streaming_and_retires_its_runner(
    fake_realm: tuple[DockerRunnerBackend, Path, Path], tmp_path: Path
) -> None:
    # Requirement: cancellation reaps the bridge and its exclusive runner before returning.
    backend, log, state = fake_realm
    received = asyncio.Event()
    released = asyncio.Event()

    async def handle(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await released.wait()
        writer.write(b"1")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        backend._cli_env["FAKE_DOCKER_RELEASE_PORT"] = str(server.sockets[0].getsockname()[1])
        lease = await backend.prepare_run(RunSpec(run_id="run_ABC-1", bundle=_bundle(tmp_path)))
        events: list[str] = []

        def on_event(kind: str, _data: Mapping[str, Any]) -> None:
            events.append(kind)
            received.set()

        task = asyncio.create_task(backend.run_agent(_spec(), lease, on_event=on_event))
        await asyncio.wait_for(received.wait(), 20)
        assert not task.done() and events == ["agent_message"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert json.loads(state.read_text(encoding="utf-8"))["containers"] == {}
        released.set()
        assert (await backend.run_agent(_spec(), lease)).content == {"answer": "agent"}
        assert (
            len(
                [
                    row
                    for row in _rows(log)
                    if row["argv"][0] == "create" and "io.conductor.resource=runner" in row["argv"]
                ]
            )
            == 2
        )
        await backend.finalize_run(lease, "cancelled")
    finally:
        released.set()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_cancellation_while_runner_starts_retires_only_its_reservation(
    fake_realm: tuple[DockerRunnerBackend, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: cancellation during startup waits for that runner's cleanup, then propagates.
    backend, log, state = fake_realm
    entered = asyncio.Event()
    release = asyncio.Event()
    original = backend._bridge_health

    async def gated_health(name: str, image: str):
        entered.set()
        await release.wait()
        return await original(name, image)

    monkeypatch.setattr(backend, "_bridge_health", gated_health)
    lease = await backend.prepare_run(RunSpec(run_id="run_ABC-1", bundle=_bundle(tmp_path)))
    task = asyncio.create_task(backend.run_agent(_spec(), lease))
    try:
        await asyncio.wait_for(entered.wait(), 20)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 20)
        assert json.loads(state.read_text(encoding="utf-8"))["containers"] == {}
        assert (await backend.run_agent(_spec(), lease)).content == {"answer": "agent"}
        assert (
            sum(
                row["argv"][0] == "create" and "io.conductor.resource=runner" in row["argv"]
                for row in _rows(log)
            )
            == 2
        )
        await backend.finalize_run(lease, "cancelled")
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_interrupt_bridge_error_fails_active_invocation(
    fake_realm: tuple[DockerRunnerBackend, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: a failed targeted interrupt cannot leave the invocation waiting indefinitely.
    backend, _log, _state = fake_realm
    streamed = asyncio.Event()
    release = asyncio.Event()
    attempted = asyncio.Event()
    original = backend._run_docker

    async def run_docker(argv, **kwargs):
        if "--interrupt" in argv:
            attempted.set()
            return 1, "", "runner interrupt HTTP 404"
        return await original(argv, **kwargs)

    async def handle(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await release.wait()
        writer.write(b"1")
        await writer.drain()
        writer.close()

    monkeypatch.setattr(backend, "_run_docker", run_docker)
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    task: asyncio.Task[AgentResult] | None = None
    try:
        backend._cli_env["FAKE_DOCKER_RELEASE_PORT"] = str(server.sockets[0].getsockname()[1])
        lease = await backend.prepare_run(RunSpec(run_id="run_ABC-1", bundle=_bundle(tmp_path)))
        signal = asyncio.Event()
        task = asyncio.create_task(
            backend.run_agent(
                _spec(),
                lease,
                interrupt_signal=signal,
                on_event=lambda _kind, _data: streamed.set(),
            )
        )
        await asyncio.wait_for(streamed.wait(), 20)
        signal.set()
        await asyncio.wait_for(attempted.wait(), 20)
        with pytest.raises(ConfigurationError, match="interrupt.*404"):
            await asyncio.wait_for(task, 5)
        await backend.finalize_run(lease, "failed")
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("release_result", [True, False])
async def test_acknowledged_interrupt_requires_partial_terminal(
    fake_realm: tuple[DockerRunnerBackend, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_result: bool,
) -> None:
    # Requirement: HTTP 200 alone is insufficient; a missing or full result fails and retires.
    backend, _log, state = fake_realm
    streamed = asyncio.Event()
    connected = asyncio.Event()
    release = asyncio.Event()
    acknowledged = asyncio.Event()
    original = backend._run_docker

    async def run_docker(argv, **kwargs):
        if "--interrupt" in argv:
            acknowledged.set()
            return 0, "", ""
        return await original(argv, **kwargs)

    async def handle(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connected.set()
        await release.wait()
        writer.write(b"1")
        await writer.drain()
        writer.close()

    monkeypatch.setattr(backend, "_run_docker", run_docker)
    if not release_result:
        monkeypatch.setattr(docker_agent, "_INTERRUPT_TIMEOUT_SECONDS", 0)
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    task: asyncio.Task[AgentResult] | None = None
    try:
        backend._cli_env["FAKE_DOCKER_RELEASE_PORT"] = str(server.sockets[0].getsockname()[1])
        lease = await backend.prepare_run(RunSpec(run_id="run_ABC-1", bundle=_bundle(tmp_path)))
        signal = asyncio.Event()
        task = asyncio.create_task(
            backend.run_agent(
                _spec(),
                lease,
                interrupt_signal=signal,
                on_event=lambda _kind, _data: streamed.set(),
            )
        )
        await asyncio.wait_for(streamed.wait(), 20)
        await asyncio.wait_for(connected.wait(), 20)
        signal.set()
        await asyncio.wait_for(acknowledged.wait(), 20)
        if release_result:
            release.set()
        with pytest.raises(ConfigurationError, match="acknowledged without a partial result"):
            await asyncio.wait_for(task, 20)
        assert json.loads(state.read_text(encoding="utf-8"))["containers"] == {}
        await backend.finalize_run(lease, "failed")
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_cancellation_during_post_eof_reap_propagates(
    fake_realm: tuple[DockerRunnerBackend, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: cancellation after stdout EOF does not wait for an unresponsive wait().
    backend, _log, state = fake_realm
    lease = await backend.prepare_run(RunSpec(run_id="run_ABC-1", bundle=_bundle(tmp_path)))
    entered = asyncio.Event()
    release = asyncio.Event()
    wait_cancelled = asyncio.Event()
    original = docker_agent._reap
    gated_once = True

    async def gated_reap(process: asyncio.subprocess.Process, *, kill: bool) -> bool:
        nonlocal gated_once
        if not kill and gated_once:
            gated_once = False
            process_wait = process.wait
            first_wait = True

            async def wait() -> int:
                nonlocal first_wait
                if not first_wait:
                    return await process_wait()
                first_wait = False
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    wait_cancelled.set()
                    raise
                return await process_wait()

            monkeypatch.setattr(process, "wait", wait)
        return await original(process, kill=kill)

    monkeypatch.setattr(docker_agent, "_reap", gated_reap)
    task = asyncio.create_task(backend.run_agent(_spec(), lease))
    try:
        await asyncio.wait_for(entered.wait(), 20)
        task.cancel()
        done, _pending = await asyncio.wait({task}, timeout=3)
        assert task in done, "cancellation remained blocked on the post-EOF wait"
        with pytest.raises(asyncio.CancelledError):
            await task
        assert wait_cancelled.is_set()
        assert json.loads(state.read_text(encoding="utf-8"))["containers"] == {}
        assert (await backend.run_agent(_spec(), lease)).content == {"answer": "agent"}
        await backend.finalize_run(lease, "cancelled")
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("phase", ["process_exit", "stderr"])
@pytest.mark.parametrize(
    ("interrupt_exit", "message"),
    [
        (1, "delivery failed"),
        (0, "acknowledged without a partial result"),
        (3, "conflicted without completing its terminal drain"),
    ],
)
async def test_interrupt_is_monitored_through_post_eof_waits(
    fake_realm: tuple[DockerRunnerBackend, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    interrupt_exit: int,
    message: str,
) -> None:
    # Requirement: delivery failure and its deadline remain observable after stdout EOF.
    backend, _log, state = fake_realm
    backend._cli_env["FAKE_DOCKER_SCENARIO"] = "realm-truncated"
    lease = await backend.prepare_run(RunSpec(run_id="run_ABC-1", bundle=_bundle(tmp_path)))
    entered = asyncio.Event()
    release = asyncio.Event()
    original_docker = backend._run_docker
    original_reap = docker_agent._reap
    original_stderr = docker_agent._stderr_tail

    async def run_docker(argv, **kwargs):
        if "--interrupt" in argv:
            return interrupt_exit, "", "delivery failed" if interrupt_exit == 1 else ""
        return await original_docker(argv, **kwargs)

    async def gated_reap(process: asyncio.subprocess.Process, *, kill: bool) -> bool:
        if not kill:
            entered.set()
            await release.wait()
        return await original_reap(process, kill=kill)

    async def gated_stderr(stream: asyncio.StreamReader) -> bytes:
        tail = await original_stderr(stream)
        entered.set()
        await release.wait()
        return tail

    monkeypatch.setattr(backend, "_run_docker", run_docker)
    if phase == "process_exit":
        monkeypatch.setattr(docker_agent, "_reap", gated_reap)
    else:
        monkeypatch.setattr(docker_agent, "_stderr_tail", gated_stderr)
    if interrupt_exit != 1:
        monkeypatch.setattr(docker_agent, "_INTERRUPT_TIMEOUT_SECONDS", 0)
    signal = asyncio.Event()
    task = asyncio.create_task(backend.run_agent(_spec(), lease, interrupt_signal=signal))
    try:
        await asyncio.wait_for(entered.wait(), 20)
        signal.set()
        with pytest.raises(ConfigurationError, match=message):
            await asyncio.wait_for(task, 5)
        assert json.loads(state.read_text(encoding="utf-8"))["containers"] == {}
        await backend.finalize_run(lease, "failed")
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_interrupt_uses_second_exec_and_returns_partial(
    fake_realm: tuple[DockerRunnerBackend, Path, Path], tmp_path: Path
) -> None:
    # Requirement: an interrupt targets the active execution through a second bridge process.
    backend, log, _state = fake_realm
    received = asyncio.Event()
    connected = asyncio.Event()
    interrupted = asyncio.Event()
    waiting: list[asyncio.StreamWriter] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not waiting:
            waiting.append(writer)
            connected.set()
            await interrupted.wait()
            return
        assert await reader.read(1) == b"I"
        interrupted.set()
        for pending in waiting:
            pending.write(b"I")
            await pending.drain()
            pending.close()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        backend._cli_env["FAKE_DOCKER_RELEASE_PORT"] = str(server.sockets[0].getsockname()[1])
        lease = await backend.prepare_run(RunSpec(run_id="run_ABC-1", bundle=_bundle(tmp_path)))
        signal = asyncio.Event()
        task = asyncio.create_task(
            backend.run_agent(
                _spec(),
                lease,
                interrupt_signal=signal,
                on_event=lambda _kind, _data: received.set(),
            )
        )
        await asyncio.wait_for(received.wait(), 20)
        await asyncio.wait_for(connected.wait(), 20)
        signal.set()
        await asyncio.wait_for(interrupted.wait(), 20)
        result = await asyncio.wait_for(task, 20)
        assert result.partial is True
        assert any("--interrupt" in row["argv"] and "exec-1" in row["argv"] for row in _rows(log))
        await backend.finalize_run(lease, "succeeded")
    finally:
        for writer in waiting:
            writer.close()
        server.close()
        await server.wait_closed()


async def test_completed_result_does_not_send_a_late_interrupt(
    fake_realm: tuple[DockerRunnerBackend, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: a signal after terminal receipt cannot discard a completed result.
    backend, log, _state = fake_realm
    lease = await backend.prepare_run(RunSpec(run_id="run_ABC-1", bundle=_bundle(tmp_path)))
    entered = asyncio.Event()
    release = asyncio.Event()
    signal = asyncio.Event()
    original_reap = docker_agent._reap

    async def gated_reap(process: asyncio.subprocess.Process, *, kill: bool) -> bool:
        if not kill:
            entered.set()
            await release.wait()
        return await original_reap(process, kill=kill)

    monkeypatch.setattr(docker_agent, "_reap", gated_reap)
    task = asyncio.create_task(backend.run_agent(_spec(), lease, interrupt_signal=signal))
    try:
        await asyncio.wait_for(entered.wait(), 20)
        signal.set()
        # Run the signal's ready callbacks before releasing the post-terminal wait.
        asyncio.get_running_loop().call_soon(release.set)
        result = await asyncio.wait_for(task, 20)
        assert result.content == {"answer": "agent"}
        assert result.partial is False
        assert not any("--interrupt" in row["argv"] for row in _rows(log))
        await backend.finalize_run(lease, "succeeded")
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("partial", [False, True])
async def test_interrupt_conflict_drains_the_racing_terminal_result(
    fake_realm: tuple[DockerRunnerBackend, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    partial: bool,
) -> None:
    # Requirement: typed HTTP 409 preserves either full or partial racing terminal output.
    backend, _log, _state = fake_realm
    connected = asyncio.Event()
    release = asyncio.Event()
    streamed = asyncio.Event()
    original_docker = backend._run_docker

    async def run_docker(argv, **kwargs):
        if "--interrupt" in argv:
            release.set()
            return 3, "", "diagnostic text is not an outcome discriminator"
        return await original_docker(argv, **kwargs)

    async def handle(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connected.set()
        try:
            await release.wait()
            writer.write(b"I" if partial else b"1")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    monkeypatch.setattr(backend, "_run_docker", run_docker)
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    task: asyncio.Task[AgentResult] | None = None
    try:
        backend._cli_env["FAKE_DOCKER_RELEASE_PORT"] = str(server.sockets[0].getsockname()[1])
        lease = await backend.prepare_run(RunSpec(run_id="run_ABC-1", bundle=_bundle(tmp_path)))
        signal = asyncio.Event()
        task = asyncio.create_task(
            backend.run_agent(
                _spec(),
                lease,
                interrupt_signal=signal,
                on_event=lambda _kind, _data: streamed.set(),
            )
        )
        await asyncio.wait_for(streamed.wait(), 20)
        await asyncio.wait_for(connected.wait(), 20)
        signal.set()
        result = await asyncio.wait_for(task, 20)
        assert result.content == {"answer": "agent"}
        assert result.partial is partial
        await backend.finalize_run(lease, "succeeded")
    finally:
        release.set()
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        server.close()
        await server.wait_closed()


def test_staged_paths_match_bundle_topology(tmp_path: Path) -> None:
    # Requirement: skill, plugin skill, and working directory paths must match staged files.
    bundle = _bundle(tmp_path)
    skill = tmp_path / "skills" / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("skill", encoding="utf-8")
    plugin = tmp_path / "plug" / "skills" / "lint"
    plugin.mkdir(parents=True)
    (plugin / "SKILL.md").write_text("plugin skill", encoding="utf-8")
    (plugin.parent.parent / ".github/plugin").mkdir(parents=True)
    (plugin.parent.parent / ".github/plugin/plugin.json").write_text("{}", encoding="utf-8")
    bundle = replace(
        bundle,
        agent_paths=(
            (str(skill), "skills/review"),
            (str(plugin.parent.parent), "plugins/plug"),
            (str(plugin), "plugins/plug/skills/lint"),
        ),
    )
    entries = []
    for path, logical in (
        (skill / "SKILL.md", "tree/skills/review/SKILL.md"),
        (plugin / "SKILL.md", "tree/plugins/plug/skills/lint/SKILL.md"),
        (
            plugin.parent.parent / ".github/plugin/plugin.json",
            "tree/plugins/plug/.github/plugin/plugin.json",
        ),
    ):
        entries.append(
            {
                "logical_path": logical,
                "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    (Path(bundle.store_path) / "bundle.json").write_text(
        json.dumps(
            {
                "entries": entries,
                "skills_topology": {
                    "review": "tree/skills/review/SKILL.md",
                    "lint": "tree/plugins/plug/skills/lint/SKILL.md",
                },
                "plugins_topology": {"plug": "tree/plugins/plug/.github/plugin/plugin.json"},
            }
        ),
        encoding="utf-8",
    )
    spec = _spec()
    mapped = map_agent_paths(
        replace(
            spec,
            skill_directories=(str(skill), str(plugin)),
            working_dir=str(tmp_path),
            mcp_servers={"server": {"command": "npx", "args": ["mcp-server"]}},
        ),
        bundle,
    )
    assert mapped.skill_directories == (
        "/workspace/skills/review",
        "/workspace/plugins/plug/skills/lint",
    )
    assert mapped.working_dir == "/workspace/main"
    (Path(bundle.store_path) / "tree/main/sub").mkdir()
    assert (
        map_agent_paths(replace(spec, working_dir="sub"), bundle).working_dir
        == "/workspace/main/sub"
    )
    with pytest.raises(ConfigurationError, match="escapes the staged bundle"):
        map_agent_paths(replace(spec, working_dir="../outside"), bundle)
    assert mapped.mcp_servers == {"server": {"command": "npx", "args": ["mcp-server"]}}
    extra = tmp_path / "external"
    extra.mkdir()
    (Path(bundle.store_path) / "tree/roots/00").mkdir(parents=True)
    extra_bundle = replace(bundle, source_roots=(*bundle.source_roots, (str(extra), "roots/00")))
    assert (
        map_agent_paths(replace(spec, working_dir=str(extra)), extra_bundle).working_dir
        == "/workspace/roots/00"
    )
    with pytest.raises(ConfigurationError, match="staged bundle topology"):
        map_agent_paths(replace(spec, skill_directories=(str(tmp_path / "other"),)), bundle)
    clone = tmp_path / "other" / "review"
    clone.mkdir(parents=True)
    (clone / "SKILL.md").write_text("skill", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="staged bundle topology"):
        map_agent_paths(replace(spec, skill_directories=(str(clone),)), bundle)
    with pytest.raises(ConfigurationError, match="outside the staged Docker bundle"):
        map_agent_paths(replace(spec, working_dir=str(tmp_path.parent / "unbundled")), bundle)
    (skill / "SKILL.md").write_text("modified after collection", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="staged bundle topology"):
        map_agent_paths(replace(spec, skill_directories=(str(skill),)), bundle)


def test_collector_exposes_only_authored_agent_directory_paths(tmp_path: Path) -> None:
    # Requirement: runtime path mapping uses collector provenance, not digest-only guesses.
    skill = tmp_path / "skills/review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review code\n---\nInstructions", encoding="utf-8"
    )
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        "workflow:\n  name: demo\n  entry_point: agent\n"
        "  runtime:\n    skills: [./skills/review]\n"
        "agents:\n  - name: agent\n    prompt: work\n"
        "    routes:\n      - to: $end\n",
        encoding="utf-8",
    )
    collected = collect_bundle(
        workflow, environment=None, allow_network=False, on_warning=lambda _message: None
    )
    assert (str(skill), "skills/review") in collected.agent_paths
