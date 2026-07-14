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
    tools: list[str] | None = None,
) -> WorkflowConfig:
    runtime_kwargs: dict[str, Any] = {"provider": "copilot"}
    if mcp_servers is not None:
        runtime_kwargs["mcp_servers"] = mcp_servers
    workflow_kwargs: dict[str, Any] = {}
    if tools is not None:
        workflow_kwargs["tools"] = tools
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="test",
            entry_point=agents[0].name,
            runtime=RuntimeConfig(**runtime_kwargs),
        ),
        agents=agents,
        parallel=parallel or [],
        for_each=for_each or [],
        **workflow_kwargs,
    )


def _for_each_workflow(
    *,
    inline: AgentDef,
    tools: list[str] | None = None,
    mcp_servers: dict[str, MCPServerDef] | None = None,
) -> WorkflowConfig:
    """Build a workflow whose only for_each group carries ``inline``.

    The entry agent opts out of tools (``tools: []``) and no workflow-wide
    defaults are set, so — unless a test says otherwise — only the inline agent
    can trip a per-agent capability check, isolating the assertion to the
    for_each path (#270).
    """
    return _build_workflow(
        agents=[AgentDef(name="entry", prompt="hi", tools=[])],
        tools=tools,
        mcp_servers=mcp_servers,
        for_each=[
            ForEachDef(
                name="loop",
                type="for_each",
                source="entry.output.items",
                **{"as": "item"},
                agent=inline,
            )
        ],
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

    def test_omitted_tools_inherits_workflow_tools_against_no_passthrough_errors(
        self, patch_caps: Any
    ) -> None:
        """Omitted ``tools:`` + non-empty workflow ``tools:`` + no passthrough.

        An omitted per-agent ``tools:`` inherits the workflow-level list at
        runtime; a non-passthrough provider would then refuse it at execute
        time with a confusing "declares tools=[...]" error even though the
        agent declared none. Catch it at validate time instead.
        """
        patch_caps({"copilot": _caps(workflow_tools_passthrough=False)})
        config = _build_workflow(
            agents=[AgentDef(name="a", prompt="hi")],
            tools=["search", "read_file"],
        )
        with pytest.raises(ConfigurationError, match="omits 'tools:' and would inherit"):
            validate_workflow_config(config)

    def test_explicit_empty_tools_with_workflow_tools_no_passthrough_passes(
        self, patch_caps: Any
    ) -> None:
        """Explicit ``tools: []`` opts out of inheritance, so it stays valid."""
        patch_caps({"copilot": _caps(workflow_tools_passthrough=False)})
        config = _build_workflow(
            agents=[AgentDef(name="a", prompt="hi", tools=[])],
            tools=["search"],
        )
        validate_workflow_config(config)  # no raise

    def test_omitted_tools_inherits_workflow_tools_with_passthrough_passes(
        self, patch_caps: Any
    ) -> None:
        """A passthrough provider honors the inherited list, so no error."""
        patch_caps({"copilot": _caps(workflow_tools_passthrough=True)})
        config = _build_workflow(
            agents=[AgentDef(name="a", prompt="hi")],
            tools=["search"],
        )
        validate_workflow_config(config)  # no raise


class TestForEachInlineToolsCrossCheck:
    """The tools cross-check must also cover a for_each group's INLINE agent.

    A ``for_each`` group carries an inline ``AgentDef`` that is NOT in
    ``config.agents`` but runs at runtime with ``workflow_tools=config.tools``,
    exactly like a top-level agent. Without an explicit pass it would slip past
    ``validate`` and fail mid-iteration with the same confusing error.
    """

    def _for_each_config(
        self,
        *,
        inline: AgentDef,
        tools: list[str] | None = None,
    ) -> WorkflowConfig:
        # The entry agent opts out with ``tools: []`` so only the inline agent
        # can trip the check — isolating the assertion to the for_each path.
        return _build_workflow(
            agents=[AgentDef(name="entry", prompt="hi", tools=[])],
            tools=tools,
            for_each=[
                ForEachDef(
                    name="loop",
                    type="for_each",
                    source="entry.output.items",
                    **{"as": "item"},
                    agent=inline,
                )
            ],
        )

    def test_inline_omitted_tools_inherits_workflow_tools_errors(self, patch_caps: Any) -> None:
        """Inline agent omits ``tools:`` + non-empty workflow ``tools:`` -> error."""
        patch_caps({"copilot": _caps(workflow_tools_passthrough=False)})
        config = self._for_each_config(
            inline=AgentDef(name="inner", prompt="{{ item }}"),
            tools=["search"],
        )
        with pytest.raises(
            ConfigurationError, match="Agent 'inner' omits 'tools:' and would inherit"
        ):
            validate_workflow_config(config)

    def test_inline_explicit_empty_tools_passes(self, patch_caps: Any) -> None:
        """Inline ``tools: []`` opts out of inheritance, so it stays valid."""
        patch_caps({"copilot": _caps(workflow_tools_passthrough=False)})
        config = self._for_each_config(
            inline=AgentDef(name="inner", prompt="{{ item }}", tools=[]),
            tools=["search"],
        )
        validate_workflow_config(config)  # no raise

    def test_inline_explicit_nonempty_tools_errors(self, patch_caps: Any) -> None:
        """An explicit non-empty inline allowlist against a non-passthrough provider errors."""
        patch_caps({"copilot": _caps(workflow_tools_passthrough=False)})
        config = self._for_each_config(
            inline=AgentDef(name="inner", prompt="{{ item }}", tools=["search"]),
        )
        with pytest.raises(ConfigurationError, match="Agent 'inner' declares tools="):
            validate_workflow_config(config)

    def test_inline_omitted_tools_with_passthrough_passes(self, patch_caps: Any) -> None:
        """A passthrough provider honors the inherited list, so no error."""
        patch_caps({"copilot": _caps(workflow_tools_passthrough=True)})
        config = self._for_each_config(
            inline=AgentDef(name="inner", prompt="{{ item }}"),
            tools=["search"],
        )
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

    def test_templated_effort_defers_capability_check(self, patch_caps: Any) -> None:
        """#262: a templated effort can't be checked vs caps until runtime.

        The provider only advertises ``low``/``medium``, but a templated
        effort must NOT raise at validate time — the resolved value is
        checked by the runtime resolver instead.
        """
        patch_caps({"copilot": _caps(reasoning_effort=("low", "medium"))})
        config = _build_workflow(
            agents=[
                AgentDef(
                    name="a",
                    prompt="hi",
                    reasoning=ReasoningConfig(effort="{{ workflow.input.eff }}"),
                ),
            ],
        )
        validate_workflow_config(config)  # must not raise

    def test_statement_style_templated_effort_defers_capability_check(
        self, patch_caps: Any
    ) -> None:
        """#262 (pr-review R1-001): a ``{% %}`` statement-style effort must
        ALSO defer the membership check, not just ``{{ }}``.

        The schema, executor, and context_tier validator all defer on ``{{``
        OR ``{%``; the capability cross-check must mirror that predicate.
        Provider supports only ``low``/``medium`` (a restricted non-None
        tuple), but a ``{% if %}...{% endif %}`` effort must NOT raise at
        validate time — it's resolved + re-validated at runtime.
        """
        patch_caps({"copilot": _caps(reasoning_effort=("low", "medium"))})
        config = _build_workflow(
            agents=[
                AgentDef(
                    name="a",
                    prompt="hi",
                    reasoning=ReasoningConfig(
                        effort="{% if workflow.input.heavy %}xhigh{% else %}low{% endif %}"
                    ),
                ),
            ],
        )
        validate_workflow_config(config)  # must not raise

    def test_templated_effort_on_no_reasoning_provider_still_errors(self, patch_caps: Any) -> None:
        """#262 (dual-RD): whether a provider supports reasoning AT ALL is a
        value-INDEPENDENT fact known at validate time. A templated effort on a
        provider with ``reasoning_effort=None`` must STILL error — no resolved
        value could ever be valid, and the provider ignores reasoning at
        runtime, so deferring would silently drop operator intent."""
        patch_caps({"copilot": _caps(reasoning_effort=None)})
        config = _build_workflow(
            agents=[
                AgentDef(
                    name="a",
                    prompt="hi",
                    reasoning=ReasoningConfig(effort="{{ workflow.input.eff }}"),
                ),
            ],
        )
        with pytest.raises(ConfigurationError, match="does not support reasoning effort"):
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


class TestConcurrencyOverrides:
    """Per-agent provider override interactions with parallel/for_each (#241 test gap)."""

    def test_parallel_group_member_override_to_safe_provider_passes(self, patch_caps: Any) -> None:
        """If a parallel group member overrides to a safe provider, no error fires."""
        patch_caps(
            {
                "copilot": _caps(concurrent_safe=False),  # default (would error)
                "claude": _caps(concurrent_safe=True),  # both members override
            }
        )
        config = _build_workflow(
            agents=[
                AgentDef(name="a", prompt="hi", provider="claude"),
                AgentDef(name="b", prompt="hi", provider="claude"),
                AgentDef(name="start", prompt="hi"),
            ],
            parallel=[ParallelGroup(name="group", agents=["a", "b"])],
        )
        config.workflow.entry_point = "group"
        validate_workflow_config(config)  # must not raise

    def test_parallel_group_member_override_to_unsafe_provider_errors(
        self, patch_caps: Any
    ) -> None:
        """If a member overrides to an unsafe provider while default is safe, error fires."""
        patch_caps(
            {
                "copilot": _caps(concurrent_safe=True),  # default — safe
                "claude": _caps(concurrent_safe=False),  # one member overrides — unsafe
            }
        )
        config = _build_workflow(
            agents=[
                AgentDef(name="a", prompt="hi"),  # uses default (safe)
                AgentDef(name="b", prompt="hi", provider="claude"),  # overrides (unsafe)
                AgentDef(name="start", prompt="hi"),
            ],
            parallel=[ParallelGroup(name="group", agents=["a", "b"])],
        )
        config.workflow.entry_point = "group"
        with pytest.raises(ConfigurationError, match="not safe to run in parallel"):
            validate_workflow_config(config)

    def test_for_each_inline_agent_override_to_safe_provider_passes(self, patch_caps: Any) -> None:
        """A for_each inline agent overriding to a safe provider clears the concurrency check."""
        patch_caps(
            {
                "copilot": _caps(concurrent_safe=False),  # default
                "claude": _caps(concurrent_safe=True),  # inline override
            }
        )
        inline = AgentDef(name="inner", prompt="{{ item }}", provider="claude")
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
        validate_workflow_config(config)  # must not raise

    def test_for_each_inline_agent_override_to_unsafe_provider_errors(
        self, patch_caps: Any
    ) -> None:
        """A for_each inline agent overriding to an unsafe provider triggers the error."""
        patch_caps(
            {
                "copilot": _caps(concurrent_safe=True),  # default (safe)
                "claude": _caps(concurrent_safe=False),  # inline override (unsafe)
            }
        )
        inline = AgentDef(name="inner", prompt="{{ item }}", provider="claude")
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
        with pytest.raises(ConfigurationError, match="concurrent_safe=False"):
            validate_workflow_config(config)


class TestWorkflowLevelMaxSessionSeconds:
    """Workflow-level runtime.max_session_seconds validated against capabilities."""

    def test_runtime_timeout_against_unsupported_provider_errors(self, patch_caps: Any) -> None:
        patch_caps({"copilot": _caps(max_session_seconds=False)})
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="a",
                runtime=RuntimeConfig(provider="copilot", max_session_seconds=120.0),
            ),
            agents=[AgentDef(name="a", prompt="hi")],
        )
        with pytest.raises(ConfigurationError, match="does not enforce session timeouts"):
            validate_workflow_config(config)

    def test_per_agent_override_skips_workflow_level_check(self, patch_caps: Any) -> None:
        """Per-agent max_session_seconds uses its own check, not the workflow default."""
        patch_caps({"copilot": _caps(max_session_seconds=True)})
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="a",
                # Workflow default + per-agent override; both honored by capability.
                runtime=RuntimeConfig(provider="copilot", max_session_seconds=60.0),
            ),
            agents=[AgentDef(name="a", prompt="hi", max_session_seconds=30.0)],
        )
        validate_workflow_config(config)

    def test_runtime_timeout_with_supported_provider_passes(self, patch_caps: Any) -> None:
        patch_caps({"copilot": _caps(max_session_seconds=True)})
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="a",
                runtime=RuntimeConfig(provider="copilot", max_session_seconds=120.0),
            ),
            agents=[AgentDef(name="a", prompt="hi")],
        )
        validate_workflow_config(config)


class TestForEachProviderRecorded:
    """ForEach inline agent providers appear in workflow_started.providers block (#241 gap)."""

    def test_for_each_inline_experimental_provider_appears_in_engine_metadata(
        self,
    ) -> None:
        """Engine's build_workflow_started_data must record for_each inline providers.

        Without this, the experimental banner won't fire and the dashboard
        won't badge nodes for for_each-only experimental providers.
        """
        pytest.importorskip("claude_agent_sdk")
        from conductor.engine.workflow import WorkflowEngine

        inline = AgentDef(name="worker", prompt="{{ item }}", provider="claude-agent-sdk")
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="setup",
                runtime=RuntimeConfig(provider="copilot"),  # default is stable
            ),
            agents=[AgentDef(name="setup", prompt="hi")],
            for_each=[
                ForEachDef(
                    name="scan",
                    type="for_each",
                    source="setup.output.items",
                    **{"as": "item"},
                    agent=inline,
                )
            ],
        )
        engine = WorkflowEngine(config=config, provider=None)
        data = engine.build_workflow_started_data()
        assert "claude-agent-sdk" in data["providers"]
        assert data["providers"]["claude-agent-sdk"]["tier"] == "experimental"


class TestMultiErrorAggregation:
    """The validator aggregates ALL capability errors, not just the first (#241 test gap)."""

    def test_multiple_independent_violations_both_reported(self, patch_caps: Any) -> None:
        """A workflow with two unrelated mismatches must report both in one ConfigurationError."""
        patch_caps(
            {
                "copilot": _caps(
                    workflow_tools_passthrough=False,
                    max_session_seconds=False,
                ),
            }
        )
        config = _build_workflow(
            agents=[
                AgentDef(name="agent_a", prompt="hi", tools=["search"]),
                AgentDef(name="agent_b", prompt="hi", max_session_seconds=60.0),
            ],
        )
        with pytest.raises(ConfigurationError) as exc_info:
            validate_workflow_config(config)
        msg = str(exc_info.value)
        # Both agent names appear, proving the validator did not short-circuit
        # on the first error.
        assert "agent_a" in msg
        assert "agent_b" in msg


class TestForEachInlineCapabilityCrossCheck:
    """Per-agent capability checks must ALSO cover a for_each group's INLINE agent (#270).

    #269 wired the *tools* check into the for_each inline pass; #270 extends the
    remaining per-agent checks (reasoning effort, structured output, per-agent
    MCP override, explicit max_session_seconds) so an inline agent gets identical
    treatment to a top-level agent. In each test the entry agent is inert, so the
    assertion isolates to the inline (``inner``) agent — without the fix these
    per-agent checks are skipped for the inline agent and NO error is raised.
    """

    def test_inline_reasoning_unsupported_level_errors(self, patch_caps: Any) -> None:
        patch_caps({"copilot": _caps(reasoning_effort=("low", "medium"))})
        config = _for_each_workflow(
            inline=AgentDef(
                name="inner", prompt="{{ item }}", reasoning=ReasoningConfig(effort="high")
            ),
        )
        with pytest.raises(ConfigurationError, match="Agent 'inner'.*supports only.*low.*medium"):
            validate_workflow_config(config)

    def test_inline_reasoning_on_no_reasoning_provider_errors(self, patch_caps: Any) -> None:
        """The confirmed #270 example: inline ``reasoning.effort`` on a provider
        that ignores reasoning (e.g. claude-agent-sdk) must error, exactly like
        the identical agent at top level."""
        patch_caps({"copilot": _caps(reasoning_effort=None)})
        config = _for_each_workflow(
            inline=AgentDef(
                name="inner", prompt="{{ item }}", reasoning=ReasoningConfig(effort="high")
            ),
        )
        with pytest.raises(
            ConfigurationError, match="Agent 'inner'.*does not support reasoning effort"
        ):
            validate_workflow_config(config)

    def test_inline_reasoning_supported_level_passes(self, patch_caps: Any) -> None:
        patch_caps({"copilot": _caps(reasoning_effort=("low", "medium", "high"))})
        config = _for_each_workflow(
            inline=AgentDef(
                name="inner", prompt="{{ item }}", reasoning=ReasoningConfig(effort="medium")
            ),
        )
        validate_workflow_config(config)  # no raise

    def test_inline_structured_output_no_support_errors(self, patch_caps: Any) -> None:
        patch_caps({"copilot": _caps(structured_output="none")})
        config = _for_each_workflow(
            inline=AgentDef(
                name="inner", prompt="{{ item }}", output={"x": OutputField(type="string")}
            ),
        )
        with pytest.raises(
            ConfigurationError, match="Agent 'inner'.*does not support structured output"
        ):
            validate_workflow_config(config)

    def test_inline_structured_output_experimental_prompt_injection_warns(
        self, patch_caps: Any
    ) -> None:
        patch_caps({"copilot": _caps(tier="experimental", structured_output="prompt_injection")})
        config = _for_each_workflow(
            inline=AgentDef(
                name="inner", prompt="{{ item }}", output={"x": OutputField(type="string")}
            ),
        )
        warnings = validate_workflow_config(config)
        assert any("inner" in w and "prompt injection" in w for w in warnings), warnings

    def test_inline_explicit_max_session_seconds_on_unsupported_errors(
        self, patch_caps: Any
    ) -> None:
        patch_caps({"copilot": _caps(max_session_seconds=False)})
        config = _for_each_workflow(
            inline=AgentDef(name="inner", prompt="{{ item }}", max_session_seconds=120.0),
        )
        with pytest.raises(
            ConfigurationError, match="Agent 'inner'.*does not enforce session timeouts"
        ):
            validate_workflow_config(config)

    def test_inline_provider_override_against_mcp_errors(self, patch_caps: Any) -> None:
        """An inline agent overriding to a non-MCP provider while the workflow
        declares mcp_servers must error (the entry agent stays on the MCP-capable
        default, so only the inline override trips the check)."""
        patch_caps(
            {
                "copilot": _caps(mcp_tools=True),  # default (entry uses this)
                "claude": _caps(mcp_tools=False),  # inline override
            }
        )
        config = _for_each_workflow(
            inline=AgentDef(name="inner", prompt="{{ item }}", provider="claude"),
            mcp_servers={"docs": MCPServerDef(command="docs-server")},
        )
        with pytest.raises(ConfigurationError, match="Agent 'inner'.*MCP servers"):
            validate_workflow_config(config)

    def test_inline_on_fully_capable_default_passes(self, patch_caps: Any) -> None:
        """A fully-capable provider honors every declared capability → no error.

        Guards against false positives from running the full check over inline
        agents.
        """
        patch_caps({"copilot": _caps()})
        config = _for_each_workflow(
            inline=AgentDef(
                name="inner",
                prompt="{{ item }}",
                reasoning=ReasoningConfig(effort="high"),
                max_session_seconds=60.0,
                output={"x": OutputField(type="string")},
            ),
        )
        validate_workflow_config(config)  # no raise


class TestForEachInlineWorkflowLevelInheritance:
    """Workflow-level inheritance checks must ALSO cover for_each inline agents (#270).

    When every top-level agent overrides to a capable provider but a for_each
    inline agent runs on the incapable *default* provider, it INHERITS the
    workflow-level ``mcp_servers`` / ``max_session_seconds`` / default reasoning
    effort. Without inline coverage the workflow-level checks (which only scanned
    ``config.agents``) find no offending agent and the inheritance silently
    degrades at runtime.
    """

    def _inheritance_config(
        self,
        *,
        inline: AgentDef,
        runtime: RuntimeConfig,
    ) -> WorkflowConfig:
        # ``entry`` overrides to a capable provider so, WITHOUT the fix, the
        # workflow-level check finds nothing to flag — the error can only come
        # from the inline agent inheriting the workflow-level setting.
        return WorkflowConfig(
            workflow=WorkflowDef(name="t", entry_point="entry", runtime=runtime),
            agents=[AgentDef(name="entry", prompt="hi", tools=[], provider="claude")],
            for_each=[
                ForEachDef(
                    name="loop",
                    type="for_each",
                    source="entry.output.items",
                    **{"as": "item"},
                    agent=inline,
                )
            ],
        )

    def test_inline_inherits_workflow_default_reasoning_effort_on_no_reasoning_provider_errors(
        self, patch_caps: Any
    ) -> None:
        patch_caps(
            {
                "copilot": _caps(reasoning_effort=None),  # default (inline inherits here)
                "claude": _caps(reasoning_effort=("low", "medium", "high")),  # entry override
            }
        )
        config = self._inheritance_config(
            inline=AgentDef(name="inner", prompt="{{ item }}"),
            runtime=RuntimeConfig(provider="copilot", default_reasoning_effort="high"),
        )
        with pytest.raises(
            ConfigurationError,
            match="Agent 'inner'.*runtime.default_reasoning_effort.*does not support reasoning",
        ):
            validate_workflow_config(config)

    def test_inline_inherits_workflow_mcp_servers_on_incapable_default_errors(
        self, patch_caps: Any
    ) -> None:
        patch_caps(
            {
                "copilot": _caps(mcp_tools=False),  # default (inline inherits here)
                "claude": _caps(mcp_tools=True),  # entry override
            }
        )
        config = self._inheritance_config(
            inline=AgentDef(name="inner", prompt="{{ item }}"),
            runtime=RuntimeConfig(
                provider="copilot",
                mcp_servers={"docs": MCPServerDef(command="docs-server")},
            ),
        )
        with pytest.raises(ConfigurationError, match="does not support MCP servers.*inner"):
            validate_workflow_config(config)

    def test_inline_inherits_workflow_max_session_seconds_on_incapable_default_errors(
        self, patch_caps: Any
    ) -> None:
        patch_caps(
            {
                "copilot": _caps(max_session_seconds=False),  # default (inline inherits here)
                "claude": _caps(max_session_seconds=True),  # entry override
            }
        )
        config = self._inheritance_config(
            inline=AgentDef(name="inner", prompt="{{ item }}"),
            runtime=RuntimeConfig(provider="copilot", max_session_seconds=120.0),
        )
        with pytest.raises(ConfigurationError, match="does not enforce session timeouts.*inner"):
            validate_workflow_config(config)

    def test_inline_inherits_capable_default_passes(self, patch_caps: Any) -> None:
        """A capable default provider honors the inherited workflow-level settings
        → no error, even though the inline agent overrides nothing."""
        patch_caps({"copilot": _caps(), "claude": _caps()})
        config = self._inheritance_config(
            inline=AgentDef(name="inner", prompt="{{ item }}"),
            runtime=RuntimeConfig(
                provider="copilot",
                default_reasoning_effort="high",
                max_session_seconds=120.0,
                mcp_servers={"docs": MCPServerDef(command="docs-server")},
            ),
        )
        validate_workflow_config(config)  # no raise

    def test_inline_non_llm_human_gate_skipped(self, patch_caps: Any) -> None:
        """A non-LLM (human_gate) inline agent must be SKIPPED by the
        ``_is_llm_agent`` filter — even on an incapable default provider that
        declares ``mcp_servers``, ``max_session_seconds``, AND
        ``default_reasoning_effort`` that an LLM inline agent WOULD inherit and
        fail on. Guards the inline ``_is_llm_agent`` guard (feeding
        ``all_llm_agents`` and the for_each per-agent loop) against a future
        refactor that drops it and spuriously fail-validates the workflow.
        """
        from conductor.config.schema import GateOption

        patch_caps(
            {
                # Default provider is incapable on all three inherited axes;
                # only a non-skipped LLM agent on it would raise.
                "copilot": _caps(mcp_tools=False, max_session_seconds=False, reasoning_effort=None),
                "claude": _caps(),  # entry override → capable, so entry never trips
            }
        )
        config = self._inheritance_config(
            inline=AgentDef(
                name="gate",
                type="human_gate",
                prompt="Approve {{ item }}?",
                options=[
                    GateOption(label="OK", value="ok", route="$end"),
                    GateOption(label="No", value="no", route="$end"),
                ],
            ),
            runtime=RuntimeConfig(
                provider="copilot",
                default_reasoning_effort="high",
                max_session_seconds=120.0,
                mcp_servers={"docs": MCPServerDef(command="docs-server")},
            ),
        )
        validate_workflow_config(config)  # must not raise — human_gate is skipped


class TestWorkingDirCrossCheck:
    """Requirement: ``working_dir`` (per-agent or runtime-wide) against a provider
    declaring ``working_dir=False`` is a hard validate error — the setting would
    otherwise be silently dropped (agent-mcp-working-dir, todo 1)."""

    def test_agent_working_dir_against_unsupported_provider_errors(self, patch_caps: Any) -> None:
        patch_caps({"copilot": _caps(working_dir=False)})
        config = _build_workflow(
            agents=[AgentDef(name="a", prompt="hi", working_dir="/repo")],
        )
        with pytest.raises(ConfigurationError, match="working_dir"):
            validate_workflow_config(config)

    def test_agent_working_dir_against_supported_provider_passes(self, patch_caps: Any) -> None:
        patch_caps({"copilot": _caps(working_dir=True)})
        config = _build_workflow(
            agents=[AgentDef(name="a", prompt="hi", working_dir="/repo")],
        )
        validate_workflow_config(config)  # no raise

    def test_runtime_working_dir_against_unsupported_provider_errors(self, patch_caps: Any) -> None:
        """runtime.working_dir is inherited by every LLM agent on that provider."""
        patch_caps({"copilot": _caps(working_dir=False)})
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test",
                entry_point="a",
                runtime=RuntimeConfig(provider="copilot", working_dir="/repo"),
            ),
            agents=[AgentDef(name="a", prompt="hi")],
        )
        with pytest.raises(ConfigurationError, match="runtime.working_dir"):
            validate_workflow_config(config)

    def test_runtime_working_dir_all_agents_override_to_capable_provider_passes(
        self, patch_caps: Any
    ) -> None:
        """Default provider incapable but every LLM agent overrides to a capable
        one → runtime.working_dir never reaches the incapable provider."""
        patch_caps(
            {
                "copilot": _caps(working_dir=False),
                "claude": _caps(working_dir=True),
            }
        )
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test",
                entry_point="a",
                runtime=RuntimeConfig(provider="copilot", working_dir="/repo"),
            ),
            agents=[AgentDef(name="a", prompt="hi", provider="claude")],
        )
        validate_workflow_config(config)  # no raise

    def test_per_agent_provider_override_against_working_dir_errors(self, patch_caps: Any) -> None:
        """Agent overriding to a working_dir=False provider errors even when the
        default provider supports working_dir."""
        patch_caps(
            {
                "copilot": _caps(working_dir=True),
                "hermes": _caps(working_dir=False),
            }
        )
        config = _build_workflow(
            agents=[AgentDef(name="a", prompt="hi", provider="hermes", working_dir="/repo")],
        )
        with pytest.raises(ConfigurationError, match="hermes"):
            validate_workflow_config(config)

    def test_for_each_inline_working_dir_against_unsupported_provider_errors(
        self, patch_caps: Any
    ) -> None:
        """for_each inline agents get the same working_dir cross-check (#270 parity)."""
        patch_caps({"copilot": _caps(working_dir=False)})
        config = _for_each_workflow(
            inline=AgentDef(name="inner", prompt="{{ item }}", working_dir="/repo"),
        )
        with pytest.raises(ConfigurationError, match="inner.*working_dir"):
            validate_workflow_config(config)

    def test_for_each_inline_inherits_runtime_working_dir_errors(self, patch_caps: Any) -> None:
        """An inline agent without its own working_dir still inherits the
        runtime-wide default → error on a working_dir=False provider."""
        patch_caps({"copilot": _caps(working_dir=False), "claude": _caps(working_dir=True)})
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test",
                entry_point="entry",
                runtime=RuntimeConfig(provider="copilot", working_dir="/repo"),
            ),
            agents=[AgentDef(name="entry", prompt="hi", provider="claude")],
            for_each=[
                ForEachDef(
                    name="loop",
                    type="for_each",
                    source="entry.output.items",
                    **{"as": "item"},
                    agent=AgentDef(name="inner", prompt="{{ item }}"),
                )
            ],
        )
        with pytest.raises(ConfigurationError, match="runtime.working_dir.*'inner'"):
            validate_workflow_config(config)

    def test_no_working_dir_anywhere_against_unsupported_provider_passes(
        self, patch_caps: Any
    ) -> None:
        """working_dir=False capability alone never errors without the setting."""
        patch_caps({"copilot": _caps(working_dir=False)})
        config = _build_workflow(agents=[AgentDef(name="a", prompt="hi")])
        validate_workflow_config(config)  # no raise

    def test_script_agent_working_dir_skipped(self, patch_caps: Any) -> None:
        """Script steps run a local subprocess (not a provider session) — the
        capability gate must not fire for them even with working_dir set."""
        patch_caps({"copilot": _caps(working_dir=False)})
        config = _build_workflow(
            agents=[AgentDef(name="s", type="script", command="ls", working_dir="/tmp")],
        )
        validate_workflow_config(config)  # no raise
