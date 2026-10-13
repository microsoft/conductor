# Claude Agent SDK provider

Deep notes for `src/conductor/providers/claude_agent_sdk.py`, the canonical experimental provider.
It delegates the agentic loop to the `claude` CLI through the `claude-agent-sdk` package and reaches
event and output parity, but many behaviors are owned by the CLI and must be constrained from the
outside. Read [providers](../providers.md) first for the shared contract.

## When to read

- Editing `src/conductor/providers/claude_agent_sdk.py`.
- Editing `claude-agent-sdk`-specific validation in `src/conductor/config/validator.py` or
  `ProviderSettings` fields `native_tools`, `setting_sources`, `auth_mode` in
  `src/conductor/config/schema.py`.
- Changing `settings_dir`, `session_key`, skills, or plugins handling for this provider.
- Bumping the `claude-agent-sdk` floor in `pyproject.toml` (currently `>=0.2.82`).

## Required invariants

### Retries and errors

- The SDK does NOT retry API failures (429, 5xx, network); its retry covers filesystem operations
  only. Wrap SDK errors in `ProviderError` with `is_retryable` derived from `stop_reason` / error
  subtype so workflow `retry:` drives all retries.
- A CLI launch failure is deterministic and MUST be non-retryable with a path-oriented hint:
  `_classify_startup_failure` special-cases the SDK's `CLIConnectionError` for a missing working
  directory and for `not a directory` / `permission denied`; otherwise `CLIConnectionError` would
  yield firewall advice and `is_retryable=True`.

### Session lifecycle: `ClaudeSDKClient` and phases S0-S5

- Drive the SDK through `ClaudeSDKClient` (`connect` / `query` / `receive_response` / `disconnect`),
  never the one-shot `query()` helper, whose stream tears itself down inside the caller's
  `__anext__` so pre-empting a blocked read can strand a live CLI.
- `max_session_seconds` is one absolute `time.monotonic()` deadline fixed at session start and never
  recomputed.
- Phases in `_execute_session`:
  - S0 pre-flight short-circuits before anything is constructed (interrupt checked first).
  - S1 connect is never cancelled by a signal (`_await_uncancellable`): before `Query` exists
    `disconnect()` has nothing to close through. A signal raised during startup waits for startup.
  - S2 send and S3 receive are raced against the interrupt and the deadline by `_race_signals`,
    which takes a factory so a short-circuit never creates work it would abandon.
  - S4 shutdown runs `disconnect()` as an owned, shielded task (`_shutdown_sdk_client`), never
    raced, retried once sequentially if it did not return. The retry relies on private SDK state
    (`ClaudeSDKClient` clears `_query` only after `close()` completes), pinned by
    `tests/test_providers/test_claude_agent_sdk_blackbox.py`.
  - S5 removes the MCP config file in an inner `finally`, so it runs even when S4 re-raises.
- Outcome precedence: outer cancellation > an SDK error that already completed > interrupt > timeout
  > message. A failed shutdown never replaces the first four, but on an otherwise successful run it
  MUST raise a non-retryable `ProviderError` and discard the result.
- Reaping (`_cancel_and_reap` / `_reap_shielded`) MUST combine `asyncio.shield()` with
  `Task.cancelling()`: cancel every owned task before awaiting any, reap each through a fresh
  shield, re-raise the first caller cancellation only after all are reaped. Do not use
  `Task.uncancel()` or add a cleanup timeout. `_reap_shielded(describe=None)` hands the exception
  back to the caller (a failed connect is the primary error); a name logs and drops it.
- Never claim bounded teardown: some SDK waits, including the final process wait, are not timeout
  bounded, and tests use a fake `Transport`, not a real CLI.
- Known deltas from the client API: the stream ends at the first `ResultMessage`, and stdin EOF is
  sent by `disconnect()`.

### MCP servers (#335)

- `_translate_mcp_servers` maps workflow servers to the SDK stdio/http/sse shapes once in
  `__init__`; plugin servers are translated per call. Providers are built lazily, so a bad server
  config surfaces when the first agent runs, not at `conductor validate`.
- The config MUST be written to a `0600` temp file (`_write_mcp_config`, `{"mcpServers": {...}}`
  envelope) and passed by path, inside `execute`'s `try`. Passing a dict would put resolved env
  values and `Authorization` headers in CLI argv. The `finally` closes the response iterator,
  completes shutdown (S4), then removes the file; on double shutdown failure the file is removed
  anyway.
- `strict_mcp_config=True` unconditionally, even with no declared servers, so the CLI never loads
  project `.mcp.json`, user, or plugin servers.
- A narrowing per-server `tools:` filter (anything but `["*"]`) MUST be refused, not ignored; a
  dropped `timeout` only warns.

### Tools policy and `native_tools`

- Per-agent `tools:` allowlists are unsupported (`workflow_tools_passthrough=False`); a non-empty
  list is refused because workflow tool names do not map to CLI tool ids, and SDK `allowed_tools` is
  a permission list, not an availability filter.
- `_resolve_tool_config` reads the raw `agent.tools` (the executor erases omitted-vs-`[]`):
  - omitted + `native_tools: none` (default): `tools=[]` (or `["Skill"]` with skills),
    `permission_mode="dontAsk"`;
  - omitted + `native_tools: claude_code`: the `claude_code` preset with `bypassPermissions`;
  - explicit `tools: []` under either value: `[]`/`["Skill"]` with `dontAsk`.
- `dontAsk`, not `None`: `None` leaves the CLI on `default`, which prompts, and a non-interactive
  run would hang.
- The string shorthand and YAML `null` resolve to `none`; `has_structured_config()` counts only
  `claude_code`. `create_provider` falls back to `none` when settings are absent (bare `--provider`
  or per-agent override), and `__init__` refuses any other value (`_NATIVE_TOOLS_VALUES`).
- Under `dontAsk`, `_mcp_permission_rules` assigns `allowed_tools` one `mcp__<server>__*` rule per
  declared workflow and plugin server, never `*` or `mcp__*`, and never under `bypassPermissions`.
  Server names MUST match `MCP_PERMISSION_SAFE_NAME` in `src/conductor/plugins/manifest.py`,
  enforced at run time and by `_check_mcp_permission_names` in `src/conductor/config/validator.py`.
- Explicit `tools: []` with any workflow or plugin MCP server is refused under either `native_tools`
  value: `_check_agent_tools` at validate, `_refuse_explicit_no_tools_with_mcp` at run time against
  the merged map. Both run-time refusals happen before `_write_mcp_config`, so a refused config
  never puts credentials on disk. The supported MCP-only form is omitted `tools:` under `none`.
- Plugin subagents with no built-ins are refused before any session starts (no grantable dispatch
  tool). The remedy names the actual cause (agent `tools: []` vs workflow `native_tools: none`) and
  offers `native_tools: claude_code`, `agents: false` plus `skills: false` when that plugin's skills
  are on, or `copilot`. Enforced by `_resolve_tool_config` and
  `_check_claude_agent_sdk_subagent_dispatch`.
- `claude_code` is announced by one fixed line in the experimental banner
  (`src/conductor/cli/run.py::_native_tools_claude_code_warning`), fed by `native_tools` in the
  `providers` block of `WorkflowEngine.build_workflow_started_data`; it inherits the banner's
  once-per-run latch (`_PRINTED_EXPERIMENTAL_BANNERS`, keyed `run_id:provider`) and `--silent`
  gating.

### Runtime config and working directory (#348)

- `temperature` and `max_tokens` are rejected at the factory; `reasoning_effort=None` (not wired).
- The engine-resolved `working_dir` is forwarded verbatim as `ClaudeAgentOptions.cwd`; do not
  re-resolve (it would collapse symlink aliases the engine preserves). Build options inside
  `execute`'s `try` so an `os.getcwd()` failure becomes a `ProviderError`. There is no provider-side
  `is_dir()` guard; `_classify_startup_failure` covers the race.
- Stdio MCP servers inherit cwd from the CLI process; do not stamp per-server cwd (the SDK stdio
  config has none).
- cwd keys the CLI transcript directory and is where `.mcp.json`/`.claude/` would load from;
  `strict_mcp_config=True` and default-empty `setting_sources` keep both out.

### `settings_dir` (`add_dirs`)

- `AgentDef.settings_dir` is the only source of `ClaudeAgentOptions.add_dirs`, per agent only. Never
  derive `add_dirs` from MCP server arguments.
- It exposes the named directory's `.claude/skills` only; `CLAUDE.md`, rules, settings (env, hooks),
  and agents still follow cwd. Whenever the session has built-in file tools (`claude_code` preset)
  it also widens `Read`/`Edit`/`Bash` to that tree; it never widens an MCP server.
- Gated by `capabilities.settings_dir`, refused on other providers twice (validator and
  `AgentExecutor._reject_unsupported_settings_dir` in `src/conductor/executor/agent.py`).
- When the `project` tier is not enabled for the agent, both the validator and
  `_warn_settings_dir_without_project_tier` warn, wording the effect by tool policy (grant under the
  preset, no effect otherwise). The run-time latch key is `(settings_dir, cause, built-in file
  tools)`.
- `WorkflowEngine._resolve_agent_directory` resolves both `working_dir` and `settings_dir`; the
  value is emitted on `agent_started` / `parallel_agent_started` / `for_each_agent_started`.
- `tests/test_integration/test_mcp_roots_negotiation.py` (`real_api`) pins the upstream MCP Roots
  behavior this design rests on.

### Skills (#352) and settings tiers (#501)

- Native (`supports_native_skills=True`) through the plugin surface: `_resolve_skill_plugins` maps
  each directory to its owning plugin via `src/conductor/skills/registry.py::resolve_skill_plugin`,
  then sets `plugins=[{"type": "local", "path": root}]` and `skills=["<plugin>:<skill>"]`.
- The resolver is strict: bounded upward walk, skill under the candidate's `skills/`, `SKILL.md`
  present with frontmatter `name` equal to the directory name, name in `[A-Za-z0-9_.-]+`. Two
  plugins claiming one qualified name are refused. All such errors are `is_retryable=False`. A skill
  under no plugin is refused statically by the validator.
- `setting_sources=[]` by default; opt in via `runtime.provider.setting_sources`. `skills=None` and
  `skills=[]` differ upstream, and setting `skills` with `setting_sources=None` makes the SDK
  default to `["user", "project"]`, so both options MUST be set together. The `[]` is invisible in
  argv, so argv tests are paired with options-level assertions.
- An enabled tier loads that tier's hooks too. With a tier and no Conductor-named skills,
  `_resolve_skill_filter` returns `"all"` (otherwise tier skills would be hidden). A per-agent
  `skills: []` opts that agent out of the tier, hooks included.
- An explicit `tools: []` would send `--tools ""` and hide skills, so `_resolve_tool_config` grants
  back only `Skill`; the SDK auto-allows `Skill(<name>)` (bare `Skill` on the tier path).
- `tests/test_skills/test_executor_integration.py::TestSkillDirectoriesReachTheProvider` asserts
  directories reach `execute`; a negative "no `<skills>` in prompt" check cannot.

### Plugins (#378)

- Subagents become inline `ClaudeAgentOptions.agents` via `_build_sdk_agents` (`None`, not `{}`,
  when there are none); plugin MCP servers use the same temp-file path.
- Registering a plugin root to reach its skills also exposes its commands, agents, and hooks, so
  `agents: false` with `skills: true` for one plugin is refused (validator and
  `AgentExecutor._reject_unfilterable_agents`), and the `hooks/` warning says "exposed to the CLI".

### Session continuity (`session_key`)

- `_session_ids` is keyed by `(session_key, cwd)`; `_resolve_resume_session` prefers this run's id
  over a restored one, passes it as `resume`, and sets `fork_session=False`.
- Only `AssistantMessage` / `ResultMessage` (`_SESSION_ID_MESSAGES`) update the map.
- Transcript guard is load-bearing: resume only when `_session_transcript_exists` finds
  `<CLAUDE_CONFIG_DIR or ~/.claude>/projects/<project_key_for_directory(cwd)>/<id>.jsonl`, falling
  back to `get_session_info` only when its recorded cwd realpath-matches; any exception means "start
  fresh".
- Checkpoint keys are namespaced `claude-agent-sdk:["<key>", "<cwd>"]` via `get_session_ids` /
  `set_resume_session_ids`; unrecognized or malformed entries are skipped. `checkpoint_resume` stays
  `False`.
- Keys are literal: `AgentDef.validate_session_key` rejects Jinja. They are rejected on non-agent
  step types, gated on `session_continuity` (validator and `_reject_unsupported_session_key`), and
  refused when two concurrent executions share `(session_key, working_dir)`.
- Tests: `tests/test_providers/test_claude_agent_sdk_session_key.py`,
  `tests/test_integration/test_session_key_continuity.py`.

### Authentication mode (`auth_mode`)

- `EffectiveAuthContext` is the single source of truth. `execute()` and `validate_connection()` call
  `_capture_auth_context` once; it snapshots env, cwd, effective and configured `setting_sources`,
  and `_find_claude_cli()`, and derives `finalized_child_env`. Environment fields are `repr=False`
  and MUST never be logged or serialized.
- `_check_auth_readiness` reads only the context. `_run_auth_status_subprocess` takes CLI path, env,
  cwd, and tiers as required arguments and runs `claude --setting-sources=<csv> auth status --json`
  with the flag always sent and placed before the subcommand. The session gets the same env, cwd,
  CLI path, and tiers, so probe and session cannot diverge.
- The full environment is passed; explicit modes pin their neutralized keys to `""` even when
  absent, because the SDK overlays `options.env` on a live `os.environ` copy.
- Matrix: `auto` (no neutralization; CLI required; ready without probe when `ANTHROPIC_API_KEY` is
  non-blank, else probe); `subscription` (blanks `ANTHROPIC_API_KEY` and
  `_EXPLICIT_MODE_NEUTRALIZED`, CLI required, probe); `api_key` (blanks
  `_EXPLICIT_MODE_NEUTRALIZED`, needs a non-blank key, never probes).
- Explicit modes refuse an inherited non-blank `_CLOUD_BACKEND_SELECTORS` variable (named, value
  never shown) and a non-empty configured `setting_sources` (checked against configured, not
  effective tiers; message shared via `explicit_auth_mode_setting_sources_error` in
  `src/conductor/config/schema.py`, enforced in the schema and in `_check_auth_readiness`).
- Readiness requires `loggedIn: true` and exit 0; stdout is parsed first so a logged-out report is
  specific, and a contradictory result never echoes output. Probe output is filtered through
  `_AUTH_STATUS_WHITELIST`; `apiProvider` is never presented as `authMethod`.
- Readiness is not billing attribution: `_derive_billing` decides per execution from
  `finalized_child_env`, effective tiers, and probe fields, returning `"unknown"` without positive
  evidence. It is a local, never instance state.
- `validate_connection()` returns `False`; `execute()` raises `ProviderError(is_retryable=False)`.
  `execute()` logs only names of overridden credentials.
- `conductor doctor` builds the provider with default settings, so its auth diagnostic carries
  `AUTH_DIAGNOSTIC_SCOPE` (`src/conductor/providers/diagnostics.py`); never document it as
  reflecting a workflow's mode.
- Hermetic tests: `tests/conftest.py::_stub_claude_auth_readiness` stubs readiness for every test;
  `@pytest.mark.claude_auth_readiness_mocked` restores the real method and makes real process
  creation fail the test. Opted-in tests clear the environment or build an `EffectiveAuthContext`.

## Change checklist

- [ ] No new cancellation point in S1 or S4; reaping still uses shield plus `cancelling()`.
- [ ] Any new option that can widen tools, tiers, or MCP is refused or enforced at both validate and
      run time, and run-time refusals precede `_write_mcp_config`.
- [ ] New SDK option checked at the options level, not only in argv.
- [ ] Auth change keeps probe and session reading one `EffectiveAuthContext`.
- [ ] Symbol names and SDK-version claims re-checked after an SDK bump.
- [ ] Ran `uv run pytest tests/test_providers -k claude_agent_sdk tests/test_skills
      tests/test_plugins`.

## Rationale and evidence

**Client API.** `query()` closes its transport inside the iterator the caller awaits; cancelling a
blocked read there could leave the CLI running but already dropped from the SDK's child registry.

**Shield plus counter.** The shield keeps a later cancellation away from a child mid-teardown; only
the cancellation counter distinguishes "caller cancelled" from "child finished" in the same loop
turn.

**Failed shutdown fails a success.** Reporting success while the CLI may still be alive is the one
failure indistinguishable from working.

**Config by path, strict MCP.** argv is world-readable through `/proc/<pid>/cmdline`; ambient
servers would run with bypassed approvals.

**`native_tools: none` default.** The preset used to be implicit, making every agent a
filesystem/shell/web agent without the workflow saying so. The CLI forms tool names as
`mcp__<normalized server>__<tool>` (non `[A-Za-z0-9_-]` replaced by `_`, read from the CLI bundled
with SDK 0.2.87), so a `.` in a server name would never match its rule, and `__` or `,` would split
it.

**`settings_dir`.** The CLI advertises exactly one MCP root, its cwd, and the filesystem MCP server
then ignores its argv directories. Narrowing cwd onto a target repo to reach its skills narrowed the
agent's only MCP root; `add_dirs` adds skills without touching cwd. Measured against CLI 2.1.263.

**Default-empty tiers.** Unset, the CLI loads user, project, and local settings, bringing in ambient
skills, `CLAUDE.md`, and hooks that vary by machine.

**Transcript guard.** `--resume` with a missing transcript aborts before the agent runs, which would
turn a first iteration or a moved `working_dir` into a hard failure. The guard is deliberately
stricter than the CLI to hold the `(session_key, cwd)` scoping contract.

**Explicit auth modes.** A settings file's `env` block is applied after the child environment and
the SDK has no override known to outrank it; Conductor has no deterministic support for explicit
modes on Bedrock/Vertex/Foundry, so the conflict is refused visibly.

## Related references

- [Experimental providers](../../providers/experimental.md) (auth, native tools, interrupt sections)
- [Workflow syntax: native tools](../../workflow-syntax.md#native-tools-native_tools), [session
  continuity](../../workflow-syntax.md#session-continuity-session_key), [authentication
  mode](../../workflow-syntax.md#authentication-mode-auth_mode), [target-repository
  skills](../../workflow-syntax.md#target-repository-skills-settings_dir)
- [Configuration: field compatibility](../../configuration.md#field-compatibility-by-provider)
- [Claude subscription guide](../../providers/claude-subscription.md), [provider
  comparison](../../providers/comparison.md)
- Examples: `examples/claude-agent-sdk-session-key.yaml`,
  `examples/claude-agent-sdk-settings-dir.yaml`, `examples/claude-agent-sdk-setting-sources.yaml`,
  `examples/claude-agent-sdk-subscription.yaml`
- [Providers (shared contract)](../providers.md), [plugins and skills](../plugins-and-skills.md)
