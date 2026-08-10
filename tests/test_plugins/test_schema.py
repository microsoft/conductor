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


class TestPluginSourceDef:
    """``plugin_sources:`` accepts a string shorthand or an object."""

    def test_string_shorthand_is_expanded(self):
        runtime = RuntimeConfig.model_validate({"plugin_sources": {"acme": "acme/plugins#v1.0.0"}})
        assert runtime.plugin_sources["acme"].source == "acme/plugins#v1.0.0"
        assert runtime.plugin_sources["acme"].path is None
        assert runtime.plugin_sources["acme"].plugin is None

    def test_object_form(self):
        runtime = RuntimeConfig.model_validate(
            {
                "plugin_sources": {
                    "acme": {
                        "source": "git@github.com:acme/p.git#3f2a1c9",
                        "path": "packages/plugins",
                        "plugin": "reviewer",
                    }
                }
            }
        )
        entry = runtime.plugin_sources["acme"]
        assert entry.path == "packages/plugins"
        assert entry.plugin == "reviewer"

    def test_defaults_to_empty(self):
        assert RuntimeConfig().plugin_sources == {}

    def test_an_unparseable_source_is_rejected_at_load_time(self):
        """A typo should name the source the author wrote, not a directory
        they never typed."""
        with pytest.raises(ValidationError, match="not a recognised plugin source"):
            RuntimeConfig.model_validate({"plugin_sources": {"acme": "nonsense"}})

    def test_a_local_source_with_a_ref_is_rejected(self):
        with pytest.raises(ValidationError, match="local path with a"):
            RuntimeConfig.model_validate({"plugin_sources": {"acme": "./vendor#v1"}})

    def test_a_marketplace_name_must_be_usable_after_an_at_sign(self):
        with pytest.raises(ValidationError, match="plugin_sources key"):
            RuntimeConfig.model_validate({"plugin_sources": {"bad name": "acme/p"}})

    @pytest.mark.parametrize("field", ["path", "plugin"])
    def test_empty_optional_fields_are_rejected(self, field):
        with pytest.raises(ValidationError, match="must be non-empty when set"):
            RuntimeConfig.model_validate(
                {"plugin_sources": {"acme": {"source": "acme/p", field: "  "}}}
            )

    def test_unknown_keys_are_rejected(self):
        with pytest.raises(ValidationError):
            RuntimeConfig.model_validate(
                {"plugin_sources": {"acme": {"source": "acme/p", "typo": 1}}}
            )


class TestMarketplaceEntryShape:
    """``plugin@marketplace`` is shape-checked where the entry is declared."""

    @pytest.mark.parametrize("entry", ["prs@acme", "code.review@jason-tools", "a_b@c-d"])
    def test_accepted(self, entry):
        assert RuntimeConfig.model_validate({"plugins": [entry]}).plugins[0].name == entry

    @pytest.mark.parametrize("entry", ["prs@", "@acme", "a@b@c!", "prs@bad name"])
    def test_malformed_is_rejected(self, entry):
        with pytest.raises(ValidationError):
            RuntimeConfig.model_validate({"plugins": [entry]})

    def test_a_path_containing_an_at_sign_is_still_a_path(self):
        """Path classification runs before the ``@`` split."""
        runtime = RuntimeConfig.model_validate({"plugins": ["./tools/my@plugin"]})
        assert runtime.plugins[0].name == "./tools/my@plugin"

    def test_per_agent_entries_accept_the_form_too(self):
        agent = AgentDef.model_validate(
            {
                "name": "a",
                "prompt": "p",
                "plugins": ["prs@acme", {"name": "ado@acme", "mcp": False}],
            }
        )
        assert [entry.name for entry in agent.plugins] == ["prs@acme", "ado@acme"]
        assert agent.plugins[1].mcp is False
