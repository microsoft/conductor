"""Human gate handler for interactive workflow decisions.

This module implements human-in-the-loop gates that pause workflow execution
for user selection via Rich interactive prompts.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.markdown import Markdown as RichMarkdown
from rich.panel import Panel
from rich.prompt import IntPrompt, Prompt

from conductor.exceptions import HumanGateError
from conductor.executor.linkify import linkify_markdown
from conductor.executor.template import TemplateRenderer

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from conductor.config.schema import AgentDef, GateOption


MULTILINE_SENTINEL = "."
"""Line that terminates a multi-line answer when typed on its own."""


def _eof_key_hint() -> str:
    """Return the platform-appropriate EOF keystroke for display."""
    return "Ctrl-Z then Enter" if sys.platform == "win32" else "Ctrl-D"


async def read_on_daemon_thread[T](fn: Callable[[], T]) -> T:
    """Run a blocking stdin read on a daemon thread and await its result.

    ``asyncio.to_thread`` would be simpler, but cancelling the returned task
    does not stop the worker: it stays blocked in ``input()`` holding a slot in
    the *shared default* executor. The gate flow cancels the losing CLI arm on
    every dashboard-answered prompt, and a ``questions`` node does that once per
    question — so the default executor's slots drain away, every unrelated
    ``asyncio.to_thread`` in the process eventually blocks forever with no
    error, and ``loop.shutdown_default_executor()`` hangs on exit joining them.

    A dedicated daemon thread is abandoned harmlessly instead: it holds no
    shared slot and does not keep the interpreter alive. Same reasoning as
    ``interrupt/listener.py``.

    Args:
        fn: The blocking callable to run.

    Returns:
        Whatever ``fn`` returns.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[T] = loop.create_future()

    def _settle_result(value: T) -> None:
        if not future.done():
            future.set_result(value)

    def _settle_error(exc: BaseException) -> None:
        if not future.done():
            future.set_exception(exc)

    def _runner() -> None:
        try:
            result = fn()
        except BaseException as exc:  # noqa: BLE001 — relayed to the awaiter
            loop.call_soon_threadsafe(_settle_error, exc)
        else:
            loop.call_soon_threadsafe(_settle_result, result)

    threading.Thread(target=_runner, daemon=True, name="conductor-gate-stdin").start()
    return await future


def option_for_value(agent: AgentDef, value: str) -> GateOption:
    """Map a response value back to a gate agent's declared option.

    Lives at module level because routing belongs to whoever owns the
    workflow graph — both the handler and the engine need this mapping, and
    neither should reach into the other for it.

    Args:
        agent: The human_gate agent definition.
        value: The selected choice value.

    Returns:
        The matching GateOption.

    Raises:
        HumanGateError: If no option carries that value.
    """
    for option in agent.options or []:
        if option.value == value:
            return option
    raise HumanGateError(
        f"Gate response value '{value}' does not match any option for gate '{agent.name}'",
        suggestion="Check the option values in the workflow YAML",
    )


@dataclass(frozen=True)
class GateChoice:
    """One selectable choice in a human prompt.

    Deliberately has no ``route`` — routing is a workflow-graph concern, not
    an interaction concern. Callers that route (``human_gate``) keep their own
    ``value -> route`` map; callers that merely record an answer
    (``questions``) would otherwise be forced to invent a meaningless
    sentinel route for every synthesized choice.
    """

    label: str
    """Display text for the choice."""

    value: str
    """Value reported back when this choice is selected."""

    prompt_for: str | None = None
    """Optional field name to collect free text for after selection."""

    multiline: bool = False
    """Whether that free-text collection accepts multi-line input."""


@dataclass(frozen=True)
class GatePrompt:
    """A single human prompt, independent of any workflow graph.

    This is the unit both ``human_gate`` and ``questions`` present. It carries
    only what an interaction needs: who is asking, the rendered text, the
    choices, and the two policy knobs that differ between callers.
    """

    name: str
    """Agent name this prompt is attributed to.

    The name the dashboard and ``conductor gate respond`` address a waiting
    prompt by. A ``questions`` node reuses its node name for every question,
    which is why ``prompt_id`` exists.
    """

    prompt: str
    """Prompt text, already rendered and linkified by the caller."""

    choices: list[GateChoice]
    """Choices to present, in display order."""

    prompt_id: str | None = None
    """Staleness token distinguishing successive prompts under one name.

    ``None`` for a standalone gate, which is never presented back-to-back
    under the same name.
    """

    auto_select: str | None = None
    """Choice value to select under ``--skip-gates``.

    Callers for which auto-selecting would fabricate a human answer (rather
    than take a declared default path) must not reach the prompt at all under
    ``--skip-gates``; they short-circuit instead of setting this.
    """


@dataclass(frozen=True)
class GateResponse:
    """A human's answer to a single :class:`GatePrompt`."""

    value: str
    """The selected choice's value."""

    label: str
    """The selected choice's label."""

    additional_input: dict[str, str] = field(default_factory=dict)
    """Free text collected via the choice's ``prompt_for``, if any."""

    prompt_id: str | None = None
    """Echo of the prompt's staleness token."""


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
            skip_gates: If True, resolve a prompt without asking, by selecting
                its ``auto_select`` value. Prompts that set no ``auto_select``
                (auto-selecting would invent an answer) are still presented,
                so such callers must short-circuit before reaching here.
        """
        self.console = console or Console()
        self.skip_gates = skip_gates
        self.renderer = TemplateRenderer()

    async def handle_gate(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        base_dir: Path | None = None,
    ) -> GateResult:
        """Handle a human gate interaction.

        Displays the prompt and options to the user, collects their selection,
        and optionally prompts for additional text input.

        Args:
            agent: The human_gate agent definition.
            context: Current workflow context for template rendering.
            base_dir: Optional directory for resolving relative file paths
                in the rendered prompt into clickable markdown links.

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

        gate_prompt = self.build_gate_prompt(agent, context, base_dir=base_dir)
        response = await self.prompt(gate_prompt)

        selected = option_for_value(agent, response.value)
        return GateResult(
            selected_option=selected,
            route=selected.route,
            additional_input=response.additional_input,
        )

    def build_gate_prompt(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        base_dir: Path | None = None,
    ) -> GatePrompt:
        """Render a ``human_gate`` agent into a graph-agnostic prompt.

        Args:
            agent: The human_gate agent definition.
            context: Current workflow context for template rendering.
            base_dir: Optional directory for resolving relative file paths
                in the rendered prompt into clickable markdown links.

        Returns:
            A GatePrompt whose ``auto_select`` is the first option, matching
            ``--skip-gates`` behavior for gates. That is a declared route on a
            real gate, not an invented human answer.
        """
        prompt_text = self.renderer.render(agent.prompt, context)
        prompt_text = linkify_markdown(prompt_text, base_dir=base_dir)
        options = agent.options or []
        return GatePrompt(
            name=agent.name,
            prompt=prompt_text,
            choices=[
                GateChoice(
                    label=o.label,
                    value=o.value,
                    prompt_for=o.prompt_for,
                    multiline=o.multiline,
                )
                for o in options
            ],
            auto_select=options[0].value if options else None,
        )

    async def prompt(self, gate_prompt: GatePrompt) -> GateResponse:
        """Present one prompt to the user and collect their answer.

        Knows nothing about the workflow graph — no routes, no agent
        definitions. Both ``human_gate`` and ``questions`` funnel through
        here so terminal behavior stays identical between them.

        Args:
            gate_prompt: The prompt to present.

        Returns:
            GateResponse with the selected value, label, and any free text.

        Raises:
            HumanGateError: If the prompt has no choices.
        """
        if not gate_prompt.choices:
            raise HumanGateError(
                f"Human prompt '{gate_prompt.name}' has no choices to present",
                suggestion="Provide at least one selectable choice",
            )

        if self.skip_gates and gate_prompt.auto_select is not None:
            return self._auto_select_choice(gate_prompt)

        selected = await self._display_and_select(gate_prompt.prompt, gate_prompt.choices)

        additional_input: dict[str, str] = {}
        if selected.prompt_for:
            additional_input = await self._collect_additional_input(
                selected.prompt_for, multiline=selected.multiline
            )

        return GateResponse(
            value=selected.value,
            label=selected.label,
            additional_input=additional_input,
            prompt_id=gate_prompt.prompt_id,
        )

    def _auto_select_choice(self, gate_prompt: GatePrompt) -> GateResponse:
        """Resolve a prompt without user interaction (``--skip-gates``).

        Args:
            gate_prompt: The prompt being auto-resolved. Its ``auto_select``
                must be set.

        Returns:
            GateResponse for the auto-selected choice.

        Raises:
            HumanGateError: If ``auto_select`` names no known choice.
        """
        for choice in gate_prompt.choices:
            if choice.value == gate_prompt.auto_select:
                self.console.print(f"\n[dim]Auto-selecting: {choice.label} (--skip-gates)[/dim]")
                return GateResponse(
                    value=choice.value,
                    label=choice.label,
                    additional_input={},  # No input collection in skip mode
                    prompt_id=gate_prompt.prompt_id,
                )
        raise HumanGateError(
            f"auto_select value '{gate_prompt.auto_select}' does not match any "
            f"choice for prompt '{gate_prompt.name}'",
            suggestion="This is an internal error; please report it",
        )

    async def _display_and_select(
        self,
        prompt_text: str,
        choices: list[GateChoice],
    ) -> GateChoice:
        """Display prompt and get user selection.

        Uses Rich for beautiful terminal UI with numbered options.

        Args:
            prompt_text: The rendered prompt to display.
            choices: List of choices to select from.

        Returns:
            The selected GateChoice.
        """
        # Display the prompt in a styled panel (render as Markdown for rich formatting)
        self.console.print()
        self.console.print(
            Panel(
                RichMarkdown(prompt_text),
                title="[bold cyan]Decision Required[/bold cyan]",
                border_style="cyan",
            )
        )

        # Display options as numbered list
        self.console.print()
        self.console.print("[bold]Options:[/bold]")
        for i, choice in enumerate(choices, 1):
            self.console.print(f"  [cyan][{i}][/cyan] {choice.label}")

        # Get user selection — run in thread to avoid blocking the event loop
        # (blocking here prevents the web dashboard from updating)
        valid_choices = [str(i) for i in range(1, len(choices) + 1)]
        while True:

            def _ask_choice() -> str:
                return Prompt.ask(
                    "\n[bold]Select option[/bold]",
                    choices=valid_choices,
                    show_choices=True,
                )

            choice_input = await read_on_daemon_thread(_ask_choice)
            try:
                index = int(choice_input) - 1
                if 0 <= index < len(choices):
                    selected = choices[index]
                    self.console.print(f"\n[green]Selected:[/green] {selected.label}")
                    return selected
            except ValueError:
                pass
            self.console.print("[red]Invalid selection. Please try again.[/red]")

    async def _collect_additional_input(
        self, field_name: str, multiline: bool = False
    ) -> dict[str, str]:
        """Collect additional text input from user.

        Prompts the user for additional text input as specified by the
        prompt_for field on the selected option.

        Args:
            field_name: The name of the field to prompt for.
            multiline: If True and stdin is a TTY, read until a lone ``.``
                or EOF instead of stopping at the first newline.

        Returns:
            Dictionary with the field name and collected value.
        """
        self.console.print()
        self.console.print(f"[bold]Please provide {field_name}:[/bold]")

        # Without a TTY, fall back: _read_multiline treats EOF as "submit" and
        # would return "" instantly on a DEVNULL stdin, letting the CLI arm win
        # the race in _resolve_human_prompt with an empty answer.
        if multiline and sys.stdin.isatty():
            value = await read_on_daemon_thread(self._read_multiline)
            return {field_name: value}

        def _ask_value() -> str:
            return Prompt.ask(f"  {field_name}")

        value = await read_on_daemon_thread(_ask_value)
        return {field_name: value}

    def _read_multiline(self) -> str:
        """Read a multi-line answer from stdin (blocking; call in a thread).

        Terminates on a line containing only ``.`` or on EOF. The sentinel is
        listed first in the hint because Ctrl-D/Ctrl-Z differs by platform.

        Returns:
            The collected text with trailing blank lines stripped. Internal
            newlines are preserved.
        """
        self.console.print(
            f"  [dim]Enter your answer. Finish with '{MULTILINE_SENTINEL}' on its own line"
            f" (or {_eof_key_hint()}).[/dim]"
        )
        lines: list[str] = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line.strip() == MULTILINE_SENTINEL:
                break
            lines.append(line)
        return "\n".join(lines).rstrip("\n")


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

            def _ask_int() -> int:
                return IntPrompt.ask(
                    "[bold]How many more iterations would you like to allow?[/bold]",
                    default=0,
                )

            value = await read_on_daemon_thread(_ask_int)
            return max(0, value)  # Ensure non-negative
        except (ValueError, KeyboardInterrupt, EOFError):
            # EOFError fires when stdin is not a TTY (CI, ``< /dev/null``,
            # containers without an attached terminal). Treat it as
            # "stop" — same as the user typing 0 — so the dashboard's
            # iteration_limit_resolved event still fires (issue #134).
            return 0
