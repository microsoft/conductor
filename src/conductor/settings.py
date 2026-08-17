"""Conductor machine-wide settings (``~/.conductor/config.toml``).

Fleet Manager D3 introduces a general settings file, alongside the existing
``registries.toml``, for cross-cutting Conductor configuration that is not
tied to any one workflow. The first (and currently only) consumer is
``[fleet.retention]`` — see ``docs/projects/fleet-manager/fleet-manager.design.md``
("Second-order cleanup") and ``conductor.fleet.retention``.

This module deliberately mirrors ``conductor.registry.config`` (D3's stated
precedent): the same ``$CONDUCTOR_HOME`` env-var honoring, the same stdlib
``tomllib`` reader, the same "missing file -> defaults, malformed file ->
raise" posture. It is **read-only in v1** — there is no writer and no
``conductor config set``; the file is hand-edited, and
``conductor fleet prune --keep-last N`` is the CLI-side override for
retention specifically.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, StrictBool, StrictInt
from pydantic import ValidationError as PydanticValidationError

from conductor.exceptions import ConductorError

CONFIG_FILENAME = "config.toml"


class FleetRetentionSettings(BaseModel):
    """``[fleet.retention]`` — event-log retention under ``$TMPDIR/conductor/``.

    See ``conductor.fleet.retention.prune_event_logs`` for the sweep this
    configures.
    """

    enabled: StrictBool = True
    """Whether the opportunistic startup sweep (run from ``cli/run.py``)
    is active. Defaults to ``True`` per the design's decision that
    retention is enabled by default. ``conductor fleet prune`` (the
    manual entry point) always runs regardless of this flag. ``StrictBool``
    rejects TOML values like ``enabled = 1`` that would otherwise coerce
    silently into a boolean."""

    keep_last: StrictInt = 200
    """Number of most-recent event logs to retain. Mirrors the
    ``keep_last`` vocabulary already used by
    ``CheckpointManager.rotate_periodic_checkpoints``, including its
    ``keep_last < 1`` guard (a value below 1 is treated as "prune
    nothing", not "delete everything") — see
    ``conductor.fleet.retention.prune_event_logs``. ``StrictInt`` rejects
    TOML values like ``keep_last = true`` (a bool is a subclass of
    ``int`` and would otherwise coerce silently)."""


class FleetSettings(BaseModel):
    """``[fleet]`` — settings for the Fleet Manager (``conductor fleet``)."""

    retention: FleetRetentionSettings = FleetRetentionSettings()


class ConductorSettings(BaseModel):
    """Top-level ``~/.conductor/config.toml`` settings."""

    fleet: FleetSettings = FleetSettings()


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def get_settings_path() -> Path:
    """Return the path to the Conductor settings file.

    Respects the ``CONDUCTOR_HOME`` environment variable, exactly as
    ``conductor.registry.config.get_config_path`` does. Falls back to
    ``~/.conductor``.
    """
    home = os.environ.get("CONDUCTOR_HOME")
    base = Path(home) if home else Path.home() / ".conductor"
    return base / CONFIG_FILENAME


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def load_settings() -> ConductorSettings:
    """Load Conductor settings from disk.

    Returns:
        Parsed ``ConductorSettings``. Defaults are returned when the file
        does not exist — a missing settings file is normal, not an error.

    Raises:
        ConductorError: If the file exists but contains malformed TOML or
            invalid values (e.g. a non-boolean ``enabled`` or a
            non-integer ``keep_last``). Callers on the opportunistic
            startup path (``cli/run.py``) must catch this broadly rather
            than let it propagate — see that call site's docstring for
            why a machine-wide settings file must never break
            ``conductor run``.
    """
    path = get_settings_path()
    if not path.exists():
        return ConductorSettings()

    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConductorError(
            f"Failed to parse {path}: {exc}",
            suggestion="Check the TOML syntax in your Conductor settings file.",
            file_path=str(path),
        ) from exc

    try:
        return ConductorSettings.model_validate(raw)
    except PydanticValidationError as exc:
        raise ConductorError(
            f"Invalid Conductor settings in {path}: {exc}",
            suggestion="Verify the structure of your Conductor settings file.",
            file_path=str(path),
        ) from exc
