# Usability Features

## 1. ~~Logging Redesign (Console + File Output)~~ ✅ Shipped

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

## 2. Interrupt & Resume: User Guidance During Workflow Execution

Allow users to interrupt a running workflow, provide guidance or redirect, and resume execution. Uses an explicit interrupt model (hotkey) rather than passive stdin reading to avoid output interleaving issues and unclear timing.

### User Experience

1. User presses `Esc` (or `Ctrl+G`) during workflow execution
2. Current agent's work is interrupted (mid-agent, not just between agents)
3. A Rich panel displays: current agent, work done so far, partial output
4. User chooses an action:
   - **Continue with guidance** — re-run/resume current agent with text guidance
   - **Skip to next agent** — route to a specific agent from the workflow
   - **Stop workflow** — terminate and return whatever output is available
   - **Cancel interrupt** — resume as if nothing happened
5. Guidance is injected automatically (no `{{ _user_guidance }}` opt-in needed)

### Design

#### Interrupt Signal

- Register a signal handler or use terminal raw mode to detect `Esc` keypress
- Set an `asyncio.Event` (`interrupt_requested`) that the engine checks
- Only activate when stdin is a TTY (`sys.stdin.isatty()`)
- Add `--no-interactive` flag to disable for CI/piped usage
- Display a subtle indicator at workflow start: `Press Esc to interrupt and provide guidance`

#### Level 1: Between-Agent Interrupts

- In the main `while True` loop of `WorkflowEngine.run()`, check `interrupt_requested` after route evaluation and before starting the next agent
- If set, display the interrupt prompt (Rich panel, modeled on `MaxIterationsHandler`)
- User provides guidance → inject into context → resume

#### Level 2: Mid-Agent Interrupts

Both providers support mid-execution interruption:

**Copilot provider** — The Copilot SDK's `CopilotSession.abort()` cancels the current message processing while keeping the session alive. After abort:
1. The session fires `session.idle`
2. The accumulated `assistant.message` content up to the abort is captured as partial output
3. User guidance is collected via Rich prompt
4. A follow-up `session.send()` delivers the guidance to the same session, preserving full conversation context
5. The session continues with awareness of everything done before the abort

**Claude provider** — The agentic tool-use loop in `_execute_agentic_loop()` checks the interrupt flag between tool-use iterations:
1. Flag is checked after each tool call result is appended
2. On interrupt: send one more API call asking Claude to `emit_output` with its best partial result
3. User guidance is collected
4. Re-invoke with guidance added to message history as a user message

#### Guidance Injection

- **Persistence**: Guidance accumulates across interrupts and persists for the remainder of the workflow. If the user interrupts twice ("focus on performance" then "also consider memory usage"), both are carried forward. Guidance is only cleared when the workflow ends or the user explicitly cancels it via the interrupt menu.
- **System prompt append**: Guidance is appended to each agent's system prompt (not replacing it). This preserves the agent's core instructions while layering on user direction. Format: `\n\n[User Guidance]\n<accumulated guidance text>`
- **Mid-agent interrupts (Copilot)**: Guidance is also sent as a follow-up message to the same session, so the model has both conversational context and the guidance
- **Mid-agent interrupts (Claude)**: Guidance added to message history as a user message before re-invoking the API
- **Routing overrides**: User can choose to skip to a different agent, overriding the normal route evaluation

#### Interrupt Handler

Modeled on the existing `MaxIterationsHandler` in `src/conductor/gates/human.py`:
- Rich panel showing current state (agent name, iteration, partial output preview)
- Numbered options for the user to select
- Text input for free-form guidance
- Returns a result struct that the engine uses to decide next steps

### Provider ABC Changes

Add an optional `interrupt_signal` parameter to `AgentProvider.execute()`:

```python
async def execute(
    self,
    agent: AgentDef,
    context: dict[str, Any],
    rendered_prompt: str,
    tools: list[str] | None = None,
    interrupt_signal: asyncio.Event | None = None,  # New
) -> AgentOutput:
```

Providers that don't support mid-execution interruption ignore the parameter. The `AgentOutput` dataclass gets a new optional field:

```python
partial: bool = False  # True if output was produced from an interrupted execution
```

### Key Files

- `src/conductor/engine/workflow.py` — interrupt check in main `run()` loop, between route evaluation and next agent dispatch
- `src/conductor/cli/run.py` — `run_workflow_async()`: start interrupt listener, pass `asyncio.Event` to engine
- `src/conductor/engine/context.py` — `build_for_agent()`: inject `_user_guidance` into system prompt automatically
- `src/conductor/gates/human.py` — new `InterruptHandler` class (modeled on `MaxIterationsHandler`)
- `src/conductor/providers/base.py` — add `interrupt_signal` to `AgentProvider.execute()`, `partial` to `AgentOutput`
- `src/conductor/providers/copilot.py` — `_send_and_wait()`: check interrupt signal, call `session.abort()`, handle follow-up
- `src/conductor/providers/claude.py` — `_execute_agentic_loop()`: check interrupt flag between tool-use iterations
- `src/conductor/cli/app.py` — `--no-interactive` flag

### SDK Capabilities

The Copilot SDK has first-class support for this via:
- `session.abort()` — cancels current message processing, session stays alive for new messages
- `session.send()` — sends follow-up messages to an existing session with full context
- Event types: `ABORT`, `TOOL_EXECUTION_START/COMPLETE/PARTIAL_RESULT`, `ASSISTANT_MESSAGE_DELTA`, `ASSISTANT_TURN_START/END`
- `send_and_wait()` — convenience method that blocks until `session.idle`

The Claude/Anthropic SDK supports this via:
- The agentic loop in `_execute_agentic_loop()` is controlled by Conductor, so interrupt checks between iterations are straightforward
- Partial output collection by forcing an `emit_output` tool call on interrupt

### Implementation Phases

1. **Phase 1**: Between-agent interrupts only (Level 1) — hotkey listener, interrupt handler UI, guidance injection into context
2. **Phase 2**: Mid-agent interrupts for Copilot (Level 2) — `session.abort()` + follow-up pattern
3. **Phase 3**: Mid-agent interrupts for Claude (Level 2) — interrupt flag in agentic loop + forced emit_output

---

## 3. ~~`!file` Tag for External File References~~ ✅ Shipped

Allow any YAML field value to reference an external file using the `!file` custom YAML tag. The tag is resolved during YAML parsing, before env var resolution or Pydantic validation.

### Syntax

```yaml
agents:
  reviewer:
    prompt: !file prompts/review-prompt.md
    tools:
      - !file tools/review-tools.yaml
```

No quotes needed — `!file` is a native YAML tag, not a string convention.

### Design

- Register a custom ruamel.yaml constructor for the `!file` tag on the `ConfigLoader`'s `YAML()` instance
- The constructor receives the scalar value (the path string), resolves it relative to the parent YAML file's directory, reads the file, and returns the content
- If loaded content parses as a YAML dict/list, use the parsed structure; if scalar, use as raw string
- Resolution happens **during YAML parsing**, before `_resolve_env_vars_recursive()` — so `${VAR}` references inside included files are resolved after inclusion
- Nested `!file` tags in included YAML files are supported automatically (ruamel applies constructors recursively)
- Cycle detection via a tracked set of resolved absolute paths passed through the loader
- For `load_string()`, uses `source_path.parent` if provided, otherwise CWD
- Error on missing files with a clear `ConfigurationError` pointing to the referencing location

### Key Files

- `src/conductor/config/loader.py` — register `!file` constructor on the `YAML()` instance in `ConfigLoader.__init__()` (~L105)
- `src/conductor/config/validator.py` — may need awareness of included files for cross-reference validation
- `docs/workflow-syntax.md` — documentation

---

## 4. ~~Script Execution Steps~~ ✅ Shipped

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

1. **~~Logging Redesign~~** — ✅ Shipped
2. **~~`!file` References~~** — ✅ Shipped
3. **~~Script Steps~~** — ✅ Shipped
4. **Interrupt & Resume** — Three-phase rollout:
   - Phase 1: Between-agent interrupts (hotkey + handler UI + guidance injection)
   - Phase 2: Mid-agent interrupts for Copilot (`session.abort()` + follow-up)
   - Phase 3: Mid-agent interrupts for Claude (agentic loop interrupt + forced emit_output)
