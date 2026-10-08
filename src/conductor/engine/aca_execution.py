"""ACA agent placement over the existing dynamic-sessions transport."""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from conductor.config.environment import AcaProfileOptions
from conductor.exceptions import ConfigurationError
from conductor.execution.agent_wire import agent_spec_to_wire_body
from conductor.execution.types import (
    AgentEventSink,
    AgentResult,
    AgentSpec,
    CommandResult,
    CommandSpec,
    RunnerCapabilities,
    RunOutcome,
    RunSpec,
    WorkspaceLease,
)

if TYPE_CHECKING:
    import asyncio


_INVALID_IDENTIFIER = re.compile(r"[^a-z0-9-]+")


class AcaGateway(Protocol):
    """Transport surface shared by the profile backend and legacy facade."""

    _runner_protocol_version: int | None
    _runner_features: tuple[str, ...]

    async def validate_connection(self) -> bool: ...

    async def close(self) -> None: ...

    async def execute_request(
        self,
        body: dict[str, Any],
        identifier: str,
        *,
        interrupt_signal: asyncio.Event | None,
        on_event: AgentEventSink | None,
    ) -> AgentResult: ...


@dataclass
class AcaIdentifierState:
    """A lease generation's reuse keys and live concurrency reservations."""

    salt: str = field(default_factory=lambda: secrets.token_hex(4))
    none_scope_counter: int = 0
    active: dict[str, set[int]] = field(default_factory=dict)

    @staticmethod
    def normalize(raw: str) -> str:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
        normalized = _INVALID_IDENTIFIER.sub("-", raw.lower()).strip("-") or "cond"
        return f"{normalized[:119]}-{digest}"

    def identifier_for(self, name: str, context: dict[str, Any], scope: str) -> str:
        item_key = context.get("_key", context.get("_index"))
        discriminator = context.get("_key")
        if discriminator is None:
            discriminator = context.get("_index")
        key = (
            "workflow"
            if scope == "workflow"
            else (str(item_key) if item_key is not None else name)
            if scope == "item"
            else name
        )
        parts = [f"cond-{self.salt}", key]
        if scope != "item" and discriminator is not None and str(discriminator):
            parts.append(str(discriminator))
        if scope == "none":
            self.none_scope_counter += 1
            parts.append(str(self.none_scope_counter))
        return self.normalize("-".join(parts))

    def acquire(self, logical_id: str) -> tuple[str, int]:
        used = self.active.setdefault(logical_id, set())
        slot = 0
        while slot in used:
            slot += 1
        used.add(slot)
        return (logical_id if slot == 0 else self.normalize(f"{logical_id}-conc{slot}"), slot)

    def release(self, logical_id: str, slot: int) -> None:
        used = self.active.get(logical_id)
        if used is not None:
            used.discard(slot)
            if not used:
                self.active.pop(logical_id)


class AcaRunnerBackend:
    """Run-scoped ACA identifiers with one shared transport implementation."""

    def __init__(
        self,
        options: AcaProfileOptions | None = None,
        *,
        transport: AcaGateway | None = None,
        transport_factory: Callable[[AcaProfileOptions], AcaGateway] | None = None,
    ) -> None:
        self.options = options
        self._transport = transport
        self._transport_factory = transport_factory
        self._leases: dict[str, AcaIdentifierState] = {}
        self._children: list[AcaRunnerBackend] = []
        self._protocol_version = 1
        self._interrupt_available = False

    def bind_profiles(
        self, profiles: Mapping[str, AcaProfileOptions]
    ) -> dict[str, AcaRunnerBackend]:
        """Bind each named pool to an adapter sharing this run's lease state."""
        bound: dict[str, AcaRunnerBackend] = {}
        for index, (name, options) in enumerate(sorted(profiles.items())):
            if index == 0:
                self.options = options
                bound[name] = self
            else:
                child = AcaRunnerBackend(options, transport_factory=self._transport_factory)
                child._leases = self._leases
                self._children.append(child)
                bound[name] = child
        return bound

    def _get_transport(self) -> AcaGateway:
        if self._transport is None:
            if self.options is None or self._transport_factory is None:
                raise ConfigurationError("ACA backend requires an aca profile")
            self._transport = self._transport_factory(self.options)
        return self._transport

    def capabilities(self) -> RunnerCapabilities:
        return RunnerCapabilities(
            batch=False,
            sessions=True,
            shared_workspace=False,
            snapshots=False,
            agent=True,
            interrupt=self._interrupt_available,
        )

    async def prepare_run(self, run: RunSpec) -> WorkspaceLease:
        state = AcaIdentifierState()
        self._leases[state.salt] = state
        return WorkspaceLease(
            run.run_id, "aca", state.salt, self.options.pool_endpoint if self.options else None
        )

    async def run_command(
        self,
        spec: CommandSpec,
        lease: WorkspaceLease | None,
        *,
        diagnostics: Callable[[str], None] | None = None,
    ) -> CommandResult:
        del spec, lease, diagnostics
        raise ConfigurationError("ACA profiles do not support batch commands")

    async def run_agent(
        self,
        spec: AgentSpec,
        lease: WorkspaceLease | None,
        *,
        on_event: AgentEventSink | None = None,
        interrupt_signal: asyncio.Event | None = None,
        execute_local: Callable[[], Awaitable[AgentResult]] | None = None,
        identifier_scope: str | None = None,
    ) -> AgentResult:
        if execute_local is not None:
            raise ConfigurationError("ACA cannot execute a local agent closure")
        if lease is None or lease.incarnation not in self._leases:
            raise ConfigurationError("ACA agent execution requires a prepared run lease")
        if self.options is not None and (spec.skill_directories or spec.custom_agents):
            raise ConfigurationError(
                "ACA execution profiles cannot stage skill or plugin directories; "
                "use a Docker agent profile for staged components"
            )
        transport = self._get_transport()
        if self.options is not None and transport._runner_protocol_version is None:
            await transport.validate_connection()
        self._protocol_version = transport._runner_protocol_version or 1
        self._interrupt_available = (
            self._protocol_version >= 2 and "interrupt" in transport._runner_features
        )
        state = self._leases[lease.incarnation]
        scope = identifier_scope or (self.options.identifier_scope if self.options else "agent")
        if self._protocol_version < 2 and spec.env_overlay:
            raise ConfigurationError(
                "ACA runner protocol v1 cannot deliver agent-scope secrets; "
                "rebuild the runner image"
            )
        logical_id = state.identifier_for(spec.name, dict(spec.context), scope)
        identifier, slot = state.acquire(logical_id)
        try:
            wire_version = self._protocol_version
            if self.options is None and (interrupt_signal is None or not self._interrupt_available):
                wire_version = 1
            body = agent_spec_to_wire_body(spec, protocol_version=wire_version)
            return await transport.execute_request(
                body, identifier, interrupt_signal=interrupt_signal, on_event=on_event
            )
        finally:
            state.release(logical_id, slot)

    async def finalize_run(self, lease: WorkspaceLease, outcome: RunOutcome) -> None:
        del outcome
        self._leases.pop(lease.incarnation, None)
        for child in self._children:
            if child._transport is not None:
                await child._transport.close()
                child._transport = None
        if self._transport is not None:
            await self._transport.close()
            self._transport = None
