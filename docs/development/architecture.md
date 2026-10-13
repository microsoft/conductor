# Architecture

How Conductor is layered: the path from a workflow YAML file to its output, which package owns
what (and which guide holds its rules), and the cross-cutting module rules (leaf modules, events,
exceptions, lazy imports) that keep imports acyclic and CLI startup cheap.

## When to read

- Adding a module or package under `src/conductor/`, or moving code between packages.
- Adding an import from one package into another, especially into or out of `conductor.cli`.
- Adding an optional third-party dependency or an expensive import.
- Adding an event type, an event subscriber, or an exception class.
- Finding which guide owns an area.

## Required invariants

### Execution flow

1. `src/conductor/config/loader.py` parses YAML (environment-variable substitution, `!file` and
   `!yamlfile` includes) into a `WorkflowConfig` (`src/conductor/config/schema.py`).
2. `src/conductor/engine/workflow.py::WorkflowEngine` is built from the config and a provider
   (or provider registry).
3. The engine loop picks the current step, executes it, evaluates its routes, and moves on. Step
   kinds: agent, human gate, questions, script, mcp, wait, set, terminate, sub-workflow, plus
   parallel groups and for-each groups (`StepDef` and `ForEachDef` in
   `src/conductor/config/schema.py`).
4. Parallel groups run their members concurrently against a deep-copied snapshot of the context.
5. For-each groups resolve `source` at run time and inject the loop variable named by `as:`,
   plus `_index` and, with `key_by`, `_key`. Those names and `workflow`/`context`/`output` are
   reserved (`ForEachDef.validate_loop_variable`).
6. Script steps render their command fields and run through a runner backend (local subprocess
   or Docker), exposing `stdout`, `stderr` and `exit_code` to the context. See
   [execution-backends.md](execution-backends.md).
7. Set steps go through `src/conductor/engine/workflow.py::WorkflowEngine._run_set_step` in all
   three positions (main loop, parallel member, for-each item): no LLM, no subprocess, `output:`
   schema enforced, `set_started`/`set_completed`/`set_failed` emitted.
8. Wait steps sleep, cancellable by interrupt or the workflow timeout. The context receives
   exactly `{"waited_seconds": float}` (#218); other metadata goes only in `wait_completed`.
9. Routes are evaluated by `src/conductor/engine/router.py::Router`: Jinja2 condition first,
   simpleeval arithmetic fallback; the first matching `when` wins and a route without `when`
   always matches.
10. The final result is rendered from the workflow `output:` templates (or a terminate step's
    `output_template`).

Step semantics and their invariants live in [workflow-engine.md](workflow-engine.md).

### Static validation is not the run path

- `conductor run` and `conductor resume` never call
  `src/conductor/config/validator.py::validate_workflow_config`; only `conductor validate`
  does. A rule that must hold at run time MUST also be enforced on the run path (schema model,
  executor or provider), not only in the validator. Several features enforce the same rule
  twice for this reason.

### Package map

| Package under `src/conductor/` | Owns | Guide |
| --- | --- | --- |
| `cli/` | Typer commands, run/resume orchestration, background launch, stop/status | [cli-and-runs.md](cli-and-runs.md) |
| `config/` | Schema, loader, cross-reference validator, instruction discovery, environment documents | [workflow-engine.md](workflow-engine.md) |
| `engine/` | Engine loop, context, router, limits, checkpoints, guidance, usage and pricing | [workflow-engine.md](workflow-engine.md) |
| `executor/` | Per-step executors (agent, script, set, wait, mcp, questions), templates, output validation | [workflow-engine.md](workflow-engine.md) |
| `gates/`, `interrupt/` | Human and dialog gates, Esc/Ctrl+G keyboard interrupt | [workflow-engine.md](workflow-engine.md) |
| `execution/`, `bundle/` | Runner-backend seam (local, Docker), content-addressed run bundles | [execution-backends.md](execution-backends.md) |
| `providers/` | `AgentProvider` implementations, capability descriptors, factory | [providers.md](providers.md) |
| `runner/`, `aca_runner/` | Remote agent-runtime wire protocol and its reference runner | [providers.md](providers.md) |
| `plugins/`, `skills/` | Plugin and skill resolution, plugin sources and fetching | [plugins-and-skills.md](plugins-and-skills.md) |
| `mcp/` | MCP client used by providers, and `conductor mcp serve` | [mcp-server.md](mcp-server.md) |
| `fleet/` | Run records, run summaries, history, retention, Textual TUI | [fleet-tui.md](fleet-tui.md) |
| `web/` | Dashboard server, auth middleware, React frontend and built assets | [dashboard.md](dashboard.md) |
| `registry/` | Workflow registries and their cache | [cli-and-runs.md](cli-and-runs.md) |
| `telemetry/` | Optional OpenTelemetry tracing driven by events | [telemetry user doc](../telemetry.md) |
| top-level modules | Leaf helpers, events, exceptions, settings | this guide |

The bundled `conductor` skill and plugin manifest under `plugins/conductor/` are owned by
[plugins-and-skills.md](plugins-and-skills.md).

### Leaf modules

- These top-level modules are leaves and MUST NOT import anything from `conductor`, not even
  inside a function: `src/conductor/duration.py`, `src/conductor/filesystem.py`,
  `src/conductor/run_id.py`, `src/conductor/rundir.py`, `src/conductor/billing.py`,
  `src/conductor/digest.py`, `src/conductor/redaction.py`, `src/conductor/install_hint.py`,
  `src/conductor/templating.py`, `src/conductor/file_string.py` (stdlib only),
  `src/conductor/console.py` (stdlib plus rich) and `src/conductor/frontmatter.py` (stdlib plus
  ruamel.yaml).
- Put a helper that more than one layer needs into a leaf module. NEVER make `engine/`,
  `executor/`, `providers/`, `gates/`, `web/` or `fleet/` import `conductor.cli` at module
  level. The existing verbose-logging bridges (for example in
  `src/conductor/executor/agent.py` and `src/conductor/executor/wait.py`) use a deferred
  import inside the function and say why; do not add new ones when a leaf will do.
- `src/conductor/cli/__init__.py` imports `conductor.cli.app`, which imports every CLI sub-app
  and reaches `conductor.web.server`. Importing any `conductor.cli` module (for example
  `conductor.cli.pid`) therefore pulls in the whole CLI and can close an import cycle.
- Reach patchable leaf functions through the module attribute, as in
  `from conductor import rundir; rundir.runs_dir()` (`src/conductor/web/auth.py`,
  `src/conductor/cli/pid.py`), so a test that patches `conductor.rundir.runs_dir` reaches every
  caller.
- `src/conductor/duration.py::parse_duration` accepts `int`/`float` seconds or a string with an
  optional `ms`/`s`/`m`/`h` suffix, rejects booleans, and raises `ValueError` so it nests inside
  a Pydantic validation error. Bounds (positive, caps) belong in the caller.
- Path diagnostics that must tell "does not exist" from "could not be read" MUST use
  `src/conductor/filesystem.py` (`stat_or_none`, `exists_strict`, `is_dir_strict`,
  `is_file_strict`) instead of `Path.exists()`/`is_dir()`/`is_file()` (#540). The probes follow
  symlinks and treat a non-encodable path as absent, like pathlib did. Covered by
  `tests/test_filesystem.py`.

### Events

- `src/conductor/events.py::WorkflowEventEmitter` decouples execution from rendering: the
  engine emits `WorkflowEvent`s and the console subscriber, the JSONL event log
  (`src/conductor/engine/event_log.py`), the web dashboard and telemetry subscribe.
- Subscribers run synchronously inside `emit`, in registration order; one that raises is
  logged and the rest still run. Keep subscribers cheap and non-blocking.
- `WorkflowEventEmitter.set_redactor` attaches the run-scoped `RunRedactor`
  (`src/conductor/redaction.py`). While it is active every subscriber receives a scrubbed copy
  and the caller's event is never mutated; while inactive the original object passes through.
  Any sink that writes run data without going through the emitter MUST scrub it itself (see
  [execution-backends.md](execution-backends.md) for secrets).

### Exceptions

- Every Conductor error derives from `src/conductor/exceptions.py::ConductorError`, which
  carries an optional `suggestion` (plus `file_path`/`line_number`) shown to the user. Raise an
  existing subclass (`ConfigurationError`, `ValidationError`, `TemplateError`, `ProviderError`,
  `ExecutionError` and its children, `CheckpointError`, ...) or add one under it; do not start
  a parallel hierarchy.
- `conductor.exceptions.ValidationError` and `TimeoutError` shadow Pydantic's and the builtin
  names. Import them explicitly and alias Pydantic's (`PydanticValidationError`) where both
  appear.

### Lazy imports and optional dependencies

- `src/conductor/cli/app.py` imports every CLI sub-app module on every invocation. A sub-app
  whose command needs a heavy package (the `mcp` SDK for `conductor mcp serve`, the bundle
  collector for `conductor bundle build`) MUST import it inside the command body, as
  `src/conductor/cli/mcp.py` and `src/conductor/cli/bundle.py` do.
- An optional SDK behind a `pyproject.toml` extra is imported in a `try`/`except ImportError`
  that sets a module-level availability flag (for example `TEXTUAL_AVAILABLE` in
  `src/conductor/cli/fleet.py`, `CLAUDE_AGENT_SDK_AVAILABLE` in
  `src/conductor/providers/claude_agent_sdk.py`). Check the flag where the feature is used and
  fail with an install hint from `src/conductor/install_hint.py::install_command`; NEVER
  hardcode a `pip install` command.

## Change checklist

- [ ] New top-level helper shared across layers: make it a leaf, list it above, and add a
      top-level unit test module for it under tests/.
- [ ] New package: add a row to the package map naming the owning guide.
- [ ] New import edge into `conductor.cli` from a lower layer: replace it with a leaf helper, or
      justify a deferred import in a comment.
- [ ] New optional dependency: add the extra to `pyproject.toml`, guard the import with an
      availability flag, and surface `install_command(...)` on failure.
- [ ] New event type: update every consumer that switches on event type (console subscriber in
      `src/conductor/cli/run.py`, the dashboard store, replay filters; see
      [dashboard.md](dashboard.md)) and make sure redaction still covers the payload.
- [ ] New run-time rule: enforce it on the run path, not only in `config/validator.py`.

## Rationale and evidence

- Leaf modules exist because `conductor.cli.__init__` re-exports the Typer app. `rundir.py`
  documents the cycle concretely (`web/` -> `cli.pid` -> `cli.app` -> `web.server`), and
  `run_id.py` (#435) consolidated two divergent copies of the run-id rule that had let a
  resumed run be killed by its own launcher. `console.py` sits at the top level, not under
  `cli/`, because `gates/` and `providers/` need it.
- `filesystem.py` (#540): Python 3.14 made `Path.exists()`/`is_dir()`/`is_file()` swallow every
  `OSError`, including `PermissionError`; 3.13 and earlier re-raised anything outside
  `ENOENT`/`ENOTDIR`/`EBADF`/`ELOOP` (and a few Windows codes). Without the strict probes an
  unreadable skill or plugin path is reported as missing, and an unreadable skills-root child
  is misfiled as a near-miss.
- Validate-versus-run: `conductor run` skipping the validator is why capability refusals for
  skills, plugins and providers are duplicated in executors and providers.
- Lazy imports keep `conductor --help` and every unrelated command from paying for SDK imports.

## Related references

- [Workflow syntax](../workflow-syntax.md)
- [Telemetry](../telemetry.md)
- [Configuration](../configuration.md)
- [Testing](testing.md), [console output](console-output.md), [workflow engine](workflow-engine.md)
- [Run bundle design](../design/run-bundle.md),
  [runner protocol design](../design/runner-protocol.md)
