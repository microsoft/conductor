# Conductor (Claude Code plugin)

Bundles the **conductor** skill so Claude Code can validate, run, debug, and author
Conductor multi-agent YAML workflows.

The skill files in this directory are the canonical source. The
`.claude/skills/conductor` path at the repo root is a symlink to here, so
local Claude Code dev sessions and the published plugin share a single source
of truth.

## What this plugin contains

- `skills/conductor/SKILL.md` — model-invoked skill describing the workflow schema,
  CLI commands, execution model, and authoring guidance.
- `skills/conductor/references/` — supporting reference docs for setup, execution,
  authoring, and the YAML schema.

This plugin ships **only markdown** — no executables, hooks, MCP servers, or
custom agents — so trust verification is straightforward: read the markdown.

## Install

Add the marketplace and install the plugin:

```text
/plugin marketplace add microsoft/conductor
/plugin install conductor@conductor
```

## Use the underlying CLI

The skill orchestrates the `conductor` Python CLI. Install it separately following
the [main project README](https://github.com/microsoft/conductor#installation).
