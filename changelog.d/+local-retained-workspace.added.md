Local workflows can opt into retained shared workspaces backed by a mutable
copy of their selected run bundle. Supported local scripts, agents, and MCP
consumers share the copy, and resume verifies its identity and runtime release
before reusing it.
