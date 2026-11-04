"""Root-owned retained-local lifetimes on the concrete filesystem backend."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

import pytest

from conductor.config.loader import load_config
from conductor.engine.checkpoint import CheckpointManager
from conductor.engine.workflow import RunContext, WorkflowEngine
from conductor.exceptions import CheckpointError, ExecutionError
from conductor.execution.errors import WorkspaceAttachError
from conductor.execution.local import LocalRunnerBackend
from conductor.execution.types import ManagedWorkspaceContext, RunSpec, WorkspaceLease
from conductor.providers.base import AgentOutput, AgentProvider
from conductor.providers.capabilities import ProviderCapabilities
from conductor.providers.openai import OpenAIProvider


@pytest.fixture
def retained_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise future public wiring without enabling the production capability."""
    original = LocalRunnerBackend.capabilities
    monkeypatch.setattr(
        LocalRunnerBackend,
        "capabilities",
        lambda self: replace(original(self), retained_workspace=True),
    )


class Provider(AgentProvider, abstract=True):
    """Deterministic model seam; workspace/controller and finalization remain real."""

    CAPABILITIES: ClassVar[ProviderCapabilities | None] = OpenAIProvider.CAPABILITIES

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[ManagedWorkspaceContext] = []
        self.released: list[str] = []
        self.closed = False

    async def execute(self, agent, context, rendered_prompt, **kwargs) -> AgentOutput:
        workspace = kwargs.get("managed_workspace")
        assert isinstance(workspace, ManagedWorkspaceContext)
        self.calls.append(workspace)
        marker = Path(workspace.root) / "work/main/mutable"
        if self.fail:
            marker.write_bytes(b"changed by first execution")
            raise ExecutionError("primary execution failure")
        assert marker.read_bytes() == b"changed by first execution"
        return AgentOutput(content={"result": "ok"}, raw_response="")

    async def release_workspace(self, workspace_root: str) -> None:
        self.released.append(workspace_root)

    async def validate_connection(self) -> bool:
        return True

    async def close(self) -> None:
        self.closed = True


def flow(tmp_path: Path, persistence: str = "on-failure") -> Path:
    path = tmp_path / "flow.yaml"
    path.write_text(
        "workflow:\n  name: lifetime\n  entry_point: worker\n"
        f"  workspace: {{persistence: {persistence}}}\n"
        "  runtime: {provider: openai}\n"
        "agents:\n  - name: worker\n    prompt: work\n    routes: [{to: $end}]\n"
        "output:\n  result: '{{ worker.output.result }}'\n"
    )
    return path


def engine(path: Path, provider: Provider) -> WorkflowEngine:
    return WorkflowEngine(
        load_config(path),
        provider,
        workflow_path=path,
        skip_gates=True,
        run_context=RunContext(run_id="root-lifetime"),
    )


async def test_failed_run_attaches_same_bytes_then_success_deletes(
    tmp_path: Path, retained_capability: None
) -> None:
    # Requirement: failure releases runtime; fresh-engine resume preserves bytes and identity.
    path = flow(tmp_path)
    first = Provider(fail=True)
    initial = engine(path, first)
    with pytest.raises(ExecutionError, match="primary execution failure"):
        await initial.run({})
    assert initial._last_checkpoint_path is not None
    checkpoint = CheckpointManager.load_checkpoint(initial._last_checkpoint_path)
    assert checkpoint.workspace is not None
    assert checkpoint.workspace["executed_backends"] == ["local"]
    identity = checkpoint.workspace["identities"]["local"]
    root = Path(identity["location"])
    assert json.loads((root / "identity.json").read_text())["runtime_state"] == "idle"
    assert first.released == [str(root)] and not first.closed
    second = Provider()
    resumed = engine(path, second)
    resumed.set_context(initial.context)
    assert await resumed.resume("worker", checkpoint=checkpoint) == {"result": "ok"}
    assert second.calls[0].incarnation == identity["incarnation"]
    assert second.released == [str(root)] and not second.closed
    assert not root.exists()


async def test_missing_required_local_identity_never_prepares_again(
    tmp_path: Path, retained_capability: None
) -> None:
    # Requirement: an executed local backend cannot be recreated from a damaged checkpoint.
    path = flow(tmp_path)
    initial = engine(path, Provider(fail=True))
    with pytest.raises(ExecutionError):
        await initial.run({})
    assert initial._last_checkpoint_path is not None
    checkpoint = CheckpointManager.load_checkpoint(initial._last_checkpoint_path)
    assert checkpoint.workspace is not None
    root = Path(checkpoint.workspace["identities"]["local"]["location"])
    before = (root / "identity.json").read_bytes()
    checkpoint.workspace["identities"].clear()
    with pytest.raises(CheckpointError, match="missing"):
        await engine(path, Provider()).resume("worker", checkpoint=checkpoint)
    assert (root / "identity.json").read_bytes() == before


async def test_shared_loop_foreign_task_survives_root_failure(
    tmp_path: Path, retained_capability: None
) -> None:
    # Requirement: direct engine cleanup owns neither the caller loop nor its foreign tasks.
    ready, finish = asyncio.Event(), asyncio.Event()

    async def foreign() -> None:
        ready.set()
        await finish.wait()

    sentinel = asyncio.create_task(foreign())
    await ready.wait()
    try:
        with pytest.raises(ExecutionError):
            await engine(flow(tmp_path), Provider(fail=True)).run({})
        assert not sentinel.done() and not sentinel.cancelling()
        assert not asyncio.get_running_loop().is_closed()
    finally:
        finish.set()
        await sentinel


@pytest.mark.parametrize("cancel", [False, True])
async def test_hung_distinct_finalizer_and_late_leaf_are_accounted_once(
    tmp_path: Path, retained_capability: None, cancel: bool
) -> None:
    # Requirement: one deadline bounds leaves/finalizers and prevents late state mutation.
    entered, interrupted = asyncio.Event(), asyncio.Event()
    release_entered, backend_entered = asyncio.Event(), asyncio.Event()
    finish, release_finish, backend_finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class HeldProvider(Provider, abstract=True):
        async def execute(self, agent, context, rendered_prompt, **kwargs) -> AgentOutput:
            workspace = kwargs["managed_workspace"]
            self.calls.append(workspace)
            entered.set()
            if cancel:
                try:
                    await finish.wait()
                except asyncio.CancelledError:
                    interrupted.set()
                    await finish.wait()
                callback = kwargs.get("event_callback")
                if callback is not None:
                    callback("agent_completed", {"late": True})
            return AgentOutput(content={"result": "ok"}, raw_response="")

        async def release_workspace(self, workspace_root: str) -> None:
            self.released.append(workspace_root)
            release_entered.set()
            await release_finish.wait()

    class OtherBackend(LocalRunnerBackend):
        def __init__(self) -> None:
            super().__init__()
            self.finalized: list[tuple[str, bool]] = []

        async def prepare_run(self, run: RunSpec) -> WorkspaceLease:
            return WorkspaceLease(run.run_id, "docker", "other")

        async def finalize_run(self, lease, outcome, *, retain=False) -> None:
            self.finalized.append((outcome, retain))
            backend_entered.set()
            await backend_finish.wait()

    provider = HeldProvider()
    run = engine(flow(tmp_path), provider)
    other = OtherBackend()
    run._execution_session.backends["docker"] = other
    run._execution_session.backends["alias"] = other
    task = asyncio.create_task(run.run({}))
    async with asyncio.timeout(5):
        try:
            await entered.wait()
            if cancel:
                task.cancel("primary cancellation")
                await interrupted.wait()
            await release_entered.wait()
            await backend_entered.wait()
            session = run._execution_session
            deadline = session.cleanup_deadline
            assert deadline is not None
            if cancel:
                task.cancel("second cancellation")
            session._cleanup_expired.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError) as caught:
                    await task
                assert caught.value.args == ("primary cancellation",)
            else:
                with pytest.raises(ExecutionError, match="unconfirmed"):
                    await task
            assert session.cleanup_deadline == deadline
            assert other.finalized == [("cancelled" if cancel else "succeeded", cancel)]
            assert session.sealed and session.cleanup_uncertain and session.leases == {}
            root = Path(provider.calls[0].root)
            marker = root / "identity.json"
            before = marker.read_bytes()
            assert json.loads(before)["runtime_state"] == "uncertain"
            checkpoint_files = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
            from conductor.engine.workspace_claim import _claim_directory

            assert not (_claim_directory() / "root-lifetime.json").exists()
            finish.set()
            release_finish.set()
            backend_finish.set()
            owners = set(session._resource_tasks)
            if owners:
                _, pending = await asyncio.wait(owners, timeout=2)
                assert not pending
            assert marker.read_bytes() == before
            assert all(p.read_bytes() == data for p, data in checkpoint_files.items())
            assert provider.released == [str(root)]
            assert other.finalized == [("cancelled" if cancel else "succeeded", cancel)]
            assert run._write_checkpoint(ExecutionError("late"), "failure") is None
            with pytest.raises(ExecutionError, match="unconfirmed"):
                await run.run({})
        finally:
            finish.set()
            release_finish.set()
            backend_finish.set()
            if not task.done():
                task.cancel()
            await asyncio.wait({task}, timeout=2)
            owners = set(run._execution_session._resource_tasks)
            if owners:
                await asyncio.wait(owners, timeout=2)


@pytest.mark.parametrize(
    "corruption", ["active", "uncertain", "incarnation", "source", "missing-source"]
)
async def test_resume_refuses_changed_source_or_marker_without_recreation(
    tmp_path: Path, retained_capability: None, corruption: str
) -> None:
    # Requirement: declared-source and marker gates precede attach/prepare and never heal a tree.
    path = flow(tmp_path)
    path.write_text(
        path.read_text().replace("  runtime:", "  bundle:\n    assets: [declared.txt]\n  runtime:")
    )
    source = tmp_path / "declared.txt"
    source.write_text("initial")
    initial = engine(path, Provider(fail=True))
    with pytest.raises(ExecutionError):
        await initial.run({})
    assert initial._last_checkpoint_path is not None
    checkpoint = CheckpointManager.load_checkpoint(initial._last_checkpoint_path)
    assert checkpoint.workspace is not None
    root = Path(checkpoint.workspace["identities"]["local"]["location"])
    marker = root / "identity.json"
    if corruption in ("active", "uncertain", "incarnation"):
        data = json.loads(marker.read_text())
        data["incarnation" if corruption == "incarnation" else "runtime_state"] = corruption
        marker.write_text(json.dumps(data))
    elif corruption == "source":
        source.write_text("changed declared source")
    else:
        source.unlink()
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with pytest.raises((CheckpointError, WorkspaceAttachError)):
        await engine(path, Provider()).resume("worker", checkpoint=checkpoint)
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


async def test_durable_two_resumes_keep_identity_and_live_bytes(
    tmp_path: Path, retained_capability: None
) -> None:
    # Requirement: durable generations attach the original incarnation without restaging.
    path = flow(tmp_path, "durable")
    initial = engine(path, Provider(fail=True))
    with pytest.raises(ExecutionError):
        await initial.run({})
    assert initial._last_checkpoint_path is not None
    checkpoint = CheckpointManager.load_checkpoint(initial._last_checkpoint_path)
    assert checkpoint.workspace is not None
    identity = dict(checkpoint.workspace["identities"]["local"])
    root = Path(identity["location"])
    (root / "work/main/late-file").write_bytes(b"retained live addition")
    for _ in range(2):
        provider = Provider()
        resumed = engine(path, provider)
        resumed.set_context(initial.context)
        assert await resumed.resume("worker", checkpoint=checkpoint) == {"result": "ok"}
        assert provider.calls[0].incarnation == identity["incarnation"]
        assert (root / "work/main/late-file").read_bytes() == b"retained live addition"
        assert json.loads((root / "identity.json").read_text())["runtime_state"] == "idle"
        assert resumed._execution_session.executed_backends() == {"local"}


async def test_close_api_error_with_complete_proof_is_only_a_warning(
    tmp_path: Path, retained_capability: None, caplog
) -> None:
    # Requirement: an API exception is not an unjoined owner when native proof is complete.
    class ApiFailure(Provider, abstract=True):
        async def execute(self, agent, context, rendered_prompt, **kwargs) -> AgentOutput:
            self.calls.append(kwargs["managed_workspace"])
            return AgentOutput(content={"result": "ok"}, raw_response="")

        async def release_workspace(self, workspace_root: str) -> None:
            self.released.append(workspace_root)
            raise RuntimeError("close API error")

    provider = ApiFailure()
    run = engine(flow(tmp_path), provider)
    assert await run.run({}) == {"result": "ok"}
    assert not run._execution_session.cleanup_uncertain
    assert provider.released == [provider.calls[0].root] and not provider.closed
    assert not Path(provider.calls[0].root).exists()
    assert "RuntimeError" in caplog.text or "close API error" in caplog.text


async def test_known_child_consumer_prepares_one_root_and_selects_child_namespace(
    tmp_path: Path, retained_capability: None
) -> None:
    # Requirement: a technical root includes known children with one finalization owner.
    child = tmp_path / "child"
    child.mkdir()
    (child / "flow.yaml").write_text(
        "workflow: {name: child, entry_point: worker, runtime: {provider: openai}}\n"
        "agents:\n  - name: worker\n    prompt: child\n    routes: [{to: $end}]\n"
        "output: {result: '{{ worker.output.result }}'}\n"
    )
    path = tmp_path / "flow.yaml"
    path.write_text(
        "workflow:\n  name: parent\n  entry_point: child\n"
        "  workspace: {persistence: on-failure}\n  runtime: {provider: openai}\n"
        "agents:\n  - name: child\n    type: workflow\n    workflow: ./child/flow.yaml\n"
        "    routes: [{to: $end}]\noutput: {result: '{{ child.output.result }}'}\n"
    )

    class ChildProvider(Provider, abstract=True):
        async def execute(self, agent, context, rendered_prompt, **kwargs) -> AgentOutput:
            self.calls.append(kwargs["managed_workspace"])
            assert Path(agent.working_dir).parts[-2:] == ("main", "child")
            return AgentOutput(content={"result": "ok"}, raw_response="")

    provider = ChildProvider()
    run = engine(path, provider)
    assert await run.run({}) == {"result": "ok"}
    assert len(provider.calls) == 1
    assert provider.released == [provider.calls[0].root]
    assert not provider.closed
    assert run._execution_session.executed_backends() == {"local"}


async def test_uncertain_marker_failure_leaves_active_without_local_dispatch(
    tmp_path: Path, retained_capability: None, monkeypatch, caplog
) -> None:
    # Requirement: failed uncertainty publication leaves active even without a local dispatch.
    from conductor.execution import local_workspace

    path = tmp_path / "flow.yaml"
    path.write_text(
        "workflow:\n  name: marker-failure\n  entry_point: mark\n"
        "  workspace: {persistence: on-failure}\n  runtime: {provider: openai}\n"
        "agents:\n  - {name: mark, type: set, value: ok, routes: [{to: $end}]}\n"
        "  - {name: worker, prompt: unused, routes: [{to: $end}]}\n"
    )
    entered, finish = asyncio.Event(), asyncio.Event()
    secret = "synthetic-marker-error-secret"
    native_state = local_workspace.set_state

    def set_state(run, lease, bundle, previous, state):
        if state == "uncertain":
            raise OSError(secret)
        return native_state(run, lease, bundle, previous, state)

    class OtherBackend(LocalRunnerBackend):
        async def prepare_run(self, run):
            return WorkspaceLease(run.run_id, "docker", "marker-other")

        async def finalize_run(self, lease, outcome, *, retain=False):
            entered.set()
            await finish.wait()

    monkeypatch.setattr(local_workspace, "set_state", set_state)
    run = engine(path, Provider())
    session = run._execution_session
    session.backends["docker"] = OtherBackend()
    task = asyncio.create_task(run.run({}))
    try:
        async with asyncio.timeout(5):
            await entered.wait()
            session.redactor.register((secret,))
            session._cleanup_expired.set()
            with pytest.raises(ExecutionError, match="unconfirmed"):
                await task
        identity = session.workspace_identities()["local"]
        assert identity.location is not None
        marker = Path(identity.location) / "identity.json"
        assert json.loads(marker.read_text())["runtime_state"] == "active"
        assert not session.executed_backends()
        assert secret not in caplog.text
    finally:
        finish.set()
        if not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=5)
        pending = set(session._resource_tasks)
        if pending:
            _, unfinished = await asyncio.wait(pending, timeout=5)
            assert not unfinished


async def test_grading_timeout_does_not_seal_the_root_or_break_fail_open(
    tmp_path: Path, retained_capability: None, monkeypatch
) -> None:
    # Requirement: invocation-local grading timeout preserves fail-open without weakening root stop.
    from conductor.engine.validator import ValidationOutcome
    from conductor.events import WorkflowEventEmitter

    path = flow(tmp_path)
    path.write_text(
        path.read_text().replace(
            "    prompt: work",
            "    prompt: work\n    timeout_seconds: 100\n"
            "    validator: {criteria: grade, max_retries: 0}",
        )
    )

    class PrimaryProvider(Provider, abstract=True):
        async def execute(self, agent, context, rendered_prompt, **kwargs) -> AgentOutput:
            self.calls.append(kwargs["managed_workspace"])
            return AgentOutput(content={"result": "ok"}, raw_response="")

    run = engine(path, PrimaryProvider())
    emitter = WorkflowEventEmitter()
    events = []
    emitter.subscribe(events.append)
    run._event_emitter = emitter
    ready = asyncio.Event()
    finish = asyncio.Event()
    box = {}

    def grade(*args, **kwargs):
        async def grading():
            ready.set()
            await finish.wait()
            return ValidationOutcome(passed=True)

        coroutine = grading()
        box["coroutine"] = coroutine
        return coroutine

    native_invoke = run._execution_session.invoke

    async def invoke(coroutine, *, nested_timeout=None):
        if coroutine is box.get("coroutine"):
            assert nested_timeout is not None
            box["timeout"] = nested_timeout
        return await native_invoke(coroutine, nested_timeout=nested_timeout)

    monkeypatch.setattr(run._output_validator, "validate", grade)
    monkeypatch.setattr(run._execution_session, "invoke", invoke)

    async def expire():
        await ready.wait()
        box["timeout"].reschedule(asyncio.get_running_loop().time())

    expiration = asyncio.create_task(expire())
    try:
        async with asyncio.timeout(5):
            assert await run.run({}) == {"result": "ok"}
        await expiration
        completed = [event for event in events if event.type == "agent_validator_complete"]
        assert len(completed) == 1 and completed[0].data["errored"]
        assert not run._execution_session.cleanup_uncertain
    finally:
        finish.set()
        if not expiration.done():
            expiration.cancel()
        await asyncio.wait({expiration}, timeout=5)
