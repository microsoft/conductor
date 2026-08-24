"""Implementation of the 'conductor run' command.

This module provides helper functions for executing workflow files.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from conductor.config.loader import load_config
from conductor.console import MarkupFreeConsole, join, make_console, styled
from conductor.engine.workflow import ExecutionPlan, WorkflowEngine
from conductor.exceptions import WorkflowTerminated
from conductor.mcp_auth import resolve_mcp_server_config
from conductor.providers.registry import ProviderRegistry

if TYPE_CHECKING:
    from conductor.config.instructions import DiscoveredInstruction
    from conductor.config.schema import ProviderSettings, WorkflowConfig
    from conductor.events import WorkflowEvent
    from conductor.fleet.records import RunMode


logger = logging.getLogger(__name__)


# Verbose console for logging (stderr).
#
# This subclass enforces the global --silent contract ("No progress output.
# Only JSON result on stdout.") at the source: every ``.print()`` call on a
# ``_SilentAwareConsole`` becomes a no-op when ``is_verbose()`` is False, so
# individual call sites do not each have to remember to gate themselves
# (see issue #209, follow-up to #203/#211). File logging is unaffected —
# ``_file_console`` is a separate Console wired up by ``init_file_logging``.
#
# Scope: only ``.print()`` is gated. Other Rich ``Console`` output methods
# (``.log()``, ``.rule()``, ``.status()``, ``.print_json()``, etc.) would
# silently bypass ``--silent`` if used on this instance. All current call
# sites in this module use only ``.print``; if you introduce a new one,
# either route it through ``.print`` or extend this subclass.
class _SilentAwareConsole(MarkupFreeConsole):
    """``Console`` that honors ``--silent`` at the print level.

    The instance is locked to ``stderr=True`` to preserve the contract that
    ``--silent`` runs emit JSON on stdout with nothing else; routing gated
    output to stdout would corrupt that channel.

    It is also locked to ``markup=False``, matching ``make_console``: this
    console renders workflow, agent and for-each iteration names, so a plain
    string must not be parsed as styling (#406). Style with ``styled``.
    """

    def __init__(self, **kwargs: Any) -> None:
        # Lock stderr=True and markup=False; everything else (highlight,
        # width, etc.) is caller-tunable.
        kwargs.pop("stderr", None)
        super().__init__(stderr=True, **kwargs)

    def print(self, *args: Any, **kwargs: Any) -> None:
        # Lazy import to avoid the cli.run -> cli.app import cycle at module
        # load time.
        from conductor.cli.app import is_verbose

        if is_verbose():
            super().print(*args, **kwargs)


_verbose_console = _SilentAwareConsole(highlight=False)

# File console for file logging (None when not active)
_file_console: Console | None = None
_file_handle: Any = None


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


def _resolve_event_log_dir(
    event_log_dir: str | None,
    workflow_path: Path,
) -> Path | None:
    """Resolve an event-log directory relative to its workflow file."""
    if not event_log_dir:
        return None

    path = Path(event_log_dir)
    if not path.is_absolute():
        path = workflow_path.resolve().parent / path
    return path.resolve()


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
    # markup=False because this console renders agent-supplied text (prompts,
    # tool output, model responses) verbatim. Rich would otherwise parse a
    # bracketed token such as ``[/nestedType]`` -- ordinary technical prose --
    # as a closing tag and raise MarkupError, killing the run (see #382).
    # Consequence: a markup-bearing renderable now prints its tags literally
    # here, so style file output with ``rich.text.Text`` rather than markup.
    _file_console = Console(
        file=_file_handle, no_color=True, highlight=False, width=200, markup=False
    )


def close_file_logging() -> None:
    """Close file logging and clean up resources."""
    global _file_console, _file_handle
    _file_console = None
    if _file_handle is not None:
        _file_handle.close()
        _file_handle = None


def verbose_log(message: str | Text, style: str = "dim") -> None:
    """Log a message if verbose mode is enabled.

    Args:
        message: The message to log. A ``str`` is treated as literal text; a
            ``Text`` (e.g. from ``styled``) keeps its styling. Accepting both
            matters because an f-string renders a ``Text`` as its plain form,
            silently discarding the styling a caller went out of its way to
            build (#406).
        style: Rich style for the message.
    """
    from conductor.cli.app import is_verbose

    # ``Text`` not an f-string for a plain ``str``: ``message`` is
    # agent-supplied, and interpolating it into markup makes a bracketed token
    # like ``[/nestedType]`` a closing tag and raises MarkupError (#382).
    # ``style=`` does not disable markup parsing, so it is not a substitute.
    renderable = message if isinstance(message, Text) else Text(message)
    if is_verbose():
        _verbose_console.print(renderable, style=style)
    if _file_console is not None:
        _file_console.print(renderable)


def _is_scoped_bg_child(web_bg: bool, web_port: int) -> bool:
    """Whether this invocation is genuinely the ``--web-bg`` child the launcher tracks.

    ``web_bg=True`` (the CLI flag on *this* invocation) is authoritative on
    its own. Otherwise, ``CONDUCTOR_WEB_BG=1`` alone is not — it is set on
    the bg child's environment by ``bg_runner._build_bg_env`` and, being a
    normal env var, is inherited by *every* descendant of that child, not
    just the one process the launcher is watching. A workflow that shells
    out (a ``type: script`` step, an agent's shell tool) and happens to
    invoke a fresh, non-bg ``conductor run --web`` would otherwise inherit
    ``CONDUCTOR_WEB_BG=1`` and be misidentified as the tracked bg child too
    (see ``cli/self_run.py``'s docstring, which documents and relies on this
    same inheritance for a different feature — issue #399).

    Cross-checking ``CONDUCTOR_WEB_PORT`` (also set by ``_build_bg_env``,
    to the exact port ``--web-port`` binds on the tracked child) against
    this invocation's own *web_port* mirrors ``self_run.py``'s signal 2:
    only the literal child ``_spawn_bg_child`` launched has both env vars
    agreeing with its own port.
    """
    if web_bg:
        return True
    if os.environ.get("CONDUCTOR_WEB_BG") != "1":
        return False
    try:
        inherited_port = int(os.environ.get("CONDUCTOR_WEB_PORT", ""))
    except ValueError:
        return False
    return inherited_port == web_port


def _describe_provider(provider: ProviderSettings) -> str:
    """Render a redacted single-line description of provider settings.

    Used in verbose logs to surface structured provider settings without
    leaking values from ``SecretStr`` fields.
    """
    if not provider.has_structured_config():
        return provider.name
    parts: list[str] = [provider.name]
    if provider.type:
        parts.append(f"type={provider.type}")
    if provider.wire_api:
        parts.append(f"wire_api={provider.wire_api}")
    if provider.base_url:
        parts.append(f"base_url={provider.base_url}")
    if provider.api_key is not None:
        parts.append("api_key=***")
    if provider.bearer_token is not None:
        parts.append("bearer_token=***")
    if provider.headers:
        parts.append(f"headers={sorted(provider.headers)}")
    if provider.azure is not None and provider.azure.api_version:
        parts.append(f"azure.api_version={provider.azure.api_version}")
    if provider.runtime_url:
        parts.append(f"runtime_url={provider.runtime_url}")
    if provider.runtime_token is not None:
        parts.append("runtime_token=***")
    return " ".join(parts)


def _apply_provider_override(config: WorkflowConfig, provider_override: str | None) -> None:
    """Replace runtime provider settings, warning when structured config is dropped."""
    if not provider_override:
        return

    had_structured = config.workflow.runtime.provider.has_structured_config()
    verbose_log(f"Provider override: {provider_override}", style="yellow")
    if had_structured:
        verbose_log(
            "Provider override discards structured runtime.provider settings "
            "(routing/runtime connection/etc.) from YAML; using SDK defaults.",
            style="yellow",
        )
    config.workflow.runtime.provider = provider_override  # type: ignore[assignment]


def verbose_log_agent_start(agent_name: str, iteration: int) -> None:
    """Log agent execution start with visual formatting.

    Args:
        agent_name: Name of the agent being executed.
        iteration: Current iteration number (1-indexed).
    """
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
        # ``content`` is agent-supplied (rendered prompts, tool output, model
        # responses) and must not be parsed as markup -- a bracketed token like
        # ``[/nestedType]`` in ordinary technical prose would raise MarkupError
        # and kill the run (#382). ``Text`` rather than ``escape``: escaping is
        # not byte-exact for input that already contains a backslash before a
        # bracket (``\[0-9\]+`` renders as ``[0-9\]+``).
        #
        # ``title`` is *not* conductor-controlled, as this comment used to
        # claim. Its only non-constant caller passes ``Prompt for '<agent>'``,
        # and inside a for-each group the engine rewrites that name to
        # ``<agent>[<key>]`` where the key comes from the source item -- so a
        # key of ``task1`` erased the iteration identity the qualified name
        # exists to carry, and one of ``/etc/x`` killed the run from a logging
        # call (#406). ``Panel`` parses its title with ``Text.from_markup``
        # regardless of the console's ``markup=False``, so this has to be a
        # ``Text``, not an f-string.
        _verbose_console.print(
            Panel(
                Text(content),
                title=styled("[cyan]{}[/cyan]", title),
                border_style="dim",
            )
        )

    # File always gets full untruncated content
    if _file_console is not None:
        # Deliberately not escaped: ``_file_console`` has ``markup=False``, so
        # escaping here would write literal backslashes into the log. The title
        # still needs wrapping -- ``markup=False`` does not reach it.
        _file_console.print(Panel(content, title=Text(title), border_style="dim"))


def verbose_log_timing(operation: str, elapsed: float) -> None:
    """Log timing information if verbose mode is enabled.

    Args:
        operation: Description of the operation.
        elapsed: Elapsed time in seconds.
    """
    from conductor.cli.app import is_verbose

    if is_verbose():
        _verbose_console.print(styled("[dim]⏱ {}: {:.2f}s[/dim]", operation, elapsed))
    if _file_console is not None:
        _file_console.print(f"⏱ {operation}: {elapsed:.2f}s")


def verbose_log_parallel_start(group_name: str, agent_count: int) -> None:
    """Log parallel group execution start.

    Args:
        group_name: Name of the parallel group.
        agent_count: Number of agents in the group.
    """
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
        # ``Text``: ``error_msg`` carries a provider exception message, so a run
        # that is already failing would otherwise have its real error replaced
        # by a MarkupError. ``style=`` does not disable markup parsing (#382).
        _verbose_console.print(Text(error_msg), style="red dim")
    if _file_console is not None:
        _file_console.print(text)
        _file_console.print(error_msg)


def verbose_log_agent_timeout(
    agent_name: str,
    elapsed: float,
    timeout_seconds: float,
) -> None:
    """Log agent timeout.

    Args:
        agent_name: Name of the agent that timed out.
        elapsed: Elapsed time in seconds.
        timeout_seconds: Configured timeout limit.
    """
    from conductor.cli.app import is_verbose

    should_console = is_verbose()
    should_file = _file_console is not None
    if not should_console and not should_file:
        return

    text = Text()
    text.append("  ⏱ ", style="yellow")
    text.append(agent_name, style="yellow bold")
    text.append(f"  timed out after {elapsed:.1f}s (limit: {timeout_seconds:.0f}s)", style="dim")

    if should_console:
        _verbose_console.print(text)
    if _file_console is not None:
        _file_console.print(text)


def verbose_log_budget_exceeded(
    budget_usd: float,
    spent_usd: float,
    budget_mode: str,
    current_agent: str | None = None,
) -> None:
    """Log a cost-budget overshoot.

    Renders the ``budget_exceeded`` event so audit-mode overshoots are
    visible on the console/log instead of only reaching the logging
    lastResort stderr handler.

    Args:
        budget_usd: Configured budget limit in USD.
        spent_usd: Cumulative spend that crossed the budget.
        budget_mode: Active mode (``audit`` or ``enforce``).
        current_agent: Agent executing when the budget was exceeded.
    """
    from conductor.cli.app import is_verbose

    should_console = is_verbose()
    should_file = _file_console is not None
    if not should_console and not should_file:
        return

    style = "red bold" if budget_mode == "enforce" else "yellow bold"
    text = Text()
    text.append("  💸 budget exceeded ", style=style)
    text.append(f"(${spent_usd:.2f} of ${budget_usd:.2f}, {budget_mode} mode)", style="dim")
    if current_agent:
        text.append(f" at agent '{current_agent}'", style="dim")

    if should_console:
        _verbose_console.print(text)
    if _file_console is not None:
        _file_console.print(text)


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
        # ``Text``: ``error_msg`` carries a provider exception message, so a run
        # that is already failing would otherwise have its real error replaced
        # by a MarkupError. ``style=`` does not disable markup parsing (#382).
        _verbose_console.print(Text(error_msg), style="red dim")
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


# Tracks which experimental-provider banners have been printed during the
# current process lifetime so that synthetic ``workflow_started`` events
# emitted during resume (which already replayed the same workflow once)
# don't print the banner twice.
_PRINTED_EXPERIMENTAL_BANNERS: set[str] = set()


def _maybe_print_experimental_banner(data: dict[str, Any]) -> None:
    """Print one Rich banner per unique experimental provider in the workflow.

    Reads ``workflow_started.providers`` (the per-provider tier metadata
    block) and prints a yellow banner per provider with ``tier ==
    "experimental"``. Uses the auto-generated limitations list from the
    capability descriptor so the operator can see at a glance what's
    missing. Idempotent across resume replays via the module-level
    ``_PRINTED_EXPERIMENTAL_BANNERS`` guard.

    No-op when the providers block is absent (older event payloads) or
    contains only stable providers — keeps the run console clean for the
    common case.
    """
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return

    # ``run_id`` sits at top level in build_workflow_started_data() AND
    # is also mirrored into the ``system`` block. Read either so the
    # banner key stays unique across re-emitted events whether tests
    # construct synthetic data or real engine output.
    run_id = (
        data.get("run_id")
        or (data.get("system", {}) if isinstance(data.get("system"), dict) else {}).get("run_id")
        or ""
    )

    from rich.panel import Panel

    for provider_name, meta in providers.items():
        if not isinstance(meta, dict):
            continue
        if meta.get("tier") != "experimental":
            continue

        banner_key = f"{run_id}:{provider_name}"
        if banner_key in _PRINTED_EXPERIMENTAL_BANNERS:
            continue
        _PRINTED_EXPERIMENTAL_BANNERS.add(banner_key)

        pin = meta.get("upstream_pin")
        maintainer = meta.get("maintainer")

        # Re-resolve capabilities from the provider name to compute the
        # limitations list. We don't ship the capability dump on the wire
        # (it's not consumed by any frontend code today), so this single
        # extra lookup keeps the limitation logic in one place AND keeps
        # the JSONL payload lean.
        limitations: list[str] = []
        try:
            from conductor.providers.capabilities import get_capabilities

            limitations = get_capabilities(provider_name).declared_limitations()
        except (KeyError, AttributeError, ImportError) as exc:
            # Provider unknown to the resolver or missing CAPABILITIES.
            # Same fallback as engine — log and print banner without
            # limitations rather than crashing.
            logger.warning(
                "Could not resolve capabilities for experimental provider %r: %s. "
                "Banner will omit the limitations line.",
                provider_name,
                exc,
            )

        header_bits = [styled("[bold]{}[/bold]", provider_name)]
        if pin:
            header_bits.append(styled("([dim]{}[/dim])", pin))
        if maintainer:
            header_bits.append(styled("maintained by [dim]{}[/dim]", maintainer))
        header = join(" ", header_bits)

        body_lines = [styled("⚠ Experimental provider in use: {}", header)]
        if limitations:
            body_lines.append(Text("Limitations: " + ", ".join(limitations) + "."))
        body_lines.append(
            Text.from_markup(
                "See [link]docs/providers/experimental.md[/link] for stability policy."
            )
        )

        # Built as ``Text`` rather than a markup-bearing string: this panel
        # goes to both consoles, and both have ``markup=False``, so a raw
        # string would write ``[bold]``/``[dim]`` tags literally instead of
        # styling them. Resolving the markup once here renders identically on
        # both sinks.
        panel = Panel(
            join("\n", body_lines),
            border_style="yellow",
            expand=False,
        )
        # Route through the silent-aware verbose console so ``--silent`` (JSON
        # output only) suppresses the banner consistently with every other
        # progress-style print in this module. The banner is a warning, but
        # ``--silent`` is the user's explicit "JSON-only" contract — emitting
        # arbitrary Rich panels would corrupt that.
        _verbose_console.print(panel)
        if _file_console is not None:
            _file_console.print(panel)


class ConsoleEventSubscriber:
    """Subscribes to WorkflowEventEmitter and drives console/file logging.

    Maps each event type to the corresponding ``verbose_log_*`` call so that
    ``workflow.py`` only needs to emit events — display logic stays here.
    """

    def on_event(self, event: WorkflowEvent) -> None:
        d = event.data
        t = event.type

        if t == "workflow_started":
            _maybe_print_experimental_banner(d)

        elif t == "agent_started":
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

        elif t == "agent_timeout":
            verbose_log_agent_timeout(
                d.get("agent_name", "?"),
                d.get("elapsed", 0.0),
                d.get("timeout_seconds", 0.0),
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

        elif t in ("script_completed", "set_completed"):
            verbose_log_agent_complete(
                d.get("agent_name", "?"),
                d.get("elapsed", 0.0),
            )

        elif t == "budget_exceeded":
            verbose_log_budget_exceeded(
                d.get("budget_usd", 0.0),
                d.get("spent_usd", 0.0),
                d.get("budget_mode", "audit"),
                d.get("current_agent"),
            )

        elif t == "wait_completed":
            interrupted = d.get("interrupted", False)
            waited = d.get("waited_seconds", d.get("elapsed", 0.0))
            suffix = " (interrupted)" if interrupted else ""
            verbose_log(f"  Wait done: {d.get('agent_name', '?')} after {waited:.2f}s{suffix}")

        elif t == "wait_failed":
            verbose_log(
                f"  Wait failed: {d.get('agent_name', '?')} — "
                f"{d.get('error_type', 'Error')}: {d.get('message', 'unknown')}",
                style="red",
            )

        elif t == "agent_validator_start":
            verbose_log(f"  Validating '{_validator_label(d)}' output…", style="cyan")

        elif t == "agent_validator_complete":
            label = _validator_label(d)
            cost = d.get("cost_usd")
            cost_str = f" · ${cost:.4f}" if isinstance(cost, int | float) else ""
            if d.get("errored"):
                verbose_log(
                    f"  Validation error for '{label}' (treated as pass){cost_str}",
                    style="yellow",
                )
            elif d.get("passed", True):
                verbose_log(f"  Validation passed for '{label}'{cost_str}", style="green")
            # Failure detail is emitted via agent_validation_failed below.

        elif t == "agent_validation_failed":
            label = _validator_label(d)
            issues = d.get("issues") or []
            if d.get("rerun_errored"):
                action = "re-run failed — keeping original output"
            elif d.get("will_retry"):
                action = "re-running once with feedback"
            else:
                action = "no retry (max_retries=0)"
            style = "red" if d.get("rerun_errored") else "yellow"
            verbose_log(
                f"  Validation failed for '{label}' ({len(issues)} issue(s)) — {action}:",
                style=style,
            )
            for issue in issues:
                verbose_log(f"    - {issue}", style="dim")

        elif t == "skill_injection_warning":
            # Only reaches the console through this branch: the executor's
            # logger.warning has no handler behind it (see the comment at the
            # emit site in executor/agent.py).
            verbose_log(
                f"  WARNING: agent '{d.get('agent_name')}' injects "
                f"{d.get('bytes', 0):,} bytes (~{d.get('approx_tokens', 0):,} tokens) "
                f"of skill content on every call — provider "
                f"'{d.get('provider')}' has no progressive disclosure "
                f"(runtime.skill_injection.warn_bytes={d.get('warn_bytes', 0):,})",
                style="yellow",
            )
            if breakdown := d.get("breakdown"):
                verbose_log(f"    {breakdown}", style="dim")

        elif t == "questions_answer_rejected":
            # The terminal has no other signal here: the same prompt simply
            # re-appears, so without this the user cannot tell a refusal from
            # a glitch. The dashboard shows it via questions_reject_reason.
            verbose_log(
                f"  {d.get('reason', 'Answer rejected.')}",
                style="yellow",
            )

        elif t == "questions_completed":
            verbose_log(
                f"  Questions {d.get('outcome', 'completed')}: "
                f"{d.get('answered_count', 0)} answered, "
                f"{d.get('skipped_count', 0)} skipped",
                style="dim",
            )

        elif t == "checkpoint_save_failed":
            n = d.get("consecutive_failures", 1)
            # Avoid spamming when every boundary fails (e.g. disk full): warn on
            # the first failure, then every 10th.
            if n == 1 or n % 10 == 0:
                err = d.get("error_type")
                detail = f" ({err})" if err else ""
                verbose_log(
                    f"  WARNING: periodic checkpoint save failed{detail} — "
                    f"this run may not be resumable if it stalls (failure #{n})",
                    style="yellow",
                )

        elif t == "pricing_hook_silent":
            models = d.get("models") or []
            names = ", ".join(models) if models else "any model"
            verbose_log(
                f"  WARNING: the provider returned no live pricing for {names} — "
                f"costs are estimates from the static pricing table",
                style="yellow",
            )

        elif t == "agent_tool_output_truncated":
            tool_name = d.get("tool_name", "?")
            original = d.get("original_chars", "?")
            kept = d.get("kept_chars", "?")
            spill_path = d.get("spill_path")
            if spill_path:
                extra = f"; full output saved at: {spill_path}"
            else:
                extra = "; full output not spilled to disk"
            verbose_log(
                f"  WARNING: tool output truncated for '{tool_name}' "
                f"({original} chars -> {kept} kept{extra})",
                style="yellow",
            )

        elif t == "agent_parse_recovery":
            agent_name = d.get("agent_name", "?")
            attempt = d.get("attempt", "?")
            max_attempts = d.get("max_attempts", "?")
            reason = d.get("reason", "?")
            error = d.get("error", "")
            kind = "output schema mismatch" if reason == "schema" else "invalid JSON"
            detail = f": {error}" if error else ""
            verbose_log(
                f"  WARNING: retrying '{agent_name}' output ({kind}) "
                f"— attempt {attempt}/{max_attempts}{detail}",
                style="yellow",
            )

        elif t == "guidance_received":
            pending = d.get("pending", 1)
            verbose_log(
                f"  Guidance received (pending: {pending}): {d.get('text', '')}",
                style="cyan",
            )

        elif t == "guidance_applied":
            source = d.get("source", "?")
            agent_name = d.get("agent_name")
            target = f" before '{agent_name}'" if agent_name else ""
            verbose_log(
                f"  Guidance applied ({source}){target}: {d.get('text', '')}",
                style="cyan",
            )


def _validator_label(data: dict[str, Any]) -> str:
    """Build an agent label including a for-each ``item_key`` when present."""
    agent = data.get("agent_name", "?")
    item = data.get("item_key")
    return f"{agent}[{item}]" if item is not None else str(agent)


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
    _print(Text.from_markup("[bold cyan]Token Usage Summary[/bold cyan]"))

    # Token totals
    total_input = usage_data.get("total_input_tokens", 0)
    total_output = usage_data.get("total_output_tokens", 0)
    total_tokens = usage_data.get("total_tokens", 0)

    if total_tokens > 0:
        _print(f"  Input:  {total_input:,} tokens", style="dim")
        _print(f"  Output: {total_output:,} tokens", style="dim")
        _print(f"  Total:  {total_tokens:,} tokens", style="dim")
    else:
        _print(Text.from_markup("  [dim]No token data available[/dim]"))

    # Cost breakdown
    total_cost = usage_data.get("total_cost_usd")
    agents = usage_data.get("agents", [])
    unpriced_count = usage_data.get("unpriced_agent_count", 0)
    unpriced_models = usage_data.get("unpriced_models", [])

    def _unpriced_suffix() -> str:
        """Render e.g. ' (2 agents unpriced: gpt-5.5, claude-opus-4.8)'."""
        if not unpriced_count:
            return ""
        noun = "agent" if unpriced_count == 1 else "agents"
        if unpriced_models:
            return f" ({unpriced_count} {noun} unpriced: {', '.join(unpriced_models)})"
        return f" ({unpriced_count} {noun} unpriced)"

    if total_cost is not None and total_cost > 0:
        _print()
        _print(Text.from_markup("[bold cyan]Cost Breakdown:[/bold cyan]"))

        for agent in agents:
            agent_cost = agent.get("cost_usd")
            if agent_cost is not None and agent_cost > 0:
                pct = (agent_cost / total_cost * 100) if total_cost > 0 else 0
                _print(
                    f"  {agent['agent_name']}: ${agent_cost:.4f} ({pct:.0f}%)",
                    style="dim",
                )

        if unpriced_count:
            # Partial total: flag it so a silently-undercounted number is not
            # presented as complete (see #265).
            _print(
                styled(
                    "  [bold]Total: ~${:.4f}[/bold][yellow]{}[/yellow]",
                    total_cost,
                    _unpriced_suffix(),
                )
            )
            _print(
                Text.from_markup(
                    "  [dim]Partial total — some agents' models had no available pricing.[/dim]"
                )
            )
        else:
            _print(styled("  [bold]Total: ${:.4f}[/bold]", total_cost))
    elif total_tokens > 0:
        _print()
        if unpriced_count:
            _print(styled("  [dim]Cost data unavailable{}[/dim]", _unpriced_suffix()))
        else:
            _print(Text.from_markup("  [dim]Cost data unavailable (unknown model pricing)[/dim]"))

    # The provider priced nothing this run, so every figure above that has a
    # cost came from the static table. Without this the summary prints a
    # confident number and the explanation goes only to stderr, where
    # ``--web-bg`` writes it to a temp file nobody was told to read.
    if usage_data.get("live_pricing_degraded"):
        _print(
            Text.from_markup(
                "  [yellow]Live pricing unavailable for every model this run.[/yellow]"
                "[dim] Costs shown are estimates from the static pricing table; "
                "set `cost.pricing` in the workflow to supply rates.[/dim]"
            )
        )

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


def parse_input_json_flags(raw_inputs: list[str]) -> dict[str, Any]:
    """Parse ``--input-json name=value`` flags, strictly JSON-decoding each value.

    This is the Fleet Manager's background-launch typed transport (hidden,
    internal-only flag): ``bg_runner.py::launch_background`` forwards
    already-declared-type-coerced values here, JSON-encoded by
    ``_serialize_input_value``, so they must be decoded with
    :func:`coerce_typed_value` (strict ``json.loads``) rather than the
    public ``--input`` heuristic in :func:`coerce_value`, which would
    reinterpret an already-typed value (e.g. re-guess a JSON-quoted string).

    Args:
        raw_inputs: List of "name=value" strings, each value JSON-encoded.

    Returns:
        Dictionary of parsed input name-value pairs.

    Raises:
        typer.BadParameter: If the format is invalid or a value is not
            valid JSON.
    """
    inputs: dict[str, Any] = {}

    for raw in raw_inputs:
        if "=" not in raw:
            raise typer.BadParameter(
                f"Invalid input-json format: '{raw}'. Expected format: name=value"
            )

        name, value = raw.split("=", 1)
        name = name.strip()

        if not name:
            raise typer.BadParameter(f"Empty input name in: '{raw}'")

        inputs[name] = coerce_typed_value(value)

    return inputs


def parse_metadata_flags(raw_metadata: list[str]) -> dict[str, str]:
    """Parse --metadata key=value flags into a dictionary.

    Unlike ``parse_input_flags``, values are kept as raw strings with no
    type coercion — metadata is opaque key-value data.

    Args:
        raw_metadata: List of "key=value" strings from CLI.

    Returns:
        Dictionary of string key-value pairs.

    Raises:
        typer.BadParameter: If metadata format is invalid.
    """
    result: dict[str, str] = {}

    for raw in raw_metadata:
        if "=" not in raw:
            raise typer.BadParameter(
                f"Invalid metadata format: '{raw}'. Expected format: key=value"
            )

        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip()

        if not key:
            raise typer.BadParameter(f"Empty metadata key in: '{raw}'")

        result[key] = value

    return result


def parse_guidance_flags(raw_guidance: list[str]) -> list[str]:
    """Validate ``--guidance`` flags, mirroring ``POST /api/guidance``.

    ``resume --guidance`` calls :meth:`WorkflowEngine.add_user_guidance`
    directly rather than going through the HTTP endpoint, so without this it
    would skip the non-empty/length checks that endpoint enforces (issue
    #400 review). Validating here — at the CLI boundary, before any
    checkpoint restore or background-process fork — gives the same
    ``typer.BadParameter`` treatment ``--metadata`` gets rather than letting
    an empty or oversized entry reach the engine.

    Args:
        raw_guidance: List of raw ``--guidance`` values from the CLI.

    Returns:
        The stripped, validated guidance texts, in the order given.

    Raises:
        typer.BadParameter: If any entry is empty after stripping, or
            exceeds :data:`conductor.engine.guidance.MAX_GUIDANCE_CHARS`.
    """
    from conductor.engine.guidance import validate_guidance_text

    result: list[str] = []
    for raw in raw_guidance:
        try:
            result.append(validate_guidance_text(raw))
        except ValueError as e:
            raise typer.BadParameter(str(e)) from e
    return result


def coerce_value(value: str) -> Any:
    """Coerce a string value to an appropriate Python type.

    This is the public ``--input``/``--input.*`` parsing heuristic used for
    values a user types directly on the command line -- it must not change,
    since it is a public, backward-compatibility-sensitive contract (e.g.
    ``1e3``, ``NaN``, ``Infinity``, and an already-JSON-quoted string like
    ``'"true"'`` all have established meanings here that differ from strict
    JSON). The Fleet Manager's background launch path has its own strict,
    unambiguous typed transport instead of reusing this heuristic -- see
    :func:`coerce_typed_value` and ``--input-json``.

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


def coerce_typed_value(value: str) -> Any:
    """Strictly decode a JSON-encoded typed value.

    Used only for the Fleet Manager's background-launch input transport
    (the ``--input-json`` flag, populated by
    ``bg_runner.py::launch_background`` from already-declared-type-coerced
    values). Unlike :func:`coerce_value`'s public, user-facing heuristic,
    this never guesses: the value was JSON-encoded by the sender (see
    ``cli/bg_runner.py::_serialize_input_value``), so it is JSON-decoded
    verbatim here, with no ambiguity between e.g. the string ``"true"`` and
    the boolean ``true``.

    Args:
        value: A JSON-encoded string, e.g. ``'"true"'`` or ``'42'``.

    Returns:
        The decoded value.

    Raises:
        typer.BadParameter: If ``value`` is not valid JSON.
    """
    try:
        return json.loads(value)
    except json.JSONDecodeError as e:
        raise typer.BadParameter(f"Invalid --input-json value: {value!r} ({e})") from None


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
    return await _execute_with_stop_signal(engine.run(inputs), dashboard, engine=engine)


async def _resume_with_stop_signal(
    engine: Any,
    current_agent: str,
    dashboard: Any | None,
) -> dict[str, Any]:
    """Resume the workflow engine, racing against a dashboard kill signal.

    Mirrors :func:`_run_with_stop_signal` but invokes ``engine.resume()``.

    Args:
        engine: The ``WorkflowEngine`` instance with restored state.
        current_agent: Name of the agent to resume from.
        dashboard: The ``WebDashboard`` instance, or None.

    Returns:
        The workflow result dict.

    Raises:
        ExecutionError: If the workflow was killed via the dashboard.
    """
    return await _execute_with_stop_signal(engine.resume(current_agent), dashboard, engine=engine)


async def _execute_with_stop_signal(
    engine_coro: Any,
    dashboard: Any | None,
    engine: Any | None = None,
) -> dict[str, Any]:
    """Execute an engine coroutine, racing against a dashboard kill signal.

    Args:
        engine_coro: The coroutine to execute (``engine.run()`` or
            ``engine.resume()``).
        dashboard: The ``WebDashboard`` instance, or None.
        engine: The ``WorkflowEngine`` instance backing ``engine_coro``. When a
            dashboard stop/kill cancels the engine task, this is used to write a
            best-effort checkpoint and emit ``workflow_failed`` so the run is
            never lost silently (issue #245). May be ``None`` (e.g. in unit
            tests that pass a bare coroutine), in which case the checkpoint step
            is skipped.

    Returns:
        The workflow result dict.

    Raises:
        ExecutionError: If the workflow was killed via the dashboard.
    """
    if dashboard is None:
        return await engine_coro

    engine_task = asyncio.create_task(engine_coro)
    stop_task = asyncio.create_task(dashboard.wait_for_stop())

    done, pending = await asyncio.wait(
        {engine_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Cancel any losing task and drain it. Use ``gather(return_exceptions=True)``
    # so a non-CancelledError stored on the losing task (e.g. dashboard.stop
    # raised, or engine raised right as the kill button fired) does not abort
    # the cleanup loop and leak an un-awaited task.
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    if engine_task in done:
        return engine_task.result()

    # Stop/kill won the race. The engine task was in ``pending`` and we just
    # cancelled + drained it. Three outcomes are possible:
    #
    #   * It actually cancelled (``engine_task.cancelled()``) — the engine's
    #     ``except asyncio.CancelledError`` arm ran, which intentionally emits
    #     no ``workflow_failed`` and saves no checkpoint. Give the run a
    #     best-effort checkpoint + terminal event here so progress isn't lost.
    #   * It completed with its own exception (e.g. ``InterruptError`` from a
    #     pause -> Kill that raised inside the loop just as Stop fired). In that
    #     case the engine already emitted ``workflow_failed`` and saved a
    #     checkpoint, so re-raise that exception untouched — do not double-handle.
    #   * It completed with a *result* (swallowed the cancellation and returned).
    #     Unreachable today since ``run``/``resume`` re-raise ``CancelledError``,
    #     but guard against a future refactor by returning that result rather
    #     than emitting a spurious ``workflow_failed`` after ``workflow_completed``.
    if not engine_task.cancelled():
        exc = engine_task.exception()
        if exc is not None:
            raise exc
        return engine_task.result()

    # Single source of truth for the user-facing stop reason: it feeds both the
    # engine's checkpoint/``workflow_failed`` message and the raised exception.
    stop_message = "Workflow stopped by user via dashboard"
    if engine is not None:
        engine.handle_dashboard_stop(stop_message)

    from conductor.exceptions import ExecutionError

    raise ExecutionError(stop_message)


def _emit_loaded_instructions_debug(start_dir: Path | None, enabled: bool) -> None:
    """Discover-and-print the workspace instructions list when the debug flag
    is enabled and auto-discovery actually ran.

    Extracted from :func:`run_workflow_async` so the branch is directly
    testable without spinning up the full workflow runner. ``start_dir`` must
    be the same path that :func:`build_instructions_preamble` was given as
    ``auto_discover_dir`` — sharing the path is what guarantees the printed
    list cannot drift from what was actually loaded.

    Args:
        start_dir: The auto-discovery start directory used by the loader, or
            ``None`` when auto-discovery did not run. ``None`` short-circuits
            so the debug print is a no-op (matching the contract that this
            flag is "meaningful only when --workspace-instructions is set").
        enabled: Whether ``--print-loaded-instructions`` was passed on the
            CLI. ``False`` short-circuits.
    """
    if not enabled or start_dir is None:
        return
    from conductor.config.instructions import discover_workspace_instructions_detailed

    detailed = discover_workspace_instructions_detailed(start_dir)
    _print_loaded_instructions(detailed)


def _print_loaded_instructions(detailed: list[DiscoveredInstruction]) -> None:
    """Emit a human-readable summary of discovered workspace instruction files
    to stderr.

    Format:

    .. code-block:: text

        [workspace-instructions] 4 file(s) loaded from CWD:
          AGENTS.md
            source=AGENTS.md  reason=file-convention
          .github/instructions/csharp-coding-standards.instructions.md
            source=.github/instructions  reason=scope-overlap  applyTo='**/*.cs'
          ...

    Goes to stderr (not stdout) so it doesn't pollute JSON output. Uses a
    plain-print rather than the rich console so it's reliably available in
    background/non-TTY launchers.

    ``DiscoveredInstruction`` is imported under ``TYPE_CHECKING`` only: this
    module has ``from __future__ import annotations``, so the annotation is
    never evaluated at import time, and the lazy import inside
    :func:`_emit_loaded_instructions_debug` is what actually keeps the
    discovery code path out of the module graph when the flag is unused.
    """
    import sys

    from conductor.config.instructions import ALWAYS_ON_SCOPE

    if not detailed:
        # Plain strings on the builtin ``print``: this goes to stderr as a
        # grep label, not through a Rich console. Wrapping it in
        # ``Text.from_markup`` would delete the ``[workspace-instructions]``
        # prefix outright — it starts with a lowercase letter, so Rich reads
        # it as a style tag, and ``print`` then renders the Text's plain form.
        print("[workspace-instructions] 0 files discovered from CWD.", file=sys.stderr)
        return
    print(
        f"[workspace-instructions] {len(detailed)} file(s) discovered from CWD:",
        file=sys.stderr,
    )
    for d in detailed:
        print(f"  {d.path}", file=sys.stderr)
        scope_part = f"  applyTo={d.scope!r}" if d.scope and d.scope != ALWAYS_ON_SCOPE else ""
        print(
            f"    source={d.source}  reason={d.reason}{scope_part}",
            file=sys.stderr,
        )


def _derive_run_mode(*, web: bool, web_bg: bool) -> RunMode:
    """Derive the fleet run-record ``mode`` field from web/web-bg flags.

    Mirrors the ``bg_mode`` expression used elsewhere in this module to
    configure ``WebDashboard`` and ``RunContext.bg_mode`` (the ``web_bg``
    CLI flag OR the ``CONDUCTOR_WEB_BG`` env var set on a ``--web-bg``
    detached child), so a run is never ``"fg-web"`` from the dashboard's
    perspective but ``"bg"`` from the run record's.

    Returns:
        ``"bg"`` for a background run (D1 never prompts for stop
        confirmation on these), ``"fg-web"`` for a foreground run with a
        dashboard, or ``"fg"`` for a plain foreground run.
    """
    if web_bg or os.environ.get("CONDUCTOR_WEB_BG") == "1":
        return "bg"
    return "fg-web" if web else "fg"


def _write_run_record_for_current_process(
    *,
    event_log_subscriber: Any,
    dashboard: Any,
    workflow_path: Path,
    web: bool,
    web_bg: bool,
) -> None:
    """Write (or replace) this process's Fleet Manager run record.

    Called from both ``run_workflow_async`` and ``resume_workflow_async``
    (Fleet Manager E2 — see
    ``docs/projects/fleet-manager/fleet-manager.design.md``) once
    ``run_id``, ``event_log_path``, and the dashboard's actual port (if
    any) are all known, so every execution path — foreground, foreground
    with a dashboard, background, and resumed — produces a discoverable
    record, closing the design's blocking problem that only ``--web-bg``
    runs used to be visible.

    A resumed run reuses ``event_log_subscriber.run_id`` (the checkpoint's
    ``existing_run_id`` when available), so calling this again for the
    same ``run_id`` *replaces* the prior record (``write_run_record``'s
    ``os.replace``) rather than creating a second one.

    ``workflow_name`` is derived from ``workflow_path.stem`` rather than
    the YAML-declared ``config.workflow.name`` — the two can differ, and
    ``CheckpointManager`` names checkpoint files after the workflow file's
    stem (``engine/checkpoint.py`` uses ``workflow_path.stem`` for both
    the checkpoint filename prefix and the periodic-checkpoint glob), so a
    fleet consumer resolving *this run's* checkpoints via
    ``workflow_name`` + ``run_id`` needs the same stem or the lookup silently
    finds nothing.

    Never raises: a failure to write this diagnostic/discovery record must
    not abort the workflow it describes.
    """
    from conductor.cli.self_run import SELF_RUN_ID_ENV
    from conductor.engine.checkpoint import CheckpointManager
    from conductor.fleet.records import RunRecord, write_run_record

    # Export the id so descendants inherit it. This is what makes `stop`'s
    # self-exclusion signal 1 work for a *foreground* run: signal 2 is
    # bg-only by definition and signal 3 walks `/proc`, so without this a fg
    # run has no self-detection at all off Linux, and an agent step running
    # `conductor stop --all` terminates its own workflow (issue #399). Set
    # before the write so it holds even if the write fails.
    os.environ[SELF_RUN_ID_ENV] = event_log_subscriber.run_id

    try:
        write_run_record(
            RunRecord(
                run_id=event_log_subscriber.run_id,
                pid=os.getpid(),
                workflow_path=str(workflow_path),
                workflow_name=workflow_path.stem,
                started_at=datetime.now(UTC).isoformat(),
                event_log_path=str(event_log_subscriber.path),
                port=(dashboard.port if dashboard is not None else None),
                mode=_derive_run_mode(web=web, web_bg=web_bg),
                checkpoint_dir=str(CheckpointManager.get_checkpoints_dir()),
            )
        )
    except Exception as exc:
        logger.warning("Failed to write fleet run record", exc_info=True)
        # Conductor installs no logging handlers, so the line above reaches
        # `logging.lastResort` as unattributed stderr. Without the record
        # this run is invisible to `stop`/`status`/`fleet list`, i.e.
        # silently back to the bug the run record exists to fix, so say so
        # where the user will see it. (Under --web-bg the child's stderr is
        # itself a temp log the parent captures -- this exact warning line
        # is what the parent's `bg_runner._finalize_background_launch`
        # points at via `_tail_log` when its own run-record poll times out.
        # That gate no longer fails the launch over a missing record --
        # issue #435 downgraded it to a warning, surfaced via
        # `cli/app.py::_print_web_bg_no_run_record_notice` -- because the
        # child may be executing perfectly normally; this line is what
        # actually carries the underlying cause into the captured bg
        # stderr log for the user to find.)
        #
        # Guarded with BaseException, not Exception: rich turns a broken
        # pipe into `SystemExit` (`Console._on_broken_pipe`), which would
        # sail past every `except Exception` between here and the top of
        # `conductor run` and kill the workflow this diagnostic describes --
        # breaking this function's "never raises" contract. A full disk
        # triggers both halves at once: the record write fails *and* the
        # stderr write fails.
        try:
            make_console(stderr=True).print(
                styled(
                    "[bold yellow]Warning:[/bold yellow] could not write this run's fleet "
                    "record ({}). It will not appear in `conductor status` / `fleet list` "
                    "and cannot be stopped with `conductor stop`; use Ctrl-C or `kill {}`.",
                    exc,
                    os.getpid(),
                )
            )
        except BaseException:  # noqa: BLE001 - a diagnostic must not kill the run
            logger.debug("Could not print the run-record warning", exc_info=True)


def _remove_run_record_for_current_process_safe() -> None:
    """Remove this process's Fleet Manager run record, tolerating failure.

    Wraps ``conductor.fleet.records.remove_run_record_for_current_process``
    so a failure while scanning/removing the record (e.g. a permission
    error creating/reading ``run_records_dir()``) cannot abort the rest of
    the caller's ``finally`` block — stopping the dashboard, closing the
    event log, and closing file logging must still happen even when this
    diagnostic/discovery cleanup step fails.
    """
    from conductor.fleet.records import remove_run_record_for_current_process

    try:
        remove_run_record_for_current_process()
    except Exception:
        logger.warning("Failed to remove fleet run record", exc_info=True)


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
    metadata: dict[str, str] | None = None,
    workspace_instructions: bool = False,
    cli_instructions: list[str] | None = None,
    print_loaded_instructions: bool = False,
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
        metadata: Optional CLI metadata to merge on top of YAML-declared metadata.
        workspace_instructions: If True, auto-discover workspace instruction files.
        cli_instructions: Optional list of instruction file paths from CLI.
        print_loaded_instructions: If True, print the resolved instruction file
            list (with scope and inclusion reason) to stderr before running.
            No-op unless ``workspace_instructions`` is also True.

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
                styled(
                    "[bold yellow]Warning:[/bold yellow] Cannot open log file {}: {}", log_file, e
                )
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
        # Log workflow loading
        verbose_log(f"Loading workflow: {workflow_path}")

        # Load configuration
        load_start = time.time()
        config = load_config(workflow_path)
        verbose_log_timing("Configuration loaded", time.time() - load_start)

        # Merge CLI metadata on top of YAML-declared metadata
        if metadata:
            config.workflow.metadata.update(metadata)

        # Log workflow details
        verbose_log(f"Workflow: {config.workflow.name}")
        verbose_log(f"Entry point: {config.workflow.entry_point}")
        verbose_log(f"Agents: {len(config.agents)}")

        # Start the dashboard only after config validation succeeds — never
        # before. Binding the port before ``load_config`` meant a workflow
        # that fails to even parse still left a live socket that
        # ``--web-bg``'s launcher (and a concurrent ``conductor status``)
        # would read as "started" (issue #410). Deliberately placed here
        # rather than after ``_build_mcp_servers`` / plugin prefetch below:
        # those can take tens of seconds (git clone), and the launcher's own
        # port-reachability probe must not have to wait them out.
        if dashboard is not None:
            try:
                await dashboard.start()
                from conductor.cli.app import is_verbose

                if is_verbose():
                    _verbose_console.print(
                        styled("[bold cyan]Dashboard:[/bold cyan] {}", dashboard.url)
                    )
            except Exception as e:
                # Never leave ``dashboard`` pointing at one whose ``start()``
                # failed — set this *before* the bg_mode branch below so the
                # unconditional ``finally: dashboard.stop()`` further down
                # can't await a serve task that never came up. Awaiting it
                # would re-raise the same underlying failure (or worse, a
                # bare ``SystemExit`` from uvicorn's own bind-failure path,
                # which isn't even an ``Exception`` and would escape the
                # CLI's error handler) and silently replace the informative
                # RuntimeError below with that raw exception instead.
                dashboard = None
                if _is_scoped_bg_child(web_bg, web_port):
                    # In a ``--web-bg`` child, silently continuing without a
                    # dashboard would leave the port never reachable, which
                    # the launcher's own probe (``bg_runner._wait_for_server``)
                    # interprets as either a false success (fast workflow) or
                    # a reason to kill an otherwise-healthy long-running
                    # workflow (issue #410) — neither of which reports the
                    # real cause. Propagate instead so the child exits
                    # non-zero and the launcher's existing "process exited"
                    # path surfaces this actual error via the stderr log.
                    raise RuntimeError(f"Dashboard failed to start: {e}") from e
                _verbose_console.print(
                    styled(
                        "[bold yellow]Warning:[/bold yellow] Dashboard failed to "
                        "start: {}. Continuing without dashboard.",
                        e,
                    )
                )

        # Start JSONL event log subscriber (always-on structured diagnostics)
        from conductor.engine.event_log import EventLogSubscriber

        _event_log_dir = _resolve_event_log_dir(
            config.workflow.runtime.event_log_dir,
            workflow_path,
        )

        event_log_subscriber = EventLogSubscriber(
            config.workflow.name,
            event_log_dir=_event_log_dir,
        )
        emitter.subscribe(event_log_subscriber.on_event)

        # Write the Fleet Manager run record (E2): this is the first point
        # where run_id, event_log_path, and the already-started dashboard's
        # port (dashboard.start() ran earlier, above, before this try block)
        # are all available.
        _write_run_record_for_current_process(
            event_log_subscriber=event_log_subscriber,
            dashboard=dashboard,
            workflow_path=workflow_path,
            web=web,
            web_bg=web_bg,
        )

        # Opportunistic event-log retention sweep (E5 — D3). Best-effort and
        # settings-driven (enabled by default, keep_last = 200): never
        # raises, and the design measured a full 1522-file scan at
        # ~0.136s, so this cannot meaningfully delay a run.
        from conductor.fleet.retention import maybe_prune_event_logs

        maybe_prune_event_logs()

        # Subscribe console output to the event emitter
        console_subscriber = ConsoleEventSubscriber()
        emitter.subscribe(console_subscriber.on_event)

        if inputs:
            # ``ensure_ascii=False`` so the panel shows real non-ASCII input values
            # rather than ``\uXXXX`` escapes (issue #356).
            verbose_log_section("Workflow Inputs", json.dumps(inputs, indent=2, ensure_ascii=False))

        # Apply provider override if specified.
        # Reassigning ``runtime.provider`` to a string re-triggers the
        # before-validator on ``RuntimeConfig`` and coerces it back to a
        # ``ProviderSettings`` with default fields, intentionally
        # discarding any structured provider config from YAML.
        _apply_provider_override(config, provider_override)

        # Build workspace instructions preamble
        instructions_preamble: str | None = None
        if workspace_instructions or cli_instructions or config.workflow.instructions:
            from conductor.config.instructions import build_instructions_preamble

            # Compute the auto-discovery start dir once and share it between
            # the loader and the --print-loaded-instructions debug dump. If
            # these diverged (e.g. the cwd changed between the two calls, or
            # one was passed a different path), the printed list would no
            # longer reflect what was actually loaded — defeating the whole
            # purpose of the debug flag.
            start_dir = Path.cwd() if workspace_instructions else None

            instructions_preamble = build_instructions_preamble(
                auto_discover_dir=start_dir,
                yaml_instructions=config.workflow.instructions or None,
                cli_instruction_paths=cli_instructions,
            )
            if instructions_preamble:
                verbose_log(
                    f"Workspace instructions loaded ({len(instructions_preamble)} chars)",
                    style="cyan",
                )

            # --print-loaded-instructions: dump the resolved discovery list to
            # stderr for debugging. Only meaningful when auto-discovery ran,
            # and must use the same start_dir as the loader above so the
            # printed list cannot silently drift from what was loaded.
            _emit_loaded_instructions_debug(start_dir, print_loaded_instructions)

        # Convert MCP servers from workflow config to SDK format
        mcp_servers = await _build_mcp_servers(config)

        # Acquire declared plugin sources before anything runs, so a cold
        # cache costs a visible startup step rather than a stalled agent.
        plugin_marketplaces = await _prefetch_plugin_sources(config, workflow_path)

        # Check if workflow uses multiple providers (has per-agent provider overrides)
        uses_multi_provider = any(agent.provider is not None for agent in config.agents)

        if uses_multi_provider:
            verbose_log("Multi-provider mode: agents use different providers", style="cyan")
        else:
            verbose_log(
                f"Single provider mode: {_describe_provider(config.workflow.runtime.provider)}"
            )

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

            from conductor.engine.workflow import RunContext

            engine = WorkflowEngine(
                config,
                registry=registry,
                skip_gates=skip_gates,
                workflow_path=workflow_path,
                interrupt_event=interrupt_event,
                event_emitter=emitter,
                keyboard_listener=listener,
                web_dashboard=dashboard,
                instructions_preamble=instructions_preamble,
                plugin_marketplaces=plugin_marketplaces,
                run_context=RunContext(
                    run_id=event_log_subscriber.run_id if event_log_subscriber else "",
                    log_file=str(event_log_subscriber.path) if event_log_subscriber else "",
                    dashboard_port=(dashboard.port if dashboard is not None else None),
                    bg_mode=web_bg or os.environ.get("CONDUCTOR_WEB_BG") == "1",
                ),
            )

            # Share interrupt_event with dashboard so POST /api/stop can abort agents
            if dashboard is not None and interrupt_event is not None:
                dashboard.set_interrupt_event(interrupt_event)

            # Share the guidance sink with the dashboard so POST /api/guidance
            # can push mid-run text into the engine (issue #400). Unlike
            # set_interrupt_event this doesn't need interrupt_event -- a plain
            # --web run with no keyboard listener still accepts guidance.
            if dashboard is not None:
                dashboard.set_guidance_sink(engine.submit_guidance)

            terminate_exc: WorkflowTerminated | None = None
            try:
                if listener is not None:
                    await listener.start()
                    _verbose_console.print(
                        Text.from_markup("[dim]Press Esc to interrupt and provide guidance[/dim]")
                    )

                result = await _run_with_stop_signal(engine, inputs, dashboard)
            except WorkflowTerminated as exc:
                # Explicit `type: terminate status: failed` is an intentional
                # outcome, not a crash — defer the raise so the dashboard
                # stays alive for the same post-execution lifecycle as a
                # successful run. Without this deferral the dashboard dies
                # immediately and a `--web` / `--web-bg` user cannot see the
                # rendered TerminateNode / red "Workflow Terminated" banner.
                # Resume hint is still suppressed: explicit terminations are
                # not resumable (defense-in-depth — see issue #219).
                terminate_exc = exc
                result = exc.output
            except BaseException:
                _print_resume_instructions(engine)
                raise
            finally:
                if listener is not None:
                    await listener.stop()

            # Log completion
            verbose_log_timing("Total workflow execution", time.time() - start_time)
            if terminate_exc is None:
                verbose_log("Workflow completed successfully", style="green")
            else:
                verbose_log(
                    f"Workflow terminated explicitly at '{terminate_exc.terminated_by}'",
                    style="yellow",
                )

            # Display usage summary if cost tracking is enabled
            if config.workflow.cost.show_summary:
                summary = engine.get_execution_summary()
                if "usage" in summary:
                    display_usage_summary(summary["usage"])

            # Post-execution dashboard lifecycle — runs for both clean exits
            # and explicit-terminate failures so the user can observe the
            # final dashboard state in either case.
            if dashboard is not None:
                # Auto-shutdown if either --web-bg was passed directly or
                # this is a background child process (CONDUCTOR_WEB_BG env var)
                is_bg = web_bg or os.environ.get("CONDUCTOR_WEB_BG") == "1"
                if is_bg:
                    await dashboard.wait_for_clients_disconnect()
                else:
                    from conductor.cli.app import is_verbose

                    if is_verbose():
                        banner = (
                            Text.from_markup("[bold yellow]Workflow terminated.[/bold yellow]")
                            if terminate_exc is not None
                            else Text.from_markup("[bold green]Workflow complete.[/bold green]")
                        )
                        _verbose_console.print(
                            styled(
                                "\n{} Dashboard still running at {} — press "
                                "[bold]Ctrl+C[/bold] to exit.",
                                banner,
                                dashboard.url,
                            )
                        )
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.Event().wait()

            if terminate_exc is not None:
                # Re-raise so the CLI handler emits the non-zero exit code
                # and prints the structured termination message/output.
                raise terminate_exc
            return result
    finally:
        # Clean up the Fleet Manager run record on every exit path (E2 —
        # normal completion, an explicit WorkflowTerminated re-raise, or an
        # unexpected exception all funnel through this finally). Unlike the
        # legacy PID file (removed only by a background child), this runs
        # unconditionally: foreground and foreground-with-dashboard runs now
        # write a record too and must remove it on exit just the same.
        # Guarded (never raises) so a failure here cannot prevent the
        # dashboard/event-log/file-logging cleanup below from running.
        _remove_run_record_for_current_process_safe()

        # Stop dashboard if it was started
        if dashboard is not None:
            await dashboard.stop()

        # Close JSONL event log and report path
        if event_log_subscriber is not None:
            event_log_subscriber.close()
            _verbose_console.print(
                styled("[dim]Event log written to: {}[/dim]", event_log_subscriber.path)
            )

        # Report log file path to stderr and close file logging
        if log_file is not None and _file_console is not None:
            _verbose_console.print(styled("[dim]Log written to: {}[/dim]", log_file))
        close_file_logging()


def format_routes(routes: list[dict[str, Any]]) -> Text:
    """Format routes for display in the dry-run table.

    Args:
        routes: List of route dictionaries with 'to', 'when', and 'is_conditional' keys.

    Returns:
        Formatted representation of routes.
    """
    if not routes:
        return Text.from_markup("[dim]$end[/dim]")

    parts = []
    for route in routes:
        if route.get("is_conditional"):
            condition = route.get("when", "?")
            # Truncate long conditions
            if len(condition) > 40:
                condition = condition[:37] + "..."
            parts.append(styled("→ {} [dim](if {})[/dim]", route["to"], condition))
        else:
            parts.append(f"→ {route['to']}")
    # ``parts`` cannot be empty: ``routes`` is non-empty past the guard above
    # and every iteration appends.
    return join("\n", parts)


def display_execution_plan(plan: ExecutionPlan, console: Console | None = None) -> None:
    """Display execution plan with Rich formatting.

    Renders a formatted view of the execution plan including workflow
    metadata, agent sequence with models, and routing information.

    Args:
        plan: The execution plan to display.
        console: Optional Rich console. Creates one if not provided.
    """
    output_console = console if console is not None else make_console()

    # Header panel with workflow metadata
    timeout_display = f"{plan.timeout_seconds}s" if plan.timeout_seconds else "unlimited"
    header_content = styled(
        "[bold]Workflow:[/bold] {}\n[bold]Entry Point:[/bold] "
        "{}\n[bold]Max Iterations:[/bold] {}\n[bold]Timeout:[/bold] {}",
        plan.workflow_name,
        plan.entry_point,
        plan.max_iterations,
        timeout_display,
    )
    output_console.print(
        Panel(header_content, title=Text.from_markup("[cyan]Execution Plan (Dry Run)[/cyan]"))
    )

    # Steps table
    table = Table(title="Agent Sequence", show_lines=True)
    table.add_column("Step", style="cyan", justify="right", width=6)
    table.add_column("Agent", style="green")
    table.add_column("Type", width=12)
    table.add_column("Model", width=20)
    table.add_column("Routes")

    for i, step in enumerate(plan.steps, 1):
        routes_str = format_routes(step.routes)
        # Interpolated via ``styled`` rather than an f-string at the two call
        # sites below: an f-string renders a ``Text`` as its plain form, which
        # would silently drop the yellow that makes a loop target stand out
        # from the agent names around it (#406).
        loop_marker = (
            Text.from_markup(" [yellow](loop target)[/yellow]") if step.is_loop_target else ""
        )

        # Handle parallel groups differently
        if step.agent_type == "parallel_group":
            # Show parallel group with failure mode
            failure_mode_display = step.failure_mode or "fail_fast"
            model_info = styled("[dim]{}[/dim]", failure_mode_display)

            table.add_row(
                str(i),
                styled("{}{}", step.agent_name, loop_marker),
                step.agent_type,
                model_info,
                routes_str,
            )

            # Add a detail row showing which agents execute in parallel
            if step.parallel_agents:
                agents_display = join(
                    ", ", (styled("[cyan]{}[/cyan]", agent) for agent in step.parallel_agents)
                )
                table.add_row(
                    "",
                    styled("[dim]  ⚡ {}[/dim]", agents_display),
                    "",
                    "",
                    "",
                )
        else:
            table.add_row(
                str(i),
                styled("{}{}", step.agent_name, loop_marker),
                step.agent_type,
                step.model or Text.from_markup("[dim]default[/dim]"),
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
        styled("[dim]Total steps:[/dim] {}", len(plan.steps)),
        styled("[dim]Loop targets:[/dim] {}", sum(1 for s in plan.steps if s.is_loop_target)),
    ]

    if parallel_group_count > 0:
        summary_parts.append(styled("[dim]Parallel groups:[/dim] {}", parallel_group_count))
        summary_parts.append(styled("[dim]Parallel agents:[/dim] {}", total_parallel_agents))

    output_console.print(join(" | ", summary_parts))


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

    class _MockProvider(AgentProvider, abstract=True):
        async def execute(
            self,
            agent: AgentDef,
            context: dict[str, Any],
            rendered_prompt: str,
            tools: list[str] | None = None,
            interrupt_signal: asyncio.Event | None = None,
            event_callback: Any = None,
            skill_directories: list[str] | None = None,
            custom_agents: list[dict[str, Any]] | None = None,
            extra_mcp_servers: dict[str, Any] | None = None,
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
    _verbose_console.print(
        styled("[bold yellow]Workflow state saved to:[/bold yellow] {}", checkpoint_path)
    )
    _verbose_console.print(
        styled(
            "[bold yellow]Resume with:[/bold yellow] conductor resume --from {}", checkpoint_path
        )
    )
    if engine.workflow_path is not None:
        _verbose_console.print(
            styled(
                "[dim]Or resume latest checkpoint:[/dim] conductor resume {}", engine.workflow_path
            )
        )
    _verbose_console.print(
        Text.from_markup(
            '[dim]Add guidance for the resumed run with:[/dim] --guidance "correction text"'
        )
    )
    _verbose_console.print()


async def resume_workflow_async(
    workflow_path: Path | None = None,
    checkpoint_path: Path | None = None,
    provider_override: str | None = None,
    skip_gates: bool = False,
    log_file: Path | None = None,
    no_interactive: bool = False,
    *,
    web: bool = False,
    web_port: int = 0,
    web_bg: bool = False,
    metadata: dict[str, str] | None = None,
    guidance: list[str] | None = None,
) -> dict[str, Any]:
    """Resume a workflow from a checkpoint.

    Loads a checkpoint file, reconstructs workflow state, and resumes
    execution from the failed agent.

    Args:
        workflow_path: Path to the workflow YAML file. Used to find
            the latest checkpoint if ``checkpoint_path`` is not provided.
        checkpoint_path: Explicit path to a checkpoint file. Takes
            precedence over ``workflow_path``.
        provider_override: Optional provider name to override workflow config
            for the resumed run.
        skip_gates: If True, auto-selects first option at human gates.
        log_file: Optional path to write full debug output to a file.
        no_interactive: If True, disables the keyboard interrupt listener.
        web: If True, start a real-time web dashboard for the resumed run.
            The dashboard is seeded with the original timeline by replaying
            the JSONL event log captured during the previous run (or by
            synthesising minimal events from the restored ``WorkflowContext``
            when the log file is unavailable), so previously completed
            agents remain visible alongside live events from the resumed
            run.
        web_port: Port for the web dashboard (0 = auto-select).
        web_bg: If True, auto-shutdown dashboard after workflow + client
            disconnect.
        metadata: Optional CLI metadata to merge on top of YAML-declared
            metadata for the resumed run.
        guidance: Optional mid-run guidance text(s) applied to the restored
            context before the resumed agent runs (issue #400). Applied via
            ``engine.add_user_guidance(text, source="cli")`` for each entry,
            in order, before the dashboard's ``workflow_started`` is
            prepended so the seeded history reflects the applied guidance.

    Returns:
        The workflow output as a dictionary.

    Raises:
        CheckpointError: If the checkpoint cannot be loaded or is invalid.
        ConductorError: If workflow execution fails.
    """
    from conductor.engine.checkpoint import CheckpointManager
    from conductor.engine.context import WorkflowContext
    from conductor.engine.limits import LimitEnforcer
    from conductor.events import WorkflowEventEmitter
    from conductor.exceptions import CheckpointError

    start_time = time.time()

    # Initialize file logging if requested
    if log_file is not None:
        try:
            init_file_logging(log_file)
        except OSError as e:
            _verbose_console.print(
                styled(
                    "[bold yellow]Warning:[/bold yellow] Cannot open log file {}: {}", log_file, e
                )
            )

    # Always create event emitter and JSONL log subscriber (parity with run)
    emitter = WorkflowEventEmitter()
    event_log_subscriber: Any = None
    dashboard: Any = None

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
                Text.from_markup(
                    "[bold yellow]⚠ Warning:[/bold yellow] "
                    "Workflow file has changed since checkpoint was created. "
                    "Resume may produce unexpected results."
                )
            )

        # Log checkpoint details
        verbose_log(f"Resuming from agent: {cp.current_agent}")
        verbose_log(
            f"Checkpoint created: {cp.created_at} (failed at: {cp.failure.get('agent', 'unknown')})"
        )

        # Load workflow config first — needed both to construct the dashboard
        # (workflow_root) and to seed the synthetic replay fallback.
        config = load_config(resolved_workflow_path)

        # Merge CLI metadata on top of YAML-declared metadata (parity with run)
        if metadata:
            config.workflow.metadata.update(metadata)

        # Apply provider override if specified (parity with run).
        # See ``run_workflow_async`` for why we re-validate via assignment.
        _apply_provider_override(config, provider_override)

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
            budget_usd=config.workflow.limits.budget_usd,
            budget_mode=config.workflow.limits.budget_mode,
        )

        # Construct the web dashboard early (subscribes to the emitter on
        # construction) but defer ``dashboard.start()`` until after we have
        # seeded ``_event_history`` with the current-config
        # ``workflow_started`` event plus the original run's replay. That
        # way the very first ``GET /api/state`` and the first WebSocket
        # client both see a fully populated, topology-correct history —
        # no race window where a client connects mid-replay.
        if web:
            from conductor.web.server import WebDashboard

            bg_mode = web_bg or os.environ.get("CONDUCTOR_WEB_BG") == "1"
            dashboard = WebDashboard(
                emitter,
                host="127.0.0.1",
                port=web_port,
                bg=bg_mode,
                workflow_root=resolved_workflow_path.resolve().parent,
            )

        # Build MCP servers config (same as run_workflow_async)
        mcp_servers = await _build_mcp_servers(config)

        # Same acquisition step as run: the checkpoint records no plugin
        # state, so a resumed run resolves its sources exactly as a fresh
        # one does. A floating ref may therefore have moved since the
        # original run — pin a SHA if that matters.
        plugin_marketplaces = await _prefetch_plugin_sources(config, resolved_workflow_path)

        # Create engine and restore state
        async with ProviderRegistry(config, mcp_servers=mcp_servers) as registry:
            verbose_log("Starting resumed workflow execution...")

            # Pass the checkpoint's merged session map so every provider that
            # supports session resume can pick out its own entries.
            if cp.copilot_session_ids:
                registry.set_resume_session_ids(cp.copilot_session_ids)
            # Pass the sessions' original working directories so the provider
            # can skip resuming a session whose cwd changed since creation.
            # Pre-cwd checkpoints carry an empty mapping (legacy behavior).
            if cp.copilot_session_cwds:
                registry.set_resume_session_cwds(cp.copilot_session_cwds)

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

            from conductor.engine.workflow import RunContext

            # Resume-mode log path: append to the original log when available
            # so a multi-resume session produces one continuous file and
            # ``run_id`` stays stable across resume generations.
            existing_log_path: Path | None = None
            if cp.event_log_path:
                candidate = Path(cp.event_log_path)
                if candidate.exists() and candidate.is_file():
                    existing_log_path = candidate

            # Build the JSONL subscriber BEFORE the engine so RunContext
            # carries the resolved ``run_id`` and ``log_file`` (used by
            # the engine to populate the ``workflow_started`` event payload).
            # When the checkpoint has the original log info, the subscriber
            # appends to it and reuses run_id; otherwise it generates fresh.
            from conductor.engine.event_log import EventLogSubscriber

            _event_log_dir = _resolve_event_log_dir(
                config.workflow.runtime.event_log_dir,
                resolved_workflow_path,
            )

            event_log_subscriber = EventLogSubscriber(
                config.workflow.name,
                existing_path=existing_log_path,
                existing_run_id=cp.run_id or None,
                event_log_dir=_event_log_dir,
            )
            emitter.subscribe(event_log_subscriber.on_event)

            # Write the Fleet Manager run record immediately, before any
            # further setup (dashboard seeding, engine construction) that
            # could take an arbitrary amount of time. `existing_log_path`
            # was just reopened in append mode above, so it must be marked
            # live as soon as possible -- otherwise a concurrent process's
            # retention sweep (E5) could see it as an unreferenced,
            # possibly-old event log and delete it out from under this
            # resume. The final call below (after the dashboard's actual
            # port is known) replaces this record rather than duplicating it.
            _write_run_record_for_current_process(
                event_log_subscriber=event_log_subscriber,
                dashboard=dashboard,
                workflow_path=resolved_workflow_path,
                web=web,
                web_bg=web_bg,
            )

            # Subscribe console output to the event emitter (parity with run)
            console_subscriber = ConsoleEventSubscriber()
            emitter.subscribe(console_subscriber.on_event)

            engine = WorkflowEngine(
                config,
                registry=registry,
                skip_gates=skip_gates,
                workflow_path=resolved_workflow_path,
                interrupt_event=interrupt_event,
                event_emitter=emitter,
                keyboard_listener=listener,
                web_dashboard=dashboard,
                instructions_preamble=cp.instructions_preamble,
                plugin_marketplaces=plugin_marketplaces,
                run_context=RunContext(
                    run_id=event_log_subscriber.run_id,
                    log_file=str(event_log_subscriber.path),
                    dashboard_port=(dashboard.port if dashboard is not None else None),
                    bg_mode=web_bg or os.environ.get("CONDUCTOR_WEB_BG") == "1",
                ),
            )
            engine.set_context(restored_context)
            engine.set_limits(restored_limits)

            # Apply any --guidance flags to the restored context before the
            # dashboard is seeded, so the prepended workflow_started (which
            # inserts at index 0) ends up before these guidance_applied
            # events in history order (issue #400).
            for guidance_text in guidance or []:
                engine.add_user_guidance(guidance_text, source="cli")

            # Seed the dashboard with the original timeline so previously
            # completed agents remain visible. Order matters:
            #   1. Prepend a fresh ``workflow_started`` built from the
            #      current config so historical events apply to the
            #      correct topology.
            #   2. Replay the original JSONL log (root-level lifecycle
            #      events are filtered to keep frontend ``wfDepth`` balanced).
            #   3. If no JSONL is available, fall back to synthesised
            #      events from the restored context.
            #   4. Suppress the engine's own ``workflow_started`` emit on
            #      resume — without this the dashboard would see two root
            #      starts and treat the live run as a child workflow.
            if dashboard is not None:
                workflow_started_data = await engine.build_workflow_started_data()
                dashboard.prepend_workflow_started(workflow_started_data)
                # Persist a resume-generation marker directly to the JSONL
                # log. The engine's own `workflow_started` emit is
                # suppressed below (so the live dashboard doesn't see a
                # duplicate root start) -- but that means a web-backed
                # resume's *persisted* log would otherwise never record
                # that a new execution attempt began here, leaving
                # History's stale-terminal-state reset (E14 review round 1)
                # unable to see it for a dashboard-backed resume (E14
                # review round 2). Written directly to the subscriber,
                # bypassing the emitter, so the dashboard is not handed a
                # second copy of the same event.
                from conductor.events import WorkflowEvent

                event_log_subscriber.on_event(
                    WorkflowEvent(
                        type="workflow_started", timestamp=time.time(), data=workflow_started_data
                    )
                )
                replayed = 0
                if existing_log_path is not None:
                    replayed = dashboard.replay_events_from_jsonl(existing_log_path)
                if replayed == 0:
                    try:
                        cp_ts: float | None = datetime.fromisoformat(cp.created_at).timestamp()
                    except (TypeError, ValueError):
                        cp_ts = None
                    replayed = dashboard.replay_synthetic_from_context(
                        restored_context, config, checkpoint_timestamp=cp_ts
                    )
                verbose_log(f"Seeded dashboard with {replayed} prior event(s)")
                engine.suppress_workflow_started_emit()

                try:
                    await dashboard.start()
                    from conductor.cli.app import is_verbose

                    if is_verbose():
                        _verbose_console.print(
                            styled("[bold cyan]Dashboard:[/bold cyan] {}", dashboard.url)
                        )
                except Exception as e:
                    # Drop the dashboard everywhere it's been wired up
                    # *before* deciding whether to raise or warn — the
                    # engine + DialogHandler captured it at construction
                    # time and would otherwise block waiting on a never-
                    # running WebSocket for human gates / dialogs. This
                    # also ensures the unconditional
                    # ``finally: dashboard.stop()`` further down can't await
                    # a serve task that never came up, which would re-raise
                    # the same underlying failure (or a bare ``SystemExit``
                    # from uvicorn's bind-failure path) and silently replace
                    # the informative RuntimeError below with that instead.
                    engine.clear_web_dashboard()
                    dashboard = None
                    if _is_scoped_bg_child(web_bg, web_port):
                        # Same reasoning as run_workflow_async: silently
                        # continuing without a dashboard in a ``--web-bg``
                        # child leaves the port never reachable, which the
                        # launcher's probe reads as either a false success
                        # or a reason to kill an otherwise-healthy resumed
                        # workflow (issue #410). Propagate so the child
                        # exits non-zero with the real cause.
                        raise RuntimeError(f"Dashboard failed to start: {e}") from e
                    _verbose_console.print(
                        styled(
                            "[bold yellow]Warning:[/bold yellow] Dashboard failed to "
                            "start: {}. Continuing without dashboard.",
                            e,
                        )
                    )

            # Re-write the Fleet Manager run record (E2) now that the
            # dashboard's actual resolved port (dashboard.start() — or its
            # failure — has just been handled above) is known. An earlier
            # call right after opening the event log subscriber already
            # marked this run live (see the retention-race comment there);
            # this one replaces that record with the final port value,
            # rather than creating a second one for the same run.
            _write_run_record_for_current_process(
                event_log_subscriber=event_log_subscriber,
                dashboard=dashboard,
                workflow_path=resolved_workflow_path,
                web=web,
                web_bg=web_bg,
            )

            # Opportunistic event-log retention sweep (E5 — D3), mirroring
            # run_workflow_async. Best-effort and settings-driven (enabled
            # by default, keep_last = 200): never raises, and cannot
            # meaningfully delay a resumed run.
            from conductor.fleet.retention import maybe_prune_event_logs

            maybe_prune_event_logs()

            # Share interrupt_event with dashboard so POST /api/stop can abort agents
            if dashboard is not None and interrupt_event is not None:
                dashboard.set_interrupt_event(interrupt_event)

            # Share the guidance sink with the dashboard so POST /api/guidance
            # can push mid-run text into the engine (issue #400). Unlike
            # set_interrupt_event this doesn't need interrupt_event -- a plain
            # --web run with no keyboard listener still accepts guidance.
            if dashboard is not None:
                dashboard.set_guidance_sink(engine.submit_guidance)

            terminate_exc: WorkflowTerminated | None = None
            try:
                if listener is not None:
                    await listener.start()
                    _verbose_console.print(
                        Text.from_markup("[dim]Press Esc to interrupt and provide guidance[/dim]")
                    )

                result = await _resume_with_stop_signal(engine, cp.current_agent, dashboard)
            except WorkflowTerminated as exc:
                # Mirror of the matching arm in `run_workflow_async`: defer
                # the raise so the dashboard stays alive for
                # explicit-terminate failures the same as it does for
                # successful runs. Resume hints stay suppressed because
                # explicit terminations are not resumable (see issue #219).
                terminate_exc = exc
                result = exc.output
            except BaseException:
                _print_resume_instructions(engine)
                raise
            finally:
                if listener is not None:
                    await listener.stop()

            # Log completion
            verbose_log_timing("Total resumed execution", time.time() - start_time)
            if terminate_exc is None:
                verbose_log("Workflow resumed successfully", style="green")
            else:
                verbose_log(
                    f"Resumed workflow terminated explicitly at '{terminate_exc.terminated_by}'",
                    style="yellow",
                )

            # Display usage summary if cost tracking is enabled
            if config.workflow.cost.show_summary:
                summary = engine.get_execution_summary()
                if "usage" in summary:
                    display_usage_summary(summary["usage"])

            # Cleanup checkpoint after the resumed run finishes. Both clean
            # completion and explicit termination are terminal outcomes — the
            # checkpoint we resumed from is no longer needed in either case.
            CheckpointManager.cleanup(cp.file_path)
            verbose_log(f"Checkpoint cleaned up: {cp.file_path}", style="dim")

            # Post-execution dashboard lifecycle (parity with run) — kept
            # alive for both clean exits and explicit-terminate failures.
            if dashboard is not None:
                is_bg = web_bg or os.environ.get("CONDUCTOR_WEB_BG") == "1"
                if is_bg:
                    await dashboard.wait_for_clients_disconnect()
                else:
                    from conductor.cli.app import is_verbose

                    if is_verbose():
                        banner = (
                            Text.from_markup("[bold yellow]Workflow terminated.[/bold yellow]")
                            if terminate_exc is not None
                            else Text.from_markup("[bold green]Workflow complete.[/bold green]")
                        )
                        _verbose_console.print(
                            styled(
                                "\n{} Dashboard still running at {} — press "
                                "[bold]Ctrl+C[/bold] to exit.",
                                banner,
                                dashboard.url,
                            )
                        )
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.Event().wait()

            if terminate_exc is not None:
                raise terminate_exc
            return result
    finally:
        # Clean up the Fleet Manager run record on every exit path (E2 —
        # mirrors run_workflow_async so a resumed run's record is removed
        # the same way a fresh run's is). Guarded (never raises) so a
        # failure here cannot prevent the dashboard/event-log/file-logging
        # cleanup below from running.
        _remove_run_record_for_current_process_safe()

        # Stop dashboard if it was started
        if dashboard is not None:
            await dashboard.stop()

        # Close JSONL event log and report path
        if event_log_subscriber is not None:
            event_log_subscriber.close()
            _verbose_console.print(
                styled("[dim]Event log written to: {}[/dim]", event_log_subscriber.path)
            )

        # Report log file path to stderr and close file logging
        if log_file is not None and _file_console is not None:
            _verbose_console.print(styled("[dim]Log written to: {}[/dim]", log_file))
        close_file_logging()


async def _prefetch_plugin_sources(config: Any, workflow_path: Path) -> dict[str, Any]:
    """Acquire ``runtime.plugin_sources`` before the engine starts.

    Up front rather than lazily, for three reasons: a cold cache means a
    ``git clone``, which would otherwise stall the first agent that
    happens to reference the plugin; a failure is a configuration problem
    and should be reported as one rather than as an agent error; and
    resolving every source together lets them be fetched concurrently
    instead of one round trip at a time.

    Runs in a thread because acquisition shells out to ``git``. Blocking
    the event loop here would be harmless (nothing else is running yet)
    but the same helper is reached from a sub-workflow spawn, where it
    would not be.

    Args:
        config: The workflow configuration.
        workflow_path: Path to the workflow file, anchoring relative
            local sources.

    Returns:
        Marketplaces keyed by the name a ``plugin@marketplace`` entry
        references. Empty when the workflow declares no sources.

    Raises:
        PluginError: If a source cannot be acquired or is unusable.
    """
    declared = config.workflow.runtime.plugin_sources
    if not declared:
        return {}

    from conductor.plugins.resolution import marketplaces_from, resolve_plugin_sources

    verbose_log(f"Resolving {len(declared)} plugin source(s): {', '.join(sorted(declared))}")
    try:
        resolved = await asyncio.to_thread(
            resolve_plugin_sources,
            declared,
            base_dir=workflow_path.resolve().parent,
            on_warning=lambda message: verbose_log(f"  Plugin sources: {message}", style="yellow"),
        )
    except OSError as exc:
        # The plugin cache is created here, so an unwritable home or a full
        # disk arrives as a bare errno naming only a random temp directory.
        from conductor.plugins.errors import PluginFetchError
        from conductor.plugins.fetch import get_plugin_cache_base

        raise PluginFetchError(
            f"Plugin sources could not be acquired into {get_plugin_cache_base()}: {exc}"
        ) from exc
    for name, entry in resolved.items():
        detail = entry.source.describe()
        if entry.sha:
            detail = f"{detail} @ {entry.sha[:12]}"
        if entry.fetched:
            detail = f"{detail} (fetched)"
        if entry.stale:
            # This line says the run used a plugin version nobody could
            # verify, so it gets the marker rather than reading as one more
            # startup progress line. Assembled in one ``styled`` call: an
            # f-string would render the Text as its plain form and drop the
            # yellow that makes it stand out.
            verbose_log(
                styled(
                    "  {}: {} [yellow]⚠ cached; ref not re-checked[/yellow] — {} plugin(s)",
                    name,
                    detail,
                    len(entry.marketplace.plugins),
                )
            )
        else:
            verbose_log(f"  {name}: {detail} — {len(entry.marketplace.plugins)} plugin(s)")
    return marketplaces_from(resolved)


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
        else:
            server_config = {
                "type": "stdio",
                "command": server.command,
                "args": server.args,
                "tools": server.tools,
            }
            if server.env:
                server_config["env"] = server.env
        if server.timeout:
            server_config["timeout"] = server.timeout
        # The same pipeline plugin-declared servers go through, so the two
        # sources cannot drift apart on env expansion or OAuth discovery.
        mcp_servers[name] = await resolve_mcp_server_config(name, server_config)
    verbose_log(f"MCP servers configured: {list(mcp_servers.keys())}")
    return mcp_servers
