"""``conductor mcp serve`` — expose Conductor workflows as MCP tools.

Deliberately re-exports **nothing** eagerly. ``conductor/cli/app.py`` builds
its full Typer app (and therefore imports every ``cli/*.py`` sub-app module,
including the future ``cli/mcp.py``) on every ``conductor`` invocation, so
importing the ``mcp`` SDK — which several modules in this package do — must
stay confined to the code path that actually runs ``conductor mcp serve``.
An eager re-export here would defeat that by making ``import
conductor.mcp.serve`` itself pull in ``mcp.types``.
"""

from __future__ import annotations
