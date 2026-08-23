"""Resolve and launch a workflow from the Fleet Manager (Fleet Manager E12).

Per the design's *Launch model: viewer, not supervisor*, the Fleet Manager
never supervises a launched run's process lifecycle -- it shells out to
``conductor run --web-bg`` (via :func:`conductor.cli.bg_runner.launch_background`,
called directly rather than as a subprocess) and forgets. Detached spawning
across platforms is already solved once in ``cli/bg_runner.py`` (issue
#116's forensic bg-log capture, Windows job-breakaway handling, and D2's
run-record poll gate) -- this module deliberately does not re-implement any
of it, since doing so would make runs die with the TUI rather than survive
it.

Three responsibilities:

* :func:`resolve_workflow` -- accept a file path or registry reference and
  resolve it to a local workflow file plus its declared inputs, reusing the
  same :func:`~conductor.registry.resolver.resolve_ref` /
  :func:`~conductor.registry.cache.resolve_and_fetch` pair ``conductor show``
  uses (``cli/app.py``).
* :func:`launch_workflow` -- validate required inputs, coerce raw
  (string-typed, as a TUI form widget would produce) values to each input's
  declared type, and call ``launch_background()`` directly. Any failure
  (a missing required field, a coercion failure, or ``launch_background()``
  itself failing -- including its own D2 run-record-poll timeout) raises
  :class:`LaunchError` with a plain-text message suitable for display in the
  TUI, never a raw traceback.
* :func:`launch_resume` -- resume a run from an on-disk checkpoint (issue
  #460, the History screen's Resume action) by calling
  :func:`conductor.cli.bg_runner.launch_background_resume` directly, for the
  exact same never-re-implement-spawning reason as ``launch_workflow``.

The Fleet Manager's launch directory (issue #477, ``FleetApp.launch_dir``) is
threaded into this module as an argument -- ``base_dir`` on
:func:`resolve_workflow`, ``cwd`` on :func:`launch_workflow`/:func:`launch_resume`
-- and never read off the process itself (no ``os.getcwd()`` here). That
keeps this module a pure function of its arguments; the one place the
directory is actually decided is ``fleet/tui/app.py``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from conductor.exceptions import ConductorError

if TYPE_CHECKING:
    from conductor.cli.bg_runner import BackgroundLaunch
    from conductor.config.schema import InputDef


class LaunchError(ConductorError):
    """Raised when resolving or launching a workflow from the Fleet Manager fails.

    Always constructed with a plain-text, already-human-readable message --
    the TUI displays ``str(exc)`` directly rather than a traceback.
    """


@dataclass(frozen=True, slots=True)
class ResolvedWorkflow:
    """A workflow resolved to a local file, ready to render a launch form for."""

    path: Path
    """Local filesystem path to the workflow YAML (fetched/cached already,
    for a registry reference)."""

    name: str
    """The workflow's declared ``workflow.name``."""

    description: str | None
    """The workflow's declared ``workflow.description``, if any."""

    inputs: dict[str, InputDef]
    """The workflow's declared ``workflow.input`` mapping (name -> InputDef),
    the same shape ``conductor show`` renders (``cli/app.py``)."""


def resolve_workflow(ref: str, *, base_dir: Path | None = None) -> ResolvedWorkflow:
    """Resolve a file path or registry reference to a launchable workflow.

    Mirrors ``conductor show``'s resolution exactly (``cli/app.py``): a
    ``resolve_ref`` file reference must exist on disk, while a registry
    reference is fetched (and cached) via ``resolve_and_fetch``.

    Args:
        ref: A local file path or registry reference
            (``name[@registry][#version]``).
        base_dir: The directory a *relative file* reference is resolved
            against (Fleet Manager E12/issue #477 -- the TUI's
            ``FleetApp.launch_dir``, not the process cwd).
            ``None`` preserves the prior behaviour of resolving relative to
            the process's current working directory. Ignored for an
            absolute reference or a registry reference, neither of which
            are relative to anything.

    Returns:
        A :class:`ResolvedWorkflow` with the local path and declared inputs.
        ``path`` is always absolute -- ``launch_background`` puts it
        straight into a detached child's argv, so a relative path would
        resolve against whatever cwd that child happens to inherit rather
        than the directory it was actually typed against.

    Raises:
        LaunchError: If the reference cannot be resolved, the file does not
            exist, or the workflow fails to parse.
    """
    from conductor.registry.cache import resolve_and_fetch
    from conductor.registry.errors import RegistryError
    from conductor.registry.resolver import resolve_ref

    try:
        resolved = resolve_ref(ref)
        if resolved.kind == "file":
            assert resolved.path is not None
            workflow_path = resolved.path
            if base_dir is not None and not workflow_path.is_absolute():
                workflow_path = base_dir / workflow_path
            if not workflow_path.exists():
                raise LaunchError(f"Workflow file not found: {ref} (looked for {workflow_path})")
        else:
            workflow_path = resolve_and_fetch(resolved)
    except RegistryError as e:
        raise LaunchError(str(e)) from e

    # `Path(os.path.abspath(...))`, not `.resolve()`: matches this repo's
    # existing "normpath, not resolve" convention
    # (`_resolve_agent_working_dir`, `skills/registry.py`) so a symlinked
    # project directory stays the alias the user typed rather than being
    # collapsed to its real path. A registry-fetched cache path is already
    # absolute, so this is a no-op for it.
    workflow_path = Path(os.path.abspath(workflow_path))

    try:
        from conductor.config.loader import load_config as load_workflow_config

        config = load_workflow_config(workflow_path)
    except Exception as e:  # noqa: BLE001 - surfaced as a LaunchError, not a traceback
        raise LaunchError(f"Failed to parse workflow: {e}") from e

    return ResolvedWorkflow(
        path=workflow_path,
        name=config.workflow.name,
        description=config.workflow.description,
        inputs=config.workflow.input,
    )


def coerce_input_value(raw: str, input_def: InputDef) -> Any:
    """Coerce a raw (string-typed) form value to ``input_def``'s declared type.

    Matches ``InputDef``'s five types (``config/schema.py``): ``string``,
    ``number``, ``boolean``, ``array``, ``object``. ``array``/``object`` are
    parsed as JSON (the same representation ``conductor run --input``'s own
    ``coerce_value`` falls back to for those shapes -- ``cli/run.py``).

    Args:
        raw: The raw string value, e.g. from a TUI ``Input`` widget.
        input_def: The input's declared type/required/default/description.

    Returns:
        The coerced value.

    Raises:
        LaunchError: If ``raw`` cannot be coerced to the declared type.
    """
    type_ = input_def.type

    if type_ == "string":
        return raw

    if type_ == "boolean":
        lowered = raw.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
        raise LaunchError(f"Cannot coerce {raw!r} to boolean")

    if type_ == "number":
        try:
            if "." in raw or "e" in raw.lower():
                return float(raw)
            return int(raw)
        except ValueError as e:
            raise LaunchError(f"Cannot coerce {raw!r} to number") from e

    if type_ in ("array", "object"):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as e:
            raise LaunchError(f"Cannot coerce {raw!r} to {type_} (expected JSON)") from e
        expected_python_type = list if type_ == "array" else dict
        if not isinstance(value, expected_python_type):
            raise LaunchError(f"Expected a JSON {type_} for {raw!r}, got {type(value).__name__}")
        return value

    raise LaunchError(f"Unknown input type: {type_!r}")


def build_launch_inputs(
    raw_values: dict[str, str], input_defs: dict[str, InputDef]
) -> dict[str, Any]:
    """Validate required inputs, fill defaults, and coerce to declared types.

    A blank (empty/whitespace-only) raw value is treated as "not provided":
    a required input with no default then rejects the launch outright,
    while an optional input either falls back to its (already
    type-validated, per ``InputDef.validate_default_type``) default or is
    omitted entirely, leaving the workflow's own handling of a missing
    input untouched.

    Args:
        raw_values: Form field name -> raw string value.
        input_defs: The workflow's declared inputs (name -> ``InputDef``).

    Returns:
        Coerced input values ready for ``launch_background(inputs=...)``.

    Raises:
        LaunchError: On a missing required input, or a value that cannot be
            coerced to its declared type.
    """
    result: dict[str, Any] = {}
    for name, input_def in input_defs.items():
        raw = raw_values.get(name, "")
        if not raw.strip():
            if input_def.default is not None:
                result[name] = input_def.default
                continue
            if input_def.required:
                raise LaunchError(f"Missing required input: {name}")
            continue
        result[name] = coerce_input_value(raw, input_def)
    return result


def build_typed_launch_inputs(
    values: dict[str, Any], input_defs: dict[str, InputDef]
) -> dict[str, Any]:
    """Validate required inputs and fill defaults for already JSON-typed values.

    The MCP invocation layer's counterpart to :func:`build_launch_inputs`
    (``mcp/serve/invoke.py``, E9-T2): an MCP host's tool-call arguments arrive
    already JSON-typed, per the tool's generated ``inputSchema`` (and are
    validated against it by the SDK before dispatch), so there is nothing to
    coerce *from a string* the way a TUI form field needs. This is the
    required-input/default-filling half of :func:`build_launch_inputs`,
    factored out so both callers share one definition of "what counts as a
    valid input set" for a workflow's declared ``input:`` rather than each
    growing its own notion of it.

    A value that is missing or explicitly ``None`` is treated as "not
    provided" (mirroring :func:`build_launch_inputs`'s blank-string
    treatment): a required input with no default rejects the launch
    outright, while an optional input either falls back to its (already
    type-validated) default or is omitted entirely, leaving the workflow's
    own handling of a missing input untouched.

    Args:
        values: Input name -> already-typed value (e.g. an MCP tool call's
            arguments, with the reserved ``_wait_seconds`` parameter already
            removed).
        input_defs: The workflow's declared inputs (name -> ``InputDef``).

    Returns:
        Coerced input values ready for ``launch_background(inputs=...)``.

    Raises:
        LaunchError: On a missing required input.
    """
    result: dict[str, Any] = {}
    for name, input_def in input_defs.items():
        value = values.get(name)
        if value is None:
            if input_def.default is not None:
                result[name] = input_def.default
                continue
            if input_def.required:
                raise LaunchError(f"Missing required input: {name}")
            continue
        result[name] = value
    return result


def launch_workflow(
    workflow_path: Path,
    raw_values: dict[str, str],
    input_defs: dict[str, InputDef],
    *,
    provider_override: str | None = None,
    skip_gates: bool = False,
    metadata: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> BackgroundLaunch:
    """Validate/coerce inputs and launch the workflow in the background.

    Calls :func:`conductor.cli.bg_runner.launch_background` directly --
    never spawns a ``conductor`` subprocess itself -- so the one already
    cross-platform-hardened detached-spawn implementation (and its D2
    run-record poll gate) is reused rather than duplicated. A successful
    return means the workflow is running, but not always already
    discoverable: check the returned ``BackgroundLaunch.run_record_written``
    -- ``False`` means the run-record poll's own bookkeeping failed (issue
    #435), so the run will not (yet) show up via ``read_run_records()``
    even though it is executing normally.

    Args:
        workflow_path: Local path to the workflow YAML (from
            :func:`resolve_workflow`).
        raw_values: Form field name -> raw string value.
        input_defs: The workflow's declared inputs (name -> ``InputDef``).
        provider_override: Optional provider name override.
        skip_gates: Whether to auto-select first option at human gates.
        metadata: Optional CLI metadata key=value pairs.
        cwd: Working directory for the detached child (issue #477 --
            the TUI's ``FleetApp.launch_dir``). ``None`` preserves the
            child's inherited cwd (today's behaviour).

    Returns:
        The ``BackgroundLaunch`` describing the launch. See above for the
        ``run_record_written`` caveat.

    Raises:
        LaunchError: On a required-field/coercion failure, or when
            ``launch_background()`` itself fails (child died early,
            dashboard never came up, or -- the D2 gate -- the child died or
            went unreachable before writing a matching run record). The
            original exception's message is preserved verbatim so the TUI
            can show it, but never the raw traceback.
    """
    inputs = build_launch_inputs(raw_values, input_defs)

    from conductor.cli.bg_runner import launch_background

    try:
        return launch_background(
            workflow_path=workflow_path,
            inputs=inputs,
            provider_override=provider_override,
            skip_gates=skip_gates,
            metadata=metadata,
            cwd=cwd,
        )
    except Exception as e:  # noqa: BLE001 - surfaced as a LaunchError, not a traceback
        raise LaunchError(str(e)) from e


def launch_resume(
    checkpoint_path: Path,
    *,
    provider_override: str | None = None,
    skip_gates: bool = False,
    metadata: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> BackgroundLaunch:
    """Resume a workflow from an on-disk checkpoint in the background (issue #460).

    Calls :func:`conductor.cli.bg_runner.launch_background_resume` directly
    -- never spawns a ``conductor`` subprocess itself -- for the same reason
    :func:`launch_workflow` calls ``launch_background()`` directly: the one
    already cross-platform-hardened detached-spawn implementation (and its
    D2 run-record poll gate) is reused rather than duplicated.

    Passes ``workflow_path=None`` and only ``checkpoint_path`` -- the
    History screen's :class:`~conductor.fleet.history.HistoryEntry` carries
    no workflow path of its own, so the checkpoint (which records its own
    ``workflow_path``) is the only route to one, and the child resolves it
    from the checkpoint itself.

    A successful return means the workflow is running, but not always
    already discoverable: check the returned ``BackgroundLaunch.
    run_record_written`` -- ``False`` means the run-record poll's own
    bookkeeping failed (issue #435), so the run will not (yet) show up via
    ``read_run_records()`` even though it is executing normally, exactly as
    for a fresh :func:`launch_workflow` launch.

    Args:
        checkpoint_path: Path to the checkpoint file to resume from.
        provider_override: Optional provider name override.
        skip_gates: Whether to auto-select first option at human gates.
        metadata: Optional CLI metadata key=value pairs.
        cwd: Working directory for the detached child (issue #477 --
            the TUI's ``FleetApp.launch_dir``). ``None`` preserves the
            child's inherited cwd (today's behaviour).

    Returns:
        The ``BackgroundLaunch`` describing the launch. See above for the
        ``run_record_written`` caveat.

    Raises:
        LaunchError: When ``launch_background_resume()`` itself fails (child
            died early, dashboard never came up, or the D2 gate). The
            original exception's message is preserved verbatim so the TUI
            can show it, but never the raw traceback.
    """
    from conductor.cli.bg_runner import launch_background_resume

    try:
        return launch_background_resume(
            workflow_path=None,
            checkpoint_path=checkpoint_path,
            provider_override=provider_override,
            skip_gates=skip_gates,
            metadata=metadata,
            cwd=cwd,
        )
    except Exception as e:  # noqa: BLE001 - surfaced as a LaunchError, not a traceback
        raise LaunchError(str(e)) from e
