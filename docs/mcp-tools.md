# MCP Tools

Conductor supports [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) servers, enabling agents to use external tools such as web search, code execution, file operations, and custom API integrations.

MCP servers are configured at the workflow level and made available to all agents. Each agent can optionally filter which tools it uses.

## Quick Start

Add an MCP server to your workflow's `runtime` section:

```yaml
workflow:
  name: research-workflow
  entry_point: researcher
  runtime:
    provider: copilot
    mcp_servers:
      web-search:
        command: npx
        args: ["-y", "open-websearch@latest"]
        tools: ["*"]
```

The agent can now use tools provided by the `web-search` MCP server.

## Server Types

Conductor supports three MCP server transport types:

### stdio (default)

Spawns a local process and communicates over stdin/stdout. This is the most common type.

```yaml
mcp_servers:
  web-search:
    type: stdio          # default, can be omitted
    command: npx
    args: ["-y", "open-websearch@latest"]
    env:
      MODE: stdio
    tools: ["*"]
```

| Field | Required | Description |
|---|---|---|
| `type` | No | `"stdio"` (default) |
| `command` | **Yes** | Command to run (e.g., `npx`, `node`, `python`) |
| `args` | No | Command-line arguments (list of strings) |
| `env` | No | Environment variables for the subprocess |
| `tools` | No | Tool filter list; `["*"]` = all tools (default) |
| `timeout` | No | Timeout in milliseconds |

### http

Connects to a remote MCP server over HTTP with streamable transport.

```yaml
mcp_servers:
  remote-tools:
    type: http
    url: https://mcp.example.com/tools
    headers:
      X-Api-Key: ${API_KEY}
    tools: ["*"]
```

| Field | Required | Description |
|---|---|---|
| `type` | **Yes** | `"http"` |
| `url` | **Yes** | Server URL |
| `headers` | No | HTTP headers (e.g., API keys) |
| `tools` | No | Tool filter list; `["*"]` = all tools (default) |
| `timeout` | No | Timeout in milliseconds |

### sse

Connects to a remote MCP server over Server-Sent Events (SSE).

```yaml
mcp_servers:
  streaming-tools:
    type: sse
    url: https://mcp.example.com/sse
    headers:
      Authorization: Bearer ${TOKEN}
    tools: ["*"]
```

The configuration fields are the same as `http`.

> **Provider note:** The Claude provider only supports `stdio` servers. The `http` and `sse` types are supported by the Copilot provider only.

## Configuration Reference

### Full Schema

```yaml
workflow:
  runtime:
    mcp_servers:
      <server-name>:           # Unique name (used as tool prefix)
        type: stdio | http | sse   # Transport type (default: stdio)
        
        # stdio fields
        command: <string>      # Command to run (required for stdio)
        args: [<string>, ...]  # Command arguments
        env:                   # Environment variables
          KEY: value
        
        # http/sse fields
        url: <string>          # Server URL (required for http/sse)
        headers:               # HTTP headers
          Header-Name: value
        
        # Common fields
        tools: ["*"]           # Tool filter (default: all)
        timeout: <int>         # Timeout in milliseconds
```

### Tool Naming

Tools from MCP servers are prefixed with the server name to avoid collisions:

```
{server-name}__{tool-name}
```

For example, a server named `web-search` providing a tool called `search` becomes `web-search__search`.

### Tool Filtering

You can control which tools are available at two levels:

**Per-server filtering** — limit which tools from a server are exposed:

```yaml
mcp_servers:
  web-search:
    command: npx
    args: ["-y", "open-websearch@latest"]
    tools: ["search"]  # Only expose the "search" tool, not all tools
```

**Per-agent filtering** — limit which tools a specific agent can use:

```yaml
agents:
  - name: researcher
    tools:
      - web-search__search      # Only this MCP tool
    prompt: "Research the topic..."
```

When an agent specifies a `tools` list, only matching MCP tools are included in its requests. If `tools` is omitted or `null`, all available tools are passed.

## Environment Variables

MCP server environment variables support `${VAR}` and `${VAR:-default}` syntax for runtime resolution:

```yaml
mcp_servers:
  my-server:
    command: node
    args: ["server.js"]
    env:
      API_KEY: ${MY_API_KEY}              # Required — fails if not set
      DEBUG: ${DEBUG_MODE:-false}          # Optional — defaults to "false"
      REGION: ${AWS_REGION:-us-east-1}    # Optional — defaults to "us-east-1"
```

Environment variables are resolved at runtime from the current process environment, not at YAML load time. This allows sensitive values like API keys to remain outside the workflow file.

### Passing values from the command line

You can pass configuration to MCP servers at runtime by setting environment variables before running the workflow:

```bash
# Pass an API key to the MCP server
MY_API_KEY=sk-abc123 uv run conductor run workflow.yaml --input.question="test"

# Or export first
export MY_API_KEY=sk-abc123
uv run conductor run workflow.yaml --input.question="test"
```

```yaml
# workflow.yaml
workflow:
  runtime:
    mcp_servers:
      my-server:
        command: node
        args: ["server.js"]
        env:
          API_KEY: ${MY_API_KEY}            # Resolved from OS environment at runtime
          ENDPOINT: ${API_ENDPOINT:-https://api.example.com}
```

On Windows (PowerShell):

```powershell
$env:MY_API_KEY = "sk-abc123"
uv run conductor run workflow.yaml --input.question="test"
```

> **Note:** Workflow inputs (`--input.*`) and MCP server environment variables are separate systems. `--input.*` values are available in Jinja2 templates (agent prompts, routes) as `{{ workflow.input.name }}`, while MCP server `env` values come from OS environment variables via `${VAR}` syntax.

> **Known issue (Copilot provider):** The Copilot SDK has a bug where `env` variables in MCP server configs are not passed to MCP server subprocesses. As a workaround, you can inline env vars into the command using shell syntax:
> ```yaml
> mcp_servers:
>   web-search:
>     command: sh
>     args: ["-c", "MODE=stdio exec npx -y open-websearch@latest"]
> ```
> See [copilot-sdk#163](https://github.com/github/copilot-sdk/issues/163) for status.

## OAuth Authentication (HTTP/SSE)

For HTTP and SSE servers that require OAuth, Conductor can automatically discover OAuth requirements and fetch Azure AD tokens.

**How it works:**

1. Conductor checks `{server-url}/.well-known/oauth-protected-resource/` for OAuth metadata
2. If the server requires OAuth and no `Authorization` header is configured, Conductor extracts the required scope
3. An Azure AD token is fetched using the Azure CLI (`az account get-access-token`)
4. The token is added as a `Bearer` token in the `Authorization` header

**Prerequisites:**
- Azure CLI installed and authenticated (`az login`)
- The MCP server must expose a `.well-known/oauth-protected-resource/` endpoint

**Manual auth:** If you prefer to manage tokens yourself, set the `Authorization` header directly:

```yaml
mcp_servers:
  secure-server:
    type: http
    url: https://mcp.example.com
    headers:
      Authorization: Bearer ${MY_TOKEN}
```

## Provider Support

| Feature | Copilot | Claude |
|---|---|---|
| stdio servers | ✅ | ✅ |
| http servers | ✅ | ❌ |
| sse servers | ✅ | ❌ |
| Tool filtering | ✅ | ✅ |
| OAuth auto-auth | ✅ | N/A |
| env var passing | ⚠️ Bug ([#163](https://github.com/github/copilot-sdk/issues/163)) | ✅ |

### Copilot Provider

The Copilot provider passes MCP server configurations directly to the Copilot SDK, which handles server lifecycle, tool discovery, and tool execution internally. All three transport types (`stdio`, `http`, `sse`) are supported.

### Claude Provider

The Claude provider uses Conductor's built-in `MCPManager` to spawn and manage MCP server connections. It:

- Connects to `stdio` servers only
- Converts MCP tools to Claude's native tool format
- Routes tool calls through the MCP session
- Runs an agentic loop: Claude decides when to call tools, Conductor executes them and returns results

HTTP and SSE servers are not supported with the Claude provider. If configured, a warning is logged and the server is skipped.

## Examples

### Web Search

```yaml
workflow:
  name: web-research
  entry_point: researcher
  runtime:
    provider: copilot
    mcp_servers:
      web-search:
        command: sh
        args: ["-c", "MODE=stdio DEFAULT_SEARCH_ENGINE=bing exec npx -y open-websearch@latest"]
        tools: ["search"]

agents:
  - name: researcher
    prompt: "Search the web for: {{ workflow.input.query }}"
    routes:
      - to: $end
```

### Multiple MCP Servers

```yaml
workflow:
  name: multi-tool
  entry_point: assistant
  runtime:
    provider: copilot
    mcp_servers:
      web-search:
        command: npx
        args: ["-y", "open-websearch@latest"]
        tools: ["*"]
      context7:
        command: npx
        args: ["-y", "@upstash/context7-mcp@latest"]
        tools: ["*"]

agents:
  - name: assistant
    prompt: "Help the user with their request: {{ workflow.input.question }}"
    routes:
      - to: $end
```

### Claude with MCP Tools

```yaml
workflow:
  name: claude-with-tools
  entry_point: researcher
  runtime:
    provider: claude
    default_model: claude-sonnet-4-5
    mcp_servers:
      web-search:
        command: npx
        args: ["-y", "open-websearch@latest"]
        env:
          MODE: stdio

agents:
  - name: researcher
    prompt: "Research the following topic: {{ workflow.input.topic }}"
    routes:
      - to: $end
```

## See Also

- [Workflow Syntax Reference](workflow-syntax.md) — agent `tools` field
- [Configuration Guide](configuration.md) — runtime configuration
- [Provider Comparison](providers/comparison.md) — feature comparison
