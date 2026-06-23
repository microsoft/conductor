"""Tests for the Pydantic schema models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conductor.config.schema import (
    AgentDef,
    CheckpointConfig,
    ContextConfig,
    ForEachDef,
    GateOption,
    HooksConfig,
    InputDef,
    LimitsConfig,
    OutputField,
    ReasoningConfig,
    RouteDef,
    RuntimeConfig,
    ValidatorConfig,
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


class TestCheckpointConfig:
    """Tests for CheckpointConfig model (issue #244)."""

    def test_default_values_disabled(self) -> None:
        """Periodic checkpoints are off by default to preserve behavior."""
        config = CheckpointConfig()
        assert config.every_agent is False
        assert config.every_seconds is None
        assert config.keep_last == 5
        assert config.is_enabled is False

    def test_is_enabled_with_every_agent(self) -> None:
        assert CheckpointConfig(every_agent=True).is_enabled is True

    def test_is_enabled_with_every_seconds(self) -> None:
        assert CheckpointConfig(every_seconds=300).is_enabled is True

    def test_every_seconds_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            CheckpointConfig(every_seconds=0)

    def test_keep_last_bounds(self) -> None:
        with pytest.raises(ValidationError):
            CheckpointConfig(keep_last=0)
        with pytest.raises(ValidationError):
            CheckpointConfig(keep_last=101)
        assert CheckpointConfig(keep_last=100).keep_last == 100

    def test_extra_fields_forbidden(self) -> None:
        """Typos like every_second should be rejected, not silently ignored."""
        with pytest.raises(ValidationError):
            CheckpointConfig(every_second=1)  # type: ignore[call-arg]

    def test_runtime_config_default_checkpoint(self) -> None:
        """RuntimeConfig exposes a disabled checkpoint config by default."""
        runtime = RuntimeConfig()
        assert isinstance(runtime.checkpoint, CheckpointConfig)
        assert runtime.checkpoint.is_enabled is False

    def test_runtime_config_parses_checkpoint_block(self) -> None:
        runtime = RuntimeConfig(
            checkpoint={"every_agent": True, "every_seconds": 120, "keep_last": 3}
        )
        assert runtime.checkpoint.every_agent is True
        assert runtime.checkpoint.every_seconds == 120
        assert runtime.checkpoint.keep_last == 3
        assert runtime.checkpoint.is_enabled is True


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


class TestAgentDefMaxSessionSeconds:
    """Tests for max_session_seconds on AgentDef."""

    def test_default_is_none(self) -> None:
        """Test that max_session_seconds defaults to None."""
        agent = AgentDef(name="a", model="gpt-4", prompt="test")
        assert agent.max_session_seconds is None

    def test_accepts_valid_value(self) -> None:
        """Test that max_session_seconds accepts valid float values."""
        agent = AgentDef(name="a", model="gpt-4", prompt="test", max_session_seconds=60.0)
        assert agent.max_session_seconds == 60.0

    def test_accepts_integer_value(self) -> None:
        """Test that max_session_seconds accepts integer values."""
        agent = AgentDef(name="a", model="gpt-4", prompt="test", max_session_seconds=120)
        assert agent.max_session_seconds == 120.0

    def test_minimum_boundary(self) -> None:
        """Test that max_session_seconds accepts the minimum value of 1.0."""
        agent = AgentDef(name="a", model="gpt-4", prompt="test", max_session_seconds=1.0)
        assert agent.max_session_seconds == 1.0

    def test_rejects_zero(self) -> None:
        """Test that max_session_seconds rejects zero."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(name="a", model="gpt-4", prompt="test", max_session_seconds=0)
        assert "greater than or equal to 1" in str(exc_info.value)

    def test_rejects_negative(self) -> None:
        """Test that max_session_seconds rejects negative values."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(name="a", model="gpt-4", prompt="test", max_session_seconds=-5.0)
        assert "greater than or equal to 1" in str(exc_info.value)

    def test_rejected_on_script_agent(self) -> None:
        """Test that script agents cannot have max_session_seconds."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(
                name="s",
                type="script",
                command="echo hello",
                max_session_seconds=60.0,
            )
        assert "max_session_seconds" in str(exc_info.value)

    def test_allowed_on_regular_agent(self) -> None:
        """Test that regular agents can have max_session_seconds."""
        agent = AgentDef(
            name="a",
            type="agent",
            model="gpt-4",
            prompt="test",
            max_session_seconds=90.0,
        )
        assert agent.max_session_seconds == 90.0


class TestRuntimeConfig:
    """Tests for RuntimeConfig model."""

    def test_default_values(self) -> None:
        """Test default runtime configuration."""
        config = RuntimeConfig()
        assert config.provider.name == "copilot"
        assert not config.provider.has_custom_routing()
        assert config.default_model is None
        assert config.temperature is None
        assert config.max_tokens is None
        assert config.timeout is None

    def test_custom_provider(self) -> None:
        """Test custom provider setting."""
        config = RuntimeConfig(provider="openai-agents", default_model="gpt-4")
        assert config.provider.name == "openai-agents"
        assert config.default_model == "gpt-4"

    def test_invalid_provider_raises(self) -> None:
        """Test that invalid provider raises ValidationError."""
        with pytest.raises(ValidationError):
            RuntimeConfig(provider="invalid")  # type: ignore

    def test_claude_provider_with_temperature(self) -> None:
        """Test Claude provider with temperature setting."""
        config = RuntimeConfig(provider="claude", temperature=0.7)
        assert config.provider.name == "claude"
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
        assert config.provider.name == "claude"
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


class TestRuntimeConfigMaxSessionSeconds:
    """Tests for max_session_seconds on RuntimeConfig."""

    def test_default_is_none(self) -> None:
        """Test that max_session_seconds defaults to None."""
        config = RuntimeConfig()
        assert config.max_session_seconds is None

    def test_accepts_valid_value(self) -> None:
        """Test that max_session_seconds accepts valid float values."""
        config = RuntimeConfig(max_session_seconds=60.0)
        assert config.max_session_seconds == 60.0

    def test_accepts_integer_value(self) -> None:
        """Test that max_session_seconds accepts integer values."""
        config = RuntimeConfig(max_session_seconds=120)
        assert config.max_session_seconds == 120.0

    def test_minimum_boundary(self) -> None:
        """Test that max_session_seconds accepts the minimum value of 1.0."""
        config = RuntimeConfig(max_session_seconds=1.0)
        assert config.max_session_seconds == 1.0

    def test_rejects_zero(self) -> None:
        """Test that max_session_seconds rejects zero."""
        with pytest.raises(ValidationError) as exc_info:
            RuntimeConfig(max_session_seconds=0)
        assert "greater than or equal to 1" in str(exc_info.value)

    def test_rejects_negative(self) -> None:
        """Test that max_session_seconds rejects negative values."""
        with pytest.raises(ValidationError) as exc_info:
            RuntimeConfig(max_session_seconds=-10.0)
        assert "greater than or equal to 1" in str(exc_info.value)

    def test_serialization_excludes_when_none(self) -> None:
        """Test that max_session_seconds is excluded from serialization when None."""
        config = RuntimeConfig()
        serialized = config.model_dump(exclude_none=True)
        assert "max_session_seconds" not in serialized

    def test_serialization_includes_when_set(self) -> None:
        """Test that max_session_seconds is included in serialization when set."""
        config = RuntimeConfig(max_session_seconds=90.0)
        serialized = config.model_dump(exclude_none=True)
        assert "max_session_seconds" in serialized
        assert serialized["max_session_seconds"] == 90.0


class TestWorkflowDef:
    """Tests for WorkflowDef model."""

    def test_minimal_workflow(self) -> None:
        """Test minimal workflow definition."""
        workflow = WorkflowDef(name="test", entry_point="agent1")
        assert workflow.name == "test"
        assert workflow.entry_point == "agent1"
        assert workflow.runtime.provider.name == "copilot"

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

    def test_metadata_defaults_to_empty_dict(self) -> None:
        """Test that metadata defaults to empty dict when not specified."""
        workflow = WorkflowDef(name="test", entry_point="agent1")
        assert workflow.metadata == {}

    def test_metadata_accepts_arbitrary_keys(self) -> None:
        """Test that metadata accepts any key-value pairs."""
        workflow = WorkflowDef(
            name="test",
            entry_point="agent1",
            metadata={
                "tracker": "ado",
                "project_url": "https://dev.azure.com/org/Project",
                "work_item_id": "1814",
                "nested": {"key": "value"},
                "count": 42,
            },
        )
        assert workflow.metadata["tracker"] == "ado"
        assert workflow.metadata["project_url"] == "https://dev.azure.com/org/Project"
        assert workflow.metadata["work_item_id"] == "1814"
        assert workflow.metadata["nested"] == {"key": "value"}
        assert workflow.metadata["count"] == 42

    def test_metadata_does_not_affect_other_fields(self) -> None:
        """Test that metadata is independent from input, context, etc."""
        workflow = WorkflowDef(
            name="test",
            entry_point="agent1",
            input={"goal": InputDef(type="string")},
            metadata={"tracker": "ado"},
        )
        assert workflow.metadata == {"tracker": "ado"}
        assert "goal" in workflow.input
        # Metadata and input are completely separate
        assert "tracker" not in workflow.input
        assert "goal" not in workflow.metadata


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


class TestAgentDefReasoning:
    """Tests for the reasoning field on AgentDef."""

    @pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh"])
    def test_accepts_valid_effort(self, effort: str) -> None:
        """Test that AgentDef accepts each valid effort level."""
        agent = AgentDef(name="a", model="gpt-4", prompt="test", reasoning={"effort": effort})
        assert agent.reasoning is not None
        assert agent.reasoning.effort == effort

    @pytest.mark.parametrize("effort", ["none", "max", 42])
    def test_rejects_invalid_effort(self, effort: object) -> None:
        """Test that invalid effort values raise ValidationError."""
        with pytest.raises(ValidationError):
            AgentDef(
                name="a",
                model="gpt-4",
                prompt="test",
                reasoning={"effort": effort},  # type: ignore[arg-type]
            )

    def test_reasoning_defaults_to_none(self) -> None:
        """Test that reasoning defaults to None when omitted."""
        agent = AgentDef(name="x", model="gpt-4", prompt="test")
        assert agent.reasoning is None

    def test_explicit_reasoning_none_is_valid(self) -> None:
        """Test that explicitly passing reasoning=None is valid."""
        agent = AgentDef(name="x", model="gpt-4", prompt="test", reasoning=None)
        assert agent.reasoning is None

    def test_reasoning_accepts_reasoning_config_instance(self) -> None:
        """Test that a ReasoningConfig instance is accepted."""
        agent = AgentDef(
            name="a",
            model="gpt-4",
            prompt="test",
            reasoning=ReasoningConfig(effort="high"),
        )
        assert agent.reasoning is not None
        assert agent.reasoning.effort == "high"

    def test_default_agent_type_accepts_reasoning(self) -> None:
        """Test that default (None) agent type accepts reasoning."""
        agent = AgentDef(name="a", model="gpt-4", prompt="test", reasoning={"effort": "medium"})
        assert agent.type is None
        assert agent.reasoning is not None
        assert agent.reasoning.effort == "medium"

    def test_explicit_agent_type_accepts_reasoning(self) -> None:
        """Test that type='agent' accepts reasoning."""
        agent = AgentDef(
            name="a",
            type="agent",
            model="gpt-4",
            prompt="test",
            reasoning={"effort": "low"},
        )
        assert agent.reasoning is not None
        assert agent.reasoning.effort == "low"

    def test_human_gate_with_reasoning_raises(self) -> None:
        """Test that human_gate agents cannot have reasoning."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(
                name="gate1",
                type="human_gate",
                prompt="Choose:",
                options=[GateOption(label="Ok", value="ok", route="next")],
                reasoning={"effort": "low"},
            )
        assert "human_gate agents cannot have 'reasoning'" in str(exc_info.value)

    def test_script_with_reasoning_raises(self) -> None:
        """Test that script agents cannot have reasoning."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(
                name="s",
                type="script",
                command="echo hello",
                reasoning={"effort": "high"},
            )
        assert "script agents cannot have 'reasoning'" in str(exc_info.value)

    def test_workflow_with_reasoning_raises(self) -> None:
        """Test that workflow agents cannot have reasoning."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(
                name="w",
                type="workflow",
                workflow="./sub.yaml",
                reasoning={"effort": "medium"},
            )
        assert "workflow agents cannot have 'reasoning'" in str(exc_info.value)


class TestAgentDefValidator:
    """Tests for the validator field on AgentDef."""

    def test_accepts_minimal_validator(self) -> None:
        """Test that a default agent accepts a minimal validator block."""
        agent = AgentDef(
            name="a",
            model="gpt-4",
            prompt="test",
            validator={"criteria": "Output must cite a real source."},
        )
        assert agent.validator is not None
        assert agent.validator.criteria == "Output must cite a real source."
        assert agent.validator.model is None
        assert agent.validator.max_retries == 1

    def test_accepts_validator_config_instance(self) -> None:
        """Test that a ValidatorConfig instance is accepted."""
        agent = AgentDef(
            name="a",
            model="gpt-4",
            prompt="test",
            validator=ValidatorConfig(criteria="Check it", model="cheap-model", max_retries=0),
        )
        assert agent.validator is not None
        assert agent.validator.model == "cheap-model"
        assert agent.validator.max_retries == 0

    def test_validator_defaults_to_none(self) -> None:
        """Test that validator defaults to None when omitted."""
        agent = AgentDef(name="x", model="gpt-4", prompt="test")
        assert agent.validator is None

    def test_explicit_agent_type_accepts_validator(self) -> None:
        """Test that type='agent' accepts validator."""
        agent = AgentDef(
            name="a",
            type="agent",
            model="gpt-4",
            prompt="test",
            validator={"criteria": "Be correct"},
        )
        assert agent.validator is not None

    def test_empty_criteria_rejected(self) -> None:
        """Test that blank criteria is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(
                name="a",
                model="gpt-4",
                prompt="test",
                validator={"criteria": "   "},
            )
        assert "non-empty string" in str(exc_info.value)

    def test_missing_criteria_rejected(self) -> None:
        """Test that criteria is required."""
        with pytest.raises(ValidationError):
            AgentDef(
                name="a",
                model="gpt-4",
                prompt="test",
                validator={"model": "gpt-4"},  # type: ignore[arg-type]
            )

    @pytest.mark.parametrize("retries", [0, 1])
    def test_accepts_max_retries_zero_and_one(self, retries: int) -> None:
        """Test that max_retries of 0 and 1 are accepted."""
        agent = AgentDef(
            name="a",
            model="gpt-4",
            prompt="test",
            validator={"criteria": "Check", "max_retries": retries},
        )
        assert agent.validator is not None
        assert agent.validator.max_retries == retries

    @pytest.mark.parametrize("retries", [2, 3, 10])
    def test_rejects_max_retries_above_one(self, retries: int) -> None:
        """Test that max_retries > 1 is rejected (hard cap at 1)."""
        with pytest.raises(ValidationError):
            AgentDef(
                name="a",
                model="gpt-4",
                prompt="test",
                validator={"criteria": "Check", "max_retries": retries},
            )

    def test_rejects_negative_max_retries(self) -> None:
        """Test that negative max_retries is rejected."""
        with pytest.raises(ValidationError):
            AgentDef(
                name="a",
                model="gpt-4",
                prompt="test",
                validator={"criteria": "Check", "max_retries": -1},
            )

    def test_unknown_validator_field_rejected(self) -> None:
        """Test that a typo'd validator key is rejected (extra='forbid')."""
        with pytest.raises(ValidationError):
            AgentDef(
                name="a",
                model="gpt-4",
                prompt="test",
                validator={"criteria": "Check", "max_retreis": 1},  # typo
            )

    def test_human_gate_with_validator_raises(self) -> None:
        """Test that human_gate agents cannot have validator."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(
                name="gate1",
                type="human_gate",
                prompt="Choose:",
                options=[GateOption(label="Ok", value="ok", route="next")],
                validator={"criteria": "Check"},
            )
        assert "human_gate agents cannot have 'validator'" in str(exc_info.value)

    def test_script_with_validator_raises(self) -> None:
        """Test that script agents cannot have validator."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(
                name="s",
                type="script",
                command="echo hello",
                validator={"criteria": "Check"},
            )
        assert "script agents cannot have 'validator'" in str(exc_info.value)

    def test_workflow_with_validator_raises(self) -> None:
        """Test that workflow agents cannot have validator."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(
                name="w",
                type="workflow",
                workflow="./sub.yaml",
                validator={"criteria": "Check"},
            )
        assert "workflow agents cannot have 'validator'" in str(exc_info.value)

    def test_wait_with_validator_raises(self) -> None:
        """Test that wait agents cannot have validator."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(
                name="w",
                type="wait",
                duration="5s",
                validator={"criteria": "Check"},
            )
        assert "wait agents cannot have 'validator'" in str(exc_info.value)

    def test_set_with_validator_raises(self) -> None:
        """Test that set agents cannot have validator."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(
                name="s",
                type="set",
                value="{{ workflow.input.x }}",
                validator={"criteria": "Check"},
            )
        assert "set agents cannot have 'validator'" in str(exc_info.value)

    def test_terminate_with_validator_raises(self) -> None:
        """Test that terminate agents cannot have validator."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(
                name="t",
                type="terminate",
                status="success",
                reason="done",
                validator={"criteria": "Check"},
            )
        assert "terminate agents cannot have 'validator'" in str(exc_info.value)


class TestRuntimeConfigDefaultReasoningEffort:
    """Tests for default_reasoning_effort on RuntimeConfig."""

    def test_default_is_none(self) -> None:
        """Test that default_reasoning_effort defaults to None."""
        config = RuntimeConfig()
        assert config.default_reasoning_effort is None

    def test_explicit_none_is_valid(self) -> None:
        """Test that explicitly passing None is valid."""
        config = RuntimeConfig(default_reasoning_effort=None)
        assert config.default_reasoning_effort is None

    @pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh"])
    def test_accepts_valid_effort(self, effort: str) -> None:
        """Test that each valid effort level is accepted."""
        config = RuntimeConfig(default_reasoning_effort=effort)  # type: ignore[arg-type]
        assert config.default_reasoning_effort == effort

    @pytest.mark.parametrize("effort", ["none", "max", "extreme", 42, ""])
    def test_rejects_invalid_effort(self, effort: object) -> None:
        """Test that invalid effort values raise ValidationError."""
        with pytest.raises(ValidationError):
            RuntimeConfig(default_reasoning_effort=effort)  # type: ignore[arg-type]


class TestRetryPolicyMaxParseRecoveryAttempts:
    """Tests for RetryPolicy.max_parse_recovery_attempts field."""

    def test_max_parse_recovery_zero_valid(self) -> None:
        """max_parse_recovery_attempts: 0 disables parse recovery."""
        from conductor.config.schema import RetryPolicy

        policy = RetryPolicy(max_parse_recovery_attempts=0)
        assert policy.max_parse_recovery_attempts == 0

    def test_max_parse_recovery_ten_valid(self) -> None:
        """max_parse_recovery_attempts: 10 is the upper bound."""
        from conductor.config.schema import RetryPolicy

        policy = RetryPolicy(max_parse_recovery_attempts=10)
        assert policy.max_parse_recovery_attempts == 10

    def test_max_parse_recovery_negative_rejected(self) -> None:
        """max_parse_recovery_attempts: -1 is rejected."""
        from conductor.config.schema import RetryPolicy

        with pytest.raises(ValidationError):
            RetryPolicy(max_parse_recovery_attempts=-1)

    def test_max_parse_recovery_eleven_rejected(self) -> None:
        """max_parse_recovery_attempts: 11 exceeds the upper bound."""
        from conductor.config.schema import RetryPolicy

        with pytest.raises(ValidationError):
            RetryPolicy(max_parse_recovery_attempts=11)

    def test_max_parse_recovery_omitted_defaults_to_none(self) -> None:
        """Omitting max_parse_recovery_attempts defaults to None (provider default)."""
        from conductor.config.schema import RetryPolicy

        policy = RetryPolicy()
        assert policy.max_parse_recovery_attempts is None


class TestAgentDefContextTier:
    """Tests for the context_tier field on AgentDef."""

    @pytest.mark.parametrize("tier", ["default", "long_context"])
    def test_accepts_valid_tier(self, tier: str) -> None:
        """Test that AgentDef accepts each valid context tier."""
        agent = AgentDef(name="a", model="gpt-4", prompt="test", context_tier=tier)  # type: ignore[arg-type]
        assert agent.context_tier == tier

    @pytest.mark.parametrize("tier", ["1m", "huge", 42, ""])
    def test_rejects_invalid_tier(self, tier: object) -> None:
        """Test that invalid context_tier values raise ValidationError."""
        with pytest.raises(ValidationError):
            AgentDef(
                name="a",
                model="gpt-4",
                prompt="test",
                context_tier=tier,  # type: ignore[arg-type]
            )

    def test_context_tier_defaults_to_none(self) -> None:
        """Test that context_tier defaults to None when omitted."""
        agent = AgentDef(name="x", model="gpt-4", prompt="test")
        assert agent.context_tier is None

    def test_context_tier_composes_with_reasoning(self) -> None:
        """Test that context_tier and reasoning can be set together."""
        agent = AgentDef(
            name="a",
            model="claude-opus-4.8",
            prompt="test",
            context_tier="long_context",
            reasoning={"effort": "high"},
        )
        assert agent.context_tier == "long_context"
        assert agent.reasoning is not None
        assert agent.reasoning.effort == "high"

    def test_human_gate_with_context_tier_raises(self) -> None:
        """Test that human_gate agents cannot have context_tier."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(
                name="g",
                type="human_gate",
                prompt="Approve?",
                options=[GateOption(label="Ok", value="ok", route="next")],
                context_tier="long_context",
            )
        assert "human_gate agents cannot have 'context_tier'" in str(exc_info.value)

    def test_script_with_context_tier_raises(self) -> None:
        """Test that script agents cannot have context_tier."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(
                name="s",
                type="script",
                command="echo hi",
                context_tier="long_context",
            )
        assert "script agents cannot have 'context_tier'" in str(exc_info.value)

    def test_workflow_with_context_tier_raises(self) -> None:
        """Test that workflow agents cannot have context_tier."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(
                name="w",
                type="workflow",
                workflow="./sub.yaml",
                context_tier="long_context",
            )
        assert "workflow agents cannot have 'context_tier'" in str(exc_info.value)

    def test_wait_with_context_tier_raises(self) -> None:
        """Test that wait agents cannot have context_tier."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(name="w", type="wait", duration="1s", context_tier="long_context")
        assert "wait agents cannot have 'context_tier'" in str(exc_info.value)

    def test_set_with_context_tier_raises(self) -> None:
        """Test that set agents cannot have context_tier."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(name="s", type="set", value="42", context_tier="long_context")
        assert "set agents cannot have 'context_tier'" in str(exc_info.value)

    def test_terminate_with_context_tier_raises(self) -> None:
        """Test that terminate agents cannot have context_tier."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(
                name="t",
                type="terminate",
                status="success",
                reason="done",
                context_tier="long_context",
            )
        assert "terminate agents cannot have 'context_tier'" in str(exc_info.value)


class TestRuntimeConfigDefaultContextTier:
    """Tests for default_context_tier on RuntimeConfig."""

    def test_default_is_none(self) -> None:
        """Test that default_context_tier defaults to None."""
        config = RuntimeConfig()
        assert config.default_context_tier is None

    def test_explicit_none_is_valid(self) -> None:
        """Test that explicitly passing None is valid."""
        config = RuntimeConfig(default_context_tier=None)
        assert config.default_context_tier is None

    @pytest.mark.parametrize("tier", ["default", "long_context"])
    def test_accepts_valid_tier(self, tier: str) -> None:
        """Test that each valid context tier is accepted."""
        config = RuntimeConfig(default_context_tier=tier)  # type: ignore[arg-type]
        assert config.default_context_tier == tier

    @pytest.mark.parametrize("tier", ["1m", "huge", 42, ""])
    def test_rejects_invalid_tier(self, tier: object) -> None:
        """Test that invalid context tier values raise ValidationError."""
        with pytest.raises(ValidationError):
            RuntimeConfig(default_context_tier=tier)  # type: ignore[arg-type]


class TestExtraFieldsForbidden:
    """Tests that workflow models reject unknown fields.

    Regression tests for https://github.com/microsoft/conductor/issues/140 —
    misnesting `parallel:` or `for_each:` inside an `agents:` item used to
    silently drop the field, leaving a wrapper agent with no model/prompt
    that failed obscurely at the provider.
    """

    def test_agentdef_misnested_parallel_rejected(self) -> None:
        """An `agents:` item with a nested `parallel:` field is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef.model_validate(
                {
                    "name": "review_group",
                    "parallel": ["technical_reviewer", "readability_reviewer"],
                    "failure_mode": "fail_fast",
                    "routes": [{"to": "$end"}],
                }
            )
        errors = exc_info.value.errors()
        # The unknown field must be reported by Pydantic's extra_forbidden
        assert any(
            err["type"] == "extra_forbidden" and "parallel" in err["loc"] for err in errors
        ), f"Expected extra_forbidden error for 'parallel', got: {errors}"

    def test_agentdef_misnested_for_each_rejected(self) -> None:
        """An `agents:` item with a nested `for_each:` field is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef.model_validate(
                {
                    "name": "fanout",
                    "for_each": [{"name": "x", "type": "for_each"}],
                }
            )
        errors = exc_info.value.errors()
        assert any(
            err["type"] == "extra_forbidden" and "for_each" in err["loc"] for err in errors
        ), f"Expected extra_forbidden error for 'for_each', got: {errors}"

    def test_agentdef_typo_field_rejected(self) -> None:
        """A typo'd field on an agent is rejected (e.g., `prmpt` instead of `prompt`)."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef.model_validate(
                {
                    "name": "answerer",
                    "model": "claude-haiku-4.5",
                    "prmpt": "Answer the question.",
                }
            )
        errors = exc_info.value.errors()
        assert any(err["type"] == "extra_forbidden" and "prmpt" in err["loc"] for err in errors), (
            f"Expected extra_forbidden error for 'prmpt', got: {errors}"
        )

    def test_parallel_group_extra_field_rejected(self) -> None:
        """An unknown field on a ParallelGroup is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            from conductor.config.schema import ParallelGroup

            ParallelGroup.model_validate(
                {
                    "name": "g",
                    "agents": ["a", "b"],
                    "fail_fast": True,  # typo: actually `failure_mode`
                }
            )
        errors = exc_info.value.errors()
        assert any(err["type"] == "extra_forbidden" for err in errors)

    def test_workflow_config_top_level_extra_field_rejected(self) -> None:
        """An unknown top-level workflow field is rejected (catches `agent:` typo etc.)."""
        from conductor.config.schema import ParallelGroup  # noqa: F401

        with pytest.raises(ValidationError) as exc_info:
            WorkflowConfig.model_validate(
                {
                    "workflow": {"name": "x", "version": "1", "entry_point": "a"},
                    "agents": [{"name": "a", "model": "m", "prompt": "p"}],
                    "agnts": [],  # typo
                }
            )
        errors = exc_info.value.errors()
        assert any(err["type"] == "extra_forbidden" and "agnts" in err["loc"] for err in errors), (
            f"Expected extra_forbidden error for 'agnts', got: {errors}"
        )

    def test_workflowdef_typo_field_rejected(self) -> None:
        """A typo'd field on the `workflow:` block is rejected.

        Without `extra="forbid"` on WorkflowDef, typos like `entery_point:` or
        `limts:` are silently dropped, leaving the user's intent ignored.
        """
        with pytest.raises(ValidationError) as exc_info:
            WorkflowDef.model_validate(
                {
                    "name": "demo",
                    "entry_point": "a",
                    "entery_point": "b",  # typo
                }
            )
        errors = exc_info.value.errors()
        assert any(
            err["type"] == "extra_forbidden" and "entery_point" in err["loc"] for err in errors
        ), f"Expected extra_forbidden error for 'entery_point', got: {errors}"

    def test_routedef_typo_when_rejected(self) -> None:
        """A typo'd `when:` on a route is rejected.

        Without `extra="forbid"` on RouteDef, `whn:` was silently dropped and
        `route.when` defaulted to `None`, turning a conditional route into an
        unconditional one — a workflow-semantics bug nearly impossible to debug.
        """
        with pytest.raises(ValidationError) as exc_info:
            RouteDef.model_validate(
                {
                    "to": "next_agent",
                    "whn": "output.score > 5",  # typo for `when`
                }
            )
        errors = exc_info.value.errors()
        assert any(err["type"] == "extra_forbidden" and "whn" in err["loc"] for err in errors), (
            f"Expected extra_forbidden error for 'whn', got: {errors}"
        )


class TestTerminateAgent:
    """Tests for ``type: terminate`` step schema validation (issue #219).

    Terminate steps are terminal nodes that end the workflow with an explicit
    ``status`` and ``reason``. The schema must:

    - Accept ``status`` (``success`` | ``failed``), ``reason``, and optional
      ``output_template`` only when ``type == "terminate"``.
    - Reject those fields on any other step type (avoids silent misuse on a
      regular agent).
    - Reject every field that doesn't make sense for a terminal step (routes,
      tools, output, prompt, model, provider, etc.) so authoring errors fail
      fast.
    """

    def test_valid_terminate_success(self) -> None:
        a = AgentDef(name="ok", type="terminate", status="success", reason="done")
        assert a.type == "terminate"
        assert a.status == "success"
        assert a.reason == "done"
        assert a.output_template is None

    def test_valid_terminate_failed_with_output_template(self) -> None:
        a = AgentDef(
            name="abort",
            type="terminate",
            status="failed",
            reason="Refusing to run on unsafe input",
            output_template={"result": "aborted", "reason": "{{ precheck.output.reason }}"},
        )
        assert a.status == "failed"
        assert a.output_template == {
            "result": "aborted",
            "reason": "{{ precheck.output.reason }}",
        }

    def test_missing_status_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(name="x", type="terminate", reason="needed")
        assert "status" in str(exc_info.value).lower()

    def test_missing_reason_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(name="x", type="terminate", status="success")
        assert "reason" in str(exc_info.value).lower()

    def test_empty_reason_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(name="x", type="terminate", status="success", reason="   ")
        assert "reason" in str(exc_info.value).lower()

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentDef(name="x", type="terminate", status="maybe", reason="x")

    def test_routes_rejected_on_terminate(self) -> None:
        """Terminate ends the workflow; outbound routes would be unreachable."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(
                name="x",
                type="terminate",
                status="success",
                reason="r",
                routes=[RouteDef(to="$end")],
            )
        assert "routes" in str(exc_info.value).lower()

    def test_tools_rejected_on_terminate(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(name="x", type="terminate", status="success", reason="r", tools=["foo"])
        assert "tools" in str(exc_info.value).lower()

    def test_output_rejected_on_terminate(self) -> None:
        """`output:` is for agent schemas; terminate uses `output_template:` instead."""
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(
                name="x",
                type="terminate",
                status="success",
                reason="r",
                output={"k": OutputField(type="string")},
            )
        assert "output" in str(exc_info.value).lower()

    def test_prompt_rejected_on_terminate(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(name="x", type="terminate", status="success", reason="r", prompt="hi")
        assert "prompt" in str(exc_info.value).lower()

    def test_model_rejected_on_terminate(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(name="x", type="terminate", status="success", reason="r", model="claude")
        assert "model" in str(exc_info.value).lower()

    def test_command_rejected_on_terminate(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(name="x", type="terminate", status="success", reason="r", command="echo")
        assert "command" in str(exc_info.value).lower()

    def test_workflow_rejected_on_terminate(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AgentDef(
                name="x",
                type="terminate",
                status="success",
                reason="r",
                workflow="./sub.yaml",
            )
        assert "workflow" in str(exc_info.value).lower()

    @pytest.mark.parametrize("forbidden_field", ["status", "reason", "output_template"])
    def test_terminate_fields_rejected_on_regular_agent(self, forbidden_field: str) -> None:
        """`status`, `reason`, `output_template` only make sense on `type: terminate`.

        Without this guard, an author who forgot to add `type: terminate` would
        silently get a regular agent that ignores these fields entirely — a
        subtle bug that breaks the workflow without any error surfaced.
        """
        payload: dict[str, object] = {"name": "a"}
        if forbidden_field == "output_template":
            payload[forbidden_field] = {"k": "{{ a.output }}"}
        else:
            payload[forbidden_field] = "success" if forbidden_field == "status" else "r"
        with pytest.raises(ValidationError) as exc_info:
            AgentDef.model_validate(payload)
        assert forbidden_field in str(exc_info.value)

    @pytest.mark.parametrize("step_type", ["script", "workflow", "human_gate"])
    @pytest.mark.parametrize(
        "forbidden_field,field_value",
        [
            ("status", "success"),
            ("reason", "halt"),
            ("output_template", {"k": "{{ a.output }}"}),
        ],
    )
    def test_terminate_fields_rejected_on_other_step_types(
        self, step_type: str, forbidden_field: str, field_value: object
    ) -> None:
        """The terminate-only-fields guard must trip for every non-terminate type
        and every terminate-exclusive field — not just `status`.

        Earlier iteration of this test only varied ``step_type`` and asserted on
        ``status``. A bug in ``validate_agent_type`` that, say, rejected only
        ``status`` on ``script`` agents but silently accepted ``reason`` and
        ``output_template`` would have slipped through. Cross-product the
        parametrisation so every (step_type, terminate-field) pair is exercised.
        """
        payload: dict[str, object] = {"name": "a", "type": step_type}
        if step_type == "script":
            payload["command"] = "echo"
        elif step_type == "workflow":
            payload["workflow"] = "./sub.yaml"
        elif step_type == "human_gate":
            payload["prompt"] = "Pick"
            payload["options"] = [GateOption(value="x", label="X", route="$end")]
        payload[forbidden_field] = field_value
        with pytest.raises(ValidationError) as exc_info:
            AgentDef.model_validate(payload)
        assert forbidden_field in str(exc_info.value)

    def test_input_allowed_on_terminate(self) -> None:
        """Terminate steps may declare context inputs to drive Jinja rendering."""
        a = AgentDef(
            name="x",
            type="terminate",
            status="success",
            reason="{{ precheck.output.reason }}",
            input=["precheck.output"],
        )
        assert a.input == ["precheck.output"]


class TestScriptStdinField:
    """The `stdin` field is exclusive to `type: script` (issue #18)."""

    def test_stdin_accepted_on_script(self) -> None:
        """A script step may declare a stdin payload template."""
        agent = AgentDef(
            name="s",
            type="script",
            command="cat",
            stdin="{{ upstream.output.evaluations | tojson }}",
        )
        assert agent.stdin == "{{ upstream.output.evaluations | tojson }}"

    def test_stdin_empty_string_accepted_on_script(self) -> None:
        """An explicit empty stdin is valid (pipes immediate EOF), distinct from omission."""
        agent = AgentDef(name="s", type="script", command="cat", stdin="")
        assert agent.stdin == ""

    def test_stdin_defaults_to_none(self) -> None:
        """Omitting stdin leaves it None (legacy inherit-stdin behavior)."""
        agent = AgentDef(name="s", type="script", command="echo")
        assert agent.stdin is None

    @pytest.mark.parametrize(
        "step_type",
        ["agent", "human_gate", "set", "wait", "terminate", "workflow"],
    )
    def test_stdin_rejected_on_non_script_types(self, step_type: str) -> None:
        """The script-exclusive guard trips for every non-script step type."""
        payload: dict[str, object] = {"name": "a", "type": step_type, "stdin": "data"}
        if step_type == "human_gate":
            payload["prompt"] = "Pick"
            payload["options"] = [GateOption(value="x", label="X", route="$end")]
        elif step_type == "set":
            payload["value"] = "{{ 1 }}"
        elif step_type == "wait":
            payload["duration"] = "1s"
        elif step_type == "terminate":
            payload["status"] = "success"
            payload["reason"] = "r"
        elif step_type == "workflow":
            payload["workflow"] = "./sub.yaml"
        with pytest.raises(ValidationError) as exc_info:
            AgentDef.model_validate(payload)
        message = str(exc_info.value)
        assert "stdin" in message
        assert "only 'script' agents support this field" in message
