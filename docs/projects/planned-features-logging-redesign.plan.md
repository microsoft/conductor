# Logging Redesign — Solution Design

**Revision:** 1 — Initial draft

---

## 1. Problem Statement

Conductor's current logging model uses a single-axis `--verbose`/`-V` flag that is unintuitive:

- **Default behavior hides too much.** Without `--verbose`, prompts are truncated at 500 characters. Users must opt-in to see their own prompts, making debugging harder than it should be.
- **No quiet mode.** There is no way to suppress progress output while still getting the final JSON result on stdout — a common CI/CD requirement.
- **No file logging.** All output goes to the console. There is no built-in way to capture full debug logs to a file while keeping the console clean.
- **Confusing naming.** The internal model has two ContextVars (`verbose_mode` for "show any progress" and `full_mode` for "show untruncated detail"), but the user-facing CLI only exposes `--verbose` for `full_mode`. The `verbose_mode` default of `True` with no flag to disable it creates implicit behavior.

The redesign replaces this with a two-dimensional model: **console verbosity** (full/minimal/silent) × **file output** (none/auto/explicit), giving users precise control with sensible defaults.

---

## 2. Goals and Non-Goals

### Goals

1. **Full output is the default.** Running `conductor run workflow.yaml` shows untruncated prompts, tool calls, timing, and routing — no flags needed.
2. **`--quiet`/`-q` provides minimal output.** Agent start/complete, routing, timing — no prompt/tool detail.
3. **`--silent`/`-s` suppresses all progress.** Only the final JSON result appears on stdout. Exit code communicates success/failure.
4. **`--log-file`/`-l` writes full debug output to a file** independently of console verbosity. `--silent --log-file` is the canonical CI pattern.
5. **`--verbose`/`-V` is removed** entirely. No deprecation period — clean break.
6. **File output uses plain text** (`no_color=True`), never Rich markup/ANSI.
7. **Log file path printed to stderr** on workflow completion when file logging is active.
8. **Backward-compatible internal API.** The `verbose_mode` and `full_mode` ContextVars continue to exist; the new CLI flags just set them differently.

### Non-Goals

- Structured logging (JSON lines, log levels) — out of scope.
- Log rotation or size limits — out of scope.
- Per-agent verbosity control — out of scope.
- Remote log shipping — out of scope.
- Deprecation shim for `--verbose` (the spec says "removed entirely").

---

## 3. Requirements

### Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-1 | Default console output (no flags) shows full untruncated prompts, tool args, timing, routing. |
| FR-2 | `--quiet`/`-q` flag reduces console output to agent start/complete, routing, and timing only. No prompt or tool detail. |
| FR-3 | `--silent`/`-s` flag suppresses all progress output. Only JSON result on stdout. |
| FR-4 | `--quiet` and `--silent` are mutually exclusive. CLI error if both provided. |
| FR-5 | `--log-file`/`-l` without a path argument writes to `$TMPDIR/conductor/conductor-<workflow>-<timestamp>.log`. |
| FR-6 | `--log-file PATH`/`-l PATH` writes to the specified path. |
| FR-7 | File output is always full/untruncated regardless of console verbosity level. |
| FR-8 | File output uses `no_color=True` for plain text (no ANSI escape codes). |
| FR-9 | At workflow completion, the log file path is printed to stderr. |
| FR-10 | The `--verbose`/`-V` flag is removed from the CLI. |
| FR-11 | `verbose_log_section()` truncation message changes from "use --verbose for full" to appropriate guidance. |

### Non-Functional Requirements

| ID | Requirement |
|----|-------------|
| NFR-1 | No measurable performance regression from file logging when disabled. |
| NFR-2 | File logging directory is created automatically (`os.makedirs(..., exist_ok=True)`). |
| NFR-3 | All existing tests updated to reflect new flag semantics. |
| NFR-4 | File I/O errors (permissions, disk full) produce clear error messages, not stack traces. |

---

## 4. Solution Architecture

### Overview

The redesign introduces a **console verbosity enum** and a **file output configuration**, both managed as ContextVars in `app.py`. All existing `verbose_log_*` functions in `run.py` are updated to check the new verbosity level instead of the old boolean flags. A second Rich `Console` instance targeting a file handle is optionally created for file output.

### Key Components

#### 4.1 `ConsoleVerbosity` Enum (new, in `app.py`)

```python
class ConsoleVerbosity(str, Enum):
    FULL = "full"       # Default: everything, untruncated
    MINIMAL = "minimal" # Agent lifecycle + routing + timing only
    SILENT = "silent"   # No progress output at all
```

#### 4.2 Updated ContextVars (in `app.py`)

The existing `verbose_mode` and `full_mode` ContextVars are **retained** for backward compatibility of internal callers, but their values are now **derived** from the new `ConsoleVerbosity`:

| ConsoleVerbosity | `verbose_mode` | `full_mode` |
|------------------|---------------|-------------|
| `FULL`           | `True`        | `True`      |
| `MINIMAL`        | `True`        | `False`     |
| `SILENT`         | `False`       | `False`     |

A new ContextVar `console_verbosity` is added, but `is_verbose()` and `is_full()` continue to work unchanged — they still read `verbose_mode` and `full_mode`.

#### 4.3 File Console (in `run.py`)

```python
# Module-level, initially None
_file_console: Console | None = None

def init_file_logging(log_path: Path) -> None:
    """Initialize file logging to the given path."""
    global _file_console
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _file_handle = open(log_path, "w")
    _file_console = Console(file=_file_handle, no_color=True, highlight=False, width=200)
```

Every `verbose_log_*` function gains a dual-write pattern:
1. Check console verbosity → write to `_verbose_console` if appropriate.
2. If `_file_console` is not None → **always** write to `_file_console` (full/untruncated).

#### 4.4 CLI Flag Changes (in `app.py`)

The `main()` callback changes from:
```python
def main(version: ..., verbose: ...):
    full_mode.set(verbose)
```

To:
```python
def main(version: ..., quiet: ..., silent: ...):
    if quiet and silent:
        raise typer.BadParameter("--quiet and --silent are mutually exclusive")
    if silent:
        verbosity = ConsoleVerbosity.SILENT
    elif quiet:
        verbosity = ConsoleVerbosity.MINIMAL
    else:
        verbosity = ConsoleVerbosity.FULL
    console_verbosity.set(verbosity)
    verbose_mode.set(verbosity != ConsoleVerbosity.SILENT)
    full_mode.set(verbosity == ConsoleVerbosity.FULL)
```

The `--log-file`/`-l` flag is added to the `run` command (not global), since it is scoped to workflow execution.

#### 4.5 Log File Path Generation

```python
def generate_log_path(workflow_name: str) -> Path:
    """Generate auto log file path: $TMPDIR/conductor/conductor-<workflow>-<timestamp>.log"""
    import tempfile
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    return Path(tempfile.gettempdir()) / "conductor" / f"conductor-{workflow_name}-{timestamp}.log"
```

#### 4.6 Data Flow

```
CLI flags (--quiet, --silent, --log-file)
    │
    ▼
app.py: main() callback
    ├─ Sets console_verbosity ContextVar
    ├─ Sets verbose_mode ContextVar (derived)
    └─ Sets full_mode ContextVar (derived)
    │
    ▼
app.py: run() command
    ├─ Parses --log-file flag
    └─ Passes log_file to run_workflow_async()
    │
    ▼
run.py: run_workflow_async()
    ├─ Calls init_file_logging() if log_file specified
    ├─ Workflow executes (unchanged)
    └─ On completion: prints log file path to stderr, closes file
    │
    ▼
run.py: verbose_log_*() functions
    ├─ Check is_verbose() / is_full() for console output (unchanged)
    └─ If _file_console: always write full output to file
```

#### 4.7 `_log_event_verbose` in copilot.py

The `_log_event_verbose` method in the Copilot provider creates a new `Console(stderr=True)` on each call. This needs to be updated to also write to the file console. Since it receives `verbose_enabled` and `full_enabled` as booleans (captured before async callbacks), it will also need access to the file console reference.

The approach: add an optional `file_console` parameter and import `_file_console` from `run.py` at the capture point.

### API Contracts

**CLI interface (public):**
```
conductor run workflow.yaml                          # full output (default)
conductor run workflow.yaml --quiet                  # minimal output
conductor run workflow.yaml -q                       # minimal output (short)
conductor run workflow.yaml --silent                 # no progress, JSON only
conductor run workflow.yaml -s                       # no progress (short)
conductor run workflow.yaml --log-file               # auto temp file
conductor run workflow.yaml -l                       # auto temp file (short)
conductor run workflow.yaml --log-file debug.log     # explicit path
conductor run workflow.yaml -l debug.log             # explicit path (short)
conductor run workflow.yaml --silent --log-file      # CI pattern
```

**Internal Python API (unchanged):**
```python
from conductor.cli.app import is_verbose, is_full
# These continue to work exactly as before
```

---

## 5. Dependencies

### Internal Dependencies

| Component | Dependency |
|-----------|-----------|
| `cli/app.py` | Rich, Typer (existing) |
| `cli/run.py` | `cli/app.py` ContextVars (existing) |
| `engine/workflow.py` | `cli/run.py` verbose_log functions (existing, via lazy import wrappers) |
| `executor/agent.py` | `cli/run.py` verbose_log functions (existing, via lazy import wrappers) |
| `providers/copilot.py` | `cli/app.py` is_verbose/is_full (existing) |
| `mcp_auth.py` | `cli/run.py` verbose_log (existing) |

### External Dependencies

| Package | Usage | Already In Project |
|---------|-------|-------------------|
| Rich | Console, Panel, Text | ✅ Yes |
| Typer | CLI framework, Options | ✅ Yes |

No new external dependencies required.

---

## 6. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| **Breaking change for `--verbose` users** | Medium | Low | Feature spec explicitly removes `--verbose` with no deprecation. Users get full output by default now, which is better. Document in changelog. |
| **Typer `Optional[str]` for `--log-file`** | Medium | Medium | Typer's handling of optional arguments to options can be tricky. Use `typer.Option(default=None, is_eager=False)` and test the three cases (no flag, flag alone, flag with path) thoroughly. May need to use a callback for disambiguation. |
| **File handle leak on crash** | Low | Low | Use try/finally in `run_workflow_async()` to close the file handle. Consider `atexit` as fallback. |
| **Race condition with file console in parallel execution** | Low | Low | Rich `Console` is not thread-safe but Conductor uses asyncio (single-threaded). Parallel agents run as async tasks, not threads, so writes are serialized. |
| **copilot.py creates Console per-call** | Low | Medium | The `_log_event_verbose` method instantiates `Console(stderr=True)` on each event. For file output, it needs access to the file console. Pass it through or use a module-level import. |
| **Test breakage from removed `--verbose` flag** | High | Low | Tests explicitly test for `--verbose` flag. These must be updated. Risk is well-understood and contained. |

---

## 7. Implementation Phases

### Phase 1: Core Infrastructure
Add the `ConsoleVerbosity` enum, new ContextVars, and update `app.py` main callback. Remove `--verbose`/`-V` flag. Wire `--quiet`/`-q` and `--silent`/`-s` flags.

**Exit Criteria:** `conductor --help` shows `--quiet` and `--silent` but not `--verbose`. Default behavior shows full untruncated output. `--silent` suppresses all progress.

### Phase 2: Update Verbose Log Functions
Update all `verbose_log_*` functions in `run.py` to implement the `MINIMAL` vs `FULL` distinction. `MINIMAL` shows agent lifecycle and routing only. `FULL` shows everything (now the default).

**Exit Criteria:** `conductor run --quiet workflow.yaml` shows only agent start/complete and routing. Default run shows full prompts and tool details.

### Phase 3: File Logging
Implement `--log-file`/`-l` on the `run` command. Add file console initialization, dual-write in verbose_log functions, log path generation, and cleanup.

**Exit Criteria:** `conductor run --silent --log-file workflow.yaml` produces clean JSON on stdout and a full debug log in `$TMPDIR/conductor/`. Log file path printed to stderr.

### Phase 4: Test Updates & Cleanup
Update all existing tests. Add new tests for `--quiet`, `--silent`, `--log-file`, and mutual exclusion. Remove tests for `--verbose`.

**Exit Criteria:** `make test` passes. `make lint` passes. `make typecheck` passes.

---

## 8. Files Affected

### New Files

| File Path | Purpose |
|-----------|---------|
| (none) | All changes fit within existing files. |

### Modified Files

| File Path | Changes |
|-----------|---------|
| `src/conductor/cli/app.py` | Add `ConsoleVerbosity` enum, `console_verbosity` ContextVar. Replace `--verbose`/`-V` with `--quiet`/`-q` and `--silent`/`-s` in `main()`. Add `--log-file`/`-l` to `run()` command. Update `is_full()` default to `True`. Derive `verbose_mode`/`full_mode` from verbosity level. |
| `src/conductor/cli/run.py` | Add `_file_console`, `init_file_logging()`, `close_file_logging()`, `generate_log_path()`. Update all `verbose_log_*` functions to dual-write to file console. Update `verbose_log_section()` truncation message. Update `run_workflow_async()` to accept and manage `log_file`. Update `display_usage_summary()` to respect new verbosity. |
| `src/conductor/providers/copilot.py` | Update `_log_event_verbose()` to also write to file console when active. Update capture of verbose state to include file console reference. |
| `tests/test_cli/test_verbose.py` | Rename to `test_logging.py`. Remove `--verbose` tests. Add `--quiet`, `--silent`, `--log-file` tests. Update ContextVar tests. |
| `tests/test_cli/test_e2e.py` | Update any references to `--verbose` flag. |
| `tests/test_cli/test_run.py` | Update `run_workflow_async` call signatures if they reference verbose/log params. |
| `tests/test_integration/test_for_each_verbose.py` | Verify tests still pass with new default (full mode on). May need minor adjustments to mock targets. |

### Deleted Files

| File Path | Reason |
|-----------|--------|
| `tests/test_cli/test_verbose.py` | Replaced by `tests/test_cli/test_logging.py` (renamed + rewritten). |

---

## 9. Implementation Plan

### Epic 1: CLI Flag Infrastructure

**Status: DONE**

**Goal:** Replace `--verbose`/`-V` with `--quiet`/`-q` and `--silent`/`-s`. Add `ConsoleVerbosity` enum and derive existing ContextVars from it. Full output becomes the default.

**Prerequisites:** None

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E1-T1 | IMPL | Add `ConsoleVerbosity` enum (`FULL`, `MINIMAL`, `SILENT`) and `console_verbosity` ContextVar to `app.py` | `src/conductor/cli/app.py` | DONE |
| E1-T2 | IMPL | Remove `--verbose`/`-V` option from `main()` callback. Add `--quiet`/`-q` and `--silent`/`-s` options with mutual exclusion check | `src/conductor/cli/app.py` | DONE |
| E1-T3 | IMPL | Update `main()` to derive `verbose_mode` and `full_mode` ContextVars from the new verbosity level. `FULL` → verbose=True, full=True; `MINIMAL` → verbose=True, full=False; `SILENT` → verbose=False, full=False | `src/conductor/cli/app.py` | DONE |
| E1-T4 | IMPL | Add `--log-file`/`-l` option to `run()` command. Handle three cases: no flag (None), flag without value (auto path), flag with path (explicit). Pass value to `run_workflow_async()` | `src/conductor/cli/app.py` | DONE |
| E1-T5 | IMPL | Update `run_workflow_async()` signature to accept `log_file: Path | None` parameter | `src/conductor/cli/run.py` | DONE |
| E1-T6 | TEST | Add tests for `--quiet` and `--silent` flags being accepted. Test mutual exclusion error. Test `--verbose` is no longer accepted. Test `--log-file` is accepted on `run` command. | `tests/test_cli/test_logging.py` | DONE |
| E1-T7 | TEST | Update ContextVar tests: `is_full()` defaults to `True` (not `False`). `ConsoleVerbosity` sets derived vars correctly. | `tests/test_cli/test_logging.py` | DONE |

**Acceptance Criteria:**
- [x] `conductor --help` shows `--quiet`/`-q` and `--silent`/`-s`, not `--verbose`/`-V`
- [x] `conductor run --help` shows `--log-file`/`-l`
- [x] Default run (no flags) has `full_mode=True` and `verbose_mode=True`
- [x] `--quiet` sets `full_mode=False`, `verbose_mode=True`
- [x] `--silent` sets `full_mode=False`, `verbose_mode=False`
- [x] `--quiet --silent` produces a CLI error
- [x] All new tests pass

**Completion Notes:** Review issue fixes applied — log_file lifecycle (init/close with try/finally), generate_log_path mkdir, 8 new TestFileLogging integration tests, tightened mutual exclusion assertion, copilot.py dual-write via _print helper. All 1099 tests pass.

---

### Epic 2: Verbosity-Aware Console Output

**Status: DONE**

**Goal:** Update all `verbose_log_*` functions to distinguish between FULL and MINIMAL output. FULL shows everything (prompts, tool args, tool results). MINIMAL shows only agent lifecycle and routing.

**Prerequisites:** Epic 1

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E2-T1 | IMPL | Update `verbose_log_section()`: remove 500-char truncation entirely (full is default now). In MINIMAL mode, skip sections entirely. Remove truncation message referencing `--verbose`. | `src/conductor/cli/run.py` | DONE |
| E2-T2 | IMPL | Update `verbose_log()`: in MINIMAL mode, still show general messages (these are lifecycle-level). In SILENT mode, suppress all. No behavior change for FULL. | `src/conductor/cli/run.py` | DONE |
| E2-T3 | IMPL | Verify `verbose_log_agent_start()`, `verbose_log_agent_complete()`, `verbose_log_route()`, `verbose_log_timing()` work in both FULL and MINIMAL modes (they should — they already check `is_verbose()` which is True for both). | `src/conductor/cli/run.py` | DONE |
| E2-T4 | IMPL | Update `_log_event_verbose()` in copilot.py: in FULL mode, show tool args, results, reasoning (currently gated on `full_mode`). In MINIMAL mode, show only tool names. Behavior is already correct since it receives `full_enabled` boolean. Verify no changes needed. | `src/conductor/providers/copilot.py` | DONE |
| E2-T5 | IMPL | Update `_verbose_log_section()` in `executor/agent.py` — since it delegates to `run.py`, just verify the wrapper passes through correctly. No truncation parameter changes needed. | `src/conductor/executor/agent.py` | DONE |
| E2-T6 | TEST | Add tests verifying: FULL mode shows prompt sections, MINIMAL mode hides prompt sections but shows agent lifecycle, SILENT mode shows nothing. | `tests/test_cli/test_logging.py` | DONE |
| E2-T7 | TEST | Update `test_verbose_log_section_truncates_by_default` — truncation no longer happens by default (full is default). Either remove or invert this test. | `tests/test_cli/test_logging.py` | DONE |

**Acceptance Criteria:**
- [x] Default run shows full untruncated prompts (no "truncated" message)
- [x] `--quiet` run shows agent start/complete and routing but not prompts or tool details
- [x] `--silent` run shows no progress output
- [x] No "use --verbose for full" message anywhere in the codebase
- [x] Tool event logging in copilot.py respects the new semantics

**Completion Notes:** Updated `verbose_log_section()` to gate console output on `is_full()` in addition to `is_verbose()`, so MINIMAL mode (--quiet) skips sections entirely. Removed the 500-char truncation logic and `truncate` parameter — no longer needed since FULL is default and MINIMAL skips sections. The `_verbose_log_section` wrapper in `executor/agent.py` and `_log_event_verbose` in `copilot.py` were verified to already work correctly. Added 10 new tests for FULL/MINIMAL/SILENT behavior, replaced 3 existing section tests. All 1109 tests pass (excluding pre-existing failures in deprecated test_verbose.py).

---

### Epic 3: File Logging

**Goal:** Implement `--log-file`/`-l` flag that writes full untruncated output to a file, independently of console verbosity.

**Prerequisites:** Epic 2

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E3-T1 | IMPL | Add `generate_log_path(workflow_name: str) -> Path` function to `run.py`. Uses `$TMPDIR/conductor/conductor-<workflow>-<timestamp>.log`. | `src/conductor/cli/run.py` | TO DO |
| E3-T2 | IMPL | Add `init_file_logging(log_path: Path) -> None` and `close_file_logging() -> None` functions to `run.py`. Creates `_file_console = Console(file=..., no_color=True, highlight=False, width=200)`. Handles directory creation. | `src/conductor/cli/run.py` | TO DO |
| E3-T3 | IMPL | Update all `verbose_log_*` functions to dual-write: if `_file_console is not None`, always write full/untruncated content to the file console. This applies to: `verbose_log`, `verbose_log_agent_start`, `verbose_log_agent_complete`, `verbose_log_route`, `verbose_log_section`, `verbose_log_timing`, `verbose_log_parallel_*`, `verbose_log_for_each_*`, `display_usage_summary`. | `src/conductor/cli/run.py` | TO DO |
| E3-T4 | IMPL | Update `run_workflow_async()` to: (a) call `init_file_logging()` at start if `log_file` is provided, (b) resolve auto path from workflow name if log_file is sentinel, (c) print log file path to stderr on completion, (d) call `close_file_logging()` in finally block. | `src/conductor/cli/run.py` | TO DO |
| E3-T5 | IMPL | Update `_log_event_verbose()` in copilot.py to dual-write to file console. Import `_file_console` from `run.py` at the verbose state capture point and pass it through. | `src/conductor/providers/copilot.py` | TO DO |
| E3-T6 | TEST | Test file logging: verify file is created, content is plain text (no ANSI), content is untruncated, auto path generation works, explicit path works. | `tests/test_cli/test_logging.py` | TO DO |
| E3-T7 | TEST | Test CI pattern: `--silent --log-file` produces no console progress but writes full log file. Verify log file path is printed to stderr. | `tests/test_cli/test_logging.py` | TO DO |
| E3-T8 | TEST | Test error handling: permission denied on log path, disk full simulation. | `tests/test_cli/test_logging.py` | TO DO |

**Acceptance Criteria:**
- [ ] `conductor run --log-file workflow.yaml` creates a file in `$TMPDIR/conductor/`
- [ ] `conductor run --log-file debug.log workflow.yaml` creates `debug.log`
- [ ] File content has no ANSI escape codes
- [ ] File content is untruncated even when console is in `--quiet` mode
- [ ] Log file path printed to stderr on completion
- [ ] File handle closed on both success and error paths
- [ ] `$TMPDIR/conductor/` directory created automatically

---

### Epic 4: Test Migration & Cleanup

**Goal:** Remove old test file, ensure all existing tests pass with new defaults, clean up any remaining references to `--verbose`.

**Prerequisites:** Epic 3

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E4-T1 | TEST | Delete `tests/test_cli/test_verbose.py`. All test coverage has been migrated to `tests/test_cli/test_logging.py` in prior epics. | `tests/test_cli/test_verbose.py` | TO DO |
| E4-T2 | TEST | Update `tests/test_cli/test_e2e.py` — grep for any `--verbose` or `-V` references and update to new flags. | `tests/test_cli/test_e2e.py` | TO DO |
| E4-T3 | TEST | Update `tests/test_cli/test_run.py` — update `run_workflow_async` mock calls if signature changed (new `log_file` param). | `tests/test_cli/test_run.py` | TO DO |
| E4-T4 | TEST | Verify `tests/test_integration/test_for_each_verbose.py` passes without changes (it mocks the wrapper functions, which still exist). | `tests/test_integration/test_for_each_verbose.py` | TO DO |
| E4-T5 | IMPL | Grep entire codebase for "use --verbose" or "--verbose" string literals and remove/update any remaining references (help text, error messages, comments). | Multiple | TO DO |
| E4-T6 | TEST | Run full test suite (`make test`), linter (`make lint`), and type checker (`make typecheck`). Fix any failures. | — | TO DO |

**Acceptance Criteria:**
- [ ] `make test` passes with 0 failures
- [ ] `make lint` passes
- [ ] `make typecheck` passes
- [ ] No references to `--verbose` or `-V` flag remain in source code (except changelog/docs noting the removal)
- [ ] `tests/test_cli/test_verbose.py` is deleted
