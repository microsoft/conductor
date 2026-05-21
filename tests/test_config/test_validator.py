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
        refs = _extract_template_refs("{{ writer.output.text }}")
        assert refs.agent_refs == {"writer"}
        assert refs.workflow_inputs == set()

    def test_simple_workflow_input_ref(self) -> None:
        refs = _extract_template_refs("Hello {{ workflow.input.name }}")
        assert refs.agent_refs == set()
        assert refs.workflow_inputs == {"name"}

    def test_outputs_plural_ref(self) -> None:
        refs = _extract_template_refs("{{ pg.outputs.member.field }}")
        assert refs.agent_refs == {"pg"}

    def test_errors_ref(self) -> None:
        refs = _extract_template_refs("{% if pg.errors %}fail{% endif %}")
        assert refs.agent_refs == {"pg"}

    def test_bare_output_ref(self) -> None:
        refs = _extract_template_refs("{{ writer.output }}")
        assert refs.agent_refs == {"writer"}

    def test_for_loop_variable_excluded(self) -> None:
        """Loop-bound vars must not be reported as agent refs (false-positive #1)."""
        refs = _extract_template_refs(
            "{% for r in researcher.outputs %}{{ r.output.text }}{% endfor %}"
        )
        # Only the iterable name; the loop variable `r` is scope-bound.
        assert refs.agent_refs == {"researcher"}

    def test_string_literal_excluded(self) -> None:
        """Names inside string literals must not be reported (false-positive #2)."""
        refs = _extract_template_refs(
            '{{ x | replace("foo.output", "y") | replace("bar.outputs", "z") }}'
        )
        assert refs.agent_refs == set()

    def test_set_binding_excluded(self) -> None:
        refs = _extract_template_refs("{% set x = 1 %}{{ x.output }}")
        assert refs.agent_refs == set()

    def test_built_in_namespaces_excluded(self) -> None:
        for builtin in ("workflow", "context", "item", "loop"):
            refs = _extract_template_refs("{{ " + builtin + ".output.x }}")
            assert refs.agent_refs == set(), f"{builtin} leaked through"

    def test_unrelated_attrs_ignored(self) -> None:
        refs = _extract_template_refs("{{ writer.metadata.author }}")
        assert refs.agent_refs == set()
        assert refs.workflow_inputs == set()

    def test_singular_error_ref_extracted(self) -> None:
        """``agent.error[.field]`` populates ``agent_error_refs``."""
        refs = _extract_template_refs("{{ failing.error.kind }}")
        # Reflected in both flat agent_refs (for unknown-agent checks)
        # AND in the dedicated agent_error_refs set (for explicit-mode
        # undeclared-input warnings on the .error path).
        assert refs.agent_refs == {"failing"}
        assert refs.agent_error_refs == {"failing"}
        # No spurious field-precision tracking for errors.
        assert refs.agent_output_fields == {}

    def test_bare_error_ref_extracted(self) -> None:
        refs = _extract_template_refs("{% if failing.error %}boom{% endif %}")
        assert refs.agent_error_refs == {"failing"}

    def test_error_and_output_can_coexist_for_same_agent(self) -> None:
        """A template referencing both ``a.output`` and ``a.error`` is legal."""
        refs = _extract_template_refs(
            "{{ a.output.text }}{% if a.error %}{{ a.error.kind }}{% endif %}"
        )
        assert refs.agent_refs == {"a"}
        assert refs.agent_error_refs == {"a"}
        assert refs.agent_output_fields == {"a": {"text"}}

    def test_no_template_tags(self) -> None:
        refs = _extract_template_refs("just plain text")
        assert refs.agent_refs == set() and refs.workflow_inputs == set()
        refs = _extract_template_refs("")
        assert refs.agent_refs == set() and refs.workflow_inputs == set()

    def test_malformed_template_returns_empty(self) -> None:
        # Don't raise — render-time validation will surface the precise error.
        refs = _extract_template_refs("{{ unterminated")
        assert refs.agent_refs == set() and refs.workflow_inputs == set()

    def test_multiple_refs_in_one_template(self) -> None:
        refs = _extract_template_refs(
            "{{ a.output }}/{{ b.outputs.x }}/{{ workflow.input.foo }}/{{ workflow.input.bar }}"
        )
        assert refs.agent_refs == {"a", "b"}
        assert refs.workflow_inputs == {"foo", "bar"}

    def test_field_extracted_for_agent_output(self) -> None:
        """Field-precision (Gap A): ``a.output.bar`` produces field ``bar``."""
        refs = _extract_template_refs("{{ a.output.bar }}")
        assert refs.agent_output_fields == {"a": {"bar"}}

    def test_bare_output_marked_with_none_field(self) -> None:
        """Field-precision (Gap A): bare ``a.output`` uses ``None`` sentinel."""
        refs = _extract_template_refs("{{ a.output }}")
        assert refs.agent_output_fields == {"a": {None}}

    def test_prefix_chain_dedup_keeps_specific_field(self) -> None:
        """Field-precision (Gap A): inner ``a.output`` Getattr from
        ``{{ a.output.bar }}`` must NOT spuriously contribute a ``None`` field.
        """
        refs = _extract_template_refs("{{ a.output.bar }}")
        assert refs.agent_output_fields == {"a": {"bar"}}, (
            f"Expected only {{'bar'}}; got {refs.agent_output_fields}"
        )

    def test_method_call_does_not_emit_field(self) -> None:
        """Field-precision (Gap A): ``a.output.items()`` is a method call —
        the ``items`` Getattr is the callee of a Call and is filtered out.
        Only the inner ``a.output`` Getattr is retained as a whole-output ref.
        """
        refs = _extract_template_refs("{% for k, v in a.output.items() %}{{ k }}{% endfor %}")
        assert refs.agent_refs == {"a"}
        assert refs.agent_output_fields == {"a": {None}}

    def test_multiple_fields_on_same_agent(self) -> None:
        refs = _extract_template_refs("{{ a.output.foo }} {{ a.output.bar }}")
        assert refs.agent_output_fields == {"a": {"foo", "bar"}}

    def test_group_member_field_extracted(self) -> None:
        """Field-precision (Gap A): ``g.outputs.m.field`` populates
        ``group_member_fields[(g, m)] = {field}``.
        """
        refs = _extract_template_refs("{{ pg.outputs.member.field }}")
        assert refs.group_member_fields == {("pg", "member"): {"field"}}

    def test_group_bare_outputs_uses_none_member(self) -> None:
        refs = _extract_template_refs("{{ pg.outputs }}")
        assert refs.group_member_fields == {("pg", None): {None}}

    def test_group_errors_kept_separate(self) -> None:
        """Errors refs never get field-precision treatment."""
        refs = _extract_template_refs("{{ pg.errors }} {{ pg.errors.member }}")
        assert refs.group_error_refs == {"pg"}
        # Group_member_fields must NOT include the errors-rooted refs.
        assert refs.group_member_fields == {}

    def test_mixed_bare_and_specific_output_chains(self) -> None:
        """``{{ a.output }} {{ a.output.foo }}`` must produce BOTH a None
        sentinel (whole-output ref) and the ``foo`` field, since each is a
        top-level Getattr chain in the AST.
        """
        refs = _extract_template_refs("{{ a.output }} {{ a.output.foo }}")
        assert refs.agent_output_fields == {"a": {None, "foo"}}


class TestInputRefPatternExtensions:
    """Test the extended INPUT_REF_PATTERN shapes added in this PR."""

    def test_pg_kind_capture(self) -> None:
        """Regression: ``pg_kind`` distinguishes the outputs vs errors
        namespace at declaration time so a ``pg.errors`` declaration cannot
        suppress warnings for ``pg.outputs.*`` references.
        """
        m_out = INPUT_REF_PATTERN.match("group.outputs.member.field")
        assert m_out is not None
        assert m_out.group("pg_kind") == "outputs"
        m_err = INPUT_REF_PATTERN.match("group.errors")
        assert m_err is not None
        assert m_err.group("pg_kind") == "errors"

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

    @pytest.mark.parametrize(
        "ref,expected_agent",
        [
            ("failing_node.error", "failing_node"),
            ("failing_node.error.kind", "failing_node"),
            ("failing_node.error.message", "failing_node"),
            ("failing_node.error?", "failing_node"),
            ("failing_node.error.kind?", "failing_node"),
        ],
    )
    def test_pattern_accepts_agent_error_shapes(self, ref: str, expected_agent: str) -> None:
        """``agent.error[.field]`` (singular) is the on_error envelope ref."""
        match = INPUT_REF_PATTERN.match(ref)
        assert match is not None, f"{ref!r} should match INPUT_REF_PATTERN"
        assert match.group("error_agent") == expected_agent
        # And it must not be misclassified as an output / parallel ref.
        assert match.group("agent") is None
        assert match.group("parallel") is None


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

    def test_script_agent_undeclared_output_ref_warns(self) -> None:
        """Script agents must still declare agent.output refs in explicit mode.

        The engine's ``_LOCAL_RENDER_AGENT_TYPES`` carve-out makes
        ``workflow.input`` available regardless of declarations, but
        ``agent.output`` references are still required (the engine raises
        ``KeyError`` via ``_add_explicit_input`` otherwise). The validator
        should warn about this just like it does for regular LLM agents.
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="producer",
                context=ContextConfig(mode="explicit"),
            ),
            agents=[
                _agent_with_prompt("producer", "make it", routes=[RouteDef(to="consumer")]),
                AgentDef(
                    name="consumer",
                    type="script",
                    command="echo",
                    args=["{{ producer.output.text }}"],
                    # Notably absent: input=["producer.output"]
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert any("producer.output" in w and "consumer" in w and "explicit" in w for w in warnings)

    def test_script_agent_undeclared_workflow_input_no_warning(self) -> None:
        """Regression: script-type carve-out for ``workflow.input`` is preserved.

        The engine populates ``workflow.input`` for script agents regardless of
        explicit mode (see ``_LOCAL_RENDER_AGENT_TYPES``), so no warning should
        be emitted even when the script's ``input:`` doesn't list it.
        """
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
        assert not any(
            "workflow.input.topic" in w and "explicit context mode" in w for w in warnings
        )

    def test_subworkflow_input_mapping_undeclared_output_ref_warns(self) -> None:
        """Sub-workflow input_mapping with undeclared agent.output should warn.

        ``input_mapping`` is rendered against the agent's scoped context (via
        ``build_for_agent``), so undeclared ``agent.output`` references still
        fail at runtime. ``workflow.input`` is carved out (same as scripts)
        but ``agent.output`` is not.
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="producer",
                context=ContextConfig(mode="explicit"),
            ),
            agents=[
                _agent_with_prompt("producer", "make it", routes=[RouteDef(to="child")]),
                AgentDef(
                    name="child",
                    type="workflow",
                    workflow="./child.yaml",
                    input_mapping={"data": "{{ producer.output.value }}"},
                    # Notably absent: input=["producer.output"]
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert any("producer.output" in w and "child" in w and "explicit" in w for w in warnings)

    def test_subworkflow_undeclared_workflow_input_no_warning(self) -> None:
        """Regression: sub-workflow carve-out for ``workflow.input`` is preserved."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="child",
                context=ContextConfig(mode="explicit"),
                input={"topic": InputDef(type="string")},
            ),
            agents=[
                AgentDef(
                    name="child",
                    type="workflow",
                    workflow="./child.yaml",
                    input_mapping={"data": "{{ workflow.input.topic }}"},
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert not any(
            "workflow.input.topic" in w and "explicit context mode" in w for w in warnings
        )

    def test_human_gate_prompt_explicit_mode_no_warning(self) -> None:
        """Regression: human_gate prompts render with the FULL accumulated context.

        Engine renders human_gate prompts via ``context.get_for_template()``
        which forces ``mode='accumulate'`` (see ``WorkflowContext.get_for_template``),
        so explicit-mode declarations are not required. The validator must not
        emit false-positive warnings for ``workflow.input`` or ``agent.output``
        references in human_gate prompts.
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="producer",
                context=ContextConfig(mode="explicit"),
                input={"topic": InputDef(type="string")},
            ),
            agents=[
                _agent_with_prompt("producer", "make it", routes=[RouteDef(to="gate")]),
                AgentDef(
                    name="gate",
                    type="human_gate",
                    prompt=(
                        "Topic: {{ workflow.input.topic }}. "
                        "Producer said: {{ producer.output.text }}. Continue?"
                    ),
                    # Notably absent: input=[...]
                    options=[
                        GateOption(label="Yes", value="y", route="$end"),
                        GateOption(label="No", value="n", route="$end"),
                    ],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert not any("explicit context mode" in w for w in warnings)


class TestExplicitModeFieldPrecision:
    """Field-precision warnings in explicit mode (Gap A in issue #105).

    Declaring ``a.output.foo`` only brings the ``foo`` field into scope at
    runtime (see ``engine/context.py:_add_agent_input``). If the prompt then
    references ``{{ a.output.bar }}``, the engine fails with
    ``TemplateError: 'dict object' has no attribute 'bar'`` — the same
    pattern the issue body describes. The validator should catch this and
    emit a warning at validation time.
    """

    def test_undeclared_field_on_specifically_declared_output_warns(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="producer",
                context=ContextConfig(mode="explicit"),
            ),
            agents=[
                _agent_with_prompt("producer", "make it", routes=[RouteDef(to="writer")]),
                _agent_with_prompt(
                    "writer",
                    "Bar value: {{ producer.output.bar }}",
                    input=["producer.output.foo"],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert any(
            "producer.output.bar" in w and "producer.output.foo" in w and "explicit" in w
            for w in warnings
        )

    def test_declared_field_matches_referenced_field_no_warning(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="producer",
                context=ContextConfig(mode="explicit"),
            ),
            agents=[
                _agent_with_prompt("producer", "make it", routes=[RouteDef(to="writer")]),
                _agent_with_prompt(
                    "writer",
                    "Foo: {{ producer.output.foo }}",
                    input=["producer.output.foo"],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert not any("explicit context mode" in w for w in warnings)

    def test_whole_output_declaration_tolerates_any_field(self) -> None:
        """Declaring ``a.output`` (no field) covers any field reference."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="producer",
                context=ContextConfig(mode="explicit"),
            ),
            agents=[
                _agent_with_prompt("producer", "make it", routes=[RouteDef(to="writer")]),
                _agent_with_prompt(
                    "writer",
                    "Foo: {{ producer.output.foo }} Bar: {{ producer.output.bar }}",
                    input=["producer.output"],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert not any("explicit context mode" in w for w in warnings)

    def test_multiple_declared_fields_allows_each(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="producer",
                context=ContextConfig(mode="explicit"),
            ),
            agents=[
                _agent_with_prompt("producer", "make it", routes=[RouteDef(to="writer")]),
                _agent_with_prompt(
                    "writer",
                    "{{ producer.output.foo }} {{ producer.output.bar }}",
                    input=["producer.output.foo", "producer.output.bar"],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert not any("explicit context mode" in w for w in warnings)

    def test_multiple_declared_fields_warns_on_other_field(self) -> None:
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="producer",
                context=ContextConfig(mode="explicit"),
            ),
            agents=[
                _agent_with_prompt("producer", "make it", routes=[RouteDef(to="writer")]),
                _agent_with_prompt(
                    "writer",
                    "{{ producer.output.baz }}",
                    input=["producer.output.foo", "producer.output.bar"],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert any("producer.output.baz" in w and "explicit" in w for w in warnings)

    def test_bare_output_ref_with_specific_declaration_warns(self) -> None:
        """``{{ a.output }}`` (whole-output ref) paired with only
        specific-field declarations should warn — at runtime the engine only
        copies the declared fields into ctx, so the whole-output access
        gets a partial dict.
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="producer",
                context=ContextConfig(mode="explicit"),
            ),
            agents=[
                _agent_with_prompt("producer", "make it", routes=[RouteDef(to="writer")]),
                _agent_with_prompt(
                    "writer",
                    "{{ producer.output | json }}",
                    input=["producer.output.foo"],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert any(
            "the whole 'producer.output' object" in w and "only declares specific fields" in w
            for w in warnings
        )

    def test_method_call_on_whole_output_declaration_no_warning(self) -> None:
        """``{% for k,v in a.output.items() %}`` paired with a whole-output
        declaration must NOT warn — the dict-method invocation is valid.
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="producer",
                context=ContextConfig(mode="explicit"),
            ),
            agents=[
                _agent_with_prompt("producer", "make it", routes=[RouteDef(to="writer")]),
                _agent_with_prompt(
                    "writer",
                    "{% for k, v in producer.output.items() %}{{ k }}{% endfor %}",
                    input=["producer.output"],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert not any("explicit context mode" in w for w in warnings)

    def test_method_call_on_specific_field_declaration_no_warning(self) -> None:
        """``{% for k,v in a.output.items() %}`` paired with a SPECIFIC-field
        declaration must NOT warn either. Without the Call-node filter the
        validator would otherwise flag ``items`` as a missing field.
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="producer",
                context=ContextConfig(mode="explicit"),
            ),
            agents=[
                _agent_with_prompt("producer", "make it", routes=[RouteDef(to="writer")]),
                _agent_with_prompt(
                    "writer",
                    "{% for k, v in producer.output.items() %}{{ k }}{% endfor %}",
                    input=["producer.output.foo"],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert not any("items" in w and "explicit context mode" in w for w in warnings)

    def test_static_parallel_member_field_precision_warns(self) -> None:
        """``pg.outputs.member.field`` precision works for static parallel groups
        because the engine field-slices on len≥3 (see context.py
        ``_add_parallel_group_input``).
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="pg",
                context=ContextConfig(mode="explicit"),
            ),
            agents=[
                _agent_with_prompt("a", "do a", routes=[]),
                _agent_with_prompt("b", "do b", routes=[]),
                _agent_with_prompt(
                    "consumer",
                    "{{ pg.outputs.a.baz }}",
                    input=["pg.outputs.a.foo"],
                ),
            ],
            parallel=[
                ParallelGroup(name="pg", agents=["a", "b"], routes=[RouteDef(to="consumer")]),
            ],
        )
        warnings = validate_workflow_config(config)
        assert any(
            "pg.outputs.a.baz" in w and "pg.outputs.a.foo" in w and "explicit" in w
            for w in warnings
        )

    def test_for_each_member_field_precision_skipped(self) -> None:
        """For-each groups copy the WHOLE member at runtime
        (``elif is_for_each_dict or len(remaining_parts) == 2``), so the
        validator must NOT emit field-precision warnings for them — that
        would be a false positive.
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="finder",
                context=ContextConfig(mode="explicit"),
            ),
            agents=[
                _agent_with_prompt("finder", "find items", routes=[RouteDef(to="fe")]),
                _agent_with_prompt(
                    "consumer",
                    # References a different field than declared. For for-each
                    # groups this works at runtime, so no warning.
                    "{{ fe.outputs.x.bar }}",
                    input=["fe.outputs.x.foo"],
                ),
            ],
            for_each=[
                ForEachDef(
                    name="fe",
                    type="for_each",
                    source="finder.output.items",
                    **{"as": "item"},
                    key_by="item.id",
                    agent=AgentDef(
                        name="worker",
                        model="gpt-4",
                        prompt="process {{ item }}",
                    ),
                    routes=[RouteDef(to="consumer")],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert not any("fe.outputs.x" in w and "explicit context mode" in w for w in warnings)

    def test_field_precision_in_sub_workflow_input_mapping_warns(self) -> None:
        """input_mapping is rendered against the agent's scoped context, so
        field-precision applies to it the same as to ``prompt``.
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="producer",
                context=ContextConfig(mode="explicit"),
            ),
            agents=[
                _agent_with_prompt("producer", "make it", routes=[RouteDef(to="child")]),
                AgentDef(
                    name="child",
                    type="workflow",
                    workflow="./child.yaml",
                    input_mapping={"data": "{{ producer.output.bar }}"},
                    input=["producer.output.foo"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert any(
            "producer.output.bar" in w and "producer.output.foo" in w and "explicit" in w
            for w in warnings
        )

    # ---------------------------------------------------------------------
    # Static parallel matrix — exhaustive coverage of declaration shapes
    # ---------------------------------------------------------------------

    def _parallel_matrix_config(self, *, declared: list[str], referenced: str) -> WorkflowConfig:
        return WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="pg",
                context=ContextConfig(mode="explicit"),
            ),
            agents=[
                _agent_with_prompt("a", "do a", routes=[]),
                _agent_with_prompt("b", "do b", routes=[]),
                _agent_with_prompt(
                    "consumer",
                    "Use {{ " + referenced + " }}",
                    input=declared,
                    routes=[RouteDef(to="$end")],
                ),
            ],
            parallel=[
                ParallelGroup(name="pg", agents=["a", "b"], routes=[RouteDef(to="consumer")]),
            ],
        )

    def test_parallel_whole_group_declaration_tolerates_any_member_or_field(self) -> None:
        config = self._parallel_matrix_config(
            declared=["pg.outputs"], referenced="pg.outputs.a.foo"
        )
        warnings = validate_workflow_config(config)
        assert not any("explicit context mode" in w for w in warnings)

    def test_parallel_whole_member_declaration_tolerates_any_field(self) -> None:
        config = self._parallel_matrix_config(
            declared=["pg.outputs.a"], referenced="pg.outputs.a.foo"
        )
        warnings = validate_workflow_config(config)
        assert not any("explicit context mode" in w for w in warnings)

    def test_parallel_exact_field_match_no_warning(self) -> None:
        config = self._parallel_matrix_config(
            declared=["pg.outputs.a.foo"], referenced="pg.outputs.a.foo"
        )
        warnings = validate_workflow_config(config)
        assert not any("explicit context mode" in w for w in warnings)

    def test_parallel_bare_member_ref_with_specific_decl_no_field_warning(self) -> None:
        """``{{ pg.outputs.a }}`` (bare member, no field) is not subject to
        the field-precision check — we don't know which fields the consumer
        actually reads on the member object.
        """
        config = self._parallel_matrix_config(
            declared=["pg.outputs.a.foo"], referenced="pg.outputs.a"
        )
        warnings = validate_workflow_config(config)
        # The member IS declared (just with a specific field), so no
        # undeclared warning. And bare-member refs skip field precision.
        assert not any("explicit context mode" in w for w in warnings)

    # ---------------------------------------------------------------------
    # Namespace separation: pg.outputs / pg.errors are NOT interchangeable
    # ---------------------------------------------------------------------

    def test_declared_errors_does_not_suppress_outputs_warning(self) -> None:
        """``input: ["pg.errors"]`` must not suppress an undeclared-outputs
        warning when the template references ``pg.outputs.*``. The engine
        only populates the declared namespace at runtime, so the outputs ref
        will fail with ``KeyError``.
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="pg",
                context=ContextConfig(mode="explicit"),
            ),
            agents=[
                _agent_with_prompt("a", "do a", routes=[]),
                _agent_with_prompt("b", "do b", routes=[]),
                _agent_with_prompt(
                    "consumer",
                    "{{ pg.outputs.a.val }}",
                    input=["pg.errors"],
                ),
            ],
            parallel=[
                ParallelGroup(name="pg", agents=["a", "b"], routes=[RouteDef(to="consumer")]),
            ],
        )
        warnings = validate_workflow_config(config)
        assert any(
            "pg.outputs" in w and "does not declare 'pg.outputs'" in w and "explicit" in w
            for w in warnings
        )

    def test_declared_outputs_does_not_suppress_errors_warning(self) -> None:
        """Symmetric: ``input: ["pg.outputs"]`` must not suppress an
        undeclared-errors warning when the template references ``pg.errors``.
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="pg",
                context=ContextConfig(mode="explicit"),
            ),
            agents=[
                _agent_with_prompt("a", "do a", routes=[]),
                _agent_with_prompt("b", "do b", routes=[]),
                _agent_with_prompt(
                    "consumer",
                    "{% if pg.errors %}fail{% endif %}",
                    input=["pg.outputs"],
                ),
            ],
            parallel=[
                ParallelGroup(name="pg", agents=["a", "b"], routes=[RouteDef(to="consumer")]),
            ],
        )
        warnings = validate_workflow_config(config)
        assert any(
            "pg.errors" in w and "does not declare 'pg.errors'" in w and "explicit" in w
            for w in warnings
        )

    def test_declared_errors_field_does_not_warn_about_outputs_field(self) -> None:
        """Regression: declaring ``pg.errors.a.foo`` must NOT make the
        validator emit a warning that says ``only declares pg.outputs.a.foo``.
        The errors-namespace declaration is unrelated to the outputs ref.
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="pg",
                context=ContextConfig(mode="explicit"),
            ),
            agents=[
                _agent_with_prompt("a", "do a", routes=[]),
                _agent_with_prompt("b", "do b", routes=[]),
                _agent_with_prompt(
                    "consumer",
                    "{{ pg.outputs.a.bar }}",
                    input=["pg.errors.a.foo"],
                ),
            ],
            parallel=[
                ParallelGroup(name="pg", agents=["a", "b"], routes=[RouteDef(to="consumer")]),
            ],
        )
        warnings = validate_workflow_config(config)
        # Should emit the "undeclared outputs" warning, NOT the misleading
        # "only declares pg.outputs.a.foo" message.
        assert not any("only declares pg.outputs.a.foo" in w for w in warnings)
        assert any("pg.outputs" in w and "does not declare 'pg.outputs'" in w for w in warnings)

    # ---------------------------------------------------------------------
    # Other edge cases flagged by review
    # ---------------------------------------------------------------------

    def test_optional_input_with_question_mark_treated_as_declared(self) -> None:
        """Optional inputs (``a.output.foo?``) still count as declarations
        for explicit-mode warning purposes — the ``?`` only affects runtime
        missing-required behavior, not the validation contract.
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="producer",
                context=ContextConfig(mode="explicit"),
            ),
            agents=[
                _agent_with_prompt("producer", "make it", routes=[RouteDef(to="writer")]),
                _agent_with_prompt(
                    "writer",
                    "{{ producer.output.foo }}",
                    input=["producer.output.foo?"],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        assert not any("explicit context mode" in w for w in warnings)

    def test_mixed_bare_and_specific_field_extraction(self) -> None:
        """``{{ a.output }} {{ a.output.foo }}`` extracts BOTH the bare
        whole-output ref and the specific-field ref, so a config declaring
        only one specific field still warns about the bare reference.
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="producer",
                context=ContextConfig(mode="explicit"),
            ),
            agents=[
                _agent_with_prompt("producer", "make it", routes=[RouteDef(to="writer")]),
                _agent_with_prompt(
                    "writer",
                    "{{ producer.output.foo }} {{ producer.output }}",
                    input=["producer.output.foo"],
                ),
            ],
        )
        warnings = validate_workflow_config(config)
        # Should warn about the whole-output ref but not the specific one.
        assert any(
            "the whole 'producer.output'" in w and "only declares specific fields" in w
            for w in warnings
        )

    def test_malformed_template_with_duplicate_block_does_not_hard_fail(self) -> None:
        """Templates that pass ``parse()`` but raise during
        ``meta.find_undeclared_variables`` (e.g. duplicate ``{% block %}``
        names) must not break validation — render-time will surface the
        precise error.
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="t",
                entry_point="writer",
                context=ContextConfig(mode="explicit"),
            ),
            agents=[
                _agent_with_prompt(
                    "writer",
                    "{% block a %}1{% endblock %}{% block a %}2{% endblock %}",
                ),
            ],
        )
        # Should not raise.
        validate_workflow_config(config)


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
        refs = _extract_template_refs("{{ old_agent.output.findings }}")
        assert "old_agent" in refs.agent_refs
        assert not refs.workflow_inputs


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
        """Registry reference fetches the workflow and validates it recursively.

        Mocks only the ``RegistriesConfig`` loader and ``fetch_workflow`` so that
        real ``resolve_ref`` parses ``fetched@team-a#v1.0.0`` end-to-end —
        verifying that workflow name, registry name, and ref all extract
        correctly.
        """
        import textwrap
        from unittest.mock import patch

        from conductor.config.validator import validate_workflow_config
        from conductor.registry.config import RegistriesConfig, RegistryEntry, RegistryType

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

        # Real registry config so resolve_ref can find "team-a"
        registry_config = RegistriesConfig(
            registries={
                "team-a": RegistryEntry(
                    type=RegistryType.github,
                    source="https://github.com/example/team-a",
                ),
            },
        )

        # Capture fetch_workflow args to verify resolve_ref produced the
        # right registry name, workflow name, and ref.
        captured_args: dict[str, object] = {}

        def capture_fetch(registry_name, registry_entry, workflow_name, ref):
            captured_args["registry_name"] = registry_name
            captured_args["workflow_name"] = workflow_name
            captured_args["ref"] = ref
            return cached_sub

        config = self._make_config("fetched@team-a#v1.0.0")
        with (
            patch("conductor.registry.resolver.load_config", return_value=registry_config),
            patch("conductor.registry.cache.fetch_workflow", side_effect=capture_fetch),
        ):
            warnings = validate_workflow_config(config, workflow_path=parent)

        assert warnings == []
        assert captured_args == {
            "registry_name": "team-a",
            "workflow_name": "fetched",
            "ref": "v1.0.0",
        }

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
            pytest.raises(ConfigurationError, match="failed to fetch sub-workflow"),
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
            pytest.raises(ConfigurationError, match="failed to fetch sub-workflow"),
        ):
            validate_workflow_config(config, workflow_path=parent)

    def test_circular_subworkflow_ref_detected(self, tmp_path: Path) -> None:
        """Circular sub-workflow references (A → B → A) are caught during validation.

        Without cycle detection, recursive validation would loop indefinitely.
        With it, validation produces a clear "circular reference" error.
        """
        import textwrap

        from conductor.config.schema import LimitsConfig, RuntimeConfig
        from conductor.config.validator import validate_workflow_config
        from conductor.exceptions import ConfigurationError

        # B references A
        b_yaml = tmp_path / "b.yaml"
        b_yaml.write_text(
            textwrap.dedent("""\
                workflow:
                  name: b
                  entry_point: ref_a
                  runtime:
                    provider: copilot
                  limits:
                    max_iterations: 10
                agents:
                  - name: ref_a
                    type: workflow
                    workflow: ./a.yaml
                    routes:
                      - to: "$end"
                output: {}
            """),
            encoding="utf-8",
        )

        # A references B
        a_yaml = tmp_path / "a.yaml"
        a_yaml.write_text(
            textwrap.dedent("""\
                workflow:
                  name: a
                  entry_point: ref_b
                  runtime:
                    provider: copilot
                  limits:
                    max_iterations: 10
                agents:
                  - name: ref_b
                    type: workflow
                    workflow: ./b.yaml
                    routes:
                      - to: "$end"
                output: {}
            """),
            encoding="utf-8",
        )

        # Parent references A, which kicks off the A → B → A cycle
        parent = tmp_path / "parent.yaml"
        parent.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
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
                    workflow="./a.yaml",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )

        with pytest.raises(ConfigurationError, match="circular sub-workflow reference"):
            validate_workflow_config(config, workflow_path=parent)

    def test_circular_subworkflow_via_case_variant_path(self, tmp_path: Path) -> None:
        """Cycle is detected even when references use different case variants.

        On case-insensitive filesystems (macOS, Windows) ``A.yaml`` and
        ``a.yaml`` are the same file but ``Path.resolve()`` returns different
        strings. The validator uses inode identity ``(st_dev, st_ino)`` for
        cycle detection so cases like ``A.yaml → B.yaml → a.yaml`` are caught
        on the first revisit, regardless of case used in references.

        Skipped on case-sensitive filesystems where the case-variant
        references are genuinely different files.
        """
        import textwrap

        from conductor.config.schema import LimitsConfig, RuntimeConfig
        from conductor.config.validator import validate_workflow_config
        from conductor.exceptions import ConfigurationError

        # Detect case-insensitivity by creating a file and checking if its
        # uppercase variant is found.
        probe = tmp_path / "_case_probe.yaml"
        probe.write_text("x", encoding="utf-8")
        case_insensitive = (tmp_path / "_CASE_PROBE.yaml").exists()
        probe.unlink()

        if not case_insensitive:
            pytest.skip("case-insensitive filesystem required (e.g. macOS, Windows)")

        # Write A.yaml that references B.YAML (uppercase)
        a_yaml = tmp_path / "A.yaml"
        a_yaml.write_text(
            textwrap.dedent("""\
                workflow:
                  name: a
                  entry_point: ref_b
                  runtime:
                    provider: copilot
                  limits:
                    max_iterations: 10
                agents:
                  - name: ref_b
                    type: workflow
                    workflow: B.YAML
                    routes:
                      - to: "$end"
                output: {}
            """),
            encoding="utf-8",
        )
        # Write B.yaml that references a.yaml (lowercase) — same file as A.yaml
        # on a case-insensitive FS, completing the cycle
        b_yaml = tmp_path / "B.yaml"
        b_yaml.write_text(
            textwrap.dedent("""\
                workflow:
                  name: b
                  entry_point: ref_a
                  runtime:
                    provider: copilot
                  limits:
                    max_iterations: 10
                agents:
                  - name: ref_a
                    type: workflow
                    workflow: a.yaml
                    routes:
                      - to: "$end"
                output: {}
            """),
            encoding="utf-8",
        )

        parent = tmp_path / "parent.yaml"
        parent.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
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
                    workflow="A.yaml",  # same file as a.yaml on case-insensitive FS
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )

        # Without inode-based detection, str(Path.resolve()) differs for
        # A.yaml vs a.yaml on macOS, so the cycle would still be caught
        # (just one level later, after both case variants are visited).
        # Inode-based detection catches it on the first revisit and also
        # correctly handles symlinks. Either way, the user sees a clear
        # circular-reference error rather than hitting the depth limit.
        with pytest.raises(ConfigurationError, match="circular sub-workflow reference"):
            validate_workflow_config(config, workflow_path=parent)

    def test_validation_depth_limit_emits_warning(self, tmp_path: Path) -> None:
        """Hitting the recursion depth limit emits a warning, not a silent pass.

        Builds a 12-level deep chain (parent → a0 → a1 → ... → a11) and
        verifies the validator stops at depth ``_MAX_SUBWORKFLOW_VALIDATION_DEPTH``
        but emits a warning so the user knows validation was truncated.
        """
        import textwrap

        from conductor.config.schema import LimitsConfig, RuntimeConfig
        from conductor.config.validator import (
            _MAX_SUBWORKFLOW_VALIDATION_DEPTH,
            validate_workflow_config,
        )

        # Build a deep linear chain a0 → a1 → a2 ... → a{N+1}
        depth = _MAX_SUBWORKFLOW_VALIDATION_DEPTH + 2
        for i in range(depth):
            next_ref = f"./a{i + 1}.yaml" if i + 1 < depth else "$end"
            if next_ref == "$end":
                # Terminal sub-workflow: just a single agent
                content = textwrap.dedent(f"""\
                    workflow:
                      name: a{i}
                      entry_point: terminal
                      runtime:
                        provider: copilot
                      limits:
                        max_iterations: 10
                    agents:
                      - name: terminal
                        type: agent
                        prompt: done
                        routes:
                          - to: "$end"
                    output: {{}}
                """)
            else:
                content = textwrap.dedent(f"""\
                    workflow:
                      name: a{i}
                      entry_point: nested
                      runtime:
                        provider: copilot
                      limits:
                        max_iterations: 10
                    agents:
                      - name: nested
                        type: workflow
                        workflow: {next_ref}
                        routes:
                          - to: "$end"
                    output: {{}}
                """)
            (tmp_path / f"a{i}.yaml").write_text(content, encoding="utf-8")

        parent = tmp_path / "parent.yaml"
        parent.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
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
                    workflow="./a0.yaml",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )

        warnings = validate_workflow_config(config, workflow_path=parent)
        assert any("depth limit" in w for w in warnings), (
            f"Expected a depth-limit warning, got warnings: {warnings}"
        )

    def test_adhoc_ref_validates_fetched_workflow(self, tmp_path: Path) -> None:
        """Ad-hoc registry reference (owner/repo) validates fetched workflow.

        Uses `_make_config("analysis@myorg/workflows#v1.0.0")` where the
        registry slot contains a literal owner/repo path. Mocks only
        ``fetch_workflow_adhoc`` so the validator's real ``resolve_ref``
        runs end-to-end, exercising the parsing of the adhoc format.
        """
        import textwrap
        from unittest.mock import patch

        from conductor.config.validator import validate_workflow_config

        cached_sub = tmp_path / "fetched.yaml"
        cached_sub.write_text(
            textwrap.dedent("""\
                workflow:
                  name: analysis
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

        # Capture fetch_workflow_adhoc args to verify the right owner/repo/workflow/ref
        captured_args: dict[str, object] = {}

        def capture_adhoc_fetch(owner, repo, workflow_name, ref):
            captured_args["owner"] = owner
            captured_args["repo"] = repo
            captured_args["workflow_name"] = workflow_name
            captured_args["ref"] = ref
            return cached_sub

        config = self._make_config("analysis@myorg/workflows#v1.0.0")
        with patch(
            "conductor.registry.cache.fetch_workflow_adhoc",
            side_effect=capture_adhoc_fetch,
        ):
            warnings = validate_workflow_config(config, workflow_path=parent)

        assert warnings == []
        assert captured_args == {
            "owner": "myorg",
            "repo": "workflows",
            "workflow_name": "analysis",
            "ref": "v1.0.0",
        }

    def test_adhoc_fetch_failure_validation_error(self, tmp_path: Path) -> None:
        """Ad-hoc registry fetch failure during validation produces ConfigurationError."""
        from unittest.mock import patch

        from conductor.config.validator import validate_workflow_config
        from conductor.registry.errors import RegistryError

        parent = tmp_path / "parent.yaml"
        parent.write_text("dummy", encoding="utf-8")

        config = self._make_config("missing@acme/tools#latest")
        with (
            patch(
                "conductor.registry.cache.fetch_workflow_adhoc",
                side_effect=RegistryError("workflow not found"),
            ),
            pytest.raises(ConfigurationError, match="failed to fetch sub-workflow"),
        ):
            validate_workflow_config(config, workflow_path=parent)

    def test_relative_ref_to_sibling_workflow_in_registry_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cross-workflow relative refs between workflows in the same registry validate.

        Reproduces the bug where ``conductor validate <registry-workflow>`` for
        a workflow at ``sdd/plan.yaml`` that references
        ``../document-review/workflow.yaml`` failed with "sub-workflow file
        not found", even though ``conductor run`` for the same workflow
        succeeds via the engine's auto-fetch hook. Validation must mirror
        the engine and auto-fetch the sibling workflow from the same
        registry+SHA cache.
        """
        import json
        import textwrap
        from unittest.mock import patch

        from conductor.config.loader import load_config
        from conductor.config.validator import validate_workflow_config
        from conductor.registry.cache import CACHE_LAYOUT_VERSION
        from conductor.registry.index import RegistryIndex, WorkflowInfo

        # Point CONDUCTOR_HOME at a temp dir so the cache lives there.
        home = tmp_path / "conductor_home"
        home.mkdir()
        monkeypatch.setenv("CONDUCTOR_HOME", str(home))

        sha = "a" * 40
        sha_dir = sha[:12]
        cache_base = home / "cache" / "registries"
        official_sha_root = cache_base / "official" / sha_dir

        # Pre-cache the parent workflow (sdd/plan.yaml) only — the sibling
        # ``document-review/workflow.yaml`` must be auto-fetched during
        # validation.
        parent_dir = official_sha_root / "sdd"
        parent_dir.mkdir(parents=True)
        parent_path = parent_dir / "plan.yaml"
        parent_path.write_text(
            textwrap.dedent("""\
                workflow:
                  name: sdd-plan
                  entry_point: document_review
                  runtime:
                    provider: copilot
                  limits:
                    max_iterations: 10
                agents:
                  - name: document_review
                    type: workflow
                    workflow: ../document-review/workflow.yaml
                    routes:
                      - to: "$end"
                output:
                  result: "{{ document_review.output.verdict }}"
            """),
            encoding="utf-8",
        )

        # Pre-write source.json + cached index + sentinel for the parent.
        meta_dir = cache_base / "official" / "_meta" / sha_dir
        meta_dir.mkdir(parents=True)
        (meta_dir / "source.json").write_text(
            json.dumps(
                {
                    "cache_layout_version": CACHE_LAYOUT_VERSION,
                    "registry_type": "github",
                    "source": "myorg/workflows",
                    "full_sha": sha,
                },
                sort_keys=True,
                indent=2,
            )
        )
        (meta_dir / "index.yaml").write_text(
            "workflows:\n"
            "  sdd-plan:\n    description: ''\n    path: sdd/plan.yaml\n"
            "  document-review:\n    description: ''\n    path: document-review/workflow.yaml\n"
        )
        (meta_dir / "sdd-plan.complete").write_text("")

        sub_yaml = textwrap.dedent(
            """\
            workflow:
              name: document-review
              entry_point: reviewer
              runtime:
                provider: copilot
              limits:
                max_iterations: 10
            agents:
              - name: reviewer
                type: agent
                prompt: review the doc
                routes:
                  - to: "$end"
            output:
              verdict: "{{ reviewer.output.verdict }}"
            """
        )

        index_obj = RegistryIndex(
            workflows={
                "sdd-plan": WorkflowInfo(description="", path="sdd/plan.yaml"),
                "document-review": WorkflowInfo(
                    description="", path="document-review/workflow.yaml"
                ),
            }
        )

        def fake_fetch_github(entry, workflow_path, sha_arg, dest_dir):
            target = dest_dir / workflow_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(sub_yaml)

        config = load_config(parent_path)

        with (
            patch("conductor.registry.cache.materialize_to_sha", return_value=sha),
            patch("conductor.registry.cache.resolve_ref", return_value=sha),
            patch("conductor.registry.cache.load_index", return_value=index_obj),
            patch("conductor.registry.cache._fetch_github", side_effect=fake_fetch_github),
        ):
            # Should succeed without raising — the sibling is auto-fetched.
            warnings = validate_workflow_config(config, workflow_path=parent_path)

        assert warnings == []
        # Confirm the sibling was actually auto-fetched into the shared SHA root.
        sibling = official_sha_root / "document-review" / "workflow.yaml"
        assert sibling.exists()
