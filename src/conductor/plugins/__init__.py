"""Plugin support — the plugin as the unit of opt-in.

A plugin is a directory shipping any combination of three things
Conductor can use:

.. code-block:: text

    <plugin>/
      .claude-plugin/plugin.json   or  .github/plugin/plugin.json
      skills/<skill>/SKILL.md
      agents/<agent>.agent.md
      .mcp.json

Conductor **deconstructs** a plugin rather than handing its root to a
provider SDK. Both native SDKs offer a whole-plugin surface (Copilot's
``plugin_directories``, claude-agent-sdk's ``plugins``), and both are
all-or-nothing: on Copilot, ``excluded_tools`` hides an MCP tool from the
model but does not stop the server subprocess from launching with the
user's credentials, so a per-component "off" switch built on it would be
a guarantee that isn't one. Registering the root also puts the providers
in opposition — plugin MCP is unavoidable on Copilot and suppressed on
claude-agent-sdk, which Conductor runs with ``strict_mcp_config=True``.

Deconstructed, each component travels the surface Conductor already uses
for it: skills through ``skill_directories`` / :mod:`conductor.skills`,
agents through the SDK's custom-agent option, and MCP servers through the
same session-level channel a workflow-declared server uses — so a
plugin's server gets the same ``runtime.tool_output`` limits and
dashboard tool events, and goes through the same credential and
environment resolution (:func:`conductor.mcp_auth.resolve_mcp_servers`).

A plugin server is *not* a :class:`~conductor.config.schema.MCPServerDef`
and therefore carries no per-server ``tools:`` filter — there is nowhere
to author one. ``mcp: false`` on the plugin entry is the available
control.

Plugins are **never discovered**. An entry is always written in
``plugins:``, so nothing enters a run unasked and a missing plugin is a
hard error rather than silently less capability.

Layering note
-------------
This package deliberately re-exports nothing. :mod:`conductor.skills.registry`
imports :mod:`conductor.plugins.manifest` (to share one definition of what a
plugin root looks like), while :mod:`conductor.plugins.registry` imports
:mod:`conductor.skills.registry` (to share one definition of what a skill
directory looks like). An eager re-export here would close that loop at
import time, so callers import the submodule they need directly.
"""

from __future__ import annotations

__all__: list[str] = []
