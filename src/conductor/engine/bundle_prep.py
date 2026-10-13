"""Prepare content-addressed run bundles for Docker-backed script steps.

Bundle materialization is an execution preflight concern, not an execution
backend concern: this module may depend on ``conductor.bundle`` while the leaf
``conductor.execution`` package remains transport-neutral. A root run scans
the statically knowable sub-workflow closure so a Docker-only child has its
bundle ready before execution starts. ``ExecutionResolverSession`` repeats the
same materialization defensively when a backend is discovered only at child
construction time.

Every resume reuses or republishes the content-addressed bundle. Ephemeral
workspaces prepare fresh leases; retained workspaces attach their prior leases.
The CLI builds its seeded resume ``workflow_started`` event before
``WorkflowEngine.resume()``, so that seeded event cannot contain the bundle
metadata prepared here. The engine's normal resume emit is suppressed.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from conductor.bundle.collector import MAX_SUBWORKFLOW_DEPTH, _Collector, collect_bundle
from conductor.bundle.errors import BundleError
from conductor.bundle.store import publish_bundle
from conductor.config.environment import ResolvedEnvironment
from conductor.config.loader import load_config
from conductor.config.schema import ExecutableStepBase, WorkflowConfig, WorkflowStepDef
from conductor.engine.run_manifest import (
    ResolvedRunManifest,
    effective_profile_name,
    executable_step_identity,
    script_step_backends,
)
from conductor.exceptions import ConfigurationError
from conductor.execution import BundleRef
from conductor.filesystem import stat_or_none

logger = logging.getLogger(__name__)

_PROGRAMMATIC_BUNDLE_ERROR = (
    "docker-backed script steps require a workflow file on disk for bundle collection; "
    "programmatic engine construction cannot collect a run bundle"
)
_CACHE_REMEDY = "run conductor plugin fetch / conductor bundle build to prime the cache"


def _executable_steps(config: WorkflowConfig) -> list[tuple[str, ExecutableStepBase]]:
    steps: list[tuple[str, ExecutableStepBase]] = []
    for step in config.agents:
        if isinstance(step, ExecutableStepBase):
            steps.append((executable_step_identity(step.name), step))
    for group in config.for_each:
        if isinstance(group.agent, ExecutableStepBase):
            steps.append(
                (
                    executable_step_identity(group.agent.name, for_each_group=group.name),
                    group.agent,
                )
            )
    return steps


def _workflow_steps(config: WorkflowConfig) -> list[WorkflowStepDef]:
    steps = [step for step in config.agents if isinstance(step, WorkflowStepDef)]
    steps.extend(
        group.agent for group in config.for_each if isinstance(group.agent, WorkflowStepDef)
    )
    return steps


def scan_subworkflow_docker_usage(
    workflow_path: Path,
    environment: ResolvedEnvironment,
) -> bool | None:
    """Scan a workflow closure for Docker-backed executable steps offline.

    Returns ``True`` when Docker is found, ``False`` when the complete scanned
    closure is local-only, and ``None`` when an offline sub-workflow reference
    cannot be resolved. The unknown result deliberately triggers full bundle
    collection, whose typed ``BundleUnfetchedError`` gives the actionable hard
    failure instead of letting a potentially Docker-backed child go unnoticed.
    """
    root = Path(os.path.abspath(os.path.normpath(workflow_path.expanduser())))
    resolver = _Collector(root, environment, False, lambda message: logger.warning("%s", message))
    visited: set[tuple[int, int]] = set()

    def _scan(path: Path, depth: int) -> bool | None:
        info = stat_or_none(path)
        if info is None:
            return None
        identity = (info.st_dev, info.st_ino)
        if identity in visited:
            return False
        visited.add(identity)

        config = load_config(path)
        for key, step in _executable_steps(config):
            profile_name = effective_profile_name(
                step,
                key,
                config.workflow.defaults,
                environment,
            )
            profile = environment.document.profiles.get(profile_name)
            if profile is None:
                available = ", ".join(sorted(environment.document.profiles))
                raise ConfigurationError(
                    f"Step '{key}' names execution profile '{profile_name}', which is not "
                    f"defined in environment '{environment.name}' (defined profiles: "
                    f"{available}).",
                    suggestion="Define the profile in the environment document, or point the "
                    "step at one of the defined profiles.",
                )
            if profile.backend == "docker":
                return True

        if depth >= MAX_SUBWORKFLOW_DEPTH and _workflow_steps(config):
            return None
        for step in _workflow_steps(config):
            try:
                child_path, _resolved = resolver._resolve_subworkflow(step.workflow, path.parent)
            except BundleError:
                return None
            result = _scan(child_path, depth + 1)
            if result is not False:
                return result
        return False

    return _scan(root, 0)


async def materialize_run_bundle(
    workflow_path: Path | None,
    environment: ResolvedEnvironment,
) -> BundleRef:
    """Collect and publish one complete offline run bundle."""
    if workflow_path is None:
        raise ConfigurationError(_PROGRAMMATIC_BUNDLE_ERROR)

    def warning_sink(message: str) -> None:
        logger.warning("Bundle collection: %s", message)

    try:
        collected = await asyncio.to_thread(
            collect_bundle,
            workflow_path,
            environment=environment,
            allow_network=False,
            on_warning=warning_sink,
        )
    except BundleError as exc:
        exc.suggestion = _CACHE_REMEDY
        raise

    if collected.descriptor.incomplete:
        details = ", ".join(collected.descriptor.incomplete)
        raise ConfigurationError(
            f"Run bundle closure is incomplete: {details}",
            suggestion=_CACHE_REMEDY,
        )

    store_path = await asyncio.to_thread(
        publish_bundle,
        collected.manifest,
        collected.files,
        collected.links,
        on_warning=warning_sink,
    )
    return BundleRef(
        digest=collected.manifest.bundle_digest,
        store_path=str(store_path),
        root=collected.root,
    )


async def prepare_run_bundle(
    manifest: ResolvedRunManifest,
    workflow_path: Path | None,
    environment: ResolvedEnvironment,
) -> BundleRef | None:
    """Prepare a bundle when this run's statically known closure may use Docker.

    The first gate examines the already-resolved in-memory environment and is
    intentionally before every path probe or loader call. An environment with
    no Docker profile therefore preserves the local-only path with zero bundle
    I/O.
    """
    if not any(profile.backend == "docker" for profile in environment.document.profiles.values()):
        return None

    # Membership-only gate: docker presence in the answer is identical under
    # both script_step_backends modes (script-only and legacy all-backends).
    needs_bundle = "docker" in script_step_backends(manifest)
    if not needs_bundle:
        if workflow_path is None:
            raise ConfigurationError(_PROGRAMMATIC_BUNDLE_ERROR)
        needs_bundle = scan_subworkflow_docker_usage(workflow_path, environment) is not False
    if not needs_bundle:
        return None
    return await materialize_run_bundle(workflow_path, environment)
