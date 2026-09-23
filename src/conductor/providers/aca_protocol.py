"""DEPRECATED re-export shim over :mod:`conductor.runner.protocol`.

This module is **deprecated**; it will be removed in a future major release.
Import from :mod:`conductor.runner.protocol` instead — the wire protocol is
backend-neutral and no longer ACA-specific, so the canonical module names the
models ``Runner*`` instead of ``Aca*``.

Legacy name mapping (this module → ``conductor.runner.protocol``):

- ``AcaAgentPayload`` → ``RunnerAgentPayload``
- ``AcaExecuteRequest`` → ``RunnerAgentRequest``
- ``AcaEventFrame`` → ``RunnerEventFrame``
- ``AcaResultData`` → ``RunnerAgentResult``
- ``AcaErrorData`` → ``AcaGatewayErrorData`` (the ACA error subclass defined
  in :mod:`conductor.providers.aca`; the neutral base is ``RunnerErrorData``)
- ``RUNNER_TOKEN_HEADER`` → unchanged

Deliberate compromise: importing this shim is heavier than importing the
canonical module, because ``AcaErrorData`` maps to ``AcaGatewayErrorData`` and
therefore pulls in ``conductor.providers.aca`` (httpx plus the optional-import
``azure-identity`` guard). That is acceptable for a deprecated path, and it is
irrelevant to the runner image, which imports only
:mod:`conductor.runner.protocol`.

Importing this module emits a :class:`DeprecationWarning`; nothing inside this
repository imports it after the protocol lift (issue #527), so the warning is
heard only by external importers.
"""

from __future__ import annotations

import warnings

from conductor.providers.aca import AcaGatewayErrorData
from conductor.runner.protocol import (
    RUNNER_TOKEN_HEADER,
    RunnerAgentPayload,
    RunnerAgentRequest,
    RunnerAgentResult,
    RunnerErrorData,
    RunnerEventFrame,
)

# Legacy aliases: plain rebinding so each legacy name is the identical
# canonical object (see the module docstring for the mapping).
AcaAgentPayload = RunnerAgentPayload
AcaExecuteRequest = RunnerAgentRequest
AcaEventFrame = RunnerEventFrame
AcaResultData = RunnerAgentResult
AcaErrorData = AcaGatewayErrorData

__all__ = [
    "RUNNER_TOKEN_HEADER",
    "AcaAgentPayload",
    "AcaExecuteRequest",
    "AcaEventFrame",
    "AcaResultData",
    "AcaErrorData",
    "RunnerErrorData",
]

warnings.warn(
    "conductor.providers.aca_protocol is deprecated; "
    "use conductor.runner.protocol instead. "
    "It will be removed in a future major release.",
    DeprecationWarning,
    stacklevel=2,
)
