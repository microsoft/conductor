"""Session wiring requirements for retained execution workspaces."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from conductor.config.environment import builtin_local_environment
from conductor.engine.execution_resolution import BACKEND_FACTORIES, ExecutionResolverSession
from conductor.exceptions import ConfigurationError
from conductor.execution import (
    LocalRunnerBackend,
    RunnerCapabilities,
    RunSpec,
    WorkspaceIdentity,
    WorkspaceLease,
)
from conductor.execution.types import BundleRef


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
