"""``discovery`` toolset: the two-tool fallback that replaces per-workflow
tools once the exposed count crosses ``--max-direct-tools`` (FR9, DD3, NFR3,
E12).

Unlike ``introspect``/``diagnose`` (E11), this toolset is never
operator-selectable via ``--toolsets`` -- see ``options.py``'s own note on
why ``"discovery"`` is deliberately absent from ``ALL_TOOLSETS``. Whether it
is active is decided once, by the catalogue builder, from the exposed
workflow count (``catalogue.py::build_catalogue``, E7/E12-T2) and acted on
by ``server.py`` when it wires ``tools/list``/``tools/call`` -- this module
supplies only the two tools' own logic, not the decision of when they
replace the catalogue's per-workflow tools.

``conductor_find_workflow`` is a thin, catalogue-only search: it never
touches the registry or filesystem again, since every field it reports
(name, description, input schema) was already resolved into the frozen
:class:`~conductor.mcp.serve.catalogue.Catalogue` at startup. ``conductor_run_workflow``
does not re-implement invocation -- it forwards straight to
:func:`conductor.mcp.serve.invoke.invoke_workflow_tool`, the exact function a
generated per-workflow tool's ``tools/call`` would reach in direct mode, so a
workflow behaves identically regardless of which mode exposed it.

**NFR3.** ``name`` on both tools is a **catalogue tool name**, exactly as
reported by ``conductor_find_workflow`` -- never a filesystem path, URL, or
registry source. ``invoke_workflow_tool`` already refuses any name that
isn't a key in ``catalogue.reverse`` (``UnknownToolError``), and a
path-shaped string is never such a key, so it is rejected the same way any
other unrecognized name is -- there is no separate "looks like a path"
check to bypass, matching ``introspect.py::resolve_catalogue_workflow_path``'s
and ``diagnose.py::conductor_validate_workflow``'s identical reasoning for
their own ``name`` parameter.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import jsonschema
from mcp.types import ResourceLink, TextContent

from conductor.fleet.launch import LaunchError
from conductor.mcp.serve.invoke import LaunchTracker, invoke_workflow_tool
from conductor.mcp.serve.toolgen import WAIT_SECONDS_PARAM

if TYPE_CHECKING:
    from conductor.mcp.serve.catalogue import Catalogue
    from conductor.mcp.serve.options import ServeOptions
    from conductor.registry.config import RegistriesConfig

# Matches `invoke.py::_ProgressSender`'s own shape -- redefined locally
# (rather than importing that private alias) since this module's only use
# of it is forwarding straight through to `invoke_workflow_tool`.
_ProgressSender = Callable[[str | int, float, float | None, str | None], Awaitable[None]]

# NFR6: hard bound on the number of workflows `conductor_find_workflow` ever
# returns in one call, regardless of how many entries match. A 1,000-workflow
# catalogue with an empty (match-everything) query would otherwise return
# every workflow's full description and input schema in a single result --
# unbounded and mirrored by the SDK as text. Matches how the rest of this
# toolset bounds a per-call result (`introspect.py`'s own `_MAX_INLINE_RESULT_BYTES`
# posture) -- a count bound here rather than a byte bound, since the shape
# that grows unboundedly is "how many workflows", not one payload's size.
_MAX_RESULTS = 25


def _validate_inputs_against_entry_schema(
    name: str, inputs: dict[str, Any], *, catalogue: Catalogue
) -> None:
    """Validate the flattened ``inputs`` against the *selected* workflow's own
    ``inputSchema`` (review round 1).

    The discovery pair's own fixed ``inputSchema`` (``server.py::_DISCOVERY_TOOLS``)
    can only assert that ``inputs`` is an object -- the SDK has no way to know
    which workflow's schema applies until ``name`` is read at call time, so it
    cannot type-check ``inputs`` the way it type-checks a direct-mode tool's
    flattened arguments (``jsonschema.validate(instance=arguments,
    schema=tool.inputSchema)``, ``mcp.server.lowlevel.server.Server.call_tool``).
    This performs that exact same check against the resolved entry's schema, so
    a wrongly-typed value (e.g. a string for a numeric input) is rejected here
    exactly as it would be at the protocol layer in direct mode, instead of
    reaching :func:`~conductor.fleet.launch.build_typed_launch_inputs`, which
    assumes SDK-equivalent validation already happened.

    A ``name`` this catalogue does not publish is left alone here --
    :func:`~conductor.mcp.serve.invoke.invoke_workflow_tool` already raises
    ``UnknownToolError`` for it.

    Raises:
        LaunchError: If ``inputs`` does not conform to the resolved entry's
            ``inputSchema``.
    """
    for entry in catalogue.entries:
        if entry.tool_name == name:
            try:
                jsonschema.validate(instance=inputs, schema=entry.tool.inputSchema)
            except jsonschema.ValidationError as exc:
                raise LaunchError(f"Invalid input for {name!r}: {exc.message}") from exc
            return


def conductor_find_workflow(query: str = "", *, catalogue: Catalogue) -> dict[str, Any]:
    """``conductor_find_workflow(query)`` (E12-T1, FR9).

    A case-insensitive substring search over the frozen catalogue's own
    tool name, workflow key, registry, and description -- every field
    already resolved and sanitized at startup, so this never touches the
    registry cache or filesystem again. An empty (or omitted) ``query``
    lists every published workflow, which is the intended way to browse
    the full catalogue once discovery mode has hidden the per-workflow
    tools from ``tools/list``.

    Args:
        query: Case-insensitive substring to match. Empty matches every
            entry.
        catalogue: The frozen catalogue built at startup.

    Returns:
        ``{"query", "count", "workflows", "truncated"}`` where each
        ``workflows`` entry is ``{"name", "description", "input_schema",
        "registry", "workflow"}`` -- ``name`` is the exact catalogue tool
        name :func:`conductor_run_workflow` accepts (NFR3). ``count`` is the
        total number of matches; ``workflows`` is capped at
        :data:`_MAX_RESULTS` (NFR6), with ``truncated`` set when ``count``
        exceeds that cap -- narrow the ``query`` to see the rest.
    """
    needle = query.strip().lower()
    matches: list[dict[str, Any]] = []
    count = 0
    for entry in catalogue.entries:
        haystack = " ".join(
            (entry.tool_name, entry.workflow, entry.registry, entry.tool.description or "")
        ).lower()
        if needle and needle not in haystack:
            continue
        count += 1
        if len(matches) < _MAX_RESULTS:
            matches.append(
                {
                    "name": entry.tool_name,
                    "description": entry.tool.description,
                    "input_schema": entry.tool.inputSchema,
                    "registry": entry.registry,
                    "workflow": entry.workflow,
                }
            )
    return {
        "query": query,
        "count": count,
        "workflows": matches,
        "truncated": count > len(matches),
    }


async def conductor_run_workflow(
    name: str,
    inputs: dict[str, Any] | None = None,
    _wait_seconds: float | None = None,
    *,
    catalogue: Catalogue,
    options: ServeOptions,
    tracker: LaunchTracker,
    registries_config: RegistriesConfig | None = None,
    progress_token: str | int | None = None,
    send_progress: _ProgressSender | None = None,
) -> tuple[list[TextContent | ResourceLink], dict[str, Any]]:
    """``conductor_run_workflow(name, inputs, _wait_seconds?)`` (E12-T1, FR9).

    Dispatches through :func:`conductor.mcp.serve.invoke.invoke_workflow_tool`
    -- the exact same invocation layer a generated per-workflow tool's
    ``tools/call`` reaches in direct mode -- so a workflow launched through
    discovery behaves identically to one launched through its own generated
    tool (always detached, bounded wait, DD11's never-skip-gates, the R3
    concurrency cap, and NFR6's output-size bound all apply unchanged).

    Args:
        name: The catalogue tool name, exactly as reported by
            :func:`conductor_find_workflow`. Never a filesystem path, URL,
            or registry source (NFR3) -- see the module docstring for why
            no separate shape check is needed.
        inputs: The workflow's own ``input:`` values. ``None`` is treated
            as ``{}``.
        _wait_seconds: The reserved bounded-wait parameter (FR5), forwarded
            unchanged. Omitted (``None``) defers to the workflow's declared
            ``mcp.mode``, exactly as it would for a generated tool.
        catalogue: The frozen catalogue built at startup.
        options: The frozen startup options.
        tracker: This server process's :class:`~conductor.mcp.serve.invoke.LaunchTracker` (R3).
        registries_config: The configured registries; defaults to
            ``registry.config.load_config()``.
        progress_token: The caller-supplied MCP progress token, if any.
        send_progress: Forwarded to ``invoke_workflow_tool`` unchanged.

    Returns:
        The same ``(content, structuredContent)`` shape
        :func:`~conductor.mcp.serve.invoke.invoke_workflow_tool` returns.

    Raises:
        UnknownToolError: If ``name`` is not a tool name this catalogue
            publishes -- including any path-, URL-, or registry-source-shaped
            string, none of which is ever a catalogue key (NFR3).
        ConcurrentRunLimitError: If ``--max-concurrent-runs`` is set and
            already reached (R3).
        LaunchError: If ``inputs`` does not conform to the resolved
            workflow's own ``inputSchema``, a required input is missing, or
            the underlying launch itself fails.
    """
    arguments: dict[str, Any] = dict(inputs) if inputs else {}
    # The fixed discovery schema (`server.py::_DISCOVERY_TOOLS`) only asserts
    # that `inputs` is an object -- it cannot know which workflow's schema
    # applies until `name` is read here. Validate against the *resolved*
    # entry's own schema so a wrongly-typed value is rejected the same way it
    # would be at the protocol layer in direct mode, before it ever reaches
    # `build_typed_launch_inputs`, which assumes that validation already
    # happened.
    _validate_inputs_against_entry_schema(name, arguments, catalogue=catalogue)
    if _wait_seconds is not None:
        arguments[WAIT_SECONDS_PARAM] = _wait_seconds

    return await invoke_workflow_tool(
        name,
        arguments,
        catalogue=catalogue,
        options=options,
        tracker=tracker,
        registries_config=registries_config,
        progress_token=progress_token,
        send_progress=send_progress,
    )
