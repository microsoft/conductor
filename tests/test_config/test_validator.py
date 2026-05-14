"""Tests for the cross-field validator."""

from __future__ import annotations

from pathlib import Path

import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    ForEachDef,
    GateOption,
    InputDef,
    ParallelGroup,
    RouteDef,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.config.validator import (
    INPUT_REF_PATTERN,
    _collect_template_strings,
    _extract_template_refs,
    validate_workflow_config,
)
from conductor.exceptions import ConfigurationError


class TestValidateWorkflowConfig:
    """Tests for the validate_workflow_config function."""

    def test_valid_simple_config(self) -> None:
        """Test validation of a valid simple config."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(name="agent1", model="gpt-4", prompt="Hello", routes=[RouteDef(to="$end")])
            ],
        )
        # Should not raise
        warnings = validate_workflow_config(config)
        assert isinstance(warnings, list)

    def test_valid_multi_agent_config(self) -> None:
        """Test validation of a valid multi-agent config."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Step 1",
                    routes=[RouteDef(to="agent2")],
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Step 2",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert isinstance(warnings, list)

    def test_warns_on_system_prompt_without_prompt(self) -> None:
        """Agent with system_prompt but no prompt: should produce a warning.

        The Copilot provider concatenates system_prompt with the user prompt,
        so a missing prompt means an empty user message — almost always a
        latent author mistake. Other providers (Claude) ignore system_prompt
        entirely, so the agent would have no instructions at all.
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="lonely"),
            agents=[
                AgentDef(
                    name="lonely",
                    model="gpt-4",
                    system_prompt="You are a helpful assistant.",
                    # no prompt:
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert any(
            "lonely" in w and "system_prompt" in w and "no `prompt`" in w for w in warnings
        ), f"expected system_prompt-without-prompt warning; got: {warnings!r}"

    def test_no_warning_when_prompt_present_alongside_system_prompt(self) -> None:
        """Having both system_prompt and prompt is the expected pattern — no warning."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="ok"),
            agents=[
                AgentDef(
                    name="ok",
                    model="gpt-4",
                    system_prompt="You are a helpful assistant.",
                    prompt="Answer: 42",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert not any("system_prompt" in w and "no `prompt`" in w for w in warnings)


class TestRouteValidation:
    """Tests for route target validation."""

    def test_valid_route_to_agent(self) -> None:
        """Test that route to existing agent is valid."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Step 1",
                    routes=[RouteDef(to="agent2")],
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Step 2",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        # Should not raise
        validate_workflow_config(config)

    def test_valid_route_to_end(self) -> None:
        """Test that route to $end is valid."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(name="agent1", model="gpt-4", prompt="Hello", routes=[RouteDef(to="$end")])
            ],
        )
        # Should not raise
        validate_workflow_config(config)


class TestHumanGateValidation:
    """Tests for human gate validation."""

    def test_valid_human_gate(self) -> None:
        """Test validation of a valid human gate."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="gate1"),
            agents=[
                AgentDef(
                    name="gate1",
                    type="human_gate",
                    prompt="Choose:",
                    options=[
                        GateOption(label="Yes", value="yes", route="agent2"),
                        GateOption(label="No", value="no", route="$end"),
                    ],
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Hello",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        # Should not raise
        validate_workflow_config(config)

    def test_gate_option_invalid_route(self) -> None:
        """Test that gate option with invalid route raises error."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="gate1"),
            agents=[
                AgentDef(
                    name="gate1",
                    type="human_gate",
                    prompt="Choose:",
                    options=[
                        GateOption(label="Yes", value="yes", route="nonexistent"),
                    ],
                ),
            ],
        )
        with pytest.raises(ConfigurationError) as exc_info:
            validate_workflow_config(config)
        assert "nonexistent" in str(exc_info.value)


class TestInputReferenceValidation:
    """Tests for input reference validation."""

    def test_valid_workflow_input_reference(self) -> None:
        """Test valid workflow input reference."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test",
                entry_point="agent1",
                input={"goal": InputDef(type="string")},
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Hello",
                    input=["workflow.input.goal"],
                    routes=[RouteDef(to="$end")],
                )
            ],
        )
        # Should not raise
        validate_workflow_config(config)

    def test_valid_agent_output_reference(self) -> None:
        """Test valid agent output reference."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Step 1",
                    routes=[RouteDef(to="agent2")],
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Step 2",
                    input=["agent1.output"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        # Should not raise
        validate_workflow_config(config)

    def test_invalid_input_reference_format(self) -> None:
        """Test that invalid input reference format raises error."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Hello",
                    input=["invalid_format"],
                    routes=[RouteDef(to="$end")],
                )
            ],
        )
        with pytest.raises(ConfigurationError) as exc_info:
            validate_workflow_config(config)
        assert "invalid_format" in str(exc_info.value)

    def test_reference_to_unknown_agent(self) -> None:
        """Test that reference to unknown agent raises error."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Hello",
                    input=["unknown.output"],
                    routes=[RouteDef(to="$end")],
                )
            ],
        )
        with pytest.raises(ConfigurationError) as exc_info:
            validate_workflow_config(config)
        assert "unknown" in str(exc_info.value)

    def test_reference_to_unknown_workflow_input(self) -> None:
        """Test that reference to unknown workflow input raises error."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Hello",
                    input=["workflow.input.nonexistent"],
                    routes=[RouteDef(to="$end")],
                )
            ],
        )
        with pytest.raises(ConfigurationError) as exc_info:
            validate_workflow_config(config)
        assert "nonexistent" in str(exc_info.value)

    def test_optional_reference_to_unknown_agent_warns(self) -> None:
        """Test that optional reference to unknown agent produces warning."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Hello",
                    input=["unknown.output?"],
                    routes=[RouteDef(to="$end")],
                )
            ],
        )
        warnings = validate_workflow_config(config)
        assert any("unknown" in w for w in warnings)


class TestToolValidation:
    """Tests for tool reference validation."""

    def test_valid_tool_reference(self) -> None:
        """Test that valid tool reference passes validation."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            tools=["web_search", "calculator"],
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Hello",
                    tools=["web_search"],
                    routes=[RouteDef(to="$end")],
                )
            ],
        )
        # Should not raise
        validate_workflow_config(config)

    def test_invalid_tool_reference(self) -> None:
        """Test that invalid tool reference raises error."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            tools=["web_search"],
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Hello",
                    tools=["unknown_tool"],
                    routes=[RouteDef(to="$end")],
                )
            ],
        )
        with pytest.raises(ConfigurationError) as exc_info:
            validate_workflow_config(config)
        assert "unknown_tool" in str(exc_info.value)

    def test_no_tools_defined_but_agent_uses_some(self) -> None:
        """Test agent using tools when no tools defined at workflow level."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            tools=[],  # No tools defined
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Hello",
                    tools=["web_search"],  # But agent wants to use one
                    routes=[RouteDef(to="$end")],
                )
            ],
        )
        with pytest.raises(ConfigurationError) as exc_info:
            validate_workflow_config(config)
        assert "web_search" in str(exc_info.value)


class TestOutputReferenceValidation:
    """Tests for workflow output reference validation."""

    def test_valid_output_reference(self) -> None:
        """Test that valid output references pass validation."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(name="agent1", model="gpt-4", prompt="Hello", routes=[RouteDef(to="$end")])
            ],
            output={"result": "{{ agent1.output }}"},
        )
        # Should not raise
        validate_workflow_config(config)

    def test_output_reference_to_unknown_agent(self) -> None:
        """Test that output reference to unknown agent raises error."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(name="agent1", model="gpt-4", prompt="Hello", routes=[RouteDef(to="$end")])
            ],
            output={"result": "{{ unknown_agent.output }}"},
        )
        with pytest.raises(ConfigurationError) as exc_info:
            validate_workflow_config(config)
        assert "unknown_agent" in str(exc_info.value)


class TestOutputPathCoverage:
    """Tests for output template path coverage validation."""

    def test_no_warning_linear_workflow(self) -> None:
        """Linear A→B→$end with output refs to both produces no warnings."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent_a"),
            agents=[
                AgentDef(
                    name="agent_a",
                    model="gpt-4",
                    prompt="A",
                    routes=[RouteDef(to="agent_b")],
                ),
                AgentDef(
                    name="agent_b",
                    model="gpt-4",
                    prompt="B",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "a_result": "{{ agent_a.output }}",
                "b_result": "{{ agent_b.output }}",
            },
        )
        warnings = validate_workflow_config(config)
        assert not warnings

    def test_warning_conditionally_skipped_agent(self) -> None:
        """Evaluator routes to deployer OR $end; output refs deployer → warning."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="evaluator"),
            agents=[
                AgentDef(
                    name="evaluator",
                    model="gpt-4",
                    prompt="Evaluate",
                    routes=[
                        RouteDef(to="deployer", when="{{ evaluator.output.approved }}"),
                        RouteDef(to="$end"),
                    ],
                ),
                AgentDef(
                    name="deployer",
                    model="gpt-4",
                    prompt="Deploy",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"summary": "{{ deployer.output.summary }}"},
        )
        warnings = validate_workflow_config(config)
        assert any("deployer" in w for w in warnings)

    def test_no_warning_agent_on_all_branches(self) -> None:
        """Router routes to A on both branches; output refs A → no warning."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="router"),
            agents=[
                AgentDef(
                    name="router",
                    model="gpt-4",
                    prompt="Route",
                    routes=[
                        RouteDef(to="agent_a", when="{{ router.output.fast }}"),
                        RouteDef(to="agent_a"),
                    ],
                ),
                AgentDef(
                    name="agent_a",
                    model="gpt-4",
                    prompt="Do work",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ agent_a.output }}"},
        )
        warnings = validate_workflow_config(config)
        assert not warnings

    def test_parallel_group_member_available(self) -> None:
        """Entry→pg(a,b)→$end, output refs member 'a' via pg → no warning."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="entry"),
            agents=[
                AgentDef(name="entry", model="gpt-4", prompt="Go", routes=[RouteDef(to="pg")]),
                AgentDef(name="agent_a", model="gpt-4", prompt="A"),
                AgentDef(name="agent_b", model="gpt-4", prompt="B"),
            ],
            parallel=[
                ParallelGroup(
                    name="pg",
                    agents=["agent_a", "agent_b"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"a_out": "{{ agent_a.output }}"},
        )
        warnings = validate_workflow_config(config)
        assert not warnings

    def test_warning_skippable_parallel_group(self) -> None:
        """Router→(pg→$end OR $end), output refs pg member agent → warning."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="router"),
            agents=[
                AgentDef(
                    name="router",
                    model="gpt-4",
                    prompt="Route",
                    routes=[
                        RouteDef(to="pg", when="{{ router.output.needs_parallel }}"),
                        RouteDef(to="$end"),
                    ],
                ),
                AgentDef(name="agent_a", model="gpt-4", prompt="A"),
                AgentDef(name="agent_b", model="gpt-4", prompt="B"),
            ],
            parallel=[
                ParallelGroup(
                    name="pg",
                    agents=["agent_a", "agent_b"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"a_out": "{{ agent_a.output }}"},
        )
        warnings = validate_workflow_config(config)
        assert any("agent_a" in w for w in warnings)

    def test_human_gate_conditional_paths(self) -> None:
        """Gate(opt1→agent_a, opt2→$end), output refs agent_a → warning."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="gate"),
            agents=[
                AgentDef(
                    name="gate",
                    type="human_gate",
                    prompt="Choose:",
                    options=[
                        GateOption(label="Approve", value="yes", route="agent_a"),
                        GateOption(label="Reject", value="no", route="$end"),
                    ],
                ),
                AgentDef(
                    name="agent_a",
                    model="gpt-4",
                    prompt="Do work",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ agent_a.output }}"},
        )
        warnings = validate_workflow_config(config)
        assert any("agent_a" in w for w in warnings)

    def test_no_warning_when_no_output_section(self) -> None:
        """No output dict → no warnings."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Hello",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert not warnings

    def test_loop_does_not_crash(self) -> None:
        """A→B→(A OR $end), output refs A and B → no crash, no warnings."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent_a"),
            agents=[
                AgentDef(
                    name="agent_a",
                    model="gpt-4",
                    prompt="A",
                    routes=[RouteDef(to="agent_b")],
                ),
                AgentDef(
                    name="agent_b",
                    model="gpt-4",
                    prompt="B",
                    routes=[
                        RouteDef(to="agent_a", when="{{ agent_b.output.retry }}"),
                        RouteDef(to="$end"),
                    ],
                ),
            ],
            output={
                "a_result": "{{ agent_a.output }}",
                "b_result": "{{ agent_b.output }}",
            },
        )
        warnings = validate_workflow_config(config)
        assert not warnings

    def test_warning_message_format(self) -> None:
        """Warning contains path string and {% if suggestion."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="evaluator"),
            agents=[
                AgentDef(
                    name="evaluator",
                    model="gpt-4",
                    prompt="Evaluate",
                    routes=[
                        RouteDef(to="deployer", when="{{ evaluator.output.approved }}"),
                        RouteDef(to="$end"),
                    ],
                ),
                AgentDef(
                    name="deployer",
                    model="gpt-4",
                    prompt="Deploy",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"summary": "{{ deployer.output.summary }}"},
        )
        warnings = validate_workflow_config(config)
        assert len(warnings) == 1
        warning = warnings[0]
        assert "deployer" in warning
        assert "evaluator" in warning
        assert "$end" in warning
        assert "{% if deployer is defined %}" in warning

    def test_for_each_on_every_path(self) -> None:
        """Entry→fe→$end, output refs fe.outputs → no warning.

        Uses a for-each group on the only path to $end, so the reference
        should not produce a path coverage warning.
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="analyzers"),
            agents=[],
            for_each=[
                ForEachDef(
                    name="analyzers",
                    type="for_each",
                    source="workflow.input.items",
                    **{"as": "item"},
                    agent=AgentDef(name="analyzer", model="gpt-4", prompt="Analyze {{ item }}"),
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"results": "{{ analyzers.outputs }}"},
        )
        warnings = validate_workflow_config(config)
        assert not warnings


def _agent_with_prompt(name: str, prompt: str, **kwargs: object) -> AgentDef:
    """Tiny helper: AgentDef with model defaulted, routes terminating at $end."""
    routes = kwargs.pop("routes", [RouteDef(to="$end")])
    model = kwargs.pop("model", "gpt-4")
    return AgentDef(name=name, model=model, prompt=prompt, routes=routes, **kwargs)  # type: ignore[arg-type]


class TestExtractTemplateRefs:
    """Unit tests for the Jinja2-AST-based reference extractor."""

    def test_simple_agent_output_ref(self) -> None:
        agents, inputs = _extract_template_refs("{{ writer.output.text }}")
        assert agents == {"writer"}
        assert inputs == set()

    def test_simple_workflow_input_ref(self) -> None:
        agents, inputs = _extract_template_refs("Hello {{ workflow.input.name }}")
        assert agents == set()
        assert inputs == {"name"}

    def test_outputs_plural_ref(self) -> None:
        agents, _ = _extract_template_refs("{{ pg.outputs.member.field }}")
        assert agents == {"pg"}

    def test_errors_ref(self) -> None:
        agents, _ = _extract_template_refs("{% if pg.errors %}fail{% endif %}")
        assert agents == {"pg"}

    def test_bare_output_ref(self) -> None:
        agents, _ = _extract_template_refs("{{ writer.output }}")
        assert agents == {"writer"}

    def test_for_loop_variable_excluded(self) -> None:
        """Loop-bound vars must not be reported as agent refs (false-positive #1)."""
        agents, _ = _extract_template_refs(
            "{% for r in researcher.outputs %}{{ r.output.text }}{% endfor %}"
        )
        # Only the iterable name; the loop variable `r` is scope-bound.
        assert agents == {"researcher"}

    def test_string_literal_excluded(self) -> None:
        """Names inside string literals must not be reported (false-positive #2)."""
        agents, _ = _extract_template_refs(
            '{{ x | replace("foo.output", "y") | replace("bar.outputs", "z") }}'
        )
        assert agents == set()

    def test_set_binding_excluded(self) -> None:
        agents, _ = _extract_template_refs("{% set x = 1 %}{{ x.output }}")
        assert agents == set()

    def test_built_in_namespaces_excluded(self) -> None:
        for builtin in ("workflow", "context", "item", "loop"):
            agents, _ = _extract_template_refs("{{ " + builtin + ".output.x }}")
            assert agents == set(), f"{builtin} leaked through"

    def test_unrelated_attrs_ignored(self) -> None:
        agents, inputs = _extract_template_refs("{{ writer.metadata.author }}")
        assert agents == set()
        assert inputs == set()

    def test_no_template_tags(self) -> None:
        assert _extract_template_refs("just plain text") == (set(), set())
        assert _extract_template_refs("") == (set(), set())

    def test_malformed_template_returns_empty(self) -> None:
        # Don't raise — render-time validation will surface the precise error.
        assert _extract_template_refs("{{ unterminated") == (set(), set())

    def test_multiple_refs_in_one_template(self) -> None:
        agents, inputs = _extract_template_refs(
            "{{ a.output }}/{{ b.outputs.x }}/{{ workflow.input.foo }}/{{ workflow.input.bar }}"
        )
        assert agents == {"a", "b"}
        assert inputs == {"foo", "bar"}


class TestInputRefPatternExtensions:
    """Test the extended INPUT_REF_PATTERN shapes added in this PR."""

    @pytest.mark.parametrize(
        "ref,expected_parallel",
        [
            ("group.errors", "group"),
            ("group.errors.member", "group"),
            ("group.errors.member.field", "group"),
            ("group.outputs", "group"),
            ("group.outputs.member", "group"),
            ("group.outputs.member.field", "group"),
            ("group.errors?", "group"),
            ("group.outputs?", "group"),
        ],
    )
    def test_pattern_accepts_new_shapes(self, ref: str, expected_parallel: str) -> None:
        match = INPUT_REF_PATTERN.match(ref)
        assert match is not None, f"{ref!r} should match INPUT_REF_PATTERN"
        assert match.group("parallel") == expected_parallel

    @pytest.mark.parametrize(
        "ref",
        [
            "agent.output",
            "agent.output.field",
            "agent.output?",
            "agent.output.field?",
            "workflow.input.param",
            "workflow.input.param?",
        ],
    )
    def test_pattern_still_accepts_legacy_shapes(self, ref: str) -> None:
        assert INPUT_REF_PATTERN.match(ref) is not None

    @pytest.mark.parametrize(
        "ref",
        [
            "workflow.input",  # bare workflow.input no longer accepted
            "agent",
            "agent.foo",
            "group.bogus",
        ],
    )
    def test_pattern_rejects_invalid_shapes(self, ref: str) -> None:
        assert INPUT_REF_PATTERN.match(ref) is None


class TestTemplateReferenceValidation:
    """End-to-end tests for stale-reference detection in agent templates."""

    def test_stale_agent_ref_in_prompt_errors(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(name="t", entry_point="writer"),
            agents=[
                _agent_with_prompt("writer", "Based on {{ old_name.output.findings }}, write."),
            ],
        )
        with pytest.raises(ConfigurationError, match="unknown agent 'old_name'"):
            validate_workflow_config(config)

    def test_stale_agent_ref_in_system_prompt_errors(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(name="t", entry_point="writer"),
            agents=[
                _agent_with_prompt(
                    "writer",
                    "ok",
                    system_prompt="Use {{ ghost.output }}",
                ),
            ],
        )
        with pytest.raises(ConfigurationError, match="system_prompt.*unknown agent 'ghost'"):
            validate_workflow_config(config)

    def test_stale_agent_ref_in_script_args_errors(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(name="t", entry_point="step"),
            agents=[
                AgentDef(
                    name="step",
                    type="script",
                    command="echo",
                    args=["{{ ghost.output.value }}"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        with pytest.raises(ConfigurationError, match=r"args\[0\].*unknown agent 'ghost'"):
            validate_workflow_config(config)

    def test_stale_agent_ref_in_command_errors(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(name="t", entry_point="step"),
            agents=[
                AgentDef(
                    name="step",
                    type="script",
                    command="run-{{ ghost.output }}",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        with pytest.raises(ConfigurationError, match="command.*unknown agent 'ghost'"):
            validate_workflow_config(config)

    def test_stale_agent_ref_in_working_dir_errors(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(name="t", entry_point="step"),
            agents=[
                AgentDef(
                    name="step",
                    type="script",
                    command="echo",
                    working_dir="/tmp/{{ ghost.output }}",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        with pytest.raises(ConfigurationError, match="working_dir.*unknown agent 'ghost'"):
            validate_workflow_config(config)

    def test_unknown_workflow_input_errors(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="writer",
                input={"topic": InputDef(type="string")},
            ),
            agents=[
                _agent_with_prompt("writer", "Write about {{ workflow.input.nonexistent }}"),
            ],
        )
        with pytest.raises(ConfigurationError, match="unknown workflow input 'nonexistent'"):
            validate_workflow_config(config)

    def test_workflow_input_unchecked_when_no_input_block(self) -> None:
        """Workflows without an input: block may use workflow.input freely."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="t", entry_point="writer"),
            agents=[
                _agent_with_prompt("writer", "{{ workflow.input.anything }}"),
            ],
        )
        # Should not raise — this is the documented escape hatch.
        validate_workflow_config(config)

    def test_for_loop_variable_does_not_error(self) -> None:
        """Regression test: false-positive #1 from PR review."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="t", entry_point="aggregator"),
            agents=[
                AgentDef(name="r1", model="gpt-4", prompt="Research"),
                AgentDef(name="r2", model="gpt-4", prompt="Research"),
                _agent_with_prompt(
                    "aggregator",
                    "{% for r in pg.outputs %}- {{ r.output.text }}{% endfor %}",
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="pg",
                    agents=["r1", "r2"],
                    routes=[RouteDef(to="aggregator")],
                ),
            ],
        )
        # No raise: `r` is a loop variable, not a stale agent name.
        validate_workflow_config(config)

    def test_string_literal_does_not_error(self) -> None:
        """Regression test: false-positive #2 from PR review."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="t", entry_point="writer"),
            agents=[
                _agent_with_prompt("writer", '{{ "x" | replace("foo.output", "y") }}'),
            ],
        )
        validate_workflow_config(config)

    def test_for_each_inline_agent_template_scanned(self) -> None:
        """For-each groups have inline agents whose templates must also be checked."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="t", entry_point="analyzers"),
            agents=[],
            for_each=[
                ForEachDef(
                    name="analyzers",
                    type="for_each",
                    source="workflow.input.items",
                    **{"as": "item"},
                    agent=AgentDef(
                        name="analyzer",
                        model="gpt-4",
                        prompt="Analyze {{ ghost.output.x }}",
                    ),
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        with pytest.raises(ConfigurationError, match="unknown agent 'ghost'"):
            validate_workflow_config(config)

    def test_for_each_inline_agent_loop_var_does_not_error(self) -> None:
        """`item`, `_index`, `_key` are built-ins and must not error inside fe agents."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="t", entry_point="analyzers"),
            agents=[],
            for_each=[
                ForEachDef(
                    name="analyzers",
                    type="for_each",
                    source="workflow.input.items",
                    **{"as": "item"},
                    agent=AgentDef(
                        name="analyzer",
                        model="gpt-4",
                        prompt="Analyze {{ item }} index={{ _index }}",
                    ),
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        validate_workflow_config(config)

    def test_for_each_group_accepted_in_input_references(self) -> None:
        """For-each group names should be valid in agent input: lists."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="t", entry_point="analyzers"),
            agents=[
                _agent_with_prompt(
                    "summarizer",
                    "{{ analyzers.outputs }}",
                    input=["analyzers.outputs"],
                ),
            ],
            for_each=[
                ForEachDef(
                    name="analyzers",
                    type="for_each",
                    source="workflow.input.items",
                    **{"as": "item"},
                    agent=AgentDef(name="a", model="gpt-4", prompt="{{ item }}"),
                    routes=[RouteDef(to="summarizer")],
                ),
            ],
        )
        validate_workflow_config(config)


class TestExplicitModeWarnings:
    """Tests for explicit-context-mode advisory warnings."""

    def test_undeclared_agent_ref_in_explicit_mode_warns(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="writer",
                context=ContextConfig(mode="explicit"),
            ),
            agents=[
                _agent_with_prompt("research", "do work", routes=[RouteDef(to="writer")]),
                _agent_with_prompt(
                    "writer",
                    "Use {{ research.output.findings }}",
                    # Notably absent: input=["research.output"]
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert any("research.output" in w and "explicit" in w and "writer" in w for w in warnings)

    def test_undeclared_workflow_input_ref_in_explicit_mode_warns(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="writer",
                context=ContextConfig(mode="explicit"),
                input={"topic": InputDef(type="string")},
            ),
            agents=[
                _agent_with_prompt(
                    "writer",
                    "About {{ workflow.input.topic }}",
                    # Notably absent: input=["workflow.input.topic"]
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert any("workflow.input.topic" in w and "explicit" in w for w in warnings)

    def test_declared_input_in_explicit_mode_no_warning(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="writer",
                context=ContextConfig(mode="explicit"),
                input={"topic": InputDef(type="string")},
            ),
            agents=[
                _agent_with_prompt(
                    "writer",
                    "About {{ workflow.input.topic }}",
                    input=["workflow.input.topic"],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        # Should have no explicit-mode advisories.
        assert not any("explicit context mode" in w for w in warnings)

    def test_script_agents_skipped_in_explicit_mode(self) -> None:
        """Script and workflow agents don't carry input declarations the same way."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="step",
                context=ContextConfig(mode="explicit"),
                input={"topic": InputDef(type="string")},
            ),
            agents=[
                AgentDef(
                    name="step",
                    type="script",
                    command="echo",
                    args=["{{ workflow.input.topic }}"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert not any("explicit context mode" in w for w in warnings)


class TestOutputTemplateValidation:
    """Tests for unknown references in workflow `output:` templates."""

    def test_unknown_agent_in_output_errors(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(name="t", entry_point="writer"),
            agents=[_agent_with_prompt("writer", "ok")],
            output={"summary": "{{ ghost.output }}"},
        )
        with pytest.raises(ConfigurationError, match=r"output 'summary'.*unknown agent 'ghost'"):
            validate_workflow_config(config)

    def test_unknown_workflow_input_in_output_errors(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="writer",
                input={"topic": InputDef(type="string")},
            ),
            agents=[_agent_with_prompt("writer", "ok")],
            output={"echo": "{{ workflow.input.bogus }}"},
        )
        with pytest.raises(
            ConfigurationError, match=r"output 'echo'.*unknown workflow input 'bogus'"
        ):
            validate_workflow_config(config)


class TestExamplesRegression:
    """Every example workflow under examples/ must still validate."""

    def test_all_bundled_examples_validate(self) -> None:
        from conductor.config.loader import load_config

        examples_dir = Path(__file__).resolve().parents[2] / "examples"
        yaml_files = sorted(examples_dir.glob("*.yaml"))
        assert yaml_files, "no example workflows found — fixture path wrong?"

        failures: list[str] = []
        for path in yaml_files:
            try:
                config = load_config(path)
                validate_workflow_config(config, workflow_path=path)
            except Exception as e:  # pragma: no cover - report on failure only
                failures.append(f"{path.name}: {type(e).__name__}: {e}")

        assert not failures, "examples failed validation:\n  " + "\n  ".join(failures)


class TestInputMappingTemplateCollection:
    """Coverage for input_mapping template collection.

    The input_mapping field was added to AgentDef by PR #109 (closing #101). On
    branches that have merged that schema change, _collect_template_strings
    should pick up its templates so stale-ref scanning catches them. The helper
    uses getattr so this stays a no-op on branches that haven't merged it yet,
    and these tests use a duck-typed object to exercise the path regardless.
    """

    @staticmethod
    def _make_agent_like(
        name: str,
        prompt: str | None,
        input_mapping: dict[str, str] | None,
    ) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(
            name=name,
            prompt=prompt,
            system_prompt=None,
            command=None,
            args=[],
            working_dir=None,
            input_mapping=input_mapping,
        )

    def test_collects_input_mapping_templates(self) -> None:
        agent = self._make_agent_like(
            name="caller",
            prompt="Call sub-workflow",
            input_mapping={
                "topic": "{{ workflow.input.topic }}",
                "research": "{{ researcher.output.findings }}",
            },
        )
        templates = _collect_template_strings(agent)  # type: ignore[arg-type]
        labels = {label for label, _ in templates}
        assert "agent 'caller' input_mapping.topic" in labels
        assert "agent 'caller' input_mapping.research" in labels

    def test_no_input_mapping_collects_nothing_extra(self) -> None:
        agent = self._make_agent_like(name="caller", prompt="Simple", input_mapping=None)
        templates = _collect_template_strings(agent)  # type: ignore[arg-type]
        labels = {label for label, _ in templates}
        assert not any("input_mapping" in label for label in labels)

    def test_extractor_catches_stale_ref_in_input_mapping(self) -> None:
        # Simulates: when this PR merges with main's AgentDef.input_mapping,
        # a stale agent reference inside an input_mapping expression is caught
        # by _extract_template_refs the same as any other template field.
        agent_refs, input_refs = _extract_template_refs("{{ old_agent.output.findings }}")
        assert "old_agent" in agent_refs
        assert not input_refs


class TestSubWorkflowRefValidation:
    """Tests for _validate_subworkflow_refs in validate_workflow_config."""

    def _make_config(self, workflow_ref: str) -> WorkflowConfig:
        from conductor.config.schema import LimitsConfig, RuntimeConfig

        return WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="sub_wf",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="sub_wf",
                    type="workflow",
                    workflow=workflow_ref,
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )

    def test_local_sub_workflow_validates_ok(self, tmp_path: Path) -> None:
        """Local file sub-workflow passes validation when file exists."""
        import textwrap

        from conductor.config.validator import validate_workflow_config

        sub = tmp_path / "sub.yaml"
        sub.write_text(
            textwrap.dedent("""\
                workflow:
                  name: sub
                  entry_point: step
                  runtime:
                    provider: copilot
                  limits:
                    max_iterations: 10
                agents:
                  - name: step
                    type: agent
                    prompt: go
                    routes:
                      - to: "$end"
                output: {}
            """),
            encoding="utf-8",
        )
        parent = tmp_path / "parent.yaml"
        parent.write_text("dummy", encoding="utf-8")

        config = self._make_config("./sub.yaml")
        warnings = validate_workflow_config(config, workflow_path=parent)
        assert warnings == []

    def test_missing_local_sub_workflow_errors(self, tmp_path: Path) -> None:
        """Missing local file sub-workflow produces a validation error."""
        from conductor.config.validator import validate_workflow_config
        from conductor.exceptions import ConfigurationError

        parent = tmp_path / "parent.yaml"
        parent.write_text("dummy", encoding="utf-8")

        config = self._make_config("./nonexistent.yaml")
        with pytest.raises(ConfigurationError, match="sub-workflow file not found"):
            validate_workflow_config(config, workflow_path=parent)

    def test_malformed_registry_ref_errors(self, tmp_path: Path) -> None:
        """Malformed registry reference (two '@') produces a validation error."""
        from conductor.config.validator import validate_workflow_config
        from conductor.exceptions import ConfigurationError

        parent = tmp_path / "parent.yaml"
        parent.write_text("dummy", encoding="utf-8")

        config = self._make_config("a@b@c")  # two '@' — malformed
        with pytest.raises(ConfigurationError, match="invalid sub-workflow reference"):
            validate_workflow_config(config, workflow_path=parent)

    def test_registry_ref_validates_fetched_workflow(self, tmp_path: Path) -> None:
        """Registry reference fetches the workflow and validates it recursively."""
        import textwrap
        from unittest.mock import patch

        from conductor.config.validator import validate_workflow_config
        from conductor.registry.config import RegistryEntry, RegistryType
        from conductor.registry.resolver import ResolvedRef

        cached_sub = tmp_path / "fetched.yaml"
        cached_sub.write_text(
            textwrap.dedent("""\
                workflow:
                  name: fetched
                  entry_point: step
                  runtime:
                    provider: copilot
                  limits:
                    max_iterations: 10
                agents:
                  - name: step
                    type: agent
                    prompt: go
                    routes:
                      - to: "$end"
                output: {}
            """),
            encoding="utf-8",
        )

        parent = tmp_path / "parent.yaml"
        parent.write_text("dummy", encoding="utf-8")

        fake_entry = RegistryEntry(type=RegistryType.github, source="https://github.com/x/y")
        fake_resolved = ResolvedRef(
            kind="registry",
            workflow="fetched",
            registry_name="team-a",
            ref="v1.0.0",
            registry_entry=fake_entry,
        )

        config = self._make_config("fetched@team-a#v1.0.0")
        with (
            patch("conductor.registry.resolver.resolve_ref", return_value=fake_resolved),
            patch("conductor.registry.cache.fetch_workflow", return_value=cached_sub),
        ):
            warnings = validate_workflow_config(config, workflow_path=parent)

        assert warnings == []

    def test_registry_fetch_failure_errors(self, tmp_path: Path) -> None:
        """Registry fetch failure during validation produces a clear error."""
        from unittest.mock import patch

        from conductor.config.validator import validate_workflow_config
        from conductor.exceptions import ConfigurationError
        from conductor.registry.config import RegistryEntry, RegistryType
        from conductor.registry.errors import RegistryError
        from conductor.registry.resolver import ResolvedRef

        parent = tmp_path / "parent.yaml"
        parent.write_text("dummy", encoding="utf-8")

        fake_entry = RegistryEntry(type=RegistryType.github, source="https://github.com/x/y")
        fake_resolved = ResolvedRef(
            kind="registry",
            workflow="missing",
            registry_name="team-a",
            ref="v1.0.0",
            registry_entry=fake_entry,
        )

        config = self._make_config("missing@team-a#v1.0.0")
        with (
            patch("conductor.registry.resolver.resolve_ref", return_value=fake_resolved),
            patch(
                "conductor.registry.cache.fetch_workflow",
                side_effect=RegistryError("workflow not found"),
            ),
            pytest.raises(ConfigurationError, match="failed to fetch registry sub-workflow"),
        ):
            validate_workflow_config(config, workflow_path=parent)

    def test_for_each_workflow_agent_ref_validated(self, tmp_path: Path) -> None:
        """Registry ref inside a for_each inline workflow agent is validated."""
        from unittest.mock import patch

        from conductor.config.schema import ForEachDef, LimitsConfig, RuntimeConfig
        from conductor.config.validator import validate_workflow_config
        from conductor.exceptions import ConfigurationError
        from conductor.registry.config import RegistryEntry, RegistryType
        from conductor.registry.errors import RegistryError
        from conductor.registry.resolver import ResolvedRef

        parent = tmp_path / "parent.yaml"
        parent.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="batch",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="loader",
                    type="agent",
                    prompt="load items",
                    routes=[RouteDef(to="batch")],
                ),
            ],
            for_each=[
                ForEachDef(
                    name="batch",
                    type="for_each",
                    source="loader.output.items",
                    **{"as": "item"},
                    agent=AgentDef(
                        name="worker",
                        type="workflow",
                        workflow="missing@team-a#v1.0.0",
                        routes=[RouteDef(to="$end")],
                    ),
                    routes=[RouteDef(to="$end")],
                )
            ],
        )

        fake_entry = RegistryEntry(type=RegistryType.github, source="https://github.com/x/y")
        fake_resolved = ResolvedRef(
            kind="registry",
            workflow="missing",
            registry_name="team-a",
            ref="v1.0.0",
            registry_entry=fake_entry,
        )

        with (
            patch("conductor.registry.resolver.resolve_ref", return_value=fake_resolved),
            patch(
                "conductor.registry.cache.fetch_workflow",
                side_effect=RegistryError("workflow not found"),
            ),
            pytest.raises(ConfigurationError, match="failed to fetch registry sub-workflow"),
        ):
            validate_workflow_config(config, workflow_path=parent)
