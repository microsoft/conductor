# Interrupt & Resume: User Guidance During Workflow Execution

> **Revision:** 2 — Addressing technical review feedback
> **Status:** In Progress
> **Feature ref:** usability-features.brainstorm.md — Feature #2

---

## 1. Problem Statement

Conductor workflows execute agents sequentially (or in parallel groups) without any mechanism for user intervention once started. If a user notices an agent heading in the wrong direction, producing poor output, or needing additional context, they must wait for the workflow to complete — or resort to Ctrl+C, which destroys all progress.

This feature adds an **explicit interrupt model** that allows users to:
- Pause execution at well-defined points (between agents, or mid-agent)
- Review current state (agent name, iteration, partial output)
- Provide free-form guidance that is injected into subsequent execution
- Skip agents, redirect routing, or gracefully stop the workflow

The interrupt model uses a hotkey (Esc / Ctrl+G) rather than passive stdin reading to avoid output interleaving and unclear timing.

---

## 2. Goals and Non-Goals

### Goals
- **G1:** Users can interrupt a running workflow via Esc or Ctrl+G hotkey
- **G2:** Between-agent interrupts: engine pauses before starting the next agent
- **G3:** Mid-agent interrupts (Copilot): abort current processing, collect guidance, resume session
- **G4:** Mid-agent interrupts (Claude): interrupt between agentic loop iterations
- **G5:** User-provided guidance accumulates and persists for the remainder of the workflow
- **G6:** Rich terminal UI displays current state and offers structured choices
- **G7:** Interrupt is only available in TTY mode; disabled for CI/piped usage
- **G8:** `--no-interactive` flag disables interrupt capability entirely

### Non-Goals
- **NG1:** Passive stdin monitoring — we use explicit hotkey detection only
- **NG2:** Interrupt support during parallel group execution (parallel agents run concurrently; interrupting individual parallel agents is out of scope for this design)
- **NG3:** Interrupt support during for-each group execution (same reasoning as parallel)
- **NG4:** Persisting guidance across workflow restarts (checkpoint resume) — may be added later
- **NG5:** Web/API-based interrupt (this is a TTY-only feature)
- **NG6:** Modifying the YAML schema — guidance injection is entirely runtime behavior

---

## 3. Requirements

### Functional Requirements

| ID | Requirement |
|----|-------------|
| FR1 | Pressing Esc or Ctrl+G sets an `asyncio.Event` signaling an interrupt request |
| FR2 | The engine checks the interrupt event after route evaluation, before starting the next agent |
| FR3 | On interrupt, a Rich panel displays: current agent name, iteration count, last agent output preview, and available actions |
| FR4 | Available actions: (1) Continue with guidance, (2) Skip to a named agent, (3) Stop workflow, (4) Cancel interrupt |
| FR5 | User guidance is appended to each subsequent agent's rendered prompt as a `[User Guidance]` section |
| FR6 | Multiple interrupts accumulate guidance (all entries shown, newest last) |
| FR7 | Mid-agent interrupt for Copilot cancels in-flight processing, captures partial output, collects guidance, sends follow-up in the same session |
| FR8 | Mid-agent interrupt for Claude checks the interrupt flag between agentic loop iterations; on interrupt, sends one final API call with a user message requesting `emit_output` tool use, then collects guidance |
| FR9 | "Skip to agent" validates the target agent exists as a top-level agent in the workflow (excluding agents nested inside parallel/for-each groups) |
| FR10 | A subtle indicator is displayed at workflow start: `Press Esc to interrupt and provide guidance` |
| FR11 | Ctrl+C behavior is unchanged — it still triggers `KeyboardInterrupt`, saves a checkpoint, and stops the workflow immediately |

### Non-Functional Requirements

| ID | Requirement |
|----|-------------|
| NFR1 | Interrupt detection latency < 100ms from keypress to event set |
| NFR2 | Keyboard listener must not interfere with Rich console output or provider logging |
| NFR3 | Keyboard listener must restore terminal state on exit (no leaked raw mode) |
| NFR4 | Interrupt handling adds < 5ms overhead per loop iteration when no interrupt is pending |
| NFR5 | All interrupt code paths are covered by unit tests with mock TTY |
| NFR6 | Feature is fully backward-compatible — existing workflows run identically without `--no-interactive` |

---

## 4. Solution Architecture

### 4.1 Overview

The solution introduces four new components and modifies five existing ones:

```
+--------------------------------------------------------------+
|  CLI Layer (cli/app.py, cli/run.py)                          |
|  - --no-interactive flag                                     |
|  - Start/stop KeyboardListener                               |
|  - Pass interrupt_event to engine                            |
+----------------+---------------------------------------------+
                 | asyncio.Event (interrupt_requested)
+----------------v---------------------------------------------+
|  Engine Layer (engine/workflow.py)                            |
|  - Check interrupt_event in _execute_loop() between agents   |
|  - Delegate to InterruptHandler for UI + guidance collection |
|  - Apply InterruptResult: inject guidance, skip, or stop     |
|  - Pass interrupt_event to providers via execute()           |
+----------------+--------------------+------------------------+
                 |                    |
+----------------v--------+ +--------v-------------------------+
|  InterruptHandler       | |  Providers                        |
|  (gates/interrupt.py)   | |  (providers/copilot.py,           |
|  - Rich panel UI        | |   providers/claude.py)            |
|  - Action selection     | |  - Accept interrupt_signal param  |
|  - Guidance text input  | |  - Copilot: session abort flow    |
|  - Returns result       | |  - Claude: check between iters   |
+-------------------------+ +----------------------------------+
```

### 4.2 Component Details

#### 4.2.1 KeyboardListener (`src/conductor/interrupt/listener.py`)

A lightweight async task that puts the terminal into cbreak mode and listens for Esc (0x1b) or Ctrl+G (0x07). When detected, it sets an `asyncio.Event`.

**Design decisions:**
- Uses `tty.setcbreak()` (not full raw mode) to avoid breaking Rich output
- Only activates when `sys.stdin.isatty()` is True and `--no-interactive` is not set
- Restores terminal settings via `termios.tcsetattr()` in a `finally` block, plus `atexit` and `signal.SIGTERM` handlers
- On non-Unix platforms (Windows), falls back to `msvcrt.kbhit()` / `msvcrt.getch()`

**Esc key disambiguation (critical):**
The Esc key (0x1b) is also the first byte of ANSI escape sequences (e.g., arrow keys send `0x1b 0x5b 0x41`). The listener must disambiguate a bare Esc press from the start of an escape sequence using a read-ahead timeout:

1. On receiving 0x1b, start a short timer (50ms)
2. If no additional bytes arrive within 50ms, treat as bare Esc and set interrupt event
3. If additional bytes arrive (e.g., 0x5b), this is an escape sequence (arrow key, etc.) — consume the full sequence and discard it

This is the standard approach used by libraries like `curses`, `blessed`, and `prompt_toolkit`.

**Thread safety for asyncio.Event:**
The listener uses `loop.run_in_executor()` for blocking stdin reads (necessary in cbreak mode). Since `asyncio.Event.set()` is not thread-safe, the executor callback must use `loop.call_soon_threadsafe(event.set)` to safely signal the event from the executor thread back to the event loop thread.

```python
@dataclass
class KeyboardListener:
    interrupt_event: asyncio.Event
    _original_settings: Any = field(default=None, repr=False)
    _task: asyncio.Task | None = field(default=None, repr=False)
    _stop: bool = field(default=False, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)

    async def start(self) -> None:
        """Enter cbreak mode and begin listening.
        Stores event loop reference for thread-safe signaling."""
        self._loop = asyncio.get_running_loop()
        ...

    async def stop(self) -> None: ...

    async def _listen_loop(self) -> None:
        """Read bytes via run_in_executor. On 0x1b, wait 50ms for
        follow-up bytes to disambiguate Esc from escape sequences.
        Uses loop.call_soon_threadsafe(event.set) for safe signaling."""
        ...

    def _read_byte_blocking(self) -> bytes:
        """Blocking single-byte read for use in executor."""
        ...
```

#### 4.2.2 InterruptHandler (`src/conductor/gates/interrupt.py`)

Modeled on `MaxIterationsHandler`. Displays a Rich panel with workflow state and collects user decisions.

```python
@dataclass
class InterruptResult:
    action: InterruptAction  # continue_with_guidance | skip_to_agent | stop | cancel
    guidance: str | None = None
    skip_target: str | None = None

class InterruptAction(str, Enum):
    CONTINUE = "continue_with_guidance"
    SKIP = "skip_to_agent"
    STOP = "stop"
    CANCEL = "cancel"

class InterruptHandler:
    def __init__(self, console: Console | None = None, skip_gates: bool = False) -> None: ...

    async def handle_interrupt(
        self,
        current_agent: str,
        iteration: int,
        last_output_preview: str | None,
        available_agents: list[str],
        accumulated_guidance: list[str],
    ) -> InterruptResult: ...
```

**Rich panel layout:**
```
+------ Workflow Interrupted ------+
| Current Agent: summarizer        |
| Iteration: 3/10                  |
| Last Output Preview:             |
|   {"summary": "Python is..."}   |
|                                  |
| Previous Guidance:               |
|   1. Focus on Python 3 only     |
|                                  |
| Actions:                         |
|   [1] Continue with guidance     |
|   [2] Skip to agent...          |
|   [3] Stop workflow              |
|   [4] Cancel (resume as-is)     |
+----------------------------------+
```

**"Skip to agent" scope:** The `available_agents` list includes only top-level agents defined in the workflow's `agents:` section. Agents that exist only within parallel group or for-each group definitions are excluded — routing into the middle of a group is not supported. If the user provides an invalid agent name, the handler re-prompts.

#### 4.2.3 Guidance Injection (`src/conductor/engine/context.py`)

Guidance is stored as a `list[str]` in `WorkflowContext`. The executor appends a formatted guidance section to the rendered user prompt before calling the provider.

**Injection point rationale:** Guidance is appended to the **rendered user prompt** (not the system prompt) because:
1. The system prompt is set by the workflow author and should remain stable
2. User guidance is conversational in nature — it is contextual direction for this specific run
3. For mid-agent interrupts (Copilot), guidance is sent as a follow-up user message anyway, so consistency favors user-prompt injection
4. Agents with strict output schemas: the guidance section is appended *before* the JSON schema instruction block (which is appended by the Copilot provider in `_execute_sdk_call()`), so the schema enforcement instruction remains the final directive

```python
# Addition to WorkflowContext
@dataclass
class WorkflowContext:
    # ... existing fields ...
    user_guidance: list[str] = field(default_factory=list)

    def add_guidance(self, text: str) -> None:
        self.user_guidance.append(text)

    def get_guidance_prompt_section(self) -> str | None:
        if not self.user_guidance:
            return None
        entries = "\n".join(f"- {g}" for g in self.user_guidance)
        return (
            "\n\n[User Guidance]\n"
            "The following guidance was provided by the user during workflow execution. "
            "Incorporate this guidance into your response:\n"
            f"{entries}"
        )
```

#### 4.2.4 Engine Integration (`src/conductor/engine/workflow.py`)

The `_execute_loop()` method gains an interrupt check point. The `WorkflowEngine` accepts an optional `interrupt_event` parameter.

**Between-agent interrupt (Phase 1):**
```python
# After route evaluation, before next iteration:
if self._interrupt_event and self._interrupt_event.is_set():
    self._interrupt_event.clear()
    result = await self._interrupt_handler.handle_interrupt(...)
    match result.action:
        case InterruptAction.CONTINUE:
            self.context.add_guidance(result.guidance)
        case InterruptAction.SKIP:
            current_agent_name = result.skip_target
        case InterruptAction.STOP:
            raise InterruptError(...)
        case InterruptAction.CANCEL:
            pass  # continue normally
```

**Ctrl+C interaction:** The existing `KeyboardInterrupt` handler in `_execute_loop()` is unchanged. Ctrl+C continues to immediately save a checkpoint and re-raise. The Esc interrupt is a *cooperative* mechanism — it does not pre-empt the currently running agent (in Phase 1), it only takes effect at the next check point between agents. This is deliberately different from Ctrl+C.

**Mid-agent interrupt (Phases 2 and 3):**
The `interrupt_event` is passed through to `AgentExecutor.execute()` then to `AgentProvider.execute()` as an optional `interrupt_signal` parameter. Each provider handles it internally (see sections 4.2.5 and 4.2.6).

#### 4.2.5 Provider ABC Changes (`src/conductor/providers/base.py`)

```python
@dataclass
class AgentOutput:
    # ... existing fields ...
    partial: bool = False
    """True if output was truncated due to interrupt."""

class AgentProvider(ABC):
    @abstractmethod
    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None = None,
        interrupt_signal: asyncio.Event | None = None,  # NEW
    ) -> AgentOutput: ...
```

**Breaking change inventory:** Adding `interrupt_signal` to the abstract `execute()` method requires updating all concrete implementations and test mocks. The parameter has a default value of `None`, so existing *callers* are unaffected, but classes that *implement* the ABC must update their signatures. Known locations requiring update:

| Location | Type | Update Required |
|----------|------|-----------------|
| `src/conductor/providers/copilot.py` CopilotProvider.execute() | Concrete class | Add parameter (Phase 2) |
| `src/conductor/providers/claude.py` ClaudeProvider.execute() | Concrete class | Add parameter (Phase 2) |
| `src/conductor/cli/run.py` _MockProvider.execute() | Mock in dry-run | Add parameter |
| `tests/test_providers/test_registry.py` MockProvider.execute() | Test mock | Add parameter |
| `tests/test_integration/test_mixed_providers.py` MockProvider.execute() | Test mock | Add parameter |
| Various test files using `AsyncMock()` assignments | Dynamic mocks | No change needed (uses `*args, **kwargs`) |

#### 4.2.6 Copilot Mid-Agent Interrupt — Session Lifecycle (Phase 2)

**Critical design issue:** The current `CopilotProvider._execute_sdk_call()` always destroys the session in its `finally` block (`await session.destroy()`, line 509 of copilot.py). Calling `provider.execute()` again after an abort would create an entirely new session, losing conversation context. The Phase 2 design must address this.

**session.abort() availability:** The Python Copilot SDK README (as of 2026-02) does **not** document a `session.abort()` method. The documented session methods are: `send()`, `destroy()`, `on()`, `get_messages()`. However, the underlying JSON-RPC protocol likely supports a `session.abort` call (the Ruby SDK documents this). **Before implementing Phase 2**, we must:
1. Check if the Python SDK session object has an `abort()` method at runtime via `hasattr(session, 'abort')`
2. If absent, attempt a raw JSON-RPC call via the client's internal RPC mechanism
3. If neither works, Phase 2 falls back to between-agent interrupt behavior for Copilot

**Note:** The Copilot SDK is explicitly labeled as "Technical Preview" and "may change in breaking ways." Phase 2 must be resilient to SDK API changes.

**Proposed session lifecycle for abort flow:**
Rather than modifying `_execute_sdk_call()` to keep sessions alive (which would be a large refactor), we introduce a new internal method `_execute_with_interrupt()` that:
1. Creates the session
2. Sends the prompt via `_send_and_wait()` with interrupt monitoring
3. If interrupted: calls abort, captures partial output, does **not** destroy session
4. Returns both the partial `AgentOutput` and a session handle
5. The engine collects guidance, then calls a new method `provider.send_followup(session_handle, guidance)` to continue in the same session
6. After the follow-up completes (or if no interrupt occurred), the session is destroyed

This avoids changing the existing `execute()` flow for non-interrupt cases.

**Post-abort event behavior (empirically unverified):** After calling `abort()`, the SDK may fire `session.idle`, an `error` event, or something else. The implementation must handle all cases:
- If `session.idle` fires: normal completion, capture partial `response_content`
- If `error` fires: log warning, treat accumulated `response_content` as partial
- If neither fires within 5s: timeout, treat accumulated content as partial

#### 4.2.7 Claude Mid-Agent Interrupt (Phase 3)

In `_execute_agentic_loop()`, check `interrupt_signal.is_set()` at the top of each iteration. On interrupt:
1. Send one more API call with a **user message** (not system message) asking Claude to call the `emit_output` tool with its best partial result. `emit_output` is a tool_use request, not a system instruction — Claude must be prompted to invoke the tool.
2. Parse the `emit_output` tool call response as partial output
3. Return `AgentOutput(partial=True)` — partial output is **not** validated against the agent's output schema (it may be incomplete)

**Re-invocation:** After the user provides guidance, the engine re-invokes `execute()` with the guidance appended to the rendered prompt as additional context. The Claude provider starts a fresh API conversation (the agentic loop does not preserve state across `execute()` calls). The guidance plus original prompt provides sufficient context for continuation.

### 4.3 Data Flow

#### Between-Agent Interrupt Flow (Phase 1)
```
User presses Esc
  -> KeyboardListener disambiguates Esc vs escape sequence (50ms timeout)
  -> Bare Esc confirmed -> loop.call_soon_threadsafe(interrupt_event.set)
  -> _execute_loop() checks interrupt_event after route evaluation
  -> interrupt_event is set -> clear it
  -> InterruptHandler.handle_interrupt() displays Rich panel
  -> User selects action + provides guidance
  -> InterruptResult returned to engine
  -> Engine applies: inject guidance / skip / stop / cancel
  -> Loop continues (or exits)
```

#### Mid-Agent Interrupt Flow — Copilot (Phase 2)
```
User presses Esc during agent execution
  -> KeyboardListener sets interrupt_event
  -> CopilotProvider._send_and_wait() detects interrupt_signal.is_set()
  -> Calls session.abort() (or raw RPC fallback)
  -> Waits for session.idle / error / timeout (5s max)
  -> Partial response_content captured in AgentOutput(partial=True)
  -> Session is NOT destroyed; handle returned to engine
  -> Engine detects partial output -> invokes InterruptHandler
  -> User provides guidance
  -> Engine calls provider.send_followup(session_handle, guidance)
  -> Follow-up send() preserves full conversation context
  -> Session destroyed after follow-up completes
```

#### Mid-Agent Interrupt Flow — Claude (Phase 3)
```
User presses Esc during agent execution
  -> KeyboardListener sets interrupt_event
  -> ClaudeProvider._execute_agentic_loop() detects interrupt_signal at top of iteration
  -> Sends one more API call: user message asking Claude to call emit_output
  -> Parses emit_output tool_use response as partial output
  -> Returns AgentOutput(partial=True) with whatever was produced
  -> Control returns to _execute_loop()
  -> Engine detects partial output -> invokes InterruptHandler
  -> User provides guidance
  -> Engine re-invokes execute() with guidance appended to rendered prompt
```

### 4.4 API Contracts

#### InterruptHandler

```python
async def handle_interrupt(
    self,
    current_agent: str,
    iteration: int,
    last_output_preview: str | None,
    available_agents: list[str],
    accumulated_guidance: list[str],
) -> InterruptResult
```

#### WorkflowEngine (modified constructor)

```python
def __init__(
    self,
    config: WorkflowConfig,
    provider: AgentProvider | None = None,
    registry: ProviderRegistry | None = None,
    skip_gates: bool = False,
    workflow_path: Path | None = None,
    interrupt_event: asyncio.Event | None = None,  # NEW
) -> None
```

#### AgentExecutor (modified execute)

```python
async def execute(
    self,
    agent: AgentDef,
    context: dict[str, Any],
    interrupt_signal: asyncio.Event | None = None,  # NEW
    guidance_section: str | None = None,  # NEW
) -> AgentOutput
```

#### CopilotProvider (new method for Phase 2)

```python
async def send_followup(
    self,
    session_handle: Any,
    guidance: str,
) -> AgentOutput:
    """Send follow-up guidance to an existing session after abort.
    Destroys the session after completion."""
    ...
```

---

## 5. Dependencies

### External Dependencies
| Dependency | Purpose | Already in project? | Notes |
|------------|---------|---------------------|-------|
| `rich` | Terminal UI (panels, prompts) | Yes | |
| `asyncio` | Event signaling, task management | Yes (stdlib) | |
| `termios` / `tty` | Terminal cbreak mode (Unix) | Yes (stdlib) | |
| `msvcrt` | Windows keyboard input | Yes (stdlib, Windows) | |
| `github-copilot-sdk` | Session abort for mid-agent interrupt | Yes (`>=0.1.0`) | **Technical Preview** — API may change. `abort()` not documented in Python SDK README; must verify at runtime. |
| `anthropic` | Claude API for mid-agent interrupt | Yes | |

### Internal Dependencies
| Component | Depends On |
|-----------|-----------|
| `KeyboardListener` | `asyncio.Event`, `tty`/`termios` (Unix), `msvcrt` (Windows) |
| `InterruptHandler` | `rich.console.Console`, `rich.panel.Panel`, `rich.prompt.Prompt` |
| `WorkflowEngine` (interrupt) | `InterruptHandler`, `KeyboardListener` (via event) |
| `AgentExecutor` (guidance) | `WorkflowContext.get_guidance_prompt_section()` |
| `CopilotProvider` (abort) | Copilot SDK session (abort via method or raw RPC) |
| `ClaudeProvider` (interrupt) | `_execute_agentic_loop()` iteration check |

---

## 6. Risk Assessment

| # | Risk | Likelihood | Impact | Mitigation |
|---|------|-----------|--------|------------|
| R1 | Terminal cbreak mode leaks on crash, leaving terminal broken | Medium | High | Use `atexit` handler + `try/finally` in listener + `signal.SIGTERM` handler. Document `stty sane` / `reset` command as fallback. |
| R2 | `session.abort()` not available in Python Copilot SDK | High | High | **Must verify at runtime** via `hasattr()` before Phase 2. Fallback: attempt raw JSON-RPC `session.abort` call. If neither works, fall back to between-agent interrupt for Copilot. The SDK is labeled "Technical Preview" and its API may change. |
| R3 | Cbreak mode interferes with Rich console output | Low | Medium | Use `tty.setcbreak()` (not raw mode) which preserves output processing. Test Rich panel rendering alongside listener. |
| R4 | Race condition: interrupt fires exactly as agent completes | Medium | Low | Clear interrupt event at each check point. If agent already completed, interrupt is handled before next agent (effectively between-agent). |
| R5 | Windows terminal compatibility | Medium | Medium | Implement Windows fallback with `msvcrt`. Test in CI with Windows runner. Phase 1 can ship Unix-only if needed. |
| R6 | Guidance injection bloats prompt for long-running workflows | Low | Medium | Cap accumulated guidance at 10 entries. Show warning if cap reached. Allow user to clear/replace guidance at interrupt. |
| R7 | Claude mid-agent interrupt: final emit_output call may fail or hallucinate | Medium | Low | Treat partial output as best-effort. Mark `partial: True` on AgentOutput. Do not validate partial output against schema. |
| R8 | Interrupt during parallel/for-each group has undefined behavior | Low | Medium | Explicitly skip interrupt checks during parallel/for-each execution. Queue the interrupt for after the group completes. |
| R9 | False positive interrupts from arrow keys / function keys | High | Medium | Esc key disambiguation via 50ms read-ahead timeout. If follow-up bytes arrive within 50ms, the keypress is an escape sequence (arrow key, etc.) and is discarded. This is the standard technique used by `curses` and `prompt_toolkit`. |
| R10 | `asyncio.Event.set()` called from non-event-loop thread | High | High | `KeyboardListener` uses `loop.run_in_executor()` for blocking reads. Must use `loop.call_soon_threadsafe(event.set)` — never call `event.set()` directly from the executor thread. |
| R11 | Guidance injection confuses agents with strict output schemas | Low | Medium | Guidance is appended to the rendered user prompt *before* the JSON schema instruction block (which CopilotProvider appends last in `_execute_sdk_call()`). The schema enforcement instruction remains the final directive. For agents with output schemas, the guidance section includes an explicit note: "The output schema requirements still apply." |
| R12 | Post-abort Copilot session behavior is empirically unverified | Medium | Medium | Implementation must handle all post-abort event outcomes: `session.idle`, `error` event, or no event (5s timeout). Test with real SDK during Phase 2 development. |
| R13 | Copilot session lifecycle mismatch for abort-then-resume | High | High | Current `_execute_sdk_call()` always destroys session in `finally`. Phase 2 introduces `_execute_with_interrupt()` that conditionally skips destruction on abort, plus `send_followup()` to continue and then destroy. This avoids modifying the existing `execute()` flow. |

---

## 7. Implementation Phases

### Phase 1: Between-Agent Interrupts
**Scope:** Keyboard listener, interrupt handler UI, guidance injection, engine integration, CLI flag.

**Exit Criteria:**
- User can press Esc during workflow execution (between agents) and see the interrupt panel
- Esc is correctly disambiguated from ANSI escape sequences (no false positives on arrow keys)
- User can provide guidance that affects subsequent agents
- User can skip to a different agent or stop the workflow
- `--no-interactive` disables the feature
- Non-TTY environments gracefully skip interrupt setup
- Ctrl+C behavior is unchanged (immediate stop + checkpoint)
- All code paths have unit test coverage

### Phase 2: Mid-Agent Interrupts — Copilot
**Scope:** Verify SDK abort availability, pass `interrupt_signal` to Copilot provider, implement abort flow with session continuity, handle partial output + follow-up message.

**Prerequisite validation:** Before starting Phase 2 implementation, empirically verify:
1. Whether `session.abort()` exists on the Python SDK session object
2. What events fire after `abort()` is called
3. Whether `session.send()` works after `abort()` (session continuity)
4. If `abort()` is unavailable, whether raw RPC `session.abort` works

**Exit Criteria:**
- Pressing Esc during Copilot agent execution aborts the current processing
- Partial output is captured and displayed in the interrupt panel
- User guidance is sent as follow-up message in the same session
- Session context is preserved across abort/resume
- Falls back to between-agent interrupt if abort is unavailable
- All provider implementations and test mocks updated for new ABC signature

### Phase 3: Mid-Agent Interrupts — Claude
**Scope:** Check `interrupt_signal` in `_execute_agentic_loop()`, send final emit_output request via user message, handle partial output.

**Exit Criteria:**
- Pressing Esc during Claude agentic loop interrupts between tool-use iterations
- One final API call requests partial output via `emit_output` tool (user message, not system)
- User guidance is appended to rendered prompt for re-invocation
- Partial output is not schema-validated
- All tests pass

---

## 8. Files Affected

### New Files

| File Path | Purpose |
|-----------|---------|
| `src/conductor/interrupt/__init__.py` | Package init for interrupt module |
| `src/conductor/interrupt/listener.py` | `KeyboardListener` — async terminal keypress detection with Esc disambiguation |
| `src/conductor/gates/interrupt.py` | `InterruptHandler`, `InterruptResult`, `InterruptAction` — Rich UI for interrupt interaction |
| `tests/test_interrupt/__init__.py` | Test package init |
| `tests/test_interrupt/test_listener.py` | Tests for `KeyboardListener` including Esc disambiguation |
| `tests/test_interrupt/test_handler.py` | Tests for `InterruptHandler` |
| `tests/test_engine/test_workflow_interrupt.py` | Integration tests for interrupt in workflow engine |
| `tests/test_executor/test_agent_guidance.py` | Tests for guidance injection in executor |
| `tests/test_providers/test_copilot_interrupt.py` | Tests for Copilot mid-agent interrupt |
| `tests/test_providers/test_claude_interrupt.py` | Tests for Claude mid-agent interrupt |

### Modified Files

| File Path | Changes |
|-----------|---------|
| `src/conductor/cli/app.py` | Add `--no-interactive` flag to `run` and `resume` commands |
| `src/conductor/cli/run.py` | Create `KeyboardListener` + `asyncio.Event`, pass to `WorkflowEngine`, start/stop listener, display Esc hint. Update `_MockProvider.execute()` signature for ABC compatibility. Also set up listener in `resume_workflow_async()`. |
| `src/conductor/engine/workflow.py` | Accept `interrupt_event` param; add interrupt check in `_execute_loop()` after route evaluation; handle `InterruptResult`; pass `interrupt_signal` to executor; queue interrupts during parallel/for-each |
| `src/conductor/engine/context.py` | Add `user_guidance: list[str]` field, `add_guidance()`, `get_guidance_prompt_section()`, update `to_dict()`/`from_dict()` with backward-compatible deserialization (`data.get("user_guidance", [])`) |
| `src/conductor/executor/agent.py` | Accept `interrupt_signal` and `guidance_section` params; append guidance to rendered prompt *before* provider call; pass `interrupt_signal` to provider |
| `src/conductor/providers/base.py` | Add `partial: bool = False` field to `AgentOutput`; add `interrupt_signal: asyncio.Event | None = None` param to `AgentProvider.execute()` |
| `src/conductor/providers/copilot.py` | Accept `interrupt_signal` in `execute()`; new `_execute_with_interrupt()` method; new `send_followup()` method; interrupt monitoring in `_send_and_wait()` |
| `src/conductor/providers/claude.py` | Accept `interrupt_signal` in `execute()` and `_execute_agentic_loop()`; check signal at top of each loop iteration; send user message requesting `emit_output` on interrupt |
| `src/conductor/exceptions.py` | Add `InterruptError` exception for workflow stop via interrupt |
| `tests/test_providers/test_registry.py` | Update `MockProvider.execute()` signature to include `interrupt_signal` param |
| `tests/test_integration/test_mixed_providers.py` | Update `MockProvider.execute()` signature to include `interrupt_signal` param |

### Deleted Files

| File Path | Reason |
|-----------|--------|
| (none) | |

---

## 9. Implementation Plan

### Epic 1: Keyboard Listener & CLI Integration

**Status:** DONE

**Goal:** Detect Esc/Ctrl+G keypresses asynchronously and expose them as an `asyncio.Event`. Handle Esc vs ANSI escape sequence disambiguation. Add `--no-interactive` CLI flag. Display Esc hint at workflow start.

**Prerequisites:** None

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E1-T1 | IMPL | Create `KeyboardListener` class with `start()`, `stop()`, `_listen_loop()`, `_read_byte_blocking()`. Use `tty.setcbreak()` on Unix to read stdin. Listen for Esc (0x1b) and Ctrl+G (0x07). **For 0x1b: implement 50ms read-ahead timeout to disambiguate bare Esc from ANSI escape sequences.** If follow-up bytes arrive within 50ms, discard the sequence; if not, it is a bare Esc. Dedicated daemon reader thread delivers bytes via `asyncio.Queue` using `loop.call_soon_threadsafe(queue.put_nowait)`. `_listen_loop` reads from `asyncio.Queue` with native async ops, eliminating thread leaks. Restore terminal with `termios.tcsetattr()` in `finally`, plus `atexit` handler and `signal.SIGTERM` handler for crash safety. | `src/conductor/interrupt/__init__.py`, `src/conductor/interrupt/listener.py` | DONE |
| E1-T2 | IMPL | Add `--no-interactive` flag to `run` and `resume` commands in `app.py`. Pass through to `run_workflow_async()` and `resume_workflow_async()` as a new parameter. | `src/conductor/cli/app.py` | DONE |
| E1-T3 | IMPL | In `run_workflow_async()` and `resume_workflow_async()`: create `asyncio.Event`, create `KeyboardListener` (only if `sys.stdin.isatty()` and not `--no-interactive`), start listener before `engine.run()`/`engine.resume()`, stop listener in `finally`. Pass event to `WorkflowEngine`. Display `[dim]Press Esc to interrupt and provide guidance[/dim]` at workflow start if listener is active (via `_verbose_console.print()` so it always displays regardless of `--verbose`). | `src/conductor/cli/run.py` | DONE |
| E1-T4 | TEST | Test `KeyboardListener`: mock `termios`/`tty` modules, verify event is set on bare Esc byte (with 50ms timeout confirming no follow-up), verify event is NOT set on arrow key sequences (0x1b 0x5b ...), verify Ctrl+G (0x07) sets event immediately, verify terminal restore on stop, verify no-op when stdin is not TTY. Added `TestReaderThread` for the dedicated reader thread. Integration-style tests call real `run_workflow_async()` with mocked dependencies. | `tests/test_interrupt/test_listener.py` | DONE |
| E1-T5 | TEST | Test CLI flag: verify `--no-interactive` is accepted on both `run` and `resume`, verify it disables listener creation, verify default behavior creates listener when TTY. | `tests/test_interrupt/test_handler.py` (CLI portion) | DONE |

**Acceptance Criteria:**
- [x] `KeyboardListener` sets event when Esc (bare, not escape sequence) or Ctrl+G is pressed
- [x] Arrow keys, function keys, and other escape sequences do NOT trigger interrupts
- [x] Event signaling uses `loop.call_soon_threadsafe()` (thread-safe)
- [x] Terminal settings are restored on stop (no leaked cbreak mode)
- [x] Listener is only created when stdin is TTY and `--no-interactive` is not set
- [x] `--no-interactive` flag is available on `run` and `resume` commands
- [x] Esc hint displayed at workflow start when listener is active
- [x] All tests pass

**Completion Notes:** Implemented with dedicated daemon reader thread + `asyncio.Queue` architecture to eliminate thread leaks from `run_in_executor`. Esc hint uses `_verbose_console.print()` to always display. Tests are integration-style, non-tautological.

---

### Epic 2: Interrupt Handler UI

**Status:** DONE

**Goal:** Create the Rich-based interrupt interaction panel that displays workflow state and collects user decisions.

**Prerequisites:** None (can be developed in parallel with Epic 1)

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E2-T1 | IMPL | Create `InterruptAction` enum (`continue_with_guidance`, `skip_to_agent`, `stop`, `cancel`) and `InterruptResult` dataclass (`action`, `guidance`, `skip_target`). | `src/conductor/gates/interrupt.py` | DONE |
| E2-T2 | IMPL | Create `InterruptHandler` class with `skip_gates: bool` constructor param. Implement `handle_interrupt()` method: display Rich panel with current agent, iteration, last output preview (truncated to 500 chars), accumulated guidance list, and numbered action options. Collect selection via `IntPrompt`. For "continue with guidance": collect text via `Prompt.ask()`. For "skip to agent": display available agents (top-level only, not nested in parallel/for-each), validate selection. If `skip_gates` is True, auto-select cancel (log message). Return `InterruptResult`. | `src/conductor/gates/interrupt.py` | DONE |
| E2-T3 | IMPL | Add `InterruptError` exception to exceptions.py, subclass of `ExecutionError`. Used when user selects "stop workflow" from interrupt menu. Includes `agent_name` field and message "Workflow stopped by user interrupt". | `src/conductor/exceptions.py` | DONE |
| E2-T4 | TEST | Test `InterruptHandler`: mock Rich console, verify panel content for various states, verify action selection flow, verify guidance text collection, verify skip-to-agent validation rejects invalid names and re-prompts, verify cancel returns no-op result, verify skip_gates auto-cancels. | `tests/test_interrupt/test_handler.py` | DONE |

**Acceptance Criteria:**
- [x] Rich panel displays current agent, iteration, output preview, and accumulated guidance
- [x] All four actions work correctly (continue, skip, stop, cancel)
- [x] Skip-to-agent validates target exists in available agents list (top-level only)
- [x] Guidance text is captured and returned in result
- [x] Panel follows same visual style as `MaxIterationsHandler`
- [x] `skip_gates` mode auto-selects cancel
- [x] All tests pass

**Completion Notes:** Implemented `InterruptAction` enum, `InterruptResult` dataclass, and `InterruptHandler` class in `src/conductor/gates/interrupt.py`. Added `InterruptError` exception to `src/conductor/exceptions.py`. Skip-to-agent supports selection by both name and number with validation and re-prompting. 35 tests cover all action flows, panel content, edge cases (empty guidance, invalid agents, KeyboardInterrupt, EOFError). Review fixes applied: Rich markup escaping via `rich.markup.escape()` for output previews and guidance items, guidance text stripped before storing to prevent whitespace injection into prompts.

---

### Epic 3: Guidance Injection & Context Integration

**Status:** DONE

**Goal:** Store accumulated guidance in `WorkflowContext` and inject it into agent prompts via the executor.

**Prerequisites:** Epic 2 (InterruptResult defines guidance format)

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E3-T1 | IMPL | Add `user_guidance: list[str]` field to `WorkflowContext` dataclass. Add `add_guidance(text: str)` method that appends to list. Add `get_guidance_prompt_section()` that returns formatted `[User Guidance]` section or None if empty. Update `to_dict()` to include `user_guidance`. Update `from_dict()` to restore guidance with backward-compatible default: `data.get("user_guidance", [])` so old checkpoints without this field load correctly. | `src/conductor/engine/context.py` | DONE |
| E3-T2 | IMPL | Modify `AgentExecutor.execute()` to accept optional `guidance_section` parameter. If provided, append it to the rendered prompt before calling `provider.execute()`. The guidance section is appended to the rendered prompt text, not to the system prompt. | `src/conductor/executor/agent.py` | DONE |
| E3-T3 | IMPL | In `WorkflowEngine._execute_loop()`, before calling `executor.execute()`, get `guidance_section = self.context.get_guidance_prompt_section()` and pass it to the executor. | `src/conductor/engine/workflow.py` | DONE |
| E3-T4 | TEST | Test `WorkflowContext` guidance methods: add single guidance, add multiple, get formatted section, empty returns None, serialization roundtrip via `to_dict()`/`from_dict()`, backward compatibility (loading dict without `user_guidance` key). | `tests/test_engine/test_context.py` (extend existing) | DONE |
| E3-T5 | TEST | Test `AgentExecutor` guidance injection: verify guidance is appended to rendered prompt, verify None guidance does not change prompt, verify guidance appears before any schema instruction block. | `tests/test_executor/test_agent_guidance.py` | DONE |

**Acceptance Criteria:**
- [x] Guidance accumulates correctly across multiple interrupts
- [x] Formatted `[User Guidance]` section is appended to agent rendered prompts
- [x] Empty guidance produces no modification to prompt
- [x] Guidance survives serialization/deserialization (checkpoint support)
- [x] Loading old checkpoints without `user_guidance` field works (backward compatible)
- [x] All tests pass

**Completion Notes:** Added `user_guidance: list[str]` field, `add_guidance()`, and `get_guidance_prompt_section()` to `WorkflowContext`. Updated `to_dict()`/`from_dict()` with backward-compatible serialization. `AgentExecutor.execute()` accepts optional `guidance_section` parameter appended after the rendered prompt. `WorkflowEngine._execute_loop()` passes guidance to executor for regular agent execution. 14 new tests cover guidance accumulation, prompt injection, serialization roundtrip, and backward compatibility.

---

### Epic 4: Engine Interrupt Integration (Between-Agent)

**Status:** DONE

**Goal:** Wire the interrupt event check into `_execute_loop()` and handle all `InterruptResult` actions.

**Prerequisites:** Epic 1, Epic 2, Epic 3

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E4-T1 | IMPL | Add `interrupt_event` parameter to `WorkflowEngine.__init__()`. Store as `self._interrupt_event`. Create `InterruptHandler` instance (stored as `self._interrupt_handler`), passing `skip_gates` to its constructor. | `src/conductor/engine/workflow.py` | DONE |
| E4-T2 | IMPL | Add `_check_interrupt()` async method to `WorkflowEngine`. Checks `self._interrupt_event.is_set()`. If set: clear event, build output preview from last stored output (truncated), call `self._interrupt_handler.handle_interrupt()` with current agent, iteration, preview, list of top-level agent names (excluding parallel/for-each nested agents), and accumulated guidance. Return `InterruptResult`. | `src/conductor/engine/workflow.py` | DONE |
| E4-T3 | IMPL | Insert interrupt check in `_execute_loop()` at the end of the while loop body, after route evaluation and before the next iteration. Handle all actions: `CONTINUE` calls `self.context.add_guidance(result.guidance)`, `SKIP` sets `current_agent_name = result.skip_target`, `STOP` raises `InterruptError(agent_name=current_agent_name)`, `CANCEL` is a no-op. On `STOP`, the existing `ConductorError` handler will save a checkpoint. | `src/conductor/engine/workflow.py` | DONE |
| E4-T4 | IMPL | Handle interrupt queuing for parallel/for-each groups: if interrupt fires during parallel/for-each execution, defer handling until after the group completes (check at the same point as regular agents). | `src/conductor/engine/workflow.py` | DONE |
| E4-T5 | IMPL | Update `run_workflow_async()` and `resume_workflow_async()` to pass `interrupt_event` to `WorkflowEngine()` constructor. For `resume`, accumulated guidance from the checkpoint is preserved (restored via `WorkflowContext.from_dict()`). | `src/conductor/cli/run.py` | DONE |
| E4-T6 | TEST | Integration test: mock interrupt event, verify engine pauses and calls handler, verify guidance is injected, verify skip changes next agent, verify stop raises InterruptError, verify cancel continues normally, verify Ctrl+C still works (KeyboardInterrupt is distinct from InterruptError). | `tests/test_engine/test_workflow_interrupt.py` | DONE |
| E4-T7 | TEST | Test interrupt queuing: fire interrupt during parallel group, verify it is handled after group completes. | `tests/test_engine/test_workflow_interrupt.py` | DONE |

**Acceptance Criteria:**
- [x] Engine pauses on interrupt event between agents
- [x] All four actions (continue, skip, stop, cancel) behave correctly
- [x] Guidance from "continue" action persists for subsequent agents
- [x] Skip-to-agent overrides normal routing
- [x] Stop raises `InterruptError` with checkpoint saved
- [x] Interrupts during parallel/for-each are deferred to after group completion
- [x] No interrupt check when `interrupt_event` is None (backward compatible)
- [x] Ctrl+C behavior unchanged (KeyboardInterrupt, not InterruptError)
- [x] All tests pass

**Completion Notes:** Added `InterruptHandler` creation in `WorkflowEngine.__init__()`, `_check_interrupt()` and `_handle_interrupt_result()` methods, and `_get_top_level_agent_names()` helper. Interrupt checks are placed at the end of the main while loop body (after route evaluation for regular agents, parallel groups, and for-each groups) and before the `continue` for script steps. Human gates are excluded per spec (user is already interacting). Parallel/for-each groups naturally defer interrupts because the check only occurs after the group completes. 25 tests cover all four actions, guidance accumulation, skip routing, checkpoint save on stop, backward compatibility, and parallel group queuing. E4-T5 was already implemented in Epic 1 (CLI already passes `interrupt_event` to `WorkflowEngine`).

---

### Epic 5: Mid-Agent Interrupt — Copilot Provider (Phase 2)

**Status:** DONE

**Goal:** Enable mid-execution interrupts for the Copilot provider. Requires runtime verification of SDK abort capability.

**Prerequisites:** Epic 4. Must empirically verify Copilot SDK abort support before implementation.

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E5-T1 | IMPL | Add `partial: bool = False` field to `AgentOutput` dataclass. | `src/conductor/providers/base.py` | DONE |
| E5-T2 | IMPL | Add `interrupt_signal` parameter to `AgentProvider.execute()` abstract method. Update docstring. | `src/conductor/providers/base.py` | DONE |
| E5-T3 | IMPL | Update all concrete `execute()` implementations and test mocks to include the new parameter: `CopilotProvider.execute()`, `ClaudeProvider.execute()`, `_MockProvider` in `cli/run.py`, `MockProvider` in `test_registry.py`, `MockProvider` in `test_mixed_providers.py`. All non-Copilot implementations accept and ignore the parameter for now. | `src/conductor/providers/copilot.py`, `src/conductor/providers/claude.py`, `src/conductor/cli/run.py`, `tests/test_providers/test_registry.py`, `tests/test_integration/test_mixed_providers.py` | DONE |
| E5-T4 | IMPL | Update `AgentExecutor.execute()` to accept and forward `interrupt_signal` to `provider.execute()`. | `src/conductor/executor/agent.py` | DONE |
| E5-T5 | IMPL | Add runtime abort capability detection to `CopilotProvider`: check `hasattr(session, 'abort')` at session creation. If unavailable, try raw RPC. Store capability flag. | `src/conductor/providers/copilot.py` | DONE |
| E5-T6 | IMPL | Create `CopilotProvider._execute_with_interrupt()` method: creates session, sends prompt, monitors `interrupt_signal` alongside `done` event in `_send_and_wait()`. If interrupt: call abort (method or RPC), wait for post-abort event (idle/error/5s timeout), capture partial content, return `(AgentOutput(partial=True), session_handle)` without destroying session. | `src/conductor/providers/copilot.py` | DONE |
| E5-T7 | IMPL | Create `CopilotProvider.send_followup(session_handle, guidance)` method: sends guidance as follow-up `session.send()`, waits for response, destroys session, returns `AgentOutput`. | `src/conductor/providers/copilot.py` | DONE |
| E5-T8 | IMPL | In `WorkflowEngine._execute_loop()`: detect `output.partial == True` after agent execution. If partial: invoke interrupt handler, then if user provides guidance, call `provider.send_followup()` for Copilot. For non-Copilot providers, re-invoke `execute()` with guidance appended to prompt. | `src/conductor/engine/workflow.py` | DONE |
| E5-T9 | TEST | Test Copilot interrupt: mock session with abort support, verify partial content captured, verify post-abort event handling (idle, error, timeout), verify follow-up send with guidance, verify fallback when abort unavailable. | `tests/test_providers/test_copilot_interrupt.py` | DONE |
| E5-T10 | TEST | Test engine partial output handling: mock provider returning partial output, verify interrupt handler invoked, verify re-execution with guidance. Test that all mock providers still work after ABC signature change. | `tests/test_engine/test_workflow_interrupt.py` (extend) | DONE |

**Acceptance Criteria:**
- [x] `interrupt_signal` parameter added to provider ABC (backward compatible via default None)
- [x] All concrete provider implementations and test mocks updated
- [x] Copilot provider detects abort capability at runtime
- [x] Copilot provider calls abort when interrupt signal is set (with RPC fallback)
- [x] Partial output is captured and returned with `partial=True`
- [x] Post-abort session is kept alive for follow-up
- [x] `send_followup()` sends guidance and destroys session
- [x] Graceful fallback if abort is unavailable (between-agent interrupt behavior)
- [x] All tests pass

---

### Epic 6: Mid-Agent Interrupt — Claude Provider (Phase 3)

**Goal:** Enable mid-execution interrupts for the Claude provider by checking the interrupt flag between agentic loop iterations.

**Prerequisites:** Epic 5 (provider ABC changes)

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E6-T1 | IMPL | In `ClaudeProvider._execute_agentic_loop()`: accept `interrupt_signal` parameter. At the top of each `while` loop iteration (after `iteration += 1`), check `interrupt_signal.is_set()`. If set: clear the event, append a **user message** (not system message) to the messages list asking Claude to call the `emit_output` tool with its best partial result. Send one final API call. Parse the `emit_output` tool_use response. Return the response as partial. | `src/conductor/providers/claude.py` | TO DO |
| E6-T2 | IMPL | Update `ClaudeProvider.execute()` to forward `interrupt_signal` to `_execute_with_retry()` and then to `_execute_agentic_loop()`. | `src/conductor/providers/claude.py` | TO DO |
| E6-T3 | IMPL | In `WorkflowEngine._execute_loop()`: when re-executing after Claude interrupt, append user guidance to the rendered prompt (Claude starts a fresh conversation on each `execute()` call, so the guidance + original prompt provides context). | `src/conductor/engine/workflow.py` | TO DO |
| E6-T4 | TEST | Test Claude interrupt: mock API responses, verify interrupt check between iterations, verify user message (not system) requesting `emit_output` is sent, verify `emit_output` tool_use response is parsed as partial output, verify partial output is NOT schema-validated. | `tests/test_providers/test_claude_interrupt.py` | TO DO |
| E6-T5 | TEST | Test Claude re-invocation with guidance: verify guidance is appended to rendered prompt, verify conversation starts fresh with guidance context. | `tests/test_providers/test_claude_interrupt.py` | TO DO |

**Acceptance Criteria:**
- [ ] Interrupt signal is checked at the start of each agentic loop iteration
- [ ] Final emit_output request is sent as a user message (tool_use request, not system instruction)
- [ ] Partial output from `emit_output` tool_use is parsed correctly
- [ ] Partial output is not schema-validated (may be incomplete)
- [ ] User guidance is appended to rendered prompt for re-invocation
- [ ] Re-invocation starts a fresh conversation (Claude agentic loop does not persist state)
- [ ] All tests pass

---

## Appendix A: Interrupt Behavior Matrix

| Scenario | Phase | Behavior |
|----------|-------|----------|
| Esc pressed between agents | 1 | Engine pauses, shows interrupt panel, collects guidance |
| Esc pressed during Copilot agent | 2 | Abort (method or RPC), partial output captured, guidance sent as follow-up via `send_followup()` |
| Esc pressed during Claude agentic loop | 3 | Loop interrupted, user message requesting emit_output sent, guidance added to prompt |
| Esc pressed during parallel group | 1 | Deferred: interrupt handled after group completes |
| Esc pressed during for-each group | 1 | Deferred: interrupt handled after group completes |
| Esc pressed during human gate | 1 | Ignored (user is already interacting) |
| Esc pressed during script step | 1 | Deferred: interrupt handled after script completes |
| Arrow key / function key pressed | - | Listener disambiguates via 50ms read-ahead; escape sequences discarded |
| Ctrl+C pressed | - | **Unchanged:** KeyboardInterrupt, checkpoint saved, workflow stops immediately |
| Non-TTY environment | - | Listener not created, no interrupt capability |
| `--no-interactive` flag | - | Listener not created, no interrupt capability |
| `--skip-gates` flag | 1 | Interrupt handler auto-selects cancel (skip-gates mode) |

## Appendix B: Guidance Prompt Format

```
[User Guidance]
The following guidance was provided by the user during workflow execution.
Incorporate this guidance into your response:
- Focus only on Python 3.12+ features
- Use async/await patterns, not threading
- Keep the response under 500 words
```

## Appendix C: Checkpoint Backward Compatibility

When loading checkpoints saved before this feature was implemented, `WorkflowContext.from_dict()` uses `data.get("user_guidance", [])` to provide a default empty list. This ensures:
- Old checkpoints load without errors
- Resumed workflows start with no accumulated guidance (user can add guidance via interrupt during the resumed run)
- No migration step is needed

## Appendix D: Thread Safety Model

```
Main Thread (asyncio event loop)
+-- WorkflowEngine._execute_loop()      <-- checks interrupt_event.is_set()
+-- InterruptHandler.handle_interrupt()  <-- runs on main loop
+-- KeyboardListener._listen_loop()      <-- asyncio.Task
    +-- loop.run_in_executor(None, self._read_byte_blocking)
        +-- Executor Thread (blocking stdin read)
            +-- On Esc detected: loop.call_soon_threadsafe(event.set)
                                 ^ schedules event.set() on main loop
```

The `asyncio.Event` is only ever `.set()` from the event loop thread (via `call_soon_threadsafe`), and only ever `.is_set()` / `.clear()` from the event loop thread (in the engine). This ensures thread safety without locks.
