# Plugins and Skills

How Conductor resolves `skills:` entries, discovers installed skills, loads plugins as a unit
(skills, subagents and MCP servers), acquires git-backed plugin sources, and how each provider
receives them. The user-facing syntax is in [workflow syntax](../workflow-syntax.md#skills); this
guide covers the invariants a contributor must not break.

## When to read

- Editing anything under `src/conductor/skills/` or `src/conductor/plugins/`, or
  `src/conductor/frontmatter.py`.
- Editing skill/plugin handling in `src/conductor/executor/agent.py`,
  `src/conductor/config/validator.py`, `src/conductor/cli/validate.py`,
  `src/conductor/cli/plugin.py`, or `src/conductor/cli/run.py::_prefetch_plugin_sources`.
- Changing a provider's `skills` / `plugins` / `plugin_flavor` capability, or the
  `skill_directories` / `custom_agents` / `extra_mcp_servers` arguments of `execute()`.
- Editing the bundled skill under `plugins/conductor/skills/conductor/` (size budget).

## Required invariants

### Module layering

- `src/conductor/plugins/__init__.py` re-exports NOTHING (`__all__ = []`): `skills.registry`
  imports `plugins.manifest` and `plugins.registry` imports `skills.registry`, so an eager
  re-export closes an import cycle. Import submodules directly.
- `src/conductor/plugins/manifest.py` stays a leaf: it imports only `conductor.plugins.errors`
  and the stdlib-only `src/conductor/filesystem.py`. `src/conductor/plugins/sources.py` imports
  only `conductor.plugins.errors`. `src/conductor/frontmatter.py` (the shared `---` splitter for
  `SKILL.md` and agent files) imports nothing from conductor and raises plain `ValueError`
  subclasses so each caller phrases its own message.
- `src/conductor/skills/errors.py::SkillError` is the shared base. `SkillNotFoundError` and
  `SkillPluginError` live in `src/conductor/skills/registry.py`; `SkillManifestError` in
  `src/conductor/skills/frontmatter.py`. Call sites MUST catch `SkillError`, not enumerate
  subclasses. Plugin errors mirror this under `src/conductor/plugins/errors.py::PluginError`.
- Path probes that must distinguish "missing" from "unreadable" use
  `src/conductor/filesystem.py` (`is_dir_strict`, `is_file_strict`, `stat_or_none`), never bare
  `pathlib` (Python 3.14 swallows `PermissionError`, #540).

### Skill entries

- Classification is syntactic (`src/conductor/skills/registry.py::is_path_entry`): a path starts
  with `~` or `.` or contains `/` or `\`; anything else is a built-in name. Never consult the disk
  to classify, so a bare `conductor` cannot be shadowed by a local directory.
- Paths resolve via `normalize_entry_path`: `~` expanded, relative entries anchored on the
  workflow file's directory (threaded from the engine as `workflow_dir`), normalized with
  `normpath`, never `resolve()`, so symlink aliases stay distinct. Shared by `skills:` and
  `plugins:` so they cannot drift.
- A path may be one skill directory or a root; `expand_skills_root` expands a root to its
  immediate children holding `SKILL.md` (not recursive), centrally inside `resolve_skills`, so
  every consumer gets one name per skill. One unreadable child MUST NOT discard readable siblings.
- Paths are trusted input (the same YAML can already run shell via `type: script`); do not add an
  allowlist.
- `AgentDef` only shape-checks path entries (no base dir at schema time) but still rejects an
  unknown built-in name at load time.
- Every resolved `SKILL.md` must have frontmatter with `name` and `description`, checked inside
  `resolve_skills` via `src/conductor/skills/frontmatter.py` (ruamel.yaml, not PyYAML). It is
  checked there, not only in the validator, because `conductor run` never calls the validator and
  both CLIs skip an unparseable skill silently.
- `src/conductor/skills/loader.py::_read_file` raises `SkillManifestError` rather than skipping;
  `lru_cache` never memoizes a raising call, so transient errors are retried.
- Skills are an `AgentDef`-only field; other step types reject them via `extra="forbid"` (see
  [workflow engine](workflow-engine.md)).
- Tri-state: omitted `skills:` inherits `runtime.skills`, `[]` opts out (the one opt-out, also
  for discovery), a list overrides.
- Built-in skills live under `plugins/conductor/skills/` (one directory per skill).
  `pyproject.toml` `force-include` MUST ship both the skill body and
  `plugins/conductor/.claude-plugin/`; without the manifest no plugin root resolves and every
  skills-enabled `claude-agent-sdk` agent fails on a wheel install.

### How skills reach the model

- The contract is the same everywhere ("the agent has access to the named skill"); the mechanism
  is `AgentProvider.supports_native_skills` (readable without instantiating a provider via
  `src/conductor/providers/capabilities.py::uses_native_skills`, which returns `None` when unknown
  so callers skip rather than guess).
  - Native `copilot`: skill directories go to the session as `skill_directories`.
  - Native `claude-agent-sdk`: each directory is mapped to its owning plugin via
    `src/conductor/skills/registry.py::resolve_skill_plugin` and enabled as `<plugin>:<skill>`.
    That SDK has no bare skill-directory option, so a path skill outside a plugin is refused at
    validate time and by the provider; details in
    [claude-agent-sdk notes](providers/claude-agent-sdk.md).
  - Eager `claude`, `openai`, `hermes`: `SKILL.md` plus `references/*.md` are injected into the
    prompt in `<skills><skill name="...">` tags.
  - `aca` declares `skills=False` (host paths are unreadable in the sandbox).
- `ProviderCapabilities.skills` MUST be accurate. A `skills=False` declaration is enforced at run
  time too (`AgentExecutor._reject_unsupported_skills`).
- Eager injection is bounded by `runtime.skill_injection` (`SkillInjectionConfig`): defaults
  `warn_bytes` 64 KiB and `max_bytes` 160 KiB, measured on the exact injected string, enforced in
  `AgentExecutor` and statically by a helper inside
  `src/conductor/config/validator.py::_validate_provider_capabilities`.
  `warn_bytes > max_bytes` is rejected. Native providers are exempt.
- The defaults MUST straddle the bundled `conductor` skill (currently about 140 KB injected), so
  enabling it on an eager provider warns but does not fail. If the skill grows past `max_bytes`,
  raise the default. Pinned by `test_bundled_skill_warns_but_does_not_error` in
  `tests/test_skills/test_injection_budget.py::TestBudgetEnforcement`.

### Skill discovery

- `runtime.skill_discovery.sources` is `personal` (`~/.copilot/skills`, `~/.claude/skills`)
  and/or `project` (`.github/skills`, `.claude/skills` walked from the workflow file's directory
  up to the first ancestor with `.git`; with no `.git`, only the workflow's own directory).
  There is no `plugins` source (removed with #378; use `plugins:`).
- Conductor scans centrally (`src/conductor/skills/discovery.py::discover_skills`) instead of
  enabling each provider's own discovery, so every agent in a run sees the same set. Never pass
  the Copilot SDK's `enable_config_discovery` (it would also auto-load `.mcp.json` servers), and
  keep claude-agent-sdk `setting_sources` empty unless the workflow opts in.
- Sources scan in the fixed order `project` then `personal`, independent of YAML order.
- `discover_skills` takes `home` as a parameter; tests MUST pass a fixture home, never read `~`.
- Discovered skills join the workflow-level default set, so an agent inheriting can get skills
  from an empty `runtime.skills`; `skills: []` still opts out.
- Strict/lenient asymmetry: broken frontmatter, a claimed name, an unreadable directory, or a
  skill `claude-agent-sdk` cannot load is an ERROR for a declared skill and a WARNING + skip for a
  discovered one. Exception: a provider with no native skill surface errors either way.
- Every eager provider (`claude`, `openai`, `hermes`; any provider without
  `supports_native_skills`) refuses discovery outright (machine-dependent size, well over
  `max_bytes`), enforced twice: `src/conductor/config/validator.py` and
  `AgentExecutor._reject_discovery_without_native_skills`.
- Precedence (`src/conductor/executor/agent.py::_merge_skills`, deduped by name): declared beats a
  plugin's skill, which beats a discovered one. `ResolvedSkill.discovered` carries provenance;
  branch on it, not on strings. Report a shadowed plugin skill through the warning sink, not a
  debug log.
- `src/conductor/cli/validate.py::_report_skill_discovery` lists the effective set (it resolves,
  so a dropped skill is not listed or billed) and forwards diagnostics the validator did not
  print.
- The single composition point for declared + discovered skills is
  `src/conductor/skills/discovery.py::resolve_effective_skills`, used by both `AgentExecutor` and
  `conductor validate`.

### Plugins

- The plugin is the unit of opt-in: `skills/`, `agents/` subagents and MCP servers load together.
  Entries are a string or `PluginDef` with `skills` / `agents` / `mcp` switches, all default
  `True`; defaulting one off recreates the partial load #378 fixed.
- Tri-state inheritance matches `skills:` (omitted inherits `runtime.plugins`, `[]` opts out).
- Entry classification (`src/conductor/plugins/registry.py::resolve_plugin`) is ordered: path
  first (so `./tools/my@plugin` stays a path), then `plugin@marketplace`, then a bare installed
  name globbed under `~/.copilot/installed-plugins/*/<name>` and `~/.claude/plugins/*/<name>`.
- An uninstalled name errors naming the searched roots. An ambiguous name (two marketplaces) is an
  error, never a silent pick; the remedies are `name@marketplace` or a path.
- Both manifest conventions are recognized (`.claude-plugin/plugin.json`,
  `.github/plugin/plugin.json`), and `mcpServers` in all three forms: a string path (the common
  case), an inline object, or a root `.mcp.json`.
- Conductor DECONSTRUCTS a plugin instead of registering its root. Each component rides the surface
  Conductor already uses: skills merge into `skill_directories`, subagents go to
  `execute(custom_agents=...)`, MCP servers to `execute(extra_mcp_servers=...)` after the same
  credential resolution as workflow servers (`src/conductor/mcp_auth.py::resolve_mcp_servers`).
  These are per-execute because `plugins:` is per-agent while providers are cached per type.
- `ProviderCapabilities.plugins` gates the feature: `copilot` and `claude-agent-sdk` `True`;
  `claude`, `openai`, `hermes`, `aca` `False` (no eager fallback can produce a subagent or an MCP
  server). Enforced twice: validator and `AgentExecutor._reject_unsupported_plugins`. A provider
  declaring `True` MUST honor all three `execute()` arguments.
- Subagents are named `<plugin>:<agent>`; `tools:` in agent files are host-CLI vocabulary,
  forwarded verbatim; `user-invocable` is not mapped.
- Collisions are refused, never deduped: two plugins with one MCP server name, a plugin server
  clashing with `runtime.mcp_servers`, two plugins with one skill name, or two agent files with
  one name. The remedy names `mcp: false` / `skills: false`. MCP server names are NEVER rewritten
  (Copilot prefixes tool names with them and plugin skills name those tools). The MCP clash is
  also refused in provider merge helpers because `conductor run` skips the validator.
- A plugin MCP server carries no per-server `tools:` filter; `mcp: false` is the control.
- `hooks/` and `commands/` are dropped loudly with a validate warning
  (`describe_dropped_components`). On a provider where reaching skills requires registering the
  root (`AgentProvider.skills_require_plugin_root`), the wording is "exposed to the CLI", not "not
  loaded".
- On `claude-agent-sdk`, `agents: false` with `skills: true` for one plugin is refused (root
  registration would expose the agents anyway), enforced in the validator and
  `AgentExecutor._reject_unfilterable_agents`. See
  [claude-agent-sdk notes](providers/claude-agent-sdk.md).
- Plugins are never discovered: naming one is resolution, so a miss is a hard error.
- `src/conductor/cli/validate.py::_report_plugins` prints component counts and every subagent by
  name.

### Flavor resolution (#497)

- A plugin build is `PluginFlavor = Literal["copilot", "claude"]`, read from whichever manifest
  convention actually matched (`src/conductor/plugins/manifest.py::manifest_flavor`), never
  guessed from location.
- `src/conductor/plugins/agents.py::read_plugin_agents(..., flavor=...)` picks the candidate rule
  from it: Copilot admits `*.agent.md`; Claude admits any `*.md`. A Claude-flavor `*.md` that is
  not `*.agent.md` and has no frontmatter (`PluginAgentFrontmatterError`) or frontmatter with
  neither `name` nor `description` (`PluginNotAnAgentError`) is skipped with a warning. The same
  failure on an explicit `*.agent.md` stays a hard error, so the broader rule is strictly
  additive.
- The requesting provider's flavor only breaks genuine ties: a dual-catalog marketplace and a bare
  name installed once per CLI under one marketplace directory. Two different marketplace names
  sharing a plugin name stay ambiguous regardless of flavor.
- Provider flavor is static: `ProviderCapabilities.plugin_flavor`, required when `plugins=True`
  (model validator). Name-keyed call sites use `plugin_flavor_for(provider_name)`;
  `AgentExecutor` reads `type(self.provider).CAPABILITIES`.
- The validator's plugin-resolution cache is keyed by `(entries, flavor)`; keying by entries alone
  served one provider's resolution to another.
- `~/.copilot/settings.json` `extraKnownMarketplaces`
  (`src/conductor/plugins/copilot_settings.py::read_copilot_marketplaces`, directory sources,
  Copilot flavor only) is consulted LAST in `_resolve_marketplace_entry`, so it can only turn an
  error into a resolution, and it prints an advisory naming `plugin_sources`.

### Git-backed plugin sources (#380)

- `runtime.plugin_sources` maps a marketplace name to a source; `plugins:` references it as
  `prs@acme`. Acquisition and activation stay separate (one repository often ships many plugins).
- A declared source registers into the same resolution table as installed marketplaces; declared
  beats installed and warns when it shadows an installed one of the same name.
- Source classification (`src/conductor/plugins/sources.py`) is by prefix only (`~`, `.`,
  absolute = local); NEVER reuse `is_path_entry`, which would turn `owner/repo` into a relative
  path. Cache keys strip credentials and ports.
- Catalog vs single plugin is auto-detected; a repo that is both needs `plugin:`. Both catalog
  conventions anchor `source` differently (repo root vs `pluginRoot`), so both anchors are tried
  (`src/conductor/plugins/marketplace.py`). A catalog `source` or `pluginRoot` escaping the
  checkout is refused.
- No lockfile: the YAML is the lock. A full 40-char SHA is pinned (fetched once); a tag, branch
  or omitted ref floats and is re-resolved every run via `git ls-remote`. There is no
  `plugin update` verb.
- Fetch (`src/conductor/plugins/fetch.py`): `git clone --depth 1` into a temp dir, published by
  atomic rename, readiness sentinel written last. Cache under
  `$CONDUCTOR_HOME/cache/plugins/<host>/<owner>/<repo>/<sha[:12]>/` (same root as the registry
  cache). The `_refs/<slug>.json` pointer is what makes the offline fallback possible. Ref
  patterns include `^{}` so an annotated tag resolves to its commit.
- Network posture: `conductor run`/`resume` prefetch all sources up front, concurrently, in a
  thread (`src/conductor/cli/run.py::_prefetch_plugin_sources`); `conductor plugin fetch` primes
  the cache; `conductor validate` and `conductor plugin list` stay offline. An unreachable remote
  with a warm cache warns and reuses the checkout; a cold cache errors naming the fetch verb.
- `src/conductor/plugins/resolution.py::resolve_plugin_sources` is the single composition point;
  run and validate differ only in `allow_network`.
- Validate resolves sources ONE AT A TIME
  (`src/conductor/config/validator.py`, `_DeferredPluginCheck`): an unfetched source
  (`PluginFetchError` / `PluginSourceUnavailableError`) is a WARNING that names the checks it
  skipped; a broken source (bad path, escaping `path:`, unparseable catalog) is an ERROR. An
  unreferenced source is reported as dead config.
- `WorkflowEngine._ensure_plugin_marketplaces` resolves declared sources when no caller supplied a
  table (directly constructed engines). Sub-workflows inherit the parent table and merge their own
  sources over it (child wins on a clash).
- Declaring a source is the consent; there is no prompt or allowlist.

## Change checklist

- [ ] No new import from `src/conductor/plugins/__init__.py`; leaf modules stay leaves.
- [ ] New skill/plugin restriction enforced both in `src/conductor/config/validator.py` and at
      run time in `AgentExecutor` or the provider.
- [ ] Declared vs discovered asymmetry preserved (error vs warning + skip).
- [ ] Provider capability (`skills`, `plugins`, `plugin_flavor`) matches behavior; a
      `plugins=True` provider honors `skill_directories`, `custom_agents`, `extra_mcp_servers`.
- [ ] Collisions refused with a remedy; MCP server names not rewritten.
- [ ] Bundled skill size checked against `SkillInjectionConfig` defaults if the skill changed.
- [ ] Tests pass a fixture `home`; fetch tests use real git over `file://`.
- [ ] Engine/executor integration tests assert components actually reach `execute`.
- [ ] Run `uv run pytest tests/test_skills tests/test_plugins tests/test_config`.

## Rationale and evidence

**Deconstruction (#378).** Both SDKs have an all-or-nothing whole-plugin surface. Copilot's
`excluded_tools` was shown to hide an MCP tool from the model while the server subprocess still
launched (a startup marker file proved it), so `mcp: false` built on root registration would be
cosmetic, and credentials used at process start would still be touched. Root registration also
inverted provider behavior. Deconstruction makes plugin MCP servers inherit `runtime.tool_output`
limits, dashboard events and credential resolution; skipping that resolution once handed the SDK
a literal, unexpanded environment-variable placeholder.

**Whole-unit opt-in.** A plugin's `SKILL.md` routinely dispatches to its own subagents or MCP
tools; loading only skills produced agents that reached for tools that were never registered.
That is also why the `plugins` discovery source was removed.

**Collisions are errors.** Every downstream consumer is name-keyed, so a silent dedupe drops a
component with nothing said; refusing with a named switch keeps the YAML authoritative.

**Frontmatter at resolve time.** Both CLIs skip an unparseable skill silently and `conductor run`
never validates, so the check must live in `resolve_skills`.

**Central discovery (#362).** Provider-native discovery would give different agents different
skill sets inside one run and, on Copilot, also auto-load `.mcp.json` servers. Discovered content
was not written by the author, so it gets warnings, not errors; erroring would bury users in
failures for plugins they never wrote.

**Flavor (#497).** A Copilot build can sit inside a Claude plugin tree via a symlink; the old
Copilot-only candidate rule silently gave a `copilot` workflow zero subagents from a Claude
build. Two agents on different providers sharing one cached resolution was the cross-agent bug.

**Sources (#380).** One-at-a-time resolution exists because batching let one unfetched source
empty the whole table, falsely reporting local sources as unacquired and silently skipping the
MCP-clash checks. A broken source is an error because no amount of fetching fixes it. Floating
refs match `src/conductor/registry/version_resolver.py`.

**Tests that are load-bearing.** `tests/test_plugins/test_fetch.py` runs real git against
`file://` repositories (built by `tests/test_plugins/conftest.py`) because annotated-tag
dereferencing, shallow SHA fetch and the offline fallback are properties of git itself.
`tests/test_plugins/test_executor_integration.py`, `tests/test_plugins/test_engine_integration.py`,
`tests/test_skills/test_executor_integration.py` (`TestSkillDirectoriesReachTheProvider`) and
`tests/test_skills/test_engine_integration.py` assert components arrive at `execute`; a negative
"nothing in the prompt" assertion cannot tell a native path from a dropped one, and plugin
subagents and MCP servers have no fallback path.

## Related references

- [Workflow syntax: skills](../workflow-syntax.md#skills),
  [plugins](../workflow-syntax.md#plugins),
  [flavor](../workflow-syntax.md#flavor-which-build-a-plugin-resolves-to),
  [declaring sources](../workflow-syntax.md#declaring-where-plugins-come-from)
- Examples: `examples/plugins.yaml`, `examples/demo-plugin/`, `examples/plugin-sources.yaml`,
  `examples/skills-discovery.yaml`, `examples/skills-self-improving-workflow.yaml`
- [Providers](providers.md), [Copilot notes](providers/copilot.md),
  [claude-agent-sdk notes](providers/claude-agent-sdk.md)
- [Workflow engine](workflow-engine.md), [testing](testing.md)
