**Agent execution realms**: explicit `tools: []` now survives the
host-to-runner boundary, so Docker and ACA agents no longer receive the inner
provider's default MCP catalog. Protocol v1 runners reject this configuration
instead of silently widening tool access.

Host upgrades require rebuilt Docker and ACA runner images with matching
Conductor code. Although the protocol remains v2, current hosts send declared
`agent.tools` on every v2 request (including `null`), and older strict runner
images reject that field with HTTP 422, even when the workflow does not use an
empty allowlist.
