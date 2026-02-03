"""Human gate handler for interactive workflow decisions.

This module implements human-in-the-loop gates that pause workflow execution
for user selection via Rich interactive prompts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.panel import Panel
from rich.prompt import IntPrompt, Prompt

from conductor.exceptions import HumanGateError
from conductor.executor.template import TemplateRenderer

if TYPE_CHECKING:
    from conductor.config.schema import AgentDef, GateOption


@dataclass
class GateResult:
    """Result of a human gate interaction.

    Contains the selected option, the route to take, and any additional
    input collected via prompt_for.
    """

    selected_option: GateOption
    """The option that was selected."""

    route: str
    """The route to take next."""

    additional_input: dict[str, str] = field(default_factory=dict)
    """Any additional text input collected via prompt_for."""


class HumanGateHandler:
    """Handles human-in-the-loop gate interactions.

    This class displays options to the user via Rich-formatted prompts
    and collects their selection. It also supports --skip-gates mode
    for automation testing.

    Example:
        >>> handler = HumanGateHandler()
        >>> result = await handler.handle_gate(agent, context)
        >>> print(f"User selected: {result.selected_option.label}")
        >>> print(f"Routing to: {result.route}")
    """

    def __init__(
        self,
        console: Console | None = None,
        skip_gates: bool = False,
    ) -> None:
        """Initialize the HumanGateHandler.

        Args:
            console: Rich console for output. Creates one if not provided.
            skip_gates: If True, auto-selects first option without prompting.
        """
        self.console = console or Console()
        self.skip_gates = skip_gates
        self.renderer = TemplateRenderer()

    async def handle_gate(
        self,
        agent: AgentDef,
        context: dict[str, Any],
    ) -> GateResult:
        """Handle a human gate interaction.

        Displays the prompt and options to the user, collects their selection,
        and optionally prompts for additional text input.

        Args:
            agent: The human_gate agent definition.
            context: Current workflow context for template rendering.

        Returns:
            GateResult with selected option, route, and any additional input.

        Raises:
            HumanGateError: If gate has no options or interaction fails.
        """
        if not agent.options:
            raise HumanGateError(
                f"Human gate '{agent.name}' has no options defined",
                suggestion="Add 'options' list to the human_gate agent",
            )

        # Render the prompt with context
        prompt_text = self.renderer.render(agent.prompt, context)

        # If skip_gates is enabled, auto-select first option
        if self.skip_gates:
            return self._auto_select(agent.options[0])

        # Display prompt and options, get user selection
        selected = await self._display_and_select(prompt_text, agent.options)

        # Handle prompt_for if specified
        additional_input: dict[str, str] = {}
        if selected.prompt_for:
            additional_input = await self._collect_additional_input(selected.prompt_for)

        return GateResult(
            selected_option=selected,
            route=selected.route,
            additional_input=additional_input,
        )

    async def _display_and_select(
        self,
        prompt_text: str,
        options: list[GateOption],
    ) -> GateOption:
        """Display prompt and get user selection.

        Uses Rich for beautiful terminal UI with numbered options.

        Args:
            prompt_text: The rendered prompt to display.
            options: List of options to choose from.

        Returns:
            The selected GateOption.
        """
        # Display the prompt in a styled panel
        self.console.print()
        self.console.print(
            Panel(
                prompt_text,
                title="[bold cyan]Decision Required[/bold cyan]",
                border_style="cyan",
            )
        )

        # Display options as numbered list
        self.console.print()
        self.console.print("[bold]Options:[/bold]")
        for i, option in enumerate(options, 1):
            self.console.print(f"  [cyan][{i}][/cyan] {option.label}")

        # Get user selection
        valid_choices = [str(i) for i in range(1, len(options) + 1)]
        while True:
            choice = Prompt.ask(
                "\n[bold]Select option[/bold]",
                choices=valid_choices,
                show_choices=True,
            )
            try:
                index = int(choice) - 1
                if 0 <= index < len(options):
                    selected = options[index]
                    self.console.print(f"\n[green]Selected:[/green] {selected.label}")
                    return selected
            except ValueError:
                pass
            self.console.print("[red]Invalid selection. Please try again.[/red]")

    async def _collect_additional_input(self, field_name: str) -> dict[str, str]:
        """Collect additional text input from user.

        Prompts the user for additional text input as specified by the
        prompt_for field on the selected option.

        Args:
            field_name: The name of the field to prompt for.

        Returns:
            Dictionary with the field name and collected value.
        """
        self.console.print()
        self.console.print(f"[bold]Please provide {field_name}:[/bold]")
        value = Prompt.ask(f"  {field_name}")
        return {field_name: value}

    def _auto_select(self, option: GateOption) -> GateResult:
        """Auto-select an option (for --skip-gates mode).

        In automation mode, this method selects the first option without
        user interaction. Useful for CI/CD pipelines and testing.

        Args:
            option: The option to auto-select (usually the first one).

        Returns:
            GateResult with the auto-selected option.
        """
        self.console.print(f"\n[dim]Auto-selecting: {option.label} (--skip-gates)[/dim]")
        return GateResult(
            selected_option=option,
            route=option.route,
            additional_input={},  # No input collection in skip mode
        )


@dataclass
class MaxIterationsPromptResult:
    """Result of a max iterations limit prompt.

    Contains whether to continue execution and how many additional
    iterations to allow.
    """

    continue_execution: bool
    """Whether to continue execution with additional iterations."""

    additional_iterations: int
    """Number of additional iterations to allow (0 if stopping)."""


class MaxIterationsHandler:
    """Handles max iterations limit prompts.

    When a workflow reaches its max iterations limit, this handler displays
    an interactive prompt allowing the user to specify additional iterations
    or stop execution. In skip_gates mode, it auto-stops without prompting.

    Example:
        >>> handler = MaxIterationsHandler()
        >>> result = await handler.handle_limit_reached(10, 10, ["agent1", "agent2"])
        >>> if result.continue_execution:
        ...     print(f"Continuing with {result.additional_iterations} more iterations")
        ... else:
        ...     print("Stopping workflow")
    """

    def __init__(
        self,
        console: Console | None = None,
        skip_gates: bool = False,
    ) -> None:
        """Initialize the MaxIterationsHandler.

        Args:
            console: Rich console for output. Creates one if not provided.
            skip_gates: If True, auto-stops without prompting (for automation).
        """
        self.console = console or Console()
        self.skip_gates = skip_gates

    async def handle_limit_reached(
        self,
        current_iteration: int,
        max_iterations: int,
        agent_history: list[str],
    ) -> MaxIterationsPromptResult:
        """Prompt user when max iterations limit is reached.

        Displays the current workflow state and prompts the user to specify
        how many additional iterations to allow. If skip_gates is enabled,
        returns immediately with continue_execution=False.

        Args:
            current_iteration: Current number of iterations executed.
            max_iterations: The configured maximum iterations limit.
            agent_history: Ordered list of agent names that were executed.

        Returns:
            MaxIterationsPromptResult with user's decision.
        """
        # In skip_gates mode, auto-stop without prompting
        if self.skip_gates:
            self.console.print("\n[dim]Max iterations reached. Auto-stopping (--skip-gates)[/dim]")
            return MaxIterationsPromptResult(
                continue_execution=False,
                additional_iterations=0,
            )

        # Display the max iterations panel
        self._display_limit_reached_panel(current_iteration, max_iterations, agent_history)

        # Prompt for additional iterations
        additional = await self._prompt_for_additional_iterations()

        if additional > 0:
            self.console.print(
                f"\n[green]Continuing with {additional} additional iteration(s)[/green]"
            )
            return MaxIterationsPromptResult(
                continue_execution=True,
                additional_iterations=additional,
            )
        else:
            self.console.print("\n[yellow]Stopping workflow execution[/yellow]")
            return MaxIterationsPromptResult(
                continue_execution=False,
                additional_iterations=0,
            )

    def _display_limit_reached_panel(
        self,
        current_iteration: int,
        max_iterations: int,
        agent_history: list[str],
    ) -> None:
        """Display the max iterations reached panel.

        Shows the current iteration state and recent agent execution history
        to help the user understand if there's a loop issue.

        Args:
            current_iteration: Current number of iterations executed.
            max_iterations: The configured maximum iterations limit.
            agent_history: Ordered list of agent names that were executed.
        """
        # Build content for the panel
        content_lines = [
            f"Workflow has reached the iteration limit ({current_iteration}/{max_iterations})",
            "",
        ]

        # Show last N agents executed
        last_n = 5
        if agent_history:
            recent_agents = agent_history[-last_n:]
            content_lines.append(f"Last {len(recent_agents)} agents executed:")
            for i, agent_name in enumerate(recent_agents, 1):
                content_lines.append(f"  {i}. {agent_name}")
            content_lines.append("")

        # Check for potential loop (same agent repeated)
        if len(agent_history) >= 3:
            last_agents = agent_history[-3:]
            if len(set(last_agents)) <= 2:
                content_lines.append("[yellow]This may indicate a loop between agents.[/yellow]")

        # Create and display the panel
        self.console.print()
        self.console.print(
            Panel(
                "\n".join(content_lines),
                title="[bold yellow]Max Iterations Reached[/bold yellow]",
                border_style="yellow",
            )
        )

    async def _prompt_for_additional_iterations(self) -> int:
        """Prompt the user for additional iterations.

        Returns:
            Number of additional iterations to allow (0 to stop).
        """
        self.console.print()
        try:
            value = IntPrompt.ask(
                "[bold]How many more iterations would you like to allow?[/bold]",
                default=0,
            )
            return max(0, value)  # Ensure non-negative
        except (ValueError, KeyboardInterrupt):
            return 0
