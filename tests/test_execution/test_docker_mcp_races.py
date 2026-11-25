"""Creation and cleanup races at the Docker CLI boundary."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from conductor.execution.errors import ExecutionSpecError
from conductor.execution.types import (
    McpServerSpec,
    RunSpec,
)

from .test_docker_mcp import FakeRealm


async def test_repeated_interruptions_share_one_target_cleanup(
    fake_realm: FakeRealm,
    mcp_run: RunSpec,
    mcp_spec: McpServerSpec,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: repeated cancellation cannot abandon or duplicate an owned cleanup.
    backend, log, store = fake_realm
    original = backend._run_docker
    entered, release = asyncio.Event(), asyncio.Event()
    identifier = ""

    async def held_remove(
        argv: Sequence[str],
        *,
        stdin_bytes: bytes | None = None,
        timeout: float | None = 300.0,
        diagnostics: Callable[[str], None] | None = None,
    ) -> tuple[int, str, str]:
        if tuple(argv) == ("rm", "-f", identifier):
            entered.set()
            await release.wait()
        return await original(
            argv, stdin_bytes=stdin_bytes, timeout=timeout, diagnostics=diagnostics
        )

    monkeypatch.setattr(backend, "_run_docker", held_remove)
    lease = await backend.prepare_run(mcp_run)
    target = await backend.open_mcp_stdio(mcp_spec, lease)
    target.mark_launch_attempted()
    await backend._run_docker(target.args)
    await target.validate_started()
    identifier = json.loads(store.read_text())["containers"][target.identity]["Id"]
    closing = asyncio.ensure_future(target.aclose())
    await asyncio.wait_for(entered.wait(), 5)
    closing.cancel()
    # Deliver each cancellation separately while the same daemon removal is still blocked.
    await asyncio.sleep(0)
    assert not closing.done()
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await target.aclose()
    rows = [json.loads(line)["argv"] for line in log.read_text().splitlines()]
    assert rows.count(["rm", "-f", identifier]) == 1
    await backend.finalize_run(lease, "succeeded")


@pytest.mark.parametrize("fault", ["create", "volume", "volume_remove", "inspect", "unlink"])
async def test_failed_create_never_starts_and_sweep_respects_receipt(
    fake_realm: FakeRealm,
    mcp_run: RunSpec,
    mcp_spec: McpServerSpec,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    # Requirement: post-create failures never start a peer or authorize removing a replacement.
    from conductor.execution.errors import WorkspaceAttachError

    backend, log, store = fake_realm
    lease = await backend.prepare_run(mcp_run)
    original = backend._run_docker
    identifier = ""
    env_path = None

    async def intercept(argv, **kwargs):
        nonlocal identifier, env_path
        if argv[0] == "create" and "--env-file" in argv:
            env_path = Path(argv[argv.index("--env-file") + 1])
            if fault == "create":
                return 1, "", "PRIVATE_CREATE_FAILURE"
            result = await original(argv, **kwargs)
            identifier = result[1].strip()
            if fault in {"volume", "volume_remove"}:
                state = json.loads(store.read_text())
                state["volumes"][backend._volume_name(lease)] = {}
                store.write_text(json.dumps(state))
            return result
        if fault == "volume_remove" and tuple(argv) == ("rm", "-f", identifier):
            return 1, "", "removal denied"
        if fault == "inspect" and argv[0] == "inspect" and argv[-1] == identifier:
            return 1, "", "PRIVATE_INSPECT_FAILURE"
        return await original(argv, **kwargs)

    monkeypatch.setattr(backend, "_run_docker", intercept)
    if fault == "unlink":

        def fail_unlink(path):
            raise PermissionError("cannot unlink private env-file")

        monkeypatch.setattr("conductor.execution.docker._unlink_with_retry", fail_unlink)
    expected = {
        "volume": WorkspaceAttachError,
        "volume_remove": WorkspaceAttachError,
        "unlink": PermissionError,
    }.get(fault, ExecutionSpecError)
    with pytest.raises(expected) as failure:
        await backend.open_mcp_stdio(mcp_spec, lease)
    assert "PRIVATE_" not in str(failure.value)
    assert env_path is not None
    assert env_path.exists() == (fault == "unlink")
    if fault in {"inspect", "unlink"}:
        state = json.loads(store.read_text())
        (replacement,) = state["containers"].values()
        assert replacement["Id"] == identifier
        replacement["Id"] = "b" * 64
        store.write_text(json.dumps(state))
    monkeypatch.setattr(backend, "_run_docker", original)
    await backend.finalize_run(lease, "failed")
    remaining = json.loads(store.read_text())
    if fault in {"inspect", "unlink"}:
        assert [c["Id"] for c in remaining["containers"].values()] == ["b" * 64]
    else:
        assert remaining["containers"] == {}
    if fault in {"volume", "volume_remove"}:
        assert remaining["volumes"] == {backend._volume_name(lease): {}}
    assert not any(json.loads(line)["argv"][0] == "start" for line in log.read_text().splitlines())


async def test_name_reassigned_after_sweep_inspect_removes_only_original_id(
    fake_realm: FakeRealm,
    mcp_run: RunSpec,
    mcp_spec: McpServerSpec,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: a verified sweep candidate can be renamed between its inspect and its removal.
    backend, log, store = fake_realm
    lease = await backend.prepare_run(mcp_run)
    target = await backend.open_mcp_stdio(mcp_spec, lease)
    name, original_id = target.identity, target.args[-1]
    original = backend._run_docker
    replacement_id = "b" * 64

    async def rename_after_inspect(argv, **kwargs):
        result = await original(argv, **kwargs)
        if tuple(argv) == ("inspect", "--format", "{{json .}}", name):
            state = json.loads(store.read_text())
            owned = state["containers"].pop(name)
            assert owned["Id"] == original_id
            state["containers"]["renamed"] = {**owned, "Name": "/renamed"}
            state["containers"][name] = {**owned, "Id": replacement_id}
            store.write_text(json.dumps(state))
        return result

    monkeypatch.setattr(backend, "_run_docker", rename_after_inspect)
    before = len(log.read_text().splitlines())
    await backend.finalize_run(lease, "cancelled", retain=True)
    containers = json.loads(store.read_text())["containers"]
    assert {key: item["Id"] for key, item in containers.items()} == {name: replacement_id}
    commands = [json.loads(line)["argv"] for line in log.read_text().splitlines()[before:]]
    assert [argv for argv in commands if argv[0] == "rm"] == [["rm", "-f", original_id]]
