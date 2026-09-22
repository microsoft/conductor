"""Dialog handler for agent-initiated user conversations.

This module implements the interactive dialog mode where an agent pauses
after execution and enters a free-form conversation with the user.
The dialog presents full context (output, file paths, reasoning) and
supports multi-turn exchanges until the user or agent concludes.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rich.markdown import Markdown as RichMarkdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from conductor.console import MarkupFreeConsole, make_console, styled
from conductor.executor.linkify import linkify_markdown
from conductor.gates.human import (
    DIALOG_SUBMIT_SENTINEL,
    read_multiline_lines,
    read_on_daemon_thread,
)

if TYPE_CHECKING:
    from pathlib import Path

    from conductor.config.schema import AgentDef
    from conductor.events import WorkflowEventEmitter
    from conductor.providers.base import AgentProvider
    from conductor.web.server import WebDashboard

logger = logging.getLogger(__name__)

# System prompt for the agent during dialog mode.
# The agent should be conversational and propose completion when ready.
DIALOG_AGENT_SYSTEM_PROMPT = """\
You are helping with a workflow dialog. A workflow agent named "{agent_name}" \
has produced output and needs to discuss it with the user.

YOUR TASK: Act as the agent "{agent_name}" and have a conversation with the \
user about the output below. You must stay in character and discuss the output \
topic naturally. This is NOT a coding task — the user wants to discuss the \
content of the agent's output, whatever the topic may be.

RULES:
- Discuss the output topic as written — do NOT refuse, redirect, or claim \
  the topic is "out of scope"
- Share full context including file paths, code snippets, and reasoning \
  when relevant
- When you believe you have enough information to proceed, include the \
  exact marker [READY_TO_CONTINUE] at the end of your message. The marker \
  means the conversation can end now: never put it in a message that asks \
  the user a question or says a further question is coming
- If the user says "done", "continue", or "go ahead", treat that as \
  permission to stop discussing
{conversation_instructions}
--- AGENT OUTPUT TO DISCUSS ---
{agent_output}
--- END AGENT OUTPUT ---
"""

# Inserted into DIALOG_AGENT_SYSTEM_PROMPT when the workflow author set
# ``dialog.conversation_prompt``; the empty string otherwise, so the built-in
# prompt is unchanged for a workflow that did not. ``trigger_prompt`` never
# reaches this prompt -- it is the evaluator's criteria for *opening* the
# dialog, read in engine/dialog_evaluator.py.
_CONVERSATION_INSTRUCTIONS_BLOCK = """\

--- WORKFLOW AUTHOR INSTRUCTIONS FOR THIS CONVERSATION ---
{conversation_prompt}
--- END WORKFLOW AUTHOR INSTRUCTIONS ---
Where these instructions conflict with the RULES above, follow the instructions.
"""

# Dismiss keywords the user can type to exit dialog
DISMISS_KEYWORDS = frozenset(
    {
        "done",
        "continue",
        "go ahead",
        "proceed",
        "that's all",
        "thats all",
        "resume",
        "exit",
        "/done",
        "/continue",
    }
)

# Marker the agent appends to signal it's ready to continue. Treated as a
# terminal control token (must be at the end of the response) to prevent
# false positives if the agent quotes the marker mid-response.
_READY_MARKER = "[READY_TO_CONTINUE]"


def _reads_multiline_turn() -> bool:
    """Whether a conversational dialog turn is read multi-line.

    Off a tty the turn falls back to a single-line ``Prompt.ask``, where the
    submit sentinel has no effect. Both the reader in
    ``DialogHandler._get_user_input`` and the opening banner's sentinel hint
    ask this, so the banner can never advertise a keystroke the reader ignores.
    """
    return sys.stdin.isatty()


def _dismiss_instruction() -> Text:
    """How to actually leave the dialog, phrased for the active reader.

    A dismiss keyword is only recognised once the turn is *submitted*, so on a
    tty it needs the sentinel after it. Saying "type done" there describes a
    keystroke that does nothing until the reply is sent -- the exit instruction
    has to move with :func:`_reads_multiline_turn` or it contradicts the very
    banner that advertises the sentinel.

    Returns:
        A pre-styled ``Text``, so a caller splices it through ``styled`` and
        keeps its own template a literal (#406).
    """
    if _reads_multiline_turn():
        return styled(
            "send [bold]done[/bold] or [bold]/done[/bold] with [bold]{}[/bold]",
            DIALOG_SUBMIT_SENTINEL,
        )
    return Text.from_markup("say [bold]done[/bold] or [bold]/done[/bold]")


# A sentence that names a question the agent still intends to ask. Checked
# clause by clause over the whole message, and a match is discarded when its
# clause is negated ("no further questions"). The
# list is a backstop behind the prompt rule above, so it leans towards
# keeping the dialog open: the user can always send a dismiss keyword, while
# a dialog ended early cannot be reopened.
_ANNOUNCED_QUESTION_RE = re.compile(
    "|".join(
        (
            r"\bnext question\b",
            r"\bquestion\s+\d+\s*(?:of|/)\s*~?\d+",
            r"\b(?:i|i'll|i will|let me|we|we'll|we can|shall we)\s+"
            r"(?:now\s+|then\s+|also\s+)?ask\b",
            r"\b(?:another|one more|a further|a follow-?up|a final|a last|"
            r"the (?:next|last|final|remaining|second|third))\s+(?:\w+\s+)?question\b",
            r"\b(?:more|further|remaining|outstanding|follow-?up|additional)\s+questions\b",
        )
    ),
    re.IGNORECASE,
)
# A clause that negates the announcement, or says the questions are already
# dealt with ("the remaining questions were answered above"). The participles
# count only after a copula or "all": "one more question I need answered" is
# an announcement.
_NEGATION_RE = re.compile(
    r"\b(?:no|not|nothing|never|without)\b|n't\b"
    r"|\b(?:is|are|was|were|been|all)\s+(?:all\s+)?(?:answered|resolved|addressed|settled)\b",
    re.IGNORECASE,
)
# Negating one of those participles says the question is still open ("I
# haven't asked the next question yet", "one more question isn't settled"),
# so such a clause is not negated.
_STILL_OPEN_RE = re.compile(
    r"(?:\bnot|\bnever|n't)\s+(?:yet\s+|been\s+)?(?:asked|answered|resolved|addressed|settled)\b",
    re.IGNORECASE,
)
# A line break ends a sentence too: a bullet or a heading carries no
# terminal punctuation, and its negation must not reach the next line.
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+|\n")
_CLAUSE_BOUNDARY_RE = re.compile(r"[,;:\u2014\u2013]")
# A URL contains no whitespace, ``<``, ``>``, ``"`` or backtick (RFC 3986), so
# it ends before an autolink's ``>``, a closing quote or a code span's closing
# backtick; it also ends before the ``)`` closing a Markdown link target, and
# never on a ``?``. A ``?`` that ends the token therefore stays in the text
# while one inside the URL's query string is removed with it. A balanced
# ``(...)`` inside the URL is part of it.
_URL_RE = re.compile(
    r"https?://(?:\([^\s()<>\"`]*\)|[^\s()<>\"`])*(?:\([^\s()<>\"`]*\)|[^\s()<>\"`?])"
)


def _asks_or_announces_question(text: str) -> bool:
    """Whether ``text`` asks the user a question or promises one.

    True when the message contains a ``?`` outside a URL, or a clause in
    which :data:`_ANNOUNCED_QUESTION_RE` matches and :data:`_NEGATION_RE`
    does not (or :data:`_STILL_OPEN_RE` does). Every paragraph counts: an agent
    that asks, offers its recommendation and signs off with "Let me know." is
    still asking.
    """
    text = _URL_RE.sub("", text)
    if "?" in text:
        return True
    return any(
        _ANNOUNCED_QUESTION_RE.search(clause)
        and (not _NEGATION_RE.search(clause) or _STILL_OPEN_RE.search(clause))
        for sentence in _SENTENCE_BOUNDARY_RE.split(text)
        for clause in _CLAUSE_BOUNDARY_RE.split(sentence)
    )


def _extract_ready_marker(response: str) -> tuple[bool, str]:
    """Return ``(proposed, cleaned)`` for an agent response.

    ``proposed`` is True only when the marker appears at the very end of the
    (right-stripped) response *and* the message does not read as asking or
    announcing a question (:func:`_asks_or_announces_question`) -- an agent
    that says "ready for the next question" has not finished, whatever it
    appended. ``cleaned`` is the response with a trailing marker removed
    whether or not it was honoured, so the token never reaches the user; a
    mid-response mention is left verbatim. Requiring the terminal position
    avoids both false positives from those mentions and the user-injection
    vector where a user pastes the marker.
    """
    stripped = response.rstrip()
    if not stripped.endswith(_READY_MARKER):
        return False, response
    cleaned = stripped[: -len(_READY_MARKER)].rstrip()
    if _asks_or_announces_question(cleaned):
        logger.debug("Ready marker withheld: the message still asks or announces a question")
        return False, cleaned
    return True, cleaned


def _is_approval(text: str) -> bool:
    """Whether a reply to the continue proposal lets the agent continue.

    Exact ``yes``/``y`` only: "yes, and the next repo is ike" is an answer
    to the agent, not to the dialog, and is sent on as the next turn.
    """
    return text.strip().lower() in ("yes", "y")


def _dismiss_keyword_list() -> str:
    """The dismiss vocabulary as one sorted, comma-separated string."""
    return ", ".join(sorted(DISMISS_KEYWORDS))


def _build_system_prompt(agent: AgentDef, agent_output: dict[str, Any]) -> str:
    """Render the conversing agent's system prompt for one dialog.

    Shared by the terminal and web paths so the author's
    ``dialog.conversation_prompt`` cannot reach one and not the other.
    """
    try:
        # ``ensure_ascii=False`` so the dialog-mode LLM sees non-ASCII
        # output literally instead of as \uXXXX escapes (issue #356).
        output_str = json.dumps(agent_output, indent=2, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        output_str = str(agent_output)

    conversation_prompt = agent.dialog.conversation_prompt if agent.dialog else None
    instructions = (
        _CONVERSATION_INSTRUCTIONS_BLOCK.format(conversation_prompt=conversation_prompt.strip())
        if conversation_prompt and conversation_prompt.strip()
        else ""
    )
    return DIALOG_AGENT_SYSTEM_PROMPT.format(
        agent_name=agent.name,
        agent_output=output_str,
        conversation_instructions=instructions,
    )


@dataclass
class DialogMessage:
    """A single message in a dialog conversation.

    Attributes:
        role: Either 'user' or 'agent'.
        content: The message content.
    """

    role: str
    content: str


@dataclass
class DialogResult:
    """Result of a dialog session.

    Attributes:
        dialog_id: Unique identifier for this dialog session.
        messages: Full transcript of the dialog conversation.
        user_dismissed: Whether the user explicitly dismissed the dialog.
        user_declined: Whether the user declined to engage at all.
        agent_proposed_continue: Whether the agent proposed continuing.
        agent_question_outstanding: Whether the dialog ended while the
            agent's last message was still asking, or announcing, a question.
    """

    dialog_id: str
    messages: list[DialogMessage] = field(default_factory=list)
    user_dismissed: bool = False
    user_declined: bool = False
    agent_proposed_continue: bool = False
    agent_question_outstanding: bool = False

    def last_agent_message_asks_question(self) -> bool:
        """Whether the most recent agent message asks or announces a question."""
        for message in reversed(self.messages):
            if message.role == "agent":
                return _asks_or_announces_question(message.content)
        return False


class DialogHandler:
    """Handles interactive dialog sessions between agents and users.

    Presents the agent's full context (output, file paths, reasoning)
    and manages a multi-turn conversation until the user or agent
    concludes the dialog.

    Example::

        handler = DialogHandler()
        result = await handler.handle_dialog(
            agent=agent_def,
            agent_output={"result": "analysis complete", "files": [...]},
            opening_question="I found some ambiguity in the requirements...",
            provider=copilot_provider,
        )
    """

    def __init__(
        self,
        console: MarkupFreeConsole | None = None,
        skip_dialogs: bool = False,
        emitter: WorkflowEventEmitter | None = None,
        web_dashboard: WebDashboard | None = None,
    ) -> None:
        """Initialize the DialogHandler.

        Args:
            console: Rich console for output. Creates one if not provided.
            skip_dialogs: If True, auto-skip all dialogs (for CI/automation).
            emitter: Optional event emitter for dialog events.
            web_dashboard: Optional web dashboard for web-based dialog input.
        """
        self.console = console or make_console()
        self.skip_dialogs = skip_dialogs
        self.emitter = emitter
        self.web_dashboard = web_dashboard

    async def handle_dialog(
        self,
        agent: AgentDef,
        agent_output: dict[str, Any],
        opening_question: str,
        provider: AgentProvider,
        base_dir: Path | None = None,
    ) -> DialogResult:
        """Run an interactive dialog session with the user.

        Presents the agent's full output and opening question, then
        manages a multi-turn conversation until conclusion.

        Args:
            agent: The agent definition that triggered dialog.
            agent_output: The agent's complete output (shown to user as context).
            opening_question: The evaluator-extracted opening question.
            provider: The provider for generating agent responses.
            base_dir: Optional directory for resolving file paths in output.

        Returns:
            DialogResult with the full conversation transcript.
        """
        dialog_id = str(uuid.uuid4())[:8]
        result = DialogResult(dialog_id=dialog_id)

        if self.skip_dialogs:
            logger.info("Dialog skipped for agent '%s' (skip_dialogs=True)", agent.name)
            result.user_declined = True
            return result

        # Dispatch to web mode if dashboard is available
        if self.web_dashboard is not None:
            return await self._web_handle_dialog(
                agent=agent,
                agent_output=agent_output,
                opening_question=opening_question,
                provider=provider,
                dialog_id=dialog_id,
                result=result,
            )

        self._emit_event(
            "dialog_started",
            {
                "dialog_id": dialog_id,
                "agent_name": agent.name,
                "opening_question": opening_question,
            },
        )

        system_prompt = _build_system_prompt(agent, agent_output)

        # Display full context and the opening question to the user
        self._display_dialog_start(agent, agent_output, opening_question, base_dir)

        # Record the opening question as the first agent message
        result.messages.append(DialogMessage(role="agent", content=opening_question))
        self._emit_event(
            "dialog_message",
            {
                "dialog_id": dialog_id,
                "agent_name": agent.name,
                "role": "agent",
                "content": opening_question,
            },
        )

        # Ask user if they want to engage or let the agent continue on its own
        engagement = await self._ask_engagement()
        if engagement == "decline":
            result.user_declined = True
            self._display_dialog_end(dismissed_by="declined")
            self._emit_event(
                "dialog_completed",
                {
                    "dialog_id": dialog_id,
                    "agent_name": agent.name,
                    "turn_count": len(result.messages),
                    "user_declined": True,
                },
            )
            return result

        # Track conversation history for the provider
        history: list[dict[str, str]] = []
        # A reply to the continue proposal that was neither approval nor
        # dismissal: it is the next turn, already recorded, and must not be
        # read again.
        pending_turn: str | None = None

        # Dialog loop
        while True:
            # Get user input
            if pending_turn is not None:
                user_input, pending_turn = pending_turn, None
            else:
                user_input = await self._get_user_input()

                if user_input is None:
                    # EOF or error
                    result.user_dismissed = True
                    break

                if not user_input.strip():
                    # Empty submission -- not a turn, and not dismissal either.
                    # On a tty this is a bare sentinel line; off a tty it is a
                    # blank line from the pipe, which ``Prompt.ask`` returns
                    # as "".
                    continue

                result.messages.append(DialogMessage(role="user", content=user_input))
                self._emit_event(
                    "dialog_message",
                    {
                        "dialog_id": dialog_id,
                        "agent_name": agent.name,
                        "role": "user",
                        "content": user_input,
                    },
                )

                # Check if user is dismissing the dialog
                if self._is_dismiss(user_input):
                    result.user_dismissed = True
                    self._display_dialog_end(dismissed_by="user")
                    break

            # Send to agent and get response
            history.append({"role": "user", "content": user_input})
            try:
                agent_response = await provider.execute_dialog_turn(
                    system_prompt=system_prompt,
                    user_message=user_input,
                    history=history[:-1],  # History excludes current message
                    model=agent.model,
                )
            except Exception:
                # Roll back the user turn so the next attempt doesn't leave two
                # consecutive user messages in the provider context.
                history.pop()
                logger.warning(
                    "Dialog turn failed for agent '%s'",
                    agent.name,
                    exc_info=True,
                )
                self.console.print(
                    styled(
                        "[dim red]  (Agent response failed — you can continue, or {})[/dim red]",
                        _dismiss_instruction(),
                    )
                )
                continue

            history.append({"role": "assistant", "content": agent_response})
            ready_proposed, stored_response = _extract_ready_marker(agent_response)
            result.messages.append(DialogMessage(role="agent", content=stored_response))
            self._emit_event(
                "dialog_message",
                {
                    "dialog_id": dialog_id,
                    "agent_name": agent.name,
                    "role": "agent",
                    "content": stored_response,
                },
            )
            self._display_agent_message(stored_response)

            # Check if agent proposed completion (terminal marker only)
            if ready_proposed:
                result.agent_proposed_continue = True
                self._display_continue_proposal(agent.name)

                # Ask user if they approve. Only an explicit "yes", EOF, or a
                # dismiss keyword ends the dialog; anything else is the user's
                # next turn. An empty reply re-asks rather than counting as
                # approval, since Enter under a question is how an interview
                # gets ended by accident.
                while True:
                    approval = await self._get_user_input(
                        prompt_text=styled(
                            "[bold]Let {} continue?[/bold] ([green]yes[/green] ends the"
                            " dialog; anything else is sent to {})",
                            agent.name,
                            agent.name,
                        )
                    )
                    if approval is None or approval.strip():
                        break
                if approval is None or _is_approval(approval):
                    self._display_dialog_end(dismissed_by="agent_approved")
                    break
                result.messages.append(DialogMessage(role="user", content=approval))
                self._emit_event(
                    "dialog_message",
                    {
                        "dialog_id": dialog_id,
                        "agent_name": agent.name,
                        "role": "user",
                        "content": approval,
                    },
                )
                if self._is_dismiss(approval):
                    result.user_dismissed = True
                    self._display_dialog_end(dismissed_by="user")
                    break
                # The reply is the user's next turn; the loop top sends it.
                pending_turn = approval
                continue

        result.agent_question_outstanding = result.last_agent_message_asks_question()
        if result.agent_question_outstanding:
            self._display_question_outstanding(agent.name)

        self._emit_event(
            "dialog_completed",
            {
                "dialog_id": dialog_id,
                "agent_name": agent.name,
                "turn_count": len(result.messages),
                "user_dismissed": result.user_dismissed,
                "agent_proposed_continue": result.agent_proposed_continue,
                "agent_question_outstanding": result.agent_question_outstanding,
            },
        )

        return result

    async def _web_handle_dialog(
        self,
        agent: AgentDef,
        agent_output: dict[str, Any],
        opening_question: str,
        provider: AgentProvider,
        dialog_id: str,
        result: DialogResult,
    ) -> DialogResult:
        """Run a dialog session with input from the web dashboard.

        Events are already emitted by the regular flow. This method replaces
        CLI prompts with web dashboard WebSocket communication.
        """
        assert self.web_dashboard is not None

        self._emit_event(
            "dialog_started",
            {
                "dialog_id": dialog_id,
                "agent_name": agent.name,
                "opening_question": opening_question,
            },
        )

        system_prompt = _build_system_prompt(agent, agent_output)

        # Record the opening question as the first agent message
        result.messages.append(DialogMessage(role="agent", content=opening_question))
        self._emit_event(
            "dialog_message",
            {
                "dialog_id": dialog_id,
                "agent_name": agent.name,
                "role": "agent",
                "content": opening_question,
            },
        )

        # Wait for engagement decision from web client
        msg = await self.web_dashboard.wait_for_dialog_message(agent.name, dialog_id)
        if msg.get("type") == "dialog_decline":
            result.user_declined = True
            self._emit_event(
                "dialog_completed",
                {
                    "dialog_id": dialog_id,
                    "agent_name": agent.name,
                    "turn_count": len(result.messages),
                    "user_declined": True,
                },
            )
            return result

        # First message content from the user (engagement + first input)
        user_input = str(msg.get("content") or "")
        history: list[dict[str, str]] = []

        # Process first user message
        result.messages.append(DialogMessage(role="user", content=user_input))
        self._emit_event(
            "dialog_message",
            {
                "dialog_id": dialog_id,
                "agent_name": agent.name,
                "role": "user",
                "content": user_input,
            },
        )

        if self._is_dismiss(user_input):
            # Skips the loop and reaches the shared completion below, so the
            # event and the result carry the same fields as any other exit.
            result.user_dismissed = True

        # Dialog loop
        while not result.user_dismissed:
            # Send to agent and get response
            history.append({"role": "user", "content": user_input})
            try:
                agent_response = await provider.execute_dialog_turn(
                    system_prompt=system_prompt,
                    user_message=user_input,
                    history=history[:-1],
                    model=agent.model,
                )
            except Exception:
                # Roll back the user turn so the next attempt doesn't leave two
                # consecutive user messages in the provider context.
                history.pop()
                logger.warning(
                    "Dialog turn failed for agent '%s'",
                    agent.name,
                    exc_info=True,
                )
                # Emit a failure message so user knows
                self._emit_event(
                    "dialog_message",
                    {
                        "dialog_id": dialog_id,
                        "agent_name": agent.name,
                        "role": "agent",
                        "content": "(Agent response failed — you can continue or type 'done')",
                    },
                )
                # Wait for next user message
                msg = await self.web_dashboard.wait_for_dialog_message(agent.name, dialog_id)
                if msg.get("type") == "dialog_decline":
                    result.user_dismissed = True
                    break
                user_input = str(msg.get("content") or "")
                result.messages.append(DialogMessage(role="user", content=user_input))
                self._emit_event(
                    "dialog_message",
                    {
                        "dialog_id": dialog_id,
                        "agent_name": agent.name,
                        "role": "user",
                        "content": user_input,
                    },
                )
                if self._is_dismiss(user_input):
                    result.user_dismissed = True
                    break
                continue

            history.append({"role": "assistant", "content": agent_response})
            ready_proposed, stored_response = _extract_ready_marker(agent_response)
            result.messages.append(DialogMessage(role="agent", content=stored_response))

            # Check if agent proposed completion (terminal marker only)
            if ready_proposed:
                result.agent_proposed_continue = True
                self._emit_event(
                    "dialog_message",
                    {
                        "dialog_id": dialog_id,
                        "agent_name": agent.name,
                        "role": "agent",
                        "content": stored_response
                        + f"\n\n*{agent.name} believes it has enough information to"
                        f" continue. Reply **yes** to end the dialog and let it continue;"
                        f" anything else is sent to {agent.name}.*",
                    },
                )
                # Wait for approval or continuation. Mirrors the terminal
                # path: "yes", a decline, or a dismiss keyword ends the dialog,
                # an empty message is ignored, anything else is the next turn.
                while True:
                    msg = await self.web_dashboard.wait_for_dialog_message(agent.name, dialog_id)
                    approval = str(msg.get("content") or "")
                    if msg.get("type") == "dialog_decline" or approval.strip():
                        break
                if msg.get("type") == "dialog_decline":
                    # The dashboard's leave-dialog control, as elsewhere on
                    # this path; only "yes" is the agent's approval.
                    result.user_dismissed = True
                    break
                if _is_approval(approval):
                    break
                # The user's reply is the next user turn. The loop top will
                # append it to provider history exactly once; we only update
                # the transcript / UI here.
                user_input = approval
                result.messages.append(DialogMessage(role="user", content=approval))
                self._emit_event(
                    "dialog_message",
                    {
                        "dialog_id": dialog_id,
                        "agent_name": agent.name,
                        "role": "user",
                        "content": approval,
                    },
                )
                if self._is_dismiss(approval):
                    result.user_dismissed = True
                    break
                continue

            self._emit_event(
                "dialog_message",
                {
                    "dialog_id": dialog_id,
                    "agent_name": agent.name,
                    "role": "agent",
                    "content": stored_response,
                },
            )

            # Wait for next user message
            msg = await self.web_dashboard.wait_for_dialog_message(agent.name, dialog_id)
            if msg.get("type") == "dialog_decline":
                result.user_dismissed = True
                break
            user_input = str(msg.get("content") or "")
            result.messages.append(DialogMessage(role="user", content=user_input))
            self._emit_event(
                "dialog_message",
                {
                    "dialog_id": dialog_id,
                    "agent_name": agent.name,
                    "role": "user",
                    "content": user_input,
                },
            )

            if self._is_dismiss(user_input):
                result.user_dismissed = True
                break

        result.agent_question_outstanding = result.last_agent_message_asks_question()
        self._emit_event(
            "dialog_completed",
            {
                "dialog_id": dialog_id,
                "agent_name": agent.name,
                "turn_count": len(result.messages),
                "user_dismissed": result.user_dismissed,
                "agent_proposed_continue": result.agent_proposed_continue,
                "agent_question_outstanding": result.agent_question_outstanding,
            },
        )

        return result

    def _display_dialog_start(
        self,
        agent: AgentDef,
        agent_output: dict[str, Any],
        opening_question: str,
        base_dir: Path | None = None,
    ) -> None:
        """Display the dialog opening with full agent context."""
        # Advertised only where the sentinel applies -- see
        # _reads_multiline_turn. Pre-rendered as a Text so the outer template
        # has fixed arity: styled() splices a Text in with its own spans
        # re-anchored, and raises IndexError on a template/argument mismatch
        # that only the tty branch would reach. Off a tty the sentence keeps
        # its original plural, since each line really is a separate response
        # there.
        instruction = (
            styled(
                "Type your response below. It can span multiple lines; send it"
                " with [bold]{}[/bold] on its own line. A dismiss keyword ends"
                " the dialog the same way: send [bold]done[/bold] or"
                " [bold]/done[/bold] with [bold]{}[/bold]. A reply that is only"
                " one of {} also ends it.",
                DIALOG_SUBMIT_SENTINEL,
                DIALOG_SUBMIT_SENTINEL,
                _dismiss_keyword_list(),
            )
            if _reads_multiline_turn()
            # Off a tty every line is a turn already, so a dismiss keyword
            # needs nothing after it.
            else styled(
                "Type your responses below. Say [bold]done[/bold] or "
                "[bold]/done[/bold] when finished. A reply that is only one of"
                " {} also ends the dialog.",
                _dismiss_keyword_list(),
            )
        )

        self.console.print()
        self.console.print(
            Panel(
                styled(
                    "[bold]Agent '{}'[/bold] would like to discuss "
                    "its output with you.\n"
                    "[dim]{}[/dim]",
                    agent.name,
                    instruction,
                ),
                title=Text.from_markup("[bold magenta]Dialog Mode[/bold magenta]"),
                border_style="magenta",
            )
        )

        # Show agent output with full context
        try:
            # ``ensure_ascii=False`` so the console panel shows real non-ASCII
            # text instead of \uXXXX escapes (issue #356).
            output_str = json.dumps(agent_output, indent=2, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            output_str = str(agent_output)

        # Linkify file paths in the output for clickable links
        output_display = linkify_markdown(output_str, base_dir=base_dir)

        self.console.print()
        self.console.print(
            Panel(
                RichMarkdown(f"```json\n{output_display}\n```"),
                title=Text.from_markup("[bold cyan]Agent Output (Full Context)[/bold cyan]"),
                border_style="cyan",
                expand=True,
            )
        )

        # Show the opening question
        self.console.print()
        question_display = linkify_markdown(opening_question, base_dir=base_dir)
        self.console.print(
            Panel(
                RichMarkdown(question_display),
                title=styled("[bold yellow]{}[/bold yellow]", agent.name),
                border_style="yellow",
            )
        )

    def _display_agent_message(self, message: str) -> None:
        """Display an agent message in the dialog."""
        self.console.print()
        self.console.print(
            Panel(
                RichMarkdown(message),
                border_style="yellow",
            )
        )

    def _display_continue_proposal(self, agent_name: str) -> None:
        """Display the agent's proposal to continue, naming the agent.

        The line sits directly under the agent's message, so it says who is
        speaking: without the name, "Continue?" reads as the agent's own
        follow-up and a conversational "yes" ends the interview.
        """
        self.console.print()
        self.console.print(
            styled(
                "[bold magenta]  ↳ {} believes it has enough information to continue."
                "[/bold magenta]",
                agent_name,
            )
        )

    def _display_question_outstanding(self, agent_name: str) -> None:
        """Say that the dialog closed on an unanswered question from the agent."""
        self.console.print(
            styled(
                "[bold yellow]  ! {} still had a question outstanding when the dialog"
                " ended.[/bold yellow]",
                agent_name,
            )
        )
        self.console.print()

    def _display_dialog_end(self, dismissed_by: str) -> None:
        """Display dialog conclusion message."""
        self.console.print()
        if dismissed_by == "user":
            self.console.print(
                Text.from_markup(
                    "[dim magenta]  ✓ Dialog ended by user — agent resuming.[/dim magenta]"
                )
            )
        elif dismissed_by == "agent_approved":
            self.console.print(
                Text.from_markup(
                    "[dim magenta]  ✓ Agent continuing — dialog complete.[/dim magenta]"
                )
            )
        elif dismissed_by == "declined":
            self.console.print(
                Text.from_markup(
                    "[dim magenta]  ✓ Dialog declined — agent will do"
                    " its best and continue.[/dim magenta]"
                )
            )
        self.console.print()

    async def _ask_engagement(self) -> str:
        """Ask the user whether they want to engage in the dialog.

        Returns:
            "engage" if the user wants to chat, "decline" to skip.
        """
        self.console.print()
        self.console.print(Text.from_markup("[bold]How would you like to proceed?[/bold]"))
        self.console.print(Text.from_markup("  [cyan][1][/cyan] Discuss this with the agent"))
        self.console.print(
            Text.from_markup(
                "  [cyan][2][/cyan] Do your best and continue [dim](skip dialog)[/dim]"
            )
        )

        def _ask() -> str:
            return Prompt.ask(
                Text.from_markup("\n[bold]Select[/bold]"),
                choices=["1", "2"],
                default="1",
                show_choices=True,
            )

        # A daemon thread for the same reason as _get_user_input: a cancelled
        # ``asyncio.to_thread`` leaves its worker blocked in ``input()``.
        choice = await read_on_daemon_thread(_ask)
        return "engage" if choice == "1" else "decline"

    async def _get_user_input(
        self,
        prompt_text: Text | None = None,
    ) -> str | None:
        """Get user input from the terminal.

        Runs in a thread to avoid blocking the event loop.

        Args:
            prompt_text: Pre-styled prompt. ``Text`` rather than ``str``
                because ``Prompt`` parses a ``str`` prompt with
                ``Text.from_markup`` regardless of the console's
                ``markup=False`` (#406), so the type keeps a caller from
                passing an interpolated f-string here.

        Returns:
            User input text, or None on EOF/error, which the caller treats as
            dismissal. The main turn (``prompt_text is None`` on a tty) reads
            multi-line, so an EOF that *terminates a paste* returns the
            accumulated content rather than dismissing; an EOF with nothing
            but whitespace accumulated is a deliberate Ctrl-D and still
            returns None.
        """
        if prompt_text is None and _reads_multiline_turn():
            self.console.print(styled("[bold magenta]You[/bold magenta]"))
            try:
                text, hit_eof = await read_on_daemon_thread(
                    lambda: read_multiline_lines(self.console, sentinel=DIALOG_SUBMIT_SENTINEL)
                )
            except (EOFError, KeyboardInterrupt):
                return None
            if hit_eof and not text.strip():
                # Ctrl-D at an empty prompt: the user is leaving, not pasting.
                return None
            return text

        prompt = styled("[bold magenta]You[/bold magenta]") if prompt_text is None else prompt_text
        try:
            # A daemon thread, not ``asyncio.to_thread``: a Ctrl-C here cancels
            # this task while the worker stays blocked in ``input()``, and the
            # loop's shutdown then joins that worker forever (see
            # read_on_daemon_thread).
            return await read_on_daemon_thread(lambda: Prompt.ask(prompt))
        except (EOFError, KeyboardInterrupt):
            return None

    def _is_dismiss(self, text: str) -> bool:
        """Check if user input is a dismiss signal."""
        return text.strip().lower() in DISMISS_KEYWORDS

    def _emit_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Emit a dialog event if emitter is available."""
        if self.emitter is not None:
            import time

            from conductor.events import WorkflowEvent

            self.emitter.emit(
                WorkflowEvent(
                    type=event_type,
                    timestamp=time.time(),
                    data=data,
                )
            )
