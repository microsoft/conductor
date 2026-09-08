"""Tests for the dialog handler."""

from __future__ import annotations

import itertools
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor.config.schema import AgentDef, DialogConfig
from conductor.console import styled
from conductor.gates.dialog import DialogHandler, DialogResult
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
