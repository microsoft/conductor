"""Regression tests for the `mcp` SDK version bound (DD0).

The existing MCP *client* (`conductor.mcp.manager`) reads the camelCase
`tool.inputSchema` attribute (`mcp/manager.py:207`), which `mcp` 2.0.0
renamed to `input_schema`. A lock refresh that silently pulls `mcp` 2.x
would raise `AttributeError` on every server connection — an error the
`except ImportError` guard in `manager.py` cannot catch, since the import
itself still succeeds under 2.x.

These tests assert against the *attributes and importability* the bound
protects, not the version string, so a future incompatible SDK release
fails here for the reason that actually matters (E1-T3), and separately
assert the declared `pyproject.toml` specifier itself excludes `2.0.0`
(E1-T4), so a future widening of the bound fails here rather than
surfacing in a user's lockfile.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

_PYPROJECT_PATH = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _mcp_requirement() -> Requirement:
    """Return the parsed `mcp` requirement declared in `pyproject.toml`."""
    data = tomllib.loads(_PYPROJECT_PATH.read_text())
    dependencies = data["project"]["dependencies"]
    for dep in dependencies:
        req = Requirement(dep)
        if req.name == "mcp":
            return req
    raise AssertionError("mcp is not declared in [project].dependencies")


def test_mcp_types_tool_exposes_camel_case_input_schema() -> None:
    """`mcp.types.Tool` must expose `inputSchema`, the attribute
    `mcp/manager.py:207` reads. `mcp` 2.0.0 renamed this to `input_schema`,
    which would raise `AttributeError` on every server connection.
    """
    from mcp.types import Tool

    tool = Tool(name="example", description="An example tool", inputSchema={"type": "object"})

    # Accessing the camelCase attribute must not raise.
    assert tool.inputSchema == {"type": "object"}


def test_mcp_types_call_tool_result_exposes_is_error() -> None:
    """`mcp.types.CallToolResult` must expose `isError` (camelCase), the
    same naming convention the SDK uses elsewhere and that 2.x is known to
    have reshaped.
    """
    from mcp.types import CallToolResult, TextContent

    result = CallToolResult(content=[TextContent(type="text", text="ok")], isError=False)

    assert result.isError is False


def test_mcp_server_fastmcp_imports() -> None:
    """`mcp.server.fastmcp` must remain importable under the pinned SDK."""
    import mcp.server.fastmcp  # noqa: F401


def test_mcp_server_lowlevel_server_imports() -> None:
    """`mcp.server.lowlevel.Server` must remain importable under the pinned
    SDK; this is the low-level server class `conductor mcp serve` will wire
    the catalogue onto.
    """
    from mcp.server.lowlevel import Server  # noqa: F401

    assert Server is not None


def test_pyproject_declares_mcp_lower_bound() -> None:
    """The declared specifier must keep the known-good floor (1.28.1)."""
    req = _mcp_requirement()
    assert req.specifier.contains(Version("1.28.1"), prereleases=True)


def test_pyproject_mcp_bound_excludes_2x() -> None:
    """The declared specifier must exclude `mcp` 2.0.0 and later.

    This is the regression the bound exists to prevent: a future
    widening of the `mcp` dependency specifier (e.g. dropping the upper
    bound during a routine version bump) must fail here rather than
    silently letting a lock refresh pull an incompatible major version.
    """
    req = _mcp_requirement()
    assert not req.specifier.contains(Version("2.0.0"), prereleases=True)
    assert not req.specifier.contains(Version("2.5.0"), prereleases=True)
