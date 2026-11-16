"""Execution backend contract for Conductor.

This package defines the data-shaped contract types and the ``RunnerBackend``
protocol through which executors run commands against pluggable execution
backends. It is a pure leaf: it imports only the standard library (plus its
own modules) and nothing from the rest of Conductor, so any layer may depend
on it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from conductor.execution.backend import RunnerBackend
from conductor.execution.errors import ExecutionSpecError, WorkspaceAttachError
from conductor.execution.types import (
    AgentEventSink,
    AgentResult,
    AgentSpec,
    BundleRef,
    CommandOutcome,
    CommandResult,
    CommandSpec,
    ResolvedExecutionSpec,
    RunnerCapabilities,
    RunOutcome,
    RunSpec,
    StartError,
    StartErrorKind,
    WorkspaceIdentity,
    WorkspaceLease,
)

__all__ = [
    "AgentEventSink",
    "AgentResult",
    "AgentSpec",
    "BundleRef",
    "CommandOutcome",
    "CommandResult",
    "CommandSpec",
    "ExecutionSpecError",
    "LocalRunnerBackend",
    "ResolvedExecutionSpec",
    "RunOutcome",
    "RunSpec",
    "RunnerBackend",
    "RunnerCapabilities",
    "StartError",
    "StartErrorKind",
    "WorkspaceLease",
    "WorkspaceIdentity",
    "WorkspaceAttachError",
]

if TYPE_CHECKING:
    from conductor.execution.local import LocalRunnerBackend


def __getattr__(name: str) -> type[LocalRunnerBackend]:
    """Initialize the workspace backend only when its public export is requested."""
    if name != "LocalRunnerBackend":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from conductor.execution.local import LocalRunnerBackend

    globals()[name] = LocalRunnerBackend
    return LocalRunnerBackend


def __dir__() -> list[str]:
    """Keep public exports discoverable without initializing them."""
    return sorted(globals().keys() | set(__all__))
