"""Real SDK engine placement harness over the accepted executable Docker stand-in."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Coroutine, Iterator
from contextlib import asynccontextmanager, contextmanager

from conductor.config.environment import EnvironmentDocument, ResolvedEnvironment
from conductor.config.schema import WorkflowConfig
from conductor.engine.workflow import WorkflowEngine
from conductor.events import WorkflowEventEmitter
from tests.test_mcp.fixtures.docker_realm import _Realm
from tests.test_mcp.fixtures.docker_stdio import LaunchReceipt, PeerReceipt


@contextmanager
def preserve_primary() -> Iterator[None]:
    """Keep a test failure primary while exposing a secondary cleanup failure."""
    primary = sys.exception()
    try:
        yield
    except BaseException as secondary:
        # Test teardown must report both failures without replacing the test's cause.
        if primary is not None and secondary is not primary:
            raise primary from secondary
        raise


@asynccontextmanager
async def owned_opening[T](
    operation: Coroutine[None, None, T], release: asyncio.Event
) -> AsyncIterator[asyncio.Task[T]]:
    """Release the held boundary and join its opening task even if readiness fails."""
    opening = asyncio.create_task(operation)
    try:
        yield opening
    finally:
        with preserve_primary():
            primary = sys.exception()
            pending = not opening.done()
            release.set()
            if pending:
                opening.cancel()
            await asyncio.wait({opening})
            if not opening.cancelled() and (pending or primary is not None):
                opening.result()


def placement_config(*, cwd: str | None = None, failure: str = "none") -> WorkflowConfig:
    """Build distinct profiles with identical inline names and a shared-profile sibling."""
    return WorkflowConfig.model_validate(
        {
            "workflow": {
                "name": "placement",
                "entry_point": "first",
                "runtime": {
                    "working_dir": cwd,
                    "mcp_servers": {
                        "srv": {
                            "command": "peer",
                            "env": {"PEER_FAILURE": failure, "LC_CTYPE": "C.UTF-8"},
                        }
                    },
                },
            },
            "agents": [
                {"name": name, "type": "mcp", "server": "srv", "tool": "receive"}
                for name in ("call", "again")
            ],
            "for_each": [
                {
                    "name": name,
                    "type": "for_each",
                    "source": "workflow.input.items",
                    "as": "item",
                    "agent": {
                        "name": "same",
                        "type": "mcp",
                        "server": "srv",
                        "tool": "receive",
                        "execution": {"profile": profile},
                    },
                }
                for name, profile in (("first", "blue"), ("second", "orange"), ("third", "blue"))
            ],
        }
    )


@asynccontextmanager
async def placement_engine(config: WorkflowConfig, realm: _Realm) -> AsyncIterator[WorkflowEngine]:
    """Use real backend allocation and real SDK sessions; retain each peer's receipt."""
    environment = ResolvedEnvironment(
        document=EnvironmentDocument.model_validate(
            {
                "default": "blue",
                "profiles": {
                    "local": {"backend": "local"},
                    "blue": {"backend": "docker", "docker": {"image": "peer:image"}},
                    "orange": {
                        "backend": "docker",
                        "docker": {"image": "peer:image", "network": "none"},
                    },
                },
            }
        ),
        name="placement",
        source="path",
        path=None,
        digest="sha256:placement",
    )
    engine = WorkflowEngine(
        config, execution_environment=environment, event_emitter=WorkflowEventEmitter()
    )
    session = engine._execution_session
    session.backends["docker"] = realm.backend
    await session.prepare_leases(realm.run)
    lease = session.leases["docker"]
    engine.context.set_workflow_inputs({"items": ["a", "b"]})
    engine.limits.start()
    try:
        yield engine
    finally:
        with preserve_primary():
            await engine._close_mcp_step_managers()
            await session.finalize_leases("failed")
            if session.mcp_lifecycle.uncertain or session.sealed:
                # Independent QA disposal only after the retained owners have
                # terminated; this is not successful product finalization.
                for attempt in session.mcp_lifecycle.attempts:
                    assert attempt.task is None or attempt.task.done()
                    for entry in attempt.entries:
                        assert entry._cleanup is not None
                        await asyncio.wait_for(asyncio.shield(entry._cleanup), 10)
                mcp = realm.backend._lease_states[realm.backend._lease_key(lease)].mcp
                assert mcp is not None
                for owner in [*mcp.target_cleanups, *mcp.operations, *mcp.probes]:
                    done, pending = await asyncio.wait({owner}, timeout=5)
                    assert done and not pending and not owner.cancelled()
                    failure = owner.exception()
                    if failure is not None:
                        from conductor.execution.errors import ExecutionSpecError

                        assert isinstance(failure, ExecutionSpecError)
                state = json.loads((realm.root / "state.json").read_text())
                for metadata in state["containers"].values():
                    labels = metadata["Config"]["Labels"]
                    assert labels["io.conductor.run_id"] == lease.lease_id
                    assert labels["io.conductor.incarnation"] == lease.incarnation
                    rc, _out, _err = await realm.backend._run_docker(("rm", "-f", metadata["Id"]))
                    assert rc == 0
                for volume in state["volumes"]:
                    assert volume == realm.backend._volume_name(lease)
                    assert state["volumes"][volume]["io.conductor.incarnation"] == lease.incarnation
                    rc, _out, _err = await realm.backend._run_docker(("volume", "rm", "-f", volume))
                    assert rc == 0
            for path in realm.root.glob("conductor-mcp-*.launcher.json"):
                launch = LaunchReceipt.model_validate_json(path.read_text())
                peer = PeerReceipt.model_validate_json(
                    path.with_name(path.name.replace(".launcher.json", ".json")).read_text()
                )
                assert launch.joined and peer.closed
            state = json.loads((realm.root / "state.json").read_text())
            assert state["containers"] == {} and state["volumes"] == {}
