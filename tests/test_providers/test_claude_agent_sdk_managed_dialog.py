"""Managed dialog namespace regressions against the real installed Claude SDK."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from conductor.exceptions import ProviderError
from conductor.providers.base import AgentProvider
from conductor.providers.claude_agent_sdk import ClaudeAgentSdkProvider
from tests.test_providers.managed_claude_sdk_fixture import native_harness

pytestmark = [
    pytest.mark.claude_auth_readiness_mocked,
    pytest.mark.skipif(sys.platform != "linux", reason="Native Unix fixture IPC requires Linux"),
]


@pytest.mark.parametrize("working_dir", [None, ""])
async def test_managed_dialog_missing_cwd_refuses_before_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, working_dir: str | None
) -> None:
    # Requirement: missing or empty managed cwd never falls back or starts a CLI.
    async with native_harness(tmp_path / "missing", monkeypatch) as h:
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        with pytest.raises(ProviderError) as error:
            await provider.execute_dialog_turn(
                "context", "dialog", managed_workspace=h.context, working_dir=working_dir
            )
        assert error.value.is_retryable is False
        assert h.processes == [] and h.events == [] and h.controller.proofs == ()
        assert provider._managed_workspaces == {}


async def test_native_managed_dialog_preserves_concurrent_nested_namespaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: same-root dialogs preserve their own selected nested cwd concurrently.
    async with native_harness(tmp_path / "dialog", monkeypatch) as h:
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        directories = [h.work / "nested" / "left", h.work / "nested" / "right"]
        for directory in directories:
            directory.mkdir(parents=True)
        outputs = await asyncio.gather(
            *(
                provider.execute_dialog_turn(
                    "context",
                    directory.name,
                    managed_workspace=h.context,
                    working_dir=str(directory),
                )
                for directory in directories
            )
        )
        assert outputs == ["fixture response", "fixture response"]
        for event_name in ("auth", "main"):
            observed = [event["cwd"] for event in h.events if event["event"] == event_name]
            assert sorted(observed) == sorted(str(directory) for directory in directories)
        await provider.release_workspace(h.context.root)
        assert (await h.controller.await_release()).status == "released"
        assert len(h.controller.proofs) == 6
        assert len({proof.invocation_id for proof in h.controller.proofs}) == 4
        assert all(
            proof.kernel_empty
            and proof.streams_drained
            and proof.facade_exited
            and proof.custodian_reaped
            and proof.done
            for proof in h.controller.proofs
        )


async def test_legacy_dialog_keeps_four_positional_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: the legacy super-call never receives managed dialog keywords.
    legacy = AsyncMock(return_value="legacy")
    monkeypatch.setattr(AgentProvider, "execute_dialog_turn", legacy)
    provider = ClaudeAgentSdkProvider(auth_mode="subscription")
    history = [{"role": "assistant", "content": "previous"}]
    output = await provider.execute_dialog_turn(
        "context", "dialog", history, "model", working_dir="/ignored"
    )
    assert output == "legacy"
    legacy.assert_awaited_once_with("context", "dialog", history, "model")
