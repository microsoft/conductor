"""Tests for the ``plugins:`` schema surface.

Covers the string/object coercion, the per-component defaults, the
tri-state inheritance signal, and the step-type rejections.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conductor.config.schema import AgentDef, PluginDef, RuntimeConfig


class TestCoercion:
    def test_string_shorthand_expands(self) -> None:
        runtime = RuntimeConfig.model_validate({"plugins": ["prs"]})
        assert runtime.plugins == [PluginDef(name="prs")]

    def test_object_form(self) -> None:
        runtime = RuntimeConfig.model_validate({"plugins": [{"name": "ado", "mcp": False}]})
        assert runtime.plugins[0].mcp is False

    def test_forms_can_be_mixed(self) -> None:
        runtime = RuntimeConfig.model_validate(
            {"plugins": ["prs", {"name": "ado", "agents": False}]}
        )
        assert [p.name for p in runtime.plugins] == ["prs", "ado"]

    def test_agent_level_coercion(self) -> None:
        agent = AgentDef.model_validate({"name": "a", "prompt": "p", "plugins": ["prs"]})
        assert agent.plugins == [PluginDef(name="prs")]


class TestComponentDefaults:
    def test_every_component_defaults_on(self) -> None:
        # Defaulting one off would recreate the partial-load bug the
        # feature exists to fix.
        entry = PluginDef(name="prs")
        assert (entry.skills, entry.agents, entry.mcp) == (True, True, True)


class TestTriState:
    def test_omitted_means_inherit(self) -> None:
        assert AgentDef.model_validate({"name": "a", "prompt": "p"}).plugins is None

    def test_empty_list_is_an_explicit_opt_out(self) -> None:
        agent = AgentDef.model_validate({"name": "a", "prompt": "p", "plugins": []})
        assert agent.plugins == []

    def test_runtime_default_is_an_empty_list(self) -> None:
        assert RuntimeConfig().plugins == []


class TestRejections:
    def test_blank_name(self) -> None:
        with pytest.raises(ValidationError, match="non-empty"):
            PluginDef(name="   ")

    def test_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            PluginDef.model_validate({"name": "p", "bogus": True})

    def test_duplicate_entries_are_refused(self) -> None:
        # Two entries can disagree about components, and there is no
        # correct merge — so refuse rather than silently keep one.
        with pytest.raises(ValidationError, match="duplicate entry"):
            RuntimeConfig.model_validate({"plugins": ["prs", {"name": "prs", "mcp": False}]})

    @pytest.mark.parametrize(
        ("kind", "extra"),
        [
            ("script", {"command": "ls"}),
            (
                "human_gate",
                {"prompt": "?", "options": [{"label": "a", "value": "a", "route": "next"}]},
            ),
            ("wait", {"duration": "1s"}),
            ("set", {"value": "x"}),
            ("terminate", {"status": "success", "reason": "done"}),
            ("workflow", {"workflow": "child.yaml"}),
        ],
    )
    def test_non_provider_backed_steps_reject_plugins(self, kind: str, extra: dict) -> None:
        with pytest.raises(ValidationError, match=f"{kind} agents cannot have 'plugins'"):
            AgentDef.model_validate({"name": "s", "type": kind, "plugins": ["prs"], **extra})


class TestSerialization:
    def test_round_trips(self) -> None:
        runtime = RuntimeConfig.model_validate({"plugins": [{"name": "ado", "mcp": False}]})
        dumped = runtime.model_dump()["plugins"]
        assert dumped == [{"name": "ado", "skills": True, "agents": True, "mcp": False}]
