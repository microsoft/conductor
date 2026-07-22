"""Entry point for the in-container `conductor-agent-runner` server (epic E4).

Usage (inside the runner image)::

    python -m conductor.aca_runner
"""

from __future__ import annotations

import os

import uvicorn

from conductor.aca_runner.server import create_app


def main() -> None:
    """Run the runner's FastAPI app under uvicorn.

    Binds to all interfaces by default (the process runs inside an Azure
    Container Apps dynamic-sessions custom container, not on a developer's
    machine). The port must match the custom container pool's configured
    target port (``<TARGET_PORT>`` in the design's *API Contracts*) —
    override both via `ACA_RUNNER_HOST` / `ACA_RUNNER_PORT` for local testing.
    """
    host = os.environ.get("ACA_RUNNER_HOST", "0.0.0.0")  # noqa: S104 - in-container by design
    port = int(os.environ.get("ACA_RUNNER_PORT", "8080"))
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
