"""Tests for Pi provider registration and static contract."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor.config.schema import AgentDef, RuntimeConfig
from conductor.exceptions import ProviderError
from conductor.providers.capabilities import get_capabilities, known_provider_names
from conductor.providers.factory import create_provider
from conductor.providers.pi import PiProvider


def test_pi_is_registered_without_starting_node() -> None:
    assert "pi" in known_provider_names()
    capabilities = get_capabilities("pi")
    assert capabilities.streaming_events is True
    assert capabilities.mcp_tools is False
    assert capabilities.workflow_tools_passthrough is False
    assert capabilities.max_session_seconds is True
    assert capabilities.checkpoint_resume is True


def test_pi_is_valid_runtime_and_agent_provider() -> None:
    assert RuntimeConfig.model_validate({"provider": "pi"}).provider.name == "pi"
    assert AgentDef(name="agent", prompt="test", provider="pi").provider == "pi"


async def test_factory_creates_pi_provider() -> None:
    provider = await create_provider("pi", validate=False)
    assert isinstance(provider, PiProvider)


def test_runner_and_manifests_live_with_provider() -> None:
    runner = PiProvider()._bridge
    assert runner.name == "runner.mjs"
    assert runner.parent.name == "_pi"
    assert runner.is_file()
    package = json.loads((runner.parent / "package.json").read_text())
    lock = json.loads((runner.parent / "package-lock.json").read_text())
    assert "@earendil-works/pi-coding-agent" in package["dependencies"]
    assert package["dependencies"] == lock["packages"][""]["dependencies"]


def test_runner_path_is_relative_to_installed_provider(tmp_path: Path) -> None:
    provider_file = tmp_path / "site-packages" / "conductor" / "providers" / "pi.py"
    with patch("conductor.providers.pi.__file__", str(provider_file)):
        assert PiProvider()._bridge == provider_file.parent / "_pi" / "runner.mjs"


async def test_execute_accepts_current_provider_contract() -> None:
    """Exercise bridge without starting Node or using personal credentials."""
    provider = PiProvider()
    process = MagicMock()
    process.stdin.drain = AsyncMock()
    process.stdout = asyncio.StreamReader()
    process.stdout.feed_data(
        (json.dumps({"type": "result", "text": "hello", "model": "test/model"}) + "\n").encode()
    )
    process.stdout.feed_eof()
    process.stderr.read = AsyncMock(return_value=b"")
    process.wait = AsyncMock(return_value=0)
    process.returncode = 0
    with patch(
        "conductor.providers.pi.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=process),
    ):
        output = await provider.execute(
            AgentDef(name="test", prompt="hello"),
            {},
            "hello",
            custom_agents=None,
            extra_mcp_servers=None,
            continuation_state=object(),
        )
    assert output.content == {"response": "hello"}
    assert output.model == "test/model"
    assert output.continuation_state is None
    process.wait.assert_awaited_once()


async def test_execute_returns_when_bridge_outlives_its_result() -> None:
    """A bridge that never closes stdout must not strand a finished agent."""
    provider = PiProvider()
    process = MagicMock()
    process.stdin.drain = AsyncMock()
    process.stdout = asyncio.StreamReader()
    process.stdout.feed_data(
        (json.dumps({"type": "result", "text": "hello", "model": "test/model"}) + "\n").encode()
    )
    # No feed_eof(): the live Pi session keeps the bridge process running.
    process.stderr = asyncio.StreamReader()
    process.stderr.feed_eof()
    process.returncode = None

    async def _wait() -> int:
        process.returncode = 0
        return 0

    process.wait = AsyncMock(side_effect=_wait)
    with patch(
        "conductor.providers.pi.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=process),
    ):
        output = await asyncio.wait_for(
            provider.execute(
                AgentDef(name="test", prompt="hello"),
                {},
                "hello",
                custom_agents=None,
                extra_mcp_servers=None,
                continuation_state=object(),
            ),
            timeout=10,
        )
    assert output.content == {"response": "hello"}
    process.terminate.assert_called_once()
    process.kill.assert_not_called()


@pytest.mark.parametrize(
    "kwargs",
    [{"custom_agents": [{"name": "child"}]}, {"extra_mcp_servers": {"server": {}}}],
)
async def test_execute_refuses_unsupported_plugin_components(kwargs: dict) -> None:
    provider = PiProvider()
    with (
        patch("conductor.providers.pi.asyncio.create_subprocess_exec") as spawn,
        pytest.raises(ProviderError, match="does not support Conductor plugin"),
    ):
        await provider.execute(AgentDef(name="test", prompt="hello"), {}, "hello", **kwargs)
    spawn.assert_not_called()
