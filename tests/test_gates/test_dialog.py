"""Tests for the dialog handler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor.config.schema import AgentDef, DialogConfig
from conductor.gates.dialog import DialogHandler, DialogResult


class TestDialogHandlerSkip:
    """Tests for dialog handler skip behavior."""

    @pytest.mark.asyncio
    async def test_skip_dialogs_returns_declined(self) -> None:
        """Test that skip_dialogs=True auto-declines."""
        handler = DialogHandler(skip_dialogs=True)
        agent = AgentDef(
            name="test",
            prompt="test",
            dialog=DialogConfig(trigger_prompt="test"),
        )
        provider = MagicMock()

        result = await handler.handle_dialog(
            agent=agent,
            agent_output={"result": "test"},
            opening_question="What do you think?",
            provider=provider,
        )

        assert result.user_declined is True
        assert result.messages == []


class TestDialogHandlerDismiss:
    """Tests for dismiss keyword detection."""

    def test_dismiss_keywords(self) -> None:
        """Test that standard dismiss keywords are detected."""
        handler = DialogHandler()
        dismiss_words = [
            "done",
            "continue",
            "go ahead",
            "proceed",
            "resume",
            "exit",
            "/done",
            "/continue",
        ]
        for keyword in dismiss_words:
            assert handler._is_dismiss(keyword) is True
            assert handler._is_dismiss(keyword.upper()) is True
            assert handler._is_dismiss(f"  {keyword}  ") is True

    def test_non_dismiss_text(self) -> None:
        """Test that normal text is not treated as dismiss."""
        handler = DialogHandler()
        assert handler._is_dismiss("I have a question") is False
        assert handler._is_dismiss("tell me more") is False
        assert handler._is_dismiss("") is False


class TestDialogHandlerEngagement:
    """Tests for the engagement choice flow."""

    @pytest.mark.asyncio
    async def test_user_declines_engagement(self) -> None:
        """Test that declining engagement skips the dialog loop."""
        handler = DialogHandler(console=MagicMock())
        agent = AgentDef(
            name="test",
            prompt="test",
            dialog=DialogConfig(trigger_prompt="test"),
        )
        provider = MagicMock()

        with patch.object(
            handler,
            "_ask_engagement",
            new_callable=AsyncMock,
            return_value="decline",
        ):
            result = await handler.handle_dialog(
                agent=agent,
                agent_output={"result": "test"},
                opening_question="What do you think?",
                provider=provider,
            )

        assert result.user_declined is True
        assert len(result.messages) == 1  # Only the opening question
        assert result.messages[0].role == "agent"

    @pytest.mark.asyncio
    async def test_user_engages_then_dismisses(self) -> None:
        """Test that user can engage and then dismiss."""
        handler = DialogHandler(console=MagicMock())
        agent = AgentDef(
            name="test",
            prompt="test",
            dialog=DialogConfig(trigger_prompt="test"),
        )
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="Here's my answer.")

        with (
            patch.object(handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"),
            patch.object(
                handler,
                "_get_user_input",
                new_callable=AsyncMock,
                side_effect=["tell me more", "done"],
            ),
        ):
            result = await handler.handle_dialog(
                agent=agent,
                agent_output={"result": "test"},
                opening_question="What do you think?",
                provider=provider,
            )

        assert result.user_dismissed is True
        # Messages: opening agent, user "tell me more", agent response, user "done"
        assert len(result.messages) == 4
        assert result.messages[0].role == "agent"
        assert result.messages[1].role == "user"
        assert result.messages[1].content == "tell me more"
        assert result.messages[2].role == "agent"
        assert result.messages[3].role == "user"
        assert result.messages[3].content == "done"


class TestDialogHandlerAgentContinue:
    """Tests for agent-proposed continuation."""

    @pytest.mark.asyncio
    async def test_agent_proposes_continue_user_approves(self) -> None:
        """Test agent proposes [READY_TO_CONTINUE] and user approves."""
        handler = DialogHandler(console=MagicMock())
        agent = AgentDef(
            name="test",
            prompt="test",
            dialog=DialogConfig(trigger_prompt="test"),
        )
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(
            return_value="I think I have enough info. [READY_TO_CONTINUE]"
        )

        with (
            patch.object(handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"),
            patch.object(
                handler,
                "_get_user_input",
                new_callable=AsyncMock,
                # First call: user message, second call: approve continuation
                side_effect=["here's context", "yes"],
            ),
        ):
            result = await handler.handle_dialog(
                agent=agent,
                agent_output={"result": "test"},
                opening_question="What do you think?",
                provider=provider,
            )

        assert result.agent_proposed_continue is True
        assert not result.user_dismissed

    @pytest.mark.asyncio
    async def test_agent_proposes_continue_user_declines(self) -> None:
        """Test agent proposes [READY_TO_CONTINUE] but user wants to keep chatting."""
        handler = DialogHandler(console=MagicMock())
        agent = AgentDef(
            name="test",
            prompt="test",
            dialog=DialogConfig(trigger_prompt="test"),
        )
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(
            side_effect=[
                "I think I have enough. [READY_TO_CONTINUE]",
                "Okay, what else?",
            ]
        )

        responses = {
            1: "here's more context",
            2: "no",  # Decline the continue proposal
            3: "actually wait",
        }
        call_count = 0

        async def mock_input(
            prompt_text: str = "[bold magenta]You[/bold magenta]",
        ) -> str:
            nonlocal call_count
            call_count += 1
            return responses.get(call_count, "done")

        with (
            patch.object(handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"),
            patch.object(handler, "_get_user_input", side_effect=mock_input),
        ):
            result = await handler.handle_dialog(
                agent=agent,
                agent_output={"result": "test"},
                opening_question="What do you think?",
                provider=provider,
            )

        assert result.agent_proposed_continue is True
        assert result.user_dismissed is True


class TestDialogHandlerEvents:
    """Tests for dialog event emission."""

    @pytest.mark.asyncio
    async def test_events_emitted_on_skip(self) -> None:
        """Test that no events are emitted when dialog is skipped."""
        emitter = MagicMock()
        handler = DialogHandler(skip_dialogs=True, emitter=emitter)
        agent = AgentDef(
            name="test",
            prompt="test",
            dialog=DialogConfig(trigger_prompt="test"),
        )

        await handler.handle_dialog(
            agent=agent,
            agent_output={"result": "test"},
            opening_question="What?",
            provider=MagicMock(),
        )

        # No events should be emitted on skip
        emitter.emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_events_emitted_on_decline(self) -> None:
        """Test that dialog_started and dialog_completed are emitted on decline."""
        emitter = MagicMock()
        handler = DialogHandler(console=MagicMock(), emitter=emitter)
        agent = AgentDef(
            name="test",
            prompt="test",
            dialog=DialogConfig(trigger_prompt="test"),
        )

        with patch.object(
            handler,
            "_ask_engagement",
            new_callable=AsyncMock,
            return_value="decline",
        ):
            await handler.handle_dialog(
                agent=agent,
                agent_output={"result": "test"},
                opening_question="What?",
                provider=MagicMock(),
            )

        # Should have: dialog_started, dialog_message (opening), dialog_completed
        event_types = [call.args[0].type for call in emitter.emit.call_args_list]
        assert "dialog_started" in event_types
        assert "dialog_message" in event_types
        assert "dialog_completed" in event_types


class TestDialogResult:
    """Tests for DialogResult dataclass."""

    def test_default_values(self) -> None:
        """Test DialogResult has sensible defaults."""
        result = DialogResult(dialog_id="test-123")
        assert result.dialog_id == "test-123"
        assert result.messages == []
        assert result.user_dismissed is False
        assert result.user_declined is False
        assert result.agent_proposed_continue is False
