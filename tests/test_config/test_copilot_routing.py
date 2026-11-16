"""Schema and static validation of Copilot routing preferences (Fusion / Auto tiers)."""

from __future__ import annotations

import textwrap

import pytest
from pydantic import ValidationError as PydanticValidationError

from conductor.config.loader import load_config_string
from conductor.config.schema import AgentDef, RuntimeConfig, ScriptStepDef, SetStepDef
from conductor.config.validator import validate_workflow_config
from conductor.exceptions import ConfigurationError
from conductor.providers.copilot_routing import (
    RoutingPreference,
    RoutingPreferenceError,
    resolve_routing_preference,
)


def _workflow(runtime: str, agents: str, for_each: str = "") -> str:
    return textwrap.dedent(
        """\
        workflow:
          name: routing
          entry_point: first
          runtime:
        {runtime}
        agents:
        {agents}
        {for_each}
        output:
          done: "yes"
        """
    ).format(
        runtime=textwrap.indent(textwrap.dedent(runtime), "    "),
        agents=textwrap.indent(textwrap.dedent(agents), ""),
        for_each=textwrap.dedent(for_each),
    )


def _validate(runtime: str, agents: str, for_each: str = "") -> list[str]:
    config = load_config_string(_workflow(runtime, agents, for_each))
    return validate_workflow_config(config)


def _errors(runtime: str, agents: str, for_each: str = "") -> str:
    with pytest.raises(ConfigurationError) as info:
        _validate(runtime, agents, for_each)
    return str(info.value)


class TestSchema:
    @pytest.mark.parametrize("tier", ["standard", "high", "max"])
    def test_fusion_tier_literals(self, tier: str) -> None:
        assert AgentDef(name="a", model="fusion", fusion_tier=tier).fusion_tier == tier

    def test_unknown_fusion_tier_is_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="fusion_tier must be one of"):
            AgentDef(name="a", model="fusion", fusion_tier="ultra")

    @pytest.mark.parametrize("tier", ["efficiency", "premium-v2", "hydrafusion"])
    def test_auto_tier_is_extensible(self, tier: str) -> None:
        # Requirement: Auto tiers are runtime-extensible identifiers, not a closed enum.
        assert AgentDef(name="a", model="auto", auto_tier=tier).auto_tier == tier

    def test_auto_tier_is_stripped(self) -> None:
        assert AgentDef(name="a", model="auto", auto_tier="  balance ").auto_tier == "balance"

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_auto_tier_is_rejected(self, value: str) -> None:
        with pytest.raises(PydanticValidationError, match="nonblank"):
            AgentDef(name="a", model="auto", auto_tier=value)

    def test_agent_preferences_are_mutually_exclusive(self) -> None:
        with pytest.raises(PydanticValidationError, match="mutually exclusive"):
            AgentDef(name="a", model="auto", fusion_tier="high", auto_tier="efficiency")

    @pytest.mark.parametrize("field", ["fusion_tier", "auto_tier"])
    def test_templates_are_deferred(self, field: str) -> None:
        agent = AgentDef(name="a", model="auto", **{field: "{{ workflow.input.tier }}"})
        assert getattr(agent, field) == "{{ workflow.input.tier }}"

    def test_runtime_defaults(self) -> None:
        assert RuntimeConfig(default_fusion_tier="max").default_fusion_tier == "max"
        assert RuntimeConfig(default_auto_tier=" efficiency ").default_auto_tier == "efficiency"

    def test_runtime_defaults_are_mutually_exclusive(self) -> None:
        with pytest.raises(PydanticValidationError, match="mutually exclusive"):
            RuntimeConfig(default_fusion_tier="high", default_auto_tier="efficiency")

    def test_runtime_fusion_default_accepts_only_literals(self) -> None:
        with pytest.raises(PydanticValidationError):
            RuntimeConfig(default_fusion_tier="{{ x }}")  # type: ignore[arg-type]
        with pytest.raises(PydanticValidationError, match="nonblank"):
            RuntimeConfig(default_auto_tier=" ")

    @pytest.mark.parametrize("step", [ScriptStepDef, SetStepDef])
    @pytest.mark.parametrize("field", ["fusion_tier", "auto_tier"])
    def test_non_agent_steps_reject_fields(self, step: type, field: str) -> None:
        base = (
            {"name": "s", "type": "script", "command": "true"}
            if step is ScriptStepDef
            else {"name": "s", "type": "set", "values": {"x": 1}}
        )
        with pytest.raises(PydanticValidationError, match=field):
            step.model_validate({**base, field: "high"})


class TestResolution:
    def _resolve(self, model: str | None, **kwargs: str | None) -> RoutingPreference:
        values: dict[str, str | None] = {
            "agent_fusion_tier": None,
            "agent_auto_tier": None,
            "default_fusion_tier": None,
            "default_auto_tier": None,
        }
        values.update(kwargs)
        return resolve_routing_preference(model=model, subject="Agent 'a'", **values)

    def test_agent_preference_replaces_defaults_as_a_unit(self) -> None:
        preference = self._resolve(
            "auto", agent_fusion_tier="standard", default_auto_tier="efficiency"
        )
        assert preference == RoutingPreference(fusion_tier="standard", source="agent")
        assert preference.to_capi() == {"fusion_tier": "standard"}

    def test_defaults_apply_to_matching_models_only(self) -> None:
        assert self._resolve("Fusion", default_fusion_tier="high").to_capi() == {
            "fusion_tier": "high"
        }
        assert self._resolve("auto", default_fusion_tier="high").to_capi() is None
        assert self._resolve("auto", default_auto_tier="x").source == "default"
        assert not self._resolve("gpt-5", default_auto_tier="x")
        assert not self._resolve(None, default_fusion_tier="high")

    def test_empty_preference_sends_nothing(self) -> None:
        assert self._resolve("fusion").to_capi() is None

    @pytest.mark.parametrize(
        ("model", "kwargs"),
        [
            ("gpt-5", {"agent_fusion_tier": "high"}),
            ("fusion", {"agent_auto_tier": "x"}),
            (None, {"agent_fusion_tier": "high"}),
            ("hydrafusion", {"agent_fusion_tier": "high"}),
            ("auto", {"agent_fusion_tier": "{{ t }}"}),
        ],
    )
    def test_explicit_incompatibilities_raise(self, model: str | None, kwargs: dict) -> None:
        with pytest.raises(RoutingPreferenceError):
            self._resolve(model, **kwargs)


class TestStaticValidation:
    def test_valid_workflow_with_defaults_and_override(self) -> None:
        warnings = _validate(
            """\
            provider: copilot
            default_model: fusion
            default_fusion_tier: high
            """,
            """\
            - name: first
              prompt: p
              routes: [{to: second}]
            - name: second
              model: auto
              auto_tier: efficiency
              prompt: p
              routes: [{to: $end}]
            """,
        )
        assert not any("default_fusion_tier" in warning for warning in warnings)

    def test_incompatible_model_is_an_error(self) -> None:
        message = _errors(
            "provider: copilot\n",
            """\
            - name: first
              model: gpt-5
              fusion_tier: high
              prompt: p
              routes: [{to: $end}]
            """,
        )
        assert "fusion_tier='high'" in message and "requires model 'fusion'" in message

    def test_inherited_default_model_is_checked(self) -> None:
        message = _errors(
            "provider: copilot\ndefault_model: fusion\n",
            """\
            - name: first
              auto_tier: efficiency
              prompt: p
              routes: [{to: $end}]
            """,
        )
        assert "its model is 'fusion'" in message

    def test_missing_model_is_an_error(self) -> None:
        message = _errors(
            "provider: copilot\n",
            """\
            - name: first
              fusion_tier: high
              prompt: p
              routes: [{to: $end}]
            """,
        )
        assert "it has no model set" in message

    def test_templated_values_are_deferred(self) -> None:
        _validate(
            "provider: copilot\n",
            """\
            - name: first
              model: "{{ workflow.input.model | default('fusion') }}"
              fusion_tier: "{{ workflow.input.tier | default('high') }}"
              prompt: p
              routes: [{to: $end}]
            """,
        )

    def test_provider_without_capability_is_an_error(self) -> None:
        message = _errors(
            "provider: copilot\n",
            """\
            - name: first
              provider: claude
              model: auto
              auto_tier: efficiency
              prompt: p
              routes: [{to: $end}]
            """,
        )
        assert "native_routing_preferences=False" in message

    def test_templated_tier_on_incapable_provider_is_still_an_error(self) -> None:
        message = _errors(
            "provider: openai\n",
            """\
            - name: first
              model: auto
              fusion_tier: "{{ workflow.input.tier }}"
              prompt: p
              routes: [{to: $end}]
            """,
        )
        assert "native_routing_preferences=False" in message

    def test_inline_for_each_agent_is_checked(self) -> None:
        message = _errors(
            "provider: copilot\n",
            """\
            - name: first
              model: gpt-5
              prompt: p
              output: {items: {type: array}}
              routes: [{to: fan}]
            """,
            """\
            for_each:
              - name: fan
                type: for_each
                source: first.output.items
                as: item
                agent:
                  name: inner
                  model: gpt-5
                  auto_tier: efficiency
                  prompt: p
                routes: [{to: $end}]
            """,
        )
        assert "Agent 'inner' sets auto_tier='efficiency'" in message

    def test_byok_routing_rejects_agent_preference(self) -> None:
        message = _errors(
            """\
            provider:
              name: copilot
              base_url: http://localhost:11434/v1
            default_model: auto
            """,
            """\
            - name: first
              auto_tier: efficiency
              prompt: p
              routes: [{to: $end}]
            """,
        )
        assert "BYOK" in message

    def test_byok_routing_rejects_runtime_default(self) -> None:
        message = _errors(
            """\
            provider:
              name: copilot
              base_url: http://localhost:11434/v1
            default_model: fusion
            default_fusion_tier: high
            """,
            """\
            - name: first
              prompt: p
              routes: [{to: $end}]
            """,
        )
        assert "runtime.default_fusion_tier" in message and "BYOK" in message

    def test_runtime_url_is_allowed(self) -> None:
        # Requirement: an existing-runtime transport is not BYOK routing.
        _validate(
            """\
            provider:
              name: copilot
              runtime_url: localhost:4321
            default_model: fusion
            default_fusion_tier: max
            """,
            """\
            - name: first
              prompt: p
              routes: [{to: $end}]
            """,
        )

    def test_unused_default_is_a_warning(self) -> None:
        warnings = _validate(
            "provider: copilot\ndefault_fusion_tier: high\n",
            """\
            - name: first
              model: gpt-5
              prompt: p
              routes: [{to: $end}]
            """,
        )
        assert any("runtime.default_fusion_tier" in warning for warning in warnings)

    def test_default_on_incapable_provider_is_only_a_warning(self) -> None:
        warnings = _validate(
            "provider: claude\ndefault_auto_tier: efficiency\n",
            """\
            - name: first
              model: auto
              prompt: p
              routes: [{to: $end}]
            """,
        )
        assert any("runtime.default_auto_tier" in warning for warning in warnings)

    def test_aca_with_non_copilot_inner_provider_rejects_preference(self) -> None:
        message = _errors(
            """\
            provider:
              name: aca
              pool_endpoint: https://pool.example.com
              inner_provider: claude-agent-sdk
            """,
            """\
            - name: first
              model: auto
              auto_tier: efficiency
              prompt: p
              routes: [{to: $end}]
            """,
        )
        assert "aca inner provider is 'claude-agent-sdk'" in message
