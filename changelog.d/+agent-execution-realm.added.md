**Agent execution realms and profiles**: run LLM agents in isolated Docker
containers or Azure Container Apps dynamic sessions via `execution.profile`.
Docker profiles accept `runner_image` to execute the agent loop, stdio MCP
tools, and staged workspace dependencies colocated in a long-lived runner
container. Remote runners support `/interrupt` for targeted mid-turn pauses,
multi-provider execution (Copilot, OpenAI, Claude), and per-call agent-scope
secret delivery (`env_overlay`) to stdio MCP processes without leaking into
the runner process environment.

**ACA execution profiles**: configure the pool and session scope under an
environment profile's `aca` block, independently of the agent's model
provider. The legacy `provider: aca` form remains available with a
deprecation notice. The obsolete `conductor.providers.aca_protocol` import
has been removed; import wire models from `conductor.runner.protocol`.
