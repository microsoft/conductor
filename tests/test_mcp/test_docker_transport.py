"""Real SDK composition of Docker CLI context and private container delivery."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Literal

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from conductor.exceptions import ConfigurationError
from conductor.execution.types import McpServerSpec, ResolvedExecutionSpec
from tests.test_mcp.fixtures.docker_realm import (
    _CANARY,
    _PEER,
    _config,
    _Realm,
    _resolve,
    create_realm,
)
from tests.test_mcp.fixtures.docker_stdio import LaunchReceipt, PeerReceipt


@pytest.fixture
def realm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Realm:
    return create_realm(tmp_path, monkeypatch)


def _names(env: Mapping[str, str]) -> Mapping[str, str]:
    return {
        (key.casefold() if sys.platform == "win32" else key): value for key, value in env.items()
    }


@pytest.mark.parametrize("confirmed_id", [False, True])
async def test_delayed_daemon_creation_outlives_sdk_and_cli(realm, confirmed_id):
    # Requirement: a terminal launcher plus first inspect absence is not daemon creation proof.
    from conductor.exceptions import ExecutionError
    from conductor.execution.docker import DockerRunnerBackend
    from conductor.execution.errors import WorkspaceAttachError
    from conductor.execution.types import WorkspaceIdentity
    from tests.test_engine.mcp_placement import placement_config, placement_engine

    if not confirmed_id:
        realm.backend._cli_env["FAKE_DOCKER_SCENARIO"] = "delayed-daemon"
    config = placement_config()
    async with placement_engine(config, realm) as engine:
        session = engine._execution_session
        lease = session.leases["docker"]
        if confirmed_id:
            await engine._run_mcp_step(config.agents[0], {})
        else:
            with pytest.raises(ExecutionError):
                await engine._run_mcp_step(config.agents[0], {})
        await session.finalize_leases("failed")
        launches = [
            LaunchReceipt.model_validate_json(path.read_text())
            for path in realm.root.glob("*.launcher.json")
        ]
        assert len(launches) == 1 and launches[0].joined
        peer = PeerReceipt.model_validate_json(
            next(
                path
                for path in realm.root.glob("conductor-mcp-*.json")
                if not path.name.endswith(".launcher.json")
            ).read_text()
        )
        assert peer.closed
        state = json.loads((realm.root / "state.json").read_text())
        if confirmed_id:
            assert state["containers"] == {} and state["volumes"] == {}
            assert not session.quarantined
        else:
            assert session.quarantined and state["volumes"]
            assert len(state["containers"]) == 1 and len(state["pending_mcp"]) == 1
            other = DockerRunnerBackend(realm.backend._docker_binary)
            with pytest.raises(WorkspaceAttachError):
                await other.attach_run(
                    realm.run, WorkspaceIdentity("docker", lease.lease_id, lease.incarnation)
                )
            rc, _out, _err = await realm.backend._run_docker(("fake-daemon-commit",))
            assert rc == 0
            state = json.loads((realm.root / "state.json").read_text())
            assert len(state["containers"]) == 2 and state["volumes"]
            assert engine._mcp_step_managers == {}
        print(
            {
                "scenario": "delayed-daemon",
                "confirmed_id": confirmed_id,
                "launcher": launches[0].model_dump(include={"pid", "child_pid", "joined"}),
                "peer": peer.model_dump(include={"pid", "closed"}),
                "retained": session.quarantined,
            }
        )


@pytest.mark.parametrize("delivery", ["AMBIENT_SOURCE", "RENAMED_TOKEN"])
@pytest.mark.parametrize("failure", ["none", "startup", "call"])
async def test_environment_delivery_through_real_sdk(
    realm: _Realm,
    delivery: str,
    failure: Literal["none", "startup", "call"],
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: actual SDK children preserve CLI context and receive only allowed MCP payload.
    # Pytest injects independent case and capture fixtures; these are not domain API parameters.
    backend = realm.backend
    lease = await backend.prepare_run(realm.run)
    identities: set[str] = set()
    env_files: set[Path] = set()
    try:
        # Requirement: fresh connections on one lease cannot reuse a previous delivery or env-file.
        for generation in ("first", "rotated"):
            value = f"{_CANARY}-{generation}"
            monkeypatch.setenv("LATE_SOURCE", value)
            resolved = await _resolve(_config(realm.root, delivery))
            expected = {
                "LITERAL": "authored",
                "DEFAULT": "fallback",
                "EXPANDED": "ambient-source-allowed-2178",
                "LC_CTYPE": "C.UTF-8",
                delivery: "ambient-source-allowed-2178",
                "PAYLOAD_TOKEN": value,
                "PEER_FAILURE": failure,
            }
            target = await backend.open_mcp_stdio(
                McpServerSpec(
                    "srv",
                    ("peer",),
                    {**resolved.env, "PEER_FAILURE": failure},
                    execution=ResolvedExecutionSpec("peer:image"),
                ),
                lease,
            )
            env_file = Path(target.args[target.args.index("--env-file") + 1])
            params = StdioServerParameters(
                command=target.command, args=list(target.args), env=dict(target.env), cwd=target.cwd
            )
            try:
                target.mark_launch_attempted()
                with anyio.fail_after(15):
                    async with (
                        stdio_client(params) as (reader, writer),
                        ClientSession(reader, writer, timedelta(seconds=5)) as session,
                    ):
                        await session.initialize()
                        await session.list_tools()
                        await target.validate_started()
                        result = await session.call_tool("receive")
                        assert result.isError is (failure == "call")
            except ExceptionGroup as error:
                assert failure == "startup"
                assert _CANARY not in str(error)
            finally:
                await target.aclose()
            receipt = realm.root / f"{target.identity}.json"
            peer = PeerReceipt.model_validate_json(receipt.read_text())
            launch = LaunchReceipt.model_validate_json(
                receipt.with_suffix(".launcher.json").read_text()
            )
            assert _names(launch.env) == _names(realm.operator)
            assert launch.payload == expected and peer.env == expected
            assert peer.is_owned_by(launch.pid, launch.child_pid)
            assert peer.called is (failure != "startup") and peer.closed and launch.joined
            assert launch.exit_code == (23 if failure == "startup" else 0)
            assert not env_file.exists()
            assert target.identity not in identities and env_file not in env_files
            identities.add(target.identity)
            env_files.add(env_file)
            assert sys.platform == "win32" or launch.mode == 0o600
            assert all(item not in json.dumps(launch.argv) for item in (_CANARY, "PAYLOAD_TOKEN"))
    finally:
        await backend.finalize_run(lease, "failed" if failure != "none" else "succeeded")
    state = json.loads((realm.root / "state.json").read_text())
    assert state["containers"] == {} and state["volumes"] == {}
    captured = capfd.readouterr()
    assert _CANARY not in caplog.text + captured.out + captured.err


async def test_environment_sdk_defaults_do_not_preserve_operator_context(realm: _Realm) -> None:
    # Requirement: a real SDK default launch distinguishes filtered inheritance from target.env.
    receipt = realm.root / "default.json"
    with anyio.fail_after(10):
        async with stdio_client(
            StdioServerParameters(command=sys.executable, args=[str(_PEER), "--peer", str(receipt)])
        ) as (reader, writer):
            async with ClientSession(reader, writer, timedelta(seconds=5)) as session:
                await session.initialize()
                await session.call_tool("receive")
    peer = PeerReceipt.model_validate_json(receipt.read_text())
    assert peer.called and peer.closed
    assert "DOCKER_HOST" in realm.operator and "DOCKER_HOST" not in peer.env
    assert "CREDENTIAL_HELPER_CONTEXT" not in peer.env and "AMBIENT_SOURCE" not in peer.env


@pytest.mark.parametrize("malformed", ["unknown", "collision"])
async def test_refusal_precedes_docker_dispatch(realm: _Realm, malformed: str) -> None:
    # Requirement: real YAML/ref resolution refuses unknown refs and duplicate delivery names.
    config = _config(
        realm.root,
        "PAYLOAD_TOKEN" if malformed == "collision" else "RENAMED_TOKEN",
        ref="unknown" if malformed == "unknown" else "ambient",
    )
    with pytest.raises(ConfigurationError, match="Unknown secret reference|collides"):
        await _resolve(config)
    assert not (realm.root / "docker.jsonl").exists()


@pytest.mark.parametrize("channel", ["stderr", "stdout"])
@pytest.mark.parametrize("phase", ["startup", "call"])
async def test_docker_stdout_stderr_diagnostic_privacy(
    realm: _Realm, channel, phase, caplog, capfd, monkeypatch
) -> None:
    # Requirement: actual SDK failures expose safe errors, never raw stderr/stdout in any sink.
    import logging
    from contextlib import asynccontextmanager

    import conductor.mcp.manager as manager_module
    from conductor.exceptions import ExecutionError
    from tests.test_engine.mcp_placement import placement_config, placement_engine
    from tests.test_mcp.fixtures.diagnostic_stdio import CANARY

    config = placement_config(failure=f"diagnostic-{channel}-{phase}")
    caplog.set_level(logging.DEBUG)
    open_stdio = manager_module.stdio_client
    sinks = []
    descriptors = []

    @asynccontextmanager
    async def observed_stdio(params, **kwargs):
        errlog = kwargs.get("errlog")
        if errlog is not None:
            sinks.append(errlog)
            descriptors.append(errlog.fileno())
        async with open_stdio(params, **kwargs) as transport:
            yield transport

    monkeypatch.setattr(manager_module, "stdio_client", observed_stdio)
    with anyio.fail_after(20):
        async with placement_engine(config, realm) as engine:
            events = []
            engine._event_emitter.subscribe(events.append)
            with pytest.raises(ExecutionError) as error:
                await engine._run_mcp_step(config.agents[0], {"argument": CANARY})
            diagnostic = engine._mcp_diagnostic_path().read_text()
            assert "category=mcp_failure" in diagnostic
            assert "diagnostic metadata" in str(error.value)
            assert "stderr suppressed" in str(error.value)
            failed = [event for event in events if event.type == "mcp_failed"]
            assert len(failed) == 1
            captured = capfd.readouterr()
            assert CANARY not in (
                diagnostic
                + str(error.value)
                + caplog.text
                + captured.out
                + captured.err
                + json.dumps([event.data for event in events])
            )
    assert sinks and all(sink.closed for sink in sinks)
    (realm.root / "sink-observation.json").write_text(
        json.dumps(
            {
                "channel": channel,
                "phase": phase,
                "descriptors": descriptors,
                "all_closed": all(sink.closed for sink in sinks),
            }
        )
    )
    if channel == "stdout":
        assert any("MCP transport diagnostic" in record.getMessage() for record in caplog.records)
