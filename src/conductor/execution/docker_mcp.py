"""Daemon identity proofs for direct stdio owners and never-started guards."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from conductor.execution.errors import ExecutionSpecError

logger = logging.getLogger(__name__)


def _late_completion[T](task: asyncio.Task[T]) -> None:
    """Retrieve late failures without logging daemon output or changing uncertainty."""
    if not task.cancelled():
        failure = task.exception()
        if failure is not None:
            logger.warning("Docker MCP late owner failed: %s", type(failure).__name__)


class DockerControl(Protocol):
    """The existing CLI boundary, with its operator environment untouched."""

    async def __call__(
        self, argv: Sequence[str], *, timeout: float | None = ...
    ) -> tuple[int, str, str]: ...


async def join_before[T](task: asyncio.Task[T], deadline: float) -> bool:
    """Observe an owned task without cancelling it or renewing the deadline."""
    cancelled = False
    while not task.done():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            task.add_done_callback(_late_completion)
            raise ExecutionSpecError("Docker MCP cleanup deadline expired")
        try:
            done, _pending = await asyncio.wait((task,), timeout=remaining)
            if not done:
                task.add_done_callback(_late_completion)
                raise ExecutionSpecError("Docker MCP cleanup deadline expired")
        except asyncio.CancelledError:
            cancelled = True
    return cancelled


@dataclass(frozen=True, slots=True)
class ContainerMetadata:
    """Parsed daemon identity; raw inspect payloads never leave this boundary."""

    identifier: str
    name: str
    labels: Mapping[str, str]
    status: str


class ContainerOwner:
    """Accumulate immutable allocation proof for exactly one launcher attempt."""

    def __init__(self, cli: DockerControl, name: str, labels: Mapping[str, str]) -> None:
        self.cli = cli
        self.name = name
        self.labels = dict(labels)
        self.identifier: str | None = None
        self.attempted = False
        self.uncertain = False
        self.released = False

    async def inspect(self, reference: str) -> ContainerMetadata | None:
        """Accept only object-specific absence and complete typed metadata."""
        try:
            rc, stdout, stderr = await self.cli(("inspect", "--format", "{{json .}}", reference))
        except OSError:
            raise ExecutionSpecError("Docker MCP container inspection failed") from None
        if rc != 0:
            absence = re.fullmatch(
                r"(?:[Ee]rror response from daemon: |[Ee]rror: )?[Nn]o such "
                r"(?:container|object): " + re.escape(reference),
                stderr.strip(),
            )
            if rc != -124 and absence is not None:
                return None
            raise ExecutionSpecError("Docker MCP container inspection unconfirmed")
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            raise ExecutionSpecError("Docker MCP container metadata malformed") from None
        match data:
            case {
                "Id": str(identifier),
                "Name": str(name),
                "Config": {"Labels": dict(labels)},
                "State": {"Status": str(status)},
            } if re.fullmatch(r"[0-9a-f]{64}", identifier):
                parsed: dict[str, str] = {}
                for key, value in labels.items():
                    match (key, value):
                        case (str(label), str(text)):
                            parsed[label] = text
                        case _:
                            raise ExecutionSpecError("Docker MCP labels malformed")
                return ContainerMetadata(identifier, name, parsed, status)
            case _:
                raise ExecutionSpecError("Docker MCP container metadata unverifiable")

    async def verify(self, *, created: bool = False) -> ContainerMetadata:
        """Remember the first verified ID; replacements can never become owners."""
        try:
            metadata = await self.inspect(self.name)
            if metadata is None:
                raise ExecutionSpecError("Docker MCP allocation is not confirmed")
            self._accept(metadata)
            if created and metadata.status != "created":
                raise ExecutionSpecError("Docker MCP fence is not stopped")
            return metadata
        except ExecutionSpecError:
            self.uncertain = True
            raise

    def _accept(self, metadata: ContainerMetadata) -> None:
        if metadata.name != "/" + self.name or any(
            metadata.labels.get(key) != value for key, value in self.labels.items()
        ):
            raise ExecutionSpecError("Docker MCP ownership mismatch")
        if self.identifier is not None and self.identifier != metadata.identifier:
            raise ExecutionSpecError("Docker MCP container replacement detected")
        self.identifier = metadata.identifier

    async def remove(self, *, deadline: float | None = None) -> None:
        """Remove only a verified immutable ID, never a reusable daemon name."""
        if self.uncertain:
            raise ExecutionSpecError("Docker MCP cleanup remains unconfirmed")
        if self.released:
            return
        try:
            metadata = await self.inspect(self.name)
            if metadata is not None:
                self._accept(metadata)
            elif self.identifier is None:
                if self.attempted:
                    raise ExecutionSpecError("Docker MCP daemon allocation is unconfirmed")
                self.released = True
                return
            else:
                metadata = await self.inspect(self.identifier)
                if metadata is None:
                    self.released = True
                    return
                self._accept(metadata)
            assert self.identifier is not None
            if self.uncertain or (
                deadline is not None and asyncio.get_running_loop().time() >= deadline
            ):
                raise ExecutionSpecError("Docker MCP removal admission expired")
            rc, _stdout, _stderr = await self.cli(("rm", "-f", self.identifier))
            if rc != 0:
                raise ExecutionSpecError("Docker MCP container removal unconfirmed")
            self.released = True
        except (ExecutionSpecError, OSError, asyncio.CancelledError):
            self.uncertain = True
            raise ExecutionSpecError("Docker MCP cleanup unconfirmed") from None
