"""Unit tests for the AgentProvider ABC and AgentOutput dataclass."""

from conductor.providers.base import AgentOutput


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
