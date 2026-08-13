"""Implementation of the ``conductor doctor`` command.

Renders the provider/environment diagnostics gathered by
:mod:`conductor.providers.diagnostics` as Rich tables (human-readable) or a
JSON document (``--json``, for CI). All data gathering lives in the
diagnostics module; this file is a thin, presentation-only layer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING

from rich.table import Table
from rich.text import Text

from conductor.console import MarkupFreeConsole, join, styled
from conductor.providers.capabilities import known_provider_names
from conductor.providers.diagnostics import (
    ALL_SECTIONS,
    DoctorReport,
    EnvDiagnostic,
    ModelDiagnostic,
    ProviderDiagnostic,
    RegistryDiagnostic,
    gather,
)

if TYPE_CHECKING:
    from rich.console import Console

    from conductor.providers.diagnostics import Section


_CHECK = Text.from_markup("[green]✓[/green]")
_CROSS = Text.from_markup("[red]✗[/red]")
_DASH = Text.from_markup("[dim]—[/dim]")
_OPTIONAL_MARK = "○"
"""Neutral glyph for an absent *optional* credential — deliberately not the
red ``✗`` used for a genuinely missing required credential (issue #319).

.. note::
   ``✓``, ``✗`` and ``○`` are not encodable in cp1252, so the default *table*
   output of ``conductor doctor`` still fails on such a console — see #401.
   This module's ``--json`` path is safe (``ensure_ascii=True``); the table path
   is deliberately out of scope here because every glyph consumer would need the
   console threaded through it. The em-dash is encodable in cp1252.
"""


def run_doctor(
    *,
    section: str | None,
    provider: str | None,
    check: bool,
    models: bool,
    as_json: bool,
    console: MarkupFreeConsole,
    err_console: MarkupFreeConsole,
) -> int:
    """Gather and render diagnostics, returning a process exit code.

    Args:
        section: Positional section filter (``env`` / ``providers`` /
            ``registries``), or ``None`` for all sections.
        provider: Scope the providers section to a single provider name.
        check: Instantiate providers and probe ``validate_connection()``.
        models: List available models (implies ``check``).
        as_json: Emit a JSON document instead of Rich tables.
        console: Console for primary output (stdout).
        err_console: Console for error messages (stderr).

    Returns:
        ``0`` on success; ``1`` when ``section``/``provider`` is invalid, or
        when ``check`` is set and the scoped provider (``--provider`` when
        given, else ``copilot``) fails to connect.
    """
    # --models implies --check.
    check = check or models

    if section is not None and section not in ALL_SECTIONS:
        err_console.print(
            styled(
                "[bold red]Error:[/bold red] Unknown section {!r}. Choose from: {}.",
                section,
                ", ".join(ALL_SECTIONS),
            )
        )
        return 1

    if provider is not None and provider not in known_provider_names():
        err_console.print(
            styled(
                "[bold red]Error:[/bold red] Unknown provider {!r}. Known providers: {}.",
                provider,
                ", ".join(known_provider_names()),
            )
        )
        return 1

    sections: tuple[Section, ...] = ALL_SECTIONS if section is None else (section,)  # type: ignore[assignment]

    report = _gather_report(sections=sections, provider=provider, check=check, models=models)

    if as_json:
        console.print_json(data=report.to_dict(), ensure_ascii=True)
        return _compute_exit_code(report.providers, check=check, provider=provider)

    if report.env is not None:
        _render_env(report.env, console)
    if report.providers is not None:
        _render_providers(report.providers, console, check=check, models=models)
        if models:
            _render_models(report.providers, console)
    if report.registries is not None:
        _render_registries(report.registries, console)

    return _compute_exit_code(report.providers, check=check, provider=provider)


def _compute_exit_code(
    providers: list[ProviderDiagnostic] | None,
    *,
    check: bool,
    provider: str | None,
) -> int:
    """Return ``1`` when a checked scoped provider failed to connect, else ``0``.

    Offline runs (``check`` is False) and runs that did not gather the
    providers section always return ``0`` — an unhealthy *optional* provider
    never fails the command. Only the scoped provider (``--provider`` when
    given, otherwise the ``copilot`` default) drives a non-zero exit.
    """
    if not check or not providers:
        return 0
    scoped = provider or "copilot"
    for diag in providers:
        if diag.name == scoped and diag.connection_ok is False:
            return 1
    return 0


@contextlib.contextmanager
def _suppressed_logging(active: bool) -> Iterator[None]:
    """Silence log records while probing providers, restoring the prior level.

    Constructing and validating providers can emit INFO/ERROR log records to
    stderr (e.g. the Claude provider logs "Connection validation failed" then
    returns ``False``). During ``doctor`` the rendered report is the single
    source of truth, so this noise is suppressed for the duration of the
    probes. A no-op when ``active`` is ``False`` (offline runs stay pristine).
    """
    if not active:
        yield
        return
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous)


def _gather_report(
    *,
    sections: tuple[Section, ...],
    provider: str | None,
    check: bool,
    models: bool,
) -> DoctorReport:
    """Run the async gather, suppressing provider log noise during checks."""
    with _suppressed_logging(check or models):
        return asyncio.run(
            gather(sections=sections, provider=provider, check=check, list_models=models)
        )


def _render_env(env: EnvDiagnostic, console: Console) -> None:
    """Render the environment section."""
    table = Table(title="Environment", show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim")
    table.add_column("Value")

    table.add_row("Conductor", f"v{env.conductor_version}")
    table.add_row("Python", env.python_version)
    table.add_row("Platform", env.platform)

    if not env.update_checked:
        update = Text.from_markup("[dim]check skipped (CONDUCTOR_NO_UPDATE_CHECK)[/dim]")
    elif env.update_available is None:
        update = Text.from_markup("[dim]unavailable (offline?)[/dim]")
    elif env.update_available:
        update = styled("[yellow]v{} available[/yellow]", env.latest_version)
    else:
        update = Text.from_markup("[green]up to date[/green]")
    table.add_row("Update", update)

    console.print(table)


def _render_providers(
    providers: list[ProviderDiagnostic],
    console: MarkupFreeConsole,
    *,
    check: bool,
    models: bool,
) -> None:
    """Render the providers section as a table (columns adapt to flags)."""
    table = Table(title="Providers", show_lines=True)
    table.add_column("Provider", style="cyan", no_wrap=True)
    table.add_column("Installed", justify="center")
    table.add_column("Tier")
    table.add_column("Credentials", no_wrap=True)
    if check:
        table.add_column("Connection")
    if models:
        table.add_column("Models")
    table.add_column("Notes")

    for diag in providers:
        row = [
            diag.name,
            _CHECK if diag.installed else _CROSS,
            _tier_cell(diag.tier),
            _credentials_cell(diag),
        ]
        if check:
            row.append(_connection_cell(diag))
        if models:
            row.append(_models_cell(diag))
        row.append(diag.note or _DASH)
        table.add_row(*row)

    console.print(table)


def _tier_cell(tier: str | None) -> Text:
    """Format the tier cell."""
    if tier is None:
        return _DASH
    if tier == "experimental":
        return Text.from_markup("[yellow]experimental[/yellow]")
    return Text(tier)


def _credentials_cell(diag: ProviderDiagnostic) -> Text:
    """Format credential env-var presence (presence only, never values).

    A present credential is a green ``✓``. An absent credential renders as a
    red ``✗`` only when it is a genuine requirement; for providers whose env
    vars are optional overrides (they authenticate via a CLI login on disk —
    ``diag.credentials_optional``), an absent credential renders as a neutral
    dim ``○`` instead, so an all-absent cell does not read as "broken". The
    accompanying auth-path note is surfaced in the Notes column (issue #319).
    """
    if not diag.credential_env_vars:
        return _DASH
    lines: list[Text] = []
    for cred in diag.credential_env_vars:
        if cred.present:
            lines.append(styled("{} {}", _CHECK, cred.name))
        elif diag.credentials_optional:
            lines.append(styled("[dim]{} {}[/dim]", _OPTIONAL_MARK, cred.name))
        else:
            lines.append(styled("[dim]{} {}[/dim]", _CROSS, cred.name))
    return join("\n", lines)


def _connection_cell(diag: ProviderDiagnostic) -> Text:
    """Format the connection-check result cell."""
    if not diag.checked or diag.connection_ok is None:
        return _DASH
    if diag.connection_ok:
        return styled("{} connected", _CHECK)
    if diag.connection_error:
        return styled("{} [dim]{}[/dim]", _CROSS, diag.connection_error)
    return styled("{} [dim]connection failed[/dim]", _CROSS)


def _models_cell(diag: ProviderDiagnostic) -> Text:
    """Format the models cell in the Providers summary table.

    Shows a count/status only — per-model reasoning-effort and
    context-window details are rendered in the separate Models detail table
    (see :func:`_render_models`) below the Providers table. ``n/a`` when
    models is ``None`` (not enumerated), ``(none)`` for an empty list.
    """
    if diag.models_error:
        return styled("{} [dim]{}[/dim]", _CROSS, diag.models_error)
    if diag.models is None:
        return Text.from_markup("[dim]n/a[/dim]")
    count = len(diag.models)
    if not count:
        return Text.from_markup("[dim](none)[/dim]")
    return styled("{} {} model{}", _CHECK, count, "s" if count != 1 else "")


def _format_tokens(value: int | None) -> Text:
    """Format a token-limit value with grouped digits, or ``—`` when unknown."""
    if value is None:
        return _DASH
    return Text(f"{value:,}")


def _efforts_cell(model: ModelDiagnostic) -> Text:
    """Format the supported-reasoning-efforts cell.

    ``n/a`` when unknown (``None``), ``none`` for a definitive empty list
    (e.g. a non-thinking Claude model), otherwise a comma-separated list.
    """
    if model.supported_reasoning_efforts is None:
        return Text.from_markup("[dim]n/a[/dim]")
    if not model.supported_reasoning_efforts:
        return Text.from_markup("[dim]none[/dim]")
    return Text(", ".join(model.supported_reasoning_efforts))


def _default_effort_cell(model: ModelDiagnostic) -> Text:
    """Format the default-reasoning-effort cell."""
    if model.default_reasoning_effort is None:
        return _DASH
    return Text(model.default_reasoning_effort)


_PRICING_SOURCE_CELLS: dict[str, Text] = {
    "provider": Text.from_markup("[green]provider[/green]"),
    "table": Text.from_markup("[dim]table[/dim]"),
    "none": Text.from_markup("[red]none[/red]"),
}
"""Pre-built cells for each :attr:`ModelDiagnostic.pricing_source` literal
(issue #386). Built as module-level constants rather than interpolated at
render time — no runtime value ever reaches the markup parser, keeping this
inside the repo's console rules (see AGENTS.md "Console Output")."""


def _rate_cell(value: float | None) -> Text:
    """Format a per-Mtok rate, or ``—`` when unknown.

    Deliberately never renders ``0.00`` for ``None`` — a zero would read as
    "free", which is exactly the silent-wrong-number class of bug issue
    #386 is about.
    """
    if value is None:
        return _DASH
    return Text(f"{value:,.2f}")


def _pricing_source_cell(model: ModelDiagnostic) -> Text:
    """Format the pricing-source cell (``provider`` / ``table`` / ``none`` / ``—``)."""
    if model.pricing_source is None:
        return _DASH
    return _PRICING_SOURCE_CELLS.get(model.pricing_source, Text(model.pricing_source))


def _render_models(providers: list[ProviderDiagnostic], console: Console) -> None:
    """Render a per-provider Models detail table (``--models`` only).

    One table per provider that returned at least one model, with columns
    for reasoning-effort support, prompt/output/context token limits, and
    per-Mtok pricing plus its source (issue #386). Providers with no models
    (``None``/empty/error) are already summarized in the Providers table and
    are skipped here — there is nothing to detail.
    """
    for diag in providers:
        if not diag.models:
            continue
        table = Table(title=f"Models — {diag.name}", show_lines=True)
        table.add_column("Model", style="cyan", no_wrap=True)
        table.add_column("Reasoning efforts")
        table.add_column("Default")
        table.add_column("Prompt", justify="right")
        table.add_column("Output", justify="right")
        table.add_column("Context", justify="right")
        table.add_column("Input $/Mtok", justify="right")
        table.add_column("Output $/Mtok", justify="right")
        table.add_column("Pricing")

        for model in diag.models:
            table.add_row(
                model.id,
                _efforts_cell(model),
                _default_effort_cell(model),
                _format_tokens(model.max_prompt_tokens),
                _format_tokens(model.max_output_tokens),
                _format_tokens(model.max_context_window_tokens),
                _rate_cell(model.input_per_mtok),
                _rate_cell(model.output_per_mtok),
                _pricing_source_cell(model),
            )

        console.print(table)


def _render_registries(registries: RegistryDiagnostic, console: Console) -> None:
    """Render the registries section."""
    if registries.error is not None:
        console.print(
            styled("{} [dim]failed to load registries: {}[/dim]", _CROSS, registries.error)
        )
        return
    if not registries.registries:
        console.print(Text.from_markup("[dim]No registries configured.[/dim]"))
        return

    table = Table(title="Registries", show_lines=False)
    table.add_column("Name", style="cyan")
    table.add_column("Type")
    table.add_column("Source")
    table.add_column("Default", justify="center")

    for reg in registries.registries:
        table.add_row(
            reg.name,
            reg.type,
            reg.source,
            _CHECK if reg.is_default else _DASH,
        )

    console.print(table)
