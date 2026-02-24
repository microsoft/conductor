# Solution Design: `!file` YAML Tag for External File References

> **Feature:** #3 from `docs/projects/planned-features.md`
> **Status:** Draft
> **Revision:** 1 — Initial draft

---

## 1. Problem Statement

Conductor workflow YAML files currently require all content to be inline. For agents with long prompts, complex tool configurations, or shared prompt fragments, this leads to:

- **Bloated YAML files** that are hard to read and navigate
- **Duplication** when multiple agents share the same prompt or configuration
- **Poor separation of concerns** — prompt engineering mixed with workflow orchestration
- **Difficulty using non-YAML content** (e.g., Markdown prompt files) natively

Users need a way to reference external files from any YAML field value, with the content transparently inlined during loading. The `!file` custom YAML tag provides this capability using native YAML tag semantics (no string conventions or post-processing).

---

## 2. Goals and Non-Goals

### Goals

1. **Any YAML field** can use `!file path/to/file` to reference an external file
2. **Relative path resolution** — paths resolve relative to the parent YAML file's directory
3. **Content-type detection** — YAML-parseable files (dict/list) are parsed as structured data; everything else is treated as raw string
4. **Transparent env var resolution** — `${VAR}` references inside included files are resolved after inclusion (during the existing env var pass)
5. **Nested `!file` support** — included YAML files may themselves contain `!file` tags
6. **Cycle detection** — circular `!file` references produce a clear error, not infinite recursion
7. **Clear error messages** — missing files produce `ConfigurationError` with the referencing file location
8. **CWD fallback** — `load_string()` uses `source_path.parent` if provided, otherwise the current working directory

### Non-Goals

- **Glob/wildcard patterns** in `!file` paths (e.g., `!file prompts/*.md`)
- **URL/HTTP references** (e.g., `!file https://...`)
- **Conditional includes** or parameterized file references
- **Caching** of included file content across multiple loads
- **Binary file support** — only text files (UTF-8) are supported
- **Schema changes** — no new Pydantic fields or models needed; inclusion happens at YAML parse time before schema validation

---

## 3. Requirements

### Functional Requirements

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-1 | Register `!file` custom YAML tag constructor on the `ConfigLoader`'s `YAML()` instance | Must |
| FR-2 | Resolve file paths relative to the parent YAML file's directory | Must |
| FR-3 | Read file content as UTF-8 text | Must |
| FR-4 | If file content parses as YAML dict or list, return the parsed structure | Must |
| FR-5 | If file content is a YAML scalar or unparseable, return as raw string | Must |
| FR-6 | Support nested `!file` tags in included YAML files | Must |
| FR-7 | Detect circular `!file` references and raise `ConfigurationError` | Must |
| FR-8 | Raise `ConfigurationError` with file path for missing referenced files | Must |
| FR-9 | For `load_string()`, use `source_path.parent` or CWD for relative resolution | Must |
| FR-10 | `${VAR}` references inside included files are resolved after inclusion | Must |
| FR-11 | Document `!file` syntax in `docs/workflow-syntax.md` | Must |

### Non-Functional Requirements

| ID | Requirement | Priority |
|----|-------------|----------|
| NFR-1 | No new dependencies — uses existing `ruamel.yaml` and `pathlib` | Must |
| NFR-2 | File reads are synchronous (acceptable since YAML loading is already synchronous) | Must |
| NFR-3 | Error messages include both the referencing YAML file and the referenced file path | Should |
| NFR-4 | Performance: file inclusion adds negligible overhead vs. inline content | Should |

---

## 4. Solution Architecture

### Overview

The solution registers a custom ruamel.yaml constructor for the `!file` tag on the `ConfigLoader`'s YAML instance. When ruamel.yaml encounters `!file path/to/file` during parsing, it invokes the constructor, which:

1. Resolves the path relative to the parent YAML file's directory
2. Checks for circular references
3. Reads the file content
4. Attempts to parse it as YAML
5. Returns structured data (dict/list) or raw string (scalar/unparseable)

Because the constructor fires during YAML parsing (before `_resolve_env_vars_recursive()`), any `${VAR}` references in included files are resolved in the subsequent env var resolution pass.

### Key Components

#### 1. `FileTagConstructor` (Custom Constructor class)

**Location:** `src/conductor/config/loader.py`

A subclass of `ruamel.yaml.Constructor` (specifically `ruamel.yaml.constructor.RoundTripConstructor`) with the `!file` tag constructor registered. This is the pattern required by ruamel.yaml 0.18.x+ for registering custom tag constructors on a per-instance basis.

**Responsibilities:**
- Receive the `!file` scalar node from ruamel.yaml
- Resolve the file path relative to the base directory
- Detect circular references via a set of resolved absolute paths
- Read and optionally parse the file content
- Return the resolved content to the YAML parse tree

#### 2. Modified `ConfigLoader.__init__()`

**Location:** `src/conductor/config/loader.py`

Sets the custom constructor class on the YAML instance and initializes the base directory and file tracking set.

#### 3. Modified `ConfigLoader.load()` and `ConfigLoader.load_string()`

**Location:** `src/conductor/config/loader.py`

Before calling `self._yaml.load()`, set the base directory and initialize the tracking set on the constructor so the `!file` handler can resolve relative paths and detect cycles.

### Data Flow

```
YAML File (with !file tags)
        │
        ▼
  ConfigLoader.load()
        │
        ├── Sets base_dir on constructor (= parent YAML dir)
        ├── Initializes _file_stack (cycle detection set) with root file
        │
        ▼
  self._yaml.load(content)
        │
        ├── ruamel.yaml encounters !file tag
        ├── Calls FileTagConstructor.construct_file_tag(node)
        │       │
        │       ├── Resolves path relative to base_dir
        │       ├── Checks _file_stack for cycles
        │       ├── Adds resolved path to _file_stack
        │       ├── Reads file content (UTF-8)
        │       ├── Tries YAML parse with a fresh YAML() instance
        │       │     (shares same constructor class → nested !file works)
        │       ├── Returns dict/list if parsed, else raw string
        │       └── Removes resolved path from _file_stack
        │
        ▼
  Raw parsed data (dict) — !file tags fully resolved
        │
        ▼
  _resolve_env_vars_recursive(data)  — ${VAR} resolved
        │
        ▼
  _validate(data, source)  — Pydantic validation
        │
        ▼
  WorkflowConfig
```

### API Contract

**YAML Syntax:**
```yaml
# String field — file content used as raw string
prompt: !file prompts/review-prompt.md

# Structured field — file parsed as YAML dict/list
output: !file schemas/output-schema.yaml

# In lists
tools:
  - !file tools/search-tool.yaml
  - !file tools/calc-tool.yaml

# Nested inclusion (in the referenced file)
# prompts/review-prompt.md can itself contain !file tags if it's YAML
```

**Error Cases:**
```
ConfigurationError: File not found: 'prompts/missing.md'
  referenced from 'workflows/review.yaml'
  💡 Suggestion: Check the file path is correct relative to the workflow file directory.

ConfigurationError: Circular file reference detected: 'prompts/a.yaml'
  File inclusion chain: workflows/main.yaml → prompts/a.yaml → prompts/b.yaml → prompts/a.yaml
  💡 Suggestion: Remove the circular !file reference.
```

### Implementation Approach for ruamel.yaml Constructor Registration

Based on the ruamel.yaml 0.18.x API (confirmed via research), custom constructors must be registered on a `Constructor` subclass, then the subclass is assigned to the `YAML()` instance:

```python
from ruamel.yaml import YAML
from ruamel.yaml.constructor import RoundTripConstructor

class FileTagConstructor(RoundTripConstructor):
    # Instance-level state set by ConfigLoader before each load
    _base_dir: Path = Path(".")
    _file_stack: set[str] = set()

    def construct_file_tag(self, node):
        path_str = self.construct_scalar(node)
        # ... resolve, read, parse, return
        
FileTagConstructor.add_constructor("!file", FileTagConstructor.construct_file_tag)

# In ConfigLoader.__init__:
self._yaml = YAML()
self._yaml.Constructor = FileTagConstructor
```

**Important nuance:** Since `_base_dir` and `_file_stack` are class-level attributes, and `ConfigLoader` sets them before each `load()` call, this is safe for single-threaded use (which is the current usage pattern). For nested `!file` resolution, the constructor method updates `_base_dir` temporarily when parsing included files and restores it afterward.

However, to avoid class-level mutable state issues, we will use instance-level attributes by setting them on the constructor instance after the YAML object creates it (via the `yaml.constructor` property), or by creating a fresh constructor subclass per `ConfigLoader` instance using a factory pattern.

**Recommended approach:** Create the constructor class per `ConfigLoader` instance to avoid shared mutable state:

```python
class ConfigLoader:
    def __init__(self):
        self._yaml = YAML()
        self._yaml.preserve_quotes = True
        
        # Create a per-instance constructor class to avoid shared state
        constructor_cls = type(
            "FileTagConstructor",
            (RoundTripConstructor,),
            {"_base_dir": Path("."), "_file_stack": set()},
        )
        
        def construct_file_tag(constructor, node):
            # ... implementation
            pass
        
        constructor_cls.add_constructor("!file", construct_file_tag)
        self._yaml.Constructor = constructor_cls
```

This ensures each `ConfigLoader` instance has isolated state.

---

## 5. Dependencies

### Internal Dependencies

| Component | Dependency Type | Notes |
|-----------|----------------|-------|
| `ConfigLoader` | Modified | Core changes to register constructor and manage state |
| `ConfigurationError` | Used (no changes) | Error reporting for missing files and cycles |
| `_resolve_env_vars_recursive` | Unchanged | Runs after `!file` resolution — no coupling |
| `validate_workflow_config` | Unchanged | No awareness needed — operates on resolved data |

### External Dependencies

| Package | Version | Notes |
|---------|---------|-------|
| `ruamel.yaml` | `>=0.18.0` (already in pyproject.toml) | Uses `RoundTripConstructor` subclass pattern |
| `pathlib` | stdlib | Path resolution |

No new dependencies are required.

---

## 6. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| **ruamel.yaml Constructor API instability** — the subclass pattern may change in future versions | Low | Medium | Pin `ruamel.yaml>=0.18.0` (already done); the subclass pattern is the officially recommended approach for 0.18.x |
| **Thread safety** — class-level state on constructor could cause issues in concurrent use | Low | High | Use per-instance constructor class (factory pattern) to isolate state |
| **Large file inclusion** — users could include very large files causing memory issues | Low | Medium | Document as a limitation; no size limit enforcement in v1 (users control their files) |
| **Encoding issues** — non-UTF-8 files could cause crashes | Low | Low | Catch `UnicodeDecodeError` and raise `ConfigurationError` with helpful message |
| **Symlink cycles** — symlinks could bypass path-based cycle detection | Very Low | Medium | Use `Path.resolve()` to resolve symlinks before cycle checking |
| **Relative path confusion** — users may expect paths relative to CWD, not YAML file | Medium | Low | Clear documentation; error messages include both the resolved path and the base directory |

---

## 7. Implementation Phases

### Phase 1: Core `!file` Constructor (MVP)
**Exit Criteria:** Basic `!file` tag works for string and structured content, with cycle detection and error handling. All unit tests pass.

### Phase 2: Documentation & Examples
**Exit Criteria:** `docs/workflow-syntax.md` updated, example workflows demonstrate `!file` usage.

### Phase 3: Integration Testing
**Exit Criteria:** End-to-end tests with the `validate` CLI command confirm `!file` works through the full pipeline.

---

## 8. Files Affected

### New Files

| File Path | Purpose |
|-----------|---------|
| `tests/test_config/fixtures/file_tag/main.yaml` | Test fixture: main workflow using `!file` |
| `tests/test_config/fixtures/file_tag/prompt.md` | Test fixture: external prompt file (raw string) |
| `tests/test_config/fixtures/file_tag/output_schema.yaml` | Test fixture: external YAML dict |
| `tests/test_config/fixtures/file_tag/nested_parent.yaml` | Test fixture: workflow with nested `!file` |
| `tests/test_config/fixtures/file_tag/nested_child.yaml` | Test fixture: YAML file itself containing `!file` |
| `tests/test_config/fixtures/file_tag/nested_leaf.md` | Test fixture: leaf file for nested inclusion |
| `tests/test_config/fixtures/file_tag/cycle_a.yaml` | Test fixture: circular reference file A |
| `tests/test_config/fixtures/file_tag/cycle_b.yaml` | Test fixture: circular reference file B |
| `tests/test_config/fixtures/file_tag/env_vars.md` | Test fixture: file containing `${VAR}` references |
| `tests/test_config/test_file_tag.py` | Comprehensive tests for `!file` tag functionality |

### Modified Files

| File Path | Changes |
|-----------|---------|
| `src/conductor/config/loader.py` | Register `!file` constructor on YAML instance; implement file resolution, cycle detection, content parsing |
| `docs/workflow-syntax.md` | Add `!file` tag documentation section with syntax, examples, and behavior details |

### Deleted Files

| File Path | Reason |
|-----------|--------|
| *(none)* | |

---

## 9. Implementation Plan

### Epic 1: Core `!file` Tag Constructor

**Goal:** Implement the `!file` YAML tag constructor in `ConfigLoader` with full file resolution, content-type detection, cycle detection, and error handling.

**Prerequisites:** None

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E1-T1 | IMPL | Create per-instance `RoundTripConstructor` subclass with `!file` tag constructor registered via `add_constructor`. The constructor: (1) extracts scalar path value, (2) resolves relative to `_base_dir`, (3) checks `_file_stack` for cycles, (4) reads file as UTF-8, (5) attempts YAML parse — returns dict/list if successful, raw string if scalar/error. | `src/conductor/config/loader.py` | DONE |
| E1-T2 | IMPL | Modify `ConfigLoader.__init__()` to create the per-instance constructor class and assign it to `self._yaml.Constructor`. Initialize `_base_dir` and `_file_stack` as class attributes on the dynamic class. | `src/conductor/config/loader.py` | DONE |
| E1-T3 | IMPL | Modify `ConfigLoader.load()` to set `_base_dir = path.parent.resolve()` and `_file_stack = {str(path.resolve())}` on the constructor class before calling `self._yaml.load()`. Reset state after loading. | `src/conductor/config/loader.py` | DONE |
| E1-T4 | IMPL | Modify `ConfigLoader.load_string()` to set `_base_dir = source_path.parent.resolve()` if `source_path` is provided, otherwise `Path.cwd()`. Initialize `_file_stack` appropriately (with `source_path` if provided, empty set if not). Reset state after loading. | `src/conductor/config/loader.py` | DONE |
| E1-T5 | IMPL | Implement cycle detection: before reading a file, check if its resolved absolute path is in `_file_stack`. If yes, raise `ConfigurationError` with the chain. Track file stack as a set of resolved path strings. | `src/conductor/config/loader.py` | DONE |
| E1-T6 | IMPL | Implement nested `!file` support: when parsing an included YAML file, temporarily update `_base_dir` to the included file's parent directory, add the file to `_file_stack`, parse with a fresh `YAML()` instance sharing the same constructor class, then restore `_base_dir` and remove from `_file_stack`. | `src/conductor/config/loader.py` | DONE |
| E1-T7 | IMPL | Error handling: catch `FileNotFoundError` → `ConfigurationError` with path and suggestion; catch `UnicodeDecodeError` → `ConfigurationError` noting encoding; catch `YAMLError` during sub-parse → treat as raw string (not an error). | `src/conductor/config/loader.py` | DONE |

**Acceptance Criteria:**
- [x] `!file` tag resolves external files during YAML parsing
- [x] Relative paths resolve from the parent YAML file's directory
- [x] YAML dict/list content is returned as parsed structure
- [x] Non-YAML or scalar content is returned as raw string
- [x] Circular references raise `ConfigurationError`
- [x] Missing files raise `ConfigurationError` with helpful message
- [x] `${VAR}` in included files is resolved after inclusion
- [x] `load_string()` uses `source_path.parent` or CWD for resolution

### Epic 2: Test Suite

**Goal:** Comprehensive test coverage for all `!file` tag behaviors, edge cases, and error conditions.

**Prerequisites:** Epic 1

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E2-T1 | TEST | Create test fixture files: `main.yaml` (workflow with `!file` prompt), `prompt.md` (raw text prompt), `output_schema.yaml` (YAML dict for structured include) | `tests/test_config/fixtures/file_tag/` | DONE |
| E2-T2 | TEST | Create test fixture files for nested inclusion: `nested_parent.yaml` → `nested_child.yaml` → `nested_leaf.md` | `tests/test_config/fixtures/file_tag/` | DONE |
| E2-T3 | TEST | Create test fixture files for cycle detection: `cycle_a.yaml` ↔ `cycle_b.yaml` | `tests/test_config/fixtures/file_tag/` | DONE |
| E2-T4 | TEST | Create test fixture for env var resolution: `env_vars.md` containing `${TEST_VAR}` | `tests/test_config/fixtures/file_tag/` | DONE |
| E2-T5 | TEST | Write tests: `test_file_tag_string_content` — `!file` loads .md file as raw string into prompt field | `tests/test_config/test_file_tag.py` | DONE |
| E2-T6 | TEST | Write tests: `test_file_tag_structured_content` — `!file` loads .yaml file as parsed dict into output field | `tests/test_config/test_file_tag.py` | DONE |
| E2-T7 | TEST | Write tests: `test_file_tag_relative_path` — paths resolve relative to parent YAML, not CWD | `tests/test_config/test_file_tag.py` | DONE |
| E2-T8 | TEST | Write tests: `test_file_tag_nested_inclusion` — nested `!file` tags in included files work | `tests/test_config/test_file_tag.py` | DONE |
| E2-T9 | TEST | Write tests: `test_file_tag_cycle_detection` — circular `!file` raises `ConfigurationError` | `tests/test_config/test_file_tag.py` | DONE |
| E2-T10 | TEST | Write tests: `test_file_tag_missing_file` — missing file raises `ConfigurationError` with path | `tests/test_config/test_file_tag.py` | DONE |
| E2-T11 | TEST | Write tests: `test_file_tag_env_var_in_included_file` — `${VAR}` in included file resolved after inclusion | `tests/test_config/test_file_tag.py` | DONE |
| E2-T12 | TEST | Write tests: `test_file_tag_load_string_with_source_path` — `load_string()` resolves relative to `source_path.parent` | `tests/test_config/test_file_tag.py` | DONE |
| E2-T13 | TEST | Write tests: `test_file_tag_load_string_without_source_path` — `load_string()` resolves relative to CWD | `tests/test_config/test_file_tag.py` | DONE |
| E2-T14 | TEST | Write tests: `test_file_tag_in_list` — `!file` works inside YAML list items | `tests/test_config/test_file_tag.py` | DONE |
| E2-T15 | TEST | Write tests: `test_file_tag_yaml_scalar_as_string` — YAML file containing only a scalar is returned as string | `tests/test_config/test_file_tag.py` | DONE |

**Acceptance Criteria:**
- [x] All happy-path scenarios have test coverage
- [x] All error scenarios have test coverage
- [x] Edge cases (scalar YAML, list items, nested, CWD fallback) covered
- [x] All tests pass with `make test`
- [x] No regressions in existing `test_loader.py` tests

### Epic 3: Documentation

**Goal:** Document the `!file` tag in the workflow syntax reference so users can discover and use it.

**Prerequisites:** Epic 1

**Tasks:**

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E3-T1 | IMPL | Add "External File References" section to `docs/workflow-syntax.md` after the "Tools" section. Include: syntax overview, path resolution rules, content-type behavior, nested inclusion, env var interaction, and error handling. | `docs/workflow-syntax.md` | TO DO |
| E3-T2 | IMPL | Add usage examples showing: prompt from .md file, structured output schema from .yaml file, tools list from external file, and nested inclusion pattern. | `docs/workflow-syntax.md` | TO DO |

**Acceptance Criteria:**
- [ ] `docs/workflow-syntax.md` has a complete `!file` section
- [ ] Examples cover string, structured, and nested use cases
- [ ] Path resolution rules are clearly documented
- [ ] Limitations (UTF-8 only, no URLs, no globs) are stated

---

## Appendix: Detailed Constructor Implementation Notes

### ruamel.yaml 0.18.x Constructor Pattern

In ruamel.yaml 0.18.x, the legacy `yaml.add_constructor()` top-level function no longer works with the `YAML()` instance-based API. The correct pattern is:

1. Subclass `RoundTripConstructor` (since `ConfigLoader` uses `YAML()` which defaults to round-trip mode)
2. Register the constructor on the subclass via `SubClass.add_constructor(tag, method)`
3. Assign the subclass to `yaml_instance.Constructor`

### Content-Type Detection Logic

```python
def _try_parse_yaml(content: str) -> Any:
    """Try to parse content as YAML. Return parsed data or raw string."""
    try:
        sub_yaml = YAML()
        # Share the same constructor class for nested !file support
        sub_yaml.Constructor = self._yaml.Constructor
        parsed = sub_yaml.load(content)
        if isinstance(parsed, (dict, list)):
            return parsed
        # Scalar YAML (e.g., a file containing just "hello") → return as string
        return content
    except YAMLError:
        # Not valid YAML → return as raw string
        return content
```

### Cycle Detection Strategy

Use a **set of resolved absolute path strings** tracked on the constructor class:

- Before parsing a `!file`, resolve the path to absolute and check if it's in the set
- If present → raise `ConfigurationError` with the cycle chain
- If not → add it, parse, then remove it after parsing completes
- This naturally handles nested `!file` chains: A → B → C (each is added during traversal)
- `Path.resolve()` canonicalizes symlinks, preventing symlink-based cycle evasion

### Base Directory Management for Nested Includes

When processing a nested `!file`:
1. Save current `_base_dir`
2. Set `_base_dir` to the included file's parent directory
3. Parse the included file (which may trigger more `!file` constructors)
4. Restore `_base_dir` to the saved value

This ensures each level of nesting resolves paths relative to its own file location.
