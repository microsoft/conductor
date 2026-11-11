**`reasoning.effort` on `claude-agent-sdk`**: the provider now forwards the
resolved effort as `ClaudeAgentOptions.effort`, which reaches the `claude`
CLI as `--effort <level>`, and its capability descriptor declares all five
levels instead of `None`. This was a hard `conductor validate` error before,
so a workflow that wanted per-agent effort on this provider could not be
validated at all. `runtime.default_reasoning_effort` now reaches this
provider like every other one; a per-agent `reasoning.effort` still wins.
Because the CLI silently drops effort on models without it and runs an
unsupported `xhigh` or `max` at `high`, the provider refuses those
combinations for explicit model ids (for example `xhigh` on the default
`claude-sonnet-4-6`). A set level is also pinned in
`CLAUDE_CODE_EFFORT_LEVEL`, which the CLI ranks above `--effort`, so an
inherited value or a settings tier cannot replace it. Leaving effort unset
is unchanged behaviour: nothing is sent and the CLI's own default applies.
