The `copilot` provider now enforces the per-agent `tools:` allowlist on the
SDK session. Previously `tools: []`, a named list, or an inherited
workflow-level `tools:` list were not passed to the Copilot SDK, so every
agent silently received the CLI's full tool catalog (shell, file read/write,
web, and every configured MCP server). Workflows that declare no tools at
either level keep the provider's default catalog.
