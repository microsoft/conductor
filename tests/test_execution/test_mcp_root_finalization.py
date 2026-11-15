"""Root/session diagnostics after partial Docker resource finalization."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from conductor.config.environment import builtin_local_environment
from conductor.engine.execution_resolution import ExecutionResolverSession
from conductor.exceptions import ExecutionError
from conductor.execution.docker import DockerRunnerBackend
from conductor.execution.errors import ExecutionSpecError
from conductor.execution.types import RunSpec


@pytest.mark.parametrize("failure", ["guard-inspect-fails", "guard-removal-fails"])
async def test_root_reports_unconfirmed_cleanup_after_workspace_removal(
    fake_realm: tuple[DockerRunnerBackend, Path, Path],
    mcp_run: RunSpec,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    # Requirement: root warnings reflect partial cleanup without authorizing poisoned reuse.
    backend, log, store = fake_realm
    session = ExecutionResolverSession(builtin_local_environment())
    session.backends["docker"] = backend
    await session.prepare_leases(mcp_run)
    lease = session.leases["docker"]
    root = session.mcp_lifecycle
    original = json.loads(store.read_text())
    volume = f"conductor-ws-{lease.lease_id}"
    assert set(original["volumes"]) == {volume}
    guard = next(iter(original["containers"].values()))
    guard_name = guard["Name"].removeprefix("/")
    assert guard["Config"]["Labels"]["io.conductor.resource"] == "mcp-fence"
    assert guard["Config"]["Labels"]["io.conductor.run_id"] == lease.lease_id
    assert guard["Config"]["Labels"]["io.conductor.incarnation"] == lease.incarnation
    assert len(guard["Id"]) == 64
    backend._cli_env["FAKE_DOCKER_SCENARIO"] = failure
    caplog.clear()

    await session.finalize_leases("failed", retain=False)

    final = json.loads(store.read_text())
    assert final["volumes"] == {}
    assert final["containers"] == original["containers"]
    rows = [json.loads(line)["argv"] for line in log.read_text().splitlines()]
    removal = rows.index(["volume", "rm", "-f", volume])
    assert rows[removal + 1] == ["inspect", "--format", "{{json .}}", guard_name]
    assert rows[removal + 2 :] == (
        [["rm", "-f", guard["Id"]]] if failure == "guard-removal-fails" else []
    )
    assert session.leases == {}
    assert session.sealed and session.cleanup_uncertain and session.quarantined
    assert root.sealed and root.uncertain
    assert session._cleanup_task is not None and session._cleanup_task.done()
    assert session._cleanup_task.result() is None
    attempt = root.attempts[0]
    assert attempt.committed and attempt.lease is lease
    assert attempt.release is not None and attempt.release.done()
    assert isinstance(attempt.release.exception(), ExecutionSpecError)
    with pytest.raises(ExecutionError, match="cannot be reused"):
        session.begin_generation()
    with pytest.raises(ExecutionSpecError, match="admission is sealed"):
        root.check_admission()
    assert session.mcp_lifecycle is root and root.uncertain

    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert [record.name for record in warnings] == [
        "conductor.execution.docker",
        "conductor.engine.mcp_lifecycle",
    ]
    assert all(record.levelno == logging.WARNING for record in warnings)
    backend_warning, root_warning = [record.getMessage() for record in warnings]
    assert "disposition is unconfirmed" in backend_warning
    assert "independent verification" in backend_warning
    assert "root cleanup is unconfirmed" in root_warning
    assert "workspace and fence" in root_warning
    assert "independent verification" in root_warning
    assert "reuse refused" in root_warning
    assert all("retained" not in warning for warning in (backend_warning, root_warning))
    assert "SECRET_DAEMON_VALUE" not in caplog.text
