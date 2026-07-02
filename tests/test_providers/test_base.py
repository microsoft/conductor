"""Unit tests for the AgentProvider ABC and AgentOutput dataclass."""

import pytest

from conductor.providers.base import AgentOutput, match_model_id


class TestAgentOutput:
    """Tests for the AgentOutput dataclass."""

    def test_agent_output_creation(self) -> None:
        """Test creating an AgentOutput with all fields."""
        output = AgentOutput(
            content={"result": "test"},
            raw_response={"raw": "data"},
            tokens_used=100,
            model="gpt-4",
        )
        assert output.content == {"result": "test"}
        assert output.raw_response == {"raw": "data"}
        assert output.tokens_used == 100
        assert output.model == "gpt-4"

    def test_agent_output_minimal(self) -> None:
        """Test creating an AgentOutput with minimal required fields."""
        output = AgentOutput(
            content={"result": "test"},
            raw_response=None,
        )
        assert output.content == {"result": "test"}
        assert output.raw_response is None
        assert output.tokens_used is None
        assert output.input_tokens is None
        assert output.output_tokens is None
        assert output.cache_read_tokens is None
        assert output.cache_write_tokens is None
        assert output.model is None

    def test_agent_output_with_token_breakdown(self) -> None:
        """Test AgentOutput with detailed token breakdown."""
        output = AgentOutput(
            content={"result": "test"},
            raw_response={"raw": "data"},
            tokens_used=1500,
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=100,
            cache_write_tokens=50,
            model="claude-sonnet-4",
        )
        assert output.tokens_used == 1500
        assert output.input_tokens == 1000
        assert output.output_tokens == 500
        assert output.cache_read_tokens == 100
        assert output.cache_write_tokens == 50

    def test_agent_output_with_complex_content(self) -> None:
        """Test AgentOutput with nested content structure."""
        output = AgentOutput(
            content={
                "analysis": {
                    "score": 8.5,
                    "issues": ["minor", "cosmetic"],
                    "approved": True,
                }
            },
            raw_response={"id": "123"},
        )
        assert output.content["analysis"]["score"] == 8.5
        assert output.content["analysis"]["issues"] == ["minor", "cosmetic"]
        assert output.content["analysis"]["approved"] is True


class TestMatchModelId:
    """Unit tests for the alias-aware model ID matcher."""

    def test_exact_match(self) -> None:
        assert match_model_id("gpt-4o", ["gpt-4o", "gpt-4.1"]) == "gpt-4o"

    def test_returns_none_when_no_known_ids(self) -> None:
        assert match_model_id("gpt-4o", []) is None

    def test_returns_none_for_unrelated_name(self) -> None:
        assert match_model_id("totally-different", ["gpt-4o"]) is None

    def test_versioned_suffix_matches_base(self) -> None:
        # Requested name has a dated suffix; SDK lists the base name.
        assert (
            match_model_id("claude-3-5-sonnet-20241022", ["claude-3-5-sonnet"])
            == "claude-3-5-sonnet"
        )

    def test_base_matches_versioned_sdk_id(self) -> None:
        # Requested name is the base; SDK lists a dated/aliased variant.
        assert (
            match_model_id("claude-3-5-sonnet", ["claude-3-5-sonnet-20241022"])
            == "claude-3-5-sonnet-20241022"
        )

    def test_latest_alias_strips_and_matches(self) -> None:
        assert (
            match_model_id("claude-3-5-sonnet-latest", ["claude-3-5-sonnet-20241022"])
            == "claude-3-5-sonnet-20241022"
        )

    def test_preview_alias_strips_and_matches(self) -> None:
        assert match_model_id("gemini-3.1-pro-preview", ["gemini-3.1-pro"]) == "gemini-3.1-pro"

    def test_longest_match_wins(self) -> None:
        # "o1-mini-20240101" must match "o1-mini" (longer), not "o1".
        assert match_model_id("o1-mini-20240101", ["o1", "o1-mini"]) == "o1-mini"

    def test_boundary_check_prevents_cross_family_match(self) -> None:
        # "claude-opus-4.7" must NOT match "claude-opus-4" (different family).
        # No valid match -> None.
        assert match_model_id("claude-opus-4.7", ["claude-opus-4"]) is None

    def test_boundary_check_prevents_cross_family_with_suffix(self) -> None:
        assert match_model_id("claude-opus-4.7-high", ["claude-opus-4"]) is None

    def test_unknown_after_suffix_strip_returns_none(self) -> None:
        assert match_model_id("totally-different-latest", ["gpt-4o"]) is None


class TestGetModelPricingDefault:
    """The base AgentProvider.get_model_pricing default returns None (#265)."""

    @pytest.mark.asyncio
    async def test_base_default_returns_none(self) -> None:
        """Providers that don't override the hook fall through to the table."""
        from conductor.providers.base import AgentProvider

        class _Fake(AgentProvider, abstract=True):
            async def execute(self, *args: object, **kwargs: object) -> AgentOutput:
                raise NotImplementedError

            async def validate_connection(self) -> bool:
                return True

            async def close(self) -> None:
                return None

        assert await _Fake().get_model_pricing("any-model") is None
