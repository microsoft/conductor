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
async def test_concurrent_agent_calls_share_one_runner(
    fake_realm: tuple[DockerRunnerBackend, Path, Path], tmp_path: Path
) -> None:
    # Requirement: overlapping agent calls on one lease create exactly one runner container.
    backend, log, _state = fake_realm
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
            == 1
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
async def test_realm_cancel_preserves_streaming_and_invalidates_generation(
    fake_realm: tuple[DockerRunnerBackend, Path, Path], tmp_path: Path
) -> None:
    # Requirement: streaming events precede result; cancellation reaps and recreates runner.
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
        map_agent_paths(replace(spec, working_dir="/unbundled"), bundle)
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
