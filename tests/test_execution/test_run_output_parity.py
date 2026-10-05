"""Pinned provider-free run output and event-surface parity."""

from __future__ import annotations

import asyncio
import errno
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from conductor.config.schema import (
    ContextConfig,
    LimitsConfig,
    RouteDef,
    RuntimeConfig,
    ScriptStepDef,
    SetStepDef,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.execution import LocalRunnerBackend
from conductor.execution.docker import DockerRunnerBackend
from conductor.execution.types import CommandResult, CommandSpec, ResolvedExecutionSpec, RunSpec
from tests.test_execution.test_docker_backend import _write_fake


@pytest.mark.asyncio
async def test_provider_free_workflow_output_shape_pinned() -> None:
    # Requirement: execution profiles add only system.execution_manifest to the run surface.
    config = WorkflowConfig(
        workflow=WorkflowDef(
            name="provider-free-parity",
            entry_point="mark",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
        ),
        agents=[
            SetStepDef(
                name="mark",
                value="'hello'",
                routes=[RouteDef(to="say")],
            ),
            ScriptStepDef(
                name="say",
                command=sys.executable,
                args=["-c", "print('{{ mark.output }}')"],
                routes=[RouteDef(to="check42")],
            ),
            ScriptStepDef(
                name="check42",
                command=sys.executable,
                args=["-c", "import sys; sys.exit(42)"],
                routes=[RouteDef(to="$end", when="exit_code == 42")],
            ),
        ],
        output={
            "greeting": "{{ mark.output }}",
            "said": "{{ say.output.stdout }}",
            "code": "{{ check42.output.exit_code }}",
        },
    )
    events: list[WorkflowEvent] = []
    emitter = WorkflowEventEmitter()
    emitter.subscribe(events.append)

    result = await WorkflowEngine(config, MagicMock(), event_emitter=emitter).run({})

    # The child emits the platform-native text newline (\r\n on Windows);
    # the parity requirement concerns the output shape and value, not LF
    # versus CRLF — compare line content, matching the test_script.py
    # precedent for cross-platform newline handling.
    normalized_result = {**result, "said": result["said"].splitlines()}
    assert normalized_result == {"greeting": "hello", "said": ["hello"], "code": 42}
    assert [event.type for event in events] == [
        "workflow_started",
        "agent_started",
        "set_started",
        "set_completed",
        "route_taken",
        "agent_started",
        "script_started",
        "script_completed",
        "route_taken",
        "agent_started",
        "script_started",
        "script_completed",
        "route_taken",
        "workflow_completed",
    ]

    started = events[0].data
    system = started["system"]
    assert set(system) == {
        "pid",
        "platform",
        "python_version",
        "conductor_version",
        "cwd",
        "started_at",
        "run_id",
        "log_file",
        "bg_mode",
        "execution_manifest",
    }
    assert {
        "dashboard_port",
        "dashboard_url",
        "parent_pid",
        "bg_stderr_log",
        "bg_stdout_log",
    }.isdisjoint(system)

    manifest = system["execution_manifest"]
    assert manifest["version"] == 1
    assert manifest["environment"]["name"] == "local/default"
    assert manifest["environment"]["source"] == "builtin"
    assert manifest["environment"]["digest"].startswith("sha256:")
    assert manifest["workflow"]["name"] == "provider-free-parity"
    assert manifest["audit"] == {
        "hermetic": False,
        "classification": "non-hermetic-compatibility",
    }
    # Requirement: the manifest pins the effective inherit-control-environment policy per step.
    assert manifest["profiles"] == {
        "say": {
            "profile": "default",
            "backend": "local",
            "inherit_control_environment": True,
        },
        "check42": {
            "profile": "default",
            "backend": "local",
            "inherit_control_environment": True,
        },
    }


class TestLocalDockerCommandResultParity:
    """Requirement: identical run scenarios produce identical CommandResult
    invariants on the local backend and on the Docker backend driving the
    stateful fake CLI (tests/test_execution/docker_fake.py) -- the container
    realm must not change the data-shaped contract the engine routes on.

    The fake CLI is canned, so payload-carrying invariants (exact stdout text)
    are aligned via FAKE_DOCKER_STDOUT; structural invariants (outcome,
    exit_code, start_error shape, timing) must hold on both backends.
    """

    @staticmethod
    async def _run_local(spec: CommandSpec) -> CommandResult:
        return await LocalRunnerBackend().run_command(spec, None)

    @staticmethod
    def _fake_backend(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[DockerRunnerBackend, Path]:
        """Requirement: reuse the fake-CLI harness pattern from
        test_docker_backend.py -- isolated PATH, JSONL invocation log, and
        JSON state under the test's tmp_path, so no scenario can reach a real
        daemon (the module must stay daemon-free for the default test run)."""
        bin_dir = tmp_path / "parity-bin"
        bin_dir.mkdir()
        binary = _write_fake(bin_dir)
        log = tmp_path / "parity-docker.jsonl"
        state = tmp_path / "parity-state.json"
        monkeypatch.setenv("PATH", str(bin_dir))
        monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
        monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
        return DockerRunnerBackend(binary), log

    @staticmethod
    async def _docker_lease(backend: DockerRunnerBackend) -> Any:
        # Requirement: a lease prepared from a bundle-free RunSpec exercises
        # run_command's command paths (volume create, image inspect, create,
        # start, inspect) without the staging cp path, exactly as the
        # test_docker_backend.py contract tests do.
        return await backend.prepare_run(RunSpec(run_id="parity-run"))

    @staticmethod
    def _docker_log_commands(log: Path) -> list[str]:
        return [
            json.loads(line)["argv"][0] for line in log.read_text(encoding="utf-8").splitlines()
        ]

    @pytest.mark.asyncio
    async def test_completed_exit_zero_matches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: argv/stdin/stdout/exit-code/timing cross the seam
        # identically -- completed exit 0 carries the workload stdout verbatim.
        monkeypatch.setenv("FAKE_DOCKER_STDOUT", "parity-payload\n")
        local = await self._run_local(
            CommandSpec(command=sys.executable, args=("-c", "print('parity-payload')"))
        )
        backend, _log = self._fake_backend(tmp_path, monkeypatch)
        docker = await backend.run_command(
            CommandSpec(
                command="print",
                args=("parity-payload",),
                inherit_control_environment=False,
                execution=ResolvedExecutionSpec(image="busybox:latest"),
                name="step",
            ),
            await self._docker_lease(backend),
        )
        assert local.outcome == docker.outcome == "completed"
        assert local.exit_code == docker.exit_code == 0
        # The local child emits the platform-native text newline (\r\n on
        # Windows) while the fake container's byte stream carries \n verbatim;
        # the parity requirement concerns the payload, not LF versus CRLF --
        # same convention as the splitlines comparison above.
        assert local.stdout.replace("\r\n", "\n") == docker.stdout == "parity-payload\n"
        assert local.stderr == docker.stderr == ""
        assert local.start_error is None and docker.start_error is None
        assert local.resolved_command and docker.resolved_command
        assert local.duration_seconds > 0 and docker.duration_seconds > 0

    @pytest.mark.asyncio
    async def test_completed_nonzero_exit_matches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: command failure is routable completed data on both
        # backends -- a started workload's non-zero exit never becomes an
        # infrastructure error.
        local = await self._run_local(
            CommandSpec(command=sys.executable, args=("-c", "raise SystemExit(42)"))
        )
        monkeypatch.setenv("FAKE_DOCKER_EXIT_CODE", "42")
        monkeypatch.setenv("FAKE_DOCKER_START_RC", "42")
        backend, _log = self._fake_backend(tmp_path, monkeypatch)
        docker = await backend.run_command(
            CommandSpec(command="false", execution=ResolvedExecutionSpec(image="busybox:latest")),
            await self._docker_lease(backend),
        )
        assert local.outcome == docker.outcome == "completed"
        assert local.exit_code == docker.exit_code == 42
        assert local.start_error is None and docker.start_error is None

    @pytest.mark.asyncio
    async def test_command_not_found_matches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: an unresolvable command is data-shaped
        # (command_not_found with file_not_found metadata) on both backends.
        local = await self._run_local(CommandSpec(command="parity-missing-binary"))
        backend = DockerRunnerBackend(str(tmp_path / "missing-docker"))
        docker = await backend.run_command(
            CommandSpec(command="true", execution=ResolvedExecutionSpec(image="busybox:latest")),
            await self._docker_lease(backend),
        )
        assert local.outcome == docker.outcome == "command_not_found"
        assert local.exit_code is None and docker.exit_code is None
        assert local.resolved_command == "parity-missing-binary"
        assert docker.resolved_command == str(tmp_path / "missing-docker")
        for result in (local, docker):
            assert result.start_error is not None
            assert result.start_error.kind == "file_not_found"
            assert result.start_error.errno == errno.ENOENT
            assert result.start_error.message

    @pytest.mark.asyncio
    async def test_start_failed_matches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: a spawn/create infrastructure failure is data-shaped
        # start_failed with os_error metadata on both backends.

        def _denied(*args: Any, **kwargs: Any) -> Any:
            raise OSError(errno.EACCES, "Permission denied", "/secret/tool")

        monkeypatch.setattr("asyncio.create_subprocess_exec", _denied)
        local = await self._run_local(CommandSpec(command="tool"))
        monkeypatch.undo()
        monkeypatch.setenv("FAKE_DOCKER_SCENARIO", "createfail")
        backend, _log = self._fake_backend(tmp_path, monkeypatch)
        docker = await backend.run_command(
            CommandSpec(command="true", execution=ResolvedExecutionSpec(image="busybox:latest")),
            await self._docker_lease(backend),
        )
        assert local.outcome == docker.outcome == "start_failed"
        assert local.exit_code is None and docker.exit_code is None
        for result in (local, docker):
            assert result.start_error is not None
            assert result.start_error.kind == "os_error"
            assert result.start_error.message

    @pytest.mark.asyncio
    async def test_timed_out_matches(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Requirement: a per-command timeout kills the workload and returns a
        # timed_out result (never an exception, never a completed) on both
        # backends.
        local = await self._run_local(
            CommandSpec(
                command=sys.executable,
                args=("-c", "import time; time.sleep(30)"),
                timeout=0.2,
            )
        )
        monkeypatch.setenv("FAKE_DOCKER_DELAY_COMMAND", "start --attach")
        monkeypatch.setenv("FAKE_DOCKER_DELAY", "30")
        backend, _log = self._fake_backend(tmp_path, monkeypatch)
        docker = await backend.run_command(
            CommandSpec(
                command="sleep",
                args=("30",),
                timeout=0.05,
                execution=ResolvedExecutionSpec(image="busybox:latest"),
            ),
            await self._docker_lease(backend),
        )
        assert local.outcome == docker.outcome == "timed_out"
        assert local.exit_code is None and docker.exit_code is None
        assert local.start_error is None and docker.start_error is None
        assert local.duration_seconds < 10 and docker.duration_seconds < 10

    @pytest.mark.asyncio
    async def test_utf8_replacement_chars_match(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: undecodable output bytes surface as U+FFFD replacement
        # characters (utf-8, errors="replace") on both backends -- the local
        # child writes raw invalid UTF-8 bytes; the fake container stdout
        # carries the already-decoded replacement characters.
        local = await self._run_local(
            CommandSpec(
                command=sys.executable,
                args=("-c", "import sys; sys.stdout.buffer.write(b'\\xff\\xfe')"),
            )
        )
        monkeypatch.setenv("FAKE_DOCKER_STDOUT", "\ufffd\ufffd")
        backend, _log = self._fake_backend(tmp_path, monkeypatch)
        docker = await backend.run_command(
            CommandSpec(command="cat", execution=ResolvedExecutionSpec(image="busybox:latest")),
            await self._docker_lease(backend),
        )
        assert local.outcome == docker.outcome == "completed"
        assert local.stdout and set(local.stdout) == {"\ufffd"}
        assert docker.stdout and set(docker.stdout) == {"\ufffd"}

    @pytest.mark.asyncio
    async def test_cancel_propagation_matches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: cancellation is not an outcome -- it propagates as
        # CancelledError after the backend kills and reaps its child on both
        # backends, and must never collapse into a data-shaped CommandResult.

        async def _cancel_local() -> None:
            task = asyncio.create_task(
                LocalRunnerBackend().run_command(
                    CommandSpec(
                        command=sys.executable,
                        args=("-c", "import time; time.sleep(30)"),
                    ),
                    None,
                )
            )
            await asyncio.sleep(0.2)
            task.cancel()
            await task

        with pytest.raises(asyncio.CancelledError):
            await _cancel_local()

        marker = tmp_path / "docker-started"
        monkeypatch.setenv("FAKE_DOCKER_DELAY_COMMAND", "start --attach")
        monkeypatch.setenv("FAKE_DOCKER_DELAY_MARKER", str(marker))
        monkeypatch.setenv("FAKE_DOCKER_DELAY", "30")
        backend, log = self._fake_backend(tmp_path, monkeypatch)
        task = asyncio.create_task(
            backend.run_command(
                CommandSpec(
                    command="sleep",
                    execution=ResolvedExecutionSpec(image="busybox:latest"),
                ),
                await self._docker_lease(backend),
            )
        )
        for _ in range(200):
            if marker.exists():
                break
            await asyncio.sleep(0.01)
        assert marker.exists(), "fake container never reached the delayed start"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Requirement: the docker backend kills and removes the container even
        # when the caller cancelled (the fake logs both commands).
        commands = self._docker_log_commands(log)
        assert "kill" in commands and "rm" in commands
