"""Implementation of the 'conductor run' command.

This module provides helper functions for executing workflow files.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from conductor.config.loader import load_config
from conductor.engine.workflow import ExecutionPlan, WorkflowEngine
from conductor.mcp_auth import resolve_mcp_server_auth
from conductor.providers.registry import ProviderRegistry

if TYPE_CHECKING:
    from conductor.events import WorkflowEvent

# Verbose console for logging (stderr)
_verbose_console = Console(stderr=True, highlight=False)

# File console for file logging (None when not active)
_file_console: Console | None = None
_file_handle: Any = None

# Pattern for resolving ${VAR} and ${VAR:-default} in env values
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")


def generate_log_path(workflow_name: str) -> Path:
    """Generate auto log file path.

    Creates a path like: $TMPDIR/conductor/conductor-<workflow>-<timestamp>.log
    The parent directory is created automatically if it doesn't exist.

    Args:
        workflow_name: Name of the workflow (used in the filename).

    Returns:
        Path to the auto-generated log file.
    """
    import secrets

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    # Append random suffix to avoid filename collisions
    # when multiple runs start in the same second
    suffix = secrets.token_hex(4)
    timestamp = f"{timestamp}-{suffix}"
    path = Path(tempfile.gettempdir()) / "conductor" / f"conductor-{workflow_name}-{timestamp}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def init_file_logging(log_path: Path) -> None:
    """Initialize file logging to the given path.

    Creates a Rich Console writing to the specified file with no_color=True
    for plain text output. The parent directory is created automatically.

    Args:
        log_path: Path to write log output to.

    Raises:
        OSError: If the file cannot be opened for writing.
    """
    global _file_console, _file_handle
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _file_handle = open(log_path, "w", encoding="utf-8")  # noqa: SIM115
    _file_console = Console(file=_file_handle, no_color=True, highlight=False, width=200)


def close_file_logging() -> None:
    """Close file logging and clean up resources."""
    global _file_console, _file_handle
    _file_console = None
    if _file_handle is not None:
        _file_handle.close()
        _file_handle = None


def resolve_mcp_env_vars(env: dict[str, str]) -> dict[str, str]:
    """Resolve ${VAR} and ${VAR:-default} patterns in env values.

    Unlike the config loader which resolves at load time, this resolves
    at runtime from the current process environment. This allows users
    to reference environment variables (like API keys) in MCP server
    configuration without hardcoding them in the YAML.

    Syntax:
        - ${VAR} - Replace with value of VAR, or empty string if not set
        - ${VAR:-default} - Replace with value of VAR, or 'default' if not set

    Args:
        env: Dictionary of environment variable names to values,
             where values may contain ${VAR} patterns.

    Returns:
        New dictionary with all ${VAR} patterns resolved.

    Example:
        >>> import os
        >>> os.environ['MY_KEY'] = 'secret123'
        >>> resolve_mcp_env_vars({'API_KEY': '${MY_KEY}', 'DEBUG': '${DEBUG:-false}'})
        {'API_KEY': 'secret123', 'DEBUG': 'false'}
    """

    def replace_match(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default_value = match.group(2)
        env_value = os.environ.get(var_name)
        if env_value is not None:
            return env_value
        elif default_value is not None:
            return default_value
        else:
            return ""

    resolved: dict[str, str] = {}
    for key, value in env.items():
        resolved[key] = _ENV_VAR_PATTERN.sub(replace_match, value)
    return resolved


def verbose_log(message: str, style: str = "dim") -> None:
    """Log a message if verbose mode is enabled.

    Args:
        message: The message to log.
        style: Rich style for the message.
    """
    from conductor.cli.app import is_verbose

    if is_verbose():
        _verbose_console.print(f"[{style}]{message}[/{style}]")
    if _file_console is not None:
        _file_console.print(message)


def verbose_log_agent_start(agent_name: str, iteration: int) -> None:
    """Log agent execution start with visual formatting.

    Args:
        agent_name: Name of the agent being executed.
        iteration: Current iteration number (1-indexed).
    """
    from rich.text import Text

    from conductor.cli.app import is_verbose

    should_console = is_verbose()
    should_file = _file_console is not None
    if not should_console and not should_file:
        return

    text = Text()
    text.append("┌─ ", style="cyan")
    text.append("Agent: ", style="cyan")
    text.append(agent_name, style="cyan bold")
    text.append(f" [iter {iteration}]", style="dim")

    if should_console:
        _verbose_console.print()  # Empty line before agent
        _verbose_console.print(text)
    if _file_console is not None:
        _file_console.print()
        _file_console.print(text)


def verbose_log_agent_complete(
    agent_name: str,
    elapsed: float,
    *,
    model: str | None = None,
    tokens: int | None = None,
    output_keys: list[str] | None = None,
    cost_usd: float | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    """Log agent completion with summary info.

    Args:
        agent_name: Name of the agent that completed.
        elapsed: Elapsed time in seconds.
        model: Model used (if any).
        tokens: Total tokens used (if any).
        output_keys: List of output keys (if dict output).
        cost_usd: Estimated cost in USD (if available).
        input_tokens: Input tokens used (if available).
        output_tokens: Output tokens generated (if available).
    """
    from rich.text import Text

    from conductor.cli.app import is_verbose

    should_console = is_verbose()
    should_file = _file_console is not None
    if not should_console and not should_file:
        return

    # Build summary line
    parts = [f"{elapsed:.2f}s"]
    if model:
        parts.append(model)
    if input_tokens is not None and output_tokens is not None:
        parts.append(f"{input_tokens} in/{output_tokens} out")
    elif tokens:
        parts.append(f"{tokens} tokens")
    if cost_usd is not None:
        parts.append(f"${cost_usd:.4f}")
    if output_keys:
        parts.append(f"→ {output_keys}")

    text = Text()
    text.append("└─ ", style="green")
    text.append("✓ ", style="green")
    text.append(agent_name, style="green")
    text.append(f"  ({', '.join(parts)})", style="dim")

    if should_console:
        _verbose_console.print(text)
    if _file_console is not None:
        _file_console.print(text)


def verbose_log_route(target: str) -> None:
    """Log routing decision.

    Args:
        target: The routing target.
    """
    from rich.text import Text

    from conductor.cli.app import is_verbose

    should_console = is_verbose()
    should_file = _file_console is not None
    if not should_console and not should_file:
        return

    text = Text()
    text.append("   → ", style="yellow")
    if target == "$end":
        text.append("$end", style="yellow bold")
    else:
        text.append("next: ", style="dim")
        text.append(target, style="yellow")

    if should_console:
        _verbose_console.print(text)
    if _file_console is not None:
        _file_console.print(text)


def verbose_log_section(title: str, content: str) -> None:
    """Log a section with title if full verbose mode is enabled.

    Sections contain detailed content like prompts and tool arguments.
    They are shown in FULL mode (default) but skipped in MINIMAL mode (--quiet).
    File logging always receives full content regardless of console verbosity.

    Args:
        title: Section title.
        content: Section content.
    """
    from conductor.cli.app import is_full, is_verbose

    # Sections are detail-level: show on console only in FULL mode
    should_console = is_verbose() and is_full()
    should_file = _file_console is not None
    if not should_console and not should_file:
        return

    if should_console:
        _verbose_console.print(Panel(content, title=f"[cyan]{title}[/cyan]", border_style="dim"))

    # File always gets full untruncated content
    if _file_console is not None:
        _file_console.print(Panel(content, title=title, border_style="dim"))


def verbose_log_timing(operation: str, elapsed: float) -> None:
    """Log timing information if verbose mode is enabled.

    Args:
        operation: Description of the operation.
        elapsed: Elapsed time in seconds.
    """
    from conductor.cli.app import is_verbose

    if is_verbose():
        _verbose_console.print(f"[dim]⏱ {operation}: {elapsed:.2f}s[/dim]")
    if _file_console is not None:
        _file_console.print(f"⏱ {operation}: {elapsed:.2f}s")


def verbose_log_parallel_start(group_name: str, agent_count: int) -> None:
    """Log parallel group execution start.

    Args:
        group_name: Name of the parallel group.
        agent_count: Number of agents in the group.
    """
    from rich.text import Text

    from conductor.cli.app import is_verbose

    should_console = is_verbose()
    should_file = _file_console is not None
    if not should_console and not should_file:
        return

    text = Text()
    text.append("┌─ ", style="magenta")
    text.append("Parallel Group: ", style="magenta")
    text.append(group_name, style="magenta bold")
    text.append(f" ({agent_count} agents)", style="dim")

    if should_console:
        _verbose_console.print()
        _verbose_console.print(text)
    if _file_console is not None:
        _file_console.print()
        _file_console.print(text)


def verbose_log_parallel_agent_complete(
    agent_name: str,
    elapsed: float,
    *,
    model: str | None = None,
    tokens: int | None = None,
    cost_usd: float | None = None,
) -> None:
    """Log parallel agent completion.

    Args:
        agent_name: Name of the agent that completed.
        elapsed: Elapsed time in seconds.
        model: Model used (if any).
        tokens: Tokens used (if any).
        cost_usd: Estimated cost in USD (if available).
    """
    from rich.text import Text

    from conductor.cli.app import is_verbose

    should_console = is_verbose()
    should_file = _file_console is not None
    if not should_console and not should_file:
        return

    parts = [f"{elapsed:.2f}s"]
    if model:
        parts.append(model)
    if tokens:
        parts.append(f"{tokens} tokens")
    if cost_usd is not None:
        parts.append(f"${cost_usd:.4f}")

    text = Text()
    text.append("  ✓ ", style="green")
    text.append(agent_name, style="green")
    text.append(f"  ({', '.join(parts)})", style="dim")

    if should_console:
        _verbose_console.print(text)
    if _file_console is not None:
        _file_console.print(text)


def verbose_log_parallel_agent_failed(
    agent_name: str,
    elapsed: float,
    exception_type: str,
    message: str,
) -> None:
    """Log parallel agent failure.

    Args:
        agent_name: Name of the agent that failed.
        elapsed: Elapsed time in seconds.
        exception_type: Type of exception.
        message: Error message.
    """
    from rich.text import Text

    from conductor.cli.app import is_verbose

    should_console = is_verbose()
    should_file = _file_console is not None
    if not should_console and not should_file:
        return

    text = Text()
    text.append("  ✗ ", style="red")
    text.append(agent_name, style="red")
    text.append(f"  ({elapsed:.2f}s)", style="dim")
    error_msg = f"      {exception_type}: {message}"

    if should_console:
        _verbose_console.print(text)
        _verbose_console.print(error_msg, style="red dim")
    if _file_console is not None:
        _file_console.print(text)
        _file_console.print(error_msg)


def verbose_log_parallel_summary(
    group_name: str,
    success_count: int,
    failure_count: int,
    total_elapsed: float,
) -> None:
    """Log parallel group execution summary.

    Args:
        group_name: Name of the parallel group.
        success_count: Number of agents that succeeded.
        failure_count: Number of agents that failed.
        total_elapsed: Total elapsed time in seconds.
    """
    from rich.text import Text

    from conductor.cli.app import is_verbose

    should_console = is_verbose()
    should_file = _file_console is not None
    if not should_console and not should_file:
        return

    text = Text()
    text.append("└─ ", style="cyan")

    if failure_count == 0:
        text.append("✓ ", style="green")
        text.append(group_name, style="green")
        text.append(
            f"  ({success_count}/{success_count} succeeded, {total_elapsed:.2f}s)",
            style="dim",
        )
    else:
        status_parts = []
        # Always show succeeded count even if 0
        status_parts.append(f"{success_count} succeeded")
        status_parts.append(f"{failure_count} failed")

        style = "yellow" if success_count > 0 else "red"
        text.append("◆ ", style=style)
        text.append(group_name, style=style)
        text.append(f"  ({', '.join(status_parts)}, {total_elapsed:.2f}s)", style="dim")

    if should_console:
        _verbose_console.print(text)
    if _file_console is not None:
        _file_console.print(text)


def verbose_log_for_each_start(
    group_name: str,
    item_count: int,
    max_concurrent: int,
    failure_mode: str,
) -> None:
    """Log for-each group execution start.

    Args:
        group_name: Name of the for-each group.
        item_count: Number of items to process.
        max_concurrent: Maximum concurrent executions.
        failure_mode: Failure mode (fail_fast, continue_on_error, all_or_nothing).
    """
    from rich.text import Text

    from conductor.cli.app import is_verbose

    should_console = is_verbose()
    should_file = _file_console is not None
    if not should_console and not should_file:
        return

    text = Text()
    text.append("┌─ ", style="blue")
    text.append("For-Each: ", style="blue")
    text.append(group_name, style="blue bold")
    text.append(
        f" ({item_count} items, max_concurrent={max_concurrent}, {failure_mode})", style="dim"
    )

    if should_console:
        _verbose_console.print()
        _verbose_console.print(text)
    if _file_console is not None:
        _file_console.print()
        _file_console.print(text)


def verbose_log_for_each_item_complete(
    item_key: str,
    elapsed: float,
    *,
    tokens: int | None = None,
    cost_usd: float | None = None,
) -> None:
    """Log for-each item completion.

    Args:
        item_key: Key/index of the item that completed.
        elapsed: Elapsed time in seconds.
        tokens: Tokens used (if any).
        cost_usd: Estimated cost in USD (if available).
    """
    from rich.text import Text

    from conductor.cli.app import is_verbose

    should_console = is_verbose()
    should_file = _file_console is not None
    if not should_console and not should_file:
        return

    parts = [f"{elapsed:.2f}s"]
    if tokens:
        parts.append(f"{tokens} tokens")
    if cost_usd is not None:
        parts.append(f"${cost_usd:.4f}")

    text = Text()
    text.append("  ✓ ", style="green")
    text.append(f"[{item_key}]", style="green")
    text.append(f"  ({', '.join(parts)})", style="dim")

    if should_console:
        _verbose_console.print(text)
    if _file_console is not None:
        _file_console.print(text)


def verbose_log_for_each_item_failed(
    item_key: str,
    elapsed: float,
    exception_type: str,
    message: str,
) -> None:
    """Log for-each item failure.

    Args:
        item_key: Key/index of the item that failed.
        elapsed: Elapsed time in seconds.
        exception_type: Type of exception.
        message: Error message.
    """
    from rich.text import Text

    from conductor.cli.app import is_verbose

    should_console = is_verbose()
    should_file = _file_console is not None
    if not should_console and not should_file:
        return

    text = Text()
    text.append("  ✗ ", style="red")
    text.append(f"[{item_key}]", style="red")
    text.append(f"  ({elapsed:.2f}s)", style="dim")
    error_msg = f"      {exception_type}: {message}"

    if should_console:
        _verbose_console.print(text)
        _verbose_console.print(error_msg, style="red dim")
    if _file_console is not None:
        _file_console.print(text)
        _file_console.print(error_msg)


def verbose_log_for_each_summary(
    group_name: str,
    success_count: int,
    failure_count: int,
    total_elapsed: float,
) -> None:
    """Log for-each group execution summary.

    Args:
        group_name: Name of the for-each group.
        success_count: Number of items that succeeded.
        failure_count: Number of items that failed.
        total_elapsed: Total elapsed time in seconds.
    """
    from rich.text import Text

    from conductor.cli.app import is_verbose

    should_console = is_verbose()
    should_file = _file_console is not None
    if not should_console and not should_file:
        return

    text = Text()
    text.append("└─ ", style="cyan")

    if failure_count == 0:
        text.append("✓ ", style="green")
        text.append(group_name, style="green")
        text.append(
            f"  ({success_count}/{success_count} succeeded, {total_elapsed:.2f}s)", style="dim"
        )
    else:
        status_parts = []
        status_parts.append(f"{success_count} succeeded")
        status_parts.append(f"{failure_count} failed")

        style = "yellow" if success_count > 0 else "red"
        text.append("◆ ", style=style)
        text.append(group_name, style=style)
        text.append(f"  ({', '.join(status_parts)}, {total_elapsed:.2f}s)", style="dim")

    if should_console:
        _verbose_console.print(text)
    if _file_console is not None:
        _file_console.print(text)


# ------------------------------------------------------------------
# Console event subscriber — bridges the event emitter to verbose_log
# ------------------------------------------------------------------


class ConsoleEventSubscriber:
    """Subscribes to WorkflowEventEmitter and drives console/file logging.

    Maps each event type to the corresponding ``verbose_log_*`` call so that
    ``workflow.py`` only needs to emit events — display logic stays here.
    """

    def on_event(self, event: WorkflowEvent) -> None:
        d = event.data
        t = event.type

        if t == "agent_started":
            verbose_log_agent_start(d.get("agent_name", "?"), d.get("iteration", 0))

        elif t == "agent_completed":
            verbose_log_agent_complete(
                d.get("agent_name", "?"),
                d.get("elapsed", 0.0),
                model=d.get("model"),
                tokens=d.get("tokens"),
                output_keys=d.get("output_keys"),
                cost_usd=d.get("cost_usd"),
                input_tokens=d.get("input_tokens"),
                output_tokens=d.get("output_tokens"),
            )

        elif t == "route_taken":
            verbose_log_route(d.get("to_agent", "?"))

        elif t == "parallel_started":
            agents = d.get("agents", [])
            verbose_log_parallel_start(d.get("group_name", "?"), len(agents))

        elif t == "parallel_agent_completed":
            verbose_log_parallel_agent_complete(
                d.get("agent_name", "?"),
                d.get("elapsed", 0.0),
                model=d.get("model"),
                tokens=d.get("tokens"),
                cost_usd=d.get("cost_usd"),
            )

        elif t == "parallel_agent_failed":
            verbose_log_parallel_agent_failed(
                d.get("agent_name", "?"),
                d.get("elapsed", 0.0),
                d.get("error_type", "Error"),
                d.get("message", "unknown"),
            )

        elif t == "parallel_completed":
            verbose_log_parallel_summary(
                d.get("group_name", "?"),
                d.get("success_count", 0),
                d.get("failure_count", 0),
                d.get("elapsed", 0.0),
            )

        elif t == "for_each_started":
            verbose_log_for_each_start(
                d.get("group_name", "?"),
                d.get("item_count", 0),
                d.get("max_concurrent", 1),
                d.get("failure_mode", "fail_fast"),
            )

        elif t == "for_each_item_completed":
            verbose_log_for_each_item_complete(
                d.get("item_key", "?"),
                d.get("elapsed", 0.0),
                tokens=d.get("tokens"),
                cost_usd=d.get("cost_usd"),
            )

        elif t == "for_each_item_failed":
            verbose_log_for_each_item_failed(
                d.get("item_key", "?"),
                d.get("elapsed", 0.0),
                d.get("error_type", "Error"),
                d.get("message", "unknown"),
            )

        elif t == "for_each_completed":
            verbose_log_for_each_summary(
                d.get("group_name", "?"),
                d.get("success_count", 0),
                d.get("failure_count", 0),
                d.get("elapsed", 0.0),
            )

        elif t == "script_completed":
            verbose_log_agent_complete(
                d.get("agent_name", "?"),
                d.get("elapsed", 0.0),
            )


def display_usage_summary(usage_data: dict[str, Any], console: Console | None = None) -> None:
    """Display final usage summary with token counts and costs.

    Args:
        usage_data: Usage dictionary from WorkflowEngine.get_execution_summary()['usage']
        console: Optional Rich console. Uses stderr console if not provided.
    """
    from conductor.cli.app import is_verbose

    should_console = is_verbose()
    should_file = _file_console is not None

    if not should_console and not should_file:
        return

    output_console = console if console is not None else _verbose_console
    targets: list[Console] = []
    if should_console:
        targets.append(output_console)
    if _file_console is not None:
        targets.append(_file_console)

    def _print(*args: Any, **kwargs: Any) -> None:
        for t in targets:
            t.print(*args, **kwargs)

    _print()
    _print("=" * 60, style="dim")
    _print("[bold cyan]Token Usage Summary[/bold cyan]")

    # Token totals
    total_input = usage_data.get("total_input_tokens", 0)
    total_output = usage_data.get("total_output_tokens", 0)
    total_tokens = usage_data.get("total_tokens", 0)

    if total_tokens > 0:
        _print(f"  Input:  {total_input:,} tokens", style="dim")
        _print(f"  Output: {total_output:,} tokens", style="dim")
        _print(f"  Total:  {total_tokens:,} tokens", style="dim")
    else:
        _print("  [dim]No token data available[/dim]")

    # Cost breakdown
    total_cost = usage_data.get("total_cost_usd")
    agents = usage_data.get("agents", [])

    if total_cost is not None and total_cost > 0:
        _print()
        _print("[bold cyan]Cost Breakdown:[/bold cyan]")

        for agent in agents:
            agent_cost = agent.get("cost_usd")
            if agent_cost is not None and agent_cost > 0:
                pct = (agent_cost / total_cost * 100) if total_cost > 0 else 0
                _print(
                    f"  {agent['agent_name']}: ${agent_cost:.4f} ({pct:.0f}%)",
                    style="dim",
                )

        _print(f"  [bold]Total: ${total_cost:.4f}[/bold]")
    elif total_tokens > 0:
        _print()
        _print("  [dim]Cost data unavailable (unknown model pricing)[/dim]")

    _print("=" * 60, style="dim")


def parse_input_flags(raw_inputs: list[str]) -> dict[str, Any]:
    """Parse --input.<name>=<value> flags into a dictionary.

    Supports type coercion for common types:
    - "true"/"false" -> bool
    - numeric strings -> int/float
    - JSON arrays/objects -> parsed JSON
    - everything else -> string

    Args:
        raw_inputs: List of "name=value" strings from CLI.

    Returns:
        Dictionary of parsed input name-value pairs.

    Raises:
        typer.BadParameter: If input format is invalid.
    """
    inputs: dict[str, Any] = {}

    for raw in raw_inputs:
        # Split on first = only
        if "=" not in raw:
            raise typer.BadParameter(f"Invalid input format: '{raw}'. Expected format: name=value")

        name, value = raw.split("=", 1)
        name = name.strip()
        value = value.strip()

        if not name:
            raise typer.BadParameter(f"Empty input name in: '{raw}'")

        # Type coercion
        inputs[name] = coerce_value(value)

    return inputs


def coerce_value(value: str) -> Any:
    """Coerce a string value to an appropriate Python type.

    Args:
        value: The string value to coerce.

    Returns:
        The coerced value (bool, int, float, list, dict, or str).
    """
    # Handle booleans
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False

    # Handle null
    if value.lower() == "null":
        return None

    # Try JSON for arrays and objects
    if value.startswith(("[", "{")):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass

    # Try numeric conversion
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        pass

    # Return as string
    return value


class InputCollector:
    """Collects input values from --input.* options.

    This class handles parsing of dynamic input options that follow
    the pattern --input.<name>=<value>.
    """

    INPUT_PATTERN = re.compile(r"^--input\.(.+)$")

    @classmethod
    def extract_from_args(cls, args: list[str] | None = None) -> dict[str, Any]:
        """Extract input values from command line arguments.

        Scans sys.argv (or provided args) for --input.* patterns and
        extracts their values.

        Args:
            args: Optional list of arguments to parse. Defaults to sys.argv.

        Returns:
            Dictionary of input name-value pairs.
        """
        if args is None:
            args = sys.argv[1:]

        inputs: dict[str, Any] = {}
        i = 0
        while i < len(args):
            arg = args[i]
            match = cls.INPUT_PATTERN.match(arg)

            if match:
                name = match.group(1)

                # Check for = in the argument (--input.name=value)
                if "=" in name:
                    name, value = name.split("=", 1)
                    inputs[name] = coerce_value(value)
                elif i + 1 < len(args) and not args[i + 1].startswith("-"):
                    # Next argument is the value
                    value = args[i + 1]
                    inputs[name] = coerce_value(value)
                    i += 1
                else:
                    # Boolean flag style (presence = true)
                    inputs[name] = True

            i += 1

        return inputs


async def _run_with_stop_signal(
    engine: Any,
    inputs: dict[str, Any],
    dashboard: Any | None,
) -> dict[str, Any]:
    """Run the workflow engine, racing against a dashboard kill signal.

    When the web dashboard's Kill button is clicked (``/api/kill``), the
    engine task is cancelled and an ``ExecutionError`` is raised.

    If no dashboard is present, this simply awaits ``engine.run()`` directly.

    Args:
        engine: The ``WorkflowEngine`` instance.
        inputs: Workflow input values.
        dashboard: The ``WebDashboard`` instance, or None.

    Returns:
        The workflow result dict.

    Raises:
        ExecutionError: If the workflow was killed via the dashboard.
    """
    if dashboard is None:
        return await engine.run(inputs)

    engine_task = asyncio.create_task(engine.run(inputs))
    stop_task = asyncio.create_task(dashboard.wait_for_stop())

    done, pending = await asyncio.wait(
        {engine_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    if engine_task in done:
        return engine_task.result()

    # Stop was requested — raise an error so the workflow is treated as failed
    from conductor.exceptions import ExecutionError

    raise ExecutionError("Workflow stopped by user via dashboard")


async def run_workflow_async(
    workflow_path: Path,
    inputs: dict[str, Any],
    provider_override: str | None = None,
    skip_gates: bool = False,
    log_file: Path | None = None,
    no_interactive: bool = False,
    *,
    web: bool = False,
    web_port: int = 0,
    web_bg: bool = False,
) -> dict[str, Any]:
    """Execute a workflow asynchronously.

    Args:
        workflow_path: Path to the workflow YAML file.
        inputs: Workflow input values.
        provider_override: Optional provider name to override workflow config.
        skip_gates: If True, auto-selects first option at human gates.
        log_file: Optional path to write full debug output to a file.
        no_interactive: If True, disables the keyboard interrupt listener.
        web: If True, start a real-time web dashboard.
        web_port: Port for the web dashboard (0 = auto-select).
        web_bg: If True, auto-shutdown dashboard after workflow + client disconnect.

    Returns:
        The workflow output as a dictionary.

    Raises:
        ConductorError: If workflow execution fails.
    """
    from conductor.events import WorkflowEventEmitter

    start_time = time.time()

    # Initialize file logging if requested
    if log_file is not None:
        try:
            init_file_logging(log_file)
        except OSError as e:
            _verbose_console.print(
                f"[bold yellow]Warning:[/bold yellow] Cannot open log file {log_file}: {e}"
            )

    # Always create event emitter and JSONL log subscriber
    emitter = WorkflowEventEmitter()
    event_log_subscriber: Any = None
    dashboard: Any = None

    if web:
        from conductor.web.server import WebDashboard

        bg_mode = web_bg or os.environ.get("CONDUCTOR_WEB_BG") == "1"
        dashboard = WebDashboard(
            emitter,
            host="127.0.0.1",
            port=web_port,
            bg=bg_mode,
            workflow_root=Path(workflow_path).resolve().parent,
        )

        try:
            await dashboard.start()
            # Print URL to stderr regardless of --silent/--quiet
            _verbose_console.print(f"[bold cyan]Dashboard:[/bold cyan] {dashboard.url}")
        except Exception as e:
            _verbose_console.print(
                f"[bold yellow]Warning:[/bold yellow] "
                f"Dashboard failed to start: {e}. Continuing without dashboard."
            )
            dashboard = None

    try:
        # Log workflow loading
        verbose_log(f"Loading workflow: {workflow_path}")

        # Load configuration
        load_start = time.time()
        config = load_config(workflow_path)
        verbose_log_timing("Configuration loaded", time.time() - load_start)

        # Log workflow details
        verbose_log(f"Workflow: {config.workflow.name}")
        verbose_log(f"Entry point: {config.workflow.entry_point}")
        verbose_log(f"Agents: {len(config.agents)}")

        # Start JSONL event log subscriber (always-on structured diagnostics)
        from conductor.engine.event_log import EventLogSubscriber

        event_log_subscriber = EventLogSubscriber(config.workflow.name)
        emitter.subscribe(event_log_subscriber.on_event)

        # Subscribe console output to the event emitter
        console_subscriber = ConsoleEventSubscriber()
        emitter.subscribe(console_subscriber.on_event)

        if inputs:
            verbose_log_section("Workflow Inputs", json.dumps(inputs, indent=2))

        # Apply provider override if specified
        if provider_override:
            verbose_log(f"Provider override: {provider_override}", style="yellow")
            config.workflow.runtime.provider = provider_override  # type: ignore[assignment]

        # Convert MCP servers from workflow config to SDK format
        mcp_servers = await _build_mcp_servers(config)

        # Check if workflow uses multiple providers (has per-agent provider overrides)
        uses_multi_provider = any(agent.provider is not None for agent in config.agents)

        if uses_multi_provider:
            verbose_log("Multi-provider mode: agents use different providers", style="cyan")
        else:
            verbose_log(f"Single provider mode: {config.workflow.runtime.provider}")

        # Use ProviderRegistry for multi-provider support
        async with ProviderRegistry(config, mcp_servers=mcp_servers) as registry:
            # Create and run workflow engine
            verbose_log("Starting workflow execution...")

            # Set up interrupt listener if interactive mode is enabled
            # Disabled in --web mode since the CLI isn't used for interaction
            interrupt_event: asyncio.Event | None = None
            listener = None
            if not no_interactive and not web and sys.stdin.isatty():
                from conductor.interrupt.listener import KeyboardListener

                interrupt_event = asyncio.Event()
                listener = KeyboardListener(interrupt_event=interrupt_event)
            elif web:
                # In --web mode: no keyboard listener, but still need interrupt_event
                # so POST /api/stop can interrupt the running agent mid-execution
                interrupt_event = asyncio.Event()

            engine = WorkflowEngine(
                config,
                registry=registry,
                skip_gates=skip_gates,
                workflow_path=workflow_path,
                interrupt_event=interrupt_event,
                event_emitter=emitter,
                keyboard_listener=listener,
                web_dashboard=dashboard,
            )

            # Share interrupt_event with dashboard so POST /api/stop can abort agents
            if dashboard is not None and interrupt_event is not None:
                dashboard.set_interrupt_event(interrupt_event)

            try:
                if listener is not None:
                    await listener.start()
                    _verbose_console.print("[dim]Press Esc to interrupt and provide guidance[/dim]")

                result = await _run_with_stop_signal(engine, inputs, dashboard)
            except BaseException:
                _print_resume_instructions(engine)
                raise
            finally:
                if listener is not None:
                    await listener.stop()

            # Log completion
            verbose_log_timing("Total workflow execution", time.time() - start_time)
            verbose_log("Workflow completed successfully", style="green")

            # Display usage summary if cost tracking is enabled
            if config.workflow.cost.show_summary:
                summary = engine.get_execution_summary()
                if "usage" in summary:
                    display_usage_summary(summary["usage"])

            # Post-execution dashboard lifecycle
            if dashboard is not None:
                # Auto-shutdown if either --web-bg was passed directly or
                # this is a background child process (CONDUCTOR_WEB_BG env var)
                is_bg = web_bg or os.environ.get("CONDUCTOR_WEB_BG") == "1"
                if is_bg:
                    await dashboard.wait_for_clients_disconnect()
                else:
                    _verbose_console.print(
                        f"\n[bold green]Workflow complete.[/bold green] "
                        f"Dashboard still running at {dashboard.url} — "
                        f"press [bold]Ctrl+C[/bold] to exit."
                    )
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.Event().wait()

            return result
    finally:
        # Clean up PID file if this is a background child process
        is_bg_child = os.environ.get("CONDUCTOR_WEB_BG") == "1"
        if is_bg_child:
            from conductor.cli.pid import remove_pid_file_for_current_process

            remove_pid_file_for_current_process()

        # Stop dashboard if it was started
        if dashboard is not None:
            await dashboard.stop()

        # Close JSONL event log and report path
        if event_log_subscriber is not None:
            event_log_subscriber.close()
            _verbose_console.print(f"[dim]Event log written to: {event_log_subscriber.path}[/dim]")

        # Report log file path to stderr and close file logging
        if log_file is not None and _file_console is not None:
            _verbose_console.print(f"[dim]Log written to: {log_file}[/dim]")
        close_file_logging()


def format_routes(routes: list[dict[str, Any]]) -> str:
    """Format routes for display in the dry-run table.

    Args:
        routes: List of route dictionaries with 'to', 'when', and 'is_conditional' keys.

    Returns:
        Formatted string representation of routes.
    """
    if not routes:
        return "[dim]$end[/dim]"

    parts = []
    for route in routes:
        if route.get("is_conditional"):
            condition = route.get("when", "?")
            # Truncate long conditions
            if len(condition) > 40:
                condition = condition[:37] + "..."
            parts.append(f"→ {route['to']} [dim](if {condition})[/dim]")
        else:
            parts.append(f"→ {route['to']}")
    return "\n".join(parts) if parts else "[dim]$end[/dim]"


def display_execution_plan(plan: ExecutionPlan, console: Console | None = None) -> None:
    """Display execution plan with Rich formatting.

    Renders a formatted view of the execution plan including workflow
    metadata, agent sequence with models, and routing information.

    Args:
        plan: The execution plan to display.
        console: Optional Rich console. Creates one if not provided.
    """
    output_console = console if console is not None else Console()

    # Header panel with workflow metadata
    timeout_display = f"{plan.timeout_seconds}s" if plan.timeout_seconds else "unlimited"
    header_content = (
        f"[bold]Workflow:[/bold] {plan.workflow_name}\n"
        f"[bold]Entry Point:[/bold] {plan.entry_point}\n"
        f"[bold]Max Iterations:[/bold] {plan.max_iterations}\n"
        f"[bold]Timeout:[/bold] {timeout_display}"
    )
    output_console.print(Panel(header_content, title="[cyan]Execution Plan (Dry Run)[/cyan]"))

    # Steps table
    table = Table(title="Agent Sequence", show_lines=True)
    table.add_column("Step", style="cyan", justify="right", width=6)
    table.add_column("Agent", style="green")
    table.add_column("Type", width=12)
    table.add_column("Model", width=20)
    table.add_column("Routes")

    for i, step in enumerate(plan.steps, 1):
        routes_str = format_routes(step.routes)
        loop_marker = " [yellow](loop target)[/yellow]" if step.is_loop_target else ""

        # Handle parallel groups differently
        if step.agent_type == "parallel_group":
            # Show parallel group with failure mode
            failure_mode_display = step.failure_mode or "fail_fast"
            model_info = f"[dim]{failure_mode_display}[/dim]"

            table.add_row(
                str(i),
                f"{step.agent_name}{loop_marker}",
                step.agent_type,
                model_info,
                routes_str,
            )

            # Add a detail row showing which agents execute in parallel
            if step.parallel_agents:
                agents_display = ", ".join(
                    f"[cyan]{agent}[/cyan]" for agent in step.parallel_agents
                )
                table.add_row(
                    "",
                    f"[dim]  ⚡ {agents_display}[/dim]",
                    "",
                    "",
                    "",
                )
        else:
            table.add_row(
                str(i),
                f"{step.agent_name}{loop_marker}",
                step.agent_type,
                step.model or "[dim]default[/dim]",
                routes_str,
            )

    output_console.print(table)

    # Print summary
    output_console.print()
    parallel_group_count = sum(1 for s in plan.steps if s.agent_type == "parallel_group")
    total_parallel_agents = sum(
        len(s.parallel_agents or []) for s in plan.steps if s.agent_type == "parallel_group"
    )

    summary_parts = [
        f"[dim]Total steps:[/dim] {len(plan.steps)}",
        f"[dim]Loop targets:[/dim] {sum(1 for s in plan.steps if s.is_loop_target)}",
    ]

    if parallel_group_count > 0:
        summary_parts.append(f"[dim]Parallel groups:[/dim] {parallel_group_count}")
        summary_parts.append(f"[dim]Parallel agents:[/dim] {total_parallel_agents}")

    output_console.print(" | ".join(summary_parts))


def build_dry_run_plan(workflow_path: Path) -> ExecutionPlan:
    """Build an execution plan for dry-run mode.

    Loads the workflow configuration and builds an execution plan
    without creating a provider or executing any agents.

    Args:
        workflow_path: Path to the workflow YAML file.

    Returns:
        ExecutionPlan showing the workflow structure.
    """
    # Load configuration
    config = load_config(workflow_path)

    # Create engine without provider (we won't execute anything)
    # We need a dummy provider for the constructor, but we won't use it
    # Instead, we'll create a minimal WorkflowEngine-like object
    # Actually, let's refactor to allow None provider for dry-run

    # For now, we'll create a minimal engine setup
    from conductor.engine.context import WorkflowContext
    from conductor.engine.limits import LimitEnforcer
    from conductor.engine.router import Router
    from conductor.executor.template import TemplateRenderer

    # Create a partial engine with just what we need for plan building
    class _DryRunEngine:
        def __init__(self, cfg: Any) -> None:
            self.config = cfg
            self.context = WorkflowContext()
            self.renderer = TemplateRenderer()
            self.router = Router()
            self.limits = LimitEnforcer(
                max_iterations=cfg.workflow.limits.max_iterations,
                timeout_seconds=cfg.workflow.limits.timeout_seconds,
            )

        def _find_agent(self, name: str) -> Any:
            return next((a for a in self.config.agents if a.name == name), None)

    # Use a real WorkflowEngine but with a mock provider
    from conductor.config.schema import AgentDef
    from conductor.providers.base import AgentOutput, AgentProvider

    class _MockProvider(AgentProvider):
        async def execute(
            self,
            agent: AgentDef,
            context: dict[str, Any],
            rendered_prompt: str,
            tools: list[str] | None = None,
            interrupt_signal: asyncio.Event | None = None,
            event_callback: Any = None,
        ) -> AgentOutput:
            return AgentOutput(content={}, raw_response="")

        async def validate_connection(self) -> bool:
            return True

        async def close(self) -> None:
            pass

    engine = WorkflowEngine(config, provider=_MockProvider())
    return engine.build_execution_plan()


def _print_resume_instructions(engine: WorkflowEngine) -> None:
    """Print checkpoint path and resume instructions to stderr.

    Called after ``engine.run()`` raises. Only prints if the engine
    successfully saved a checkpoint (``_last_checkpoint_path`` is set).

    Args:
        engine: The workflow engine that failed.
    """
    checkpoint_path = engine._last_checkpoint_path
    if checkpoint_path is None:
        return

    _verbose_console.print()
    _verbose_console.print(f"[bold yellow]Workflow state saved to:[/bold yellow] {checkpoint_path}")
    _verbose_console.print(
        f"[bold yellow]Resume with:[/bold yellow] conductor resume --from {checkpoint_path}"
    )
    if engine.workflow_path is not None:
        _verbose_console.print(
            f"[dim]Or resume latest checkpoint:[/dim] conductor resume {engine.workflow_path}"
        )
    _verbose_console.print()


async def resume_workflow_async(
    workflow_path: Path | None = None,
    checkpoint_path: Path | None = None,
    skip_gates: bool = False,
    log_file: Path | None = None,
    no_interactive: bool = False,
) -> dict[str, Any]:
    """Resume a workflow from a checkpoint.

    Loads a checkpoint file, reconstructs workflow state, and resumes
    execution from the failed agent.

    Args:
        workflow_path: Path to the workflow YAML file. Used to find
            the latest checkpoint if ``checkpoint_path`` is not provided.
        checkpoint_path: Explicit path to a checkpoint file. Takes
            precedence over ``workflow_path``.
        skip_gates: If True, auto-selects first option at human gates.
        log_file: Optional path to write full debug output to a file.
        no_interactive: If True, disables the keyboard interrupt listener.

    Returns:
        The workflow output as a dictionary.

    Raises:
        CheckpointError: If the checkpoint cannot be loaded or is invalid.
        ConductorError: If workflow execution fails.
    """
    from conductor.engine.checkpoint import CheckpointManager
    from conductor.engine.context import WorkflowContext
    from conductor.engine.limits import LimitEnforcer
    from conductor.exceptions import CheckpointError

    start_time = time.time()

    # Initialize file logging if requested
    if log_file is not None:
        try:
            init_file_logging(log_file)
        except OSError as e:
            _verbose_console.print(
                f"[bold yellow]Warning:[/bold yellow] Cannot open log file {log_file}: {e}"
            )

    try:
        # Resolve checkpoint file
        if checkpoint_path is not None:
            verbose_log(f"Loading checkpoint: {checkpoint_path}")
            cp = CheckpointManager.load_checkpoint(checkpoint_path)
        elif workflow_path is not None:
            verbose_log(f"Finding latest checkpoint for: {workflow_path}")
            latest = CheckpointManager.find_latest_checkpoint(workflow_path)
            if latest is None:
                raise CheckpointError(
                    f"No checkpoints found for workflow: {workflow_path.name}",
                    suggestion=f"Run the workflow first: conductor run {workflow_path}",
                )
            verbose_log(f"Found checkpoint: {latest}")
            cp = CheckpointManager.load_checkpoint(latest)
        else:
            raise CheckpointError(
                "Either workflow path or --from checkpoint path is required",
                suggestion="Use: conductor resume workflow.yaml "
                "or conductor resume --from <checkpoint.json>",
            )

        # Resolve workflow path from checkpoint if not provided
        resolved_workflow_path = workflow_path or Path(cp.workflow_path)
        if not resolved_workflow_path.exists():
            raise CheckpointError(
                f"Workflow file not found: {resolved_workflow_path}",
                suggestion="Ensure the workflow file exists at the original path",
                checkpoint_path=str(cp.file_path),
            )

        # Compare workflow hashes — warn if different
        current_hash = CheckpointManager.compute_workflow_hash(resolved_workflow_path)
        if current_hash != cp.workflow_hash:
            _verbose_console.print(
                "[bold yellow]⚠ Warning:[/bold yellow] "
                "Workflow file has changed since checkpoint was created. "
                "Resume may produce unexpected results."
            )

        # Log checkpoint details
        verbose_log(f"Resuming from agent: {cp.current_agent}")
        verbose_log(
            f"Checkpoint created: {cp.created_at} (failed at: {cp.failure.get('agent', 'unknown')})"
        )

        # Load workflow config
        config = load_config(resolved_workflow_path)

        # Verify the current_agent exists in the workflow
        agent_names = {a.name for a in config.agents}
        parallel_names = {g.name for g in config.parallel} if config.parallel else set()
        for_each_names = {g.name for g in config.for_each} if config.for_each else set()
        all_names = agent_names | parallel_names | for_each_names
        if cp.current_agent not in all_names:
            raise CheckpointError(
                f"Agent '{cp.current_agent}' from checkpoint not found in workflow",
                suggestion=(
                    "The workflow may have been modified. "
                    "Check that the agent still exists, or re-run the workflow."
                ),
                checkpoint_path=str(cp.file_path),
            )

        # Reconstruct state from checkpoint
        restored_context = WorkflowContext.from_dict(cp.context)
        restored_limits = LimitEnforcer.from_dict(
            cp.limits,
            timeout_seconds=config.workflow.limits.timeout_seconds,
        )

        # Build MCP servers config (same as run_workflow_async)
        mcp_servers = await _build_mcp_servers(config)

        # Create engine and restore state
        async with ProviderRegistry(config, mcp_servers=mcp_servers) as registry:
            verbose_log("Starting resumed workflow execution...")

            # Pass stored session IDs to registry for Copilot session resume
            if cp.copilot_session_ids:
                registry.set_resume_session_ids(cp.copilot_session_ids)

            # Set up interrupt listener if interactive mode is enabled
            interrupt_event: asyncio.Event | None = None
            listener = None
            if not no_interactive and sys.stdin.isatty():
                from conductor.interrupt.listener import KeyboardListener

                interrupt_event = asyncio.Event()
                listener = KeyboardListener(interrupt_event=interrupt_event)

            engine = WorkflowEngine(
                config,
                registry=registry,
                skip_gates=skip_gates,
                workflow_path=resolved_workflow_path,
                interrupt_event=interrupt_event,
                keyboard_listener=listener,
            )
            engine.set_context(restored_context)
            engine.set_limits(restored_limits)

            try:
                if listener is not None:
                    await listener.start()
                    _verbose_console.print("[dim]Press Esc to interrupt and provide guidance[/dim]")

                result = await engine.resume(cp.current_agent)
            except BaseException:
                _print_resume_instructions(engine)
                raise
            finally:
                if listener is not None:
                    await listener.stop()

            # Log completion
            verbose_log_timing("Total resumed execution", time.time() - start_time)
            verbose_log("Workflow resumed successfully", style="green")

            # Display usage summary if cost tracking is enabled
            if config.workflow.cost.show_summary:
                summary = engine.get_execution_summary()
                if "usage" in summary:
                    display_usage_summary(summary["usage"])

            # Cleanup checkpoint after successful resume
            CheckpointManager.cleanup(cp.file_path)
            verbose_log(f"Checkpoint cleaned up: {cp.file_path}", style="dim")

            return result
    finally:
        # Report log file path to stderr and close file logging
        if log_file is not None and _file_console is not None:
            _verbose_console.print(f"[dim]Log written to: {log_file}[/dim]")
        close_file_logging()


async def _build_mcp_servers(config: Any) -> dict[str, Any] | None:
    """Build MCP server configurations from workflow config.

    Extracted from ``run_workflow_async`` for reuse in ``resume_workflow_async``.

    Args:
        config: The workflow configuration.

    Returns:
        MCP server configurations dict, or None if none configured.
    """
    if not config.workflow.runtime.mcp_servers:
        return None

    mcp_servers: dict[str, Any] = {}
    for name, server in config.workflow.runtime.mcp_servers.items():
        if server.type in ("http", "sse"):
            server_config: dict[str, Any] = {
                "type": server.type,
                "url": server.url,
                "tools": server.tools,
            }
            if server.headers:
                server_config["headers"] = server.headers
            if server.timeout:
                server_config["timeout"] = server.timeout
            server_config = await resolve_mcp_server_auth(name, server_config)
        else:
            server_config = {
                "type": "stdio",
                "command": server.command,
                "args": server.args,
                "tools": server.tools,
            }
            if server.env:
                server_config["env"] = resolve_mcp_env_vars(server.env)
            if server.timeout:
                server_config["timeout"] = server.timeout
        mcp_servers[name] = server_config
    verbose_log(f"MCP servers configured: {list(mcp_servers.keys())}")
    return mcp_servers
