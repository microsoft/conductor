"""Remote agent-runner support — the serialized wire contract for runtimes.

Layering note
-------------
``conductor.execution`` is the in-process Python backend seam (stdlib-only,
frozen dataclass contracts); ``conductor.runner.protocol`` is the serialized
wire contract between the Conductor CLI host and a remote agent runtime
(Pydantic models, NDJSON frames). Both sides of the wire install the one
``conductor`` package — ``docker/aca-runner/Dockerfile`` pins the runner image
by git SHA, so host and runner deploy independently and the models here must
stay forward/backward compatible.

This package deliberately re-exports nothing (precedent:
:mod:`conductor.plugins`). Import what you need directly:

.. code-block:: python

    from conductor.runner.protocol import RunnerAgentRequest, ...
"""

from __future__ import annotations

__all__: list[str] = []
