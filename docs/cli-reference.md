# CLI Reference

Complete command-line reference for Conductor.

## Table of Contents

- [`conductor run`](#conductor-run)
- [`conductor stop`](#conductor-stop)
- [`conductor validate`](#conductor-validate)
- [`conductor registry`](#conductor-registry)

## `conductor run`

Execute a workflow from a YAML file.

```bash
conductor run <workflow.yaml> [OPTIONS]
```

### Options

| Option | Short | Description |
|--------|-------|-------------|
| `--input NAME=VALUE` | `-i` | Workflow input (repeatable) |
| `--input.NAME=VALUE` | | Alternative input syntax |
| `--metadata KEY=VALUE` | `-m` | Workflow metadata (repeatable). Merged on top of YAML `metadata:` and surfaced in the `workflow_started` event. |
| `--workspace-instructions` | | Auto-discover convention files (`AGENTS.md`, `CLAUDE.md`, `.github/copilot-instructions.md`, and `.github/instructions/**/*.instructions.md`) by walking from CWD up to the git root. Concatenated and prepended to every agent's prompt. See [Workspace Instructions](#workspace-instructions) below for details on the `.github/instructions/` directory convention. |
| `--instructions PATH` | | Explicit path to an instructions file (repeatable). Combines with auto-discovered files when both flags are used. |
| `--provider PROVIDER` | `-p` | Override provider (copilot, claude) |
| `--dry-run` | | Show execution plan without running |
| `--skip-gates` | | Auto-select first option at human gates |
| `--quiet` | `-q` | Minimal output (agent lifecycle and routing only) |
| `--silent` | `-s` | No progress output (JSON result only) |
| `--log-file <auto\|PATH>` | `-l` | Write full debug output to a file |
| `--web` | | Start a real-time web dashboard |
| `--web-bg` | | Run in background, print dashboard URL, exit |
| `--web-port PORT` | | Port for web dashboard (0 = auto-select) |
| `--no-interactive` | | Disable Esc-to-interrupt capability |

### Examples

#### Basic Execution

```bash
# Run with a single input
conductor run workflow.yaml --input question="What is AI?"

# Run with multiple inputs
conductor run workflow.yaml -i question="Hello" -i context="Greeting"

# Alternative input syntax
conductor run workflow.yaml --input.question="What is AI?"
```

#### Provider Override

```bash
# Override the workflow's default provider
conductor run workflow.yaml --provider claude

# Use Copilot instead of Claude
conductor run workflow.yaml -p copilot
```

#### Dry Run and Debugging

```bash
# Preview execution plan without running
conductor run workflow.yaml --dry-run

# Quiet output (agent lifecycle only)
conductor run workflow.yaml --quiet --input question="Test"

# Write full debug log to a file
conductor run workflow.yaml --log-file debug.log
```

#### Web Dashboard

```bash
# Start dashboard in foreground (keeps running after workflow completes)
conductor run workflow.yaml --web --input question="Test"

# Start dashboard on a specific port
conductor run workflow.yaml --web --web-port 8080 --input question="Test"

# Background mode: prints URL and exits immediately
conductor run workflow.yaml --web-bg --input question="Test"
# Dashboard auto-shuts down after workflow completes and clients disconnect
```

The `--web` flag starts a real-time browser dashboard showing:
- DAG visualization of the workflow graph with live node state updates
- Agent detail panel with rendered prompt, reasoning, tool calls, and output
- Streaming activity as agents execute (reasoning chunks, tool invocations)

The `--web-bg` flag is a convenience shortcut: it forks a background process running the workflow with the dashboard, prints the URL, and exits the CLI immediately. The background process shuts down automatically after the workflow completes and all browser clients disconnect.

`--web` and `--web-bg` are mutually exclusive.

**`--web-bg` and `human_gate`** — `--web-bg` is incompatible with workflows
that contain `human_gate` agents (unless `--skip-gates` is also passed),
because the detached background process has no stdin to prompt on and the
child would crash with `EOFError` mid-run. Conductor detects this at
load time and aborts before forking, with a message listing the options:

1. Use `--web` (foreground) instead of `--web-bg`
2. Add `--skip-gates` to auto-select the first option at every gate
3. Remove `human_gate` steps from the workflow
4. Wait for CLI gate-resolution support (planned follow-up)

The same check applies to `conductor resume --web-bg`.

Background workflows can be stopped with `conductor stop` (see below) or via the stop button in the web dashboard.

#### Automation Mode

```bash
# Skip human gates (auto-select first option)
conductor run workflow.yaml --skip-gates

# CI/CD pattern: silent console + full file log
conductor run workflow.yaml --silent --log-file auto --skip-gates --input question="Automated test"
```

#### Metadata and Instructions

```bash
# Inject runtime metadata (visible in the workflow_started event)
conductor run twig-sdlc.yaml --metadata work_item_id=1814 --metadata env=staging

# Auto-discover and inject convention instruction files (see "Workspace Instructions" below)
conductor run workflow.yaml --workspace-instructions

# Combine auto-discovery with an explicit extra file
conductor run workflow.yaml --workspace-instructions --instructions ./style-guide.md
```

##### Workspace Instructions

When `--workspace-instructions` is set, conductor walks from the current
working directory up to the git root and discovers four conventions, in this
order:

| Convention | Type | Discovery |
|---|---|---|
| `AGENTS.md` | File | Closest-to-CWD wins |
| `.github/copilot-instructions.md` | File | Closest-to-CWD wins |
| `CLAUDE.md` | File | Closest-to-CWD wins |
| `.github/instructions/**/*.instructions.md` | Directory (recursive) | Closest-to-CWD wins per relative path within the directory |

The directory convention follows GitHub Copilot's documented format
([GitHub docs](https://docs.github.com/en/copilot/customizing-copilot/about-customizing-github-copilot-chat-responses),
[VS Code docs](https://code.visualstudio.com/docs/copilot/customization/custom-instructions)):

- Files must use the double `*.instructions.md` extension.
- A YAML frontmatter block with `applyTo` controls activation:

  ```markdown
  ---
  description: 'Coding conventions for the API layer'
  applyTo: '**'
  ---
  Use four-space indentation.
  ```

- `applyTo: "**"` → loaded as always-on (matches Copilot's "always applied").
- `applyTo: "<other glob>"` → **skipped** (the convention scopes these per-file
  in the chat; conductor has no equivalent per-agent file scoping).
- `applyTo` absent → **skipped** (the convention says these are manual-attach
  only, never auto-applied).

This conservative interpretation matches the documented semantics exactly. To
include unscoped instructions today, use the explicit `--instructions PATH`
flag.

#### Complex Inputs

```bash
# JSON array input
conductor run workflow.yaml --input items='["item1", "item2", "item3"]'

# JSON object input
conductor run workflow.yaml --input config='{"key": "value", "count": 5}'

# Multi-line input (use quotes)
conductor run workflow.yaml --input text="Line 1
Line 2
Line 3"
```

## `conductor stop`

Stop background workflow processes launched with `--web-bg`.

```bash
conductor stop [OPTIONS]
```

### Options

| Option | Description |
|--------|-------------|
| `--port PORT` | Stop the workflow running on this specific port |
| `--all` | Stop all background conductor workflows |

With no options, `conductor stop` lists running background workflows. If exactly one is found, it stops automatically. If multiple are running, it prints the list and asks you to specify `--port`.

### How It Works

When a workflow is launched with `--web-bg`, Conductor writes a PID file to `~/.conductor/runs/` tracking the background process. The `stop` command reads these PID files, sends `SIGTERM` to the process, and cleans up the file. PID files are also automatically cleaned up when a background workflow completes normally.

The web dashboard also has a stop button that cancels the running workflow directly via `POST /api/stop`.

### Examples

```bash
# Stop the only running background workflow
conductor stop

# Stop a specific workflow by port
conductor stop --port 8080

# Stop all running background workflows
conductor stop --all
```

## `conductor validate`

Validate a workflow file without executing it. Checks YAML syntax, schema compliance, cross-references (agent names, routes, parallel groups), and Jinja2 template references throughout the workflow.

```bash
conductor validate <workflow.yaml>
```

### Examples

```bash
# Validate a single workflow
conductor validate my-workflow.yaml

# Validate with full path
conductor validate ./workflows/production/main.yaml

# Validate all examples (using shell expansion)
for f in examples/*.yaml; do conductor validate "$f"; done
```

### Validation Checks

**Errors** (validation fails):
- YAML syntax errors
- Schema compliance (required fields, types)
- Agent name references in routes
- Parallel group agent references
- For-each source references
- Circular dependency detection
- Input/output schema validation
- **Stale agent references in templates** — `{{ old_agent.output.field }}` where `old_agent` doesn't exist
- **Missing workflow input references** — `{{ workflow.input.x }}` where `x` isn't declared in `input:`
- Stale references checked across `prompt`, `system_prompt`, `command`, `args`, `working_dir`, `input_mapping`, parallel-group inputs, and workflow `output:` templates

**Warnings** (validation passes with notes):
- **Undeclared dependencies in explicit mode** — agent prompt references `{{ a.output.val }}` but doesn't declare `a.output` in its `input:` list

## `conductor registry`

Manage workflow registries — named sources (GitHub repos or local directories) for shared workflows.

```bash
conductor registry <subcommand> [OPTIONS]
```

### Subcommands

| Subcommand | Description |
|------------|-------------|
| `list [NAME]` | List configured registries, or list workflows in a specific registry. For GitHub registries, the per-registry listing also prints a "Latest tags:" footer with up to 5 newest tags. |
| `add <NAME> <SOURCE>` | Add a new registry (GitHub `owner/repo` or local path) |
| `remove <NAME>` | Remove a registry |
| `set-default <NAME>` | Set the default registry |
| `update [NAME]` | Refresh the cached index for one or all registries. For GitHub registries, the index is re-fetched via a SHA-pinned raw URL that bypasses Fastly's CDN, so updates always reflect the current state of the registry repo. |
| `show <NAME>` | Show details for a single configured registry: type, source, default status, and (for GitHub registries) a "Latest tags:" footer listing up to 5 newest tags discovered on the registry repo. Use `list <NAME>` to inspect the workflows it contains. |

### Options

| Option | Description |
|--------|-------------|
| `--default` | Mark as the default registry (with `add`) |

### Examples

```bash
# Add a GitHub-hosted registry and set it as default
conductor registry add official myorg/conductor-workflows --default

# Add a local directory registry
conductor registry add local ./my-workflows

# List all configured registries
conductor registry list

# List workflows in a specific registry
conductor registry list official

# Show registry details
conductor registry show official

# Set a different default
conductor registry set-default local

# Update cached registry index
conductor registry update

# Remove a registry
conductor registry remove local
```

### Running Workflows from a Registry

Once a registry is configured, `conductor run` accepts short workflow names
of the form `<workflow>[@<registry>][#<ref>]`. `@` selects the registry;
`#` selects a git ref (tag, branch, or commit SHA). Quote the reference in
shell commands so `#` isn't treated as a comment.

```bash
# Run from default registry (default-branch HEAD)
conductor run qa-bot

# Run from a specific registry (latest)
conductor run qa-bot@official

# Pin a specific tag
conductor run 'qa-bot@official#v1.2.3'

# Pin the default-branch HEAD or any other branch
conductor run 'qa-bot@official#main'

# Pin a specific commit SHA
conductor run 'qa-bot@official#a1b2c3d'

# Pin a tag in the default registry (empty registry segment)
conductor run 'qa-bot@#v1.2.3'
```

Path-type registries do not support `#<ref>` and will reject any reference
that includes one.

See [design/registry.md](./design/registry.md) for the full design.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | API key for Claude provider |
| `GITHUB_TOKEN` | Token for Copilot provider (if not using GitHub CLI auth) |
| `CONDUCTOR_LOG_LEVEL` | Logging level: DEBUG, INFO, WARNING, ERROR |

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Workflow execution error |
| 2 | Validation error |
| 3 | Configuration error |
| 130 | User interrupt (Ctrl+C) |

## See Also

- [Workflow Syntax Reference](./workflow-syntax.md) - Complete YAML syntax
- [Examples](../examples/) - Example workflows
- [Providers](./providers/) - Provider-specific documentation
