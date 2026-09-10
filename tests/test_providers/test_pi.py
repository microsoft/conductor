"""Tests for Pi provider registration and static contract."""

import json
from pathlib import Path
from unittest.mock import patch

from conductor.config.schema import AgentDef, RuntimeConfig
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
    assert RuntimeConfig(provider="pi").provider.name == "pi"
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
