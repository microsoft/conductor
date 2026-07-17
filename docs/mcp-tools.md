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

## Working Directory

You can specify a default directory where stdio-based MCP servers and agent sessions execute. This is useful for workflows interacting with local repositories or specific folders.

The working directory is configured in the workflow `runtime` block or on individual agents:

```yaml
workflow:
  runtime:
    working_dir: "/path/to/default/workspace" # Global default
```

Or on a specific agent:

```yaml
agents:
  - name: code_expert
    working_dir: "/path/to/specific/repo"
```

### Precedence and Resolution

When determining the active working directory, Conductor follows this precedence:

1. **Agent level:** The agent's own `working_dir` configuration.
2. **Runtime level:** The global `workflow.runtime.working_dir` default.
3. **Fallback:** If neither is set, Conductor falls back to the current directory of the parent process (`os.getcwd()`).

Both levels support dynamic values using Jinja2 templates. You can resolve the path at runtime using outputs from previous steps:

```yaml
agents:
  - name: find_repo
    type: set
    value: "/repositories/my-project"

  - name: git_agent
    working_dir: "{{ find_repo.output }}"
    prompt: "List the last commits in the repository."
```

Relative paths in `working_dir` resolve against the parent directory of the workflow YAML file. If the workflow has no path (e.g. constructed dynamically in memory), they resolve against the current process directory. Conductor lexically normalizes the resolved path. A missing target directory causes Conductor to raise an execution error before any provider call.

> ⚠️ **Warning: Working directory is NOT a sandbox**
> Setting the working directory doesn't restrict filesystem access. It only sets the default path where the agent session and stdio MCP subprocesses run. The model can still read or write files outside this directory if it uses absolute paths or parent directory traversals (e.g., `../`). Avoid relying on this setting to sandbox untrusted model execution.

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

## Tool Output Limits

To prevent large tool outputs from consuming the entire context window or causing API errors, Conductor supports limiting the size of individual MCP tool results. This behavior is configured globally under the `workflow.runtime.tool_output` block:

```yaml
workflow:
  runtime:
    tool_output:
      enabled: true          # Default: true
      max_chars: 50000       # Default: 50000 (minimum: 1000)
      spill_to_file: true    # Default: true
      spill_dir: null        # Default: null (resolves to OS temp dir /conductor/tool-output)
```

### Configuration Fields

* **`enabled`** (boolean): Controls whether per-result MCP tool output size limiting is active. Defaults to `true`.
* **`max_chars`** (integer): The maximum number of characters to retain from each individual tool result. Defaults to `50000`. Must be at least `1000`.
* **`spill_to_file`** (boolean): When enabled, the full, raw tool output is saved to disk and a marker containing the file path is appended to the truncated prefix delivered to the model. When disabled, the output is truncated in-place without saving a copy. Defaults to `true`.
* **`spill_dir`** (string or null): Custom directory for spill files. If `null`, it defaults to the process's temporary directory (`<tempfile.gettempdir()>/conductor/tool-output`). Relative paths are resolved against the current working directory of the process. Parent directories are created automatically.

### Important Notes and Constraints

* **Per-Result Cap:** The limit is a **per-result** cap applied to each tool result independently, not a cumulative context window budget. Multiple truncated tool results, combined with the agent's prompt and conversation history, can still exceed the model's context window. Tuning should be done via `max_chars` or `max_agent_iterations` to keep context consumption in check; cumulative context budgeting is out of scope.
* **Spill File Security and Lifecycle:** Spill files contain the **raw** tool output, which may include secrets, API keys, or sensitive data. Conductor doesn't delete these files after execution. They are left in the operating system's temporary directory (or the custom `spill_dir`) and are cleaned up only by ambient OS temp cleanup processes or manual operator action.
* **Multibyte Truncation (Copilot):** For the Copilot provider, `max_chars` is forwarded to the native SDK as bytes. This means multibyte UTF-8 characters (such as CJK characters or emojis) may be truncated earlier than `max_chars` characters.
* **Truncation Event:** The `agent_tool_output_truncated` event is **Claude-only**. Because the Copilot SDK doesn't expose a hook for tool output truncation, this event is never emitted when using the Copilot provider.

### Provider-Specific Behavior

* **Claude Provider:** Truncation is handled conductor-side. If a tool result exceeds `max_chars`, Conductor truncates it and, if `spill_to_file` is true, writes the raw output to a spill file. For standard file-system tools (like `file_reader` or `grep`), Conductor appends a hint instructing the model that it can access the full contents using file-system tools directly.
* **Copilot Provider:** Execution is delegated to the native Copilot SDK's `large_output` capability. If not configured, the SDK uses its default limit of `51200` bytes. The Conductor-configured `max_chars` is mapped to this byte limit.
* **Claude Agent SDK Provider:** Handles tool execution via the native `claude` CLI. Configuration in `runtime.tool_output` is ignored. Truncation and token budgets for tool results are managed by the CLI's native `MAX_MCP_OUTPUT_TOKENS` environment variable.
* **Hermes Provider:** Doesn't support MCP tools. The `runtime.tool_output` configuration is not applicable.

## Provider Support

| Feature | Copilot | Claude | Claude Agent SDK | Hermes |
|---|---|---|---|---|
| stdio servers | ✅ | ✅ | ❌ | ❌ |
| http servers | ✅ | ❌ | ❌ | ❌ |
| sse servers | ✅ | ❌ | ❌ | ❌ |
| Tool filtering | ✅ | ✅ | ❌ | ❌ |
| OAuth auto-auth | ✅ | N/A | ❌ | ❌ |
| env var passing | ⚠️ Bug ([#163](https://github.com/github/copilot-sdk/issues/163)) | ✅ | ❌ | ❌ |
| Tool output limits | ✅ (native SDK) | ✅ (conductor-side) | ✅ (native CLI env var) | N/A |

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
