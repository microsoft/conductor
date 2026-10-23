**`runtime.provider.system_prompt_preset` on `claude-agent-sdk`**: set to
`claude_code`, every session starts from the Claude Code CLI's own system
prompt (tool guidance, coding and safety instructions) instead of the SDK's
minimal one, and an agent's `system_prompt:` is appended to it via the
preset's `append` rather than replacing it. The per-machine part of the
preset moves into the first user message (`exclude_dynamic_sections`), so
all agents of a workflow share one cacheable system prompt. Unset is
unchanged behaviour: an empty `--system-prompt`, replaced by an agent's own.
