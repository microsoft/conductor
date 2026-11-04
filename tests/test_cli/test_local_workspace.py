"""Both synchronous CLI entry points with the real registry and retained filesystem."""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Literal, assert_never

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app
from conductor.config.loader import load_config
from conductor.config.schema import ScriptStepDef
from conductor.engine.checkpoint import CheckpointManager
from conductor.execution.local import LocalRunnerBackend
from conductor.execution.local_process_scope import LocalProcessScopeController
from conductor.execution.types import (
    CommandResult,
    CommandSpec,
    LocalWorkspaceBinding,
    RunOutcome,
    WorkspaceLease,
)
from tests.test_engine.test_local_workspace_lifecycle import Provider, flow
from tests.test_engine.test_local_workspace_lifecycle import (
    retained_capability as retained_capability,
)


@pytest.mark.usefixtures("retained_capability")
def test_sync_run_and_resume_share_registry_lifecycle(tmp_path: Path, monkeypatch) -> None:
    # Requirement: sync resume reuses failed-run bytes after runtime release, then deletes the tree.
    path = flow(tmp_path)
    first, second = Provider(fail=True), Provider()
    providers = iter((first, second))

    async def create(**kwargs):
        return next(providers)

    monkeypatch.setattr("conductor.providers.registry.create_provider", create)
    monkeypatch.setattr(CheckpointManager, "get_checkpoints_dir", lambda: tmp_path)
    runner = CliRunner()
    failed = runner.invoke(app, ["--silent", "run", str(path), "--no-interactive"])
    assert failed.exit_code == 1, failed.output
    checkpoint = CheckpointManager.find_latest_checkpoint(path)
    assert checkpoint is not None
    data = CheckpointManager.load_checkpoint(checkpoint)
    assert data.workspace is not None
    root = Path(data.workspace["identities"]["local"]["location"])
    assert root.exists() and first.released == [str(root)] and first.closed
    resumed = runner.invoke(
        app, ["--silent", "resume", str(path), "--from", str(checkpoint), "--no-interactive"]
    )
    assert resumed.exit_code == 0, resumed.output
    assert json.loads(resumed.stdout) == {"result": "ok"}
    assert not root.exists()
    assert second.released == [str(root)] and second.closed


@pytest.mark.parametrize("persistence", ["on-failure", "durable"])
def test_real_scripts_resume_exact_failure_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, persistence: Literal["on-failure", "durable"]
) -> None:
    # Requirement: real CLI resume attaches the released incarnation without restaging live edits.
    # Given: the bundled-asset example, with no provider or capability replacement.
    examples = Path(__file__).parents[2] / "examples"
    example = examples / "local-retained-workspace.yaml"
    asset = examples / "local-retained-workspace-input.txt"
    config = load_config(example)
    assert config.workflow.workspace is not None
    config.workflow.workspace.persistence = persistence
    commands = {
        tuple(step.args): step.name for step in config.agents if isinstance(step, ScriptStepDef)
    }
    source = tmp_path / "source"
    source.mkdir()
    path = source / example.name
    path.write_text(config.model_dump_json(exclude_none=True), encoding="utf-8")
    (source / asset.name).write_bytes(asset.read_bytes())
    original = {p: p.read_bytes() for p in (example, asset, path, source / asset.name)}
    expected = b"This input came from the bundled asset.\nCreated in the retained copy.\n"
    edited = expected + b"Edited between CLI invocations.\n"
    bindings: list[LocalWorkspaceBinding] = []
    leases: list[WorkspaceLease] = []
    backends: list[LocalRunnerBackend] = []
    completed: list[str] = []
    finalized: list[bool] = []
    checkpoints: list[Path] = []
    native_invocations: list[tuple[tuple[str, ...], int]] = []
    faulted = False
    run_command = LocalRunnerBackend.run_command
    finalize_run = LocalRunnerBackend.finalize_run

    class BeforeReadFailure(RuntimeError):
        """One deterministic failure before the reader starts its subprocess."""

    def capture_save[**P](save: Callable[P, Path | None]) -> Callable[P, Path | None]:
        @wraps(save)
        def capture(*args: P.args, **kwargs: P.kwargs) -> Path | None:
            saved = save(*args, **kwargs)
            assert saved is not None
            checkpoints.append(saved)
            return saved

        return capture

    async def fail_before_read(
        backend: LocalRunnerBackend,
        spec: CommandSpec,
        lease: WorkspaceLease | None,
        *,
        diagnostics: Callable[[str], None] | None = None,
        on_dispatch: Callable[[], None] | None = None,
    ) -> CommandResult:
        nonlocal faulted
        assert backend.capabilities().retained_workspace
        assert lease is not None
        binding = backend.local_workspace_binding(lease)
        assert binding is not None
        live = Path(binding.work_root) / "main"
        assert Path(spec.working_dir or "") == live
        step_name = commands[spec.args]
        if step_name == "read_file" and not faulted:
            assert completed == ["write_file"]
            assert (live / "generated.txt").read_bytes() == expected
            faulted = True
            raise BeforeReadFailure
        bindings.append(binding)
        leases.append(lease)
        backends.append(backend)
        result = await run_command(
            backend, spec, lease, diagnostics=diagnostics, on_dispatch=on_dispatch
        )
        assert result.outcome == "completed" and result.exit_code == 0
        completed.append(step_name)
        return result

    async def observe_finalize(
        backend: LocalRunnerBackend,
        lease: WorkspaceLease,
        outcome: RunOutcome,
        *,
        retain: bool = False,
    ) -> None:
        binding = backend.local_workspace_binding(lease)
        assert binding is not None
        controller = binding.runtime_context.process_scopes
        assert isinstance(controller, LocalProcessScopeController)
        proof = await controller.owners.wait_terminal()
        assert proof.status == "released" and controller._closed
        assert not controller.owners.tasks and not controller.owners.quarantined
        assert len(controller.proofs) == 1
        native = controller.proofs[0]
        assert native.kernel_empty and native.streams_drained and native.custodian_reaped
        assert native.done and native.facade_exited
        assert all(task.done() for task in controller._tasks)
        assert not controller._connections and not controller._send_tails
        for scope in controller._scopes.values():
            assert scope.process is not None and scope.process.returncode == 0
            assert not scope.resources
            native_invocations.append((scope.launch.argv, scope.process.pid))
        controller._accepting.join(timeout=5)
        assert not controller._accepting.is_alive()
        root = Path(binding.workspace_root)
        assert json.loads((root / "identity.json").read_text())["runtime_state"] == "idle"
        assert (root / "work/main/generated.txt").read_bytes() == (
            expected if not finalized else edited
        )
        finalized.append(retain)
        await finalize_run(backend, lease, outcome, retain=retain)

    monkeypatch.setattr(
        CheckpointManager,
        "save_checkpoint",
        staticmethod(capture_save(CheckpointManager.save_checkpoint)),
    )
    monkeypatch.setattr(LocalRunnerBackend, "run_command", fail_before_read)
    monkeypatch.setattr(LocalRunnerBackend, "finalize_run", observe_finalize)
    runner = CliRunner()

    # When: writer completes, the reader boundary fails once, then a fresh CLI resumes that save.
    failed = runner.invoke(app, ["--silent", "run", str(path), "--no-interactive"])
    assert failed.exit_code == 1, failed.output
    assert faulted and finalized == [True] and len(checkpoints) == 1
    checkpoint = checkpoints[0]
    data = CheckpointManager.load_checkpoint(checkpoint)
    assert data.trigger == "failure" and data.current_agent == "read_file"
    assert data.context["execution_history"] == ["write_file"]
    assert data.context["agent_outputs"]["write_file"]["exit_code"] == 0
    assert data.workspace is not None and data.resume_contract is not None
    assert data.workspace["executed_backends"] == ["local"]
    identity = data.workspace["identities"]["local"]
    first = bindings[0]
    root = Path(first.workspace_root)
    assert identity["incarnation"] == first.incarnation and identity["location"] == str(root)
    assert json.loads((root / "identity.json").read_text())["runtime_state"] == "idle"
    cas = Path(first.bundle.store_path)
    cached = {p.relative_to(cas): p.read_bytes() for p in cas.rglob("*") if p.is_file()}
    assert cached[Path("tree/main") / asset.name] == original[asset]
    assert Path("tree/main/generated.txt") not in cached
    assert data.resume_contract["bundle_digest"] == first.bundle.digest
    (root / "work/main/generated.txt").write_bytes(edited)
    resumed = runner.invoke(
        app, ["--silent", "resume", str(path), "--from", str(checkpoint), "--no-interactive"]
    )

    # Then: the real reader observes edited bytes; cleanup follows positive native proof only.
    assert resumed.exit_code == 0, resumed.output
    assert json.loads(resumed.stdout) == {"result": edited.decode().strip()}
    assert completed == ["write_file", "read_file"]
    assert backends[0] is not backends[1] and leases[0] is not leases[1]
    assert bindings[1].incarnation == first.incarnation
    assert bindings[1].workspace_root == first.workspace_root
    assert finalized == [True, persistence == "durable"]
    assert checkpoints == [checkpoint]
    assert {p: p.read_bytes() for p in original} == original
    assert not (source / "generated.txt").exists()
    assert {p.relative_to(cas): p.read_bytes() for p in cas.rglob("*") if p.is_file()} == cached
    match persistence:
        case "durable":
            assert (root / "work/main/generated.txt").read_bytes() == edited
            assert json.loads((root / "identity.json").read_text())["runtime_state"] == "idle"
        case "on-failure":
            assert not root.exists()
        case unreachable:
            assert_never(unreachable)
    print(
        json.dumps(
            {
                "failure_checkpoint": str(checkpoint),
                "incarnation": first.incarnation,
                "native_commands": completed,
                "native_invocations": native_invocations,
                "policy": persistence,
                "source_cas_unchanged": True,
                "native_barriers": len(finalized),
            }
        )
    )
