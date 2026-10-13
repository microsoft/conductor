# MCP Server (`conductor mcp serve`)

Contributor invariants for the MCP server that exposes Conductor workflows as tools
(`src/conductor/mcp/serve/`, CLI entry `src/conductor/cli/mcp.py`), and for the `mcp:` workflow
block it reads. For host configuration, the exposure ladder, toolsets, and the run lifecycle as a
user sees them, read the [MCP server user guide](../mcp-server.md). The MCP *client*
(`src/conductor/mcp/manager.py`, `runtime.mcp_servers`) is a separate subsystem.

## When to read

- Editing anything under `src/conductor/mcp/serve/` or `src/conductor/cli/mcp.py`.
- Changing `McpConfig` in `src/conductor/config/schema.py`.
- Bumping the `mcp` dependency in `pyproject.toml`.
- Changing run records, terminal records, or `launch_background`, which the server consumes.

## Required invariants

### The server owns no execution state

- The server is a protocol adapter over three existing subsystems and MUST NOT duplicate them:
  the registry (catalogue), `src/conductor/cli/bg_runner.py::launch_background` (every
  invocation forks a real detached `conductor run`; NEVER execute a workflow in-process), and
  `src/conductor/fleet/` (every status/event/log query reads the same run records, terminal
  records, and event logs as the TUI).
- `src/conductor/mcp/serve/invoke.py::LaunchTracker` enforces `--max-concurrent-runs` in-process
  and resets on restart; that is intended, not a gap.
- `src/conductor/mcp/serve/runs.py::resolve_run` answers a `run_id` from exactly three sources in
  order, and reports which one answered: live run record, else
  `src/conductor/fleet/records.py::TerminalRunRecord`, else an event-log fallback for a crashed
  run.

### Inputs come only from the operator (NFR3)

- No tool accepts a filesystem path, URL, or registry source. Every such value is a startup
  argument on the frozen `src/conductor/mcp/serve/options.py::ServeOptions`; a value not on it did
  not come from the operator. Keep new knobs on `ServeOptions`, never as tool parameters.
- The launch directory follows the same rule (#544): resolved once at startup by
  `src/conductor/mcp/serve/launch_dir.py` (`--launch-dir`, else the nearest Copilot host ancestor's
  cwd via `psutil`, else the server's own cwd) and passed as `cwd=options.launch_dir` to
  `launch_background`. Never re-evaluate it per call and never `os.chdir()`.

### Catalogue and `tools/list`

- `src/conductor/mcp/serve/catalogue.py::build_catalogue` builds an immutable list of
  `mcp.types.Tool` that is byte-identical across connections (DD3). Toolsets (`workflows`, `runs`
  by default; `introspect`, `diagnose` opt-in; `src/conductor/mcp/serve/options.py`) are decided
  once at startup, never per request.
- Exposure ladder, highest precedence first: `--deny` > `--allow` > workflow `mcp.expose` >
  default on (DD4).
- Schema resolution falls back to a permissive `degraded` entry rather than silently dropping a
  workflow (NFR2); degraded entries are listed in the stderr startup summary.
- Every exposed workflow is pinned to an immutable identity (commit SHA or content hash,
  `src/conductor/mcp/serve/pinning.py`) (DD6).
- At every server start, a configured GitHub registry resolves its current default branch and pins
  the catalogue to that commit (#596), reusing cached content at that SHA. If resolution fails, the
  server falls back to the last recorded SHA and logs a warning that includes the original
  `RegistryError`, so an auth failure or rate limit is visible rather than silently serving a stale
  catalogue. The running catalogue never refreshes; a restart adopts registry changes
  (`tests/test_mcp/test_serve_catalogue.py::TestOnlineDefaultRef`).
- Registry index resolution runs inside
  `src/conductor/mcp/serve/catalogue.py::_SchemaBudget.paused`, so network time NEVER counts
  against the schema-resolution budget; a slow lookup must not starve later registries or
  `--workflow-dir` files into permissive `degraded` entries
  (`tests/test_mcp/test_serve_catalogue.py::TestSchemaBudget`).
- Names are bare and registry-qualified only on collision (`src/conductor/mcp/serve/naming.py`,
  DD10); descriptions are sanitized (`src/conductor/mcp/serve/sanitize.py`, NFR4); input schemas
  come from `WorkflowDef.input` and there is NO `outputSchema`
  (`src/conductor/mcp/serve/toolgen.py`, DD5).
- Above `--max-direct-tools`, the server serves the two discovery tools instead
  (`conductor_find_workflow` / `conductor_run_workflow`, `src/conductor/mcp/serve/discovery.py`,
  FR9).

### Transport

- stdout is the JSON-RPC transport (DD9). NOTHING else may write to it: the startup summary and
  all diagnostics go to `make_console(stderr=True)` in `src/conductor/mcp/serve/server.py` and
  `src/conductor/cli/mcp.py`. stdio is the only transport.
- `src/conductor/cli/mcp.py` imports `conductor.mcp.*` (and therefore the `mcp` SDK and `psutil`)
  only inside `serve()`. Pinned by `tests/test_cli/test_mcp_serve.py::TestLazyImportBoundary`;
  see [CLI and runs](cli-and-runs.md).

### Gates, waiting, and observability

- `launch_background` MUST be called with `skip_gates=False`, always (DD11). It is the named
  constant `_NEVER_SKIP_GATES` in `src/conductor/mcp/serve/invoke.py` with an assertion; never
  replace it with a literal or a parameter. A gate parks and returns an approval URL.
- Non-blocking is the intended path. The reserved `_wait_seconds` parameter's description MUST
  lead with "leave this unset", say the run is detached either way, and say setting it blocks
  only the caller's own call. `src/conductor/mcp/serve/server.py::_DISCOVERY_TOOLS` hand-writes a
  second copy of that schema; both copies MUST steer identically
  (`tests/test_mcp/test_serve_toolgen.py::TestWaitSecondsSteersTowardBackground`).
- `resolve_wait_seconds` in `src/conductor/mcp/serve/invoke.py` caps every blocking path at
  `--max-wait-seconds`, including an omitted `_wait_seconds` on a `mode: sync` workflow;
  `conductor_await_run` (`src/conductor/mcp/serve/runs.py`) applies the same ceiling. So
  `--max-wait-seconds 0` guarantees no invocation blocks. `mode: auto` behaves like `async`.
- The returned handle carries `port`, an `observe` block of terminal commands, and the
  capture-log paths; the immediate `next` action names `conductor fleet` before
  `conductor_await_run`, conditioned on the user having asked to block. Every `observe` command
  MUST exist in the real CLI
  (`tests/test_mcp/test_serve_invoke.py::test_every_observe_command_actually_exists_in_the_cli`).
- `conductor_run_events` reduces tool payloads to `{name, status, byte_size}` unless
  `--introspect-full` (R4, `src/conductor/mcp/serve/introspect.py`). `conductor_run_logs` and the
  other diagnose tools return `ResourceLink`s and bounded metadata only, never file contents
  (DD12, `src/conductor/mcp/serve/diagnose.py`).

### The `mcp:` workflow block

- `src/conductor/config/schema.py::McpConfig` is typed with `extra="forbid"`, so a typo is a
  schema error in `conductor validate`, not silence (FR11). NEVER move these fields into the
  untyped `metadata:` dict. Fields: `expose` (default `true`), `mode` (`async`/`sync`/`auto`,
  default `async`), `read_only` / `destructive` (surfaced as `readOnlyHint`/`destructiveHint`),
  `estimated_minutes` (positive). Example: `examples/mcp-serve.yaml`.

### Dependency bound

- `pyproject.toml` pins `mcp>=1.28.1,<2`. The bound also protects the MCP client in
  `src/conductor/mcp/manager.py`: `mcp` 2.0 renames camelCase attributes (for example
  `Tool.inputSchema`), which would become runtime `AttributeError`s with no import error.
  Pinned by `tests/test_mcp/test_sdk_bound.py`.

### R1: completed runs are a CLI-wide scope change

- Every run, MCP-launched or not, writes a terminal record, so `conductor status`,
  `conductor fleet list`, and the TUI History screen list completed runs; `--live` restores the
  old scope. Treat terminal records as shared infrastructure owned by
  [CLI and runs](cli-and-runs.md), not as an MCP-only artifact.

## Change checklist

- [ ] New tool or tool parameter: no path/URL/registry inputs; add any operator knob to
      `ServeOptions` and the CLI.
- [ ] Any change to `_wait_seconds` wording: update both copies and keep the steering test green.
- [ ] Any change to `observe` commands: the CLI-existence test must still pass.
- [ ] New output on the server path: stderr console only.
- [ ] New `mcp:` field: add it to `McpConfig`, `docs/mcp-server.md`, and an example; run
      `make validate-examples`.
- [ ] Run `uv run pytest tests/test_mcp tests/test_cli/test_mcp_serve.py`.

## Rationale and evidence

**Adapter only.** Owning execution would fork the run lifecycle into a second implementation that
`conductor stop`, `status`, and the TUI could not see.

**Steering `_wait_seconds`.** With three equally weighted options and no recommendation, a model
passed `_wait_seconds: 300` unprompted and held its turn for a detached five-minute run, which
reads to a user as "the MCP server runs workflows in the foreground".

**Real `observe` commands.** A plausible invented command (`conductor fleet list --json`) shipped
in a draft and would have failed in front of the user exactly when told how to watch a run.

**Never skip gates (DD11).** A gate is a control the author placed in the path; auto-selecting it
on the caller's behalf is one parameter away from rubber-stamping every gate.

## Related references

- [MCP server user guide](../mcp-server.md), including its
  [limits](../mcp-server.md#limits) and [launch directory](../mcp-server.md#launch-directory)
- [CLI reference: `conductor mcp serve`](../cli-reference.md#conductor-mcp-serve)
- [MCP server design](../projects/mcp-server/conductor-mcp.design.md)
- [CLI and runs](cli-and-runs.md), [Fleet TUI](fleet-tui.md), [testing](testing.md)
