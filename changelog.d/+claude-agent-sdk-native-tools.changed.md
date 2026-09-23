**Breaking (experimental `claude-agent-sdk` provider):** an agent that omits
`tools:` no longer receives the full Claude Code tool preset. The new
`runtime.provider.native_tools` setting defaults to `none` — no built-in
filesystem, shell, web or editing tools — including for the bare
`provider: claude-agent-sdk` shorthand. Workflows that relied on omitting
`tools:` to read files, run commands or edit code must now opt in explicitly
with `native_tools: claude_code`; without it they still validate, but their
agents run with no built-in tools. `claude_code` grants the full preset with
automatic approval, and the run warns once that `working_dir` is not a
sandbox. Under `none`, declared MCP servers stay usable through one
server-scoped permission rule each (`mcp__<server>__*`). Server names are
restricted to letters, digits, `-` and single `_` characters so the rule
matches the tool names the CLI actually generates; a name that cannot form a
matching rule, and plugin subagents that would have no dispatch tool, are
refused at `conductor validate` and at run time. An
explicit `tools: []` on an agent that would still get MCP servers, from the
workflow or a plugin, is now refused by `conductor run` as well as by
`conductor validate`, instead of running with those servers attached.
