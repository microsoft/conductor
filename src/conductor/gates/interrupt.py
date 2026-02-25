"""Interrupt handler for interactive workflow interruption.

This module implements the interrupt interaction UI that displays workflow state
and collects user decisions when a workflow is interrupted via Esc or Ctrl+G.
Modeled on ``MaxIterationsHandler`` in ``gates/human.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import IntPrompt, Prompt

logger = logging.getLogger(__name__)

# Maximum length for output preview in the interrupt panel
_OUTPUT_PREVIEW_MAX_LENGTH = 500


class InterruptAction(str, Enum):
    """Actions available when a workflow is interrupted."""

    CONTINUE = "continue_with_guidance"
    """Continue execution with user-provided guidance."""

    SKIP = "skip_to_agent"
    """Skip to a specific agent in the workflow."""

    STOP = "stop"
    """Stop the workflow entirely."""

    CANCEL = "cancel"
    """Cancel the interrupt and resume as-is."""


@dataclass
class InterruptResult:
    """Result of an interrupt interaction.

    Contains the selected action and any associated data (guidance text
    or skip target agent name).
    """

    action: InterruptAction
    """The action the user selected."""

    guidance: str | None = None
    """User-provided guidance text (for CONTINUE action)."""

    skip_target: str | None = None
    """Target agent name (for SKIP action)."""


class InterruptHandler:
    """Handles user interrupt interactions during workflow execution.

    Displays a Rich panel with workflow state and collects user decisions.
    Follows the same visual style as ``MaxIterationsHandler``.

    In ``skip_gates`` mode, auto-selects cancel without prompting (for
    automation and testing).

    Example:
        >>> handler = InterruptHandler()
        >>> result = await handler.handle_interrupt(
        ...     current_agent="summarizer",
        ...     iteration=3,
        ...     last_output_preview='{"summary": "..."}',
        ...     available_agents=["researcher", "summarizer", "reviewer"],
        ...     accumulated_guidance=["Focus on Python 3 only"],
        ... )
        >>> print(f"Action: {result.action}")
    """

    def __init__(
        self,
        console: Console | None = None,
        skip_gates: bool = False,
    ) -> None:
        """Initialize the InterruptHandler.

        Args:
            console: Rich console for output. Creates one if not provided.
            skip_gates: If True, auto-selects cancel without prompting.
        """
        self.console = console or Console()
        self.skip_gates = skip_gates

    async def handle_interrupt(
        self,
        current_agent: str,
        iteration: int,
        last_output_preview: str | None,
        available_agents: list[str],
        accumulated_guidance: list[str],
    ) -> InterruptResult:
        """Handle an interrupt interaction.

        Displays the interrupt panel with workflow state and collects
        the user's decision.

        Args:
            current_agent: Name of the current/last agent.
            iteration: Current iteration number.
            last_output_preview: Preview of the last agent's output (may be None).
            available_agents: List of top-level agent names available for skip.
            accumulated_guidance: List of previously provided guidance entries.

        Returns:
            InterruptResult with the user's selected action and any data.
        """
        if self.skip_gates:
            self.console.print("\n[dim]Interrupt received. Auto-cancelling (--skip-gates)[/dim]")
            logger.debug("Interrupt auto-cancelled due to skip_gates mode")
            return InterruptResult(action=InterruptAction.CANCEL)

        # Display the interrupt panel
        self._display_interrupt_panel(
            current_agent, iteration, last_output_preview, accumulated_guidance
        )

        # Collect user action
        return await self._collect_action(available_agents)

    def _display_interrupt_panel(
        self,
        current_agent: str,
        iteration: int,
        last_output_preview: str | None,
        accumulated_guidance: list[str],
    ) -> None:
        """Display the interrupt panel with workflow state.

        Args:
            current_agent: Name of the current/last agent.
            iteration: Current iteration number.
            last_output_preview: Preview of the last agent's output.
            accumulated_guidance: List of previously provided guidance entries.
        """
        content_lines = [
            f"[bold]Current Agent:[/bold] {current_agent}",
            f"[bold]Iteration:[/bold] {iteration}",
        ]

        # Add output preview if available
        if last_output_preview:
            truncated = last_output_preview[:_OUTPUT_PREVIEW_MAX_LENGTH]
            if len(last_output_preview) > _OUTPUT_PREVIEW_MAX_LENGTH:
                truncated += "..."
            content_lines.append("")
            content_lines.append("[bold]Last Output Preview:[/bold]")
            content_lines.append(f"  {escape(truncated)}")

        # Add accumulated guidance if any
        if accumulated_guidance:
            content_lines.append("")
            content_lines.append("[bold]Previous Guidance:[/bold]")
            for i, guidance in enumerate(accumulated_guidance, 1):
                content_lines.append(f"  {i}. {escape(guidance)}")

        # Add action options
        content_lines.append("")
        content_lines.append("[bold]Actions:[/bold]")
        content_lines.append("  [cyan][1][/cyan] Continue with guidance")
        content_lines.append("  [cyan][2][/cyan] Skip to agent...")
        content_lines.append("  [cyan][3][/cyan] Stop workflow")
        content_lines.append("  [cyan][4][/cyan] Cancel (resume as-is)")

        self.console.print()
        self.console.print(
            Panel(
                "\n".join(content_lines),
                title="[bold yellow]Workflow Interrupted[/bold yellow]",
                border_style="yellow",
            )
        )

    async def _collect_action(self, available_agents: list[str]) -> InterruptResult:
        """Collect the user's action selection.

        Args:
            available_agents: List of top-level agent names for skip validation.

        Returns:
            InterruptResult with the selected action and associated data.
        """
        while True:
            try:
                choice = IntPrompt.ask(
                    "\n[bold]Select action[/bold]",
                    choices=["1", "2", "3", "4"],
                    show_choices=True,
                )
            except (KeyboardInterrupt, EOFError):
                return InterruptResult(action=InterruptAction.CANCEL)

            if choice == 1:
                return await self._collect_guidance()
            elif choice == 2:
                result = await self._collect_skip_target(available_agents)
                if result is not None:
                    return result
                # If None, re-prompt (user cancelled skip selection)
            elif choice == 3:
                self.console.print("\n[yellow]Stopping workflow execution[/yellow]")
                return InterruptResult(action=InterruptAction.STOP)
            elif choice == 4:
                self.console.print("\n[green]Resuming workflow[/green]")
                return InterruptResult(action=InterruptAction.CANCEL)

    async def _collect_guidance(self) -> InterruptResult:
        """Collect guidance text from the user.

        Returns:
            InterruptResult with CONTINUE action and guidance text.
        """
        self.console.print()
        try:
            guidance = Prompt.ask("[bold]Enter guidance for subsequent agents[/bold]")
        except (KeyboardInterrupt, EOFError):
            return InterruptResult(action=InterruptAction.CANCEL)

        if not guidance.strip():
            self.console.print("[yellow]No guidance provided. Resuming as-is.[/yellow]")
            return InterruptResult(action=InterruptAction.CANCEL)

        guidance = guidance.strip()
        self.console.print(f"\n[green]Guidance added:[/green] {guidance}")
        return InterruptResult(action=InterruptAction.CONTINUE, guidance=guidance)

    async def _collect_skip_target(self, available_agents: list[str]) -> InterruptResult | None:
        """Collect and validate the skip target agent.

        Displays available agents and validates the user's selection.
        Re-prompts on invalid agent names.

        Args:
            available_agents: List of valid top-level agent names.

        Returns:
            InterruptResult with SKIP action and target, or None if user cancels.
        """
        if not available_agents:
            self.console.print("[red]No agents available to skip to.[/red]")
            return None

        # Display available agents
        self.console.print()
        self.console.print("[bold]Available agents:[/bold]")
        for i, agent_name in enumerate(available_agents, 1):
            self.console.print(f"  [cyan][{i}][/cyan] {agent_name}")

        while True:
            try:
                target = Prompt.ask(
                    "\n[bold]Enter agent name or number (or 'back' to go back)[/bold]"
                )
            except (KeyboardInterrupt, EOFError):
                return None

            target = target.strip()

            if target.lower() == "back":
                return None

            # Allow selection by number
            try:
                index = int(target) - 1
                if 0 <= index < len(available_agents):
                    selected = available_agents[index]
                    self.console.print(f"\n[green]Skipping to agent:[/green] {selected}")
                    return InterruptResult(action=InterruptAction.SKIP, skip_target=selected)
            except ValueError:
                pass

            # Allow selection by name
            if target in available_agents:
                self.console.print(f"\n[green]Skipping to agent:[/green] {target}")
                return InterruptResult(action=InterruptAction.SKIP, skip_target=target)

            self.console.print(
                f"[red]Agent '{target}' not found. "
                f"Available agents: {', '.join(available_agents)}[/red]"
            )
