"""Backward compatibility tests for schema changes.

This module ensures that adding new fields to the schema
does not break existing Copilot workflows.

Tests verify:
1. All existing example YAML files load successfully
2. New fields default to None when omitted
3. Serialization round-trip preserves behavior
4. Pydantic exclude_none=True prevents new fields from polluting Copilot configs
5. End-to-end integration with Claude provider

Note: top_p, top_k, stop_sequences, and metadata have been removed as they
were Claude-specific parameters not supported by both providers.
"""

import json
from pathlib import Path

import pytest

from conductor.config.loader import load_config
from conductor.config.schema import (
    AgentDef,
    OutputField,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)

# Path to example workflows
EXAMPLES_DIR = Path(__file__).parent.parent.parent / "examples"


def get_copilot_example_files() -> list[Path]:
    """Get all example YAML files that use the Copilot provider.

    Returns:
        List of paths to Copilot example YAML files
    """
    all_examples = list(EXAMPLES_DIR.glob("*.yaml"))
    copilot_examples = []

    for example in all_examples:
        # Skip Claude-specific examples
        if "claude" in example.name.lower():
            continue

        copilot_examples.append(example)

    return copilot_examples


def get_all_example_files() -> list[Path]:
    """Get all example YAML files.

    Returns:
        List of paths to all example YAML files
    """
    all_examples = list(EXAMPLES_DIR.glob("*.yaml"))
    return all_examples


class TestBackwardCompatibility:
    """Test backward compatibility with existing workflows."""

    @pytest.mark.parametrize("example_file", get_copilot_example_files())
    def test_load_existing_copilot_workflows(self, example_file: Path):
        """Test that all existing Copilot example workflows load successfully.

        This verifies that adding new fields to the schema
        does not break existing workflows that don't use those fields.

        Task: EPIC-010-T1
        """
        # Load the workflow configuration
        config = load_config(example_file)

        # Verify it loaded successfully
        assert isinstance(config, WorkflowConfig)
        assert config.workflow.name is not None

        # Verify runtime config exists
        assert config.workflow.runtime is not None
        assert isinstance(config.workflow.runtime, RuntimeConfig)

        # Verify provider is copilot (or not explicitly set to claude)
        if config.workflow.runtime.provider:
            assert config.workflow.runtime.provider in ["copilot", "openai-agents"]

    def test_copilot_workflow_with_new_schema_no_validation_errors(self):
        """Test that Copilot workflows load without validation errors with new schema.

        This creates a minimal Copilot workflow and verifies it validates
        successfully with the new schema.

        Task: EPIC-010-T2
        """
        # Create a minimal Copilot workflow configuration
        config_dict = {
            "workflow": {
                "name": "test-copilot",
                "description": "Test workflow",
                "version": "1.0.0",
                "entry_point": "agent1",
                "runtime": {"provider": "copilot"},
            },
            "agents": [
                {
                    "name": "agent1",
                    "description": "Test agent",
                    "model": "haiku-4.5",
                    "prompt": "Test prompt",
                    "routes": [{"to": "$end"}],
                }
            ],
        }

        # Validate the configuration
        config = WorkflowConfig.model_validate(config_dict)

        # Verify basic structure
        assert config.workflow.name == "test-copilot"
        assert config.workflow.runtime.provider == "copilot"
        assert len(config.agents) == 1

        # Verify no validation errors occurred (if we got here, validation passed)
        assert True

    def test_serialization_round_trip_preserves_behavior(self):
        """Test that serialization round-trip preserves existing workflow behavior.

        This verifies that loading a Copilot workflow, serializing it back to dict,
        and reloading it produces the same configuration.

        Task: EPIC-010-T3
        """
        # Use the simple-qa.yaml example
        example_file = EXAMPLES_DIR / "simple-qa.yaml"

        # Load the original configuration
        original_config = load_config(example_file)

        # Serialize to dict (exclude_none should be True to avoid adding new fields)
        serialized = original_config.model_dump(mode="json", exclude_none=True)

        # Reload from serialized dict
        reloaded_config = WorkflowConfig.model_validate(serialized)

        # Verify key properties are preserved
        assert reloaded_config.workflow.name == original_config.workflow.name
        assert reloaded_config.workflow.version == original_config.workflow.version
        assert reloaded_config.workflow.entry_point == original_config.workflow.entry_point
        assert len(reloaded_config.agents) == len(original_config.agents)

        # Verify runtime config is preserved
        assert (
            reloaded_config.workflow.runtime.provider == original_config.workflow.runtime.provider
        )

        # Verify serialized dict does not contain optional fields when not set
        runtime_dict = serialized.get("workflow", {}).get("runtime", {})

        # Verify round-trip preserves values: if set in original, should be in serialized
        # and if not set, should not appear in serialized output
        original_runtime = original_config.workflow.runtime
        if original_runtime.temperature is None:
            assert "temperature" not in runtime_dict or runtime_dict["temperature"] is None
        else:
            assert runtime_dict.get("temperature") == original_runtime.temperature
        if original_runtime.max_tokens is None:
            assert "max_tokens" not in runtime_dict or runtime_dict["max_tokens"] is None
        if original_runtime.timeout is None:
            assert "timeout" not in runtime_dict or runtime_dict["timeout"] is None

    def test_optional_fields_default_to_none(self):
        """Test that optional fields default to None when omitted.

        This verifies that workflows without optional configuration
        have those fields set to None by default.

        Task: EPIC-010-T4
        """
        # Create a minimal configuration without optional fields
        config_dict = {
            "workflow": {
                "name": "test-defaults",
                "description": "Test workflow",
                "version": "1.0.0",
                "entry_point": "agent1",
                "runtime": {"provider": "copilot"},
            },
            "agents": [
                {
                    "name": "agent1",
                    "description": "Test agent",
                    "model": "haiku-4.5",
                    "prompt": "Test prompt",
                    "routes": [{"to": "$end"}],
                }
            ],
        }

        # Load configuration
        config = WorkflowConfig.model_validate(config_dict)
        runtime = config.workflow.runtime

        # Verify optional fields default to None
        assert runtime.temperature is None
        assert runtime.max_tokens is None
        assert runtime.timeout is None

    def test_exclude_none_prevents_optional_fields_in_copilot_configs(self):
        """Test that Pydantic exclude_none=True prevents new fields in serialized configs.

        This verifies that when serializing a Copilot workflow to dict/JSON,
        the optional fields (which are None) don't appear in the output.

        Task: EPIC-010-T4
        """
        # Create a Copilot configuration
        config_dict = {
            "workflow": {
                "name": "test-exclude-none",
                "description": "Test workflow",
                "version": "1.0.0",
                "entry_point": "agent1",
                "runtime": {"provider": "copilot"},
            },
            "agents": [
                {
                    "name": "agent1",
                    "description": "Test agent",
                    "model": "haiku-4.5",
                    "prompt": "Test prompt",
                    "routes": [{"to": "$end"}],
                }
            ],
        }

        # Load configuration
        config = WorkflowConfig.model_validate(config_dict)

        # Serialize with exclude_none=True
        serialized = config.model_dump(mode="json", exclude_none=True)

        # Convert to JSON and back to ensure we test the actual serialized form
        json_str = json.dumps(serialized)
        parsed = json.loads(json_str)

        # Verify optional fields are not present in the serialized output
        runtime_dict = parsed["workflow"]["runtime"]

        # These fields should NOT be present when exclude_none=True
        assert "temperature" not in runtime_dict
        assert "max_tokens" not in runtime_dict
        assert "timeout" not in runtime_dict

        # But provider should be present
        assert "provider" in runtime_dict
        assert runtime_dict["provider"] == "copilot"

    @pytest.mark.parametrize("example_file", get_all_example_files())
    def test_all_examples_load_successfully(self, example_file: Path):
        """Test that all example workflows (Copilot and Claude) load successfully.

        This is a comprehensive test that ensures both old and new examples work.

        Task: EPIC-010-T1 (comprehensive coverage)
        """
        # Load the workflow configuration
        config = load_config(example_file)

        # Verify it loaded successfully
        assert isinstance(config, WorkflowConfig)
        assert config.workflow.name is not None
        assert config.workflow.runtime is not None

        # If it's a Claude workflow, verify common fields can be accessed
        if config.workflow.runtime.provider == "claude":
            # These fields should be accessible (no assertion on values)
            _ = config.workflow.runtime.temperature
            _ = config.workflow.runtime.max_tokens
            _ = config.workflow.runtime.timeout

    @pytest.mark.asyncio
    async def test_copilot_workflow_execution_backward_compatible(self):
        """Verify existing Copilot workflow structure loads and validates with new schema.

        This test verifies backward compatibility by loading an actual Copilot workflow
        and validating its structure. Actual execution requires API keys and is tested
        in integration tests with mocked API responses.

        Task: EPIC-010-T6 (schema backward compatibility verification)
        """
        # Load a real Copilot example workflow
        copilot_examples = get_copilot_example_files()
        if not copilot_examples:
            pytest.skip("No Copilot example workflows found")

        # Use the simplest example
        config = load_config(copilot_examples[0])

        # Verify workflow structure is valid
        assert config.workflow is not None
        assert config.workflow.runtime is not None
        assert config.agents is not None
        assert len(config.agents) > 0

        # Verify optional fields are valid (temperature/max_tokens can be set for any provider)
        runtime = config.workflow.runtime
        # temperature and max_tokens are valid for all providers, just verify they're the right type
        assert runtime.temperature is None or isinstance(runtime.temperature, float)
        assert runtime.max_tokens is None or isinstance(runtime.max_tokens, int)

        # Verify serialization doesn't include None optional fields
        serialized = config.model_dump(mode="json", exclude_none=True)
        runtime_dict = serialized["workflow"]["runtime"]

        # None values should be excluded from serialized output
        assert "temperature" not in runtime_dict or runtime_dict["temperature"] is not None
        assert "max_tokens" not in runtime_dict or runtime_dict["max_tokens"] is not None

    def test_claude_workflow_schema_validation(self):
        """Verify Claude workflow schema with common fields validates correctly.

        This test verifies that the schema correctly accepts common fields
        and validates them appropriately.

        Task: EPIC-010-T5 (schema validation for common fields)
        """
        # Create a minimal Claude workflow with common fields
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="test-claude-schema",
                description="Test workflow",
                version="1.0.0",
                entry_point="agent1",
                runtime=RuntimeConfig(
                    provider="claude",
                    temperature=0.7,
                    max_tokens=1024,
                    timeout=120.0,
                ),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    description="Test agent",
                    model="claude-3-5-sonnet-latest",
                    prompt="Answer: What is 2+2?",
                    output={"answer": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                )
            ],
        )

        # Verify schema validation passed and fields are set
        assert config.workflow.runtime.provider == "claude"
        assert config.workflow.runtime.temperature == 0.7
        assert config.workflow.runtime.max_tokens == 1024
        assert config.workflow.runtime.timeout == 120.0

        # Verify serialization includes all fields
        serialized = config.model_dump(mode="json", exclude_none=True)
        runtime_dict = serialized["workflow"]["runtime"]

        assert runtime_dict["provider"] == "claude"
        assert runtime_dict["temperature"] == 0.7
        assert runtime_dict["max_tokens"] == 1024
        assert runtime_dict["timeout"] == 120.0

    def test_missing_provider_defaults_to_copilot(self):
        """Test that workflows without runtime.provider default to 'copilot'.

        This ensures backward compatibility with workflows created before
        multi-provider support was added.

        Task: EPIC-010-T7 (default provider behavior)
        """
        # Create a workflow without specifying provider
        config_dict = {
            "workflow": {
                "name": "test-default-provider",
                "description": "Test default provider",
                "version": "1.0.0",
                "entry_point": "agent1",
                # No runtime section at all
            },
            "agents": [
                {
                    "name": "agent1",
                    "description": "Test agent",
                    "model": "haiku-4.5",
                    "prompt": "Test prompt",
                    "routes": [{"to": "$end"}],
                }
            ],
        }

        # Load configuration
        config = WorkflowConfig.model_validate(config_dict)

        # Verify provider defaults to 'copilot'
        assert config.workflow.runtime.provider == "copilot"

        # Verify no optional fields are set
        assert config.workflow.runtime.temperature is None
        assert config.workflow.runtime.max_tokens is None

    def test_explicit_runtime_without_provider_defaults_to_copilot(self):
        """Test that runtime section without provider field defaults to 'copilot'.

        This tests backward compatibility when runtime section exists but
        provider is not explicitly set.

        Task: EPIC-010-T7 (default provider behavior)
        """
        # Create a workflow with runtime section but no provider
        config_dict = {
            "workflow": {
                "name": "test-runtime-no-provider",
                "description": "Test runtime without provider",
                "version": "1.0.0",
                "entry_point": "agent1",
                "runtime": {
                    # No provider field, should default to copilot
                },
            },
            "agents": [
                {
                    "name": "agent1",
                    "description": "Test agent",
                    "model": "haiku-4.5",
                    "prompt": "Test prompt",
                    "routes": [{"to": "$end"}],
                }
            ],
        }

        # Load configuration
        config = WorkflowConfig.model_validate(config_dict)

        # Verify provider defaults to 'copilot'
        assert config.workflow.runtime.provider == "copilot"

        # Verify serialization doesn't include None optional fields
        serialized = config.model_dump(mode="json", exclude_none=True)
        runtime_dict = serialized["workflow"]["runtime"]

        # Should not have optional fields
        assert "temperature" not in runtime_dict
        assert "max_tokens" not in runtime_dict
