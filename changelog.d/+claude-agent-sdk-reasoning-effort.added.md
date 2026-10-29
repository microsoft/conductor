**`reasoning.effort` on `claude-agent-sdk`**: the provider now forwards the
resolved effort as `ClaudeAgentOptions.effort`, which reaches the `claude`
CLI as `--effort <level>`, and its capability descriptor declares all five
levels instead of `None`. This was a hard `conductor validate` error before,
not a silently ignored field, so a workflow that wanted per-agent effort on
this provider could not be validated at all. The SDK's `EffortLevel` is the
same `low` / `medium` / `high` / `xhigh` / `max` vocabulary as Conductor's,
so nothing is mapped, and no version floor moves: the field exists at the
provider's declared `claude-agent-sdk>=0.2.82` pin. `__init__` also takes
`default_reasoning_effort` now (the factory always had it and dropped it
here), so `runtime.default_reasoning_effort` reaches this provider like every
other one; a per-agent `reasoning.effort` still wins. Leaving both unset is
unchanged behaviour: the flag is omitted and the CLI's own default applies.
