"""In-container HTTP server for Conductor's `aca` provider (epic E4, issue #284).

Wraps a real :class:`conductor.providers.copilot.CopilotProvider` behind the
``POST /execute`` (streaming NDJSON) + ``GET /health`` contract consumed by
:class:`conductor.providers.aca.AcaRuntimeProvider`. See
:mod:`conductor.providers.aca_protocol` for the shared wire-protocol models
and ``docs/projects/aca/aca-provider.design.md`` for the full contract.

Entry point for the runner image::

    python -m conductor.aca_runner
"""

from __future__ import annotations

from conductor.aca_runner.server import create_app

__all__ = ["create_app"]
