"""Test verbose logging for for-each execution.

This module tests that verbose logging functions are called correctly
during for-each execution.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    ForEachDef,
    LimitsConfig,
    OutputField,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.providers.base import AgentOutput


class TestForEachVerboseLogging:
    """Tests for verbose logging during for-each execution."""

    @pytest.mark.asyncio
    async def test_verbose_logging_called_for_each_execution(self):
        """Test that verbose logging functions are called during for-each execution."""
        # Create a minimal workflow with for-each
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="verbose-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=50),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
            ],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "processors",
                        "type": "for_each",
                        "source": "finder.output.items",
                        "as": "item",
                        "max_concurrent": 2,
                        "failure_mode": "continue_on_error",
                        "agent": {
                            "name": "processor",
                            "model": "gpt-4",
                            "prompt": "Process {{ item }}",
                            "output": {"result": {"type": "string"}},
                        },
                        "routes": [{"to": "$end"}],
                    }
                ),
            ],
            output={
                "count": "{{ processors.count }}",
            },
        )

        provider = MagicMock()
        provider.execute = AsyncMock()

        # Setup: finder returns 3 items, all process successfully
        provider.execute.side_effect = [
            AgentOutput(
                content={"items": ["A", "B", "C"]},
                raw_response={},
                model="gpt-4",
                tokens_used=20,
            ),
            AgentOutput(content={"result": "ok-A"}, raw_response={}, model="gpt-4", tokens_used=10),
            AgentOutput(content={"result": "ok-B"}, raw_response={}, model="gpt-4", tokens_used=10),
            AgentOutput(content={"result": "ok-C"}, raw_response={}, model="gpt-4", tokens_used=10),
        ]

        # Mock the verbose logging functions
        mock_path_start = "conductor.engine.workflow._verbose_log_for_each_start"
        mock_path_complete = "conductor.engine.workflow._verbose_log_for_each_item_complete"
        mock_path_failed = "conductor.engine.workflow._verbose_log_for_each_item_failed"
        mock_path_summary = "conductor.engine.workflow._verbose_log_for_each_summary"

        with (
            patch(mock_path_start) as mock_start,
            patch(mock_path_complete) as mock_complete,
            patch(mock_path_failed) as mock_failed,
            patch(mock_path_summary) as mock_summary,
        ):
            engine = WorkflowEngine(config, provider)
            result = await engine.run({})

            # Verify verbose_log_for_each_start was called
            mock_start.assert_called_once_with(
                "processors",
                3,  # item_count
                2,  # max_concurrent
                "continue_on_error",
            )

            # Verify verbose_log_for_each_item_complete was called for each item
            assert mock_complete.call_count == 3
            # Check the calls were made with correct keys (0, 1, 2 as strings)
            item_keys = [call[0][0] for call in mock_complete.call_args_list]
            assert set(item_keys) == {"0", "1", "2"}

            # Verify verbose_log_for_each_item_failed was NOT called (no failures)
            mock_failed.assert_not_called()

            # Verify verbose_log_for_each_summary was called
            mock_summary.assert_called_once()
            call_args = mock_summary.call_args[0]
            assert call_args[0] == "processors"  # group_name
            assert call_args[1] == 3  # success_count
            assert call_args[2] == 0  # failure_count
            # call_args[3] is elapsed time, which we don't validate precisely

            # Verify the workflow completed successfully
            assert result["count"] == 3

    @pytest.mark.asyncio
    async def test_verbose_logging_failure_handling(self):
        """Test verbose logging when items fail."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="verbose-fail-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=50),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
            ],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "processors",
                        "type": "for_each",
                        "source": "finder.output.items",
                        "as": "item",
                        "max_concurrent": 10,
                        "failure_mode": "continue_on_error",
                        "agent": {
                            "name": "processor",
                            "model": "gpt-4",
                            "prompt": "Process {{ item }}",
                            "output": {"result": {"type": "string"}},
                        },
                        "routes": [{"to": "$end"}],
                    }
                ),
            ],
            output={
                "count": "{{ processors.count }}",
            },
        )

        provider = MagicMock()
        provider.execute = AsyncMock()

        # Setup: finder returns 3 items, second one fails
        provider.execute.side_effect = [
            AgentOutput(
                content={"items": ["A", "B", "C"]},
                raw_response={},
                model="gpt-4",
                tokens_used=20,
            ),
            AgentOutput(content={"result": "ok-A"}, raw_response={}, model="gpt-4", tokens_used=10),
            Exception("Processing failed"),
            AgentOutput(content={"result": "ok-C"}, raw_response={}, model="gpt-4", tokens_used=10),
        ]

        # Mock the verbose logging functions
        mock_path_start = "conductor.engine.workflow._verbose_log_for_each_start"
        mock_path_complete = "conductor.engine.workflow._verbose_log_for_each_item_complete"
        mock_path_failed = "conductor.engine.workflow._verbose_log_for_each_item_failed"
        mock_path_summary = "conductor.engine.workflow._verbose_log_for_each_summary"

        with (
            patch(mock_path_start) as _mock_start,
            patch(mock_path_complete) as mock_complete,
            patch(mock_path_failed) as mock_failed,
            patch(mock_path_summary) as mock_summary,
        ):
            engine = WorkflowEngine(config, provider)
            result = await engine.run({})

            # Verify verbose_log_for_each_item_complete was called twice (for A and C)
            assert mock_complete.call_count == 2

            # Verify verbose_log_for_each_item_failed was called once (for B)
            assert mock_failed.call_count == 1
            failed_call = mock_failed.call_args[0]
            assert failed_call[0] == "1"  # item_key for second item
            assert failed_call[2] == "Exception"  # exception_type
            assert "Processing failed" in failed_call[3]  # message

            # Verify summary shows 2 succeeded, 1 failed
            mock_summary.assert_called_once()
            call_args = mock_summary.call_args[0]
            assert call_args[1] == 2  # success_count
            assert call_args[2] == 1  # failure_count

            # Verify workflow completed (continue_on_error allows this)
            assert result["count"] == 3
