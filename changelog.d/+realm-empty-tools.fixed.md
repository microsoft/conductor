**Agent execution realms**: explicit `tools: []` now survives the
host-to-runner boundary, so Docker and ACA agents no longer receive the inner
provider's default MCP catalog. Protocol v1 runners reject this configuration
instead of silently widening tool access.
