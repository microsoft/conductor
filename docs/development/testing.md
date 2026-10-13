# Testing

How Conductor's test suite is organized and selected, the hermetic rules every test must obey,
the conventions for building workflows in tests, and the load-bearing tests that encode a
non-obvious rule and must not be weakened.

## When to read

- Writing, moving or deleting any test under `tests/`.
- Editing `tests/conftest.py`, a package-level `conftest.py`, or the pytest markers in
  `pyproject.toml`.
- Adding a test that touches the home directory, the temp directory, run records, a real CLI
  process, git, Docker or the network.
- Writing a test that scans source code instead of exercising behavior.

## Required invariants

### Layout and selection

- Tests mirror `src/conductor/`: one test directory per package (`test_cli`, `test_config`,
  `test_engine`, `test_executor`, `test_execution`, `test_providers`, `test_plugins`,
  `test_skills`, `test_fleet`, `test_mcp`, `test_web`, `test_gates`, `test_registry`,
  `test_bundle`, `test_runner`, `test_aca_runner`, `test_telemetry`, `test_interrupt`) and
  top-level test files for top-level modules (for example `tests/test_console.py`,
  `tests/test_filesystem.py`, `tests/test_billing.py`). `tests/test_integration/` holds
  end-to-end runs, including the opt-in real-process tests.
- Put a test beside the code it covers, even for a cross-cutting feature: billing tests live in
  `tests/test_billing.py`, `tests/test_providers/test_claude_agent_sdk_billing.py`,
  `tests/test_engine/test_billing_provenance.py` and `tests/test_fleet/`.
- `asyncio_mode = "auto"` (`pyproject.toml`), so `async def` tests run without a decorator.
- The suite runs under xdist with `--dist loadfile` (`Makefile`). A test MUST NOT depend on
  another file's tests having run, or on any shared writable path outside its `tmp_path`.

### Markers

| Marker | Meaning | How it is selected |
| --- | --- | --- |
| `performance` | Timing-sensitive tests | Deselected by `make test`; a bare `uv run pytest` runs them |
| `real_api` | Real Copilot/Claude/OpenAI processes or network calls | Auto-skipped unless `-m` names it |
| `install_scripts` | Runs `install.sh`/`install.ps1` end to end | Auto-skipped unless `-m` names it; `make test-install-scripts` |
| `docker_integration` | Needs a real Docker daemon | Deselected by `make test`; skips only when no daemon answers |
| `claude_auth_readiness_mocked` | Runs the real claude-agent-sdk auth preflight | Opt-in per test; see hermetic rules |

- `tests/conftest.py::pytest_collection_modifyitems` skips every test carrying a name in
  `_OPT_IN_MARKER_NAMES` (`real_api`, `install_scripts`) unless that name appears as a whole
  word in the `-m` expression. `make test` and CI deselect them explicitly as well.
- Register every marker in `[tool.pytest.ini_options].markers` in `pyproject.toml`. A new
  marker for expensive or host-mutating tests MUST be added to `_OPT_IN_MARKER_NAMES` and to
  `PYTEST_DEFAULT_MARKERS` in `Makefile`, and kept out of the default CI lane in
  `.github/workflows/ci.yml`. `tests/test_config/test_real_api_marker.py` and
  `tests/test_config/test_install_scripts_marker.py` pin the hook with `pytester`.
- A `docker_integration` test may skip only because the daemon is absent (the `docker_daemon`
  fixture in `tests/test_execution/test_docker_integration.py`); any failure against a live
  daemon MUST fail.

### Hermetic environment

- NEVER read or write the developer's real home directory, real temp directory, real run
  records, or a live dashboard token. Four autouse fixtures in `tests/conftest.py` enforce this
  for every test:
  - `_isolate_event_log_root` points `tempfile.gettempdir()` and `TMPDIR`/`TEMP`/`TMP` at
    `tmp_path`.
  - `_isolate_run_records_root` sets `CONDUCTOR_HOME` under `tmp_path` and patches
    `conductor.cli.pid.pid_dir` (the legacy PID directory deliberately ignores
    `CONDUCTOR_HOME`, so both are needed).
  - `_isolated_runs_dir` patches `conductor.rundir.runs_dir` and asserts on every call that it
    never resolves to the real `~/.conductor/runs`.
  - `_stub_claude_auth_readiness` stubs the claude-agent-sdk auth preflight to "ready".
- Do not undo these. A test that needs its own root may patch again with `monkeypatch` (the
  later patch wins) as long as the new root is under `tmp_path`. Set `CONDUCTOR_HOME` rather
  than patching `run_records_dir`, so tests that set the variable themselves still win.
- Code that looks up the home directory takes it as a parameter so tests can pass a fixture
  (for example `src/conductor/skills/discovery.py::discover_skills` takes `home=`). Packages
  whose code reaches `Path.home()` without a parameter isolate `HOME`/`USERPROFILE` with an
  autouse fixture, as `tests/test_plugins/conftest.py` does.
- Tests MUST NOT spawn a real `copilot`, `claude` or other agent CLI outside `real_api` tests.
  Use `CopilotProvider(mock_handler=...)` or patch `provider.execute`.
- A test marked `claude_auth_readiness_mocked` runs the real `_check_auth_readiness`, but
  `asyncio.create_subprocess_exec` is replaced with a guard that fails the test. Such a test
  MUST mock process creation (or `_run_auth_status_subprocess`) itself and control the
  environment (`patch.dict(os.environ, ..., clear=True)` or an explicit
  `EffectiveAuthContext`).
- A test asserting "no credential found" MUST stub every fallback source the code consults
  (environment variables and any CLI it shells out to), or it passes or fails depending on the
  developer's machine.
- `tests/test_conftest_isolation.py` and `tests/test_rundir_isolation.py` pin the isolation
  fixtures themselves.

### Building workflows in tests

Follow `tests/test_engine/test_limits.py` (canonical examples) when constructing a
`WorkflowConfig` programmatically:

- `AgentDef` takes `prompt=` (not `instructions=`), `output={"key": OutputField(type="string")}`
  (a dict, not a list) and `routes=[RouteDef(to=...)]` (models, not raw dicts).
- `WorkflowDef` requires `entry_point=`; `limits=LimitsConfig(...)` goes inside
  `workflow=WorkflowDef(...)`. `agents=` and `output=` are top level on `WorkflowConfig`.
- Build the engine as `WorkflowEngine(config, provider)` and run it with
  `await engine.run({})` (there is no `execute`). Pass `skip_gates=True` where a gate or limit
  prompt would otherwise block.
- For deterministic content use `CopilotProvider(mock_handler=handler)` where
  `handler(agent, prompt, context)` returns the output dict. For controlled token and cost
  data, patch `provider.execute` to return an `AgentOutput` with explicit `input_tokens`,
  `output_tokens` and `model`.
- `AgentDef`, `WorkflowDef`, `WorkflowConfig`, `RouteDef` and `OutputField` forbid extra
  fields, so a misspelled keyword fails at construction (not every model does, for example
  `LimitsConfig`).

### Load-bearing tests

These encode rules that are easy to "simplify" away. Keep their shape.

- Integration over helper tests. `tests/test_skills/test_engine_integration.py`,
  `tests/test_skills/test_executor_integration.py`,
  `tests/test_plugins/test_engine_integration.py` and
  `tests/test_plugins/test_executor_integration.py` assert that skill directories, plugin
  subagents and plugin MCP servers actually arrive at `provider.execute`. A test that builds
  `AgentExecutor` directly supplies what the engine should supply, and a negative assertion
  ("no skills text in the prompt") cannot tell a working native path from a dropped one.
- Markup safety has two layers. `tests/test_cli/test_markup_guards.py` scans the source with
  `ast`; `tests/test_cli/test_markup_injection.py` drives real commands. The scan stops the next
  call site, the behavioral tests prove the output is right; neither replaces the other. See
  [console-output.md](console-output.md).
- Source-scanning tests MUST have negative controls. Each rule is a single predicate called by
  both the scan and a control that feeds it a known violation
  (`TestTheGuardsActuallyDetectViolations`), plus a check that the rule matches something in
  the real source (`TestTheGuardsCoverRealSource`). A scan that matches nothing reports "all
  clear" forever.
- Run the real thing where the risk is in the real thing.
  `tests/test_integration/test_install_script_extras.py` sources `install.sh` (with its trailing
  `main` stripped) and executes `install.ps1` helpers under `pwsh`, then feeds both the same
  receipt. `tests/test_plugins/test_fetch.py` runs real `git` against `file://` repositories:
  annotated-tag dereferencing, shallow SHA fetches and offline fallback are git behaviors a
  mocked `subprocess` cannot prove.
- `tests/test_mcp/test_serve_invoke.py::test_every_observe_command_actually_exists_in_the_cli`
  resolves every suggested `observe` command against the real Typer app, and
  `tests/test_mcp/test_serve_toolgen.py::TestWaitSecondsSteersTowardBackground` asserts both
  hand-written copies of the `_wait_seconds` schema steer identically.
- Provider parity is pinned across providers, not per provider:
  `tests/test_providers/test_output_constraints_parity.py` (output-schema constraints),
  `tests/test_providers/test_base.py::TestSuppressMcpServersContract` (the
  `suppress_mcp_servers` signature on every provider) and
  `tests/test_engine/test_validator_integration.py::TestValidatorRunsWithoutMcpServers` (drives
  the real `OutputValidator`, whose fail-open would hide a helper-level bug).
- `tests/test_integration/test_mcp_roots_negotiation.py` (`real_api`) pins upstream MCP Roots
  behavior that the claude-agent-sdk `settings_dir` design depends on.
- Textual pilot tests have their own deadlock caution (`settle` versus a modal waiting for a
  second keypress); see [fleet-tui.md](fleet-tui.md).

## Change checklist

- [ ] The test lives under the `tests/` path mirroring the code it covers.
- [ ] It writes only under `tmp_path` and does not depend on the developer's home, temp
      directory, installed CLIs, credentials or network.
- [ ] Real processes, network, Docker or host-wide installs are behind the right marker, and a
      new marker is registered and wired into `tests/conftest.py`, `Makefile` and CI.
- [ ] A new source-scanning rule has a shared predicate, a negative control and a
      covers-real-source check.
- [ ] A feature that must reach `provider.execute` has an engine-level test with a positive
      assertion.
- [ ] `make test` (or the targeted file with `PYTEST_WORKERS=0 make test` when debugging
      ordering) passes, and `make lint` is clean on `tests`.

## Rationale and evidence

- Temp isolation: the startup retention sweep (`maybe_prune_event_logs`, on by default) deleted
  real `conductor replay` material from the developer's temp directory when a single wiring
  test ran unisolated.
- Run-record isolation: `src/conductor/fleet/records.py::read_run_records` is a pruning reader
  that deletes records it judges corrupt or dead, so an unisolated `stop`/`fleet list`/retention
  test deletes live records.
- Runs-dir isolation: a module that never opted in (`tests/test_cli/test_markup_injection.py`)
  read the real `~/.conductor/runs` and, with a dashboard running, its live token (#397).
- Plugin home isolation: a test asserting a missing plugin passed vacuously because an
  installed marketplace satisfied it.
- Auth stub: the claude-agent-sdk preflight can spawn `claude auth status`; unstubbed, results
  depend on whether the machine is logged in.
- Opt-in markers: `real_api` tests spawn real agent processes (#326); `install_scripts` tests
  build wheels and run the installers.
- Negative controls: one markup-guard control had already drifted from the rule it claimed to
  control before predicates were shared (#406).
- Install-script tests execute rather than grep because the failures are quoting and `sed`
  mistakes that reading the file cannot catch (#441).

## Related references

- [Development README: commands](README.md)
- [Console output](console-output.md)
- [Architecture](architecture.md)
- [Plugins and skills](plugins-and-skills.md), [providers](providers.md),
  [MCP server](mcp-server.md), [fleet TUI](fleet-tui.md)
