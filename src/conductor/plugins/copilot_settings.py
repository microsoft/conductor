"""Read git-backed marketplaces the Copilot CLI itself registered.

``~/.copilot/settings.json`` is the CLI's own record of
``extraKnownMarketplaces`` — directories a user pointed the Copilot CLI
at, outside anything Conductor knows about. On an ordinary developer
machine this is where most installed plugins actually come from: the
directory is registered here, and ``~/.copilot/installed-plugins/`` (the
location :mod:`conductor.plugins.registry` otherwise searches) is empty.
Reading it is what makes a ``plugin@marketplace`` reference that already
works for the Copilot CLI also work for a ``provider: copilot`` agent,
with no ``runtime.plugin_sources`` entry required.

This is deliberately scoped to the ``copilot`` flavor only — see its one
call site in :mod:`conductor.plugins.registry` — since the file is
Copilot's own registry and a Claude-flavored agent has no business
resolving against it.

Only the ``"directory"`` source kind is read. A marketplace registered
some other way (a git remote the CLI itself clones) names a place
Conductor cannot read without acquiring it exactly the way
``plugin_sources:`` does, so it is skipped rather than guessed at.

``enabledPlugins`` is deliberately not read. Plugins stay named-only
resolution, never discovery — the same reasoning
:mod:`conductor.plugins.registry`'s module docstring gives for why an
installed plugin is not ambiently enabled.

Kept as a leaf module (stdlib + :mod:`conductor.plugins.errors` only,
though it currently needs neither) so it can be imported freely from
:mod:`conductor.plugins.registry` without adding any new edge to the
package's layering.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Location of the Copilot CLI's own settings file, relative to $HOME.
COPILOT_SETTINGS_RELATIVE: Path = Path(".copilot") / "settings.json"


def read_copilot_marketplaces(home: Path) -> dict[str, Path]:
    """Parse directory-backed marketplaces out of the Copilot CLI's settings.

    Args:
        home: Home directory to read ``.copilot/settings.json`` under. A
            parameter rather than a lookup so tests never read the
            developer's real ``~``.

    Returns:
        Marketplace name to absolute, expanded, normalised directory path,
        for every ``extraKnownMarketplaces`` entry whose
        ``source.source == "directory"``. Empty when the file is absent,
        unreadable, unparseable, or declares no such entries.

        Never raises. A malformed personal settings file — one this
        process did not write and has no control over — must not break
        ``conductor run``; a problem here degrades to an empty table and
        a debug-level log line rather than surfacing to the user.
    """
    settings_path = home / COPILOT_SETTINGS_RELATIVE
    try:
        text = settings_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("Could not read %s: %s", settings_path, exc)
        return {}

    try:
        parsed = json.loads(text)
    except ValueError as exc:
        logger.debug("Could not parse %s: %s", settings_path, exc)
        return {}

    if not isinstance(parsed, dict):
        return {}
    marketplaces = parsed.get("extraKnownMarketplaces")
    if not isinstance(marketplaces, dict):
        return {}

    resolved: dict[str, Path] = {}
    for name, entry in marketplaces.items():
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        if not isinstance(source, dict):
            continue
        if source.get("source") != "directory":
            # A git-remote (or other) marketplace kind names a place
            # Conductor cannot read without acquiring it — that acquisition
            # is exactly what 'runtime.plugin_sources' exists for.
            continue
        path = source.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        resolved[name.strip()] = Path(os.path.normpath(os.path.expanduser(path.strip())))
    return resolved


__all__ = ["read_copilot_marketplaces"]
