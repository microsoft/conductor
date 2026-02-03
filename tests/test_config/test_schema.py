"""Tests for the Pydantic schema models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    ForEachDef,
    GateOption,
    HooksConfig,
    InputDef,
    LimitsConfig,
    OutputField,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)


class TestInputDef:
    """Tests for InputDef model."""

    def test_valid_string_input(self) -> None:
        """Test creating a valid string input."""
        input_def = InputDef(type="string", required=True, description="A test input")
        assert input_def.type == "string"
        assert input_def.required is True
        assert input_def.default is None

    def test_valid_input_with_default(self) -> None:
        """Test creating an input with a default value."""
        input_def = InputDef(type="string", required=False, default="hello")
        assert input_def.default == "hello"

    def test_number_input_with_valid_default(self) -> None:
        """Test number input with valid numeric default."""
        input_def = InputDef(type="number", default=42)
        assert input_def.default == 42

    def test_number_input_with_float_default(self) -> None:
        """Test number input with float default."""
        input_def = InputDef(type="number", default=3.14)
        assert input_def.default == 3.14

    def test_boolean_input_with_valid_default(self) -> None:
        """Test boolean input with valid boolean default."""
        input_def = InputDef(type="boolean", default=True)
        assert input_def.default is True

    def test_array_input_with_valid_default(self) -> None:
        """Test array input with valid list default."""
        input_def = InputDef(type="array", default=["a", "b"])
        assert input_def.default == ["a", "b"]

    def test_object_input_with_valid_default(self) -> None:
        """Test object input with valid dict default."""
        input_def = InputDef(type="object", default={"key": "value"})
        assert input_def.default == {"key": "value"}

    def test_invalid_default_type_raises(self) -> None:
        """Test that mismatched default type raises ValidationError."""
        with pytest.raises(ValidationError):
            InputDef(type="string", default=123)

    def test_invalid_type_raises(self) -> None:
        """Test that invalid type raises ValidationError."""
        with pytest.raises(ValidationError):
            InputDef(type="invalid_type")  # type: ignore


class TestOutputField:
    """Tests for OutputField model."""

    def test_simple_string_output(self) -> None:
        """Test creating a simple string output field."""
        output = OutputField(type="string", description="A result")
        assert output.type == "string"
        assert output.description == "A result"

    def test_array_output_with_items(self) -> None:
        """Test array output with item schema."""
        output = OutputField(
            type="array",
            items=OutputField(type="string"),
        )
        assert output.type == "array"
        assert output.items is not None
        assert output.items.type == "string"

    def test_object_output_with_properties(self) -> None:
        """Test object output with properties."""
        output = OutputField(
            type="object",
            properties={
                "name": OutputField(type="string"),
                "count": OutputField(type="number"),
            },
        )
        assert output.type == "object"
        assert output.properties is not None
        assert "name" in output.properties
        assert output.properties["name"].type == "string"


class TestRouteDef:
    """Tests for RouteDef model."""

    def test_simple_route(self) -> None:
        """Test creating a simple unconditional route."""
        route = RouteDef(to="next_agent")
        assert route.to == "next_agent"
        assert route.when is None

    def test_conditional_route(self) -> None:
        """Test creating a conditional route."""
        route = RouteDef(to="success_agent", when="{{ output.success }}")
        assert route.to == "success_agent"
        assert route.when == "{{ output.success }}"

    def test_route_with_output_transform(self) -> None:
        """Test route with output transformation."""
        route = RouteDef(
            to="next",
            output={"result": "{{ output.value }}"},
        )
        assert route.output == {"result": "{{ output.value }}"}

    def test_end_route(self) -> None:
        """Test route to $end."""
        route = RouteDef(to="$end")
        assert route.to == "$end"

    def test_empty_target_raises(self) -> None:
        """Test that empty route target raises ValidationError."""
        with pytest.raises(ValidationError):
            RouteDef(to="")


class TestGateOption:
    """Tests for GateOption model."""

    def test_simple_option(self) -> None:
        """Test creating a simple gate option."""
        option = GateOption(label="Approve", value="approved", route="next_agent")
        assert option.label == "Approve"
        assert option.value == "approved"
        assert option.route == "next_agent"
        assert option.prompt_for is None

    def test_option_with_prompt_for(self) -> None:
        """Test gate option with text input prompt."""
        option = GateOption(
            label="Request Changes",
            value="changes",
            route="reviewer",
            prompt_for="feedback",
        )
        assert option.prompt_for == "feedback"


class TestContextConfig:
    """Tests for ContextConfig model."""

    def test_default_values(self) -> None:
        """Test default context configuration."""
        config = ContextConfig()
        assert config.mode == "accumulate"
        assert config.max_tokens is None
        assert config.trim_strategy is None

    def test_explicit_mode(self) -> None:
        """Test explicit context mode."""
        config = ContextConfig(mode="explicit")
        assert config.mode == "explicit"

    def test_with_trimming(self) -> None:
        """Test context config with trimming options."""
        config = ContextConfig(
            mode="accumulate",
            max_tokens=4000,
            trim_strategy="truncate",
        )
        assert config.max_tokens == 4000
        assert config.trim_strategy == "truncate"

    def test_invalid_mode_raises(self) -> None:
        """Test that invalid mode raises ValidationError."""
        with pytest.raises(ValidationError):
            ContextConfig(mode="invalid")  # type: ignore


class TestLimitsConfig:
    """Tests for LimitsConfig model."""

    def test_default_values(self) -> None:
        """Test default limits configuration."""
        config = LimitsConfig()
        assert config.max_iterations == 10
        assert config.timeout_seconds is None  # Unlimited by default

    def test_custom_limits(self) -> None:
        """Test custom limits."""
        config = LimitsConfig(max_iterations=50, timeout_seconds=1200)
        assert config.max_iterations == 50
        assert config.timeout_seconds == 1200

    def test_max_iterations_bounds(self) -> None:
        """Test max_iterations bounds validation."""
        with pytest.raises(ValidationError):
            LimitsConfig(max_iterations=0)  # Below minimum

        with pytest.raises(ValidationError):
            LimitsConfig(max_iterations=501)  # Above maximum

    def test_timeout_bounds(self) -> None:
        """Test timeout_seconds bounds validation."""
        with pytest.raises(ValidationError):
            LimitsConfig(timeout_seconds=0)  # Below minimum (must be >= 1 when set)

        # No upper bound - large values are allowed (unlimited when None)
        config = LimitsConfig(timeout_seconds=3601)
        assert config.timeout_seconds == 3601


class TestHooksConfig:
    """Tests for HooksConfig model."""

    def test_empty_hooks(self) -> None:
        """Test empty hooks configuration."""
        config = HooksConfig()
        assert config.on_start is None
        assert config.on_complete is None
        assert config.on_error is None

    def test_all_hooks(self) -> None:
        """Test all hooks configured."""
        config = HooksConfig(
            on_start="starting",
            on_complete="completed",
            on_error="error occurred",
        )
        assert config.on_start == "starting"
        assert config.on_complete == "completed"
        assert config.on_error == "error occurred"


class TestAgentDef:
    """Tests for AgentDef model."""

    def test_minimal_agent(self) -> None:
        """Test creating a minimal agent."""
        agent = AgentDef(name="agent1", model="gpt-4", prompt="Hello")
        assert agent.name == "agent1"
        assert agent.model == "gpt-4"
        assert agent.type is None
        assert agent.routes == []
        assert agent.input == []

    def test_agent_with_all_fields(self) -> None:
        """Test agent with all fields populated."""
        agent = AgentDef(
            name="full_agent",
            description="A fully configured agent",
            type="agent",
            model="gpt-4",
            input=["workflow.input.goal"],
            tools=["web_search"],
            system_prompt="You are helpful.",
            prompt="Process: {{ workflow.input.goal }}",
            output={"result": OutputField(type="string")},
            routes=[RouteDef(to="$end")],
        )
        assert agent.description == "A fully configured agent"
        assert agent.type == "agent"
        assert len(agent.tools) == 1
        assert agent.system_prompt == "You are helpful."

    def test_human_gate_with_options(self) -> None:
        """Test human_gate agent with options."""
        agent = AgentDef(
            name="gate1",
            type="human_gate",
            prompt="Choose an option:",
            options=[
                GateOption(label="Yes", value="yes", route="next"),
                GateOption(label="No", value="no", route="$end"),
            ],
        )
        assert agent.type == "human_gate"
        assert len(agent.options) == 2

    def test_human_gate_without_options_raises(self) -> None:
        """Test that human_gate without options raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(name="gate1", type="human_gate", prompt="Choose:")
        assert "options" in str(exc_info.value)

    def test_human_gate_without_prompt_raises(self) -> None:
        """Test that human_gate without prompt raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(
                name="gate1",
                type="human_gate",
                options=[GateOption(label="Ok", value="ok", route="next")],
            )
        assert "prompt" in str(exc_info.value)


class TestRuntimeConfig:
    """Tests for RuntimeConfig model."""

    def test_default_values(self) -> None:
        """Test default runtime configuration."""
        config = RuntimeConfig()
        assert config.provider == "copilot"
        assert config.default_model is None
        assert config.temperature is None
        assert config.max_tokens is None
        assert config.timeout is None

    def test_custom_provider(self) -> None:
        """Test custom provider setting."""
        config = RuntimeConfig(provider="openai-agents", default_model="gpt-4")
        assert config.provider == "openai-agents"
        assert config.default_model == "gpt-4"

    def test_invalid_provider_raises(self) -> None:
        """Test that invalid provider raises ValidationError."""
        with pytest.raises(ValidationError):
            RuntimeConfig(provider="invalid")  # type: ignore

    def test_claude_provider_with_temperature(self) -> None:
        """Test Claude provider with temperature setting."""
        config = RuntimeConfig(provider="claude", temperature=0.7)
        assert config.provider == "claude"
        assert config.temperature == 0.7

    def test_temperature_boundary_values(self) -> None:
        """Test temperature field accepts boundary values."""
        # Lower bound
        config = RuntimeConfig(temperature=0.0)
        assert config.temperature == 0.0

        # Upper bound
        config = RuntimeConfig(temperature=1.0)
        assert config.temperature == 1.0

        # Mid-range value
        config = RuntimeConfig(temperature=0.5)
        assert config.temperature == 0.5

    def test_temperature_out_of_range_raises(self) -> None:
        """Test temperature field rejects out-of-range values."""
        # Below lower bound
        with pytest.raises(ValidationError) as exc_info:
            RuntimeConfig(temperature=-0.1)
        assert "greater than or equal to 0" in str(exc_info.value)

        # Above upper bound
        with pytest.raises(ValidationError) as exc_info:
            RuntimeConfig(temperature=1.1)
        assert "less than or equal to 1" in str(exc_info.value)

    def test_max_tokens_boundary_values(self) -> None:
        """Test max_tokens field accepts boundary values."""
        # Lower bound
        config = RuntimeConfig(max_tokens=1)
        assert config.max_tokens == 1

        # Typical value for Haiku
        config = RuntimeConfig(max_tokens=4096)
        assert config.max_tokens == 4096

        # Typical value for Opus/Sonnet
        config = RuntimeConfig(max_tokens=8192)
        assert config.max_tokens == 8192

        # Upper bound (context window)
        config = RuntimeConfig(max_tokens=200000)
        assert config.max_tokens == 200000

    def test_max_tokens_out_of_range_raises(self) -> None:
        """Test max_tokens field rejects out-of-range values."""
        # Below lower bound
        with pytest.raises(ValidationError) as exc_info:
            RuntimeConfig(max_tokens=0)
        assert "greater than or equal to 1" in str(exc_info.value)

        # Above upper bound
        with pytest.raises(ValidationError) as exc_info:
            RuntimeConfig(max_tokens=200001)
        assert "less than or equal to 200000" in str(exc_info.value)

    def test_all_common_fields_together(self) -> None:
        """Test RuntimeConfig with all common fields."""
        config = RuntimeConfig(
            provider="claude",
            default_model="claude-3-5-sonnet-latest",
            temperature=0.7,
            max_tokens=4096,
            timeout=120.0,
        )
        assert config.provider == "claude"
        assert config.default_model == "claude-3-5-sonnet-latest"
        assert config.temperature == 0.7
        assert config.max_tokens == 4096
        assert config.timeout == 120.0

    def test_serialization_excludes_none_values(self) -> None:
        """Test Pydantic serialization with exclude_none=True excludes new fields.

        This verifies backward compatibility: existing Copilot workflows should
        not have optional fields appear in their serialized output.
        """
        # Create a minimal config (Copilot provider, no optional fields)
        config = RuntimeConfig(provider="copilot", default_model="gpt-4")

        # Serialize with exclude_none=True
        serialized = config.model_dump(exclude_none=True)

        # Verify only non-None fields are present
        assert "provider" in serialized
        assert "default_model" in serialized
        assert serialized["provider"] == "copilot"
        assert serialized["default_model"] == "gpt-4"

        # Verify optional fields are NOT present when None
        assert "temperature" not in serialized
        assert "max_tokens" not in serialized
        assert "timeout" not in serialized

    def test_serialization_includes_explicit_values(self) -> None:
        """Test that explicitly set fields are serialized."""
        config = RuntimeConfig(
            provider="claude",
            temperature=0.7,
            max_tokens=4096,
        )

        serialized = config.model_dump(exclude_none=True)

        # Explicitly set fields should be present
        assert "provider" in serialized
        assert "temperature" in serialized
        assert "max_tokens" in serialized
        assert serialized["temperature"] == 0.7
        assert serialized["max_tokens"] == 4096

    def test_round_trip_serialization(self) -> None:
        """Test round-trip serialization preserves values."""
        original = RuntimeConfig(
            provider="claude",
            default_model="claude-3-5-sonnet-latest",
            temperature=0.8,
            max_tokens=8192,
            timeout=120.0,
        )

        # Serialize and deserialize
        serialized = original.model_dump()
        restored = RuntimeConfig(**serialized)

        # Verify all fields match
        assert restored.provider == original.provider
        assert restored.default_model == original.default_model
        assert restored.temperature == original.temperature
        assert restored.max_tokens == original.max_tokens
        assert restored.timeout == original.timeout


class TestWorkflowDef:
    """Tests for WorkflowDef model."""

    def test_minimal_workflow(self) -> None:
        """Test minimal workflow definition."""
        workflow = WorkflowDef(name="test", entry_point="agent1")
        assert workflow.name == "test"
        assert workflow.entry_point == "agent1"
        assert workflow.runtime.provider == "copilot"

    def test_full_workflow(self) -> None:
        """Test fully configured workflow definition."""
        workflow = WorkflowDef(
            name="full",
            description="A full workflow",
            version="1.0.0",
            entry_point="start",
            runtime=RuntimeConfig(provider="copilot"),
            input={"goal": InputDef(type="string")},
            context=ContextConfig(mode="explicit"),
            limits=LimitsConfig(max_iterations=20),
            hooks=HooksConfig(on_start="starting"),
        )
        assert workflow.version == "1.0.0"
        assert workflow.context.mode == "explicit"
        assert workflow.limits.max_iterations == 20


class TestWorkflowConfig:
    """Tests for WorkflowConfig model."""

    def test_minimal_config(self) -> None:
        """Test minimal valid workflow configuration."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(name="agent1", model="gpt-4", prompt="Hello", routes=[RouteDef(to="$end")])
            ],
        )
        assert config.workflow.name == "test"
        assert len(config.agents) == 1

    def test_entry_point_validation(self) -> None:
        """Test that entry_point must exist in agents."""
        with pytest.raises(ValidationError) as exc_info:
            WorkflowConfig(
                workflow=WorkflowDef(name="test", entry_point="nonexistent"),
                agents=[AgentDef(name="agent1", model="gpt-4", prompt="Hello")],
            )
        assert "entry_point" in str(exc_info.value)
        assert "nonexistent" in str(exc_info.value)

    def test_route_target_validation(self) -> None:
        """Test that route targets must exist."""
        with pytest.raises(ValidationError) as exc_info:
            WorkflowConfig(
                workflow=WorkflowDef(name="test", entry_point="agent1"),
                agents=[
                    AgentDef(
                        name="agent1",
                        model="gpt-4",
                        prompt="Hello",
                        routes=[RouteDef(to="unknown_agent")],
                    )
                ],
            )
        assert "unknown_agent" in str(exc_info.value)

    def test_end_route_is_valid(self) -> None:
        """Test that $end is always a valid route target."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(name="agent1", model="gpt-4", prompt="Hello", routes=[RouteDef(to="$end")])
            ],
        )
        assert config.agents[0].routes[0].to == "$end"

    def test_multi_agent_routing(self) -> None:
        """Test routing between multiple agents."""
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
        assert len(config.agents) == 2
        assert config.agents[0].routes[0].to == "agent2"

    def test_workflow_with_tools(self) -> None:
        """Test workflow with tools configuration."""
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
        assert len(config.tools) == 2
        assert config.agents[0].tools == ["web_search"]

    def test_workflow_with_output(self) -> None:
        """Test workflow with output templates."""
        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="agent1"),
            agents=[
                AgentDef(name="agent1", model="gpt-4", prompt="Hello", routes=[RouteDef(to="$end")])
            ],
            output={
                "result": "{{ agent1.output }}",
                "summary": "Completed",
            },
        )
        assert len(config.output) == 2


class TestParallelGroup:
    """Tests for ParallelGroup model."""

    def test_valid_parallel_group(self) -> None:
        """Test creating a valid parallel group."""
        from conductor.config.schema import ParallelGroup

        group = ParallelGroup(
            name="research_group",
            agents=["agent1", "agent2"],
            description="Parallel research agents",
        )
        assert group.name == "research_group"
        assert len(group.agents) == 2
        assert group.failure_mode == "fail_fast"
        assert group.description == "Parallel research agents"

    def test_parallel_group_default_failure_mode(self) -> None:
        """Test that failure_mode defaults to fail_fast."""
        from conductor.config.schema import ParallelGroup

        group = ParallelGroup(name="test", agents=["a1", "a2"])
        assert group.failure_mode == "fail_fast"

    def test_parallel_group_all_failure_modes(self) -> None:
        """Test all valid failure modes."""
        from conductor.config.schema import ParallelGroup

        for mode in ["fail_fast", "continue_on_error", "all_or_nothing"]:
            group = ParallelGroup(name="test", agents=["a1", "a2"], failure_mode=mode)
            assert group.failure_mode == mode

    def test_parallel_group_invalid_failure_mode(self) -> None:
        """Test that invalid failure mode raises error."""
        from conductor.config.schema import ParallelGroup

        with pytest.raises(ValidationError) as exc_info:
            ParallelGroup(name="test", agents=["a1", "a2"], failure_mode="invalid")
        assert "failure_mode" in str(exc_info.value)

    def test_parallel_group_minimum_agents_validation(self) -> None:
        """Test that parallel groups require at least 2 agents."""
        from conductor.config.schema import ParallelGroup

        with pytest.raises(ValidationError) as exc_info:
            ParallelGroup(name="test", agents=["only_one"])
        assert "at least 2 agents" in str(exc_info.value)

    def test_parallel_group_empty_agents(self) -> None:
        """Test that parallel groups cannot have empty agents list."""
        from conductor.config.schema import ParallelGroup

        with pytest.raises(ValidationError) as exc_info:
            ParallelGroup(name="test", agents=[])
        assert "at least 2 agents" in str(exc_info.value)

    def test_parallel_group_many_agents(self) -> None:
        """Test parallel group with many agents."""
        from conductor.config.schema import ParallelGroup

        agents = [f"agent{i}" for i in range(10)]
        group = ParallelGroup(name="big_group", agents=agents)
        assert len(group.agents) == 10


class TestWorkflowConfigWithParallel:
    """Tests for WorkflowConfig with parallel groups."""

    def test_workflow_with_parallel_group(self) -> None:
        """Test workflow configuration with parallel group."""
        from conductor.config.schema import ParallelGroup

        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="parallel_group"),
            agents=[
                AgentDef(name="agent1", model="gpt-4", prompt="Task 1"),
                AgentDef(name="agent2", model="gpt-4", prompt="Task 2"),
            ],
            parallel=[ParallelGroup(name="parallel_group", agents=["agent1", "agent2"])],
        )
        assert len(config.parallel) == 1
        assert config.parallel[0].name == "parallel_group"

    def test_workflow_parallel_group_agent_validation(self) -> None:
        """Test that parallel groups must reference existing agents."""
        from conductor.config.schema import ParallelGroup

        with pytest.raises(ValidationError) as exc_info:
            WorkflowConfig(
                workflow=WorkflowDef(name="test", entry_point="agent1"),
                agents=[
                    AgentDef(name="agent1", model="gpt-4", prompt="Task 1"),
                ],
                parallel=[ParallelGroup(name="pg", agents=["agent1", "nonexistent"])],
            )
        assert "unknown agent 'nonexistent'" in str(exc_info.value).lower()

    def test_workflow_route_to_parallel_group(self) -> None:
        """Test routing from agent to parallel group."""
        from conductor.config.schema import ParallelGroup

        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="starter"),
            agents=[
                AgentDef(name="starter", model="gpt-4", prompt="Start", routes=[RouteDef(to="pg")]),
                AgentDef(name="agent1", model="gpt-4", prompt="Task 1"),
                AgentDef(name="agent2", model="gpt-4", prompt="Task 2"),
            ],
            parallel=[ParallelGroup(name="pg", agents=["agent1", "agent2"])],
        )
        assert config.agents[0].routes[0].to == "pg"

    def test_workflow_entry_point_can_be_parallel_group(self) -> None:
        """Test that entry_point can be a parallel group."""
        from conductor.config.schema import ParallelGroup

        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="pg"),
            agents=[
                AgentDef(name="agent1", model="gpt-4", prompt="Task 1"),
                AgentDef(name="agent2", model="gpt-4", prompt="Task 2"),
            ],
            parallel=[ParallelGroup(name="pg", agents=["agent1", "agent2"])],
        )
        assert config.workflow.entry_point == "pg"


class TestForEachDef:
    """Tests for ForEachDef model."""

    def test_minimal_for_each(self) -> None:
        """Test creating a minimal for-each group."""

        for_each = ForEachDef(
            name="analyzers",
            type="for_each",
            source="finder.output.kpis",
            **{"as": "kpi"},  # Using dict unpacking to work with Python keyword
            agent=AgentDef(name="analyzer", model="gpt-4", prompt="Analyze {{ kpi }}"),
        )
        assert for_each.name == "analyzers"
        assert for_each.type == "for_each"
        assert for_each.source == "finder.output.kpis"
        assert for_each.as_ == "kpi"
        assert for_each.agent.name == "analyzer"
        assert for_each.max_concurrent == 10  # Default
        assert for_each.failure_mode == "fail_fast"  # Default
        assert for_each.key_by is None
        assert for_each.routes == []

    def test_for_each_with_all_fields(self) -> None:
        """Test for-each group with all fields populated."""

        for_each = ForEachDef(
            name="processors",
            description="Process all items",
            type="for_each",
            source="collector.output.items",
            **{"as": "item"},
            agent=AgentDef(
                name="processor",
                model="gpt-4",
                prompt="Process {{ item.id }}",
                output={"result": OutputField(type="string")},
            ),
            max_concurrent=5,
            failure_mode="continue_on_error",
            key_by="item.id",
            routes=[RouteDef(to="next_step")],
        )
        assert for_each.description == "Process all items"
        assert for_each.max_concurrent == 5
        assert for_each.failure_mode == "continue_on_error"
        assert for_each.key_by == "item.id"
        assert len(for_each.routes) == 1

    def test_as_field_alias_serialization(self) -> None:
        """Test that 'as' field uses proper Pydantic v2 aliases."""

        for_each = ForEachDef(
            name="test",
            type="for_each",
            source="a.output.b",
            **{"as": "item"},
            agent=AgentDef(name="a", model="gpt-4", prompt="test"),
        )

        # Check that serialization uses "as" not "as_"
        serialized = for_each.model_dump(by_alias=True)
        assert "as" in serialized
        assert "as_" not in serialized
        assert serialized["as"] == "item"

        # Check that internal access uses as_
        assert for_each.as_ == "item"

    def test_as_field_alias_deserialization(self) -> None:
        """Test that 'as' field can be loaded from YAML-like dict."""

        # Simulate YAML loading with "as" key
        data = {
            "name": "test",
            "type": "for_each",
            "source": "a.output.b",
            "as": "item",  # This should map to as_ field
            "agent": {
                "name": "a",
                "model": "gpt-4",
                "prompt": "test",
            },
        }

        for_each = ForEachDef(**data)
        assert for_each.as_ == "item"

    def test_reserved_loop_variable_names_rejected(self) -> None:
        """Test that reserved loop variable names are rejected."""

        reserved_names = ["workflow", "context", "output", "_index", "_key"]

        for reserved in reserved_names:
            with pytest.raises(ValidationError) as exc_info:
                ForEachDef(
                    name="test",
                    type="for_each",
                    source="a.output.b",
                    **{"as": reserved},
                    agent=AgentDef(name="a", model="gpt-4", prompt="test"),
                )
            assert "conflicts with reserved name" in str(exc_info.value).lower()
            assert reserved in str(exc_info.value)

    def test_invalid_loop_variable_identifier(self) -> None:
        """Test that invalid Python identifiers are rejected for loop variables."""

        invalid_identifiers = ["123item", "item-name", "item name", ""]

        for invalid in invalid_identifiers:
            with pytest.raises(ValidationError) as exc_info:
                ForEachDef(
                    name="test",
                    type="for_each",
                    source="a.output.b",
                    **{"as": invalid},
                    agent=AgentDef(name="a", model="gpt-4", prompt="test"),
                )
            assert "valid Python identifier" in str(exc_info.value)

    def test_valid_loop_variable_names(self) -> None:
        """Test that valid loop variable names are accepted."""

        valid_names = ["item", "kpi", "user", "data_point", "x", "i"]

        for valid_name in valid_names:
            for_each = ForEachDef(
                name="test",
                type="for_each",
                source="a.output.b",
                **{"as": valid_name},
                agent=AgentDef(name="a", model="gpt-4", prompt="test"),
            )
            assert for_each.as_ == valid_name

    def test_source_format_validation_valid(self) -> None:
        """Test that valid source formats are accepted."""

        valid_sources = [
            "finder.output.kpis",
            "agent1.output.items",
            "collector.output.data.nested.field",
        ]

        for source in valid_sources:
            for_each = ForEachDef(
                name="test",
                type="for_each",
                source=source,
                **{"as": "item"},
                agent=AgentDef(name="a", model="gpt-4", prompt="test"),
            )
            assert for_each.source == source

    def test_source_format_validation_invalid(self) -> None:
        """Test that invalid source formats are rejected."""

        # Too few parts (need at least 3)
        invalid_sources = [
            "finder",  # 1 part
            "finder.output",  # 2 parts
            "123invalid.output.field",  # Invalid identifier
        ]

        for invalid_source in invalid_sources:
            with pytest.raises(ValidationError) as exc_info:
                ForEachDef(
                    name="test",
                    type="for_each",
                    source=invalid_source,
                    **{"as": "item"},
                    agent=AgentDef(name="a", model="gpt-4", prompt="test"),
                )
            assert (
                "invalid source format" in str(exc_info.value).lower()
                or "not a valid identifier" in str(exc_info.value).lower()
            )

    def test_max_concurrent_validation(self) -> None:
        """Test max_concurrent bounds validation."""

        # Too low
        with pytest.raises(ValidationError) as exc_info:
            ForEachDef(
                name="test",
                type="for_each",
                source="a.output.b",
                **{"as": "item"},
                agent=AgentDef(name="a", model="gpt-4", prompt="test"),
                max_concurrent=0,
            )
        assert "must be at least 1" in str(exc_info.value)

        # Too high
        with pytest.raises(ValidationError) as exc_info:
            ForEachDef(
                name="test",
                type="for_each",
                source="a.output.b",
                **{"as": "item"},
                agent=AgentDef(name="a", model="gpt-4", prompt="test"),
                max_concurrent=101,
            )
        assert "cannot exceed 100" in str(exc_info.value)

        # Valid range
        for valid_max in [1, 10, 50, 100]:
            for_each = ForEachDef(
                name="test",
                type="for_each",
                source="a.output.b",
                **{"as": "item"},
                agent=AgentDef(name="a", model="gpt-4", prompt="test"),
                max_concurrent=valid_max,
            )
            assert for_each.max_concurrent == valid_max

    def test_failure_modes(self) -> None:
        """Test all failure modes are accepted."""

        failure_modes = ["fail_fast", "continue_on_error", "all_or_nothing"]

        for mode in failure_modes:
            for_each = ForEachDef(
                name="test",
                type="for_each",
                source="a.output.b",
                **{"as": "item"},
                agent=AgentDef(name="a", model="gpt-4", prompt="test"),
                failure_mode=mode,  # type: ignore
            )
            assert for_each.failure_mode == mode

    def test_invalid_failure_mode(self) -> None:
        """Test that invalid failure modes are rejected."""

        with pytest.raises(ValidationError):
            ForEachDef(
                name="test",
                type="for_each",
                source="a.output.b",
                **{"as": "item"},
                agent=AgentDef(name="a", model="gpt-4", prompt="test"),
                failure_mode="invalid_mode",  # type: ignore
            )


class TestWorkflowConfigWithForEach:
    """Tests for WorkflowConfig with for-each groups."""

    def test_workflow_with_for_each_group(self) -> None:
        """Test creating workflow with for-each group."""

        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="finder"),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find items",
                    routes=[RouteDef(to="processors")],
                ),
                AgentDef(name="processor", model="gpt-4", prompt="Process {{ item }}"),
            ],
            for_each=[
                ForEachDef(
                    name="processors",
                    type="for_each",
                    source="finder.output.items",
                    **{"as": "item"},
                    agent=AgentDef(name="processor", model="gpt-4", prompt="Process"),
                ),
            ],
        )
        assert len(config.for_each) == 1
        assert config.for_each[0].name == "processors"

    def test_entry_point_can_be_for_each_group(self) -> None:
        """Test that entry_point can be a for-each group."""

        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="processors"),
            agents=[
                AgentDef(name="finder", model="gpt-4", prompt="Find"),
                AgentDef(name="processor", model="gpt-4", prompt="Process {{ item }}"),
            ],
            for_each=[
                ForEachDef(
                    name="processors",
                    type="for_each",
                    source="workflow.input.items",
                    **{"as": "item"},
                    agent=AgentDef(name="processor", model="gpt-4", prompt="Process"),
                ),
            ],
        )
        assert config.workflow.entry_point == "processors"

    def test_route_to_for_each_group(self) -> None:
        """Test routing from agent to for-each group."""

        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="finder"),
            agents=[
                AgentDef(
                    name="finder", model="gpt-4", prompt="Find", routes=[RouteDef(to="processors")]
                ),
                AgentDef(name="processor", model="gpt-4", prompt="Process"),
            ],
            for_each=[
                ForEachDef(
                    name="processors",
                    type="for_each",
                    source="finder.output.items",
                    **{"as": "item"},
                    agent=AgentDef(name="processor", model="gpt-4", prompt="Process"),
                ),
            ],
        )
        assert config.agents[0].routes[0].to == "processors"

    def test_route_from_for_each_group(self) -> None:
        """Test routing from for-each group to agent."""

        config = WorkflowConfig(
            workflow=WorkflowDef(name="test", entry_point="processors"),
            agents=[
                AgentDef(name="finder", model="gpt-4", prompt="Find"),
                AgentDef(name="aggregator", model="gpt-4", prompt="Aggregate"),
            ],
            for_each=[
                ForEachDef(
                    name="processors",
                    type="for_each",
                    source="workflow.input.items",
                    **{"as": "item"},
                    agent=AgentDef(name="processor", model="gpt-4", prompt="Process"),
                    routes=[RouteDef(to="aggregator")],
                ),
            ],
        )
        assert config.for_each[0].routes[0].to == "aggregator"

    def test_invalid_route_target_from_for_each(self) -> None:
        """Test that invalid route targets from for-each groups are rejected."""

        with pytest.raises(ValidationError) as exc_info:
            WorkflowConfig(
                workflow=WorkflowDef(name="test", entry_point="processors"),
                agents=[
                    AgentDef(name="processor", model="gpt-4", prompt="Process"),
                ],
                for_each=[
                    ForEachDef(
                        name="processors",
                        type="for_each",
                        source="workflow.input.items",
                        **{"as": "item"},
                        agent=AgentDef(name="processor", model="gpt-4", prompt="Process"),
                        routes=[RouteDef(to="nonexistent")],
                    ),
                ],
            )
        assert "unknown target" in str(exc_info.value).lower()

    def test_nested_for_each_prohibited(self) -> None:
        """Test that nested for-each groups are prohibited."""

        # This should fail because inner_processors is a for-each group
        # and we're trying to reference it in another for-each group's agent
        with pytest.raises(ValidationError) as exc_info:
            WorkflowConfig(
                workflow=WorkflowDef(name="test", entry_point="outer"),
                agents=[
                    AgentDef(name="processor", model="gpt-4", prompt="Process"),
                ],
                for_each=[
                    ForEachDef(
                        name="outer",
                        type="for_each",
                        source="workflow.input.items",
                        **{"as": "item"},
                        # Trying to use another for-each group as the agent
                        agent=AgentDef(name="inner_processors", model="gpt-4", prompt="Process"),
                    ),
                    ForEachDef(
                        name="inner_processors",
                        type="for_each",
                        source="workflow.input.items",
                        **{"as": "inner"},
                        agent=AgentDef(name="processor", model="gpt-4", prompt="Process"),
                    ),
                ],
            )
        assert "nested for-each groups are not allowed" in str(exc_info.value).lower()
