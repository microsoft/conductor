"""Contract tests for the ``conductor.execution`` package.

Each test pins one requirement of the runner-backend contract: immutability,
field defaults, protocol shape, leaf purity, literal vocabularies, and the
exact ``StartError`` fill-in/reconstruction rule the executor relies on for
``__cause__`` parity.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal, get_args

import pytest

from conductor.execution import (
    BundleRef,
    CommandOutcome,
    CommandResult,
    CommandSpec,
    ResolvedExecutionSpec,
    RunnerBackend,
    RunnerCapabilities,
    RunOutcome,
    RunSpec,
    StartError,
    StartErrorKind,
    WorkspaceIdentity,
    WorkspaceLease,
)
from conductor.execution.docker import DockerRunnerBackend
from conductor.execution.errors import ExecutionSpecError, WorkspaceAttachError
from conductor.execution.local import LocalRunnerBackend


def _fill_start_error(e: OSError) -> StartError:
    """Fill ``StartError`` from an exception exactly as the plan specifies."""
    return StartError(
        kind="file_not_found" if isinstance(e, FileNotFoundError) else "os_error",
        message=e.strerror if getattr(e, "errno", None) is not None else str(e),
        errno=e.errno,
        filename=e.filename,
    )


def _reconstruct_cause(start_error: StartError) -> OSError:
    """Reconstruct the original exception exactly as the plan specifies."""
    cause_type = FileNotFoundError if start_error.kind == "file_not_found" else OSError
    if start_error.errno is not None:
        return cause_type(start_error.errno, start_error.message, start_error.filename)
    return cause_type(start_error.message)


class TestFrozenContract:
    """Requirement: every contract type is a frozen (immutable) dataclass."""

    @pytest.mark.parametrize(
        "instance",
        [
            CommandSpec(command="echo", args=("hello",)),
            CommandResult(outcome="completed"),
            StartError(kind="file_not_found", message="missing"),
            WorkspaceLease(lease_id="r1", backend="local", incarnation="i1"),
            WorkspaceIdentity(backend="local", lease_id="r1", incarnation="i1"),
            RunSpec(run_id="r1"),
            RunnerCapabilities(batch=True, sessions=False, shared_workspace=True, snapshots=False),
        ],
    )
    def test_assignment_raises_frozen_instance_error(self, instance: Any) -> None:
        # Requirement: contract immutability — no caller may mutate a shared
        # contract value after the backend handed it out. Frozen dataclasses
        # reject any attribute assignment, so "outcome" works for every type.
        with pytest.raises(dataclasses.FrozenInstanceError):
            instance.outcome = "timed_out"


class TestDefaults:
    """Requirement: field defaults match the contract spec exactly."""

    def test_command_spec_defaults(self) -> None:
        # Requirement: a bare rendered command carries no args, inherits the
        # working dir and control environment, and sends no stdin/timeout.
        spec = CommandSpec(command="echo")
        assert spec.args == ()
        assert spec.working_dir is None
        assert spec.env == {}
        assert spec.inherit_control_environment is True
        assert spec.stdin is None
        assert spec.timeout is None

    def test_command_result_defaults(self) -> None:
        # Requirement: an outcome-only result implies empty output, no exit
        # code, no resolved command, no start error, zero duration.
        result = CommandResult(outcome="completed")
        assert result.stdout == ""
        assert result.stderr == ""
        assert result.exit_code is None
        assert result.resolved_command == ""
        assert result.start_error is None
        assert result.duration_seconds == 0.0

    def test_start_error_defaults(self) -> None:
        # Requirement: errno/filename are optional metadata of a start error.
        error = StartError(kind="os_error", message="boom")
        assert error.errno is None
        assert error.filename is None

    def test_workspace_lease_and_run_spec_defaults(self) -> None:
        # Requirement: lease location and workflow name are optional hints.
        assert WorkspaceLease(lease_id="r1", backend="local", incarnation="i1").location is None
        assert RunSpec(run_id="r1").workflow_name is None


class TestProtocolShape:
    """Requirement: ``RunnerBackend`` is a typing.Protocol, not a base class."""

    def test_runner_backend_is_a_protocol(self) -> None:
        # Requirement: structural typing — backends need not inherit anything.
        assert getattr(RunnerBackend, "_is_protocol", False) is True

    def test_protocol_declares_exactly_four_methods(self) -> None:
        # Requirement: the seam stays minimal — no run_agent/open_mcp/cancel,
        # no close(), no context manager "for symmetry".
        method_names = {
            name
            for name, member in vars(RunnerBackend).items()
            if callable(member) and not name.startswith("_")
        }
        assert method_names == {
            "capabilities",
            "prepare_run",
            "attach_run",
            "run_command",
            "finalize_run",
        }


class TestLeafPurity:
    """Requirement: ``conductor.execution`` imports nothing from Conductor."""

    def test_import_does_not_pull_conductor_layers(self) -> None:
        # Requirement: leaf purity — importing the contract package must not
        # transitively import conductor.cli/engine/executor. Subprocess
        # isolation is load-bearing: an in-process sys.modules check would be
        # polluted by the test session's own imports.
        repo_src = Path(__file__).resolve().parents[2] / "src"
        env = {**os.environ, "PYTHONPATH": str(repo_src)}
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys\n"
                    "import conductor.execution  # noqa: F401\n"
                    "forbidden = ('conductor.cli', 'conductor.engine', 'conductor.executor')\n"
                    "leaked = [m for m in forbidden if m in sys.modules]\n"
                    "assert not leaked, f'conductor.execution pulled in: {leaked}'\n"
                    "print('leaf-pure')\n"
                ),
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        assert "leaf-pure" in proc.stdout

    def test_package_modules_import_only_stdlib(self) -> None:
        # Requirement: leaf purity, negative control at the AST level — this
        # fails if any conductor import (executor/cli/engine/...) sneaks into
        # the package, even one a subprocess smoke test might not exercise.
        import ast

        package_dir = Path(__file__).resolve().parents[2] / "src" / "conductor" / "execution"
        offenders: list[str] = []
        for source_path in sorted(package_dir.glob("*.py")):
            tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
            for node in ast.walk(tree):
                imported: str | None = None
                if isinstance(node, ast.Import):
                    imported = node.names[0].name
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported = node.module
                if imported is None or not imported.startswith("conductor"):
                    continue
                is_self = imported == "conductor.execution" or imported.startswith(
                    "conductor.execution."
                )
                if not is_self:
                    offenders.append(f"{source_path.name}:{node.lineno}:{imported}")
        assert offenders == []

    def test_types_module_imports_only_stdlib(self) -> None:
        # Requirement: serialized contract dataclasses remain independent of Conductor packages.
        import sys

        package_dir = Path(__file__).resolve().parents[2] / "src" / "conductor" / "execution"
        stdlib_modules = set(sys.stdlib_module_names)
        stdlib_modules.update(sys.builtin_module_names)
        source_path = package_dir / "types.py"
        import ast

        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        imports = [
            node.names[0].name.split(".")[0]
            if isinstance(node, ast.Import)
            else (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            or isinstance(node, ast.ImportFrom)
            and node.module is not None
        ]
        assert all(name in stdlib_modules for name in imports), imports

    def test_workspace_identity_is_frozen_and_serializable(self) -> None:
        # Requirement: retained identity is an immutable data-only checkpoint value.
        identity = WorkspaceIdentity("docker", "lease", "incarnation", "volume")
        assert dataclasses.asdict(identity) == {
            "backend": "docker",
            "lease_id": "lease",
            "incarnation": "incarnation",
            "location": "volume",
        }
        with pytest.raises(dataclasses.FrozenInstanceError):
            identity.backend = "local"

    def test_lifecycle_type_defaults_preserve_legacy_construction(self) -> None:
        # Requirement: additive fields preserve old constructors and ephemeral behavior.
        assert CommandSpec("echo").attempt_id is None
        assert RunSpec("x").workspace_persistence is None
        assert RunnerCapabilities(True, False, True, False).retained_workspace is False
        assert RunnerCapabilities(
            batch=True, sessions=False, shared_workspace=True, snapshots=False
        ) == RunnerCapabilities(True, False, True, False, False)

    @pytest.mark.asyncio
    async def test_local_rejects_retained_workspaces(self) -> None:
        # Requirement: local execution must not claim attach or retention support.
        backend = LocalRunnerBackend()
        identity = WorkspaceIdentity("local", "x", "i")
        with pytest.raises(
            ExecutionSpecError, match="^local backend does not support retained workspaces$"
        ):
            await backend.attach_run(RunSpec("x"), identity)
        with pytest.raises(
            ExecutionSpecError, match="^local backend does not support retained workspaces$"
        ):
            await backend.prepare_run(RunSpec("x", workspace_persistence="durable"))

    @pytest.mark.asyncio
    async def test_docker_retained_workspace_verifies_and_preserves_volume(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: attach verifies without allocation; retained preparation and finalization
        # create an owned volume eagerly and leave it in place without staging.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        helper = Path(__file__).with_name("docker_fake_stateful.py")
        wrapper = bin_dir / ("docker.cmd" if sys.platform == "win32" else "docker")
        if sys.platform == "win32":
            wrapper.write_text(f'@"{sys.executable}" "{helper}" %*\n', encoding="utf-8")
        else:
            wrapper.write_text(
                f"#!{sys.executable}\nexec(open({str(helper)!r}).read())\n", encoding="utf-8"
            )
            wrapper.chmod(wrapper.stat().st_mode | 0o100)
        log = tmp_path / "docker.jsonl"
        store = tmp_path / "daemon"
        monkeypatch.setenv("PATH", str(bin_dir))
        monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
        monkeypatch.setenv("FAKE_DOCKER_STORE", str(store))
        backend = DockerRunnerBackend(wrapper.name)
        with pytest.raises(WorkspaceAttachError, match="missing"):
            await backend.attach_run(
                RunSpec("missing"), WorkspaceIdentity("docker", "missing", "incarnation")
            )
        lease = await backend.prepare_run(RunSpec("retained", workspace_persistence="durable"))
        volume = store / "volumes/conductor-ws-retained"
        labels = json.loads((volume / "labels.json").read_text(encoding="utf-8"))
        assert labels["io.conductor.retention"] == "durable"
        assert labels["io.conductor.incarnation"] == lease.incarnation
        await backend.finalize_run(lease, "succeeded", retain=True)
        assert volume.exists()
        commands = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        assert not any(argv[0] == "cp" or argv[:2] == ["volume", "rm"] for argv in commands)
        assert sum(argv[:2] == ["volume", "create"] for argv in commands) == 1


class TestLiteralVocabularies:
    """Requirement: outcome/kind vocabularies are Literals in house style."""

    def test_command_outcome_values(self) -> None:
        # Requirement: the four command outcomes, and no more.
        assert get_args(CommandOutcome) == (
            "completed",
            "command_not_found",
            "start_failed",
            "timed_out",
        )

    def test_run_outcome_values(self) -> None:
        # Requirement: run outcomes distinguish success, failure, cancellation.
        assert get_args(RunOutcome) == ("succeeded", "failed", "cancelled")

    def test_start_error_kind_values(self) -> None:
        # Requirement: exactly the two OSError branches the executor chains.
        assert get_args(StartErrorKind) == ("file_not_found", "os_error")

    def test_start_error_kind_is_literal(self) -> None:
        # Requirement: kind is typed as the Literal alias, not a free str.
        kind_field = next(f for f in dataclasses.fields(StartError) if f.name == "kind")
        assert kind_field.type == "StartErrorKind"
        assert get_args(StartErrorKind) == ("file_not_found", "os_error")


class TestStartErrorRoundTrip:
    """Requirement: StartError reconstructs the original exception for chaining."""

    @pytest.mark.parametrize(
        "original",
        [
            FileNotFoundError(2, "No such file or directory", "no-such-cmd"),
            PermissionError(13, "Permission denied", "/root/secret.sh"),
            OSError(98, "Address already in use"),
        ],
    )
    def test_errno_bearing_round_trip(self, original: OSError) -> None:
        # Requirement: with a structured errno the reconstructed exception
        # matches the original on type/str/errno/filename (CPython's
        # OSError(errno, ...) constructor auto-selects the built-in subclass,
        # e.g. PermissionError for errno 13).
        start_error = _fill_start_error(original)
        reconstructed = _reconstruct_cause(start_error)
        assert type(reconstructed) is type(original)
        assert str(reconstructed) == str(original)
        assert reconstructed.errno == original.errno
        assert reconstructed.filename == original.filename
        assert start_error.message == (
            original.strerror if original.errno is not None else str(original)
        )

    def test_errno_free_round_trip(self) -> None:
        # Requirement: without an errno the fill uses str(e) and the rebuild
        # passes the message positionally — no fabricated errno appears.
        original = FileNotFoundError("no errno at all")
        assert original.errno is None
        start_error = _fill_start_error(original)
        assert start_error.kind == "file_not_found"
        assert start_error.errno is None
        assert start_error.message == str(original)
        reconstructed = _reconstruct_cause(start_error)
        assert type(reconstructed) is FileNotFoundError
        assert str(reconstructed) == str(original)
        assert reconstructed.errno is None

    def test_kind_classification(self) -> None:
        # Requirement: FileNotFoundError maps to "file_not_found", any other
        # OSError to "os_error".
        assert _fill_start_error(FileNotFoundError(2, "nope", "x")).kind == "file_not_found"
        assert _fill_start_error(OSError(5, "io error", "y")).kind == "os_error"

    def test_no_raw_exception_in_contract(self) -> None:
        # Requirement: raw exception objects never appear in the contract —
        # every StartError field is plain serializable data.
        error = _fill_start_error(PermissionError(13, "Permission denied", "/tmp/x"))
        for field in dataclasses.fields(error):
            value = getattr(error, field.name)
            assert not isinstance(value, BaseException)


def test_aliases_are_typing_literals() -> None:
    # Requirement: outcomes are plain data (Literal aliases), not enums or
    # exception subclasses — house style, see RunMode in fleet/records.py.
    assert CommandOutcome.__origin__ is Literal  # type: ignore[attr-defined]
    assert RunOutcome.__origin__ is Literal  # type: ignore[attr-defined]


class TestResolvedExecutionSpecAndBundleRef:
    """Requirement: ResolvedExecutionSpec and BundleRef contract types and extensions."""

    def test_bundle_ref_fields_and_immutability(self) -> None:
        # Requirement: BundleRef holds digest and store_path as frozen data.
        bundle = BundleRef(
            digest="sha256:abc123",
            store_path="/var/conductor/cache/bundles/sha256-abc123",
        )
        assert bundle.digest == "sha256:abc123"
        assert bundle.store_path == "/var/conductor/cache/bundles/sha256-abc123"
        with pytest.raises(dataclasses.FrozenInstanceError):
            bundle.digest = "sha256:def456"  # type: ignore[misc]

    def test_resolved_execution_spec_defaults_and_immutability(self) -> None:
        # Requirement: ResolvedExecutionSpec omits all flags by default (None/False),
        # preserving platform-native daemon defaults.
        spec = ResolvedExecutionSpec(image="alpine:3.20")
        assert spec.image == "alpine:3.20"
        assert spec.platform is None
        assert spec.network is None
        assert spec.user is None
        assert spec.init is False
        assert spec.read_only is False
        assert spec.cap_drop_all is False
        assert spec.no_new_privileges is False
        assert spec.tmpfs is False
        assert spec.cpu is None
        assert spec.memory is None
        assert spec.pids is None
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.image = "busybox"  # type: ignore[misc]

    def test_resolved_execution_spec_populated(self) -> None:
        # Requirement: ResolvedExecutionSpec holds fully populated container configuration.
        spec = ResolvedExecutionSpec(
            image="alpine@sha256:1234567890abcdef",
            platform="linux/amd64",
            network="none",
            user="1000:1000",
            init=True,
            read_only=True,
            cap_drop_all=True,
            no_new_privileges=True,
            tmpfs="1g",
            cpu=2.0,
            memory="512m",
            pids=100,
        )
        assert spec.image == "alpine@sha256:1234567890abcdef"
        assert spec.platform == "linux/amd64"
        assert spec.network == "none"
        assert spec.user == "1000:1000"
        assert spec.init is True
        assert spec.read_only is True
        assert spec.cap_drop_all is True
        assert spec.no_new_privileges is True
        assert spec.tmpfs == "1g"
        assert spec.cpu == 2.0
        assert spec.memory == "512m"
        assert spec.pids == 100

    def test_command_spec_new_fields_defaults_and_population(self) -> None:
        # Requirement: CommandSpec new fields (execution, name) default to None,
        # ensuring backward compatibility, and accept explicit values.
        default_spec = CommandSpec(command="echo")
        assert default_spec.execution is None
        assert default_spec.name is None

        exec_spec = ResolvedExecutionSpec(image="alpine:latest")
        populated_spec = CommandSpec(
            command="python",
            args=("-c", "print(1)"),
            execution=exec_spec,
            name="step-1",
        )
        assert populated_spec.execution == exec_spec
        assert populated_spec.name == "step-1"
        with pytest.raises(dataclasses.FrozenInstanceError):
            populated_spec.execution = None  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            populated_spec.name = "step-2"  # type: ignore[misc]

    def test_run_spec_bundle_default_and_population(self) -> None:
        # Requirement: RunSpec bundle field defaults to None and accepts BundleRef.
        default_spec = RunSpec(run_id="run-123")
        assert default_spec.bundle is None

        bundle_ref = BundleRef(digest="sha256:abc", store_path="/path/to/bundle")
        populated_spec = RunSpec(run_id="run-123", workflow_name="wf", bundle=bundle_ref)
        assert populated_spec.bundle == bundle_ref
        with pytest.raises(dataclasses.FrozenInstanceError):
            populated_spec.bundle = None  # type: ignore[misc]

    def test_backward_compatibility_and_equality_parity(self) -> None:
        # Requirement: Old-style constructions of CommandSpec and RunSpec behave
        # exactly as before with full equality and positional/keyword parity.
        spec1 = CommandSpec("echo")
        spec2 = CommandSpec(
            command="echo",
            args=(),
            working_dir=None,
            env={},
            inherit_control_environment=True,
            stdin=None,
            timeout=None,
            execution=None,
            name=None,
        )
        assert spec1 == spec2

        run1 = RunSpec("run-1")
        run2 = RunSpec(run_id="run-1", workflow_name=None, bundle=None)
        assert run1 == run2
