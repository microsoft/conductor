"""The single definition of the run-registry directory.

``~/.conductor/runs/`` holds both the PID files ``cli/pid.py`` writes for
background workflows and the per-run dashboard token files ``web/auth.py``
writes. This is a stdlib-only leaf module (no conductor imports) so it can
be imported from ``web/`` without dragging in ``conductor.cli`` — importing
``conductor.cli.pid`` directly from ``web/`` would import
``conductor.cli.__init__``, which does ``from conductor.cli.app import
app``, and ``app.py`` itself reaches ``conductor.web.server``, closing a
cycle.
"""

from __future__ import annotations

from pathlib import Path

_RUNS_DIR_NAME = "runs"


def runs_dir() -> Path:
    """Return the run-registry directory, creating it if needed.

    Returns:
        Path to ``~/.conductor/runs/``.
    """
    d = Path.home() / ".conductor" / _RUNS_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d
