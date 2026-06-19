"""Provider tier metadata in the workflow_started event (#241)."""

from __future__ import annotations

from typing import Any

import pytest

from conductor.config.schema import (
    AgentDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine


def _engine(agents: list[AgentDef], default_provider: str = "copilot") -> WorkflowEngine:
    config = WorkflowConfig(
        workflow=WorkflowDef(
            name="test",
            entry_point=agents[0].name,
            runtime=RuntimeConfig(provider=default_provider),
        ),
        agents=agents,
    )
    return WorkflowEngine(config=config, provider=None)


class TestProvidersBlock:
    def test_default_provider_recorded(self) -> None:
        engine = _engine([AgentDef(name="a", prompt="hi")])
        data = engine.build_workflow_started_data()
        providers = data["providers"]
        assert "copilot" in providers
        copilot = providers["copilot"]
        assert copilot["name"] == "copilot"
        assert copilot["status"] == "ok"
        assert copilot["tier"] == "stable"
        # The wire payload intentionally does NOT include the full
        # capability dump — it's not consumed by any frontend and would
        # bloat the JSONL. The CLI banner re-resolves capabilities from
        # the provider name when needed.
        assert "capabilities" not in copilot

    def test_per_agent_override_recorded(self) -> None:
        engine = _engine(
            [
                AgentDef(name="a", prompt="hi"),
                AgentDef(name="b", prompt="hi", provider="claude"),
            ]
        )
        data = engine.build_workflow_started_data()
        # Both providers appear in the providers block.
        assert "copilot" in data["providers"]
        assert "claude" in data["providers"]

    def test_agent_entries_include_provider_name(self) -> None:
        engine = _engine(
            [
                AgentDef(name="a", prompt="hi"),
                AgentDef(name="b", prompt="hi", provider="claude"),
            ]
        )
        data = engine.build_workflow_started_data()
        by_name = {a["name"]: a for a in data["agents"]}
        assert by_name["a"]["provider_name"] == "copilot"
        assert by_name["b"]["provider_name"] == "claude"

    def test_experimental_provider_surfaces_in_block(self) -> None:
        """claude-agent-sdk shows up with tier=experimental and an upstream_pin."""
        pytest.importorskip("claude_agent_sdk")
        engine = _engine([AgentDef(name="a", prompt="hi", provider="claude-agent-sdk")])
        data = engine.build_workflow_started_data()
        sdk_meta = data["providers"]["claude-agent-sdk"]
        assert sdk_meta["tier"] == "experimental"
        assert sdk_meta["upstream_pin"] is not None
        assert "claude-agent-sdk" in sdk_meta["upstream_pin"]

    def test_unknown_provider_gets_unresolved_stub(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When CAPABILITIES is missing/broken, emit status='unresolved' (NOT tier='unknown').

        The wire ``tier`` field stays constrained to the same Literal as
        ``ProviderCapabilities.tier``; a ``status`` discriminator
        differentiates resolved from fallback entries.
        """

        def fake(name: str) -> Any:
            raise AttributeError(f"no CAPABILITIES on {name}")

        monkeypatch.setattr("conductor.providers.capabilities.get_capabilities", fake)
        engine = _engine([AgentDef(name="a", prompt="hi")])
        data = engine.build_workflow_started_data()
        stub = data["providers"]["copilot"]
        assert stub["status"] == "unresolved"
        assert stub["tier"] is None
