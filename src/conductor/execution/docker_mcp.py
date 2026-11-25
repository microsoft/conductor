"""Verify the immutable identity returned by Docker create."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Protocol

from conductor.execution.errors import ExecutionSpecError


class DockerControl(Protocol):
    """The existing CLI boundary, with its operator environment untouched."""

    async def __call__(
        self, argv: Sequence[str], *, timeout: float | None = ...
    ) -> tuple[int, str, str]: ...


async def verify_container(
    cli: DockerControl, identifier: str, name: str, labels: Mapping[str, str]
) -> None:
    """Do not accept a successful inspect of a different allocation."""
    rc, output, _stderr = await cli(("inspect", "--format", "{{json .}}", identifier))
    if not rc:
        try:
            metadata = json.loads(output)
        except ValueError:
            raise ExecutionSpecError("Docker MCP container metadata malformed") from None
        match metadata:
            case {
                "Id": str(found),
                "Name": str(actual),
                "Config": {"Labels": dict(tags)},
                "State": {"Status": str()},
            } if (
                found == identifier
                and actual == "/" + name
                and all(tags.get(key) == value for key, value in labels.items())
            ):
                return
    raise ExecutionSpecError("Docker MCP container ownership unconfirmed")
