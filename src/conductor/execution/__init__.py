"""Execution backend contract for Conductor.

This package defines the data-shaped contract types and the ``RunnerBackend``
protocol through which executors run commands against pluggable execution
backends. It is a pure leaf: it imports only the standard library (plus its
own modules) and nothing from the rest of Conductor, so any layer may depend
on it.
"""

from conductor.execution.backend import RunnerBackend
from conductor.execution.local import LocalRunnerBackend
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
    "LocalRunnerBackend",
    "ResolvedExecutionSpec",
    "RunOutcome",
    "RunSpec",
    "RunnerBackend",
    "RunnerCapabilities",
    "StartError",
    "StartErrorKind",
    "WorkspaceLease",
]
