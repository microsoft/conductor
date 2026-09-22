"""Tests for the dialog handler."""

from __future__ import annotations

import itertools
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor.config.schema import AgentDef, DialogConfig
from conductor.console import styled
from conductor.gates.dialog import (
    DIALOG_AGENT_SYSTEM_PROMPT,
    DISMISS_KEYWORDS,
    DialogHandler,
    DialogMessage,
    DialogResult,
    _asks_or_announces_question,
    _build_system_prompt,
    _extract_ready_marker,
)
from conductor.gates.human import (
    DIALOG_SUBMIT_SENTINEL,
    read_multiline_lines,
    read_on_daemon_thread,
)


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
                "Okay, what else?",  # the reply to "no", sent as its own turn
                "Sure.",
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


class TestDialogHandlerExceptionRecovery:
    """Provider exceptions must not corrupt history with orphan user turns."""

    @pytest.mark.asyncio
    async def test_cli_exception_pops_user_history(self) -> None:
        """If the provider raises, the next call must not see two user turns in a row."""
        handler = DialogHandler(console=MagicMock())
        agent = AgentDef(
            name="test",
            prompt="test",
            dialog=DialogConfig(trigger_prompt="test"),
        )
        provider = MagicMock()
        # Sequence: first call succeeds, second raises, third must NOT see the
        # orphaned "second try" user turn left over from the failed attempt.
        captured_histories: list[list[dict[str, str]]] = []

        async def execute(
            *,
            system_prompt: str,
            user_message: str,
            history: list[dict[str, str]],
            model: str | None,
        ) -> str:
            captured_histories.append(list(history))
            if len(captured_histories) == 2:
                raise RuntimeError("boom")
            return f"agent-reply-{len(captured_histories)}"

        provider.execute_dialog_turn = AsyncMock(side_effect=execute)

        with (
            patch.object(handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"),
            patch.object(
                handler,
                "_get_user_input",
                new_callable=AsyncMock,
                side_effect=["hello", "second try", "third try", "done"],
            ),
        ):
            result = await handler.handle_dialog(
                agent=agent,
                agent_output={"result": "test"},
                opening_question="?",
                provider=provider,
            )

        # Three provider calls: hello (ok), second try (fail), third try (ok).
        assert len(captured_histories) == 3
        # Third call must see only the FIRST successful exchange — not "second try".
        assert captured_histories[2] == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "agent-reply-1"},
        ]
        assert result.user_dismissed is True


class TestReadyMarkerTerminalOnly:
    """The READY marker must only fire as a terminal token (anti-injection)."""

    @pytest.mark.asyncio
    async def test_ready_marker_in_middle_of_response_does_not_fire(self) -> None:
        """If the agent merely quotes the marker mid-response, dialog continues."""
        handler = DialogHandler(console=MagicMock())
        agent = AgentDef(
            name="test",
            prompt="test",
            dialog=DialogConfig(trigger_prompt="test"),
        )
        provider = MagicMock()
        # Marker mid-response (e.g., quoting the user back) — must not end dialog.
        provider.execute_dialog_turn = AsyncMock(
            side_effect=[
                "You said [READY_TO_CONTINUE] but let's keep going.",
                "Okay last word.",
            ]
        )

        with (
            patch.object(handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"),
            patch.object(
                handler,
                "_get_user_input",
                new_callable=AsyncMock,
                side_effect=["please discuss", "more please", "done"],
            ),
        ):
            result = await handler.handle_dialog(
                agent=agent,
                agent_output={"result": "test"},
                opening_question="?",
                provider=provider,
            )

        # The mid-marker response must NOT have been treated as a continue proposal.
        assert result.agent_proposed_continue is False
        # And the marker text must not appear in the stored agent message.
        agent_msgs = [m.content for m in result.messages if m.role == "agent"]
        # Opening question (first agent msg) doesn't contain marker; the actual
        # response does (because it wasn't terminal, so we left the text intact).
        assert any("[READY_TO_CONTINUE]" in c for c in agent_msgs), (
            "Mid-response marker should be preserved verbatim when not terminal"
        )

    @pytest.mark.asyncio
    async def test_ready_marker_at_end_strips_from_stored_message(self) -> None:
        """Terminal marker fires the proposal AND is stripped from the stored message."""
        handler = DialogHandler(console=MagicMock())
        agent = AgentDef(
            name="test",
            prompt="test",
            dialog=DialogConfig(trigger_prompt="test"),
        )
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="All clear. [READY_TO_CONTINUE]")

        with (
            patch.object(handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"),
            patch.object(
                handler,
                "_get_user_input",
                new_callable=AsyncMock,
                side_effect=["context", "yes"],
            ),
        ):
            result = await handler.handle_dialog(
                agent=agent,
                agent_output={"result": "test"},
                opening_question="?",
                provider=provider,
            )

        assert result.agent_proposed_continue is True
        agent_msgs = [m.content for m in result.messages if m.role == "agent"]
        assert all("[READY_TO_CONTINUE]" not in c for c in agent_msgs), (
            "Terminal marker should be stripped from stored agent messages"
        )


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


class TestWebDialogFlow:
    """Tests for web-mode dialog driven by `WebDashboard.wait_for_dialog_message`.

    The web flow lives in `_web_handle_dialog` and was previously uncovered.
    These tests mock the dashboard's queue read with scripted message payloads.
    """

    def _make_handler(
        self,
        scripted_messages: list[dict[str, Any]],
    ) -> tuple[DialogHandler, MagicMock]:
        """Build a handler whose dashboard returns the scripted messages in order."""
        dashboard = MagicMock()
        dashboard.wait_for_dialog_message = AsyncMock(side_effect=scripted_messages)
        handler = DialogHandler(console=MagicMock(), web_dashboard=dashboard)
        return handler, dashboard

    @pytest.mark.asyncio
    async def test_web_decline_at_engagement(self) -> None:
        """If the first dashboard message is a decline, the dialog ends without provider calls."""
        handler, _ = self._make_handler([{"type": "dialog_decline", "agent_name": "test"}])
        agent = AgentDef(
            name="test",
            prompt="test",
            dialog=DialogConfig(trigger_prompt="test"),
        )
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock()

        result = await handler.handle_dialog(
            agent=agent,
            agent_output={"result": "test"},
            opening_question="?",
            provider=provider,
        )

        assert result.user_declined is True
        provider.execute_dialog_turn.assert_not_called()

    @pytest.mark.asyncio
    async def test_web_happy_path_single_turn(self) -> None:
        """Engage with a message, agent replies, user types 'done' to dismiss."""
        handler, _ = self._make_handler(
            [
                {"type": "dialog_message", "agent_name": "test", "content": "tell me more"},
                {"type": "dialog_message", "agent_name": "test", "content": "done"},
            ]
        )
        agent = AgentDef(
            name="test",
            prompt="test",
            dialog=DialogConfig(trigger_prompt="test"),
        )
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="here is more info")

        result = await handler.handle_dialog(
            agent=agent,
            agent_output={"result": "test"},
            opening_question="?",
            provider=provider,
        )

        assert result.user_dismissed is True
        # Provider should have been called exactly once with the user's first message
        provider.execute_dialog_turn.assert_called_once()
        # Transcript: opening agent question, user "tell me more", agent reply, user "done"
        roles = [m.role for m in result.messages]
        assert roles == ["agent", "user", "agent", "user"]

    @pytest.mark.asyncio
    async def test_web_exception_pops_user_history(self) -> None:
        """Provider exception in web mode must not leave an orphan user turn."""
        handler, _ = self._make_handler(
            [
                {"type": "dialog_message", "agent_name": "test", "content": "first try"},
                {"type": "dialog_message", "agent_name": "test", "content": "second try"},
                {"type": "dialog_message", "agent_name": "test", "content": "done"},
            ]
        )
        agent = AgentDef(
            name="test",
            prompt="test",
            dialog=DialogConfig(trigger_prompt="test"),
        )

        captured_histories: list[list[dict[str, str]]] = []

        async def execute(
            *,
            system_prompt: str,
            user_message: str,
            history: list[dict[str, str]],
            model: str | None,
        ) -> str:
            captured_histories.append(list(history))
            if len(captured_histories) == 1:
                raise RuntimeError("boom")
            return "recovered reply"

        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(side_effect=execute)

        result = await handler.handle_dialog(
            agent=agent,
            agent_output={"result": "test"},
            opening_question="?",
            provider=provider,
        )

        # First call (failed): history was empty, message="first try"
        # Second call (recovered): history must STILL be empty — no orphan "first try"
        assert captured_histories == [[], []]
        assert result.user_dismissed is True

    @pytest.mark.asyncio
    async def test_web_ready_marker_decline_no_duplicate_history(self) -> None:
        """Agent proposes continue, user declines with new content — provider must
        see the approval as a single user turn, not duplicated."""
        handler, _ = self._make_handler(
            [
                # engagement message
                {"type": "dialog_message", "agent_name": "test", "content": "first message"},
                # user's "no, here's more thoughts" reply to the continue proposal
                {"type": "dialog_message", "agent_name": "test", "content": "no, here's more"},
                # final dismiss
                {"type": "dialog_message", "agent_name": "test", "content": "done"},
            ]
        )
        agent = AgentDef(
            name="test",
            prompt="test",
            dialog=DialogConfig(trigger_prompt="test"),
        )

        captured_histories: list[list[dict[str, str]]] = []

        async def execute(
            *,
            system_prompt: str,
            user_message: str,
            history: list[dict[str, str]],
            model: str | None,
        ) -> str:
            captured_histories.append(list(history))
            if len(captured_histories) == 1:
                return "I think I have enough. [READY_TO_CONTINUE]"
            return "Ok, anything else?"

        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(side_effect=execute)

        await handler.handle_dialog(
            agent=agent,
            agent_output={"result": "test"},
            opening_question="?",
            provider=provider,
        )

        # Two provider calls expected. The second call's history must contain
        # exactly user→agent→<implicit current>, NOT user→agent→user→user.
        assert len(captured_histories) == 2
        second_call_history = captured_histories[1]
        # Count consecutive user-role entries
        for prev, curr in zip(second_call_history, second_call_history[1:], strict=False):
            assert not (prev["role"] == "user" and curr["role"] == "user"), (
                f"Two consecutive user turns in history: {second_call_history}"
            )
        # Specifically: history should be [user="first message", assistant=clean READY response]
        assert second_call_history == [
            {"role": "user", "content": "first message"},
            {"role": "assistant", "content": "I think I have enough. [READY_TO_CONTINUE]"},
        ]

    @pytest.mark.asyncio
    async def test_web_ready_marker_approval_yes(self) -> None:
        """Agent proposes continue, user says 'yes' — dialog ends cleanly."""
        handler, _ = self._make_handler(
            [
                {"type": "dialog_message", "agent_name": "test", "content": "context"},
                {"type": "dialog_message", "agent_name": "test", "content": "yes"},
            ]
        )
        agent = AgentDef(
            name="test",
            prompt="test",
            dialog=DialogConfig(trigger_prompt="test"),
        )
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="All set. [READY_TO_CONTINUE]")

        result = await handler.handle_dialog(
            agent=agent,
            agent_output={"result": "test"},
            opening_question="?",
            provider=provider,
        )

        assert result.agent_proposed_continue is True
        assert not result.user_dismissed
        assert not result.user_declined


class TestDialogNonAsciiOutput:
    """Non-ASCII agent output must reach the dialog LLM and the console unescaped."""

    @pytest.mark.asyncio
    async def test_cli_system_prompt_serializes_non_ascii_unescaped(self) -> None:
        # Requirement: the dialog-mode system prompt embeds the agent output
        # literally for every language — Cyrillic/CJK output must not reach the
        # model as \uXXXX escape sequences (issue #356, PR #359 review).
        handler = DialogHandler(console=MagicMock())
        agent = AgentDef(
            name="test",
            prompt="test",
            dialog=DialogConfig(trigger_prompt="test"),
        )
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="answer")

        with (
            patch.object(handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"),
            patch.object(
                handler,
                "_get_user_input",
                new_callable=AsyncMock,
                side_effect=["привет", "done"],
            ),
        ):
            await handler.handle_dialog(
                agent=agent,
                agent_output={"result": "你好 мир"},
                opening_question="?",
                provider=provider,
            )

        system_prompt = provider.execute_dialog_turn.call_args.kwargs["system_prompt"]
        assert "你好 мир" in system_prompt
        assert "\\u4f60" not in system_prompt
        assert "\\u043f" not in system_prompt

    @pytest.mark.asyncio
    async def test_web_system_prompt_serializes_non_ascii_unescaped(self) -> None:
        # Requirement: the web-mode dialog builds the same system prompt — the
        # non-ASCII output must be embedded literally there too (issue #356,
        # PR #359 review).
        dashboard = MagicMock()
        dashboard.wait_for_dialog_message = AsyncMock(
            side_effect=[
                {"type": "dialog_message", "agent_name": "test", "content": "расскажи"},
                {"type": "dialog_message", "agent_name": "test", "content": "done"},
            ]
        )
        handler = DialogHandler(console=MagicMock(), web_dashboard=dashboard)
        agent = AgentDef(
            name="test",
            prompt="test",
            dialog=DialogConfig(trigger_prompt="test"),
        )
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="answer")

        await handler.handle_dialog(
            agent=agent,
            agent_output={"result": "你好 мир"},
            opening_question="?",
            provider=provider,
        )

        provider.execute_dialog_turn.assert_called_once()
        system_prompt = provider.execute_dialog_turn.call_args.kwargs["system_prompt"]
        assert "你好 мир" in system_prompt
        assert "\\u4f60" not in system_prompt
        assert "\\u043f" not in system_prompt

    def test_console_panel_renders_non_ascii_unescaped(self) -> None:
        # Requirement: the "Agent Output" console panel a human reads during a
        # CLI dialog session must show real non-ASCII text, not \uXXXX escapes
        # (issue #356, PR #359 review).
        console = MagicMock()
        handler = DialogHandler(console=console)
        agent = AgentDef(
            name="test",
            prompt="test",
            dialog=DialogConfig(trigger_prompt="test"),
        )

        handler._display_dialog_start(agent, {"result": "你好 мир"}, "?", base_dir=None)

        # Panels render lazily, so inspect the RichMarkdown renderable inside
        # the "Agent Output" panel rather than str() of the Panel itself.
        from rich.markdown import Markdown as RichMarkdown
        from rich.panel import Panel

        panels = [call.args[0] for call in console.print.call_args_list if call.args]
        markdown_bodies = [
            p.renderable.markup
            for p in panels
            if isinstance(p, Panel) and isinstance(p.renderable, RichMarkdown)
        ]
        assert any("你好 мир" in body for body in markdown_bodies)
        assert all("\\u4f60" not in body for body in markdown_bodies)
        assert all("\\u043f" not in body for body in markdown_bodies)


class TestDialogMultilineInput:
    """Terminal dialog turns must accept pasted multi-line blocks."""

    @pytest.mark.asyncio
    async def test_pasted_block_is_one_user_prompt_with_newlines(self) -> None:
        """A pasted block is ingested as ONE prompt with internal newlines intact."""
        handler = DialogHandler(console=MagicMock())
        agent = AgentDef(name="t", prompt="p", dialog=DialogConfig(trigger_prompt="t"))
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="ack")
        with (
            patch.object(handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"),
            patch("conductor.gates.dialog.sys.stdin.isatty", return_value=True),
            patch(
                "builtins.input",
                side_effect=["line one", "line two", "line three", "/send", "done", EOFError()],
            ),
        ):
            result = await handler.handle_dialog(
                agent=agent,
                agent_output={"result": "x"},
                opening_question="Q?",
                provider=provider,
            )
        user_msgs = [m for m in result.messages if m.role == "user"]
        # Exactly one paste ingested as a single prompt, both newlines intact:
        assert user_msgs[0].content == "line one\nline two\nline three"
        provider.execute_dialog_turn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_eof_mid_paste_submits_content_not_dismissal(self) -> None:
        """EOF (Ctrl-D) mid-paste dispatches accumulated content, not dismissal."""
        handler = DialogHandler(console=MagicMock())
        agent = AgentDef(name="t", prompt="p", dialog=DialogConfig(trigger_prompt="t"))
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="ack")
        with (
            patch.object(handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"),
            patch("conductor.gates.dialog.sys.stdin.isatty", return_value=True),
            # Paste, then Ctrl-D (EOF) instead of /send; then a real dismissal.
            patch("builtins.input", side_effect=["ticket text", EOFError(), "done", EOFError()]),
        ):
            result = await handler.handle_dialog(
                agent=agent,
                agent_output={"result": "x"},
                opening_question="Q?",
                provider=provider,
            )
        # The paste-terminating EOF submitted its content and did NOT dismiss --
        # the dialog went on to accept a further turn, which is what ended it.
        assert [m.content for m in result.messages if m.role == "user"] == [
            "ticket text",
            "done",
        ]
        provider.execute_dialog_turn.assert_awaited_once()

    @pytest.mark.parametrize("isatty", [True, False])
    def test_opening_banner_advertises_the_sentinel_only_on_a_tty(self, isatty: bool) -> None:
        """The banner names the sentinel exactly when a turn requires it.

        Off a tty the turn falls back to the single-line ``Prompt.ask`` branch,
        where the sentinel does nothing -- so advertising it there would tell
        the user to type something with no effect.

        Rendered for real, and asserted against the constant rather than a
        literal, so the banner cannot drift from DIALOG_SUBMIT_SENTINEL.
        """
        import io

        from conductor.console import make_console

        buf = io.StringIO()
        handler = DialogHandler(console=make_console(file=buf, width=300, no_color=True))
        agent = AgentDef(name="t", prompt="p", dialog=DialogConfig(trigger_prompt="t"))

        with patch("conductor.gates.dialog.sys.stdin.isatty", return_value=isatty):
            handler._display_dialog_start(agent, {"out": 1}, "question?")

        rendered = "".join(buf.getvalue().split())
        needle = "".join(DIALOG_SUBMIT_SENTINEL.split())
        assert (needle in rendered) is isatty, rendered
        # The markup must be parsed, not inserted verbatim as a value.
        assert "[bold]" not in rendered, rendered
        # Off a tty the sentence keeps its original plural, since each line
        # really is a separate response there. Asserted because this wording
        # has already drifted to the singular once, and only a byte comparison
        # against the unmodified banner would otherwise have caught it.
        expected = "Typeyourresponsesbelow." if not isatty else "Typeyourresponsebelow."
        assert expected in rendered, rendered
        # The exit instruction has to describe the *active* reader. On a tty a
        # dismiss keyword is only seen once the turn is submitted, so telling
        # the user to "say done" there names a keystroke that does nothing.
        if isatty:
            assert "senddoneor/donewith/send" in rendered, rendered
            assert "Saydoneor/donewhenfinished." not in rendered, rendered
        else:
            assert "Saydoneor/donewhenfinished." in rendered, rendered

    @pytest.mark.asyncio
    async def test_ctrl_d_at_empty_prompt_dismisses(self) -> None:
        """A deliberate Ctrl-D with nothing typed ends the dialog.

        Regression: the multi-line reader converts EOF into a returned string,
        so an empty read must not be fed back round the loop -- otherwise the
        dismissal branch is unreachable and the dialog cannot be exited.
        """
        handler = DialogHandler(console=MagicMock())
        agent = AgentDef(name="t", prompt="p", dialog=DialogConfig(trigger_prompt="t"))
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="ack")

        # A *bounded* EOF source. ``side_effect=EOFError()`` re-raises forever,
        # so dropping the dismissal branch would spin this loop and hang the
        # suite rather than fail it -- there is no pytest-timeout configured.
        calls = itertools.count()

        def _eof_but_bounded(*_args: object, **_kwargs: object) -> str:
            assert next(calls) < 10, "dialog loop spun on an empty EOF read"
            raise EOFError

        with (
            patch.object(handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"),
            patch("conductor.gates.dialog.sys.stdin.isatty", return_value=True),
            patch("builtins.input", side_effect=_eof_but_bounded),
            patch(
                "conductor.gates.dialog.read_multiline_lines",
                wraps=read_multiline_lines,
            ) as reader,
            patch(
                "conductor.gates.dialog.read_on_daemon_thread",
                wraps=read_on_daemon_thread,
            ) as dispatch,
        ):
            result = await handler.handle_dialog(
                agent=agent,
                agent_output={"result": "x"},
                opening_question="Q?",
                provider=provider,
            )
        assert result.user_dismissed is True
        assert [m for m in result.messages if m.role == "user"] == []
        provider.execute_dialog_turn.assert_not_awaited()
        # Pins *this* reader, not merely "some reader dismissed on EOF".
        reader.assert_called_once()
        # And pins the dispatch: a cancelled ``asyncio.to_thread`` leaves its
        # worker blocked in ``input()`` holding a slot in the shared default
        # executor, which eventually deadlocks unrelated ``to_thread`` calls --
        # see ``read_on_daemon_thread``'s own docstring.
        dispatch.assert_called_once()

    @pytest.mark.asyncio
    async def test_reader_exception_dismisses_rather_than_crashing_the_dialog(self) -> None:
        """An exception out of the reader dismisses instead of escaping.

        This does **not** cover Ctrl-C. CPython runs signal handlers on the
        main thread only, and the read happens on a daemon thread, so a real
        SIGINT never reaches this ``except``: asyncio cancels the main task and
        ``KeyboardInterrupt`` tears the run down, as it does everywhere else.
        What is covered is an exception the reader itself raises.
        """
        handler = DialogHandler(console=MagicMock())
        agent = AgentDef(name="t", prompt="p", dialog=DialogConfig(trigger_prompt="t"))
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="ack")
        with (
            patch.object(handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"),
            patch("conductor.gates.dialog.sys.stdin.isatty", return_value=True),
            patch("builtins.input", side_effect=KeyboardInterrupt()),
        ):
            result = await handler.handle_dialog(
                agent=agent,
                agent_output={"result": "x"},
                opening_question="Q?",
                provider=provider,
            )
        assert result.user_dismissed is True

    @pytest.mark.asyncio
    async def test_web_path_unaffected_by_multiline(self) -> None:
        """The web seam still passes whole messages through, never via the new reader."""
        dashboard = MagicMock()
        dashboard.wait_for_dialog_message = AsyncMock(
            side_effect=[
                {"type": "dialog_message", "agent_name": "test", "content": "a\nb\nc"},
                {"type": "dialog_decline", "agent_name": "test"},
            ]
        )
        handler = DialogHandler(console=MagicMock(), web_dashboard=dashboard)
        agent = AgentDef(name="test", prompt="p", dialog=DialogConfig(trigger_prompt="t"))
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="ack")

        with patch(
            "conductor.gates.dialog.read_multiline_lines",
            side_effect=AssertionError("must not be called on the web path"),
        ):
            result = await handler.handle_dialog(
                agent=agent,
                agent_output={"result": "x"},
                opening_question="Q?",
                provider=provider,
            )

        user_msgs = [m for m in result.messages if m.role == "user"]
        assert user_msgs[0].content == "a\nb\nc"
        assert result.user_dismissed is True

    @pytest.mark.asyncio
    async def test_non_tty_main_turn_uses_the_single_line_prompt(self) -> None:
        """Off a tty the conversational turn stays on ``Prompt.ask``.

        Half of the reader gate: without the ``isatty()`` check the multi-line
        reader would activate under a pipe or in CI, waiting for a sentinel
        nobody can type.
        """
        handler = DialogHandler(console=MagicMock())
        agent = AgentDef(name="t", prompt="p", dialog=DialogConfig(trigger_prompt="t"))
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="ack")
        with (
            patch.object(handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"),
            patch("conductor.gates.dialog.sys.stdin.isatty", return_value=False),
            patch(
                "conductor.gates.dialog.Prompt.ask",
                side_effect=["piped answer", "done"],
            ) as ask,
            patch("builtins.input", side_effect=AssertionError("must not read raw stdin")),
        ):
            result = await handler.handle_dialog(
                agent=agent,
                agent_output={"result": "x"},
                opening_question="Q?",
                provider=provider,
            )
        assert ask.call_count == 2
        assert [m.content for m in result.messages if m.role == "user"] == [
            "piped answer",
            "done",
        ]

    @pytest.mark.asyncio
    async def test_confirmation_prompt_stays_single_line_on_a_tty(self) -> None:
        """A ``prompt_text`` question must not require the sentinel.

        The other half of the reader gate: without the ``prompt_text is None``
        check the yes/no confirmation would start demanding ``/send`` after
        "yes" on every interactive run.
        """
        handler = DialogHandler(console=MagicMock())
        with (
            patch("conductor.gates.dialog.sys.stdin.isatty", return_value=True),
            patch("conductor.gates.dialog.Prompt.ask", return_value="yes") as ask,
            patch("builtins.input", side_effect=AssertionError("must not read multi-line")),
        ):
            answer = await handler._get_user_input(prompt_text=styled("[bold]Continue?[/bold]"))
        assert answer == "yes"
        ask.assert_called_once()

    @pytest.mark.asyncio
    async def test_bare_sentinel_is_skipped_and_the_loop_continues(self) -> None:
        """An empty submission is neither a turn nor dismissal."""
        handler = DialogHandler(console=MagicMock())
        agent = AgentDef(name="t", prompt="p", dialog=DialogConfig(trigger_prompt="t"))
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="ack")
        with (
            patch.object(handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"),
            patch("conductor.gates.dialog.sys.stdin.isatty", return_value=True),
            patch(
                "builtins.input",
                side_effect=["/send", "real turn", "/send", "done", "/send", EOFError()],
            ),
        ):
            result = await handler.handle_dialog(
                agent=agent,
                agent_output={"result": "x"},
                opening_question="Q?",
                provider=provider,
            )
        # No empty turn recorded, none dispatched, and the loop carried on to
        # accept a real turn afterwards.
        assert [m.content for m in result.messages if m.role == "user"] == [
            "real turn",
            "done",
        ]
        provider.execute_dialog_turn.assert_awaited_once()
        assert result.user_dismissed is True

    @pytest.mark.parametrize("isatty", [True, False])
    def test_failure_notice_names_a_working_exit(self, isatty: bool) -> None:
        """The recovery notice must not name an inert keystroke either.

        It fires when a provider call has just failed -- the moment the user
        most wants a reliable way out -- so it has to move with the reader the
        same way the banner does.
        """
        from conductor.gates.dialog import _dismiss_instruction

        with patch("conductor.gates.dialog.sys.stdin.isatty", return_value=isatty):
            hint = _dismiss_instruction()

        assert ("with /send" in hint) is isatty, hint

    @pytest.mark.asyncio
    async def test_dismiss_keyword_still_exits_a_tty_dialog(self) -> None:
        """ "done" submitted with the sentinel ends the dialog.

        The banner promises this; without it the only exits from a tty dialog
        would be Ctrl-D and whatever the agent decides.
        """
        handler = DialogHandler(console=MagicMock())
        agent = AgentDef(name="t", prompt="p", dialog=DialogConfig(trigger_prompt="t"))
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="ack")
        with (
            patch.object(handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"),
            patch("conductor.gates.dialog.sys.stdin.isatty", return_value=True),
            patch("builtins.input", side_effect=["done", "/send", EOFError()]),
        ):
            result = await handler.handle_dialog(
                agent=agent,
                agent_output={"result": "x"},
                opening_question="Q?",
                provider=provider,
            )
        assert result.user_dismissed is True
        provider.execute_dialog_turn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pasted_indentation_reaches_the_provider_intact(self) -> None:
        """A pasted code block keeps its leading and interior whitespace.

        The empty-submission guards strip only to *decide* whether there is
        anything to send; the text itself must go through verbatim. Stripping
        it would silently reindent a pasted block, which is the data loss this
        whole reader exists to prevent, and no other test covers the leading
        edge of it.
        """
        handler = DialogHandler(console=MagicMock())
        agent = AgentDef(name="t", prompt="p", dialog=DialogConfig(trigger_prompt="t"))
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="ack")
        block = ["    def f():", "", "        return 1", "    "]
        with (
            patch.object(handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"),
            patch("conductor.gates.dialog.sys.stdin.isatty", return_value=True),
            patch("builtins.input", side_effect=[*block, "/send", "done", EOFError()]),
        ):
            result = await handler.handle_dialog(
                agent=agent,
                agent_output={"result": "x"},
                opening_question="Q?",
                provider=provider,
            )
        expected = "    def f():\n\n        return 1\n    "
        assert [m.content for m in result.messages if m.role == "user"][0] == expected
        assert provider.execute_dialog_turn.await_args.kwargs["user_message"] == expected

    @pytest.mark.asyncio
    async def test_whitespace_only_submission_is_not_a_turn(self) -> None:
        """Whitespace must not slip past the empty-submission guard.

        The reader strips trailing newlines, not whitespace, so a buffer of
        spaces survives as a truthy string. Dispatched, it would re-run the
        agent believing the user replied with whitespace.
        """
        handler = DialogHandler(console=MagicMock())
        agent = AgentDef(name="t", prompt="p", dialog=DialogConfig(trigger_prompt="t"))
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="ack")
        with (
            patch.object(handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"),
            patch("conductor.gates.dialog.sys.stdin.isatty", return_value=True),
            patch("builtins.input", side_effect=["   ", "/send", "done", EOFError()]),
        ):
            result = await handler.handle_dialog(
                agent=agent,
                agent_output={"result": "x"},
                opening_question="Q?",
                provider=provider,
            )
        assert [m.content for m in result.messages if m.role == "user"] == ["done"]
        provider.execute_dialog_turn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ctrl_d_after_whitespace_dismisses_without_submitting(self) -> None:
        """Ctrl-D means "I am leaving", even with whitespace in the buffer.

        Without a stripping guard the whitespace is dispatched as a turn and
        the dialog dismisses afterwards -- the user asked to leave and sent a
        message instead.
        """
        handler = DialogHandler(console=MagicMock())
        agent = AgentDef(name="t", prompt="p", dialog=DialogConfig(trigger_prompt="t"))
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="ack")
        with (
            patch.object(handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"),
            patch("conductor.gates.dialog.sys.stdin.isatty", return_value=True),
            patch("builtins.input", side_effect=["   ", EOFError()]),
        ):
            result = await handler.handle_dialog(
                agent=agent,
                agent_output={"result": "x"},
                opening_question="Q?",
                provider=provider,
            )
        provider.execute_dialog_turn.assert_not_awaited()
        assert [m for m in result.messages if m.role == "user"] == []
        assert result.user_dismissed is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blank", ["", "   "])
    async def test_blank_piped_line_is_skipped_off_a_tty(self, blank: str) -> None:
        """The empty guard also covers the non-tty path.

        ``Prompt.ask`` returns "" for a blank line, which previously reached
        the provider as an empty turn. The whitespace case is parametrised
        because ``rich.prompt.PromptBase.process_response`` strips its result,
        so in production a whitespace-only line already arrives as "" -- these
        mocks bypass that, and the guard has to hold either way.
        """
        handler = DialogHandler(console=MagicMock())
        agent = AgentDef(name="t", prompt="p", dialog=DialogConfig(trigger_prompt="t"))
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="ack")
        with (
            patch.object(handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"),
            patch("conductor.gates.dialog.sys.stdin.isatty", return_value=False),
            patch("conductor.gates.dialog.Prompt.ask", side_effect=[blank, "done"]),
        ):
            result = await handler.handle_dialog(
                agent=agent,
                agent_output={"result": "x"},
                opening_question="Q?",
                provider=provider,
            )
        assert [m.content for m in result.messages if m.role == "user"] == ["done"]
        provider.execute_dialog_turn.assert_not_awaited()


# --- The ready marker under a question -------------------------------------
#
# The agent is told to append [READY_TO_CONTINUE] when it has enough
# information, and a model routinely appends it to "ready for the next
# question when you are." Honouring that ended real interviews with the
# announced questions unasked. The guard withholds the marker when the final
# paragraph asks or announces a question; these tests pin the predicate and
# what the dialog does with a withheld marker.

_RECORDED_TAILS = [
    "Two tickets it is. Ready to move on to the next question when you are.",
    "Shall we move to the next question?",
    "Next I will ask about expected repos.",
]


def _make_agent(name: str = "grill", conversation_prompt: str | None = None) -> AgentDef:
    return AgentDef(
        name=name,
        prompt="p",
        dialog=DialogConfig(trigger_prompt="t", conversation_prompt=conversation_prompt),
    )


def _events(emitter: MagicMock, event_type: str) -> list[dict[str, Any]]:
    return [
        call.args[0].data for call in emitter.emit.call_args_list if call.args[0].type == event_type
    ]


class TestAsksOrAnnouncesQuestion:
    """The predicate behind the guard, measured on message tails."""

    @pytest.mark.parametrize(
        "text",
        [
            *_RECORDED_TAILS,
            "Question 1 of ~3: which repos does this touch?",
            "Great. Question 2 of 3 — where do the boundaries fall.",
            "That settles scope. I have two more questions for you.",
            "Noted. Let me ask about ordering next.",
            "Understood, thanks. One more question on dependencies.",
            "Does that sound right to you?",
            "Good.\n\nNow, does the deliver phase own the API design?",
            # A "no" idiom opening the sentence does not negate the announcement.
            "No problem, next question is about the repos.",
            "No worries, let me ask about the next thing.",
            # Negation does not cross a sentence boundary.
            "Not a problem. Question 2 of 3 is about ordering.",
            # Question, recommendation, sign-off: the question is not in the
            # final paragraph, and it is still being asked.
            "Question 2 of 3: where do the boundaries fall?\n\nMy recommendation: ike only."
            "\n\nLet me know.",
            "Which repos does this touch?\n\nTake your time.",
            "Two tickets it is. I have one more question for you before we finish."
            "\n\nTake your time!",
            # A question quoted earlier in the message keeps the dialog open
            # too: the heuristic leans that way on purpose.
            "The user asked whether this is one ticket or two?\n\nSummary: one ticket, confirmed.",
            # A "?" right after a Markdown link or an autolink is the
            # message's, not the URL's.
            "Should we use [this repository](https://example.com/repo)?",
            "Should we use <https://example.com>?",
            "Do you mean [w](https://en.wikipedia.org/wiki/Foo_(bar))?",
            "Try `https://example.com/a?b=c`?",
            # ...and so is one right after a bare URL: a URL never ends on "?".
            "Have you seen https://example.com?",
            "Which: https://a.com/x?y=1 or https://b.com/x?y=2?",
            # The "?" is followed by closing emphasis, so only the delimiter
            # exclusion keeps it out of the URL.
            "**Should we use [this](https://example.com)?**",
            "**Should we use <https://example.com>?**",
            # A quote and a backtick are not URL characters either, so a "?"
            # before one is the message's even when more follows it.
            'Did you mean "https://example.com?"',
            "Try `https://example.com/a?b=c`?!",
            # A participle negates only after a copula; a negated participle
            # does not negate at all; a line break ends a sentence.
            "There's one more question I need answered before continuing.",
            "I have one more question that needs to be addressed.",
            "I haven't asked the next question yet.",
            "The remaining questions have not been answered.",
            "One more question isn't settled yet.",
            "- No changes to CI\n- Next question: repo list",
            "Nothing else on scope\nNext question: which repos",
        ],
    )
    def test_asking_or_announcing_a_question_is_detected(self, text: str) -> None:
        assert _asks_or_announces_question(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "That covers everything I need.",
            "All clear.",
            "I think I have enough info.",
            "I have no further questions.",
            "I don't need to ask anything else.",
            "Thanks, that resolves the last open point. No more questions from me.",
            "I asked about repos earlier and you confirmed ike only. We are done.",
            # Ordinary closings that name an action, not a question.
            "Thanks, I have everything I need. I will move on to drafting the ticket.",
            "Great, that settles it. Let me get to work.",
            "Understood. I'll turn to the implementation now.",
            "We can move on. Nothing else from me.",
            # Counting past questions is not announcing one; a URL's query
            # string is not a question.
            "You raised two questions; both are answered.",
            "See https://example.com/a?b=1 for the spec.",
            "The [spec](https://example.com/a?b=c) covers it.",
            "The spec at <https://example.com/a?b=c> covers it.",
            "Run `https://example.com/a?b=c` first.",
            'Run "https://example.com/a?b=c" first.',
            "Read https://en.wikipedia.org/wiki/Foo_(bar)?x=1 first.",
            # Negation is scoped to the clause it is in, wherever it falls.
            "No, that is settled. That resolves it.",
            "Not a problem, I'll ask nothing more of you.",
            "I will ask no more.",
            # Questions already dealt with are not questions coming.
            "The remaining questions were all answered above.",
            "Both follow-up questions are answered.",
            "The follow-up questions have been answered.",
            "Remaining questions all answered.",
            "",
        ],
    )
    def test_a_closing_message_is_not(self, text: str) -> None:
        assert _asks_or_announces_question(text) is False


class TestReadyMarkerUnderAQuestion:
    """A trailing marker on a message that asks or announces a question is withheld."""

    @pytest.mark.parametrize("tail", _RECORDED_TAILS)
    def test_marker_is_withheld_and_stripped(self, tail: str) -> None:
        proposed, cleaned = _extract_ready_marker(f"{tail} [READY_TO_CONTINUE]")
        assert proposed is False
        assert cleaned == tail

    def test_marker_after_a_link_then_a_question_mark_is_withheld(self) -> None:
        question = "Should we use [this repository](https://example.com/repo)?"
        proposed, cleaned = _extract_ready_marker(f"{question} [READY_TO_CONTINUE]")
        assert proposed is False
        assert cleaned == question

    def test_marker_on_a_closing_message_is_honoured(self) -> None:
        proposed, cleaned = _extract_ready_marker(
            "That covers everything I need. [READY_TO_CONTINUE]"
        )
        assert proposed is True
        assert cleaned == "That covers everything I need."

    def test_prompt_tells_the_agent_the_same_rule(self) -> None:
        """The guard is a backstop; the instruction is the mechanism."""
        assert "never put it in a message that asks" in DIALOG_AGENT_SYSTEM_PROMPT

    @pytest.mark.asyncio
    async def test_cli_dialog_stays_open_when_the_marker_is_withheld(self) -> None:
        """No Continue? prompt: the user's next reply goes to the agent."""
        emitter = MagicMock()
        handler = DialogHandler(console=MagicMock(), emitter=emitter)
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(
            side_effect=[
                "Two tickets it is. Ready to move on to the next question when you are."
                " [READY_TO_CONTINUE]",
                "Which repos does this touch? I propose ike.",
                "Noted, ike only. That covers everything I need. [READY_TO_CONTINUE]",
            ]
        )
        prompts_seen: list[str] = []

        async def user_input(prompt_text: Any = None) -> str:
            prompts_seen.append(prompt_text.plain if prompt_text is not None else "<turn>")
            return ["two tickets", "yes", "ike only", "yes"][len(prompts_seen) - 1]

        with (
            patch.object(handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"),
            patch.object(handler, "_get_user_input", side_effect=user_input),
        ):
            result = await handler.handle_dialog(
                agent=_make_agent(),
                agent_output={"result": "x"},
                opening_question="Scope?",
                provider=provider,
            )

        # The "yes" after the withheld marker was a turn, not an approval.
        assert prompts_seen[:3] == ["<turn>", "<turn>", "<turn>"]
        assert provider.execute_dialog_turn.await_count == 3
        assert provider.execute_dialog_turn.await_args_list[1].kwargs["user_message"] == "yes"
        # Only the closing message proposed continuing.
        assert result.agent_proposed_continue is True
        assert result.user_dismissed is False
        assert result.agent_question_outstanding is False
        # The withheld marker never reached the transcript or the events.
        agent_contents = [m.content for m in result.messages if m.role == "agent"]
        assert all("[READY_TO_CONTINUE]" not in c for c in agent_contents)
        assert all(
            "[READY_TO_CONTINUE]" not in e["content"] for e in _events(emitter, "dialog_message")
        )

    @pytest.mark.asyncio
    async def test_web_dialog_stays_open_when_the_marker_is_withheld(self) -> None:
        dashboard = MagicMock()
        dashboard.wait_for_dialog_message = AsyncMock(
            side_effect=[
                {"type": "dialog_message", "content": "two tickets"},
                {"type": "dialog_message", "content": "yes"},
                {"type": "dialog_message", "content": "yes"},
            ]
        )
        handler = DialogHandler(console=MagicMock(), web_dashboard=dashboard)
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(
            side_effect=[
                "Next I will ask about expected repos. [READY_TO_CONTINUE]",
                "All set. [READY_TO_CONTINUE]",
            ]
        )

        result = await handler.handle_dialog(
            agent=_make_agent(),
            agent_output={"result": "x"},
            opening_question="Scope?",
            provider=provider,
        )

        assert provider.execute_dialog_turn.await_count == 2
        assert provider.execute_dialog_turn.await_args_list[1].kwargs["user_message"] == "yes"
        assert result.agent_proposed_continue is True
        assert all("[READY_TO_CONTINUE]" not in m.content for m in result.messages)


class TestContinueProposalPrompt:
    """What the operator sees, and what each reply does, once the marker is honoured."""

    async def _run_cli(
        self,
        replies: list[str | None],
        *,
        agent_responses: list[str] | None = None,
    ) -> tuple[DialogResult, MagicMock, MagicMock, list[Any]]:
        emitter = MagicMock()
        console = MagicMock()
        handler = DialogHandler(console=console, emitter=emitter)
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(
            side_effect=agent_responses or ["All clear. [READY_TO_CONTINUE]", "Okay."]
        )
        prompts_seen: list[Any] = []
        replies_iter = iter(replies)

        async def user_input(prompt_text: Any = None) -> str | None:
            prompts_seen.append(prompt_text)
            return next(replies_iter)

        with (
            patch.object(handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"),
            patch.object(handler, "_get_user_input", side_effect=user_input),
        ):
            result = await handler.handle_dialog(
                agent=_make_agent(),
                agent_output={"result": "x"},
                opening_question="Scope?",
                provider=provider,
            )
        return result, provider, emitter, prompts_seen

    @pytest.mark.asyncio
    async def test_prompt_and_proposal_name_the_agent(self) -> None:
        """The operator can tell from the prompt alone who is asking."""
        import io

        from conductor.console import make_console

        buf = io.StringIO()
        handler = DialogHandler(console=make_console(file=buf, width=300, no_color=True))
        handler._display_continue_proposal("grill")
        assert "grill believes it has enough information to continue" in buf.getvalue()
        assert "The agent believes" not in buf.getvalue()

        result, _, _, prompts = await self._run_cli(["context", "yes"])
        approval_prompt = prompts[1].plain
        assert "Let grill continue?" in approval_prompt
        assert "yes ends the dialog" in approval_prompt
        assert "anything else is sent to grill" in approval_prompt
        assert result.agent_proposed_continue is True

    @pytest.mark.asyncio
    async def test_conversational_affirmative_is_sent_to_the_agent(self) -> None:
        result, provider, emitter, _ = await self._run_cli(
            ["context", "yes, and ike is the repo", "done"]
        )
        assert provider.execute_dialog_turn.await_count == 2
        second = provider.execute_dialog_turn.await_args_list[1].kwargs
        assert second["user_message"] == "yes, and ike is the repo"
        # Sent exactly once, with no two consecutive user turns in history.
        assert second["history"] == [
            {"role": "user", "content": "context"},
            {"role": "assistant", "content": "All clear. [READY_TO_CONTINUE]"},
        ]
        # The reply is in the transcript the engine feeds back, and on the wire.
        assert [m.content for m in result.messages if m.role == "user"] == [
            "context",
            "yes, and ike is the repo",
            "done",
        ]
        user_events = [
            e["content"] for e in _events(emitter, "dialog_message") if e["role"] == "user"
        ]
        assert user_events == ["context", "yes, and ike is the repo", "done"]
        assert result.user_dismissed is True

    @pytest.mark.asyncio
    async def test_empty_reply_re_asks_instead_of_approving(self) -> None:
        result, provider, _, prompts = await self._run_cli(["context", "", "   ", "yes"])
        assert provider.execute_dialog_turn.await_count == 1
        # Three approval prompts: the two blank replies were re-asked.
        assert len(prompts) == 4
        assert result.agent_proposed_continue is True
        assert result.user_dismissed is False

    @pytest.mark.asyncio
    async def test_dismiss_keyword_at_the_prompt_dismisses(self) -> None:
        """``done`` under the proposal ends the dialog rather than being sent on."""
        result, provider, emitter, _ = await self._run_cli(["context", "done"])
        assert provider.execute_dialog_turn.await_count == 1
        assert result.user_dismissed is True
        (completed,) = _events(emitter, "dialog_completed")
        assert completed["user_dismissed"] is True
        assert completed["agent_proposed_continue"] is True

    @pytest.mark.asyncio
    async def test_padded_yes_is_still_approval(self) -> None:
        result, provider, _, _ = await self._run_cli(["context", "  Yes "])
        assert provider.execute_dialog_turn.await_count == 1
        assert result.agent_proposed_continue is True
        assert result.user_dismissed is False

    @pytest.mark.asyncio
    async def test_web_decline_at_the_proposal_is_a_dismissal(self) -> None:
        emitter = MagicMock()
        dashboard = MagicMock()
        dashboard.wait_for_dialog_message = AsyncMock(
            side_effect=[
                {"type": "dialog_message", "content": "context"},
                {"type": "dialog_decline"},
            ]
        )
        handler = DialogHandler(console=MagicMock(), web_dashboard=dashboard, emitter=emitter)
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="All clear. [READY_TO_CONTINUE]")
        result = await handler.handle_dialog(
            agent=_make_agent(), agent_output={"r": 1}, opening_question="Scope?", provider=provider
        )
        assert result.user_dismissed is True
        (completed,) = _events(emitter, "dialog_completed")
        assert completed["user_dismissed"] is True

    @pytest.mark.asyncio
    async def test_eof_at_the_prompt_still_ends_the_dialog(self) -> None:
        result, _, _, _ = await self._run_cli(["context", None])
        assert result.agent_proposed_continue is True
        assert result.user_dismissed is False

    @pytest.mark.asyncio
    async def test_web_proposal_names_the_agent_and_dismiss_works(self) -> None:
        emitter = MagicMock()
        dashboard = MagicMock()
        dashboard.wait_for_dialog_message = AsyncMock(
            side_effect=[
                {"type": "dialog_message", "content": "context"},
                {"type": "dialog_message", "content": ""},
                {"type": "dialog_message", "content": None},  # a client may send null
                {"type": "dialog_message", "content": "done"},
            ]
        )
        handler = DialogHandler(console=MagicMock(), web_dashboard=dashboard, emitter=emitter)
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="All clear. [READY_TO_CONTINUE]")

        result = await handler.handle_dialog(
            agent=_make_agent(),
            agent_output={"result": "x"},
            opening_question="Scope?",
            provider=provider,
        )

        proposal = _events(emitter, "dialog_message")[-2]["content"]
        assert dashboard.wait_for_dialog_message.await_count == 4
        assert "grill believes it has enough information to continue" in proposal
        assert "Reply **yes** to end the dialog" in proposal
        assert "anything else is sent to grill" in proposal
        assert result.user_dismissed is True
        assert provider.execute_dialog_turn.await_count == 1
        assert [m.content for m in result.messages if m.role == "user"] == ["context", "done"]


class TestEngagementPromptDispatch:
    """The engagement prompt is the first stdin read of every terminal dialog."""

    @pytest.mark.asyncio
    async def test_engagement_prompt_reads_on_the_daemon_thread(self) -> None:
        """Same dispatch as the main turn: a cancelled ``asyncio.to_thread``
        would leave its worker blocked in ``input()`` holding a shared
        executor slot (see ``read_on_daemon_thread``)."""
        handler = DialogHandler(console=MagicMock())
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock()
        with (
            patch("builtins.input", return_value="2"),
            patch("asyncio.to_thread", side_effect=AssertionError("to_thread must not be used")),
            patch(
                "conductor.gates.dialog.read_on_daemon_thread",
                wraps=read_on_daemon_thread,
            ) as dispatch,
        ):
            result = await handler.handle_dialog(
                agent=_make_agent(),
                agent_output={"result": "x"},
                opening_question="Scope?",
                provider=provider,
            )
        assert result.user_declined is True
        dispatch.assert_called_once()
        provider.execute_dialog_turn.assert_not_awaited()


class TestDismissKeywordsAreStated:
    """``continue``/``proceed`` stay dismiss keywords, so the banner has to say so."""

    @pytest.mark.parametrize("isatty", [True, False])
    def test_banner_lists_every_dismiss_keyword(self, isatty: bool) -> None:
        import io

        from conductor.console import make_console

        buf = io.StringIO()
        handler = DialogHandler(console=make_console(file=buf, width=300, no_color=True))
        with patch("conductor.gates.dialog.sys.stdin.isatty", return_value=isatty):
            handler._display_dialog_start(_make_agent(), {"out": 1}, "question?")
        rendered = " ".join(buf.getvalue().split())
        for keyword in DISMISS_KEYWORDS:
            assert keyword in rendered, (keyword, rendered)


class TestAgentQuestionOutstanding:
    """A dialog that ends under an unanswered question says so in its output."""

    @pytest.mark.asyncio
    async def test_cli_dismiss_under_a_question_is_recorded(self) -> None:
        import io

        from conductor.console import make_console

        buf = io.StringIO()
        emitter = MagicMock()
        handler = DialogHandler(
            console=make_console(file=buf, width=300, no_color=True), emitter=emitter
        )
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(
            return_value="Two tickets it is. Next I will ask about expected repos."
        )
        with (
            patch.object(handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"),
            patch.object(
                handler, "_get_user_input", new_callable=AsyncMock, side_effect=["two", "done"]
            ),
        ):
            result = await handler.handle_dialog(
                agent=_make_agent(),
                agent_output={"result": "x"},
                opening_question="Scope?",
                provider=provider,
            )

        assert result.agent_question_outstanding is True
        (completed,) = _events(emitter, "dialog_completed")
        assert completed["agent_question_outstanding"] is True
        assert "grill still had a question outstanding" in buf.getvalue()

    @pytest.mark.asyncio
    async def test_cli_clean_close_is_not_flagged(self) -> None:
        emitter = MagicMock()
        handler = DialogHandler(console=MagicMock(), emitter=emitter)
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="That covers everything I need.")
        with (
            patch.object(handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"),
            patch.object(
                handler, "_get_user_input", new_callable=AsyncMock, side_effect=["two", "done"]
            ),
        ):
            result = await handler.handle_dialog(
                agent=_make_agent(),
                agent_output={"result": "x"},
                opening_question="Scope?",
                provider=provider,
            )
        assert result.agent_question_outstanding is False
        (completed,) = _events(emitter, "dialog_completed")
        assert completed["agent_question_outstanding"] is False

    @pytest.mark.asyncio
    async def test_web_dismiss_under_a_question_is_recorded(self) -> None:
        emitter = MagicMock()
        dashboard = MagicMock()
        dashboard.wait_for_dialog_message = AsyncMock(
            side_effect=[
                {"type": "dialog_message", "content": "two"},
                {"type": "dialog_message", "content": "done"},
            ]
        )
        handler = DialogHandler(console=MagicMock(), web_dashboard=dashboard, emitter=emitter)
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="Shall we move to the next question?")

        result = await handler.handle_dialog(
            agent=_make_agent(),
            agent_output={"result": "x"},
            opening_question="Scope?",
            provider=provider,
        )
        assert result.agent_question_outstanding is True
        (completed,) = _events(emitter, "dialog_completed")
        assert completed["agent_question_outstanding"] is True

    @pytest.mark.asyncio
    async def test_web_null_content_on_an_ordinary_turn_does_not_raise(self) -> None:
        """A hand-crafted frame with ``"content": null`` is read as an empty turn."""
        dashboard = MagicMock()
        dashboard.wait_for_dialog_message = AsyncMock(
            side_effect=[
                {"type": "dialog_message", "content": "two"},
                {"type": "dialog_message", "content": None},
                {"type": "dialog_message", "content": "done"},
            ]
        )
        handler = DialogHandler(console=MagicMock(), web_dashboard=dashboard)
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="Noted.")

        result = await handler.handle_dialog(
            agent=_make_agent(), agent_output={"r": 1}, opening_question="Scope?", provider=provider
        )
        assert result.user_dismissed is True
        assert provider.execute_dialog_turn.await_args_list[1].kwargs["user_message"] == ""

    @pytest.mark.asyncio
    async def test_web_clean_close_is_not_flagged(self) -> None:
        emitter = MagicMock()
        dashboard = MagicMock()
        dashboard.wait_for_dialog_message = AsyncMock(
            side_effect=[
                {"type": "dialog_message", "content": "two"},
                {"type": "dialog_message", "content": "done"},
            ]
        )
        handler = DialogHandler(console=MagicMock(), web_dashboard=dashboard, emitter=emitter)
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="That covers everything I need.")

        result = await handler.handle_dialog(
            agent=_make_agent(),
            agent_output={"result": "x"},
            opening_question="Scope?",
            provider=provider,
        )
        assert result.agent_question_outstanding is False
        (completed,) = _events(emitter, "dialog_completed")
        assert completed["agent_question_outstanding"] is False

    async def _dismiss_right_after_the_opening(
        self, opening_question: str, *, web: bool
    ) -> tuple[DialogResult, dict[str, Any]]:
        """Run a dialog whose first user input is a dismiss keyword; no agent turn."""
        emitter = MagicMock()
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(side_effect=AssertionError("no agent turn"))
        if web:
            dashboard = MagicMock()
            dashboard.wait_for_dialog_message = AsyncMock(
                return_value={"type": "dialog_message", "content": "done"}
            )
            handler = DialogHandler(console=MagicMock(), web_dashboard=dashboard, emitter=emitter)
            result = await handler.handle_dialog(
                agent=_make_agent(),
                agent_output={"result": "x"},
                opening_question=opening_question,
                provider=provider,
            )
        else:
            handler = DialogHandler(console=MagicMock(), emitter=emitter)
            with (
                patch.object(
                    handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"
                ),
                patch.object(
                    handler, "_get_user_input", new_callable=AsyncMock, return_value="done"
                ),
            ):
                result = await handler.handle_dialog(
                    agent=_make_agent(),
                    agent_output={"result": "x"},
                    opening_question=opening_question,
                    provider=provider,
                )
        (completed,) = _events(emitter, "dialog_completed")
        return result, completed

    @pytest.mark.asyncio
    async def test_dismissal_right_after_the_opening_question_matches_on_both_paths(
        self,
    ) -> None:
        """The opening question is the agent's last message on either path."""
        opening = "Which repository should I use?"
        cli_result, cli_completed = await self._dismiss_right_after_the_opening(opening, web=False)
        web_result, web_completed = await self._dismiss_right_after_the_opening(opening, web=True)

        assert cli_result.agent_question_outstanding is True
        assert web_result.agent_question_outstanding is True
        assert cli_completed["agent_question_outstanding"] is True
        assert web_completed["agent_question_outstanding"] is True
        assert cli_completed["user_dismissed"] is True
        assert web_completed["user_dismissed"] is True
        # Opening question and "done" only: the web loop was skipped, not
        # entered and left by its own dismiss check.
        assert cli_completed["turn_count"] == web_completed["turn_count"] == 2

    @pytest.mark.asyncio
    async def test_web_dismissal_right_after_a_closing_opening_is_not_flagged(self) -> None:
        result, completed = await self._dismiss_right_after_the_opening(
            "I have everything I need.", web=True
        )
        assert result.agent_question_outstanding is False
        assert completed["agent_question_outstanding"] is False
        assert completed["user_dismissed"] is True

    def test_result_default(self) -> None:
        assert DialogResult(dialog_id="d").agent_question_outstanding is False

    def test_no_agent_message_means_no_question(self) -> None:
        assert DialogResult(dialog_id="d").last_agent_message_asks_question() is False
        only_user = DialogResult(dialog_id="d", messages=[DialogMessage(role="user", content="?")])
        assert only_user.last_agent_message_asks_question() is False


class TestConversationPrompt:
    """``dialog.conversation_prompt`` reaches the agent holding the conversation."""

    @pytest.mark.asyncio
    async def test_cli_system_prompt_carries_the_author_instructions(self) -> None:
        handler = DialogHandler(console=MagicMock())
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="ack")
        with (
            patch.object(handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"),
            patch.object(
                handler, "_get_user_input", new_callable=AsyncMock, side_effect=["hi", "done"]
            ),
        ):
            await handler.handle_dialog(
                agent=_make_agent(conversation_prompt="Ask one question at a time. {not a slot}"),
                agent_output={"result": "x"},
                opening_question="Scope?",
                provider=provider,
            )
        system_prompt = provider.execute_dialog_turn.await_args.kwargs["system_prompt"]
        assert "--- WORKFLOW AUTHOR INSTRUCTIONS FOR THIS CONVERSATION ---" in system_prompt
        assert "Ask one question at a time. {not a slot}" in system_prompt
        assert "follow the instructions" in system_prompt
        # Placed after the built-in rules, before the output being discussed.
        assert system_prompt.index("RULES:") < system_prompt.index("WORKFLOW AUTHOR")
        assert system_prompt.index("WORKFLOW AUTHOR") < system_prompt.index(
            "AGENT OUTPUT TO DISCUSS"
        )

    @pytest.mark.asyncio
    async def test_web_system_prompt_carries_the_author_instructions(self) -> None:
        dashboard = MagicMock()
        dashboard.wait_for_dialog_message = AsyncMock(
            side_effect=[
                {"type": "dialog_message", "content": "hi"},
                {"type": "dialog_message", "content": "done"},
            ]
        )
        handler = DialogHandler(console=MagicMock(), web_dashboard=dashboard)
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="ack")
        await handler.handle_dialog(
            agent=_make_agent(conversation_prompt="Confirm the repo list before closing."),
            agent_output={"result": "x"},
            opening_question="Scope?",
            provider=provider,
        )
        system_prompt = provider.execute_dialog_turn.await_args.kwargs["system_prompt"]
        assert "Confirm the repo list before closing." in system_prompt

    @pytest.mark.parametrize("conversation_prompt", [None, "", "   \n"])
    @pytest.mark.asyncio
    async def test_absent_or_blank_prompt_leaves_the_built_in_prompt_alone(
        self, conversation_prompt: str | None
    ) -> None:
        handler = DialogHandler(console=MagicMock())
        provider = MagicMock()
        provider.execute_dialog_turn = AsyncMock(return_value="ack")
        with (
            patch.object(handler, "_ask_engagement", new_callable=AsyncMock, return_value="engage"),
            patch.object(
                handler, "_get_user_input", new_callable=AsyncMock, side_effect=["hi", "done"]
            ),
        ):
            await handler.handle_dialog(
                agent=_make_agent(conversation_prompt=conversation_prompt),
                agent_output={"result": "x"},
                opening_question="Scope?",
                provider=provider,
            )
        system_prompt = provider.execute_dialog_turn.await_args.kwargs["system_prompt"]
        assert "WORKFLOW AUTHOR" not in system_prompt
        assert "{conversation_instructions}" not in system_prompt

    def test_trigger_prompt_still_never_reaches_the_conversation(self) -> None:
        agent = AgentDef(
            name="a", prompt="p", dialog=DialogConfig(trigger_prompt="TRIGGER CRITERIA")
        )
        assert "TRIGGER CRITERIA" not in _build_system_prompt(agent, {"r": 1})


class TestBuildSystemPrompt:
    """The one prompt builder behind both paths."""

    def test_output_json_cannot_serialise_is_rendered_with_str(self) -> None:
        """A tuple key defeats ``json.dumps`` even with ``default=str``."""
        agent_output: dict[Any, Any] = {("a", "b"): 1}
        assert str(agent_output) in _build_system_prompt(_make_agent(), agent_output)
