"""Docker CLI implementation of the runner backend contract.

The backend requires Docker Engine 20.10 or newer (API 1.41).  It deliberately
uses the Docker CLI rather than an SDK so the operator's active context,
credential helpers, and remote ``DOCKER_HOST`` continue to work.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import json
import logging
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import cast
from uuid import uuid4

from conductor.execution.errors import ExecutionSpecError
from conductor.execution.types import (
    BundleRef,
    CommandResult,
    CommandSpec,
    ResolvedExecutionSpec,
    RunnerCapabilities,
    RunOutcome,
    RunSpec,
    StartError,
    WorkspaceLease,
)

logger = logging.getLogger(__name__)

_AUXILIARY_TIMEOUT_SECONDS = 300.0
_BOUNDED_OUTPUT_CHARS = 2_000
_CLI_TIMEOUT_RC = -124
_WINDOWS_SHARING_RETRIES = 6
_WINDOWS_SHARING_DELAY_SECONDS = 0.05
_WINDOWS_TREE_KILL_TIMEOUT_SECONDS = 5.0
_WINDOWS_SHARING_ERRORS = {errno.EACCES, errno.EPERM}
_CONTAINER_NAME = re.compile(r"\A/?conductor-[A-Za-z0-9_-]+\Z")
_ENV_NAME = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")
_WINDOWS_PATH = re.compile(r"\A([A-Za-z]:|\\\\)")

# Spike finding (see docs/design/docker-backend.md, "Spike Findings and
# Ownership Mechanism"): candidate 1 (plain cp) left uid 65532 unable to
# write; candidate 2 (docker cp -a) succeeded.
_STAGING_COPY_MODE = "archive-to-container-user"
"""Use ``docker cp -a`` with the scratch container's resolved ``--user``.

An ordered real-daemon spike proved this is the first candidate that lets
uid 65532 write the staged volume (the findings are summarized in
docs/design/docker-backend.md).
"""


@dataclass(frozen=True)
class _LeaseState:
    run: RunSpec


class _DockerFailure(RuntimeError):
    pass


def _bounded(value: str) -> str:
    if len(value) <= _BOUNDED_OUTPUT_CHARS:
        return value
    return value[-_BOUNDED_OUTPUT_CHARS:]


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


async def _kill_and_reap(
    process: asyncio.subprocess.Process,
    communicate_task: asyncio.Task[tuple[bytes, bytes]],
) -> bool:
    """Kill a Docker CLI process and drain/reap it despite racing cancellation."""
    if sys.platform == "win32":
        # A wrapper-shim CLI (e.g. docker.cmd run through cmd.exe) puts the
        # real workload in a grandchild whose inherited pipe handles stay open
        # after the direct child dies, wedging the drain below until that
        # grandchild exits on its own. Best-effort tree kill first, bounded so
        # the event loop is never blocked unboundedly; any failure falls
        # through to the plain kill of the direct child.
        with contextlib.suppress(Exception):
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_WINDOWS_TREE_KILL_TIMEOUT_SECONDS,
                check=False,
            )
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    absorbed_cancel = False
    while not communicate_task.done():
        try:
            await asyncio.shield(communicate_task)
        except asyncio.CancelledError:
            absorbed_cancel = True
        except Exception:
            break
    if communicate_task.done() and not communicate_task.cancelled():
        communicate_task.exception()
    wait_task = asyncio.ensure_future(process.wait())
    while not wait_task.done():
        try:
            await asyncio.shield(wait_task)
        except asyncio.CancelledError:
            absorbed_cancel = True
        except Exception:
            break
    if wait_task.done() and not wait_task.cancelled():
        wait_task.exception()
    return absorbed_cancel


async def _await_task_cleanup(task: asyncio.Task[object]) -> bool:
    absorbed_cancel = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            absorbed_cancel = True
        except Exception:
            break
    if task.done() and not task.cancelled():
        task.exception()
    return absorbed_cancel


def _remove_tree_with_retry(path: Path) -> None:
    for attempt in range(_WINDOWS_SHARING_RETRIES):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            sharing_violation = sys.platform == "win32" and exc.errno in _WINDOWS_SHARING_ERRORS
            if not sharing_violation or attempt == _WINDOWS_SHARING_RETRIES - 1:
                raise
            time.sleep(_WINDOWS_SHARING_DELAY_SECONDS * (attempt + 1))


def _unlink_with_retry(path: Path) -> None:
    # Windows may keep a short-lived handle on the env file after the Docker
    # CLI exits (wrapper shims, indexers), so deletion follows the same
    # bounded sharing-violation retry as the staging tree removal.
    for attempt in range(_WINDOWS_SHARING_RETRIES):
        try:
            os.unlink(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            sharing_violation = sys.platform == "win32" and exc.errno in _WINDOWS_SHARING_ERRORS
            if not sharing_violation or attempt == _WINDOWS_SHARING_RETRIES - 1:
                raise
            time.sleep(_WINDOWS_SHARING_DELAY_SECONDS * (attempt + 1))


def _resolve_symlink_within_root(source: Path, root: Path, target: str) -> str | None:
    """Resolve a link target lexically, returning None when it escapes root."""
    if os.path.isabs(target):
        return None
    resolved = os.path.abspath(os.path.normpath(source.parent / target))
    root_path = os.path.abspath(root)
    try:
        if os.path.commonpath((resolved, root_path)) != root_path:
            return None
    except ValueError:
        return None
    return resolved


def _copy_tree_preserving_symlinks(
    source: Path,
    destination: Path,
    *,
    allowed_root: Path | None = None,
) -> None:
    """Materialize a tree with hardlinks while preserving safe symlinks."""
    root = source if allowed_root is None else allowed_root
    destination.mkdir(parents=True, exist_ok=True)
    for entry in os.scandir(source):
        src = Path(entry.path)
        dst = destination / entry.name
        if entry.is_symlink():
            target = os.readlink(src)
            resolved = _resolve_symlink_within_root(src, root, target)
            if resolved is None:
                raise ExecutionSpecError(
                    f"staging symlink escapes its declared root: {src} -> {target!r}"
                )
            # The directory bit is read from the lexically resolved target,
            # never by following the link object: a Linux-authored
            # forward-slash target must be staged verbatim even on hosts that
            # cannot traverse such a link (Windows stat-through fails with
            # WinError 123). A target that cannot be stat'ed (dangling or
            # otherwise unreadable) is treated as a non-directory; for a
            # dangling target that matches the previous follow-the-link
            # behavior.
            os.symlink(target, dst, target_is_directory=os.path.isdir(resolved))
        elif entry.is_dir(follow_symlinks=False):
            _copy_tree_preserving_symlinks(src, dst, allowed_root=root)
        elif entry.is_file(follow_symlinks=False):
            try:
                os.link(src, dst)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                shutil.copy2(src, dst)


def _map_working_dir(value: str | None, root: str = "main") -> str:
    if value is None:
        return "/workspace"
    stripped = value.strip()
    if not stripped:
        raise ExecutionSpecError(f"working_dir must not be empty after stripping: {value!r}")
    if "\\" in stripped:
        raise ExecutionSpecError(
            f"working_dir must use POSIX separators, not backslashes: {value!r}"
        )
    if _WINDOWS_PATH.match(stripped):
        raise ExecutionSpecError(f"working_dir must not be a Windows drive or UNC path: {value!r}")
    if posixpath.isabs(stripped):
        return stripped
    base = posixpath.join("/workspace", root)
    mapped = posixpath.normpath(posixpath.join(base, stripped))
    if not mapped.startswith(f"{base}/") and mapped != base:
        raise ExecutionSpecError(f"working_dir must not escape {base} with '..': {value!r}")
    return mapped


class DockerRunnerBackend:
    """Run commands in short-lived containers over a run-scoped named volume."""

    def __init__(self, docker_binary: str = "docker") -> None:
        """Capture configuration without contacting the filesystem or daemon."""
        self._docker_binary = docker_binary
        self._cli_env = dict(os.environ)
        self._lease_states: dict[tuple[str, str], _LeaseState] = {}
        self._staged: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._closed_leases: set[tuple[str, str]] = set()
        self._attempts: dict[tuple[str, str], int] = {}

    def capabilities(self) -> RunnerCapabilities:
        """Declare Docker batch execution with a shared named-volume workspace."""
        return RunnerCapabilities(
            batch=True,
            sessions=False,
            shared_workspace=True,
            snapshots=False,
        )

    async def prepare_run(self, run: RunSpec) -> WorkspaceLease:
        """Create a cheap lease and retain its bundle reference for lazy staging."""
        lease = WorkspaceLease(
            lease_id=run.run_id,
            backend="docker",
            incarnation=uuid4().hex[:12],
        )
        self._lease_states[self._lease_key(lease)] = _LeaseState(run=run)
        return lease

    @staticmethod
    def _lease_key(lease: WorkspaceLease) -> tuple[str, str]:
        return (lease.lease_id, lease.incarnation)

    @staticmethod
    def _volume_name(lease: WorkspaceLease) -> str:
        return f"conductor-ws-{lease.lease_id}"

    @staticmethod
    def _labels(lease: WorkspaceLease, resource: str) -> dict[str, str]:
        return {
            "io.conductor.managed": "true",
            "io.conductor.run_id": lease.lease_id,
            "io.conductor.workspace": lease.lease_id,
            "io.conductor.resource": resource,
            "io.conductor.incarnation": lease.incarnation,
        }

    @staticmethod
    def _label_args(labels: Mapping[str, str]) -> list[str]:
        result: list[str] = []
        for key, value in labels.items():
            result.extend(("--label", f"{key}={value}"))
        return result

    def _resolved_binary(self) -> str | None:
        return shutil.which(self._docker_binary, path=self._cli_env.get("PATH"))

    async def _run_docker(
        self,
        argv: Sequence[str],
        *,
        stdin_bytes: bytes | None = None,
        timeout: float | None = _AUXILIARY_TIMEOUT_SECONDS,
        diagnostics: Callable[[str], None] | None = None,
    ) -> tuple[int, str, str]:
        """Run one Docker CLI command with bounded diagnostics and safe teardown."""
        binary = self._resolved_binary()
        if binary is None:
            raise FileNotFoundError(errno.ENOENT, "Docker CLI was not found", self._docker_binary)
        process = await asyncio.create_subprocess_exec(
            binary,
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._cli_env,
        )
        communicate_task = asyncio.create_task(process.communicate(input=stdin_bytes))
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                asyncio.shield(communicate_task), timeout=timeout
            )
        except TimeoutError:
            if await _kill_and_reap(process, communicate_task):
                raise asyncio.CancelledError from None
            return _CLI_TIMEOUT_RC, "", "Docker CLI command timed out"
        except asyncio.CancelledError:
            await _kill_and_reap(process, communicate_task)
            raise
        stdout = _decode(stdout_bytes)
        stderr = _decode(stderr_bytes)
        if diagnostics is not None and stderr.strip():
            diagnostics(f"  Docker: {_bounded(stderr.strip())}")
        assert process.returncode is not None
        return process.returncode, stdout, stderr

    async def _inspect_labels(self, object_type: str, identifier: str) -> dict[str, str] | None:
        format_value = (
            "{{json .Config.Labels}}" if object_type == "container" else "{{json .Labels}}"
        )
        command = "inspect" if object_type == "container" else "volume"
        argv = [command]
        if object_type == "volume":
            argv.append("inspect")
        argv.extend(("--format", format_value, identifier))
        rc, stdout, stderr = await self._run_docker(argv)
        if rc != 0:
            # Tolerate only an object-specific confirmed absence. A broad
            # "no such" match would also swallow unrelated daemon errors such
            # as "no such host" and misreport the object as missing.
            if rc != _CLI_TIMEOUT_RC and f"no such {object_type}" in stderr.lower():
                return None
            raise _DockerFailure(
                f"could not inspect Docker {object_type} {identifier}: {_bounded(stderr)}"
            )
        try:
            labels = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise _DockerFailure(
                f"Docker {object_type} {identifier} returned malformed labels: {_bounded(stdout)}"
            ) from exc
        if not isinstance(labels, dict):
            raise _DockerFailure(
                f"Docker {object_type} {identifier} returned non-dict labels: {_bounded(stdout)}"
            )
        return labels

    async def _ensure_staged(
        self,
        lease: WorkspaceLease,
        execution: ResolvedExecutionSpec,
        diagnostics: Callable[[str], None] | None,
    ) -> None:
        key = self._lease_key(lease)
        if key in self._closed_leases:
            raise ExecutionSpecError("backend is finalizing")
        task = self._staged.get(key)
        if task is None:
            task = asyncio.create_task(self._stage_workspace(lease, execution, diagnostics))
            self._staged[key] = task
        await asyncio.shield(task)

    async def _ensure_image(
        self,
        execution: ResolvedExecutionSpec,
        diagnostics: Callable[[str], None] | None,
    ) -> None:
        rc, _stdout, _stderr = await self._run_docker(("image", "inspect", execution.image))
        if rc == 0:
            return
        if diagnostics is not None:
            diagnostics(f"  Docker: pulling {execution.image}")
        pull = ["pull"]
        if execution.platform is not None:
            pull.extend(("--platform", execution.platform))
        pull.append(execution.image)
        rc, _stdout, stderr = await self._run_docker(pull, diagnostics=diagnostics)
        if rc != 0:
            raise _DockerFailure(
                self._version_hint(stderr) or f"docker pull failed: {_bounded(stderr)}"
            )

    @staticmethod
    def _version_hint(stderr: str) -> str:
        lowered = stderr.lower()
        if "unknown flag" in lowered or "unknown shorthand" in lowered:
            return f"{_bounded(stderr)} Docker Engine 20.10 or newer (API 1.41) is required."
        return ""

    async def _stage_workspace(
        self,
        lease: WorkspaceLease,
        execution: ResolvedExecutionSpec,
        diagnostics: Callable[[str], None] | None,
    ) -> None:
        key = self._lease_key(lease)
        if key in self._closed_leases:
            raise ExecutionSpecError("backend is finalizing")
        state = self._lease_states.get(key)
        if state is None:
            raise ExecutionSpecError("Docker lease was not prepared by this backend")
        volume = self._volume_name(lease)
        existing = await self._inspect_labels("volume", volume)
        expected = self._labels(lease, "workspace")
        if existing is not None and all(existing.get(k) == v for k, v in expected.items()):
            return
        if existing is not None:
            rc, _stdout, stderr = await self._run_docker(("volume", "rm", "-f", volume))
            if rc != 0:
                raise _DockerFailure(f"could not replace stale Docker volume: {_bounded(stderr)}")
        create = ["volume", "create", *self._label_args(expected), volume]
        rc, _stdout, stderr = await self._run_docker(create)
        if rc != 0:
            raise _DockerFailure(f"docker volume create failed: {_bounded(stderr)}")
        bundle = state.run.bundle
        if bundle is None:
            return
        await self._ensure_image(execution, diagnostics)
        scratch = f"conductor-stage-{lease.lease_id[:8]}-{uuid4().hex[:6]}"
        create_scratch = [
            "create",
            "--name",
            scratch,
            *self._label_args(self._labels(lease, "scratch")),
        ]
        if execution.platform is not None:
            create_scratch.extend(("--platform", execution.platform))
        if execution.user is not None:
            create_scratch.extend(("--user", execution.user))
        create_scratch.extend(
            (
                "-v",
                f"{volume}:/workspace",
                "--entrypoint",
                "/conductor-staging-placeholder",
                execution.image,
            )
        )
        staging = Path(tempfile.mkdtemp(prefix=f"conductor-stage-{lease.lease_id[:8]}-"))
        try:
            self._build_staging_tree(bundle, staging)
            rc, _stdout, stderr = await self._run_docker(create_scratch)
            if rc != 0:
                raise _DockerFailure(
                    self._version_hint(stderr)
                    or f"docker staging create failed: {_bounded(stderr)}"
                )
            source = PurePath(staging).as_posix().rstrip("/") + "/."
            rc, _stdout, stderr = await self._run_docker(
                ("cp", "-a", source, f"{scratch}:/workspace")
            )
            if rc != 0:
                raise _DockerFailure(f"docker cp staging failed: {_bounded(stderr)}")
        finally:

            async def cleanup_staging() -> None:
                try:
                    with contextlib.suppress(Exception):
                        await self._run_docker(("rm", "-f", scratch))
                finally:
                    try:
                        _remove_tree_with_retry(staging)
                    except OSError as exc:
                        logger.warning(
                            "Could not remove Docker staging directory %s for run_id=%s: %s",
                            staging,
                            lease.lease_id,
                            exc,
                        )

            cleanup_task = asyncio.create_task(cleanup_staging())
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await _await_task_cleanup(cast(asyncio.Task[object], cleanup_task))
                raise

    @staticmethod
    def _build_staging_tree(bundle: BundleRef, staging: Path) -> None:
        tree = Path(bundle.store_path) / "tree"
        if not tree.is_dir():
            raise ExecutionSpecError(f"bundle tree does not exist: {tree}")
        _copy_tree_preserving_symlinks(tree, staging, allowed_root=tree)

    def _next_attempt(self, lease: WorkspaceLease) -> int:
        key = self._lease_key(lease)
        attempt = self._attempts.get(key, 0) + 1
        self._attempts[key] = attempt
        return attempt

    @staticmethod
    def _create_environment(spec: CommandSpec, snapshot: Mapping[str, str]) -> dict[str, str]:
        # On Windows the host env is case-insensitive while the container's
        # Linux env is case-sensitive; a spec.env key differing from a host
        # variable only by case replaces it here (see the Windows environment
        # case-semantics section of docs/design/docker-backend.md).
        env = dict(snapshot) if spec.inherit_control_environment else {}
        if sys.platform == "win32":
            overridden = {key.casefold() for key in spec.env}
            env = {key: value for key, value in env.items() if key.casefold() not in overridden}
        env.update(spec.env)
        return env

    @staticmethod
    @contextlib.contextmanager
    def _container_env_file(env: Mapping[str, str]) -> Iterator[str]:
        for name, value in env.items():
            if _ENV_NAME.fullmatch(name) is None:
                raise ExecutionSpecError(f"invalid container environment variable name: {name}")
            if "\0" in value or "\n" in value or "\r" in value:
                raise ExecutionSpecError(f"invalid container environment variable value for {name}")
        path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", prefix="conductor-env-", delete=False
            ) as stream:
                path = stream.name
                for name, value in env.items():
                    stream.write(f"{name}={value}\n")
            yield path
        finally:
            if path is not None:
                try:
                    _unlink_with_retry(Path(path))
                except OSError as exc:
                    logger.warning("Could not remove Docker container env file %s: %s", path, exc)

    def _create_argv(
        self,
        spec: CommandSpec,
        lease: WorkspaceLease,
        execution: ResolvedExecutionSpec,
        name: str,
        attempt: int,
        env_file: str,
    ) -> list[str]:
        labels = self._labels(lease, "exec") | {
            "io.conductor.step": spec.name or spec.command,
            "io.conductor.attempt": str(attempt),
        }
        argv = ["create", "--name", name, *self._label_args(labels)]
        if execution.init:
            argv.append("--init")
        if execution.read_only:
            argv.append("--read-only")
        if execution.cap_drop_all:
            argv.append("--cap-drop=ALL")
        if execution.no_new_privileges:
            argv.append("--security-opt=no-new-privileges")
        if execution.tmpfs is True:
            argv.extend(("--tmpfs", "/tmp"))
        elif isinstance(execution.tmpfs, str):
            argv.extend(("--tmpfs", f"/tmp:size={execution.tmpfs}"))
        for flag, value in (
            ("--network", execution.network),
            ("--platform", execution.platform),
            ("--user", execution.user),
            ("--cpus", str(execution.cpu) if execution.cpu is not None else None),
            ("--memory", execution.memory),
            ("--pids-limit", str(execution.pids) if execution.pids is not None else None),
        ):
            if value is not None:
                argv.extend((flag, value))
        argv.extend(("-v", f"{self._volume_name(lease)}:/workspace"))
        state = self._lease_states.get(self._lease_key(lease))
        if state is None:
            raise ExecutionSpecError("Docker lease was not prepared by this backend")
        root = state.run.bundle.root if state.run.bundle is not None else "main"
        argv.extend(("-w", _map_working_dir(spec.working_dir, root)))
        argv.extend(("--env-file", env_file))
        if spec.stdin is not None:
            argv.append("--interactive")
        argv.extend(("--entrypoint", spec.command, execution.image, *spec.args))
        return argv

    async def run_command(
        self,
        spec: CommandSpec,
        lease: WorkspaceLease | None,
        *,
        diagnostics: Callable[[str], None] | None = None,
    ) -> CommandResult:
        """Run one command in Docker and return its data-shaped outcome."""
        started_at = time.monotonic()
        if spec.execution is None:
            raise ExecutionSpecError("Docker command requires an execution specification")
        if lease is None or lease.backend != "docker":
            raise ExecutionSpecError("Docker command requires a Docker workspace lease")
        key = self._lease_key(lease)
        if key in self._closed_leases:
            raise ExecutionSpecError("backend is finalizing")
        binary = self._resolved_binary()
        if binary is None:
            error = FileNotFoundError(errno.ENOENT, "Docker CLI was not found", self._docker_binary)
            return CommandResult(
                outcome="command_not_found",
                resolved_command=self._docker_binary,
                start_error=StartError(
                    kind="file_not_found",
                    message=cast(str, error.strerror),
                    errno=error.errno,
                    filename=cast(str, error.filename),
                ),
                duration_seconds=time.monotonic() - started_at,
            )
        execution = spec.execution
        attempt = self._next_attempt(lease)
        step_hash = hashlib.sha1((spec.name or spec.command).encode()).hexdigest()[:8]
        name = f"conductor-{lease.lease_id[:8]}-{step_hash}-{attempt}"
        created = False
        started = False
        try:
            try:
                await self._ensure_staged(lease, execution, diagnostics)
                await self._ensure_image(execution, diagnostics)
                create_env = self._create_environment(spec, self._cli_env)
                with self._container_env_file(create_env) as env_file:
                    rc, _stdout, stderr = await self._run_docker(
                        self._create_argv(spec, lease, execution, name, attempt, env_file)
                    )
                if rc != 0:
                    return self._start_failed(
                        name, self._version_hint(stderr) or _bounded(stderr), started_at
                    )
                created = True
                start_argv = ["start", "--attach"]
                if spec.stdin is not None:
                    start_argv.append("--interactive")
                start_argv.append(name)
                start_rc, stdout, stderr = await self._run_docker(
                    start_argv,
                    stdin_bytes=spec.stdin,
                    timeout=spec.timeout,
                    diagnostics=diagnostics,
                )
                if start_rc == _CLI_TIMEOUT_RC:
                    cleanup_task = asyncio.create_task(
                        self._cleanup_container(lease, name, kill=True)
                    )
                    try:
                        removed = await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:
                        await _await_task_cleanup(cast(asyncio.Task[object], cleanup_task))
                        created = not cleanup_task.result()
                        raise
                    created = not removed
                    return CommandResult(
                        outcome="timed_out",
                        resolved_command=spec.command,
                        duration_seconds=time.monotonic() - started_at,
                    )
                started = True
                rc, inspect_stdout, inspect_stderr = await self._run_docker(
                    ("inspect", "--format", "{{json .State}}", name)
                )
                if rc != 0:
                    message = inspect_stderr or stderr
                    return self._start_failed(name, _bounded(message), started_at)
                parsed_state = json.loads(inspect_stdout)
                state = (
                    cast(dict[str, object], parsed_state) if isinstance(parsed_state, dict) else {}
                )
                exit_code = state.get("ExitCode")
                if not isinstance(exit_code, int):
                    return self._start_failed(
                        name, "Docker returned no integer ExitCode", started_at
                    )
                if start_rc != 0 and exit_code != start_rc:
                    return self._start_failed(name, _bounded(stderr), started_at)
                return CommandResult(
                    outcome="completed",
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=exit_code,
                    resolved_command=spec.command,
                    duration_seconds=time.monotonic() - started_at,
                )
            except (_DockerFailure, ExecutionSpecError) as exc:
                return self._start_failed(name, str(exc), started_at)
            except OSError as exc:
                return self._start_failed(name, str(exc), started_at, exc)
            except json.JSONDecodeError as exc:
                return self._start_failed(name, str(exc), started_at)
        except asyncio.CancelledError:
            cleanup_task = asyncio.create_task(
                self._cleanup_container(lease, name, kill=created or started)
            )
            await _await_task_cleanup(cast(asyncio.Task[object], cleanup_task))
            created = not cleanup_task.result()
            raise
        finally:
            if created:
                cleanup_task = asyncio.create_task(self._cleanup_container(lease, name, kill=False))
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    await _await_task_cleanup(cast(asyncio.Task[object], cleanup_task))
                    raise

    @staticmethod
    def _start_failed(
        name: str,
        message: str,
        started_at: float,
        cause: OSError | None = None,
    ) -> CommandResult:
        return CommandResult(
            outcome="start_failed",
            stderr=_bounded(message),
            resolved_command=name,
            start_error=StartError(
                kind="os_error",
                message=_bounded(message),
                errno=cause.errno if cause is not None else None,
                filename=cast(str | None, cause.filename) if cause is not None else None,
            ),
            duration_seconds=time.monotonic() - started_at,
        )

    async def _cleanup_container(self, lease: WorkspaceLease, name: str, *, kill: bool) -> bool:
        failures: list[str] = []
        absent = False
        if kill:
            try:
                rc, _stdout, stderr = await self._run_docker(("kill", name))
                if rc != 0:
                    absent = rc != _CLI_TIMEOUT_RC and "no such container" in stderr.lower()
                    if not absent:
                        failures.append(f"kill: {_bounded(stderr)} (exit {rc})")
            except Exception as exc:
                failures.append(f"kill: {_bounded(str(exc))}")
        try:
            rc, _stdout, stderr = await self._run_docker(("rm", "-f", name))
            removed = rc == 0 or (rc != _CLI_TIMEOUT_RC and "no such container" in stderr.lower())
            if not removed:
                failures.append(f"rm -f: {_bounded(stderr)} (exit {rc})")
        except Exception as exc:
            removed = False
            failures.append(f"rm -f: {_bounded(str(exc))}")
        if not removed and not absent:
            logger.warning(
                "Docker cleanup unconfirmed for run_id=%s container=%s: %s. "
                "Manual cleanup: docker rm -f %s",
                lease.lease_id,
                name,
                _bounded("; ".join(failures)),
                name,
            )
        return removed or absent

    async def finalize_run(self, lease: WorkspaceLease, outcome: RunOutcome) -> None:
        """Fail-closed cleanup of containers and the run-scoped workspace volume."""
        del outcome
        key = self._lease_key(lease)
        self._closed_leases.add(key)
        task = self._staged.pop(key, None)
        caller = asyncio.current_task()
        cancelling_before = caller.cancelling() if caller is not None else 0
        if task is not None and not task.done():
            task.cancel()
            await _await_task_cleanup(cast(asyncio.Task[object], task))
        cancelled = caller is not None and caller.cancelling() > cancelling_before
        self._attempts.pop(key, None)
        self._lease_states.pop(key, None)
        cleanup_task = asyncio.create_task(self._finalize_resources(lease))
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            await _await_task_cleanup(cast(asyncio.Task[object], cleanup_task))
            cancelled = True
        except Exception as exc:
            self._warn_cleanup(lease, str(exc))
        if cancelled:
            raise asyncio.CancelledError

    async def _finalize_resources(self, lease: WorkspaceLease) -> None:
        try:
            rc, stdout, stderr = await self._run_docker(
                (
                    "ps",
                    "-aq",
                    "--filter",
                    "label=io.conductor.managed=true",
                    "--filter",
                    f"label=io.conductor.run_id={lease.lease_id}",
                )
            )
            if rc != 0:
                self._warn_cleanup(lease, f"container listing failed: {_bounded(stderr)}")
            else:
                for candidate in stdout.split():
                    await self._finalize_container(lease, candidate)
            await self._finalize_volume(lease)
        except Exception as exc:
            self._warn_cleanup(lease, str(exc))

    async def _finalize_container(self, lease: WorkspaceLease, candidate: str) -> None:
        rc, stdout, stderr = await self._run_docker(
            ("inspect", "--format", "{{json .}}", candidate)
        )
        if rc != 0:
            if "no such container" not in stderr.lower():
                self._warn_cleanup(
                    lease, f"could not inspect container {candidate}: {_bounded(stderr)}"
                )
            return
        try:
            data = json.loads(stdout)
            labels = (
                data[0]["Config"]["Labels"] if isinstance(data, list) else data["Config"]["Labels"]
            )
            name = data[0]["Name"] if isinstance(data, list) else data["Name"]
        except (KeyError, TypeError, json.JSONDecodeError):
            self._warn_cleanup(lease, f"container {candidate} returned unverifiable metadata")
            return
        expected = self._labels(lease, cast(str, labels.get("io.conductor.resource")))
        valid_resource = labels.get("io.conductor.resource") in {"exec", "scratch"}
        valid_labels = valid_resource and all(labels.get(k) == v for k, v in expected.items())
        if not valid_labels or not isinstance(name, str) or _CONTAINER_NAME.match(name) is None:
            self._warn_cleanup(lease, f"container {candidate} failed label/name verification")
            return
        rc, _stdout, stderr = await self._run_docker(("rm", "-f", candidate))
        if rc != 0 and "no such container" not in stderr.lower():
            self._warn_cleanup(lease, f"could not remove container {candidate}: {_bounded(stderr)}")

    async def _finalize_volume(self, lease: WorkspaceLease) -> None:
        volume = self._volume_name(lease)
        try:
            labels = await self._inspect_labels("volume", volume)
        except _DockerFailure as exc:
            self._warn_cleanup(lease, str(exc))
            return
        if labels is None:
            return
        expected = self._labels(lease, "workspace")
        if not all(labels.get(k) == v for k, v in expected.items()):
            self._warn_cleanup(lease, f"volume {volume} failed label verification")
            return
        rc, _stdout, stderr = await self._run_docker(("volume", "rm", "-f", volume))
        if rc != 0 and "no such volume" not in stderr.lower():
            self._warn_cleanup(lease, f"could not remove volume {volume}: {_bounded(stderr)}")

    @staticmethod
    def _warn_cleanup(lease: WorkspaceLease, reason: str) -> None:
        logger.warning(
            "Docker cleanup failed for run_id=%s: %s. Manual cleanup: "
            "docker ps -aq --filter label=io.conductor.run_id=%s | xargs docker rm -f; "
            "docker volume rm -f conductor-ws-%s",
            lease.lease_id,
            reason,
            lease.lease_id,
            lease.lease_id,
        )
