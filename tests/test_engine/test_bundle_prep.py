"""Run-bundle preparation and manifest-driven backend lifecycle tests."""

from __future__ import annotations

import asyncio
import json
import logging
import textwrap
from pathlib import Path
from typing import Any

import pytest

import conductor.engine.bundle_prep as bundle_prep
import conductor.engine.execution_resolution as resolution_module
from conductor.bundle.errors import BundleUnfetchedError
from conductor.config.environment import (
    DockerProfileOptions,
    EnvironmentDocument,
    ProfileDefinition,
    ResolvedEnvironment,
)
from conductor.config.loader import load_config
from conductor.engine.execution_resolution import ExecutionResolverSession
from conductor.engine.run_manifest import BACKEND_CAPABILITY_PROVIDERS, compile_run_manifest
from conductor.engine.workflow import WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.exceptions import ConfigurationError
from conductor.execution import (
    BundleRef,
    CommandResult,
    CommandSpec,
    RunnerCapabilities,
    RunOutcome,
    RunSpec,
    WorkspaceLease,
)
from conductor.registry.cache import CACHE_LAYOUT_VERSION, _readiness_marker_payload, _sentinel_path


class StubBackend:
    instances = 0

    def __init__(self, name: str = "docker") -> None:
        type(self).instances += 1
        self.name = name
        self.prepare_calls: list[RunSpec] = []
        self.run_calls: list[CommandSpec] = []
        self.finalize_calls: list[tuple[WorkspaceLease, RunOutcome]] = []

    def capabilities(self) -> RunnerCapabilities:
        return RunnerCapabilities(
            batch=True, sessions=False, shared_workspace=True, snapshots=False
        )

    async def prepare_run(self, run: RunSpec) -> WorkspaceLease:
        self.prepare_calls.append(run)
        return WorkspaceLease(f"{self.name}-lease", self.name, "one")

    async def run_command(
        self,
        spec: CommandSpec,
        lease: WorkspaceLease | None,
        *,
        diagnostics: Any = None,
    ) -> CommandResult:
        del lease, diagnostics
        self.run_calls.append(spec)
        return CommandResult(
            outcome="completed",
            stdout=f"{spec.command}\n",
            stderr="",
            exit_code=0,
            resolved_command=spec.command,
            duration_seconds=0.0,
        )

    async def finalize_run(self, lease: WorkspaceLease, outcome: RunOutcome) -> None:
        self.finalize_calls.append((lease, outcome))


@pytest.fixture
def docker_environment() -> ResolvedEnvironment:
    document = EnvironmentDocument(
        default="local",
        profiles={
            "local": ProfileDefinition(backend="local"),
            "docker": ProfileDefinition(
                backend="docker",
                docker=DockerProfileOptions(image="python:3.12"),
            ),
        },
    )
    return ResolvedEnvironment(
        document=document,
        name="test",
        source="path",
        path=None,
        digest="sha256:test",
    )


@pytest.fixture(autouse=True)
def docker_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = StubBackend()
    monkeypatch.setitem(BACKEND_CAPABILITY_PROVIDERS, "docker", backend)
    StubBackend.instances = 0


def _write_script_workflow(
    path: Path,
    *,
    profile: str | None = None,
    workflow_default: str | None = None,
) -> Path:
    lines = ["workflow:", f"  name: {path.stem}", "  entry_point: script"]
    if workflow_default is not None:
        lines.extend(["  defaults:", "    execution:", f"      profile: {workflow_default}"])
    lines.extend(["agents:", "  - name: script", "    type: script", "    command: ok"])
    if profile is not None:
        lines.extend(["    execution:", f"      profile: {profile}"])
    lines.extend(
        [
            "    routes:",
            "      - to: $end",
            "output:",
            '  result: "{{ script.output.stdout }}"',
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _manifest(path: Path, environment: ResolvedEnvironment):
    return compile_run_manifest(load_config(path), workflow_path=path, environment=environment)


@pytest.mark.asyncio
async def test_docker_bundle_is_published_and_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    docker_environment: ResolvedEnvironment,
) -> None:
    # Requirement: Docker preflight publishes a sentinel-backed CAS entry and reuses it.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    workflow = _write_script_workflow(tmp_path / "workflow.yaml", profile="docker")
    manifest = _manifest(workflow, docker_environment)

    first = await bundle_prep.prepare_run_bundle(manifest, workflow, docker_environment)
    second = await bundle_prep.prepare_run_bundle(manifest, workflow, docker_environment)

    assert first is not None
    assert second == first
    assert first.digest.startswith("sha256:")
    assert (Path(first.store_path) / "bundle.json").is_file()


@pytest.mark.asyncio
async def test_registry_root_bundle_ref_uses_collected_logical_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    docker_environment: ResolvedEnvironment,
) -> None:
    # Requirement: materialization identifies the root from collector mapping, not main's presence.
    home = tmp_path / "home"
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))
    root = "registry/team/aaaaaaaaaaaa/nested"
    (home / "cache/registries/team/aaaaaaaaaaaa/nested").mkdir(parents=True)
    workflow = _write_script_workflow(
        home / "cache/registries/team/aaaaaaaaaaaa/nested/workflow.yaml", profile="docker"
    )
    bundle = await bundle_prep.materialize_run_bundle(workflow, docker_environment)

    assert bundle.root == root
    assert (Path(bundle.store_path) / "tree" / root / "workflow.yaml").is_file()


@pytest.mark.asyncio
async def test_environment_without_docker_profile_performs_zero_bundle_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: the no-Docker environment gate returns before every bundle I/O operation.
    environment = ResolvedEnvironment(
        document=EnvironmentDocument(
            default="local", profiles={"local": ProfileDefinition(backend="local")}
        ),
        name="local",
        source="builtin",
        path=None,
        digest="sha256:local",
    )
    workflow = _write_script_workflow(tmp_path / "workflow.yaml")
    manifest = _manifest(workflow, environment)
    monkeypatch.setattr(
        bundle_prep, "collect_bundle", lambda *args, **kwargs: pytest.fail("collect")
    )
    monkeypatch.setattr(
        bundle_prep, "publish_bundle", lambda *args, **kwargs: pytest.fail("publish")
    )
    monkeypatch.setattr(
        bundle_prep, "scan_subworkflow_docker_usage", lambda *args: pytest.fail("scan")
    )

    assert await bundle_prep.prepare_run_bundle(manifest, workflow, environment) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["local", "docker"])
async def test_programmatic_docker_environment_requires_file(
    tmp_path: Path,
    docker_environment: ResolvedEnvironment,
    profile: str,
) -> None:
    # Requirement: a Docker-capable environment cannot scan child workflows without a root file.
    workflow = _write_script_workflow(tmp_path / "workflow.yaml", profile=profile)
    manifest = compile_run_manifest(
        load_config(workflow), workflow_path=None, environment=docker_environment
    )

    with pytest.raises(ConfigurationError, match="programmatic engine construction"):
        await bundle_prep.prepare_run_bundle(manifest, None, docker_environment)


@pytest.mark.parametrize("profile_source", ["workflow-default", "environment-default"])
def test_scan_finds_child_docker_from_effective_profile_chain(
    tmp_path: Path,
    docker_environment: ResolvedEnvironment,
    profile_source: str,
) -> None:
    # Requirement: closure scanning uses the compiler's workflow-default/environment-default chain.
    if profile_source == "environment-default":
        document = docker_environment.document.model_copy(update={"default": "docker"})
        environment = ResolvedEnvironment(
            document=document,
            name=docker_environment.name,
            source=docker_environment.source,
            path=None,
            digest=docker_environment.digest,
        )
        child = _write_script_workflow(tmp_path / "child.yaml")
    else:
        environment = docker_environment
        child = _write_script_workflow(tmp_path / "child.yaml", workflow_default="docker")
    root = tmp_path / "root.yaml"
    root.write_text(
        textwrap.dedent(
            """\
            workflow:
              name: root
              entry_point: child
            agents:
              - name: child
                type: workflow
                workflow: child.yaml
                execution:
                  profile: local
                routes:
                  - to: $end
            """
        ),
        encoding="utf-8",
    )

    assert child.is_file()
    assert bundle_prep.scan_subworkflow_docker_usage(root, environment) is True


def test_scan_finds_cached_registry_child_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    docker_environment: ResolvedEnvironment,
) -> None:
    # Requirement: scan resolution matches the collector's offline registry-cache semantics.
    sha = "a" * 40
    home = tmp_path / "home"
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))
    cache = home / "cache" / "registries"
    child = cache / "_adhoc" / "acme" / "flows" / sha[:12] / "nested" / "child.yaml"
    child.parent.mkdir(parents=True)
    _write_script_workflow(child, profile="docker")
    metadata = cache / "_adhoc" / "acme" / "flows" / "_meta" / sha[:12]
    metadata.mkdir(parents=True)
    (metadata / "source.json").write_text(
        json.dumps(
            {
                "cache_layout_version": CACHE_LAYOUT_VERSION,
                "registry_type": "github",
                "source": "acme/flows",
                "full_sha": sha,
            }
        ),
        encoding="utf-8",
    )
    (metadata / "index.yaml").write_text(
        "workflows:\n  child:\n    description: ''\n    path: nested/child.yaml\n",
        encoding="utf-8",
    )
    sentinel = _sentinel_path("_adhoc/acme/flows", sha, "child")
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(_readiness_marker_payload(), encoding="utf-8")
    root = tmp_path / "root.yaml"
    root.write_text(
        textwrap.dedent(
            f"""\
            workflow:
              name: root
              entry_point: child
            agents:
              - name: child
                type: workflow
                workflow: child@acme/flows#{sha}
                execution:
                  profile: local
                routes:
                  - to: $end
            """
        ),
        encoding="utf-8",
    )

    assert bundle_prep.scan_subworkflow_docker_usage(root, docker_environment) is True


@pytest.mark.asyncio
async def test_unfetched_subworkflow_is_a_typed_hard_error(
    tmp_path: Path, docker_environment: ResolvedEnvironment
) -> None:
    # Requirement: an unknown offline child triggers collection and preserves BundleUnfetchedError.
    root = tmp_path / "root.yaml"
    root.write_text(
        textwrap.dedent(
            """\
            workflow:
              name: root
              entry_point: child
            agents:
              - name: child
                type: workflow
                workflow: missing@acme/flows#main
                execution:
                  profile: local
                routes:
                  - to: $end
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(BundleUnfetchedError, match="not available for bundling") as caught:
        await bundle_prep.prepare_run_bundle(
            _manifest(root, docker_environment), root, docker_environment
        )
    assert "plugin fetch" in str(caught.value)


@pytest.mark.asyncio
async def test_uncached_plugin_source_is_incomplete_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    docker_environment: ResolvedEnvironment,
) -> None:
    # Requirement: plugin-source cache misses use descriptor.incomplete and never publish.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        textwrap.dedent(
            """\
            workflow:
              name: incomplete
              entry_point: script
              runtime:
                plugin_sources:
                  acme:
                    source: acme/plugins
            agents:
              - name: script
                type: script
                command: ok
                execution:
                  profile: docker
                routes:
                  - to: $end
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        bundle_prep, "publish_bundle", lambda *args, **kwargs: pytest.fail("publish")
    )

    with pytest.raises(ConfigurationError, match="plugin-source:acme") as caught:
        await bundle_prep.prepare_run_bundle(
            _manifest(workflow, docker_environment), workflow, docker_environment
        )
    assert "bundle build" in str(caught.value)


@pytest.mark.asyncio
async def test_factory_constructs_registered_backend_and_rejects_unknown(
    monkeypatch: pytest.MonkeyPatch,
    docker_environment: ResolvedEnvironment,
) -> None:
    # Requirement: tests can register one factory while genuinely unknown names remain errors.
    backend = StubBackend("fictitious")
    monkeypatch.setitem(resolution_module.BACKEND_FACTORIES, "fictitious", lambda: backend)
    session = ExecutionResolverSession(docker_environment)

    await session.ensure_backends(["fictitious"])
    await session.ensure_backends(["fictitious"])
    assert session.backends["fictitious"] is backend
    with pytest.raises(ConfigurationError, match="unknown"):
        await session.ensure_backends(["unknown"])


@pytest.mark.asyncio
async def test_concurrent_late_discovery_serializes_bundle_and_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    docker_environment: ResolvedEnvironment,
) -> None:
    # Requirement: concurrent child discovery creates, bundles, and prepares one backend once.
    backend = StubBackend("fictitious")
    monkeypatch.setitem(resolution_module.BACKEND_FACTORIES, "fictitious", lambda: backend)
    calls = 0

    async def fake_materialize(path: Path | None, environment: ResolvedEnvironment):
        nonlocal calls
        del path, environment
        calls += 1
        await asyncio.sleep(0)
        return BundleRef("sha256:" + "a" * 64, str(tmp_path / "bundle"))

    monkeypatch.setattr(resolution_module, "materialize_run_bundle", fake_materialize)
    session = ExecutionResolverSession(docker_environment)
    await session.prepare_leases(RunSpec("run", "root"), workflow_path=tmp_path / "root.yaml")

    await asyncio.gather(
        session.ensure_backends(["fictitious"]),
        session.ensure_backends(["fictitious"]),
    )

    assert calls == 1
    assert backend.prepare_calls == [session._root_run_spec]
    assert session.lease_for_backend("fictitious") is not None


@pytest.mark.asyncio
async def test_programmatic_late_discovery_requires_root_workflow_file(
    monkeypatch: pytest.MonkeyPatch,
    docker_environment: ResolvedEnvironment,
) -> None:
    # Requirement: defensive late materialization reports the eager path's programmatic error.
    monkeypatch.setitem(
        resolution_module.BACKEND_FACTORIES,
        "fictitious",
        lambda: StubBackend("fictitious"),
    )
    session = ExecutionResolverSession(docker_environment)
    await session.prepare_leases(RunSpec("run", "root"))

    with pytest.raises(ConfigurationError, match="programmatic engine construction"):
        await session.ensure_backends(["fictitious"])


@pytest.mark.asyncio
async def test_root_preflight_bundles_child_docker_before_child_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    docker_environment: ResolvedEnvironment,
) -> None:
    # Requirement: closure scan prepares bundle metadata before either child constructor runs.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    docker_backend = StubBackend("docker")
    monkeypatch.setitem(resolution_module.BACKEND_FACTORIES, "docker", lambda: docker_backend)
    _write_script_workflow(tmp_path / "child.yaml", profile="docker")
    root = tmp_path / "root.yaml"
    root.write_text(
        textwrap.dedent(
            """\
            workflow:
              name: root
              entry_point: child
            agents:
              - name: child
                type: workflow
                workflow: child.yaml
                execution:
                  profile: local
                routes:
                  - to: $end
            output:
              result: "{{ child.output.result }}"
            """
        ),
        encoding="utf-8",
    )
    events: list[WorkflowEvent] = []
    emitter = WorkflowEventEmitter()
    emitter.subscribe(events.append)
    engine = WorkflowEngine(
        load_config(root),
        workflow_path=root,
        execution_environment=docker_environment,
        event_emitter=emitter,
    )

    result = await engine.run({})

    assert result == {"result": "ok\n"}
    started = next(event for event in events if event.type == "workflow_started")
    assert started.data["system"]["bundle"]["digest"].startswith("sha256:")
    assert len(docker_backend.prepare_calls) == 1
    assert docker_backend.prepare_calls[0].bundle is not None
    assert [call.command for call in docker_backend.run_calls] == ["ok"]


@pytest.mark.asyncio
async def test_for_each_child_constructor_discovers_docker_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    docker_environment: ResolvedEnvironment,
) -> None:
    # Requirement: the for-each child-constructor path ensures its own manifest's backend.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    docker_backend = StubBackend("docker")
    monkeypatch.setitem(resolution_module.BACKEND_FACTORIES, "docker", lambda: docker_backend)
    _write_script_workflow(tmp_path / "child.yaml", profile="docker")
    root = tmp_path / "root.yaml"
    root.write_text(
        textwrap.dedent(
            """\
            workflow:
              name: root
              entry_point: children
              input:
                items:
                  type: array
            agents: []
            for_each:
              - name: children
                type: for_each
                source: workflow.input.items
                as: item
                agent:
                  name: child
                  type: workflow
                  workflow: child.yaml
                  execution:
                    profile: local
                routes:
                  - to: $end
            output:
              result: "{{ children.outputs }}"
            """
        ),
        encoding="utf-8",
    )
    engine = WorkflowEngine(
        load_config(root),
        workflow_path=root,
        execution_environment=docker_environment,
    )

    await engine.run({"items": ["one", "two"]})

    assert len(docker_backend.prepare_calls) == 1
    assert [call.command for call in docker_backend.run_calls] == ["ok", "ok"]


@pytest.mark.asyncio
async def test_mixed_backends_warn_and_execute_with_stub(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    docker_environment: ResolvedEnvironment,
) -> None:
    # Requirement: a root mixed-script manifest logs both backend names at run time.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    docker_backend = StubBackend("docker")
    monkeypatch.setitem(resolution_module.BACKEND_FACTORIES, "docker", lambda: docker_backend)
    workflow = tmp_path / "mixed.yaml"
    workflow.write_text(
        textwrap.dedent(
            """\
            workflow:
              name: mixed
              entry_point: local_step
            agents:
              - name: local_step
                type: script
                command: local
                execution:
                  profile: local
                routes:
                  - to: docker_step
              - name: docker_step
                type: script
                command: docker
                execution:
                  profile: docker
                routes:
                  - to: $end
            output:
              result: "{{ docker_step.output.stdout }}"
            """
        ),
        encoding="utf-8",
    )
    engine = WorkflowEngine(
        load_config(workflow),
        workflow_path=workflow,
        execution_environment=docker_environment,
    )
    engine._execution_session.backends["local"] = StubBackend("local")

    with caplog.at_level(logging.WARNING, logger="conductor.engine.workflow"):
        result = await engine.run({})

    assert result == {"result": "docker\n"}
    warnings = [
        record.getMessage()
        for record in caplog.records
        if "mixed script execution backends" in record.getMessage()
    ]
    assert warnings == ["Workflow uses mixed script execution backends: docker, local"]
    assert [call.command for call in docker_backend.run_calls] == ["docker"]
