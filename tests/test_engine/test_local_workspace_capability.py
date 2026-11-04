"""Production retained-local admission and mixed consumer boundaries."""

import json
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

from conductor.config.environment import (
    DockerProfileOptions,
    EnvironmentDocument,
    ProfileDefinition,
    ResolvedEnvironment,
    builtin_local_environment,
)
from conductor.config.loader import load_config
from conductor.config.schema import WorkflowConfig
from conductor.config.validator import validate_workflow_config
from conductor.engine.checkpoint import CheckpointManager
from conductor.engine.execution_resolution import BACKEND_FACTORIES
from conductor.engine.run_manifest import compile_run_manifest
from conductor.engine.workflow import RunContext, WorkflowEngine
from conductor.engine.workspace_claim import WorkspaceClaimError, acquire_workspace_claim
from conductor.exceptions import ConfigurationError
from conductor.execution.docker import DockerRunnerBackend
from conductor.execution.errors import WorkspaceAttachError
from tests.test_execution.test_workspace_retention import install_docker_processes, restore


@pytest.fixture(params=["manifest", "validator"])
def admit(request: pytest.FixtureRequest) -> Callable[[WorkflowConfig, Path | None], None]:
    """Exercise the same admission rules on static validation and ordinary run compilation."""
    environment = builtin_local_environment()

    def invoke(config: WorkflowConfig, path: Path | None) -> None:
        if request.param == "manifest":
            compile_run_manifest(config, workflow_path=path, environment=environment)
        else:
            validate_workflow_config(
                config,
                workflow_path=path,
                _environment_context={
                    "refs_found": False,
                    "environments": {"local": environment},
                    "explicit": True,
                    "root_workflow_dir": path.parent if path is not None else None,
                    "warned_no_environments": False,
                    "warned_no_secret_environments": False,
                },
            )

    return invoke


@pytest.mark.parametrize("field", ["working_dir", "settings_dir"])
def test_retained_local_refuses_escaping_directory_before_execution(
    field: str, admit: Callable[[WorkflowConfig, Path | None], None]
) -> None:
    # Requirement: compile/run admission rejects directory escapes without rendering a payload.
    config = WorkflowConfig.model_validate(
        {
            "workflow": {
                "name": "unsafe",
                "entry_point": "worker",
                "workspace": {"persistence": "durable"},
                "runtime": {"provider": "claude-agent-sdk"},
            },
            "agents": [{"name": "worker", field: "../outside"}],
        }
    )
    with pytest.raises(ConfigurationError) as caught:
        admit(config, None)
    assert "namespace_escape" in str(caught.value)


def test_retained_local_requires_managed_provider_without_explicit_cwd(
    admit: Callable[[WorkflowConfig, Path | None], None],
) -> None:
    # Requirement: default cwd still requires managed provider ownership and release support.
    config = WorkflowConfig.model_validate(
        {
            "workflow": {
                "name": "unsupported",
                "entry_point": "worker",
                "workspace": {"persistence": "durable"},
                "runtime": {"provider": {"name": "aca", "pool_endpoint": "https://unused.invalid"}},
            },
            "agents": [{"name": "worker"}],
        }
    )
    with pytest.raises(ConfigurationError) as caught:
        admit(config, None)
    assert "managed_local_workspace" in str(caught.value)


def test_production_local_script_manifest_accepts_retention() -> None:
    # Requirement: the real local backend admits retained scripts without a retaining double.
    config = WorkflowConfig.model_validate(
        {
            "workflow": {
                "name": "local",
                "entry_point": "script",
                "workspace": {"persistence": "on-failure"},
            },
            "agents": [{"name": "script", "type": "script", "command": "true"}],
        }
    )
    manifest = compile_run_manifest(
        config, workflow_path=None, environment=builtin_local_environment()
    )
    assert manifest.workspace is not None
    assert manifest.workspace.persistence == "on-failure"


def test_known_child_inherits_retained_provider_admission(
    tmp_path: Path, admit: Callable[[WorkflowConfig, Path | None], None]
) -> None:
    # Requirement: a technical root cannot hide an unsupported managed child provider.
    child = tmp_path / "child.yaml"
    child.write_text(
        "workflow:\n  name: child\n  entry_point: worker\n"
        "  runtime:\n    provider: {name: aca, pool_endpoint: 'https://unused.invalid'}\n"
        "agents:\n  - name: worker\n    routes: [{to: $end}]\n"
    )
    root = tmp_path / "root.yaml"
    root.write_text(
        "workflow:\n  name: root\n  entry_point: child\n"
        "  workspace: {persistence: durable}\n"
        "agents:\n  - name: child\n    type: workflow\n    workflow: child.yaml\n"
        "    routes: [{to: $end}]\n"
    )
    with pytest.raises(ConfigurationError):
        admit(load_config(root), root)


async def test_literal_directory_created_by_prior_step_is_admitted(tmp_path: Path) -> None:
    # Requirement: static checks allow staged directories created by earlier real payloads.
    path = tmp_path / "flow.yaml"
    path.write_text(
        "workflow:\n  name: generated\n  entry_point: create\n"
        "  workspace: {persistence: on-failure}\n"
        "agents:\n  - name: create\n    type: script\n"
        f"    command: {json.dumps(sys.executable)}\n"
        "    args: ['-c', 'from pathlib import Path; Path(\"generated\").mkdir()']\n"
        "    routes: [{to: consume}]\n"
        "  - name: consume\n    type: script\n"
        f"    command: {json.dumps(sys.executable)}\n"
        "    args: ['-c', 'from pathlib import Path; assert Path.cwd().name == \"generated\"']\n"
        "    working_dir: generated\n"
        "    routes: [{to: $end}]\n"
    )
    instance = WorkflowEngine(
        load_config(path),
        workflow_path=path,
        run_context=RunContext(run_id="generated-directory"),
        skip_gates=True,
    )
    await instance.run({})
    assert instance.context.execution_history == ["create", "consume"]


@pytest.fixture(params=["local", "docker"])
def lifecycle(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, Path, Callable[[str], WorkflowEngine]]:
    """Use the native local tree or the existing disk-backed Docker CLI fake."""
    backend = request.param
    home = tmp_path / "home"
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))
    if backend == "docker":
        original_path = os.environ["PATH"]
        _, binary, store, _ = install_docker_processes(tmp_path, monkeypatch)
        monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + original_path)
        monkeypatch.setitem(BACKEND_FACTORIES, "docker", lambda: DockerRunnerBackend(binary))
        root = store / "volumes/conductor-ws-capability-parity"
    else:
        root = home / "workspaces/capability-parity/local"
    source = tmp_path / "source"
    source.mkdir()
    path = source / "flow.yaml"
    (source / "payload.txt").write_bytes(b"source")
    environment = ResolvedEnvironment(
        name="parity",
        source="path",
        path=None,
        digest="sha256:parity",
        document=EnvironmentDocument(
            default="target",
            profiles={
                "target": ProfileDefinition(
                    backend=backend,
                    docker=DockerProfileOptions(image="busybox:test")
                    if backend == "docker"
                    else None,
                )
            },
        ),
    )

    def make(persistence: str) -> WorkflowEngine:
        # Preserve source bytes across run/resume so the digest is meaningful.
        text = (
            "workflow:\n  name: parity\n  entry_point: payload\n"
            f"  workspace: {{persistence: {persistence}}}\n"
            "  bundle: {assets: [payload.txt]}\n"
            "agents:\n  - name: payload\n    type: script\n"
            f"    command: {json.dumps(sys.executable)}\n"
            "    args: ['-c', 'pass']\n"
            "    routes: [{to: $end}]\n"
        )
        if not path.exists():
            path.write_text(text)
        return WorkflowEngine(
            load_config(path),
            workflow_path=path,
            execution_environment=environment,
            run_context=RunContext(run_id="capability-parity"),
            skip_gates=True,
        )

    return backend, root, make


async def test_lifecycle_failure_attach_success_deletes(lifecycle, tmp_path: Path) -> None:
    # Requirement: both concrete realms retain changed bytes, attach, then delete on success.
    backend, root, make = lifecycle
    first = make("on-failure")
    mutable = root / ("work/main/payload.txt" if backend == "local" else "main/payload.txt")

    def fail_store(name, value) -> None:
        mutable.write_bytes(b"retained change")
        raise RuntimeError("after dispatch")

    with (
        patch.object(first.context, "store", side_effect=fail_store),
        patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
        pytest.raises(RuntimeError),
    ):
        await first.run({})
    assert first._last_checkpoint_path is not None
    checkpoint = CheckpointManager.load_checkpoint(first._last_checkpoint_path)
    assert checkpoint.workspace is not None
    assert checkpoint.workspace["executed_backends"] == [backend]
    assert mutable.read_bytes() == b"retained change"
    resumed = make("on-failure")
    restore(resumed, checkpoint)
    original = resumed.context.store

    def observe(name, value) -> None:
        assert mutable.read_bytes() == b"retained change"
        original(name, value)

    with patch.object(resumed.context, "store", side_effect=observe):
        await resumed.resume(checkpoint.current_agent, checkpoint=checkpoint)
    assert not root.exists()


async def test_lifecycle_durable_preserves_on_success(lifecycle) -> None:
    # Requirement: durable success leaves the owned resource intact in both realms.
    backend, root, make = lifecycle
    await make("durable").run({})
    assert root.is_dir()
    if backend == "local":
        assert json.loads((root / "identity.json").read_text())["runtime_state"] == "idle"
    else:
        assert (root / ".conductor-staged").is_file()


async def test_lifecycle_claim_conflict_has_zero_payload(lifecycle) -> None:
    # Requirement: an already claimed run cannot create a workspace or dispatch any payload.
    _, root, make = lifecycle
    claim = acquire_workspace_claim("capability-parity")
    try:
        with pytest.raises(WorkspaceClaimError):
            await make("durable").run({})
    finally:
        claim.release()
    assert not root.exists()


@pytest.mark.parametrize("damage", ["missing", "marker", "unknown_attempt"])
async def test_lifecycle_resume_refuses_damage_or_reruns_unknown_attempt(
    lifecycle, tmp_path: Path, damage: str
) -> None:
    # Requirement: missing/drifted resources never restage; unknown attempts rerun at least once.
    backend, root, make = lifecycle
    first = make("on-failure")
    with (
        patch.object(first.context, "store", side_effect=RuntimeError("after dispatch")),
        patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
        pytest.raises(RuntimeError),
    ):
        await first.run({})
    assert first._last_checkpoint_path is not None
    checkpoint = CheckpointManager.load_checkpoint(first._last_checkpoint_path)
    if damage == "missing":
        shutil.rmtree(root)
    elif damage == "marker":
        marker = root / ("identity.json" if backend == "local" else ".conductor-staged")
        marker.write_bytes(b"drift")
    else:
        assert checkpoint.interrupted_step is not None
        checkpoint.interrupted_step.pop("attempt_id", None)
    resumed = make("on-failure")
    restore(resumed, checkpoint)
    if damage == "unknown_attempt":
        await resumed.resume(checkpoint.current_agent, checkpoint=checkpoint)
        assert not root.exists()
    else:
        expected = (
            ConfigurationError
            if backend == "docker" and damage == "marker"
            else WorkspaceAttachError
        )
        with pytest.raises(expected):
            await resumed.resume(checkpoint.current_agent, checkpoint=checkpoint)
        assert root.exists() == (damage != "missing")
