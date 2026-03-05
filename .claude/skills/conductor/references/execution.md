# Workflow Execution Guide

Complete reference for running, validating, and debugging Conductor workflows.

## CLI Commands

### conductor run

Execute a workflow:

```bash
conductor run <workflow.yaml> [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--input`, `-i NAME=VALUE` | Workflow input (repeatable) |
| `--input.NAME=VALUE` | Alternative input syntax |
| `--provider`, `-p PROVIDER` | Override provider (copilot, claude) |
| `--dry-run` | Show execution plan only |
| `--skip-gates` | Auto-select first option at human gates |
| `--web` | Start real-time web dashboard |
| `--web-bg` | Run in background, print dashboard URL, exit |
| `--web-port PORT` | Port for web dashboard (0 = auto) |
| `--no-interactive` | Disable Esc-to-interrupt capability |
| `--log-file`, `-l PATH` | Write full debug output to file (`auto` for auto-generated) |

**Global options** (before the subcommand):

| Option | Description |
|--------|-------------|
| `--quiet`, `-q` | Minimal output: agent lifecycle and routing only |
| `--silent`, `-s` | No progress output. Only JSON result on stdout |
| `--version`, `-v` | Show version and exit |

> **Note:** Full output is shown by default (prompts, tool calls, reasoning). Use `-q` for minimal output or `-s` for JSON-only. `--quiet` and `--silent` are mutually exclusive.

**Examples:**

```bash
# Standard run (full output by default)
conductor run workflow.yaml --input question="Hello"

# Quiet mode (lifecycle + routing only)
conductor -q run workflow.yaml --input question="Hello"

# Silent mode (JSON result only, no progress)
conductor -s run workflow.yaml --input question="Hello"

# Log full debug output to auto-generated file
conductor run workflow.yaml --log-file auto

# Silent terminal + full file logging
conductor -s run workflow.yaml --log-file auto

# Multiple inputs
conductor run workflow.yaml -i topic="AI" -i depth="detailed"

# Skip human gates for automation
conductor run workflow.yaml --skip-gates

# Dry run to preview execution plan
conductor run workflow.yaml --dry-run

# Override provider
conductor run workflow.yaml -p claude

# Start real-time web dashboard
conductor run workflow.yaml --web --input question="Hello"

# Background mode: prints URL and exits immediately
conductor run workflow.yaml --web-bg --input question="Hello"
```

The `--web` flag opens a browser dashboard with a DAG visualization showing live agent status, streaming reasoning/tool calls, and an agent detail panel. The `--web-bg` flag forks a background process and exits immediately. `--web` and `--web-bg` are mutually exclusive.

Background workflows can be stopped with `conductor stop` (see below) or via the stop button in the web dashboard.

### conductor stop

Stop background workflow processes launched with `--web-bg`:

```bash
conductor stop [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--port PORT` | Stop the workflow running on this specific port |
| `--all` | Stop all background conductor workflows |

With no options, lists running workflows and auto-stops if exactly one is found.

**Examples:**

```bash
# Stop the only running background workflow
conductor stop

# Stop a specific workflow by port
conductor stop --port 8080

# Stop all running background workflows
conductor stop --all
```

### conductor update

Check for and install the latest version of Conductor:

```bash
conductor update
```

The command:
1. Fetches the latest release from the GitHub Releases API
2. Compares the remote version with the locally installed version
3. If a newer version is available, runs `uv tool install --force git+https://github.com/microsoft/conductor.git@v{version}` to upgrade
4. Clears the update-check cache on success so the next invocation re-checks cleanly

If already up to date, prints a confirmation message and exits.

**Examples:**

```bash
# Check for updates and install if available
conductor update
```

### conductor resume

Resume a workflow from a checkpoint after failure:

```bash
conductor resume <workflow.yaml> [OPTIONS]
conductor resume --from <checkpoint.json> [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--from PATH` | Resume from a specific checkpoint file |
| `--skip-gates` | Auto-select first option at human gates |
| `--log-file`, `-l PATH` | Write debug output to file |
| `--no-interactive` | Disable Esc-to-interrupt |

When a workflow fails, Conductor automatically saves a checkpoint to `$TMPDIR/conductor/checkpoints/`. The checkpoint contains all prior agent outputs and workflow state, enabling seamless resumption from the failed agent.

**Examples:**

```bash
# Resume the latest checkpoint for a workflow
conductor resume workflow.yaml

# Resume from a specific checkpoint file
conductor resume --from /tmp/conductor/checkpoints/my-workflow-20260303-153000.json

# Resume with log file
conductor resume workflow.yaml --log-file auto
```

**Behavior:**
- If the workflow file has changed since the checkpoint was saved, a warning is displayed
- Execution resumes from the exact agent that failed
- All prior agent outputs are restored from the checkpoint

### conductor checkpoints

List available workflow checkpoints:

```bash
conductor checkpoints [workflow.yaml]
```

Shows all checkpoint files with metadata: workflow name, timestamp, failed agent, and error type. Optionally filter by workflow file.

**Examples:**

```bash
# List all checkpoints
conductor checkpoints

# List checkpoints for a specific workflow
conductor checkpoints workflow.yaml
```

### conductor validate

Validate without executing:

```bash
conductor validate <workflow.yaml>
```

Checks:
- YAML syntax
- Required fields and schema structure
- Agent references and route targets
- Route reachability
- Template syntax
- Parallel group agent references
- For-each source format and reserved names

### conductor init

Create workflow from template:

```bash
conductor init <name> [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--template`, `-t TEMPLATE` | Template to use (default: simple) |
| `--output`, `-o PATH` | Output file path |

**Examples:**

```bash
conductor init my-workflow
conductor init my-workflow --template loop
conductor init review -t human-gate -o ./workflows/review.yaml
```

### conductor templates

List available templates:

```bash
conductor templates
```

| Template | Description |
|----------|-------------|
| `simple` | Single agent with basic I/O |
| `loop` | Iterative refinement pattern |
| `human-gate` | Workflow with approval gate |

## Execution Flow

1. **Load** — Parse YAML and validate structure
2. **Initialize** — Set up provider(s) and MCP servers
3. **Execute** — Run agents following routes:
   - Sequential agents execute one at a time
   - Parallel groups execute agents concurrently with context snapshots
   - For-each groups spawn N agent instances from runtime array
4. **Collect** — Gather outputs per schema
5. **Return** — Output final result as JSON

### Iteration Counting

- Each agent execution counts as 1 iteration
- Parallel agents count individually (3 parallel agents = 3 iterations)
- For-each instances each count as 1 iteration
- Loop-back patterns increment the counter on each cycle

## Cost Tracking

Conductor tracks token usage and costs automatically:

```yaml
cost:
  show_per_agent: true    # Per-agent cost breakdown
  show_summary: true      # Total cost summary at end
  pricing:                # Override default pricing
    custom-model:
      input_per_mtok: 3.0
      output_per_mtok: 15.0
      cache_read_per_mtok: 0.3
      cache_write_per_mtok: 3.75
```

Output includes input/output token counts and estimated costs per agent and in total.

## Debugging

### Default Output

Full output is shown by default:

```bash
conductor run workflow.yaml --input question="test"
```

Shows:
- Agent execution order
- Full prompt content (untruncated)
- Output received
- Route decisions
- Tool call arguments and reasoning
- Token usage and costs per agent

Use `--quiet` for minimal output (lifecycle + routing only) or `--silent` for JSON-only.

### Log File

```bash
conductor run workflow.yaml --log-file auto
conductor -s run workflow.yaml --log-file debug.log
```

Capture full debug output to a file. Combine with `--silent` for quiet terminal with full logging. Auto mode generates files in `$TMPDIR/conductor/`.

### Dry Run

```bash
conductor run workflow.yaml --dry-run
```

Preview execution plan without running agents. Shows the workflow graph, agent order, and configuration.

### Web Dashboard

```bash
conductor run workflow.yaml --web --input question="test"
```

Visualize execution in real-time with a browser dashboard. Shows agent prompts, reasoning, tool calls, and outputs as they stream in.

### Validate First

```bash
conductor validate workflow.yaml
```

Catch configuration errors before execution. Reports agent count, parallel groups, for-each groups, human gates, and more.

### Check Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Workflow error |
| 2 | Validation error |
| 3 | Timeout |
| 4 | Max iterations exceeded |

## Common Errors

### "Missing required input"

```
Error: Missing required input: question
```

**Fix:** Provide all required inputs:
```bash
conductor run workflow.yaml --input question="value"
```

### "Unknown agent: X"

```
Error: Route references unknown agent: reviewer
```

**Fix:** Check agent/group name spelling matches in routes.

### "Unreachable agent"

```
Error: Agent 'helper' is not reachable from entry_point
```

**Fix:** Add route to the agent or remove if unused.

### "Max iterations exceeded"

```
Error: Workflow exceeded max_iterations (10)
```

**Fix:** Increase limit or fix loop condition:
```yaml
limits:
  max_iterations: 50  # Max: 500
```

### "Timeout"

```
Error: Workflow timed out after 600 seconds
```

**Fix:** Increase timeout:
```yaml
limits:
  timeout_seconds: 1200
```

### Template Errors

```
Error: Undefined variable 'agent_name' in template
```

**Fix:** Check variable exists or use conditional:
```jinja2
{% if agent_name is defined %}
{{ agent_name.output.field }}
{% endif %}
```

### "Parallel groups must contain at least 2 agents"

**Fix:** Add at least 2 agents to the parallel group.

### "Invalid source format" (for-each)

**Fix:** Use dotted path with 3+ parts: `agent_name.output.field`

### "Loop variable conflicts with reserved name"

**Fix:** Choose a different `as` name. Reserved: `workflow`, `context`, `output`, `_index`, `_key`

## Human Gates

When workflow reaches a human gate:

1. **Display** — Shows prompt and options in terminal
2. **Wait** — Pauses for user selection
3. **Capture** — Records selected value and optional text input (prompt_for)
4. **Route** — Continues to the route specified on the selected option

### Skip Gates for Automation

```bash
conductor run workflow.yaml --skip-gates
```

Auto-selects the first option at each gate.

## Provider Configuration

### Override Provider

```bash
conductor run workflow.yaml -p claude       # Use Claude for all agents
conductor run workflow.yaml -p copilot      # Use Copilot (default)
```

### Per-Agent Provider Override

Set `provider` on individual agents in YAML for multi-provider workflows:

```yaml
agents:
  - name: fast_task
    provider: claude
    model: claude-haiku-4.5
  - name: complex_task
    # Uses workflow default provider
    model: gpt-5.2
```

## Output Handling

Workflow output is JSON:

```json
{
  "answer": "Python is a programming language...",
  "confidence": 0.95
}
```

### Capture Output

```bash
# Save to file
conductor run workflow.yaml --input q="test" > output.json

# Parse with jq
conductor run workflow.yaml --input q="test" | jq '.answer'
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `GITHUB_TOKEN` | GitHub Copilot authentication |
| `ANTHROPIC_API_KEY` | Claude provider API key |
| `CONDUCTOR_LOG_LEVEL` | Logging level (DEBUG, INFO, WARNING, ERROR) |

Environment variables in YAML configs support `${VAR}` and `${VAR:-default}` interpolation syntax.

## Performance Tips

1. **Use appropriate models** — Smaller models (Haiku) for simple tasks, larger (Sonnet/Opus) for complex reasoning
2. **Use `explicit` context mode** — Reduces token usage by only passing declared inputs
3. **Set timeouts** — Prevent runaway workflows with `limits.timeout_seconds`
4. **Use parallel groups** — Run independent agents concurrently
5. **Use for-each groups** — Process arrays in parallel with `max_concurrent` batching
6. **Set `max_tokens`** — Limit output length to save costs (especially with Claude)
7. **Use per-agent provider** — Pick the best model/provider for each task

## Debugging Checklist

1. [ ] Run `conductor validate workflow.yaml`
2. [ ] Check all agent/group names match between definition and routes
3. [ ] Verify entry_point exists as an agent, parallel group, or for-each group
4. [ ] Ensure all paths lead to `$end`
5. [ ] Test with `--dry-run` first
6. [ ] Use `-V` to trace execution with full details
7. [ ] Check template variables are defined before use
8. [ ] Verify for-each `source` resolves to an array
9. [ ] Check parallel groups have 2+ agents
10. [ ] Review cost output for unexpected token usage

## Interactive Interrupt

During execution, press **Esc** or **Ctrl+G** to pause the workflow. An interactive menu appears with these actions:

| Action | Description |
|--------|-------------|
| **Continue with guidance** | Provide text guidance that is appended to subsequent agent prompts |
| **Skip to agent** | Jump to a specific agent in the workflow |
| **Stop** | Stop the workflow entirely |
| **Cancel** | Resume execution as-is |

Guidance text accumulates across multiple interrupts and is injected into agent context.

Disable with `--no-interactive`. In `--skip-gates` mode, interrupts auto-cancel.

## Checkpoint & Resume

When a workflow fails, Conductor automatically saves a checkpoint containing:
- All completed agent outputs
- Current workflow state and iteration count
- Workflow file hash (to detect changes)
- Failure details (agent, error type, message)

Checkpoints are stored in `$TMPDIR/conductor/checkpoints/`.

```bash
# List available checkpoints
conductor checkpoints

# Resume from latest checkpoint for a workflow
conductor resume workflow.yaml

# Resume from a specific checkpoint file
conductor resume --from /tmp/conductor/checkpoints/my-workflow-20260303-153000.json
```

If the workflow file has changed since the checkpoint was saved, a warning is displayed but resumption proceeds.
