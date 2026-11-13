"""Session wiring requirements for retained execution workspaces."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from conductor.config.environment import builtin_local_environment
from conductor.config.loader import load_config
from conductor.engine.execution_resolution import BACKEND_FACTORIES, ExecutionResolverSession
from conductor.engine.run_manifest import compile_run_manifest
from conductor.engine.workspace_claim import acquire_workspace_claim
from conductor.exceptions import ConfigurationError
from conductor.execution import (
    LocalRunnerBackend,
    RunnerCapabilities,
    RunSpec,
    WorkspaceIdentity,
    WorkspaceLease,
)
from conductor.execution.types import BundleRef, CommandSpec


@dataclass
class RetainedBackend(LocalRunnerBackend):
    prepared: list[RunSpec] = field(default_factory=list)
    attached: list[tuple[RunSpec, WorkspaceIdentity, bool]] = field(default_factory=list)
    finalized: list[tuple[WorkspaceLease, str, bool]] = field(default_factory=list)

    def capabilities(self) -> RunnerCapabilities:
        return RunnerCapabilities(True, False, True, False, retained_workspace=True)

    async def prepare_run(self, run: RunSpec) -> WorkspaceLease:
        self.prepared.append(run)
        return WorkspaceLease(run.run_id, "docker", "new")

    async def attach_run(
        self, run: RunSpec, identity: WorkspaceIdentity, *, expect_staged: bool = False
    ) -> WorkspaceLease:
        self.attached.append((run, identity, expect_staged))
        return WorkspaceLease(identity.lease_id, "docker", identity.incarnation)

    async def finalize_run(self, lease, outcome, *, retain: bool = False) -> None:
        self.finalized.append((lease, outcome, retain))


def _session(docker: RetainedBackend) -> ExecutionResolverSession:
    session = ExecutionResolverSession(builtin_local_environment())
    session.backends["docker"] = docker
    return session


@pytest.mark.asyncio
async def test_local_lease_gets_no_retention_while_docker_gets_policy() -> None:
    # Requirement: local prepare succeeds even for a Docker-retained run.
    docker = RetainedBackend()
    session = _session(docker)
    await session.prepare_leases(RunSpec("run", workspace_persistence="durable"))
    assert session.leases["local"].backend == "local"
    assert docker.prepared[0].workspace_persistence == "durable"
    assert set(session.workspace_identities()) == {"docker"}
    await session.finalize_leases("succeeded")


@pytest.mark.asyncio
async def test_resume_attaches_only_named_backend_with_staging_flag() -> None:
    # Requirement: identity routes to attach while the local backend prepares normally.
    docker = RetainedBackend()
    session = _session(docker)
    identity = WorkspaceIdentity("docker", "run", "saved")
    await session.prepare_leases(
        RunSpec("run", workspace_persistence="on-failure"),
        resume_identities={"docker": identity},
        expect_staged={"docker": True, "local": False},
    )
    assert docker.attached == [(RunSpec("run", workspace_persistence="on-failure"), identity, True)]
    assert docker.prepared == []
    assert session.leases["local"].backend == "local"
    assert session.workspace_identities()["docker"].incarnation == "saved"
    await session.finalize_leases("succeeded", retain=True)
    assert docker.finalized[0][1:] == ("succeeded", True)


@pytest.mark.asyncio
async def test_unknown_backend_identity_is_configuration_error() -> None:
    # Requirement: checkpoint identities never disappear silently.
    session = ExecutionResolverSession(builtin_local_environment())
    with pytest.raises(ConfigurationError, match="missing"):
        await session.prepare_leases(
            RunSpec("run"), resume_identities={"missing": WorkspaceIdentity("missing", "run", "x")}
        )
    assert session.leases == {}


@pytest.mark.asyncio
async def test_late_backend_attaches_using_saved_identity(monkeypatch) -> None:
    # Requirement: a late-discovered backend uses the stored per-backend resume map.
    docker = RetainedBackend()
    monkeypatch.setitem(BACKEND_FACTORIES, "docker", lambda: docker)
    session = ExecutionResolverSession(builtin_local_environment())
    identity = WorkspaceIdentity("docker", "run", "saved")
    await session.prepare_leases(
        RunSpec("run", bundle=BundleRef("sha256:x", "/tmp/store")),
        resume_identities={"docker": identity},
    )
    await session.ensure_backends(["docker"])
    assert docker.attached[0][1:] == (identity, False)


def test_executed_backend_history_is_union() -> None:
    # Requirement: a fresh process cannot erase history restored from checkpoint.
    session = ExecutionResolverSession(builtin_local_environment())
    session.restore_executed_backends(["docker"])
    session.record_backend_execution("local")
    session.restore_executed_backends([])
    assert session.executed_backends() == frozenset({"docker", "local"})


async def test_technical_local_is_not_retained_even_when_capable(monkeypatch) -> None:
    # Requirement: capability alone never creates or exports a technical local filesystem identity.
    original = LocalRunnerBackend.capabilities
    monkeypatch.setattr(
        LocalRunnerBackend,
        "capabilities",
        lambda self: replace(original(self), retained_workspace=True),
    )
    docker = RetainedBackend()
    session = _session(docker)
    await session.prepare_leases(RunSpec("technical", workspace_persistence="durable"))
    assert session.leases["local"].location is None
    assert set(session.workspace_identities()) == {"docker"}
    await session.finalize_leases("succeeded")


@pytest.mark.parametrize("claim_active", [False, True])
async def test_late_local_consumer_uses_original_bundle_under_claim(
    tmp_path: Path, monkeypatch, claim_active: bool
) -> None:
    # Requirement: late preparation is claim-fenced and binds its original published topology.
    original = LocalRunnerBackend.capabilities
    monkeypatch.setattr(
        LocalRunnerBackend,
        "capabilities",
        lambda self: replace(original(self), retained_workspace=True),
    )
    path = tmp_path / "root.yaml"
    path.write_text(
        "workflow:\n  name: late\n  entry_point: mark\n"
        "  bundle: {assets: [late.py]}\n"
        "agents:\n  - {name: mark, type: set, value: ok, routes: [{to: $end}]}\n"
    )
    (tmp_path / "late.py").write_text(
        "from pathlib import Path\nPath('late-result').write_text('late bytes')\n"
    )
    child_path = tmp_path / "child.yaml"
    child_path.write_text(
        "workflow: {name: child, entry_point: script}\n"
        "agents:\n  - name: script\n    type: script\n    command: python\n"
        "    routes: [{to: $end}]\n"
    )
    environment = builtin_local_environment()
    session = ExecutionResolverSession(environment)
    claim = acquire_workspace_claim("late-local") if claim_active else None
    try:
        await session.prepare_leases(
            RunSpec("late-local", workspace_persistence="durable"),
            workflow_path=path,
            claim_active=claim is not None,
        )
        technical = session.leases["local"]
        assert technical.location is None and not session.workspace_identities()
        child = load_config(child_path)
        session.note_participants(
            child, compile_run_manifest(child, workflow_path=child_path, environment=environment)
        )
        if not claim_active:
            with pytest.raises(ConfigurationError, match="claim"):
                await session.ensure_backends(["local"])
            assert session.leases["local"] is technical
            assert session.workspace_identities() == {}
        else:
            await session.ensure_backends(["local"])
            backend = session.backends["local"]
            assert isinstance(backend, LocalRunnerBackend)
            lease = session.leases["local"]
            binding = backend.local_workspace_binding(lease)
            assert binding is not None and session.run_bundle is binding.bundle
            assert session.agent_dependencies == ()
            result = await backend.run_command(
                CommandSpec(
                    str(Path(sys.executable)),
                    ("./late.py",),
                    working_dir=str(Path(binding.work_root) / "main"),
                ),
                lease,
                on_dispatch=lambda: session.record_backend_execution("local"),
            )
            assert result.exit_code == 0
            assert (Path(binding.work_root) / "main/late-result").read_text() == "late bytes"
            assert session.executed_backends() == {"local"}
    finally:
        await session.finalize_leases("failed", retain=True)
        if claim is not None:
            claim.release()
