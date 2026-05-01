"""Dialog handler for agent-initiated user conversations.

This module implements the interactive dialog mode where an agent pauses
after execution and enters a free-form conversation with the user.
The dialog presents full context (output, file paths, reasoning) and
supports multi-turn exchanges until the user or agent concludes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.markdown import Markdown as RichMarkdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from conductor.executor.linkify import linkify_markdown

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
  exact marker [READY_TO_CONTINUE] at the end of your message
- If the user says "done", "continue", or "go ahead", treat that as \
  permission to stop discussing

--- AGENT OUTPUT TO DISCUSS ---
{agent_output}
--- END AGENT OUTPUT ---
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
    """

    dialog_id: str
    messages: list[DialogMessage] = field(default_factory=list)
    user_dismissed: bool = False
    user_declined: bool = False
    agent_proposed_continue: bool = False


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
        console: Console | None = None,
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
        self.console = console or Console()
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

        # Build the system prompt with full agent output context
        try:
            output_str = json.dumps(agent_output, indent=2, default=str)
        except (TypeError, ValueError):
            output_str = str(agent_output)

        system_prompt = DIALOG_AGENT_SYSTEM_PROMPT.format(
            agent_name=agent.name, agent_output=output_str
        )

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

        # Dialog loop
        while True:
            # Get user input
            user_input = await self._get_user_input()

            if user_input is None:
                # EOF or error
                result.user_dismissed = True
                break

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
                logger.warning(
                    "Dialog turn failed for agent '%s'",
                    agent.name,
                    exc_info=True,
                )
                self.console.print(
                    "[dim red]  (Agent response failed — you can continue or type 'done')[/dim red]"
                )
                continue

            history.append({"role": "assistant", "content": agent_response})
            result.messages.append(DialogMessage(role="agent", content=agent_response))
            self._emit_event(
                "dialog_message",
                {
                    "dialog_id": dialog_id,
                    "agent_name": agent.name,
                    "role": "agent",
                    "content": agent_response,
                },
            )

            # Check if agent proposed completion
            if "[READY_TO_CONTINUE]" in agent_response:
                result.agent_proposed_continue = True
                clean_response = agent_response.replace("[READY_TO_CONTINUE]", "").strip()
                self._display_agent_message(clean_response)
                self._display_continue_proposal()

                # Ask user if they approve
                approval = await self._get_user_input(
                    prompt_text="[bold]Continue?[/bold] ([green]yes[/green]/no)"
                )
                if approval is None or approval.lower() in ("yes", "y", ""):
                    self._display_dialog_end(dismissed_by="agent_approved")
                    break
                # User wants to keep chatting
                history.append({"role": "user", "content": approval})
                result.messages.append(DialogMessage(role="user", content=approval))
                continue

            self._display_agent_message(agent_response)

        self._emit_event(
            "dialog_completed",
            {
                "dialog_id": dialog_id,
                "agent_name": agent.name,
                "turn_count": len(result.messages),
                "user_dismissed": result.user_dismissed,
                "agent_proposed_continue": result.agent_proposed_continue,
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

        # Build the system prompt with full agent output context
        try:
            output_str = json.dumps(agent_output, indent=2, default=str)
        except (TypeError, ValueError):
            output_str = str(agent_output)

        system_prompt = DIALOG_AGENT_SYSTEM_PROMPT.format(
            agent_name=agent.name, agent_output=output_str
        )

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
        user_input = msg.get("content", "")
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
            result.user_dismissed = True
            self._emit_event(
                "dialog_completed",
                {
                    "dialog_id": dialog_id,
                    "agent_name": agent.name,
                    "turn_count": len(result.messages),
                    "user_dismissed": True,
                },
            )
            return result

        # Dialog loop
        while True:
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
                user_input = msg.get("content", "")
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
            result.messages.append(DialogMessage(role="agent", content=agent_response))

            # Check if agent proposed completion
            if "[READY_TO_CONTINUE]" in agent_response:
                result.agent_proposed_continue = True
                clean_response = agent_response.replace("[READY_TO_CONTINUE]", "").strip()
                self._emit_event(
                    "dialog_message",
                    {
                        "dialog_id": dialog_id,
                        "agent_name": agent.name,
                        "role": "agent",
                        "content": clean_response
                        + "\n\n*The agent believes it has enough information to continue.*",
                    },
                )
                # Wait for approval or continuation
                msg = await self.web_dashboard.wait_for_dialog_message(agent.name, dialog_id)
                if msg.get("type") == "dialog_decline":
                    break
                approval = msg.get("content", "")
                if approval.lower() in ("yes", "y", ""):
                    break
                # User wants to keep chatting
                user_input = approval
                history.append({"role": "user", "content": approval})
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
                continue

            self._emit_event(
                "dialog_message",
                {
                    "dialog_id": dialog_id,
                    "agent_name": agent.name,
                    "role": "agent",
                    "content": agent_response,
                },
            )

            # Wait for next user message
            msg = await self.web_dashboard.wait_for_dialog_message(agent.name, dialog_id)
            if msg.get("type") == "dialog_decline":
                result.user_dismissed = True
                break
            user_input = msg.get("content", "")
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

        self._emit_event(
            "dialog_completed",
            {
                "dialog_id": dialog_id,
                "agent_name": agent.name,
                "turn_count": len(result.messages),
                "user_dismissed": result.user_dismissed,
                "agent_proposed_continue": result.agent_proposed_continue,
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
        self.console.print()
        self.console.print(
            Panel(
                Text.from_markup(
                    f"[bold]Agent '{agent.name}'[/bold] would like to discuss "
                    f"its output with you.\n"
                    f"[dim]Type your responses below. Say [bold]done[/bold] or "
                    f"[bold]/done[/bold] when finished.[/dim]"
                ),
                title="[bold magenta]Dialog Mode[/bold magenta]",
                border_style="magenta",
            )
        )

        # Show agent output with full context
        try:
            output_str = json.dumps(agent_output, indent=2, default=str)
        except (TypeError, ValueError):
            output_str = str(agent_output)

        # Linkify file paths in the output for clickable links
        output_display = linkify_markdown(output_str, base_dir=base_dir)

        self.console.print()
        self.console.print(
            Panel(
                RichMarkdown(f"```json\n{output_display}\n```"),
                title="[bold cyan]Agent Output (Full Context)[/bold cyan]",
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
                title=f"[bold yellow]{agent.name}[/bold yellow]",
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

    def _display_continue_proposal(self) -> None:
        """Display the agent's proposal to continue."""
        self.console.print()
        msg = (
            "[bold magenta]  ↳ The agent believes it has enough "
            "information to continue.[/bold magenta]"
        )
        self.console.print(msg)

    def _display_dialog_end(self, dismissed_by: str) -> None:
        """Display dialog conclusion message."""
        self.console.print()
        if dismissed_by == "user":
            self.console.print(
                "[dim magenta]  ✓ Dialog ended by user — agent resuming.[/dim magenta]"
            )
        elif dismissed_by == "agent_approved":
            self.console.print("[dim magenta]  ✓ Agent continuing — dialog complete.[/dim magenta]")
        elif dismissed_by == "declined":
            self.console.print(
                "[dim magenta]  ✓ Dialog declined — agent will do"
                " its best and continue.[/dim magenta]"
            )
        self.console.print()

    async def _ask_engagement(self) -> str:
        """Ask the user whether they want to engage in the dialog.

        Returns:
            "engage" if the user wants to chat, "decline" to skip.
        """
        self.console.print()
        self.console.print("[bold]How would you like to proceed?[/bold]")
        self.console.print("  [cyan][1][/cyan] Discuss this with the agent")
        self.console.print("  [cyan][2][/cyan] Do your best and continue [dim](skip dialog)[/dim]")

        def _ask() -> str:
            return Prompt.ask(
                "\n[bold]Select[/bold]",
                choices=["1", "2"],
                default="1",
                show_choices=True,
            )

        choice = await asyncio.to_thread(_ask)
        return "engage" if choice == "1" else "decline"

    async def _get_user_input(
        self,
        prompt_text: str = "[bold magenta]You[/bold magenta]",
    ) -> str | None:
        """Get user input from the terminal.

        Runs in a thread to avoid blocking the event loop.

        Returns:
            User input text, or None on EOF/error.
        """
        try:

            def _ask() -> str:
                return Prompt.ask(prompt_text)

            return await asyncio.to_thread(_ask)
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
