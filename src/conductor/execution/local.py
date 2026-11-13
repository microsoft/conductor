"""Local-process implementation of the runner backend contract."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import cast
from uuid import uuid4

from conductor.exceptions import ConfigurationError
from conductor.execution import local_workspace
from conductor.execution.errors import ExecutionSpecError, WorkspaceAttachError
from conductor.execution.local_process_scope import (
    LocalProcessScopeController,
    _ControllerInstallation,
    _installation_warning,
)
from conductor.execution.types import (
    AgentEventSink,
    AgentResult,
    AgentSpec,
    CommandResult,
    CommandSpec,
    LocalWorkspaceBinding,
    ManagedWorkspaceContext,
    RunnerCapabilities,
    RunOutcome,
    RunSpec,
    StartError,
    WorkspaceIdentity,
    WorkspaceLease,
)

_log = logging.getLogger(__name__)


@dataclass
class _ManagedLease:
    """Mutable backend-owned marker state, never reconstructed from a location hint."""

    run: RunSpec
    identity: local_workspace.LocalIdentity
    controller: LocalProcessScopeController
    released: bool = False
    uncertain: bool = False


async def _kill_and_reap(
    process: asyncio.subprocess.Process,
    communicate_task: asyncio.Task[tuple[bytes, bytes]],
) -> bool:
    """Kill the child and finish draining it before returning.

    The shielded ``communicate_task`` is never cancelled here, so the pipe
    transports keep reading to EOF — a killed child that left full pipe
    buffers behind cannot wedge the reap. The subsequent ``process.wait()``
    runs as its own shielded task for the same reason: a second cancellation
    racing the cleanup is absorbed and reported (return value) instead of
    interrupting it, and the caller re-raises ``CancelledError`` afterward.

    Args:
        process: The spawned child process.
        communicate_task: The still-running ``Process.communicate()`` task.

    Returns:
        True when a racing cancellation was absorbed during cleanup and must
        be re-raised by the caller, False otherwise.
    """
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    absorbed_cancel = False
    while not communicate_task.done():
        try:
            await asyncio.shield(communicate_task)
        except asyncio.CancelledError:
            absorbed_cancel = True
        except Exception:
            # The drain itself failed; the child is already dead and the
            # explicit wait below still reaps it.
            break
    if communicate_task.done() and not communicate_task.cancelled():
        # Mark a finished drain's outcome as retrieved -- a failed one would
        # otherwise surface as "Task exception was never retrieved".
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


class LocalRunnerBackend:
    """Run commands as child processes on the local machine."""

    def __init__(self) -> None:
        self._leases: dict[str, WorkspaceLease] = {}
        self._bindings: dict[str, LocalWorkspaceBinding] = {}
        self._managed: dict[str, _ManagedLease] = {}
        self._failed_installations: dict[str, _ControllerInstallation] = {}

    def capabilities(self) -> RunnerCapabilities:
        """Declare local batch execution with a shared workspace."""
        return RunnerCapabilities(
            batch=True,
            sessions=False,
            shared_workspace=True,
            snapshots=False,
            agent=True,
            retained_workspace=True,
        )

    async def prepare_run(self, run: RunSpec) -> WorkspaceLease:
        """Issue a managed active workspace; idle is restored only after confirmed release."""
        if run.run_id in self._failed_installations:
            raise ExecutionSpecError("Local workspace installation is terminally unconfirmed")
        if run.local_workspace_root is not None:
            if run.workspace_persistence is None or run.bundle is None:
                raise ExecutionSpecError("Managed local workspace requires retention and a bundle")
            if run.run_id in self._leases:
                lease = self._leases[run.run_id]
                state = self._managed.get(run.run_id)
                if state is None or state.run != run or state.uncertain or state.released:
                    raise ExecutionSpecError("Local workspace lease is already issued or closed")
                _ = local_workspace.verify(
                    run, lease, run.bundle, idle=False, previous=state.identity
                )
                return lease
            LocalProcessScopeController.preflight()
            root = local_workspace.expected_root(run)
            lease = WorkspaceLease(run.run_id, "local", str(uuid4()), str(root))
            identity = local_workspace.stage(run, lease, run.bundle)
            return self._install_managed(run, lease, identity, fresh=True)
        if run.workspace_persistence is not None:
            raise ExecutionSpecError("local backend does not support retained workspaces")
        if run.run_id in self._managed:
            raise ExecutionSpecError("Cannot replace a managed lease with a legacy local lease")
        lease = WorkspaceLease(
            lease_id=run.run_id,
            backend="local",
            incarnation=uuid4().hex[:12],
            location=None,
        )
        self._leases[lease.lease_id] = lease
        return lease

    def _install_managed(
        self,
        run: RunSpec,
        lease: WorkspaceLease,
        identity: local_workspace.LocalIdentity,
        *,
        fresh: bool = False,
    ) -> WorkspaceLease:
        """Fence active before allocations and commit gated admission after local assembly."""
        assert run.bundle is not None and run.local_workspace_root is not None
        installation = _ControllerInstallation()
        previous_registries = self._leases, self._managed, self._bindings
        active: local_workspace.LocalIdentity | None = None
        try:
            installation.stage = "active-fence"
            active = local_workspace.set_state(run, lease, run.bundle, identity, "active")
            installation.stage = "preflight"
            controller = LocalProcessScopeController(
                run.local_workspace_root, lease.incarnation, _installation=installation
            )
            installation.stage = "backend-assembly"
            context = ManagedWorkspaceContext(
                run.local_workspace_root, lease.incarnation, controller
            )
            state = _ManagedLease(run, active, controller)
            binding = LocalWorkspaceBinding(
                run.run_id,
                lease.incarnation,
                run.local_workspace_root,
                os.path.join(run.local_workspace_root, "work"),
                run.bundle,
                context,
            )
            leases = self._leases.copy()
            managed = self._managed.copy()
            bindings = self._bindings.copy()
            leases[lease.lease_id] = lease
            managed[lease.lease_id] = state
            bindings[lease.lease_id] = binding
            installation.stage = "registry-commit"
            self._leases, self._managed, self._bindings = leases, managed, bindings
            installation.commit()
        except BaseException as primary:
            registries_restored = True
            try:
                self._leases, self._managed, self._bindings = previous_registries
            except BaseException:
                registries_restored = False
                _installation_warning(
                    primary, "Managed local installation registry rollback unconfirmed."
                )
            outcome = installation.abort(primary)
            if outcome == "unconfirmed" or not registries_restored:
                installation.outcome = "unconfirmed"
                self._failed_installations[lease.lease_id] = installation
                if active is not None:
                    try:
                        local_workspace.set_state(run, lease, run.bundle, active, "uncertain")
                    except BaseException:
                        _installation_warning(
                            primary,
                            "Managed local installation uncertain-marker write failed; workspace "
                            "retained. Identity or attachment fence may have drifted; inspect "
                            "before manual recovery. Do not bypass identity.json.",
                        )
                _installation_warning(
                    primary,
                    f"Managed local installation failed at {installation.stage}; cleanup "
                    "unconfirmed; workspace retained. Inspect owned resources "
                    "before manual recovery.",
                )
            else:
                try:
                    if active is not None:
                        local_workspace.set_state(run, lease, run.bundle, active, "idle")
                    # Only THIS prepare's original stage-issued idle object authorizes deletion.
                    local_workspace.verify(run, lease, run.bundle, previous=identity)
                except BaseException:
                    _installation_warning(
                        primary,
                        "Managed local installation identity recovery unconfirmed; workspace "
                        "retained. Detected replacement or drift is not overwritten or deleted.",
                    )
                else:
                    if fresh:
                        try:
                            local_workspace.delete(run, lease, run.bundle, identity)
                        except BaseException:
                            _installation_warning(
                                primary,
                                "Managed local installation rollback incomplete; workspace "
                                "retained where present. No recursive deletion retry "
                                "will be attempted.",
                            )
                    _installation_warning(
                        primary,
                        f"Managed local installation failed at {installation.stage}; "
                        "cleanup confirmed.",
                    )
            raise
        return lease

    def local_workspace_binding(self, lease: WorkspaceLease) -> LocalWorkspaceBinding | None:
        """Return only this backend's binding after verifying the complete lease."""
        if lease.lease_id in self._failed_installations:
            raise ExecutionSpecError("Local workspace installation is terminally unconfirmed")
        issued = self._leases.get(lease.lease_id)
        if issued != lease or (lease.lease_id in self._managed and issued is not lease):
            raise ExecutionSpecError("local workspace lease is foreign or stale")
        if state := self._managed.get(lease.lease_id):
            assert state.run.bundle is not None
            _ = local_workspace.verify(
                state.run, lease, state.run.bundle, idle=False, previous=state.identity
            )
        return self._bindings.get(lease.lease_id)

    def mark_workspace_in_use(self, lease: WorkspaceLease) -> None:
        """Persist active before dispatch, refusing terminal or uncertain incarnations."""
        binding = self.local_workspace_binding(lease)
        if binding is None:
            return
        state = self._managed[lease.lease_id]
        state.controller.check_admission()
        if state.released or state.uncertain or not state.controller.owners.mutations_allowed(None):
            raise ExecutionSpecError("Managed local workspace is sealed or uncertain")
        state.identity = local_workspace.set_state(
            state.run, lease, binding.bundle, state.identity, "active"
        )

    def _mark_uncertain(self, lease: WorkspaceLease) -> None:
        """Keep uncertainty sticky even when marker drift prevents recording it."""
        state = self._managed[lease.lease_id]
        state.uncertain = True
        assert state.run.bundle is not None
        try:
            state.identity = local_workspace.set_state(
                state.run, lease, state.run.bundle, state.identity, "uncertain"
            )
        except (OSError, ValueError, TypeError) as exc:
            _log.warning(
                "Cannot record uncertain local workspace; leaving it untouched: %s",
                state.controller.scrub(str(exc)),
            )

    def mark_workspace_uncertain(self, lease: WorkspaceLease) -> None:
        """Poison owned cleanup after seal, without authorizing any new admission."""
        if self._leases.get(lease.lease_id) is not lease or lease.lease_id not in self._managed:
            raise ExecutionSpecError("local workspace lease is foreign or stale")
        self._mark_uncertain(lease)

    async def confirm_workspace_released(self, lease: WorkspaceLease) -> None:
        """Accept an existing common root proof; never initiate or retry its barrier."""
        binding = self.local_workspace_binding(lease)
        if binding is None:
            return
        state = self._managed[lease.lease_id]
        if state.uncertain:
            raise ExecutionSpecError("Managed local workspace release is unconfirmed")
        if state.controller.owners.mutations_allowed(None):
            raise ExecutionSpecError("Common root release proof is required before confirming idle")
        proof = await state.controller.owners.wait_terminal()
        if proof.status != "released":
            self._mark_uncertain(lease)
            raise ExecutionSpecError("Managed local workspace release is unconfirmed")
        _ = await state.controller.close()
        if state.uncertain:
            raise ExecutionSpecError("Managed local workspace release became unconfirmed")
        state.identity = local_workspace.set_state(
            state.run, lease, binding.bundle, state.identity, "idle"
        )
        state.released = True

    async def attach_run(
        self, run: RunSpec, identity: WorkspaceIdentity, *, expect_staged: bool = False
    ) -> WorkspaceLease:
        """Verify a ready/idle incarnation, then install it active without restaging."""
        del expect_staged
        if run.local_workspace_root is None or run.bundle is None:
            raise WorkspaceAttachError("local backend does not support retained workspaces")
        if run.workspace_persistence is None:
            raise WorkspaceAttachError("Managed local attach requires retention")
        try:
            root = local_workspace.expected_root(run)
            if identity.location != str(root) or identity.backend != "local":
                raise ExecutionSpecError("Retained local identity does not name the expected root")
            if run.run_id in self._leases:
                raise ExecutionSpecError("Local workspace lease has already been issued")
            if run.run_id in self._failed_installations:
                raise ExecutionSpecError("Local workspace installation is terminally unconfirmed")
            lease = WorkspaceLease(
                identity.lease_id, identity.backend, identity.incarnation, str(root)
            )
            if identity.lease_id != run.run_id:
                raise ExecutionSpecError("Retained local lease does not match this run")
            marker = local_workspace.verify(run, lease, run.bundle)
        except (OSError, ValueError, TypeError) as exc:
            raise WorkspaceAttachError(f"Cannot attach retained local workspace: {exc}") from exc
        LocalProcessScopeController.preflight()
        return self._install_managed(run, lease, marker)

    async def finalize_run(
        self, lease: WorkspaceLease, outcome: RunOutcome, *, retain: bool = False
    ) -> None:
        """Finalize a local run.

        A legacy local lease owns no run-scoped resources, so ``retain`` has
        no effect. A managed lease owns a workspace tree: ``retain=True``
        preserves it. With ``retain=False``, deletion requires a previously
        confirmed release and no uncertainty; the tree's identity and idle
        state are verified before deletion. While release is unconfirmed, the
        lease records remain in place.
        """
        del outcome
        if lease.lease_id in self._failed_installations:
            _log.warning("Leaving local workspace intact: installation cleanup is unconfirmed")
            return
        issued = self._leases.get(lease.lease_id)
        if issued == lease and (lease.lease_id not in self._managed or issued is lease):
            state = self._managed.get(lease.lease_id)
            if state is not None:
                if retain:
                    if not state.released:
                        _log.warning("Retaining local workspace without confirmed root release")
                        return
                elif state.uncertain or not state.released:
                    _log.warning("Leaving local workspace intact: root release is unconfirmed")
                    return
                else:
                    assert state.run.bundle is not None
                    try:
                        local_workspace.delete(state.run, lease, state.run.bundle, state.identity)
                    except (OSError, ValueError, TypeError) as exc:
                        _log.warning(
                            "Leaving unverifiable local workspace intact: %s",
                            state.controller.scrub(str(exc)),
                        )
                        return
                _ = self._managed.pop(lease.lease_id)
            _ = self._leases.pop(lease.lease_id)
            _ = self._bindings.pop(lease.lease_id, None)

    async def run_agent(
        self,
        spec: AgentSpec,
        lease: WorkspaceLease | None,
        *,
        on_event: AgentEventSink | None = None,
        interrupt_signal: asyncio.Event | None = None,
        execute_local: Callable[[], Awaitable[AgentResult]] | None = None,
        on_dispatch: Callable[[], None] | None = None,
    ) -> AgentResult:
        """Delegate an agent invocation to its in-process closure.

        The closure already captures the event and interrupt signals, so this
        backend deliberately does not consume on_event or interrupt_signal
        itself. The returned result is passed through by identity without
        serialization or reconstruction.
        """
        del spec
        if execute_local is None:
            raise ConfigurationError("local agent execution requires execute_local")
        if lease is not None and (lease.location is not None or lease.lease_id in self._managed):
            self.mark_workspace_in_use(lease)
        if on_dispatch is not None:
            on_dispatch()
        return await execute_local()

    async def run_command(
        self,
        spec: CommandSpec,
        lease: WorkspaceLease | None,
        *,
        diagnostics: Callable[[str], None] | None = None,
        on_dispatch: Callable[[], None] | None = None,
    ) -> CommandResult:
        """Run one command locally and return its data-shaped outcome."""
        started_at = time.monotonic()

        # Build environment (merge os.environ + declared overrides).
        # Always set PYTHONUTF8=1 so child Python processes use UTF-8 encoding
        # instead of the system default (cp1252 on Windows), preventing garbled
        # Unicode characters in script output.
        if spec.inherit_control_environment:
            env = {**os.environ, "PYTHONUTF8": "1", **spec.env}
        else:
            env = {"PYTHONUTF8": "1", **spec.env}

        # Resolve bare command names and absolute paths against PATH so that a
        # bare name (e.g. "python") finds the executable the shell would, and a
        # path missing an extension resolves correctly. Resolution uses the
        # subprocess's own ``PATH`` (``env`` may override it via ``spec.env``),
        # so the resolved binary matches the one the child would have executed.
        # Relative paths containing a separator are left untouched so they keep
        # resolving against ``working_dir``. Resolution is non-destructive: when
        # ``which`` cannot resolve the command we fall back to the rendered value.
        resolved_command = spec.command
        has_separator = os.sep in resolved_command or (
            os.altsep is not None and os.altsep in resolved_command
        )
        if (os.path.isabs(resolved_command) or not has_separator) and not (
            sys.platform == "win32"
            and lease is not None
            and (lease.location is not None or lease.lease_id in self._managed)
        ):
            resolved_command = (
                shutil.which(resolved_command, path=env.get("PATH")) or resolved_command
            )

        if diagnostics is not None:
            diagnostics(f"  Script: {resolved_command} {' '.join(spec.args)}")
            if spec.stdin is not None:
                diagnostics(f"  Script stdin: {len(spec.stdin)} bytes")

        if lease is not None and (lease.location is not None or lease.lease_id in self._managed):
            return await self._run_managed(spec, lease, diagnostics, on_dispatch)

        try:
            if on_dispatch is not None:
                on_dispatch()
            process = await asyncio.create_subprocess_exec(
                resolved_command,
                *spec.args,
                stdin=asyncio.subprocess.PIPE if spec.stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=spec.working_dir,
                env=env,
            )
        except FileNotFoundError as exc:
            return CommandResult(
                outcome="command_not_found",
                resolved_command=resolved_command,
                start_error=StartError(
                    kind="file_not_found",
                    message=cast(str, exc.strerror) if exc.errno is not None else str(exc),
                    errno=exc.errno,
                    filename=cast(str | None, exc.filename),
                ),
                duration_seconds=time.monotonic() - started_at,
            )
        except OSError as exc:
            return CommandResult(
                outcome="start_failed",
                resolved_command=resolved_command,
                start_error=StartError(
                    kind="os_error",
                    message=cast(str, exc.strerror) if exc.errno is not None else str(exc),
                    errno=exc.errno,
                    filename=cast(str | None, exc.filename),
                ),
                duration_seconds=time.monotonic() - started_at,
            )

        # ``communicate`` runs as its own task, shielded from the wait_for
        # timeout and from cancellation of this coroutine, so pipe drainage
        # never stops mid-flight: killing a child whose output filled the
        # pipe buffers reaps reliably only while the parent keeps reading to
        # EOF. Cancelling communicate() itself pauses the pipe transports, and
        # a bare ``process.wait()`` after ``kill()`` can then stay blocked even
        # though the child is already dead (observed on the Windows CI fleet
        # with a continuously-writing child, returncode already -9).
        communicate_task = asyncio.create_task(process.communicate(input=spec.stdin))
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                asyncio.shield(communicate_task), timeout=spec.timeout
            )
        except TimeoutError:
            if await _kill_and_reap(process, communicate_task):
                # A run cancellation raced the timeout cleanup; it must not be
                # swallowed into a data-shaped outcome.
                raise asyncio.CancelledError from None
            return CommandResult(
                outcome="timed_out",
                resolved_command=resolved_command,
                duration_seconds=time.monotonic() - started_at,
            )
        except asyncio.CancelledError:
            await _kill_and_reap(process, communicate_task)
            raise

        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")

        if stderr_text and diagnostics is not None:
            diagnostics(f"  Script stderr: {stderr_text.strip()}")

        # ``returncode`` is guaranteed non-None after ``communicate``.
        assert process.returncode is not None
        return CommandResult(
            outcome="completed",
            stdout=stdout_text,
            stderr=stderr_text,
            exit_code=process.returncode,
            resolved_command=resolved_command,
            duration_seconds=time.monotonic() - started_at,
        )

    async def _run_managed(
        self,
        spec: CommandSpec,
        lease: WorkspaceLease,
        diagnostics: Callable[[str], None] | None,
        on_dispatch: Callable[[], None] | None,
    ) -> CommandResult:
        """Run a batch scope and join its proof without sealing the entire root."""
        self.mark_workspace_in_use(lease)
        state = self._managed[lease.lease_id]
        controller = state.controller
        binding = self._bindings[lease.lease_id]
        if spec.working_dir is None:
            spec = replace(spec, working_dir=os.path.join(binding.work_root, binding.bundle.root))
        invocation = uuid4().hex
        cancelled = False
        result: CommandResult | None = None
        try:
            result = await controller.spawn_script(invocation, spec, on_dispatch=on_dispatch)
        except asyncio.CancelledError:
            cancelled = True
        finally:
            controller.begin_teardown(invocation)
            try:
                proof = await controller.await_release(invocation)
            except asyncio.CancelledError:
                cancelled = True
                proof = await controller.owners.wait_terminal(invocation)
            if proof.status != "released":
                self._mark_uncertain(lease)
        if cancelled:
            raise asyncio.CancelledError
        assert result is not None
        if state.uncertain and result.outcome in ("completed", "timed_out"):
            return CommandResult(
                "start_failed",
                resolved_command=result.resolved_command,
                start_error=StartError("os_error", "Managed process release is unconfirmed"),
                duration_seconds=result.duration_seconds,
            )
        if result.stderr and diagnostics is not None:
            diagnostics(f"  Script stderr: {result.stderr.strip()}")
        return result
