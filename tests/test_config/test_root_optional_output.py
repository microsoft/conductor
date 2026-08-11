"""Tests for root-level optional output fields gate.

Requirement: root-level agent output fields must be required; optional fields
(required: false) are only allowed inside object properties.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conductor.config.loader import load_config, load_config_string
from conductor.config.validator import validate_workflow_config


class TestRootLevelOptionalOutputGate:
    """Root-level required: false must be rejected at load and validate time."""

    def _workflow_with_root_optional(self) -> str:
        return """
workflow:
  name: root-optional-test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: "Hello"
    output:
      answer:
        type: string
        required: false
    routes:
      - to: $end
"""

    def _workflow_with_nested_optional(self) -> str:
        return """
workflow:
  name: nested-optional-test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: "Hello"
    output:
      data:
        type: object
        properties:
          optional_child:
            type: string
            required: false
    routes:
      - to: $end
"""

    def _workflow_with_for_each_root_optional(self) -> str:
        return """
workflow:
  name: foreach-optional-test
  entry_point: finder

agents:
  - name: finder
    model: gpt-4
    prompt: "Find items"
    output:
      items:
        type: array
        items:
          type: string
    routes:
      - to: processors

for_each:
  - name: processors
    type: for_each
    source: finder.output.items
    as: item
    agent:
      name: processor
      model: gpt-4
      prompt: "Process {{ item }}"
      output:
        result:
          type: string
          required: false
    routes:
      - to: $end
"""

    def test_root_optional_rejected_by_load_config_string(self) -> None:
        """load_config_string must reject a root-level optional output field."""
        with pytest.raises(Exception) as exc_info:
            load_config_string(self._workflow_with_root_optional())

        message = str(exc_info.value)
        assert "Agent 'agent1' output field 'answer'" in message
        assert "root-level output fields cannot be optional" in message

    def test_root_optional_rejected_by_validate_command_load_path(self, tmp_path: Path) -> None:
        """load_config must reject a root-level optional output field,

        matching the validate command path.
        """
        workflow_file = tmp_path / "workflow.yaml"
        workflow_file.write_text(self._workflow_with_root_optional())
        with pytest.raises(Exception) as exc_info:
            load_config(str(workflow_file))

        message = str(exc_info.value)
        assert "Agent 'agent1' output field 'answer'" in message
        assert "root-level output fields cannot be optional" in message

    def test_nested_optional_allowed_by_load_config_string(self) -> None:
        """Optional fields inside object properties must load without error."""
        config = load_config_string(self._workflow_with_nested_optional())
        assert config.agents[0].output is not None
        properties = config.agents[0].output["data"].properties
        assert properties is not None
        assert properties["optional_child"].required is False

    def test_nested_optional_allowed_by_validate_workflow_config(self) -> None:
        """Optional fields inside object properties must validate without error."""
        config = load_config_string(self._workflow_with_nested_optional())
        # Should not raise
        validate_workflow_config(config)

    def test_for_each_inline_root_optional_rejected_by_load_config_string(self) -> None:
        """A root-level optional field on an inline for-each agent must be rejected."""
        with pytest.raises(Exception) as exc_info:
            load_config_string(self._workflow_with_for_each_root_optional())

        message = str(exc_info.value)
        assert "Agent 'processor' output field 'result'" in message
        assert "root-level output fields cannot be optional" in message

    def test_for_each_inline_root_optional_rejected_by_validate_command_load_path(
        self, tmp_path: Path
    ) -> None:
        """load_config must reject a root-level optional field on an inline for-each agent,

        matching the validate command path.
        """
        workflow_file = tmp_path / "workflow.yaml"
        workflow_file.write_text(self._workflow_with_for_each_root_optional())
        with pytest.raises(Exception) as exc_info:
            load_config(str(workflow_file))

        message = str(exc_info.value)
        assert "Agent 'processor' output field 'result'" in message
        assert "root-level output fields cannot be optional" in message
