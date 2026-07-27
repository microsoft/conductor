"""Performance tests for Claude provider.

These tests measure orchestration overhead of the Claude provider against the
Pydantic AI TestModel seam. No real network calls are made.
"""

from __future__ import annotations

import asyncio
import gc
import time
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from conductor.config.schema import AgentDef, OutputField
from conductor.providers.claude import ClaudeProvider


def _build_structured_agent(
    model_cls: type[BaseModel], data: dict[str, Any]
) -> Agent[Any, Any]:
    """Build a Pydantic AI structured-output agent backed by TestModel."""
    return Agent(
        model=TestModel(custom_output_args=data),
        output_type=model_cls,
    )


@pytest.fixture(autouse=True)
def _ensure_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a dummy API key so ClaudeProvider construction succeeds."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


class TestClaudeProviderPerformance:
    """Performance tests for the Pydantic AI Claude provider seam."""

    @pytest.mark.performance
    def test_provider_initialization_latency(self) -> None:
        """Test that provider initialization completes within acceptable time."""
        start = time.perf_counter()
        ClaudeProvider(api_key="test-key")
        elapsed = time.perf_counter() - start

        # Initialization should be < 500ms (sync operation, may vary with system load)
        assert elapsed < 0.5, f"Initialization took {elapsed:.3f}s, expected < 0.5s"

    @pytest.mark.performance
    def test_retry_history_starts_empty(self) -> None:
        """Test that retry history is accessible and starts empty."""
        provider = ClaudeProvider(api_key="test-key")

        # Verify retry history starts empty
        retry_history = provider.get_retry_history()
        assert retry_history == [], "Retry history should start empty"

        # Verify retry config is accessible
        assert provider._retry_config is not None
        assert provider._retry_config.max_attempts >= 1

    @pytest.mark.performance
    @pytest.mark.asyncio
    async def test_concurrent_request_handling(self) -> None:
        """Test that provider can handle multiple concurrent requests."""

        class ResultModel(BaseModel):
            result: str

        provider = ClaudeProvider(api_key="test-key")
        agent = AgentDef(
            name="test",
            prompt="test",
            output={"result": OutputField(type="string")},
        )
        mock_agent = _build_structured_agent(ResultModel, {"result": "test"})

        # Run multiple concurrent requests
        start = time.perf_counter()
        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=mock_agent,
        ):
            tasks = [provider.execute(agent, {}, f"test prompt {i}") for i in range(5)]
            results = await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start

        # All should complete successfully
        assert len(results) == 5
        for result in results:
            assert result.content["result"] == "test"

        # With mocked responses, should be very fast
        assert elapsed < 1.0, f"Concurrent requests took {elapsed:.2f}s, expected < 1.0s"

    @pytest.mark.performance
    @pytest.mark.asyncio
    async def test_memory_efficiency(self) -> None:
        """Test that provider doesn't leak memory during repeated operations."""

        class ResultModel(BaseModel):
            result: str

        provider = ClaudeProvider(api_key="test-key")
        mock_agent = _build_structured_agent(ResultModel, {"result": "test"})

        agent = AgentDef(
            name="test",
            prompt="test",
            output={"result": OutputField(type="string")},
        )

        # Run many iterations
        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=mock_agent,
        ):
            for _i in range(100):
                await provider.execute(agent, {}, "test prompt")

        # Force garbage collection
        gc.collect()

        # Retry history should not grow unbounded
        retry_history = provider.get_retry_history()
        assert len(retry_history) < 1000, "Retry history growing unbounded"

    # Removed: test_parse_recovery_latency.
    # The legacy multi-turn JSON parse recovery loop was deleted by design;
    # Pydantic AI structured outputs natively enforce the output schema, so
    # there is no parse-recovery latency to measure. Structured output behavior
    # is covered in tests/test_providers/test_pydantic_ai_structured_output.py.
