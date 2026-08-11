"""Integration test for output field constraints through the engine.

Verifies that a workflow loaded from a YAML file with constrained output
fields surfaces a ValidationError when a mocked provider returns a payload
that violates those constraints.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from conductor.config.loader import load_config
from conductor.engine.workflow import WorkflowEngine
from conductor.exceptions import ValidationError
from conductor.providers.copilot import CopilotProvider


class TestOutputConstraintsIntegration:
    """End-to-end constraint validation via workflow YAML and engine execution."""

    @pytest.mark.asyncio
    async def test_violating_payload_raises_validation_error(self, tmp_path: Path) -> None:
        """A workflow with enum/range/length constraints on an agent output
        must raise ValidationError when the provider returns a value outside
        the allowed enum set."""
        workflow_file = tmp_path / "constrained.yaml"
        workflow_file.write_text(
            textwrap.dedent(
                """\
                workflow:
                  name: constrained-workflow
                  entry_point: checker

                agents:
                  - name: checker
                    model: gpt-4
                    prompt: Return a category and score.
                    output:
                      category:
                        type: string
                        enum:
                          - A
                          - B
                          - C
                      score:
                        type: number
                        minimum: 0
                        maximum: 100
                    routes:
                      - to: $end

                output:
                  result: "{{ checker.output.category }}"
                """
            )
        )

        config = load_config(workflow_file)

        def mock_handler(agent: Any, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
            # 'Z' is not in the allowed enum and must be rejected.
            return {"category": "Z", "score": 50}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        with pytest.raises(ValidationError) as exc_info:
            await engine.run({})

        assert "category" in str(exc_info.value)
        assert "must be one of" in str(exc_info.value)
