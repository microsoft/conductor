# Planned Features

## 1. Logging Redesign (Console + File Output)

Replaces the current `--verbose`/`-V` flag with a cleaner two-dimensional model: console verbosity and file output are independent.

### Console Output

| Level | Flag | Behavior |
|---|---|---|
| **full** (default) | *(none)* | Untruncated prompts, tool args, timing, routing — everything |
| **minimal** | `--quiet` / `-q` | Agent start/complete, routing decisions, timing — no prompt/tool detail |
| **silent** | `--silent` / `-s` | No progress output — only final JSON result on stdout |

### File Output

| Mode | Flag | Behavior |
|---|---|---|
| **none** (default) | *(none)* | No file logging |
| **auto** | `--log-file` / `-l` | Writes to `$TMPDIR/conductor/conductor-<workflow>-<timestamp>.log` |
| **explicit** | `--log-file PATH` / `-l PATH` | Writes to specified path |

File output is **always full/untruncated** regardless of console level. This enables CI usage like `--silent --log-file` for clean stdout with full debug log in a file.

### Removed Flags

- `--verbose` / `-V` — removed entirely (full output is now the default)

### Implementation Notes

- The existing `verbose_mode` and `full_mode` ContextVars in `src/conductor/cli/app.py` still work internally; the new flags just set them differently
- File console uses `no_color=True` for plain text output
- File console bypasses the 500-char truncation in `verbose_log_section()`
- At workflow completion, print the log file path to stderr

### Short Flag Summary

| Flag | Short | Scope |
|---|---|---|
| `--version` | `-v` | global |
| `--quiet` | `-q` | global |
| `--silent` | `-s` | global |
| `--log-file` | `-l` | run command |
| `--provider` | `-p` | run command |
| `--input` | `-i` | run command |
| `--template` | `-t` | init command |
| `--output` | `-o` | init command |

---

## 2. Async Stdin Input During Workflow Execution

Allow users to type guidance into the terminal while a workflow is running. Input is captured asynchronously and injected into context for the next agent.

### Design

- Spawn a background asyncio task that reads stdin lines via `loop.run_in_executor(None, sys.stdin.readline)` into an `asyncio.Queue`
- Between each agent step (after route evaluation, before next agent starts), drain the queue
- Store user input in context under `_user_guidance` key, accessible to agents via `{{ _user_guidance }}`
- Only activate when stdin is a TTY (`sys.stdin.isatty()`)
- Display a subtle indicator at workflow start: "Type to provide guidance at any time"
- Add `--no-interactive` flag to disable for CI/piped usage

### Key Files

- `src/conductor/engine/workflow.py` — queue integration in main `run()` loop (~L519)
- `src/conductor/cli/run.py` — queue creation and stdin reader task in `run_workflow_async()`
- `src/conductor/engine/context.py` — ensure `_user_guidance` included in `build_for_agent()`

---

## 3. `$file` Reference Resolution in YAML

Allow any YAML field value to reference an external file using the `$file: path/to/file` pattern. Resolved during loading before Pydantic validation.

### Syntax

```yaml
agents:
  reviewer:
    prompt: "$file: prompts/review-prompt.md"
    tools:
      - "$file: tools/review-tools.yaml"
```

### Design

- Add `_resolve_file_refs_recursive(data, base_path)` in `src/conductor/config/loader.py`, following the same recursive dict-walking pattern as `_resolve_env_vars_recursive()`
- Runs **after** env var resolution so paths can contain `${VAR}` references
- Paths are relative to the parent YAML file's directory
- If loaded content parses as a YAML dict/list, use the parsed structure; if scalar, use as raw string
- Supports nested `$file` references (files referencing other files)
- Cycle detection via tracked set of resolved absolute paths
- For `load_string()`, uses `source_path.parent` if provided, otherwise CWD

### Key Files

- `src/conductor/config/loader.py` — new resolver function, called at ~L181 after env var resolution
- `src/conductor/config/validator.py` — may need awareness of included files for cross-reference validation
- `docs/workflow-syntax.md` — documentation

---

## 4. Script Execution Steps

Add `type: script` as a new workflow step type that runs shell commands, captures stdout, and stores it in context like agent outputs.

### YAML Syntax

```yaml
agents:
  run-tests:
    type: script
    command: pytest
    args: ["tests/", "--tb=short"]
    env:
      PYTHONPATH: ./src
    working_dir: .
    timeout: 300
    routes:
      - when: "{{ exit_code == 0 }}"
        next: summarize-results
      - next: fix-failures
```

### Design

- Extend `AgentDef.type` to `Literal["agent", "human_gate", "script"]` in `src/conductor/config/schema.py`
- Add fields: `command` (required for scripts), `args`, `env`, `working_dir`, `timeout`
- Model validator: if `type == "script"`, `command` is required, `prompt`/`provider`/`model` are forbidden
- Follow `MCPServerDef` pattern (~L415-L455 in schema.py) for command/args/env structure
- Create `src/conductor/executor/script.py` with `ScriptExecutor` using `asyncio.create_subprocess_exec()`
- Capture stdout as text output (not JSON-parsed)
- `exit_code` exposed in route evaluation context
- Jinja2 template rendering supported in `command` and `args` for context injection

### Key Files

- `src/conductor/config/schema.py` — schema changes
- `src/conductor/executor/script.py` — new file
- `src/conductor/engine/workflow.py` — dispatch logic in main loop (~L728-L735)
- `src/conductor/config/validator.py` — validation for script steps

---

## Implementation Order

1. **Logging Redesign** — smallest diff, foundational for everything else
2. **`$file` References** — isolated to loader, well-scoped
3. **Script Steps** — new executor + schema, moderate scope
4. **Async Stdin** — most experimental, depends on logging being settled
