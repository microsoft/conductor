The `copilot` provider now enforces per-agent tool allowlists on the SDK
session; previously every agent silently received the CLI's full tool catalog.
Explicit agent tool lists, including `tools: []`, are enforced. When agent tools
are omitted, a non-empty workflow tool list is inherited; an empty workflow list
retains the SDK's default tools. MCP tools named `server__tool` are translated
to the Copilot runtime's `server-tool` name, so allowing one no longer hides it.
