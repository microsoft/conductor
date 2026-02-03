"""Tests for the config loader module."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from conductor.config.loader import (
    ConfigLoader,
    load_config,
    load_config_string,
    resolve_env_vars,
)
from conductor.exceptions import ConfigurationError


class TestResolveEnvVars:
    """Tests for environment variable resolution."""

    def test_resolve_simple_env_var(self) -> None:
        """Test resolving a simple environment variable."""
        with patch.dict(os.environ, {"MY_VAR": "hello"}):
            result = resolve_env_vars("Value: ${MY_VAR}")
            assert result == "Value: hello"

    def test_resolve_env_var_with_default(self) -> None:
        """Test resolving env var with default when var is not set."""
        # Make sure the var is not set
        with patch.dict(os.environ, {}, clear=True):
            result = resolve_env_vars("Value: ${UNSET_VAR:-default_value}")
            assert result == "Value: default_value"

    def test_resolve_env_var_ignores_default_when_set(self) -> None:
        """Test that default is ignored when env var is set."""
        with patch.dict(os.environ, {"MY_VAR": "actual_value"}):
            result = resolve_env_vars("Value: ${MY_VAR:-default}")
            assert result == "Value: actual_value"

    def test_resolve_multiple_env_vars(self) -> None:
        """Test resolving multiple env vars in one string."""
        with patch.dict(os.environ, {"VAR1": "one", "VAR2": "two"}):
            result = resolve_env_vars("${VAR1} and ${VAR2}")
            assert result == "one and two"

    def test_resolve_nested_env_vars(self) -> None:
        """Test recursive resolution of env vars."""
        with patch.dict(os.environ, {"OUTER": "${INNER}", "INNER": "resolved"}):
            result = resolve_env_vars("${OUTER}")
            assert result == "resolved"

    def test_resolve_missing_required_env_var_raises(self) -> None:
        """Test that missing required env var raises ConfigurationError."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ConfigurationError) as exc_info:
                resolve_env_vars("${REQUIRED_VAR}")
            assert "REQUIRED_VAR" in str(exc_info.value)
            assert "not set" in str(exc_info.value)

    def test_resolve_empty_default(self) -> None:
        """Test that empty default is allowed."""
        with patch.dict(os.environ, {}, clear=True):
            result = resolve_env_vars("${UNSET:-}")
            assert result == ""

    def test_resolve_no_env_vars(self) -> None:
        """Test that strings without env vars are unchanged."""
        result = resolve_env_vars("No env vars here")
        assert result == "No env vars here"

    def test_resolve_max_depth_exceeded(self) -> None:
        """Test that infinite recursion is prevented."""
        # Create a circular reference that would cause infinite recursion
        with patch.dict(os.environ, {"A": "${B}", "B": "${A}"}):
            with pytest.raises(ConfigurationError) as exc_info:
                resolve_env_vars("${A}")
            assert "Maximum recursion depth" in str(exc_info.value)


class TestConfigLoader:
    """Tests for the ConfigLoader class."""

    def test_load_valid_simple_workflow(self, fixtures_dir: Path) -> None:
        """Test loading a simple valid workflow."""
        loader = ConfigLoader()
        config = loader.load(fixtures_dir / "valid_simple.yaml")

        assert config.workflow.name == "simple-workflow"
        assert config.workflow.entry_point == "greeter"
        assert len(config.agents) == 1
        assert config.agents[0].name == "greeter"
        assert config.agents[0].model == "gpt-4"

    def test_load_valid_full_workflow(self, fixtures_dir: Path) -> None:
        """Test loading a workflow with all features."""
        with patch.dict(os.environ, {"MODEL": "claude-sonnet-4"}):
            loader = ConfigLoader()
            config = loader.load(fixtures_dir / "valid_full.yaml")

        assert config.workflow.name == "full-workflow"
        assert config.workflow.version == "1.0.0"
        assert config.workflow.runtime.provider == "copilot"
        assert config.workflow.limits.max_iterations == 20
        assert config.workflow.limits.timeout_seconds == 300
        assert config.workflow.context.mode == "accumulate"
        assert len(config.tools) == 3
        assert len(config.agents) == 4

        # Check env var was resolved
        planner = next(a for a in config.agents if a.name == "planner")
        assert planner.model == "claude-sonnet-4"

    def test_load_nonexistent_file_raises(self) -> None:
        """Test that loading a nonexistent file raises ConfigurationError."""
        loader = ConfigLoader()
        with pytest.raises(ConfigurationError) as exc_info:
            loader.load("/nonexistent/path/workflow.yaml")
        assert "not found" in str(exc_info.value)

    def test_load_directory_raises(self, tmp_path: Path) -> None:
        """Test that loading a directory raises ConfigurationError."""
        loader = ConfigLoader()
        with pytest.raises(ConfigurationError) as exc_info:
            loader.load(tmp_path)
        assert "not a file" in str(exc_info.value)

    def test_load_malformed_yaml_raises(self, fixtures_dir: Path) -> None:
        """Test that malformed YAML raises ConfigurationError with line info."""
        loader = ConfigLoader()
        with pytest.raises(ConfigurationError) as exc_info:
            loader.load(fixtures_dir / "invalid_malformed.yaml")
        assert "Invalid YAML syntax" in str(exc_info.value)

    def test_load_missing_entry_point_raises(self, fixtures_dir: Path) -> None:
        """Test that missing entry_point raises ConfigurationError."""
        loader = ConfigLoader()
        with pytest.raises(ConfigurationError) as exc_info:
            loader.load(fixtures_dir / "invalid_missing_entry.yaml")
        assert "entry_point" in str(exc_info.value)
        assert "does_not_exist" in str(exc_info.value)

    def test_load_bad_route_raises(self, fixtures_dir: Path) -> None:
        """Test that invalid route target raises ConfigurationError."""
        loader = ConfigLoader()
        with pytest.raises(ConfigurationError) as exc_info:
            loader.load(fixtures_dir / "invalid_bad_route.yaml")
        assert "unknown_agent" in str(exc_info.value)

    def test_load_env_vars_resolved(self, fixtures_dir: Path) -> None:
        """Test that environment variables are resolved."""
        with patch.dict(
            os.environ,
            {
                "PROVIDER": "openai-agents",
                "DEFAULT_MODEL": "gpt-4-turbo",
                "AGENT_MODEL": "gpt-3.5",
                "API_KEY": "secret123",
            },
        ):
            loader = ConfigLoader()
            config = loader.load(fixtures_dir / "valid_env_vars.yaml")

        assert config.workflow.runtime.provider == "openai-agents"
        assert config.workflow.runtime.default_model == "gpt-4-turbo"
        assert config.agents[0].model == "gpt-3.5"
        assert "secret123" in config.agents[0].prompt

    def test_load_missing_required_env_var_raises(self, fixtures_dir: Path) -> None:
        """Test that missing required env var raises ConfigurationError."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove the env var if it exists
            os.environ.pop("REQUIRED_API_KEY", None)

            loader = ConfigLoader()
            with pytest.raises(ConfigurationError) as exc_info:
                loader.load(fixtures_dir / "invalid_missing_env.yaml")
            assert "REQUIRED_API_KEY" in str(exc_info.value)

    def test_load_empty_file_raises(self, tmp_path: Path) -> None:
        """Test that an empty file raises ConfigurationError."""
        empty_file = tmp_path / "empty.yaml"
        empty_file.write_text("")

        loader = ConfigLoader()
        with pytest.raises(ConfigurationError) as exc_info:
            loader.load(empty_file)
        assert "Empty configuration" in str(exc_info.value)

    def test_load_gate_without_options_raises(self, fixtures_dir: Path) -> None:
        """Test that human_gate without options raises ConfigurationError."""
        loader = ConfigLoader()
        with pytest.raises(ConfigurationError) as exc_info:
            loader.load(fixtures_dir / "invalid_gate_no_options.yaml")
        assert "options" in str(exc_info.value).lower()


class TestLoadConfigFunctions:
    """Tests for the convenience functions."""

    def test_load_config_function(self, fixtures_dir: Path) -> None:
        """Test the load_config convenience function."""
        config = load_config(fixtures_dir / "valid_simple.yaml")
        assert config.workflow.name == "simple-workflow"

    def test_load_config_string_function(self) -> None:
        """Test the load_config_string convenience function."""
        yaml_content = """
workflow:
  name: string-test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: "Hello"
    routes:
      - to: $end
"""
        config = load_config_string(yaml_content)
        assert config.workflow.name == "string-test"
        assert len(config.agents) == 1


class TestConfigLoaderEdgeCases:
    """Tests for edge cases in config loading."""

    def test_load_workflow_with_defaults(self) -> None:
        """Test that defaults are applied correctly."""
        yaml_content = """
workflow:
  name: defaults-test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: "Hello"
    routes:
      - to: $end
"""
        config = load_config_string(yaml_content)

        # Check defaults
        assert config.workflow.runtime.provider == "copilot"
        assert config.workflow.limits.max_iterations == 10
        assert config.workflow.limits.timeout_seconds is None  # Unlimited by default
        assert config.workflow.context.mode == "accumulate"

    def test_load_workflow_with_empty_output(self) -> None:
        """Test loading workflow with empty output dict."""
        yaml_content = """
workflow:
  name: no-output
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: "Hello"
    routes:
      - to: $end

output: {}
"""
        config = load_config_string(yaml_content)
        assert config.output == {}

    def test_load_workflow_with_no_tools(self) -> None:
        """Test loading workflow without tools."""
        yaml_content = """
workflow:
  name: no-tools
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: "Hello"
    routes:
      - to: $end
"""
        config = load_config_string(yaml_content)
        assert config.tools == []

    def test_route_to_end(self) -> None:
        """Test that $end is a valid route target."""
        yaml_content = """
workflow:
  name: end-route
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: "Hello"
    routes:
      - to: $end
"""
        config = load_config_string(yaml_content)
        assert config.agents[0].routes[0].to == "$end"


class TestParallelGroupLoading:
    """Tests for loading workflows with parallel groups."""

    def test_load_workflow_with_parallel_group(self) -> None:
        """Test loading a workflow with a parallel group."""
        yaml_content = """
workflow:
  name: parallel-test
  entry_point: parallel_group

agents:
  - name: agent1
    model: gpt-4
    prompt: "Task 1"
  - name: agent2
    model: gpt-4
    prompt: "Task 2"

parallel:
  - name: parallel_group
    agents:
      - agent1
      - agent2
    failure_mode: fail_fast
"""
        config = load_config_string(yaml_content)
        assert len(config.parallel) == 1
        assert config.parallel[0].name == "parallel_group"
        assert len(config.parallel[0].agents) == 2
        assert config.parallel[0].failure_mode == "fail_fast"

    def test_load_parallel_group_with_description(self) -> None:
        """Test loading parallel group with description."""
        yaml_content = """
workflow:
  name: parallel-test
  entry_point: pg

agents:
  - name: agent1
    model: gpt-4
    prompt: "Task 1"
  - name: agent2
    model: gpt-4
    prompt: "Task 2"

parallel:
  - name: pg
    description: "Research agents running in parallel"
    agents:
      - agent1
      - agent2
"""
        config = load_config_string(yaml_content)
        assert config.parallel[0].description == "Research agents running in parallel"

    def test_load_multiple_parallel_groups(self) -> None:
        """Test loading workflow with multiple parallel groups."""
        yaml_content = """
workflow:
  name: multi-parallel
  entry_point: pg1

agents:
  - name: agent1
    model: gpt-4
    prompt: "Task 1"
  - name: agent2
    model: gpt-4
    prompt: "Task 2"
  - name: agent3
    model: gpt-4
    prompt: "Task 3"
  - name: agent4
    model: gpt-4
    prompt: "Task 4"

parallel:
  - name: pg1
    agents: [agent1, agent2]
  - name: pg2
    agents: [agent3, agent4]
"""
        config = load_config_string(yaml_content)
        assert len(config.parallel) == 2
        assert config.parallel[0].name == "pg1"
        assert config.parallel[1].name == "pg2"

    def test_load_parallel_group_all_failure_modes(self) -> None:
        """Test loading parallel groups with different failure modes."""
        for mode in ["fail_fast", "continue_on_error", "all_or_nothing"]:
            yaml_content = f"""
workflow:
  name: test
  entry_point: pg

agents:
  - name: agent1
    model: gpt-4
    prompt: "Task 1"
  - name: agent2
    model: gpt-4
    prompt: "Task 2"

parallel:
  - name: pg
    agents: [agent1, agent2]
    failure_mode: {mode}
"""
            config = load_config_string(yaml_content)
            assert config.parallel[0].failure_mode == mode

    def test_parallel_group_minimum_agents_error(self) -> None:
        """Test that parallel groups with fewer than 2 agents fail to load."""
        yaml_content = """
workflow:
  name: test
  entry_point: pg

agents:
  - name: agent1
    model: gpt-4
    prompt: "Task 1"

parallel:
  - name: pg
    agents: [agent1]
"""
        with pytest.raises(ConfigurationError) as exc_info:
            load_config_string(yaml_content)
        assert "at least 2 agents" in str(exc_info.value)

    def test_parallel_group_unknown_agent_error(self) -> None:
        """Test that parallel groups referencing unknown agents fail to load."""
        yaml_content = """
workflow:
  name: test
  entry_point: pg

agents:
  - name: agent1
    model: gpt-4
    prompt: "Task 1"

parallel:
  - name: pg
    agents: [agent1, unknown_agent]
"""
        with pytest.raises(ConfigurationError) as exc_info:
            load_config_string(yaml_content)
        assert "unknown agent" in str(exc_info.value).lower()

    def test_route_to_parallel_group(self) -> None:
        """Test that agents can route to parallel groups."""
        yaml_content = """
workflow:
  name: test
  entry_point: starter

agents:
  - name: starter
    model: gpt-4
    prompt: "Start"
    routes:
      - to: pg
  - name: agent1
    model: gpt-4
    prompt: "Task 1"
  - name: agent2
    model: gpt-4
    prompt: "Task 2"

parallel:
  - name: pg
    agents: [agent1, agent2]
"""
        config = load_config_string(yaml_content)
        assert config.agents[0].routes[0].to == "pg"
