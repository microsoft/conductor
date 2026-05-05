"""Tests for the dialog evaluator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from conductor.config.schema import AgentDef, DialogConfig
from conductor.engine.dialog_evaluator import DialogEvaluator


class TestDialogEvaluatorParsing:
    """Tests for the evaluator response parsing."""

    def setup_method(self) -> None:
        self.evaluator = DialogEvaluator()

    def test_parse_valid_trigger_response(self) -> None:
        """Test parsing a valid trigger=true response."""
        response = (
            '{"trigger": true, "reason": "Agent is uncertain", "question": "What do you mean?"}'
        )
        result = self.evaluator._parse_evaluation(response)
        assert result.trigger is True
        assert result.reason == "Agent is uncertain"
        assert result.question == "What do you mean?"

    def test_parse_valid_no_trigger_response(self) -> None:
        """Test parsing a valid trigger=false response."""
        response = '{"trigger": false, "reason": "Output is clear"}'
        result = self.evaluator._parse_evaluation(response)
        assert result.trigger is False
        assert result.reason == "Output is clear"

    def test_parse_markdown_wrapped_json(self) -> None:
        """Test parsing JSON wrapped in markdown code blocks."""
        response = '```json\n{"trigger": true, "reason": "test", "question": "What?"}\n```'
        result = self.evaluator._parse_evaluation(response)
        assert result.trigger is True
        assert result.question == "What?"

    def test_parse_invalid_json_returns_no_trigger(self) -> None:
        """Test that invalid JSON gracefully returns no trigger."""
        response = "This is not JSON at all"
        result = self.evaluator._parse_evaluation(response)
        assert result.trigger is False
        assert "Failed to parse" in result.reason

    def test_parse_empty_response(self) -> None:
        """Test that empty response returns no trigger."""
        result = self.evaluator._parse_evaluation("")
        assert result.trigger is False

    def test_parse_missing_fields_defaults(self) -> None:
        """Test that missing fields use defaults."""
        response = '{"trigger": true}'
        result = self.evaluator._parse_evaluation(response)
        assert result.trigger is True
        assert result.reason == ""
        assert result.question == ""

    def test_parse_unterminated_code_fence_keeps_last_line(self) -> None:
        """Unterminated ```json fences must not slice off the last JSON line."""
        # No closing ``` — the previous parser dropped the last line and failed.
        response = '```json\n{"trigger": true, "reason": "ok"}'
        result = self.evaluator._parse_evaluation(response)
        assert result.trigger is True
        assert result.reason == "ok"

    def test_parse_terminated_code_fence_strips_both(self) -> None:
        """Closing fence must still be stripped when present."""
        response = '```\n{"trigger": false, "reason": "clear"}\n```'
        result = self.evaluator._parse_evaluation(response)
        assert result.trigger is False
        assert result.reason == "clear"


class TestEvaluatorTruncation:
    """Tests for the agent-output truncation marker."""

    def test_truncate_below_limit_unchanged(self) -> None:
        from conductor.engine.dialog_evaluator import _truncate_for_evaluator

        text = "abc"
        assert _truncate_for_evaluator(text, limit=10) == "abc"

    def test_truncate_above_limit_appends_marker(self) -> None:
        from conductor.engine.dialog_evaluator import _truncate_for_evaluator

        text = "x" * 6000
        result = _truncate_for_evaluator(text, limit=4000)
        assert result.endswith("…[truncated]")
        assert len(result) <= 4000

    @pytest.mark.asyncio
    async def test_evaluate_truncates_large_output(self) -> None:
        """Large agent outputs should reach the evaluator with a marker."""
        evaluator = DialogEvaluator()
        agent = AgentDef(
            name="test",
            prompt="test",
            dialog=DialogConfig(trigger_prompt="Enter dialog if uncertain"),
        )
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(
            return_value='{"trigger": false, "reason": "clear"}'
        )

        # 5000+ char output to force truncation
        big_output = {"text": "x" * 5500}
        await evaluator.evaluate(agent, big_output, provider)

        call_args = provider.execute_dialog_turn.call_args
        user_message = call_args.kwargs["user_message"]
        assert "…[truncated]" in user_message


class TestDialogEvaluatorEvaluate:
    """Tests for the full evaluate() method."""

    def setup_method(self) -> None:
        self.evaluator = DialogEvaluator()

    @pytest.mark.asyncio
    async def test_no_dialog_config_returns_no_trigger(self) -> None:
        """Test that agents without dialog config never trigger."""
        agent = AgentDef(name="test", prompt="test")
        provider = MagicMock()
        result = await self.evaluator.evaluate(agent, {"result": "test"}, provider)
        assert result.trigger is False
        assert result.reason == "No dialog config"

    @pytest.mark.asyncio
    async def test_evaluator_calls_provider(self) -> None:
        """Test that the evaluator makes an LLM call via the provider."""
        agent = AgentDef(
            name="test",
            prompt="test",
            dialog=DialogConfig(trigger_prompt="Enter dialog if uncertain"),
        )
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(
            return_value=(
                '{"trigger": true, "reason": "Agent is uncertain", "question": "What do you need?"}'
            )
        )

        result = await self.evaluator.evaluate(
            agent, {"result": "I am uncertain about this."}, provider
        )
        assert result.trigger is True
        assert result.question == "What do you need?"
        provider.execute_dialog_turn.assert_called_once()

    @pytest.mark.asyncio
    async def test_evaluator_no_trigger(self) -> None:
        """Test evaluator correctly returns no trigger."""
        agent = AgentDef(
            name="test",
            prompt="test",
            dialog=DialogConfig(trigger_prompt="Enter dialog if uncertain"),
        )
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(
            return_value='{"trigger": false, "reason": "All clear"}'
        )

        result = await self.evaluator.evaluate(agent, {"result": "Clear output."}, provider)
        assert result.trigger is False
        provider.execute_dialog_turn.assert_called_once()

    @pytest.mark.asyncio
    async def test_evaluator_failure_returns_no_trigger(self) -> None:
        """Test that evaluator LLM failure gracefully returns no trigger."""
        agent = AgentDef(
            name="test",
            prompt="test",
            dialog=DialogConfig(trigger_prompt="Enter dialog if uncertain"),
        )
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(side_effect=Exception("API error"))

        result = await self.evaluator.evaluate(agent, {"result": "test"}, provider)
        assert result.trigger is False
        assert result.reason == "Evaluation failed"

    @pytest.mark.asyncio
    async def test_evaluator_prompt_includes_trigger_criteria(self) -> None:
        """Test that the evaluator prompt includes the trigger_prompt criteria."""
        agent = AgentDef(
            name="my_agent",
            prompt="test",
            dialog=DialogConfig(trigger_prompt="Trigger when confused"),
        )
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(
            return_value='{"trigger": false, "reason": "nope"}'
        )

        await self.evaluator.evaluate(agent, {"result": "hello"}, provider)

        # Verify the system prompt contains the trigger criteria
        call_args = provider.execute_dialog_turn.call_args
        assert "Trigger when confused" in call_args.kwargs["system_prompt"]
        assert "my_agent" in call_args.kwargs["user_message"]
