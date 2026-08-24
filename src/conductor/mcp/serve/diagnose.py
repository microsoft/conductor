"""``diagnose`` toolset: ``conductor_doctor``, ``conductor_validate_workflow``,
and ``conductor_run_logs`` (FR8, DD12, NFR6, E11-T3, E11-T4). Off by default
-- see ``options.py::ALL_TOOLSETS`` and ``server.py``'s startup summary
(E11-T1).

**DD12's link-only rule is deliberately scoped.** It exists because
:func:`conductor_run_logs` is the one tool whose payload is *verbatim
third-party text this server did not generate* (a ``--web-bg`` child's
captured stdout/stderr, or the raw event log) -- Conductor has no
redaction layer to apply to it, so it is reduced to :class:`mcp.types.ResourceLink`
content blocks plus bounded metadata, never bytes. :func:`conductor_doctor`
and :func:`conductor_validate_workflow` are different in kind: both return a
*structured report the server generated itself* (``providers/diagnostics.py``
and ``cli/validate.py`` respectively), the same way :func:`conductor_run_events`
and :func:`conductor_node_detail` do in ``introspect.py``. DD12 does not apply
to either -- do not "fix" them into ``resource_link`` wrappers; there is no
file backing this content to link to.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp.types import ResourceLink
from pydantic import AnyUrl

from conductor.cli.validate import validate_workflow
from conductor.console import make_console
from conductor.fleet.records import is_valid_run_id
from conductor.mcp.serve.introspect import _event_log_path_for, resolve_catalogue_workflow_path
from conductor.mcp.serve.runs import RunLookup, resolve_run
from conductor.providers import diagnostics as diagnostics_module

if TYPE_CHECKING:
    from conductor.mcp.serve.catalogue import Catalogue
    from conductor.mcp.serve.options import ServeOptions
    from conductor.registry.config import RegistriesConfig

# ---------------------------------------------------------------------------
# E11-T3: conductor_doctor
# ---------------------------------------------------------------------------


async def conductor_doctor() -> dict[str, Any]:
    """``conductor_doctor()`` (E11-T3, FR8).

    The same structured report ``conductor doctor`` renders on the CLI,
    gathered directly from :func:`conductor.providers.diagnostics.gather`
    -- a report the server generated itself. See the module docstring for
    why DD12's link-only rule does not apply here.

    Returns:
        ``DoctorReport.to_dict()`` -- a JSON-safe dict with whichever of
        ``env``/``providers``/``registries`` the default section set
        includes.
    """
    report = await diagnostics_module.gather()
    return report.to_dict()


# ---------------------------------------------------------------------------
# E11-T3: conductor_validate_workflow
# ---------------------------------------------------------------------------


def _silent_console() -> Any:
    """A ``Rich`` console that discards its output, so
    ``validate_workflow``'s CLI-style diagnostics printing does not reach
    this server's stdout (DD9) or its stderr startup channel -- mirrors
    ``runs.py::_silent_console`` exactly, reimplemented here (rather than
    imported) for the same reason: neither module should depend on the
    other for a two-line helper."""
    import io

    return make_console(file=io.StringIO(), width=200)


def conductor_validate_workflow(
    name: str,
    *,
    catalogue: Catalogue,
    options: ServeOptions,
    registries_config: RegistriesConfig | None = None,
) -> dict[str, Any]:
    """``conductor_validate_workflow(name)`` (E11-T3, FR8).

    Runs the same validation ``conductor validate`` performs
    (:func:`conductor.cli.validate.validate_workflow`), against a silent
    console so its Rich-formatted diagnostics never reach this server's
    protocol stream.

    ``name`` is a **catalogue tool name**, never a path (NFR3) -- resolved
    via :func:`conductor.mcp.serve.introspect.resolve_catalogue_workflow_path`,
    the same lookup a real invocation of that tool would use. A
    path-shaped argument is never a key in the catalogue's reverse map, so
    it is refused the same way any other unrecognized name is.

    Args:
        name: The catalogue tool name to validate.
        catalogue: The frozen catalogue built at startup.
        options: The frozen startup options.
        registries_config: The configured registries; defaults to
            ``registry.config.load_config()``.

    Returns:
        ``{"name", "is_valid", "entry_point", "agents"}`` -- a structured
        report, not the CLI's rendered console output.

    Raises:
        UnknownToolError: If ``name`` is not a tool name this catalogue
            publishes.
    """
    path = resolve_catalogue_workflow_path(
        name, catalogue=catalogue, options=options, registries_config=registries_config
    )
    is_valid, config = validate_workflow(path, console=_silent_console())
    return {
        "name": name,
        "is_valid": is_valid,
        "entry_point": config.workflow.entry_point if config is not None else None,
        "agents": [agent.name for agent in config.agents] if config is not None else [],
    }


# ---------------------------------------------------------------------------
# E11-T4: conductor_run_logs
# ---------------------------------------------------------------------------

_BG_STDERR_SUFFIX = ".bg.stderr.log"
_BG_STDOUT_SUFFIX = ".bg.stdout.log"


def _glob_bg_log(directory: Path, run_id: str, suffix: str) -> str | None:
    """Locate a run's ``.bg.stderr.log`` / ``.bg.stdout.log`` companion by
    ``run_id``, mirroring ``fleet/retention.py::_companion_paths``'s own
    glob-by-``run_id`` matching -- the parent (bg log) and child (events
    log) processes generate their ``ts`` filename segment independently
    and can differ by a clock tick, so matching on the full filename
    prefix is not reliable. Used only for the ``live``/``event_log``
    sources, which (unlike a :class:`TerminalRunRecord`) carry no stored
    path for either file. Never raises: an unreadable directory, or a
    ``run_id`` that is not path-safe (rejected before it ever reaches the
    glob pattern -- a value like ``"*"`` must never be interpolated into
    one), yields ``None``, same as "no companion found". The **newest**
    match wins when a resumed run reuses its predecessor's ``run_id`` and
    both left a bg log behind -- the oldest one is a previous attempt's
    stale diagnostics, not this run's."""
    if not is_valid_run_id(run_id):
        return None
    try:
        matches = sorted(directory.glob(f"conductor-*-{run_id}{suffix}"))
    except OSError:
        return None
    return str(matches[-1]) if matches else None


def _bg_log_paths_for(lookup: RunLookup, run_id: str) -> tuple[str | None, str | None]:
    """Resolve ``(bg_stderr_log, bg_stdout_log)`` across all three
    resolvable sources.

    A :class:`~conductor.fleet.records.TerminalRunRecord` stores both
    paths verbatim (even after the files themselves have been pruned --
    see DD13's "a record whose log was reaped ... is not a contradiction
    of this decision"), so the ``terminal`` source is authoritative and
    never globs. A live :class:`~conductor.fleet.records.RunRecord` carries
    neither field (nine fields, no more), so the ``live`` and
    ``event_log`` sources locate them by :func:`_glob_bg_log` instead,
    which only finds a file that still exists on disk.

    A child that died before writing its own run record or event log --
    the primary failure this tool exists to diagnose -- leaves no
    ``event_log_path`` to derive a search directory from, even though the
    *parent*-created ``.bg.stderr.log`` / ``.bg.stdout.log`` companions
    (``cli/bg_runner.py``) still exist on disk. Falling back to the
    well-known ``$TMPDIR/conductor`` directory (the same one
    ``fleet/records.py::find_event_log_for_run`` reads) rather than giving
    up is what makes those captures reachable in that case.
    """
    if lookup.source == "terminal":
        assert lookup.terminal is not None
        return lookup.terminal.bg_stderr_log, lookup.terminal.bg_stdout_log

    event_log_path: Path | None = None
    if lookup.source == "live" and lookup.record is not None and lookup.record.event_log_path:
        event_log_path = Path(lookup.record.event_log_path)
    elif lookup.source == "event_log":
        event_log_path = lookup.event_log_path

    parent = (
        event_log_path.parent
        if event_log_path is not None
        else Path(tempfile.gettempdir()) / "conductor"
    )
    return (
        _glob_bg_log(parent, run_id, _BG_STDERR_SUFFIX),
        _glob_bg_log(parent, run_id, _BG_STDOUT_SUFFIX),
    )


def _file_info(path: str | Path | None) -> dict[str, Any]:
    """Bounded per-file metadata for one log artefact -- never its
    contents (NFR6). A ``None``/empty path, or one that no longer exists
    on disk (DD13's pruned-log case), reports ``exists: false`` with
    whatever path was known rather than raising."""
    if not path:
        return {"path": None, "exists": False, "size": None, "modified_at": None}
    resolved = Path(path)
    try:
        stat = resolved.stat()
    except OSError:
        return {"path": str(resolved), "exists": False, "size": None, "modified_at": None}
    modified_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
    return {"path": str(resolved), "exists": True, "size": stat.st_size, "modified_at": modified_at}


_LOG_ARTEFACT_LABELS: dict[str, tuple[str, str]] = {
    # key -> (display suffix, mime type)
    "events_log": ("events.jsonl", "application/x-ndjson"),
    "bg_stderr_log": ("bg.stderr.log", "text/plain"),
    "bg_stdout_log": ("bg.stdout.log", "text/plain"),
}

_RESOURCE_LINK_DESCRIPTION = (
    "A log file, not its contents -- conductor_run_logs never returns log bytes "
    "(DD12, NFR6). Read it with your own file-reading tool."
)


def _resource_link(info: dict[str, Any], *, name: str, mime_type: str) -> ResourceLink | None:
    """Build one ``ResourceLink`` content block for a file that exists.
    Returns ``None`` for a missing (or never-known) path -- there is
    nothing to link to."""
    if not info["exists"]:
        return None
    path = Path(info["path"])
    return ResourceLink(
        type="resource_link",
        uri=AnyUrl(path.resolve().as_uri()),
        name=name,
        mimeType=mime_type,
        size=info["size"],
        description=_RESOURCE_LINK_DESCRIPTION,
    )


def conductor_run_logs(run_id: str) -> tuple[list[ResourceLink], dict[str, Any]]:
    """``conductor_run_logs(run_id)`` (E11-T4, FR8, DD12, NFR6).

    Answers the one thing the structured event log cannot: a child that
    died before the engine emitted anything. Returns ``ResourceLink``
    content blocks for whichever of ``.events.jsonl`` /
    ``.bg.stderr.log`` / ``.bg.stdout.log`` still exist on disk, plus
    bounded per-file metadata (``size``, ``modified_at``, ``exists``) for
    all three regardless -- **never** file contents, regardless of size.
    A path that no longer exists (DD13's pruned-log case) reports
    ``exists: false`` with the same path rather than omitting the entry.

    The terminal record supplies ``error_type``/``error_message`` when
    ``run_id`` resolves to one -- Conductor's own structured field for the
    single most useful fact about a failure, rather than scraped log
    text.

    Args:
        run_id: The run identifier to look up.

    Returns:
        A ``(links, structuredContent)`` pair: zero or more ``ResourceLink``
        blocks (one per existing file), and a dict carrying ``status``,
        ``error_type``, ``error_message``, per-file metadata under
        ``"files"``, and a note pointing at the host's own file-reading
        tool.
    """
    # Reuse `runs.py`'s own status shaping rather than re-deriving it here
    # -- this module must not re-implement the three-source status
    # derivation any more than `introspect.py` does (see that module's
    # docstring for the same reasoning).
    from conductor.mcp.serve.runs import _status_payload

    lookup = resolve_run(run_id)
    status_payload = _status_payload(lookup)

    event_log_path = _event_log_path_for(lookup)
    bg_stderr_log, bg_stdout_log = _bg_log_paths_for(lookup, run_id)

    files = {
        "events_log": _file_info(event_log_path),
        "bg_stderr_log": _file_info(bg_stderr_log),
        "bg_stdout_log": _file_info(bg_stdout_log),
    }

    links: list[ResourceLink] = []
    for key, info in files.items():
        suffix, mime_type = _LOG_ARTEFACT_LABELS[key]
        link = _resource_link(info, name=f"{run_id}-{suffix}", mime_type=mime_type)
        if link is not None:
            links.append(link)

    structured: dict[str, Any] = {
        "run_id": run_id,
        "source": lookup.source,
        "status": status_payload.get("status"),
        "error_type": status_payload.get("error_type"),
        "error_message": status_payload.get("error_message"),
        "files": files,
        "note": (
            "These entries are file paths and metadata only -- conductor_run_logs never "
            "returns log contents (DD12, NFR6). Read them with your own file-reading tool."
        ),
    }
    if lookup.source == "not_found":
        structured["error"] = status_payload.get("error")

    return links, structured
