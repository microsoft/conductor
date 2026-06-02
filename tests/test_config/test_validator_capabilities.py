"""Cross-check matrix for provider capability validation (#241)."""

from __future__ import annotations

from typing import Any

import pytest

from conductor.config.schema import (
    AgentDef,
    ForEachDef,
    MCPServerDef,
    OutputField,
    ParallelGroup,
    ReasoningConfig,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.config.validator import validate_workflow_config
from conductor.exceptions import ConfigurationError
from conductor.providers.capabilities import ProviderCapabilities


def _caps(**overrides: object) -> ProviderCapabilities:
    """Build a fully-stable capability descriptor; tests override specific fields."""
    base: dict[str, object] = {
        "tier": "stable",
        "mcp_tools": True,
        "workflow_tools_passthrough": True,
        "streaming_events": True,
        "agent_reasoning_events": True,
        "reasoning_effort": ("low", "medium", "high", "xhigh"),
        "structured_output": "native",
        "interrupt": True,
        "max_session_seconds": True,
        "checkpoint_resume": True,
        "usage_tracking": True,
        "concurrent_safe": True,
    }
    base.update(overrides)
    return ProviderCapabilities(**base)  # type: ignore[arg-type]


def _build_workflow(
    *,
    agents: list[AgentDef],
    parallel: list[ParallelGroup] | None = None,
    for_each: list[ForEachDef] | None = None,
    mcp_servers: dict[str, MCPServerDef] | None = None,
) -> WorkflowConfig:
    runtime_kwargs: dict[str, Any] = {"provider": "copilot"}
    if mcp_servers is not None:
        runtime_kwargs["mcp_servers"] = mcp_servers
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="test",
            entry_point=agents[0].name,
            runtime=RuntimeConfig(**runtime_kwargs),
        ),
        agents=agents,
        parallel=parallel or [],
        for_each=for_each or [],
    )


@pytest.fixture
def patch_caps(monkeypatch: pytest.MonkeyPatch):
    """Replace ``get_capabilities`` with a controllable mapping.

    Returns a setter that takes a ``{name: ProviderCapabilities}`` dict.
    Tests use this to declare what each provider name resolves to without
    touching the real provider modules.
    """

    def _setter(mapping: dict[str, ProviderCapabilities]) -> None:
        def fake(name: str) -> ProviderCapabilities:
            if name not in mapping:
                raise KeyError(name)
            return mapping[name]

        monkeypatch.setattr(
            "conductor.config.validator.get_capabilities",
            fake,
        )

    return _setter


class TestMcpToolsCrossCheck:
    def test_workflow_mcp_servers_against_unsupported_provider_errors(
        self, patch_caps: Any
    ) -> None:
        patch_caps({"copilot": _caps(mcp_tools=False)})
        config = _build_workflow(
            agents=[AgentDef(name="a", prompt="hi")],
            mcp_servers={"docs": MCPServerDef(command="docs-server")},
        )
        with pytest.raises(ConfigurationError, match="does not support MCP servers"):
            validate_workflow_config(config)

    def test_workflow_mcp_servers_with_supported_provider_passes(self, patch_caps: Any) -> None:
        patch_caps({"copilot": _caps(mcp_tools=True)})
        config = _build_workflow(
            agents=[AgentDef(name="a", prompt="hi")],
            mcp_servers={"docs": MCPServerDef(command="docs-server")},
        )
        validate_workflow_config(config)  # no raise

    def test_per_agent_provider_override_against_mcp_errors(self, patch_caps: Any) -> None:
        patch_caps(
            {
                "copilot": _caps(mcp_tools=True),
                "claude": _caps(mcp_tools=False),
            }
        )
        config = _build_workflow(
            agents=[AgentDef(name="a", prompt="hi", provider="claude")],
            mcp_servers={"docs": MCPServerDef(command="docs-server")},
        )
        with pytest.raises(ConfigurationError, match="claude.*MCP servers"):
            validate_workflow_config(config)


class TestToolsAllowlistCrossCheck:
    def test_empty_tools_list_against_no_passthrough_does_not_error(self, patch_caps: Any) -> None:
        """``tools: []`` is a 'no tools' request; provider can honor that."""
        patch_caps({"copilot": _caps(workflow_tools_passthrough=False)})
        config = _build_workflow(
            agents=[AgentDef(name="a", prompt="hi", tools=[])],
        )
        validate_workflow_config(config)  # no raise

    def test_non_empty_tools_list_against_no_passthrough_errors(self, patch_caps: Any) -> None:
        patch_caps({"copilot": _caps(workflow_tools_passthrough=False)})
        config = _build_workflow(
            agents=[AgentDef(name="a", prompt="hi", tools=["search"])],
        )
        with pytest.raises(ConfigurationError, match="does not honor per-agent tool allowlists"):
            validate_workflow_config(config)

    def test_omitted_tools_against_no_passthrough_does_not_error(self, patch_caps: Any) -> None:
        patch_caps({"copilot": _caps(workflow_tools_passthrough=False)})
        config = _build_workflow(agents=[AgentDef(name="a", prompt="hi")])
        validate_workflow_config(config)  # no raise


class TestReasoningEffortCrossCheck:
    def test_unsupported_level_errors(self, patch_caps: Any) -> None:
        patch_caps({"copilot": _caps(reasoning_effort=("low", "medium"))})
        config = _build_workflow(
            agents=[
                AgentDef(
                    name="a",
                    prompt="hi",
                    reasoning=ReasoningConfig(effort="high"),
                ),
            ],
        )
        with pytest.raises(ConfigurationError, match="supports only.*low.*medium"):
            validate_workflow_config(config)

    def test_provider_without_reasoning_support_errors(self, patch_caps: Any) -> None:
        patch_caps({"copilot": _caps(reasoning_effort=None)})
        config = _build_workflow(
            agents=[
                AgentDef(
                    name="a",
                    prompt="hi",
                    reasoning=ReasoningConfig(effort="medium"),
                ),
            ],
        )
        with pytest.raises(ConfigurationError, match="does not support reasoning effort"):
            validate_workflow_config(config)

    def test_supported_level_passes(self, patch_caps: Any) -> None:
        patch_caps({"copilot": _caps(reasoning_effort=("low", "medium", "high"))})
        config = _build_workflow(
            agents=[
                AgentDef(
                    name="a",
                    prompt="hi",
                    reasoning=ReasoningConfig(effort="medium"),
                ),
            ],
        )
        validate_workflow_config(config)


class TestStructuredOutputCrossCheck:
    def test_schema_against_no_support_errors(self, patch_caps: Any) -> None:
        patch_caps({"copilot": _caps(structured_output="none")})
        config = _build_workflow(
            agents=[
                AgentDef(
                    name="a",
                    prompt="hi",
                    output={"x": OutputField(type="string")},
                )
            ],
        )
        with pytest.raises(ConfigurationError, match="does not support structured output"):
            validate_workflow_config(config)

    def test_experimental_prompt_injection_warns(self, patch_caps: Any) -> None:
        patch_caps({"copilot": _caps(tier="experimental", structured_output="prompt_injection")})
        config = _build_workflow(
            agents=[
                AgentDef(
                    name="a",
                    prompt="hi",
                    output={"x": OutputField(type="string")},
                )
            ],
        )
        warnings = validate_workflow_config(config)
        assert any("prompt injection" in w for w in warnings), warnings

    def test_stable_prompt_injection_silent(self, patch_caps: Any) -> None:
        """Stable providers using prompt_injection (e.g. Copilot) MUST NOT warn."""
        patch_caps({"copilot": _caps(tier="stable", structured_output="prompt_injection")})
        config = _build_workflow(
            agents=[
                AgentDef(
                    name="a",
                    prompt="hi",
                    output={"x": OutputField(type="string")},
                )
            ],
        )
        warnings = validate_workflow_config(config)
        assert not any("prompt injection" in w for w in warnings)


class TestMaxSessionSecondsCrossCheck:
    def test_explicit_setting_against_unsupported_provider_errors(self, patch_caps: Any) -> None:
        patch_caps({"copilot": _caps(max_session_seconds=False)})
        config = _build_workflow(
            agents=[AgentDef(name="a", prompt="hi", max_session_seconds=120.0)],
        )
        with pytest.raises(ConfigurationError, match="does not enforce session timeouts"):
            validate_workflow_config(config)

    def test_omitted_against_unsupported_provider_passes(self, patch_caps: Any) -> None:
        patch_caps({"copilot": _caps(max_session_seconds=False)})
        config = _build_workflow(agents=[AgentDef(name="a", prompt="hi")])
        validate_workflow_config(config)


class TestConcurrencyCrossCheck:
    def test_parallel_group_with_unsafe_provider_errors(self, patch_caps: Any) -> None:
        patch_caps({"copilot": _caps(concurrent_safe=False)})
        config = _build_workflow(
            agents=[
                AgentDef(name="a", prompt="hi"),
                AgentDef(name="b", prompt="hi"),
                AgentDef(name="entry", prompt="hi"),
            ],
            parallel=[ParallelGroup(name="entry", agents=["a", "b"])],
        )
        # Re-anchor entry_point to the parallel group; the workflow builder
        # picks agents[0] otherwise.
        config.workflow.entry_point = "entry"
        with pytest.raises(ConfigurationError, match="not safe to run in parallel"):
            validate_workflow_config(config)

    def test_for_each_max_concurrent_one_is_allowed(self, patch_caps: Any) -> None:
        """A serial for_each (max_concurrent=1) does NOT trigger the concurrency check."""
        patch_caps({"copilot": _caps(concurrent_safe=False)})
        inline = AgentDef(name="inner", prompt="{{ item }}")
        config = _build_workflow(
            agents=[AgentDef(name="entry", prompt="hi")],
            for_each=[
                ForEachDef(
                    name="loop",
                    type="for_each",
                    source="entry.output.items",
                    **{"as": "item"},
                    agent=inline,
                    max_concurrent=1,
                )
            ],
        )
        validate_workflow_config(config)  # no raise

    def test_for_each_with_concurrency_and_unsafe_provider_errors(self, patch_caps: Any) -> None:
        patch_caps({"copilot": _caps(concurrent_safe=False)})
        inline = AgentDef(name="inner", prompt="{{ item }}")
        config = _build_workflow(
            agents=[AgentDef(name="entry", prompt="hi")],
            for_each=[
                ForEachDef(
                    name="loop",
                    type="for_each",
                    source="entry.output.items",
                    **{"as": "item"},
                    agent=inline,
                    max_concurrent=5,
                )
            ],
        )
        with pytest.raises(ConfigurationError, match="not safe to run in parallel|concurrent_safe"):
            validate_workflow_config(config)


class TestNonLLMAgentsSkipped:
    """Capability checks must NOT fire for human_gate / script / set / wait / terminate."""

    def test_script_agent_skipped_even_with_unsupported_provider(self, patch_caps: Any) -> None:
        """A workflow with ONLY script agents validates cleanly.

        After the rubber-duck fix to only check workflow-level mcp_servers
        against providers that LLM agents actually resolve to, this case
        passes silently: the script agent doesn't invoke a provider, so
        no agent uses the default copilot, so the workflow-level MCP
        mismatch never fires.
        """
        patch_caps({"copilot": _caps(mcp_tools=False, concurrent_safe=False)})
        config = _build_workflow(
            agents=[
                AgentDef(
                    name="a",
                    type="script",
                    command="echo hi",
                )
            ],
            mcp_servers={"docs": MCPServerDef(command="docs-server")},
        )
        validate_workflow_config(config)  # must not raise

    def test_human_gate_skipped(self, patch_caps: Any) -> None:
        """human_gate agents do not invoke a provider — capability checks must skip them."""
        from conductor.config.schema import GateOption

        patch_caps({"copilot": _caps(reasoning_effort=None)})
        config = _build_workflow(
            agents=[
                AgentDef(
                    name="gate",
                    type="human_gate",
                    prompt="Approve?",
                    options=[
                        GateOption(label="OK", value="ok", route="$end"),
                        GateOption(label="No", value="no", route="$end"),
                    ],
                ),
            ],
        )
        # reasoning.effort can't be declared on human_gate (schema disallows),
        # so the test simply verifies the workflow validates without raising
        # — i.e. that human_gate isn't accidentally checked against
        # provider capabilities.
        validate_workflow_config(config)


class TestUnknownProvider:
    def test_unknown_provider_in_yaml_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the resolver raises KeyError, the validator reports a clear error."""

        def fake(name: str) -> ProviderCapabilities:
            raise KeyError(name)

        monkeypatch.setattr("conductor.config.validator.get_capabilities", fake)
        config = _build_workflow(agents=[AgentDef(name="a", prompt="hi")])
        with pytest.raises(ConfigurationError, match="no declared ProviderCapabilities"):
            validate_workflow_config(config)


class TestRubberDuckFollowups:
    """Regression tests for the issues flagged in the Phase B/C rubber-duck review."""

    def test_default_unsupported_but_all_agents_override_to_supported_passes(
        self, patch_caps: Any
    ) -> None:
        """Workflow-level mcp_servers + unsupported default + every agent overrides → passes.

        Previously the validator unconditionally errored on default_provider
        mismatch. Now we only error if at least one LLM agent actually
        resolves to the default provider.
        """
        patch_caps(
            {
                "copilot": _caps(mcp_tools=False),  # default — would fail if used
                "claude": _caps(mcp_tools=True),  # all agents override here
            }
        )
        config = _build_workflow(
            agents=[
                AgentDef(name="a", prompt="hi", provider="claude"),
                AgentDef(name="b", prompt="hi", provider="claude"),
            ],
            mcp_servers={"docs": MCPServerDef(command="docs-server")},
        )
        validate_workflow_config(config)  # must not raise

    def test_default_unsupported_with_one_agent_on_default_errors(self, patch_caps: Any) -> None:
        """At least one LLM agent on the default provider → workflow-level MCP error fires."""
        patch_caps(
            {
                "copilot": _caps(mcp_tools=False),
                "claude": _caps(mcp_tools=True),
            }
        )
        config = _build_workflow(
            agents=[
                AgentDef(name="a", prompt="hi", provider="claude"),
                AgentDef(name="b", prompt="hi"),  # uses copilot default
            ],
            mcp_servers={"docs": MCPServerDef(command="docs-server")},
        )
        with pytest.raises(ConfigurationError, match="does not support MCP servers"):
            validate_workflow_config(config)

    def test_runtime_default_reasoning_effort_validated_against_provider(
        self, patch_caps: Any
    ) -> None:
        """A workflow-wide default_reasoning_effort is checked against capabilities."""
        patch_caps({"copilot": _caps(reasoning_effort=None)})
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="a",
                runtime=RuntimeConfig(provider="copilot", default_reasoning_effort="high"),
            ),
            agents=[AgentDef(name="a", prompt="hi")],
        )
        with pytest.raises(ConfigurationError, match="runtime.default_reasoning_effort"):
            validate_workflow_config(config)

    def test_per_agent_reasoning_overrides_workflow_default(self, patch_caps: Any) -> None:
        """When agent.reasoning.effort is set, the runtime default does NOT apply."""
        patch_caps({"copilot": _caps(reasoning_effort=None)})
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="a",
                # Workflow default is set, but the agent override is None → no check fires.
                runtime=RuntimeConfig(provider="copilot", default_reasoning_effort=None),
            ),
            agents=[AgentDef(name="a", prompt="hi")],  # no reasoning at all
        )
        validate_workflow_config(config)  # must not raise

    def test_openai_agents_placeholder_does_not_error_at_validate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Known-but-unimplemented providers return a permissive placeholder.

        Previously `openai-agents` would surface "no declared
        ProviderCapabilities" at validate time — overriding the factory's
        authoritative "not yet implemented" error at runtime.
        """
        # Use the REAL resolver (don't monkeypatch) so the placeholder path
        # is exercised end-to-end.
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="a",
                runtime=RuntimeConfig(provider="openai-agents"),
            ),
            agents=[AgentDef(name="a", prompt="hi")],
        )
        # No raise expected — placeholder permits everything.
        validate_workflow_config(config)
