"""Unit tests for Pydantic AI -> Conductor usage mapping.

These tests verify that ``RunUsage`` from pydantic-ai is translated into the
``AgentOutput`` token fields (input, output, cache read/write, model, total) in
a way that Conductor's usage tracker can price correctly.
"""

from __future__ import annotations

from typing import Any

from pydantic_ai.usage import RunUsage

from conductor.providers._pydantic_ai.usage import (
    build_agent_output,
    run_usage_to_agent_output_fields,
)
from conductor.providers.base import AgentOutput


class TestRunUsageMapping:
    """Requirement: ``RunUsage`` fields map to ``AgentOutput`` token fields."""

    def test_full_usage_mapping(self) -> None:
        """All first-class token fields are preserved, including cache fields."""
        usage = RunUsage(
            requests=2,
            tool_calls=1,
            input_tokens=100,
            output_tokens=40,
            cache_read_tokens=30,
            cache_write_tokens=20,
        )
        fields = run_usage_to_agent_output_fields(usage, model="claude-3-5-sonnet")

        assert fields == {
            "model": "claude-3-5-sonnet",
            "tokens_used": 140,
            "input_tokens": 100,
            "output_tokens": 40,
            "cache_read_tokens": 30,
            "cache_write_tokens": 20,
        }

    def test_model_passthrough(self) -> None:
        """The model name is forwarded unchanged to the output fields."""
        usage = RunUsage(input_tokens=10, output_tokens=5)
        fields = run_usage_to_agent_output_fields(usage, model="custom-model-v1")

        assert fields["model"] == "custom-model-v1"

    def test_zero_usage_returns_none(self) -> None:
        """A zero-value usage object is treated as unavailable (no tokens)."""
        usage = RunUsage()
        fields = run_usage_to_agent_output_fields(usage, model="claude-3-5-sonnet")

        assert fields["tokens_used"] is None
        assert fields["input_tokens"] is None
        assert fields["output_tokens"] is None
        assert fields["cache_read_tokens"] is None
        assert fields["cache_write_tokens"] is None

    def test_none_usage_returns_none(self) -> None:
        """When no usage is supplied, all token fields are ``None``."""
        fields = run_usage_to_agent_output_fields(None, model="claude-3-5-sonnet")

        assert fields["model"] == "claude-3-5-sonnet"
        assert fields["tokens_used"] is None
        assert fields["input_tokens"] is None
        assert fields["output_tokens"] is None
        assert fields["cache_read_tokens"] is None
        assert fields["cache_write_tokens"] is None


class TestCacheDetailsFallback:
    """Requirement: Anthropic detail keys map to cache fields when top-level fields are zero."""

    def test_anthropic_cache_read_details(self) -> None:
        """``cache_read_input_tokens`` in ``details`` becomes ``cache_read_tokens``."""
        usage = RunUsage(
            input_tokens=200,
            output_tokens=50,
            details={"cache_read_input_tokens": 25},
        )
        fields = run_usage_to_agent_output_fields(usage)

        assert fields["cache_read_tokens"] == 25
        assert fields["cache_write_tokens"] is None

    def test_anthropic_cache_write_details(self) -> None:
        """``cache_creation_input_tokens`` in ``details`` becomes ``cache_write_tokens``."""
        usage = RunUsage(
            input_tokens=200,
            output_tokens=50,
            details={"cache_creation_input_tokens": 15},
        )
        fields = run_usage_to_agent_output_fields(usage)

        assert fields["cache_read_tokens"] is None
        assert fields["cache_write_tokens"] == 15

    def test_first_class_fields_take_precedence_over_details(self) -> None:
        """Non-zero top-level cache fields win over stale or different detail keys."""
        usage = RunUsage(
            input_tokens=200,
            output_tokens=50,
            cache_read_tokens=60,
            cache_write_tokens=40,
            details={
                "cache_read_input_tokens": 25,
                "cache_creation_input_tokens": 15,
                "cache_read_tokens": 5,
                "cache_write_tokens": 3,
            },
        )
        fields = run_usage_to_agent_output_fields(usage)

        assert fields["cache_read_tokens"] == 60
        assert fields["cache_write_tokens"] == 40

    def test_details_legacy_keys(self) -> None:
        """Legacy ``cache_read_tokens`` / ``cache_write_tokens`` detail keys are fallback."""
        usage = RunUsage(
            input_tokens=200,
            output_tokens=50,
            details={
                "cache_read_tokens": 12,
                "cache_write_tokens": 8,
            },
        )
        fields = run_usage_to_agent_output_fields(usage)

        assert fields["cache_read_tokens"] == 12
        assert fields["cache_write_tokens"] == 8


class TestBuildAgentOutput:
    """Requirement: ``build_agent_output`` assembles a full ``AgentOutput``."""

    def test_build_agent_output_full(self) -> None:
        """Content, raw response, usage, and model are combined into ``AgentOutput``."""
        usage = RunUsage(
            input_tokens=1000,
            output_tokens=200,
            cache_read_tokens=50,
            cache_write_tokens=10,
        )
        raw_response: dict[str, Any] = {"some": "sdk-response"}
        output = build_agent_output(
            {"answer": "42"},
            raw_response,
            usage=usage,
            model="claude-sonnet-5",
        )

        assert output == AgentOutput(
            content={"answer": "42"},
            raw_response=raw_response,
            model="claude-sonnet-5",
            tokens_used=1200,
            input_tokens=1000,
            output_tokens=200,
            cache_read_tokens=50,
            cache_write_tokens=10,
        )

    def test_build_agent_output_no_usage(self) -> None:
        """When usage is absent, token fields are ``None`` but content/model are set."""
        output = build_agent_output(
            {"answer": "unknown"},
            None,
            usage=None,
            model="fallback-model",
        )

        assert output.content == {"answer": "unknown"}
        assert output.raw_response is None
        assert output.model == "fallback-model"
        assert output.tokens_used is None
        assert output.input_tokens is None
        assert output.output_tokens is None
        assert output.cache_read_tokens is None
        assert output.cache_write_tokens is None
