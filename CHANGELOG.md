# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased](https://github.com/microsoft/conductor/compare/v0.1.27...HEAD)

### Added

- **`conductor status` — see what is running without stopping it** (#384).
  `conductor stop` with no arguments lists background workflows, but stops one
  when exactly one is running, so the natural "what's running?" reflex was
  destructive precisely when there was a single run to lose. `status` never
  terminates anything and never removes a PID file, so a run stays
  discoverable even when its liveness cannot be confirmed. It prints each
  run's dashboard URL, which is otherwise unrecoverable once the launching
  terminal is gone, and `--json` makes it scriptable. A malformed PID file is
  skipped with a warning rather than taking down the listing.
- **Git-backed plugin sources** (#380). `runtime.plugins` alone resolves
  against machine state — an installed plugin name, or a path — so a workflow
  shared with a teammate still needed "first install these plugins" in a
  README, and a teammate who skipped that step got a hard error rather than a
  working run. `runtime.plugin_sources` maps a marketplace name to where it
  comes from (`owner/repo#v1.4.0`, any http/https/ssh or `git@host:path`
  remote, or a local path), and `plugins:` entries reference it as
  `prs@acme`. The split follows the Copilot CLI's own settings, which
  separate `extraKnownMarketplaces` from `enabledPlugins` — eleven plugins
  commonly come from one repository, so inlining a URL per entry would either
  clone it eleven times or silently pick one of eleven refs. The load-bearing
  property is that `prs@acme` means the same thing whether the marketplace
  was declared, installed via a CLI, or is a local directory: a declared
  source registers its name into the same resolution table the installed
  roots populate, so git *feeds* resolution rather than adding a second code
  path. It also gives the ambiguity error added in #378 a second remedy —
  qualify `git` as `git@acme` instead of falling back to a path. Both
  repository shapes are handled: a `marketplace.json` catalog or a
  `plugin.json` single plugin, with a `plugin:` key for a repository that is
  both. There is **no lockfile** — the YAML is the lock. A ref that is a full
  40-character SHA is pinned and fetched once; a tag, branch, or absent ref
  floats and is re-resolved every run, matching how workflow registries
  already behave. `conductor run` acquires sources up front and in parallel;
  `conductor plugin fetch` primes the cache as its own step, which is what
  keeps `conductor validate` off the network entirely; `conductor plugin
  list` reports what a run would load, including the component counts that
  make a change in what a plugin ships visible. An unreachable remote with a
  warm cache warns and reuses the checkout, so offline runs keep working.
  Checkouts are cached under `$CONDUCTOR_HOME/cache/plugins/`, keyed by
  resolved commit. Cloning shells out to `git`, so existing SSH keys and
  credential helpers apply and self-hosted forges work.

  Sources are resolved one at a time, so a source that is unfetched or
  broken costs its own diagnostic rather than the report for every healthy
  source beside it. The two are distinguished: an *unfetched* source is a
  warning naming `conductor plugin fetch`, since `conductor run` heals it,
  while a source that is itself wrong — a path that does not exist, a
  `path:` that escapes the checkout, an unparseable catalog — is an error,
  because no amount of fetching fixes it. A declared source that shadows a
  same-named installed marketplace is reported, since the two can ship
  different subagents or a different MCP server. See
  `examples/plugin-sources.yaml` and the Plugins section of
  `docs/workflow-syntax.md`.
- **Output field constraints — `enum`, `pattern`, `minimum`/`maximum`,
  `minLength`/`maxLength`, `required`, `nullable`**
  ([#372](https://github.com/microsoft/conductor/pull/372)). An `output:` field
  could declare a type and nothing more, so "verdict is one of three values" or
  "score is 0-100" lived in the prompt, where it was a suggestion rather than a
  contract. The eight new keywords are emitted into the schema each provider
  shows its model and enforced when the response comes back, and because a
  violation raises the same `ValidationError` a type mismatch does, it lands
  inside the existing in-session recovery loop — the model gets a chance to
  correct itself before the workflow fails. Constraints are checked recursively,
  so they hold inside object properties and array items too.

  Illegal combinations are rejected at load time rather than at run time:
  `pattern` on a number, an `enum` whose members do not match the declared type,
  `minLength` above `maxLength`, a regex that does not compile. Unknown keys are
  rejected as well, so a misspelled `minlength` fails validation instead of
  quietly leaving the field unconstrained. `required: false` is allowed only
  inside object properties — a root-level output field cannot be optional.

  Two things worth knowing when using them. `pattern` runs under a one-second
  deadline on a `re`-compatible engine, because model output is untrusted input
  and a backtracking pattern would otherwise stall the event loop and every
  agent sharing it; an exceeded deadline is a validation failure, not a hang.
  And templates render with `StrictUndefined`, so a `nullable` field that came
  back null renders as `None` and an omitted optional property raises — guard
  both with `is not none` / `is defined`, as
  [`examples/output-constraints.yaml`](examples/output-constraints.yaml) shows.
  See [`docs/workflow-syntax.md`](docs/workflow-syntax.md) (Field Constraints).

- **Plugins as the unit of opt-in** (#378). Conductor loaded a plugin's
  `skills/` and dropped everything else it shipped. That is a problem because
  a plugin's parts are written to work together: its `SKILL.md` routinely
  tells the agent to hand work to `prs:code-reviewer`, or to call an `ado` MCP
  tool. The skill loaded, the agent read those instructions, reached for a
  subagent that was never registered — and said nothing. `runtime.plugins`
  (and per-agent `plugins:`) now opts into the whole unit: skills,
  `agents/*.agent.md` subagents, and declared MCP servers. Entries take a
  string shorthand or an object with per-component switches (`skills`,
  `agents`, `mcp`), all defaulting on, because defaulting one off would
  recreate the partial load the feature exists to fix. An entry is an
  installed plugin name or a path, classified by the same syntactic rule
  `skills:` uses; an uninstalled name errors naming where it looked, and an
  ambiguous one errors rather than picking a winner. Conductor
  **deconstructs** a plugin rather than handing its root to the SDK: both
  SDKs' whole-plugin surfaces are all-or-nothing, and on Copilot hiding an
  MCP tool from the model does **not** stop its server subprocess launching
  with the user's credentials — so `mcp: false` built that way would be a
  guarantee that isn't one. Deconstructed, a plugin's MCP servers also pick
  up the same `runtime.tool_output` limits, dashboard tool events, and
  credential/`${VAR}` resolution as a workflow-declared server. Supported on `copilot` and
  `claude-agent-sdk`; `claude`, `hermes` and `aca` reject `plugins:` at
  validation time, since injecting text into a prompt cannot produce a
  subagent or an MCP server. `conductor validate` prints what each plugin
  contributes, including every subagent by name, so a change in what a plugin
  ships is visible before the run rather than during it. See
  `examples/plugins.yaml` and the Plugins section of `docs/workflow-syntax.md`.

- **Copilot-convention plugin manifests are recognised** (#378).
  `.github/plugin/plugin.json` now resolves alongside
  `.claude-plugin/plugin.json`. Both have always worked at runtime, so
  recognising only the latter was Conductor's own gap — on an ordinary
  machine it stranded 12 of 13 installed plugins, which on
  `claude-agent-sdk` meant a packaged skill was rejected outright and on
  `copilot` meant it was silently demoted to a bare directory.

- **`type: questions` — ask a human a set of questions in one step** (#376).
  `human_gate` handles a single decision; asking N questions previously meant
  hand-rolling a gate that loops back through a `set` step accumulating a
  string transcript. That loop cannot support going back — a workflow step
  cannot be un-executed, and a concatenated transcript has no addressable
  per-question answer to overwrite — and it costs two engine iterations per
  question. A `questions` node holds the cursor and answers internally, so the
  whole set costs one iteration and answers land in a keyed dict where
  revisiting question 3 overwrites `answers.q3`. Questions come from an inline
  `questions:` list or a `source:` dotted path; entries may be plain strings or
  objects with `choices`, so an agent already emitting `array of string`
  migrates unchanged while gaining candidate answers is a backward-compatible
  upgrade. Supports back/skip/skip-all/abort, `required`, per-question
  `default`s, a closing review, and partial answers that survive a checkpoint.
  `--skip-gates` never selects a suggested answer — those come from the agent,
  so recording one would feed invented input back as though a human gave it.
  See `examples/questions.yaml` and the Questions section of
  `docs/workflow-syntax.md`.
- **Opt-in multi-line text for human gates** — `GateOption.multiline` (default
  `false`, so existing gates are unchanged). The terminal reads until a lone
  `.` or EOF; the dashboard renders a textarea where Enter inserts a newline
  and Ctrl/Cmd+Enter submits. Previously a multi-paragraph answer to a
  `prompt_for` input was silently truncated at the first newline.

### Removed

- **`skill_discovery.sources: [plugins]`** (#378). The source scanned every
  installed plugin's `skills/` directory — reaching into a plugin and taking
  exactly one of the three things it ships, which is the bug `runtime.plugins`
  fixes rather than a feature with a gap. It was also wrong more often than it
  looked: of 13 plugins on an ordinary machine, 3 loaded their instructions
  without the subagents those instructions dispatch to, and the 3 most
  plugin-like — MCP and subagent toolkits with no `skills/` at all — were never
  discovered by it. Nothing distinguished the working set from the broken set
  at authoring time, on a machine the workflow author may not even be using.
  `personal` and `project` are unchanged. Replace `sources: [plugins]` with
  `runtime.plugins`, which brings the whole plugin and, unlike a scan,
  reproduces on another machine.

### Fixed

- **`conductor stop` now confirms the process actually stopped, and never
  stops the wrong one** (#344). `stop` sent one signal and reported success
  without checking, so a workflow that ignored it was reported as stopped and
  its PID file deleted — leaving a live run untracked, invisible to `stop`,
  and holding its port. Termination is now a ladder (ask the dashboard to
  cancel, then signal, then force-terminate), each rung confirmed before the
  next, and the PID file is removed only once the process is confirmed gone.
  Every PID-directed rung is gated on the dashboard confirming its own PID,
  because between a PID file being written and `stop` reading it the OS may
  have recycled that PID onto an unrelated process. `--force` overrides
  *uncertainty* only: a positive identity mismatch blocks every rung, force
  included. PID files are written atomically, so a concurrent `stop` can no
  longer read a half-written file and deregister a live run, and the reader
  logs before deleting anything it cannot parse. `--force` can clear an entry
  whose liveness cannot be probed, which would otherwise wedge `stop --all`
  at exit 2 permanently (#166).
- **Agent text containing bracketed tokens no longer kills a run** (#382). A
  step whose output contained ordinary technical prose such as
  `{provider}/{type}[/{nestedType}...]/read` was parsed by rich as a closing
  markup tag, raising `MarkupError` and ending the workflow — unresumably,
  since the crash happened while rendering rather than while running. Every
  console sink that renders agent-supplied text now passes it as `rich.text.Text`
  rather than interpolating it into markup, and the file-log console disables
  markup entirely. `style=` does not turn markup parsing off, which is what hid
  three of the five sinks; two of those were reachable on a bare `conductor run`
  with no flags. Opening tags such as `[bold]` were the quieter half of the same
  bug: rich consumed them without raising and the text simply disappeared.
- **Structured `runtime.provider` for `name: claude` no longer drops a
  YAML-declared `api_key`** — the schema accepted `api_key` (alongside
  `base_url` and `auth_token`) but the provider factory silently discarded it,
  so only the `ANTHROPIC_API_KEY` env var ever reached the Anthropic client.
  The factory now forwards it. Two credential-semantics fixes ride along so
  the documented behavior is what the code does: the Claude provider's model
  path now resolves credentials as a unit like the Anthropic SDK does —
  setting either credential in YAML suppresses both `ANTHROPIC_API_KEY` and
  `ANTHROPIC_AUTH_TOKEN`, where previously a YAML `auth_token` still let an
  ambient `ANTHROPIC_API_KEY` ride along and the SDK sent both `X-Api-Key`
  and `Authorization: Bearer` headers to whatever `base_url` pointed at
  (a credential leak against gateway endpoints). And when both credentials
  are set, Conductor now logs a warning naming that behavior instead of
  shipping both headers silently — the same parity warning the Copilot
  provider already logs for `api_key` + `bearer_token`. New example:
  [`examples/claude-custom-endpoint.yaml`](examples/claude-custom-endpoint.yaml);
  see [`docs/providers/claude.md`](docs/providers/claude.md) (Custom
  Endpoints and Gateways).
- **`conductor resume --web` no longer shows a running workflow as stopped** —
  a workflow that was paused from the dashboard (Stop, then Kill) recorded an
  `agent_paused` event in its event log with no `agent_resumed` counterpart.
  On resume the CLI seeds the dashboard from that log, so the pause replayed
  and latched the dashboard's global paused state for the entire resumed run:
  the header showed Resume/Kill instead of Stop for a pause that never
  happened, hiding the only graceful stop behind a Kill that would hard-stop
  the healthy resumed run. Pause, iteration-limit-gate, and dialog events are
  now dropped on replay at every workflow depth, alongside the root lifecycle
  events already filtered; a gate the resumed run genuinely re-enters emits
  its own fresh event. Prior agent output and messages are still replayed.
  The dashboard's live-control buttons are also hidden in `conductor replay`
  mode, where the recorded-log server serves no `/api/stop`, `/api/resume`,
  or `/api/kill` endpoint.
- **Dashboard Stop/Resume/Kill no longer hang on a failed request** — `fetch`
  resolves rather than rejects on a 4xx/5xx response, so a non-2xx reply left
  the button disabled and reading "Stopping…" indefinitely, with nothing
  logged and no way back except reloading the page. The response status is now
  checked explicitly and surfaced next to the controls.
- **Dashboard: expanding a subworkflow no longer slides the graph out from
  under you** ([#375](https://github.com/microsoft/conductor/issues/375)) —
  the graph layout normalizes its bounding box to the origin on every rebuild,
  so growing one container repositioned every node while the camera stayed
  where it was. Expanding or collapsing a subworkflow now pans the viewport by
  the same amount the toggled container moved, so that container stays pinned
  under the cursor and the surrounding nodes visibly move out of its way
  instead. Collapsing that same subworkflow returns you to the view you
  started from. Expand-all, and any other change that toggles several
  subworkflows at once, anchors on whatever sits nearest the center of the
  pane; the view is not refit in either case. The same compensation steadies
  the graph when a running workflow's topology grows — a `for_each` fanning
  out or a subworkflow's DAG arriving.

## [0.1.27](https://github.com/microsoft/conductor/compare/v0.1.26...v0.1.27) - 2026-08-04

### Added

- **Skills: `runtime.skills` and per-agent `skills:` give agents opt-in
  knowledge bases** ([#180](https://github.com/microsoft/conductor/issues/180))
  — `runtime.skills` enables a list of skills for every provider-backed agent
  in the workflow, and any agent can override it with its own `skills:`
  (omitted = inherit, `[]` = explicit opt-out, a list = explicit set).
  Conductor ships a built-in `conductor` skill covering its own YAML schema,
  execution model, authoring patterns, and CLI commands, so an agent that
  writes or reviews workflows can be made Conductor-aware without pasting
  documentation into its prompt. The observable contract is the same on every
  provider — the agent has access to the named skill — but the mechanism
  differs: `copilot` registers the skill directory on the SDK session, so only
  the frontmatter is read up front and the body is loaded on demand, while
  providers with no native skill surface receive the skill content injected
  into the prompt. Providers declare whether they can load skills at all, and
  `conductor validate` rejects a workflow that enables skills on one that
  cannot, rather than letting the content be silently dropped at run time.
  Skills are rejected on step types with no model behind them (`script`,
  `set`, `wait`, `terminate`, `workflow`, `human_gate`). See
  [`examples/skills-self-improving-workflow.yaml`](examples/skills-self-improving-workflow.yaml)
  and [`docs/workflow-syntax.md`](docs/workflow-syntax.md) (Skills).

- **Skill discovery: `runtime.skill_discovery` picks up skills already
  installed on the machine** (issue #362) — `sources: [personal, project,
  plugins]` scans `~/.copilot/skills` and `~/.claude/skills`,
  `.github/skills` and `.claude/skills` from the workflow file's directory
  up to the repository root, and every installed plugin's `skills/`
  directory, so a workflow can use a personal or team skill library without
  enumerating it. Off by default. `exclude:` drops individual skills by
  name. Conductor scans the union of both CLIs' locations itself rather
  than enabling each provider's own discovery: locations are
  provider-specific, so a per-provider flag would give a `copilot` agent
  and a `claude-agent-sdk` agent different skill sets inside a single run.
  Scanning centrally also keeps Copilot's `enable_config_discovery` off,
  which would otherwise auto-load MCP servers from any `.mcp.json` in the
  working directory. Discovered skills join `runtime.skills`, so an agent
  declaring its own `skills:` (including `skills: []`) still overrides
  them; a skill named in `skills:` beats a discovered one of the same name.
  Discovered content is held to a laxer standard than declared content —
  broken frontmatter, a taken name, an unreadable directory, or a skill
  `claude-agent-sdk` cannot load are errors for a declared skill and
  warning-plus-skip for a discovered one (a provider with no native skill
  surface at all is the exception, and errors either way). Rejected at validation time on `claude` and
  `hermes`, which inject every skill body into every prompt and cannot
  bound a machine-dependent set; on `claude-agent-sdk` only discovered
  skills inside a Claude Code plugin are loaded and the rest are skipped
  with a warning. `conductor validate` lists what was found, where each
  skill came from, and the total size if eagerly injected. See
  `examples/skills-discovery.yaml` and `docs/workflow-syntax.md`
  (Discovering installed skills).

- **`skills:` now accepts filesystem paths, not just built-in names**
  (issue #350) — an entry is treated as a path when it starts with `.`
  or `~`, or contains `/` or `\`; everything else must still be
  a registered built-in, so a bare `conductor` can never be shadowed by a
  same-named local directory. A path may point at a single skill directory
  (one holding `SKILL.md`) or at a root of them, which expands to every
  immediate child that holds one. Relative paths resolve against the workflow
  file's directory — the same rule `working_dir` uses — so a team can version
  a skill alongside the workflow that uses it with no per-developer install
  step, and the workflow resolves identically from any working directory.
  Conductor expands roots itself rather than handing them to a provider,
  because eager injection needs a name per skill and `claude-agent-sdk` needs
  a `<plugin>:<skill>` name; doing it centrally keeps every provider seeing
  the same set. Skill paths are trusted input by design: the same workflow
  file can already declare `type: script` steps running arbitrary shell, so
  no additional allowlist applies.
- **`runtime.skill_injection` bounds eagerly injected skill content**
  (issue #350) — `warn_bytes` (default 64KB) logs a warning and reports from
  `conductor validate`; `max_bytes` (default 128KB) fails the agent. Either
  can be set to `null` to disable it. Providers without a native skill
  surface (`claude`, `hermes`) have no progressive disclosure: `AgentExecutor`
  prepends each enabled skill's `SKILL.md` **plus its entire `references/`
  tree** on every call and every retry, and there
  was previously no ceiling at all. The bundled `conductor` skill alone is
  ~117KB (~29K tokens), so the defaults deliberately straddle it — enabling
  it on `claude` now warns instead of breaking, while accumulating several
  large skills errors. Both limits are measured against the exact string
  being prepended and report a per-skill breakdown naming the offender.
  Providers with progressive disclosure (`copilot`, `claude-agent-sdk`) are
  unaffected.
- **`hermes` declares `skills=True`** (issue #350) — the provider omitted
  `skills` from its `CAPABILITIES`, which defaults to `False`, so
  `conductor validate` rejected `skills:` on it while its own `execute()`
  docstring described eager injection working. Injection happens in
  `AgentExecutor`, upstream of every provider, so the path was always
  reachable and the declaration was simply inaccurate. Now bounded by
  `runtime.skill_injection` like `claude`.

- **`claude-agent-sdk` provider now honors `working_dir`** — the directory
  resolved from `agent.working_dir` / `runtime.working_dir` is forwarded to
  `ClaudeAgentOptions.cwd`, so the `claude` CLI runs there and every stdio MCP
  server it spawns inherits the same directory. Previously the provider
  declared `working_dir=False` and `conductor validate` rejected any workflow
  that set it, which was accurate but left the provider out of step with
  `copilot` and `claude`. There is no per-server stamping as there is for
  Copilot, because the SDK's stdio server config has no working-directory
  field — inheritance from the CLI subprocess covers it. A missing directory
  still fails before the provider is reached, and `strict_mcp_config` remains
  enabled so a `.mcp.json` sitting in the new directory cannot inject
  undeclared servers. The `claude` CLI would also read `CLAUDE.md` and
  `.claude/settings*.json` from its working directory, but the same release
  pins `setting_sources` to an empty list (see the skills entry below), so
  those are no longer loaded from wherever the agent happens to run.
  Launch failures caused by a bad working directory are now reported as such
  rather than as connection problems, and are no longer treated as retryable.
  See
  [`docs/workflow-syntax.md`](docs/workflow-syntax.md#working-directory) and
  [`docs/providers/experimental.md`](docs/providers/experimental.md).
  ([#348](https://github.com/microsoft/conductor/issues/348))

- **`claude-agent-sdk` provider now supports MCP servers** — workflow-level
  `runtime.mcp_servers` are translated to the SDK's own `stdio` / `http` /
  `sse` config shapes and passed through `ClaudeAgentOptions`, so an agent can
  use custom MCP tool servers *and* the built-in `claude_code` tool preset at
  the same time. Previously the two were mutually exclusive: the provider
  declared `mcp_tools=False` and the factory rejected any workflow declaring
  MCP servers. The generated config is written to a `0600` temp file and
  passed by path so resolved `env` values and `Authorization` headers never
  reach the `claude` CLI's command line, and `strict_mcp_config` is always
  enabled so ambient project/user MCP config cannot inject undeclared servers.
  A narrowing per-server `tools:` filter has no SDK equivalent and is refused
  (when the first agent on this provider runs) rather than silently ignored.
  See
  [`examples/claude-agent-sdk-mcp.yaml`](examples/claude-agent-sdk-mcp.yaml)
  and [`docs/mcp-tools.md`](docs/mcp-tools.md).
  ([#335](https://github.com/microsoft/conductor/issues/335))

- **Conservative unwrapping of wrapper-shaped scalars.** When a scalar output
  field receives an object holding exactly one value of the expected type
  under either the field's own name or a generic `value`/`result` key,
  providers unwrap it and log a warning instead of spending a recovery
  round-trip. Ambiguous shapes (two matching candidates) and any other key
  shape are left alone and re-prompted, so an object like
  `{"error": "I could not complete the task"}` is never laundered into an
  answer. Validation itself stays strict, so `set` and `script` step output is
  unaffected. ([#343](https://github.com/microsoft/conductor/issues/343))

- **Non-object JSON responses are recoverable.** A response parsing to a bare
  scalar, `null`, or an array is now re-prompted as a shape failure rather
  than producing an unhelpful terminal error. On Claude this previously
  reached `validate_output` inside the API error handler and surfaced as
  "check API key, model name, and request parameters"; on Copilot it reached
  the executor backstop and was reported as a missing required field.
  ([#343](https://github.com/microsoft/conductor/issues/343))

- **`agent_parse_recovery` event.** Recovery attempts were previously visible
  only under verbose console logging, so a run could burn its entire recovery
  budget without leaving a trace. All three providers now emit an event
  carrying the attempt number, the budget, whether the cause was `schema` or
  `syntax`, and the error. Rendered in the dashboard activity stream, the
  console, and the structured event log.
  ([#343](https://github.com/microsoft/conductor/issues/343))

- **Output validation errors describe the offending value.** Container values
  are rendered by shape (`object with keys ['a', 'b']`) rather than dumped,
  since `validate_output` also runs on `set` and `script` step output that may
  carry secrets. ([#343](https://github.com/microsoft/conductor/issues/343))

- **Sub-workflow nodes in the dashboard are expandable before they run.** A
  `type: workflow` node previously became expandable only once the engine
  actually reached that step, so the inner DAG of a sub-workflow that had not
  started yet was invisible. Conductor now resolves each sub-workflow's static
  topology up front and attaches it to the graph, so the real inner DAG can be
  expanded — recursively, for nested sub-workflows — from the moment the run
  starts. Resolution is best-effort: a missing file, registry error, cycle, or
  depth limit simply leaves the node collapsed until the engine reaches it.
  ([#360](https://github.com/microsoft/conductor/pull/360))

### Fixed

- **A malformed `SKILL.md` no longer fails silently** (issue #350) — both the
  Copilot CLI and Claude Code skip a skill whose YAML frontmatter cannot be
  parsed, with no warning and no error, leaving an agent running without the
  knowledge its author asked for. The trap is ordinary: a `description`
  containing `Triggers: ...` as an unquoted plain scalar is invalid YAML.
  Conductor now parses the frontmatter itself, requires a non-empty `name`
  and `description`, and reports the underlying YAML error along with the
  `description: |` block-scalar fix. Enforced during resolution rather than
  only in `conductor validate`, because `conductor run` never invokes the
  static validator.
- **`conductor validate` rejects a `claude-agent-sdk` skill outside a plugin**
  (issue #350) — that SDK exposes no bare skill-directory option, only plugin
  roots plus skill names, so such a skill is unreachable there even though
  `copilot` loads it fine. It previously surfaced as a runtime
  `ProviderError` on first execution; it is now reported before the run
  starts, naming the directory and offering both remedies (package it as a
  plugin, or run the agent on `copilot`).

- **`skills: []` is now a real opt-out on `claude-agent-sdk`, and agents no
  longer inherit ambient skills from the machine.** The provider left the SDK's
  `setting_sources` unset, so the `claude` CLI discovered and enabled skills
  from `~/.claude/skills/`, every `.claude/skills/` up the directory tree, and
  enabled plugins — none of which the workflow declared, and all of which
  varied by developer machine and launch directory. Conductor documents
  `skills: []` as an explicit opt-out; on this provider it silently opted out
  of nothing. Two options now carry that fix together and neither is redundant:
  `setting_sources` is always `[]` (the same unconditional isolation
  `strict_mcp_config` already applies to MCP servers), and `skills` is always
  passed explicitly, because the SDK treats an omitted list as "CLI defaults
  apply" and re-defaults `setting_sources` to `["user", "project"]` whenever
  `skills` is set without it.
  **Behavior change:** agents on this provider also stop picking up ambient
  `CLAUDE.md`, `.claude/rules/*.md`, user/project/local `settings.json`
  (including `env` and `apiKeyHelper`), and hooks. Instruction files can be
  supplied explicitly with `--workspace-instructions` (or `--instructions`);
  settings and hooks have no equivalent, so move anything load-bearing there
  into the environment. Note the SDK's skill list is a context filter, not a
  sandbox — undeclared skills are hidden from the model's listing, but their
  files stay readable on disk.
  ([#352](https://github.com/microsoft/conductor/issues/352))

- **`tools: []` no longer fails validation when no MCP servers are declared** —
  the capability cross-check rejected an explicit empty allowlist against any
  provider with `mcp_tools=True` and `workflow_tools_passthrough=False` (such
  as `aca`), even when the workflow declared no `mcp_servers` and therefore had
  nothing to forward. The check is now gated on MCP servers actually being
  configured.
  ([#335](https://github.com/microsoft/conductor/issues/335))

- **Schema-shape failures now go through the parse-recovery loop instead of
  killing the workflow.** When an agent returned syntactically valid JSON whose
  fields had the wrong shape (for example an object where `type: string` was
  declared), the Copilot and Claude providers failed the run immediately with
  zero recovery attempts, even though `max_parse_recovery_attempts` exists for
  exactly this class of contract violation. Schema validation ran one layer up
  in `executor/agent.py`, after the provider had already returned and — for
  Copilot — after its SDK session had been disconnected, so the loop that could
  have re-prompted never saw the error. Both providers now validate inside the
  recovery loop, matching Hermes. ([#343](https://github.com/microsoft/conductor/issues/343))

- **Exhausted recovery keeps the specific validation error.** A schema-shape
  failure that survives every recovery attempt now re-raises the original
  `ValidationError` naming the offending field and its expected type, rather
  than collapsing into a generic "failed to parse structured output" provider
  error. Syntax failures keep raising `ProviderError` as before. This also
  fixes Hermes, which previously discarded the field detail.
  ([#343](https://github.com/microsoft/conductor/issues/343))

- **Hermes now honors `retry.max_parse_recovery_attempts`.** It used a
  hardcoded module constant of 3 and silently ignored the YAML value that
  Copilot and Claude both respect. ([#343](https://github.com/microsoft/conductor/issues/343))

- **Nested `array` item schemas in `output:` are now enforced.** `validate_output`
  type-checked only the top level of an array, so an `array<object>` output whose
  items were missing declared fields — or had the wrong types — passed validation
  silently, even though the full nested schema had been sent to the model.
  Validation now recurses through both `object.properties` and `array.items` at
  every depth, for LLM agents, `set` steps, and `script` steps alike.
  **Behavior change:** a workflow that was quietly emitting output violating its
  own declared nested schema will now fail with `ValidationError` instead of
  passing. Flat schemas and `object` nesting are unaffected, and arrays declared
  without `items` keep their existing passthrough.
  ([#337](https://github.com/microsoft/conductor/pull/337))

- **MCP server connections are now closed by the task that opened them.** Each
  stdio/session lifecycle is held in its own owner task and signalled at
  shutdown, so AnyIO cancel scopes always exit in the task that entered them.
  Previously a connection opened in a worker task and closed from the root task
  could raise a cancel-scope error during teardown, surfacing as a spurious
  failure at the end of an otherwise successful run. Cleanup failures are logged
  rather than masking the caller's own cancellation, and registering a duplicate
  server name is now rejected instead of orphaning the existing connection.
  ([#353](https://github.com/microsoft/conductor/issues/353))

- **Non-ASCII agent output is no longer truncated ~6x too aggressively before
  semantic validation.** The `validator:` grader and the dialog-trigger evaluator
  serialized the agent output with ASCII escaping before applying their fixed
  character budget, so every Cyrillic or CJK code point cost 6 characters and
  every emoji 12. Non-English output reached the grader with a fraction of the
  source material an equivalent English output would get, and the cut could land
  mid-escape, leaving malformed JSON in the prompt. Both now serialize without
  escaping, so the budget is measured in real characters for every language.
  ([#356](https://github.com/microsoft/conductor/issues/356))

- **An inline-expanded sub-workflow now follows the live run after a loop-back.**
  When a loop-back route re-invoked the same sequential sub-workflow, the inline
  graph expansion stayed pinned to the first, already-completed invocation, even
  though double-click navigation and the Activity tab correctly tracked the live
  one. Inline expansion now resolves newest-first, matching the rest of the
  dashboard. ([#361](https://github.com/microsoft/conductor/issues/361))

- **Every historical sub-workflow iteration is now reachable from the
  dashboard.** In the "Subworkflow Runs (N)" list, each row resolved by slot
  rather than by position, and every re-invocation of a sequential sub-workflow
  shares a slot — so clicking an older, completed run always landed on the most
  recent one. Rows now navigate to the exact run clicked, are labelled
  `Iteration N` once a slot repeats, and the breadcrumb trail says which
  iteration is being viewed. Following the live run (double-click, inline
  expansion, deep links) is deliberately unchanged.
  ([#365](https://github.com/microsoft/conductor/issues/365))

- **Handled fail-open paths no longer print a full traceback at WARNING level.**
  When a semantic validator rejected an output and the retry itself failed,
  Conductor correctly kept the original output and carried on — but logged the
  warning with a complete Python traceback, making a recovered run look like a
  crash. The warning is now a single line naming the exception type and message,
  with the traceback kept at DEBUG. The same treatment was applied to the sibling
  best-effort paths: validator call failures and timeouts, provider session-ID
  collection for checkpoints, checkpoint save and rotation failures, and
  event-callback errors in `hermes`.
  ([#357](https://github.com/microsoft/conductor/issues/357))

### Changed

- **The `claude` provider now runs its agentic loop through Pydantic AI.** The
  hand-written inner loop was replaced with a Pydantic AI runtime while keeping
  Conductor's provider contract at the boundary — the same `AgentOutput`, event
  vocabulary, retry and interrupt semantics, usage accounting, MCP policy, and
  output validation. The visible gain is streaming: `claude` now emits model,
  reasoning, and tool events as they happen rather than only at completion, so
  the dashboard, console, and event log follow a Claude agent live the way they
  already followed Copilot. Structured output is produced natively by the
  runtime and still re-validated against the declared `output:` schema.
  **This adds `pydantic-ai>=1.44.0` as a required runtime dependency**, so a
  `conductor` upgrade pulls in a larger dependency set than before.
  ([#355](https://github.com/microsoft/conductor/pull/355))

- **`claude-agent-sdk` now loads skills natively instead of injecting them into
  every prompt.** The provider previously took the eager preamble path on the
  grounds that the SDK had no skill surface — out of date, and expensive: the
  full `SKILL.md` plus the entire `references/` tree was prepended to every
  call and every retry (~29K tokens for the bundled `conductor` skill). The owning Claude Code plugin is now registered on the
  session and the skill enabled by its `<plugin>:<skill>` name, so the CLI reads
  only the frontmatter up front and loads the body on demand. An agent with an
  explicit `tools: []` is granted back the single `Skill` tool when it has
  skills enabled, since an empty base tool set would otherwise leave the
  declared skill unreachable. Wheels now also ship
  `plugins/conductor/.claude-plugin/`; without the manifest no plugin root
  resolves at all, so a non-editable install would fail every skills-enabled
  agent on this provider.
  ([#352](https://github.com/microsoft/conductor/issues/352))

- The `claude-agent-sdk` optional dependency floor is now
  `claude-agent-sdk>=0.2.82` — the 0.2.x line is what Conductor tests against.
  ([#335](https://github.com/microsoft/conductor/issues/335))
- `claude-agent-sdk` agents no longer inherit ambient MCP configuration.
  Conductor now always sets `strict_mcp_config`, so a project `.mcp.json`,
  user-global settings, or plugin-provided servers are ignored and only
  servers declared in `runtime.mcp_servers` attach. Workflows that relied on
  Claude Code's own MCP settings must declare those servers in the workflow.
  ([#335](https://github.com/microsoft/conductor/issues/335))



## [0.1.26](https://github.com/microsoft/conductor/compare/v0.1.25...v0.1.26) - 2026-07-27

### Added

- **New experimental `aca` provider (Azure Container Apps)** — delegates an
  agent's entire agentic loop, tools, and MCP calls to a remote ACA
  dynamic-sessions sandbox instead of running it on the host, so untrusted or
  isolation-sensitive agents execute in a disposable container. Includes
  provisioning tooling (`scripts/aca/provision-pool.sh`), an in-package
  runner image, an end-to-end example (`examples/aca-coding-agent.yaml`), and
  automatic inner-Copilot credential resolution — falling back through
  `COPILOT_PROVIDER_BASE_URL` (BYOK) →
  `COPILOT_GITHUB_TOKEN`/`GH_TOKEN`/`GITHUB_TOKEN` → `gh auth token` — so an
  operator already signed in with the GitHub CLI needs no ACA-specific
  credential setup. See [`docs/providers/aca.md`](docs/providers/aca.md) for
  architecture, setup, and the declared experimental-tier capability
  carve-outs. ([#284](https://github.com/microsoft/conductor/issues/284))

### Fixed

- **Copilot and Hermes prompt schemas no longer fall back to a synthetic
  `"The {field} field"` description**, matching Claude's existing behavior —
  fields without an explicit `description` now produce a shorter JSON Schema
  object instead of a made-up one. Hermes also now shares the same recursive
  prompt-schema builder as Copilot, fixing cases its old hand-rolled builder
  got wrong: `array<object>` items now include `required` alongside
  `properties`, and `array<array>` nesting recurses correctly instead of
  collapsing. ([#317](https://github.com/microsoft/conductor/pull/317))

## [0.1.25](https://github.com/microsoft/conductor/compare/v0.1.24...v0.1.25) - 2026-07-21

### Fixed

- **Web dashboard warns when stuck reconnecting after a silent crash** — a
  new amber banner appears if the dashboard's WebSocket client has been
  disconnected and retrying for more than 60 seconds while a workflow is
  still marked "running", pointing at the best available log
  (`--web-bg`'s captured stderr/stdout log, the `--log-file` debug log, or
  the launching terminal) so a silently crashed `--web-bg` process is no
  longer indistinguishable from a healthy, still-running workflow.
  ([#332](https://github.com/microsoft/conductor/pull/332))

## [0.1.24](https://github.com/microsoft/conductor/compare/v0.1.23...v0.1.24) - 2026-07-21

### Added

- **Grouped CLI command surface** — related subcommands are now organised under
  noun groups: `conductor checkpoint list` (was `conductor checkpoints`) and
  `conductor gate respond` (was `conductor gate-respond`), alongside the
  existing `registry` group. The root `--help` groups commands into
  *Run & Recover*, *Author & Inspect*, *Environment*, *Interact*, and *State*
  panels, while the hot-path verbs (`run`, `resume`, `validate`, `show`,
  `stop`, `replay`, `update`, `doctor`) stay flat.
  ([#275](https://github.com/microsoft/conductor/issues/275))

### Deprecated

- **`conductor checkpoints` and `conductor gate-respond`** — replaced by
  `conductor checkpoint list` and `conductor gate respond` respectively. The old
  names still work and forward to the new commands, but print a one-line stderr
  deprecation warning, are hidden from `--help`, and will be removed in a future
  release. ([#275](https://github.com/microsoft/conductor/issues/275))

### Fixed

- **`--web-bg` no longer aborts when a workflow contains a `human_gate`** — the
  dashboard already supported resolving gates (modal, WebSocket
  `gate_response`, `POST /api/gate-respond`, `conductor gate-respond`), but the
  detached background process raced a CLI prompt against it and crashed with
  `EOFError` on its closed stdin. The gate now waits web-only when running in
  `--web-bg` (or any non-TTY context), and the CLI prints a notice pointing at
  the dashboard URL and `conductor gate-respond` instead of aborting the launch.
  ([#286](https://github.com/microsoft/conductor/issues/286))
- **`--web-bg` could hang forever if no dashboard client ever connected** — the
  auto-shutdown grace timer was only armed from WebSocket-disconnect code
  paths, so an unwatched run that finished before anyone opened the dashboard
  never started its grace countdown and the detached process became a zombie
  holding its port and PID file. The timer now also arms on the workflow's root
  completion event. ([#318](https://github.com/microsoft/conductor/issues/318))
- **`conductor doctor` showed the `copilot` provider as red/unconfigured even
  when fully authenticated** — credential env vars are now modeled as optional
  per provider; an absent optional credential (e.g. `copilot`'s GitHub/Copilot
  CLI login) renders as a neutral `○` with an explanatory note instead of a red
  `✗`, while genuinely required credentials (e.g. `claude`'s
  `ANTHROPIC_API_KEY`) still flag as missing.
  ([#319](https://github.com/microsoft/conductor/issues/319))
- **Web dashboard could keep showing a stale UI after `conductor update`** —
  `index.html` is now served with `Cache-Control: no-cache` so the browser
  revalidates it on every load and always picks up the current build's
  version-hashed asset bundle. CI also now fails the Frontend job if the
  committed `static/` bundle doesn't match a fresh `make build-frontend`, so a
  frontend change can no longer merge without its built assets.
  ([#321](https://github.com/microsoft/conductor/pull/321))

## [0.1.23](https://github.com/microsoft/conductor/compare/v0.1.22...v0.1.23) - 2026-07-20

### Added

- **`working_dir` for LLM agents and their MCP servers** — agents gain an
  optional `working_dir` (with a workflow-wide `runtime.working_dir` default) so
  an agent and its MCP servers run in a chosen directory. It is resolved with
  precedence agent > runtime > `os.getcwd()` and accepts static values or Jinja
  templates. The Copilot and Claude providers stamp the resolved directory onto
  the agent session and each MCP server; `conductor validate` rejects
  `working_dir` on providers that don't support it (`hermes`,
  `claude-agent-sdk`) and on `wait` / `set` / `terminate` / `human_gate` /
  `workflow` step types. See the "Working Directory" section of
  [`docs/mcp-tools.md`](docs/mcp-tools.md).
  ([#297](https://github.com/microsoft/conductor/pull/297))
- **`runtime.tool_output` limits for oversized MCP tool results** — a
  configurable per-result cap stops a single large MCP tool result from
  overflowing the model's context window with a fatal token-limit error.
  Oversized results are truncated (`max_chars`, default `50000`) and the full
  text is spilled to a temp file the agent can page through, with a notice
  surfaced in the console, event log, and dashboard. Claude truncates
  conductor-side; Copilot forwards the limit to its native SDK spill feature;
  the setting is ignored by `claude-agent-sdk` (managed via the native
  `MAX_MCP_OUTPUT_TOKENS`) and is N/A for `hermes`. See
  [`examples/tool-output-limits.yaml`](examples/tool-output-limits.yaml) and the
  "Tool output limits" section of [`docs/mcp-tools.md`](docs/mcp-tools.md).
  ([#313](https://github.com/microsoft/conductor/pull/313))
- **Inline expand/collapse for subworkflows in the dashboard graph** —
  subworkflow nodes now expand inline (collapsed by default) to reveal their
  internal DAG without leaving the current view, alongside the existing
  double-click drill-down "focus mode." Adds an Expand/Collapse-all toolbar
  control and an `E` keyboard shortcut, and expands `for_each`-of-workflow
  groups into inline sub-containers.
  ([#316](https://github.com/microsoft/conductor/pull/316))

### Fixed

- **Subworkflow-adjacent agent node could appear stuck "running" in the
  dashboard** — the graph store now clones the `subworkflowContexts` tree on
  every event, so selectors keyed on it reliably observe nested status updates.
  An agent node next to a `type: workflow` step no longer renders as "running"
  after its underlying data is already `completed` (a pure rendering desync, not
  an engine data bug). ([#308](https://github.com/microsoft/conductor/pull/308))

## [0.1.22](https://github.com/microsoft/conductor/compare/v0.1.21...v0.1.22) - 2026-07-15

### Added

- **`conductor doctor --models` surfaces per-model reasoning-effort support and
  context-window limits** — a new optional `AgentProvider.get_model_capabilities`
  hook (alongside the existing `get_max_prompt_tokens` / `get_model_pricing`
  hooks) reports, per model: which `reasoning.effort` levels it accepts, its
  default effort, and its prompt/output/context-window token limits. `--models`
  now renders a separate per-provider **Models** detail table with this data
  (the Providers table's Models column shows a count); the JSON `models` field
  is now a list of capability objects rather than plain id strings. The Copilot
  provider implements the hook fully via `client.list_models()`; the Claude
  provider derives reasoning-effort support from the existing thinking-model
  heuristic and reports prompt tokens only (the Anthropic SDK exposes no
  output/total-context split); other providers (`claude-agent-sdk`, `hermes`,
  `openai-agents`) don't implement model enumeration at all, so they show
  `n/a` in the Providers table and get no Models detail table. See the
  "Per-model capabilities" section in
  [`docs/cli-reference.md`](docs/cli-reference.md#per-model-capabilities---models).
  ([#301](https://github.com/microsoft/conductor/issues/301))

- **`max` reasoning-effort level** — the unified reasoning scale is now
  `low | medium | high | xhigh | max`, unifying it with the GitHub Copilot CLI.
  On the Copilot provider `max` is forwarded to the SDK and still validated
  per-model against the model's advertised `supported_reasoning_efforts`. On
  the Claude provider `max` maps to a `59904`-token extended-thinking budget
  (`64000 − 4096`, the largest budget that keeps the default answer headroom
  under the 64000-token output cap); both the main agentic-loop and dialog-turn
  code paths share the same clamping helper, so the cap is enforced
  consistently. The experimental Hermes provider keeps the original four
  levels — `max` is rejected both statically (`conductor validate`) and at
  execute time (including when a Jinja-templated `reasoning.effort` only
  resolves to `max` after rendering).
  ([#299](https://github.com/microsoft/conductor/issues/299))

### Fixed

- **Copilot per-model `reasoning_effort` validation was a silent no-op** —
  `_validate_reasoning_effort_for_model` read
  `capabilities.supported_reasoning_efforts`, but the installed
  `github-copilot-sdk` (>=1.0.0) exposes that field (and
  `default_reasoning_effort`) at the top level of the `Model` object, not
  nested under `capabilities`. The lookup always returned `None`, so the
  per-model check (including the `max`-rejection behavior from #299) never
  fired against the real SDK — a model without `max` support would only be
  caught by the backend, not by Conductor's own validation. Fixed to read the
  correct field; discovered and corrected while implementing #301, which
  needed the same field for `doctor --models`.

- **Install-script tests no longer pollute the developer's shell profile** — the
  install scripts ran `uv tool update-shell` unconditionally, so the
  `-m install_scripts` integration tests appended each run's throwaway
  `UV_TOOL_BIN_DIR` to the real `~/.zshenv` (and shell equivalents). Those stale
  entries shadowed the user's actual `conductor` install with an old `v0.0.2`
  test fixture, so `conductor` reported the wrong version and `conductor update`
  appeared to do nothing. `install.sh` / `install.ps1` now honor
  `CONDUCTOR_INSTALL_SKIP_PATH_UPDATE=1` (and a matching
  `--skip-path-update` / `-SkipPathUpdate` flag) — set by default in the test
  harness — and a regression test asserts the scripts never touch shell
  profiles.

### Documentation

- **README provider comparison table corrected** — set Context Window to
  "Per-model" across all providers, set Pricing to "Subscription" for Copilot
  and Claude Agent SDK, labeled Claude Agent SDK as experimental in the
  top-level Features list (matching the Providers table's Tier column), and
  added a "Using Copilot" section with a config example and auth notes.
  ([#296](https://github.com/microsoft/conductor/pull/296))

## [0.1.21](https://github.com/microsoft/conductor/compare/v0.1.20...v0.1.21) - 2026-07-13

### Added

- **Connect the Copilot provider to an existing runtime** — `runtime.provider`
  gains a Copilot-only `runtime_url` (plus optional `runtime_token`) that points
  Conductor at an already-running `copilot --headless` process instead of
  spawning its own nested one. Agents share the authenticated runtime process
  while retaining separate SDK sessions. Both fields also resolve from the namespaced
  `COPILOT_PROVIDER_RUNTIME_URL` / `COPILOT_PROVIDER_RUNTIME_TOKEN` environment
  variables, which activate the connection with no YAML — the zero-config path
  for external orchestrators that already own an authenticated
  Copilot process. Runtime transport can be combined with custom model-provider
  routing. See
  `examples/copilot-existing-runtime.yaml` and the "Connecting to an Existing
  Copilot Runtime" section of `docs/configuration.md`.
- **Provider-supplied model pricing** — cost reporting now resolves pricing via a
  new `AgentProvider.get_model_pricing` hook before falling back to the static
  table. Resolution order is workflow `cost.pricing` → provider hook → built-in
  `DEFAULT_PRICING` → unpriced. The Copilot provider derives live rates from its
  SDK billing metadata (AI Credits → USD), so newly-released models are priced
  without waiting for a table refresh; providers whose SDK exposes no pricing
  (e.g. the Anthropic API) fall back to the table.
  ([#265](https://github.com/microsoft/conductor/issues/265))
- **`conductor doctor` — provider & environment diagnostics** — a safe,
  read-only command that reports which providers are installed, their
  capability tier (`stable` / `experimental`), which credential environment
  variables are detected (presence only — values are never printed), plus
  Conductor version / update status and configured registries. Offline by
  default; `--check` tests provider connections, `--models` lists available
  models, `--provider NAME` scopes to one provider, and `--json` emits
  machine-readable output for CI. Exit code is `1` only when `--check` is set
  and the scoped provider (default `copilot`) fails to connect. Also adds a
  public `list_models()` method to the provider interface (implemented for
  Copilot and Claude). ([#274](https://github.com/microsoft/conductor/issues/274))
- **Jinja `include`/`import`/`extends` in `!file`-loaded prompts** — prompt
  templates loaded via `prompt: !file ...` (and `system_prompt: !file ...`) now
  support loader-dependent Jinja constructs, resolved relative to the prompt
  file's own directory, enabling reusable prompt partials across workflows.
  Inline prompts that attempt these constructs get a clear error instead of a
  confusing failure.
  ([#291](https://github.com/microsoft/conductor/pull/291),
  closes [#287](https://github.com/microsoft/conductor/issues/287))

### Fixed

- **Cost summary silently undercounted unpriced models** — the run summary summed
  only the priced subset of agents and presented it as the complete total, so
  spend on models without available pricing vanished with no signal. Unpriced
  agents are now surfaced in the CLI summary and the web dashboard status bar
  (`~$X (N agents unpriced: model-a, model-b)`), so a partial total is never
  shown as a clean, complete number.
  ([#265](https://github.com/microsoft/conductor/issues/265))
- **Missing pricing for current models costed them at $0** — several current
  models had no `DEFAULT_PRICING` entry, so `get_pricing` returned `None` and
  they were silently costed at $0 in the Token Usage Summary / cost breakdown
  (dotted version suffixes like `claude-opus-4.8` are not bridged by the
  `-`-delimited fuzzy fallback). Added entries for `claude-opus-4.7`,
  `claude-opus-4.8`, `claude-sonnet-5`, `gpt-5.3-codex`, `gpt-5.4`, `gpt-5.5`,
  `gpt-5-mini`, `gpt-5.4-mini`, and `gemini-3.5-flash`. (The related
  sub-workflow usage under-reporting in the same issue was already fixed in
  [#212](https://github.com/microsoft/conductor/pull/212).)
  ([#266](https://github.com/microsoft/conductor/issues/266))
- **`for_each` inline agents skipped most provider-capability checks** —
  `conductor validate`'s provider-capability cross-check applied the full
  per-agent matrix (reasoning effort, structured output, per-agent MCP provider
  override, explicit `max_session_seconds`) only to top-level `agents:`. A
  `for_each` group's inline agent — which runs at runtime exactly like a
  top-level agent — was checked for tool allowlists only, so it could request a
  capability its provider doesn't support (e.g. `reasoning.effort: high` on the
  `claude-agent-sdk` provider), pass validation, then fail or silently degrade
  mid-iteration. The per-agent checks are now shared and run over inline agents
  too, and the workflow-level `mcp_servers` / `max_session_seconds` inheritance
  checks now also account for inline agents on the default provider.
  ([#270](https://github.com/microsoft/conductor/issues/270))
- **For-each dive-in worked only for finished items** — in the web dashboard's
  for-each group detail panel, the per-item "Dive into subworkflow" control was
  nested inside the row's expand/collapse `<button>`, which is `disabled` while
  an item has no expandable details (a running workflow-type iteration that has
  not yet produced a prompt/output/activity/error). A disabled button suppresses
  clicks across its whole subtree, so dive-in only fired once the item had failed
  or completed. The toggle and the dive-in control are now siblings, so dive-in
  stays clickable for running items too.
  ([#273](https://github.com/microsoft/conductor/pull/273))
- **Custom `default` Jinja filter rejected the standard `boolean` argument** —
  Conductor's override of Jinja2's built-in `default` filter only accepted two
  parameters, so valid templates using the standard third `boolean` argument
  raised a `TypeError`. The filter now matches Jinja's built-in signature
  (`default(value, default_value="", boolean=False)`) while preserving
  Conductor's existing two-argument behavior when `boolean` is omitted.
  ([#292](https://github.com/microsoft/conductor/pull/292),
  closes [#288](https://github.com/microsoft/conductor/issues/288))
- **Claude provider silently dropped `agent.system_prompt`** — the native
  `claude` provider (Anthropic Messages API) ignored `system_prompt` instead of
  sending it. It is now passed as the native top-level `system` parameter on
  every API call in the agent execution path (main loop, tool-use iterations,
  parse recovery, interrupt partial output, retries), and the validator
  rubric's system prompt now reaches the model the same way.
  ([#293](https://github.com/microsoft/conductor/pull/293),
  closes [#289](https://github.com/microsoft/conductor/issues/289))

### Documentation

- **`--quiet` / `--silent` documented as `run` options** — both flags are
  root-level options defined on the app callback, so they must appear *before*
  the subcommand (`conductor --quiet run workflow.yaml`); placing them after it
  is rejected with "No such option". `docs/cli-reference.md` and the README
  listed them in the `conductor run` options table, while `docs/cli-reference.md`
  also showed post-subcommand examples (`conductor run workflow.yaml --quiet`)
  that fail as written — as did the `conductor run --help` epilog. Moved the
  flags to a new Root-Level Options section, corrected the examples (docs and
  `--help` text), and fixed the same mis-ordering in the conductor skill's
  quick reference.

## [0.1.20](https://github.com/microsoft/conductor/compare/v0.1.19...v0.1.20) - 2026-06-26

### Added

- **Hermes provider (experimental)** — optional third provider built on the
  NousResearch [`hermes-agent`](https://github.com/NousResearch/hermes-agent)
  library, which manages its own tool ecosystem (no MCP configuration). Install
  separately with `pip install hermes-agent`; Conductor works without it.
  Declared in the experimental provider tier with documented capability
  carve-outs (no MCP servers, no per-agent workflow `tools:` allowlist,
  structured output via prompt injection). Supports custom endpoints via
  structured `runtime.provider` (`base_url` / `api_key`), `hermes_home`
  profiles, `hermes_toolsets`, streaming + reasoning event callbacks,
  cooperative interrupt, session-history resume, and `max_session_seconds`.
  See [`docs/providers/hermes.md`](docs/providers/hermes.md).
  ([#235](https://github.com/microsoft/conductor/pull/235))
- **Cost budget enforcement** — set a USD ceiling for a run with
  `runtime.budget_usd` and choose how the engine reacts via `runtime.budget_mode`:
  `audit` (default — track spend and warn as the cap is approached) or `enforce`
  (stop the run once the projected cost would exceed the cap). Off by default — no
  budget tracking happens unless `budget_usd` is set.
  ([#212](https://github.com/microsoft/conductor/pull/212))
- **External-workflow friction improvements** — four knobs that surfaced while
  running real-world workflows: per-agent `output_mode` (`raw` | `envelope`) for
  cross-provider output-shape parity; `retry.max_parse_recovery_attempts` to cap
  the in-session JSON-correction prompts sent when an agent's output fails to
  parse; a new `conductor gate-respond --port <port> --choice <value>` command to
  resolve a human gate from the CLI (handy for `--web` / `--web-bg` runs); and
  more robust Windows path handling.
  ([#234](https://github.com/microsoft/conductor/pull/234))
- **Templated `reasoning.effort` and `context_tier`** — both per-agent fields now
  accept Jinja2 templates (e.g. `effort: "{{ workflow.input.eff }}"`) resolved at
  runtime against the workflow context, instead of being rejected at YAML load as
  invalid enum literals. The rendered value is re-validated against the allowed
  set. ([#263](https://github.com/microsoft/conductor/pull/263),
  closes [#262](https://github.com/microsoft/conductor/issues/262))
- **Scoped `applyTo` instruction loading** — `.github/instructions/*.md` files
  with a scoped `applyTo` glob (e.g. `**/*.cs`, `services/foo/**`) are now loaded
  when their scope overlaps the run's working directory, instead of being silently
  dropped unless `applyTo: "**"`. Multi-glob values separated by `;` or `,` are
  supported, and the closest-owning convention directory wins for nested
  instruction files.
  ([#238](https://github.com/microsoft/conductor/pull/238),
  closes [#231](https://github.com/microsoft/conductor/issues/231))

### Fixed

- **Copilot model attribution for auto-routed runs** — when an agent uses
  `model: auto`, `AgentOutput.model` now records the concrete model the Copilot
  SDK resolved to (captured from the `assistant.usage` event) instead of the
  literal string `"auto"`, so token usage and cost are attributed to the real
  model. ([#268](https://github.com/microsoft/conductor/pull/268))
- **claude-agent-sdk default tool preset** — an agent that omits `tools:` on the
  `claude-agent-sdk` provider again receives the full `claude_code` preset instead
  of zero tools. The omitted-vs-empty distinction (`tools:` absent means "all
  tools"; `tools: []` means "none") was being lost at the executor→provider
  boundary whenever the workflow declared no MCP tools.
  ([#269](https://github.com/microsoft/conductor/pull/269))

## [0.1.19](https://github.com/microsoft/conductor/compare/v0.1.18...v0.1.19) - 2026-06-16

### Added

- **Context tier** — new `context_tier` knob (`default` | `long_context`) to
  select a model's long-context (e.g. 1M-token) window on the Copilot provider.
  Set per agent via `context_tier:` (sibling to `model`) or workflow-wide via
  `runtime.default_context_tier`; the per-agent value wins. It composes
  independently with `reasoning.effort` (the two map to separate
  `create_session` kwargs). The Copilot provider forwards the resolved value as
  `context_tier` to `create_session`; other providers ignore it. Only valid on
  standard `agent`-type agents (rejected on `script`, `human_gate`, and
  `workflow` agents). See [`examples/context-tier.yaml`](examples/context-tier.yaml)
  and [Context Tier](docs/configuration.md#context-tier).
  ([#251](https://github.com/microsoft/conductor/issues/251))
- **Validator block** — an optional `validator:` block on provider-backed
  agents that runs a second LLM call to grade the agent's output against a
  user-defined rubric. Distinct from `retry:` (transient failures, same
  prompt) and `output:` (shape/type): it catches structurally valid but
  semantically wrong, incomplete, or off-rubric output. Fields: `criteria`
  (required), `model` (defaults to the agent's model), and `max_retries`
  (`0` or `1`, default `1`, hard-capped at `1`). On `passed: false` the agent
  re-runs **once** with the validator's issues appended under a
  `## Validation feedback` section; the second output is final (no second
  validation loop). Validation is **fail-open** — grader errors or unparseable
  responses are logged and treated as a pass, so a hung or broken grader can't
  block the workflow. Wired into the main loop, parallel groups, and for-each
  loops; emits `agent_validator_start` / `agent_validator_complete` /
  `agent_validation_failed` events surfaced in `--verbose` console output and
  the web dashboard, and records the grading call (plus any discarded first
  attempt) as a separate `<agent> (validator)` usage row. Rejected on
  `script` / `human_gate` / `workflow` / `wait` / `set` / `terminate` steps.
  See [`examples/validator.yaml`](examples/validator.yaml) and the
  [Validator section](docs/workflow-syntax.md) of the workflow syntax docs.
  ([#256](https://github.com/microsoft/conductor/pull/256),
  closes [#220](https://github.com/microsoft/conductor/issues/220))
- **Periodic / milestone checkpoints** — opt-in `runtime.checkpoint` block that
  saves a resumable checkpoint at step boundaries so a long run that stalls or
  is hard-killed stays recoverable (previously Conductor only checkpointed on
  failure). Two OR-combined triggers — `every_agent: true` (save at every step
  boundary) and `every_seconds: N` (a throttle: save at the first boundary once
  `N` seconds have elapsed since the last checkpoint) — plus `keep_last`
  (default `5`) to rotate older periodic checkpoints per run. Off by default, so
  failure-only behaviour is preserved. Periodic checkpoints are scoped to their
  own run and trigger, so failure checkpoints and other runs' files are never
  rotated away, and they are cleaned up automatically on clean completion or an
  explicit `terminate`. A failed periodic save emits a `checkpoint_save_failed`
  event (console + JSONL + dashboard) so a recovery-reliant run is never left
  silently without checkpoints, and `conductor checkpoints` gains a `Trigger`
  column. See
  [`examples/periodic-checkpoints.yaml`](examples/periodic-checkpoints.yaml)
  and the [Periodic Checkpoints section](docs/workflow-syntax.md) of the
  workflow syntax docs.
  ([#255](https://github.com/microsoft/conductor/pull/255),
  closes [#244](https://github.com/microsoft/conductor/issues/244))
- **Script `stdin:` payload transport** — `type: script` steps accept a new
  `stdin:` field: a Jinja2 string template rendered against the workflow
  context and piped to the child process on stdin as UTF-8. Workflows can now
  hand large or structured payloads to scripts without hitting command-line
  length limits (notably Windows, which caps the command line at ~32 KB),
  removing argument-name-specific temp-file workarounds. `stdin` and `args` are
  orthogonal; an explicit empty string pipes an immediate EOF, and omitting it
  preserves the legacy behaviour of inheriting the parent's stdin. The payload
  is streamed in the background so multi-MB inputs can't deadlock, the submitted
  byte count is surfaced as `stdin_bytes` on the `script_completed` event, and
  an unencodable payload raises a named `ExecutionError`. Rejected on every
  non-script step type. See
  [`examples/script-stdin.yaml`](examples/script-stdin.yaml).
  ([#253](https://github.com/microsoft/conductor/pull/253),
  refs [#18](https://github.com/microsoft/conductor/issues/18))
- **Experimental provider tier** — providers that delegate part of the agentic
  loop to an upstream SDK can now declare an *experimental* stability tier with
  explicit, allowed capability carve-outs instead of silently eroding provider
  parity. Every provider declares a class-level `CAPABILITIES`
  (`ProviderCapabilities`) descriptor, and `conductor validate` cross-checks
  workflow features against each agent's resolved provider — surfacing silent
  mismatches (unsupported `runtime.mcp_servers`, non-empty per-agent `tools:`
  allowlists, `reasoning.effort`, structured `output:`, concurrent use in
  parallel/for-each groups, `max_session_seconds`) at validate time rather than
  at runtime. The `workflow_started` event gains a `providers` block (tier,
  upstream pin, maintainer, full capability dump) plus a `provider_name` per
  agent; the CLI prints a one-time banner per experimental provider per run, and
  the web dashboard renders a yellow "exp" badge on affected agent nodes. See
  [Experimental Providers](docs/providers/experimental.md).
  ([#242](https://github.com/microsoft/conductor/pull/242),
  closes [#241](https://github.com/microsoft/conductor/issues/241))
- **`claude-agent-sdk` provider** — a new, experimental provider that delegates
  the agentic loop, tool execution, and structured-output extraction to the
  Claude Code CLI via the `claude-agent-sdk` package. Unlike the raw `claude`
  provider it does not manage its own retry logic, MCP servers, or tool wiring —
  the SDK runtime owns those. It achieves event and output parity
  (`agent_turn_start`, `agent_message`, `agent_reasoning`,
  `agent_tool_start` / `agent_tool_complete`, and the standard `AgentOutput`
  shape), pairing real `ToolResultBlock` results rather than emitting nulls.
  Workflow-level `runtime.mcp_servers`, non-empty per-agent `tools:` lists, and
  `temperature` / `max_tokens` are rejected at the factory (the CLI controls
  these). Install with the optional extra
  (`pip install conductor[claude-agent-sdk]`) plus the `claude` CLI. See
  [`examples/experimental-claude-agent-sdk.yaml`](examples/experimental-claude-agent-sdk.yaml).
  ([#104](https://github.com/microsoft/conductor/pull/104))

### Fixed

- Web dashboard **Stop/Kill now always writes a checkpoint** (or clearly
  explains why it couldn't). Previously, killing a run while an agent was
  actively executing — or clicking Stop during the brief startup window before
  the engine bound its interrupt event — cancelled the engine task from the CLI
  wrapper, bypassing the engine's failure handling so **no checkpoint and no
  `workflow_failed` event** were produced and progress was silently lost. Now:
  - A dashboard stop that cancels the engine routes through a best-effort
    checkpoint + `workflow_failed`/`checkpoint_saved` emit
    (`WorkflowEngine.handle_dashboard_stop`), so the run is resumable with
    `conductor resume`.
  - `POST /api/stop` during startup is **queued** until the interrupt event is
    bound (graceful pause path) instead of falling back to a hard cancel.
  - The dashboard shows a dedicated, calm **"Workflow Stopped"** banner with
    `Checkpoint saved: <path>` — or `No checkpoint could be saved — <reason>`
    when one genuinely couldn't be written — instead of an alarming red
    "Workflow Failed". ([#245](https://github.com/microsoft/conductor/issues/245))
- `human_gate` agents: the dict returned by `prompt_for` text-collection fields
  is no longer spread into the gate's output root, where it could silently
  overwrite the reserved `selected` key (e.g. an option declaring
  `prompt_for: selected` would clobber the chosen option value with whatever
  the user typed). Collected values are now nested under an explicit
  `additional_input` key, matching the shape the `gate_resolved` event already
  used. ([#237](https://github.com/microsoft/conductor/pull/237))
- `context: explicit` mode now supports **nested output projection** in
  `input:` declarations. References of the form
  `agent_name.output.field.subfield...` (and the
  `agent_name.field.subfield...` shorthand) project arbitrarily deep into a
  prior step's structured output, where previously only a single level
  (`agent_name.output.field`) resolved and deeper paths silently failed.
  Optional refs (`?`) skip missing intermediate paths, and projected leaves are
  deep-copied to avoid mutation aliasing. Static validation in
  `conductor validate` was aligned with the runtime so valid nested references
  no longer fail validation.
  ([#239](https://github.com/microsoft/conductor/pull/239))

### Changed

- **BREAKING (templates)** — `human_gate` output shape changed.
  - Before: `{{ <gate>.output.<prompt_for_field> }}` (root-level).
  - After: `{{ <gate>.output.additional_input.<prompt_for_field> }}` (nested).
  - Gates without any `prompt_for` now produce `additional_input: {}` rather
    than just `{"selected": ...}` — the key is always present.
  - `<gate>.output.selected` is unchanged.
  - Templates that referenced the old flat path now raise `TemplateError`
    (`StrictUndefined`), so the migration fails loudly rather than rendering
    to empty strings.
  - In `context: explicit` mode, `input:` declarations can reference either
    `<gate>.output.additional_input` (the whole dict) or an individual
    `<gate>.output.additional_input.<field>`. Nested explicit-input projection
    landed in this same release
    ([#239](https://github.com/microsoft/conductor/pull/239)), so the dotted
    field path now resolves directly; you can also still declare the parent and
    read individual fields via Jinja2 in the consuming agent's prompt or output
    template.

## [0.1.18](https://github.com/microsoft/conductor/compare/v0.1.17...v0.1.18) - 2026-05-28

### Added
- New `type: set` workflow step that evaluates Jinja2 expressions and binds
  the results into the workflow context — no LLM call, no subprocess, no I/O.
  Two surface forms: `value:` (single expression bound as `<step>.output`,
  scalar / list / dict by auto-detection or explicit `output_type:`) and
  `values:` (named bindings rendered in one pass against the pre-step context
  and bound as `<step>.output.<key>`). Type detection defaults to YAML
  auto-parsing with a JSON-safety pass that converts `datetime`/`date`/`time`
  to ISO 8601 strings and raises `ExecutionError` on other non-JSON-safe
  values (including non-string dict keys) so checkpoint round-trips stay
  stable. Explicit `output_type:` (single-`value` only) supports `string`,
  `number`, `integer`, `boolean`, `list`, `dict`. The engine dispatches set
  steps in the main loop, parallel groups, and for-each groups via the
  shared `_run_set_step` helper, emitting `set_started` / `set_completed` /
  `set_failed` and enforcing the `output:` schema (rejected for scalar
  outputs with a friendly suggestion). `WorkflowContext.store` was widened
  to accept any JSON-safe value; `_add_agent_input` returns scalars verbatim
  for `step.output` and raises a clear `KeyError` for `step.output.field`
  shorthand on non-dict outputs. The web dashboard adds a dedicated `SetNode`
  (variable icon, key count / value preview) and `SetDetail` panel showing
  output type, bindings, and rendered value. New `examples/set-step.yaml`
  demonstrates single + multi binding plus a boolean route on the derived
  flag
  ([#226](https://github.com/microsoft/conductor/pull/226),
  closes [#221](https://github.com/microsoft/conductor/issues/221)).
- New `type: wait` workflow step that pauses execution for a parsed
  duration via in-process `asyncio.sleep`. Cross-platform — no shell
  `sleep` dependency. Use for rate-limit cooldowns, polling intervals,
  external-system catch-up, and demos. The `duration:` field accepts
  plain numbers (seconds), suffixed strings (`"500ms"`, `"60s"`,
  `"2.5m"`, `"1h"`), or a Jinja2 template that renders to one of
  those (e.g. `"{{ workflow.input.poll_interval }}s"`). Schema enforces
  `0 < duration <= 24h` and rejects boolean values pre-coercion.
  `Esc` / `Ctrl+G` cancels in-progress waits immediately (the engine
  races the sleep against the interrupt event), and the workflow-level
  `limits.timeout_seconds` also cancels them. Wait steps emit
  `wait_started` / `wait_completed` / `wait_failed` events alongside
  the generic `agent_started` (with `agent_type: "wait"`), so existing
  dashboards keyed on agent lifecycle pick them up automatically. The
  dashboard adds a dedicated `WaitNode` (clock icon) and `WaitDetail`
  panel that show the requested duration, actual elapsed time, reason,
  and an "interrupted" indicator. The public output contract is strict
  — only `{"waited_seconds": float}` is exposed to workflow context;
  extra metadata lives in event payloads. Wait steps count toward
  `limits.max_iterations` (each pause is one step) but are not subject
  to `max_agent_iterations` (per-LLM-agent tool counter). Wait cannot
  be used inside `parallel` or `for_each` groups. New `examples/wait-step.yaml`
  demonstrates a polling pattern with a templated poll interval and
  route loop-back
  ([#224](https://github.com/microsoft/conductor/pull/224),
  closes [#218](https://github.com/microsoft/conductor/issues/218)).
- New `type: terminate` workflow step that explicitly ends the workflow with
  a structured `status` (`success` | `failed`) and Jinja2-rendered `reason`,
  plus an optional `output_template` (`dict[str, str]`) that replaces the
  workflow-level `output:` mapping for that termination path. Reaching a
  terminate step ends the workflow immediately (no routes evaluated after).
  `status: success` returns the rendered output cleanly (CLI exit 0,
  dashboard ✅, emits `workflow_completed { termination_reason, terminated_by,
  is_explicit: true, status: "success" }`); `status: failed` raises a new
  `WorkflowTerminated` exception (`ExecutionError` subclass), gives the CLI a
  non-zero exit code while still printing the rendered output JSON to stdout
  for downstream tooling, and intentionally **skips** the on-failure
  checkpoint save because explicit termination is not a resumable transient
  failure. Inside a sub-workflow, a failed terminate is downgraded at the
  parent boundary to a new `SubworkflowTerminatedError` (also an
  `ExecutionError`) preserving the child's rendered `terminated_output` /
  `terminated_reason` / `terminated_by` as structured attributes, so the
  parent treats it as a normal sub-workflow failure (its own
  `workflow_failed` does NOT inherit `is_explicit: true`) while debugging
  surfaces can still inspect what the child intended to emit. Schema
  validation rejects `routes`, `tools`, `output`, `prompt`, `model`,
  `provider`, and the other agent-only fields on terminate steps, and
  conversely rejects `status` / `reason` / `output_template` on every other
  step type so authors who forget `type: terminate` get a clear error
  instead of silently dropped fields. Terminate cannot be used as a
  parallel-group member or as a `for_each` inline agent — route to one
  from those groups' `routes:` instead. The example workflow lives at
  `examples/terminate.yaml`
  ([#219](https://github.com/microsoft/conductor/issues/219)).
- `runtime.provider` now accepts either the bare string shorthand
  (`provider: copilot`) or a structured `ProviderSettings` object that
  forwards a `ProviderConfig` to the Copilot SDK's
  `create_session(provider=…)` parameter. This lets workflows route the
  Copilot SDK at OpenAI-compatible / Azure / Anthropic endpoints —
  Ollama, vLLM, LM Studio, Azure OpenAI, llamafile, or any other
  OpenAI-compatible REST endpoint — instead of being locked to the
  GitHub Copilot service. The structured form supports `name`, `type`
  (`openai`|`azure`|`anthropic`), `wire_api`
  (`completions`|`responses`), `base_url`, `api_key`, `bearer_token`,
  `headers`, and `azure.api_version`. `api_key` and `bearer_token` are
  Pydantic `SecretStr` (redacted in `model_dump`, dashboard payloads,
  event logs, and checkpoints). Custom routing activates only when YAML
  sets at least one non-`name` field — ambient `OPENAI_*` env vars
  never divert default routing on their own. Once activated, missing
  fields fall back from `COPILOT_PROVIDER_BASE_URL` → `OPENAI_BASE_URL`
  for `base_url`, `COPILOT_PROVIDER_API_KEY` for `api_key`, and
  `COPILOT_PROVIDER_BEARER_TOKEN` for `bearer_token`. Ambient
  `OPENAI_API_KEY` is intentionally NOT consulted as an implicit
  fallback (credential-leak risk); use `api_key: ${OPENAI_API_KEY}`
  YAML interpolation for explicit opt-in. The schema rejects every
  non-`name` field when `name != "copilot"` (structured config for
  Claude / openai-agents is a follow-up), and rejects anchorless or
  empty combinations (`wire_api` / `type` / `headers` / `azure` alone,
  empty `headers`, empty `SecretStr`, empty `azure` block) so silent
  no-ops cannot reach the SDK. Custom routing applies to both agent
  execution and dialog turns so all sessions hit the same endpoint.
  See `examples/copilot-local-llm.yaml` and
  [Configuration → Custom Provider Routing](docs/configuration.md#custom-provider-routing-ollama--vllm--azure-openai)
  ([#225](https://github.com/microsoft/conductor/pull/225),
  [#136](https://github.com/microsoft/conductor/issues/136)).

### Added
- New `output_mode` field on `AgentDef` (`raw` | `envelope`). Setting
  `output_mode: raw` bypasses JSON schema injection and parse-recovery entirely,
  wrapping the model's response as `{"result": "<text>"}`. Useful for agents
  that produce large Markdown, prose, or code output that should not be
  JSON-extracted. `output_mode: raw` is incompatible with `output:` — declaring
  both raises a `ValidationError` at config load time.
- New `max_parse_recovery_attempts` field on `RetryPolicy` (YAML `retry:`
  block, per-agent or workflow-level). Overrides the provider default (Copilot:
  5, Claude: 2) for agents that need tighter or looser in-session parse-recovery
  budgets. Accepts integer 0–10; `0` disables all recovery attempts and lets
  the first parse failure propagate immediately. Threaded through both the
  Copilot and Claude providers.
- New `POST /api/gate-respond` and `GET /api/gate-status` HTTP API endpoints
  on the web dashboard server. `GET /api/gate-status` returns whether a
  `human_gate` agent is currently waiting, and which agent name it is.
  `POST /api/gate-respond` resolves the parked gate by injecting a
  `GateResponse` into the engine's queue. When the optional
  `CONDUCTOR_GATE_TOKEN` secret is configured on the server, `POST
  /api/gate-respond` requires an `Authorization: Bearer <token>` header
  matching it (compared in constant time) — requests with a missing or
  mismatched token are rejected with HTTP 403. `GET /api/gate-status` is
  unauthenticated. The matching WebSocket `gate_response` path enforces the
  same token and waiting-state checks so it cannot be used to bypass auth.
- New `conductor gate-respond` CLI command for resolving a parked human gate
  from the command line without opening a browser. Accepts `--port`, `--choice`,
  `--agent` (auto-discovered via `/api/gate-status` when omitted), `--input`,
  and `--token` / `CONDUCTOR_GATE_TOKEN` env var. Designed for SSH or headless
  environments where the web dashboard UI is unreachable.
- `script` steps now resolve a bare command name (e.g. `python`) or an
  extension-less path against the executable search path before launching, so
  the binary the shell would pick is the one that runs (and a Windows path
  missing its `.exe`/`.cmd` suffix resolves correctly). Resolution uses the
  subprocess's own `PATH` — including any `env.PATH` override on the step — so
  the resolved binary matches what the child process would execute. Relative
  paths containing a separator are left untouched so they keep resolving against
  `working_dir`, and an unresolvable command falls back to the rendered value so
  the existing not-found error still fires.

### Changed
- **Breaking (Claude provider):** `ClaudeProvider._extract_text_content` now
  returns `{"result": "<text>"}` instead of `{"text": "<text>"}`. This aligns
  the Claude provider with the Copilot provider (cross-provider parity). Any
  existing Claude workflow that references `{{ <agent>.output.text }}` must be
  updated to `{{ <agent>.output.result }}`. Workflows that declare an `output:`
  schema are unaffected (the schema fields take precedence). See the new
  `output_mode: raw` feature if you need to consume unstructured text output
  reliably across both providers.

### Fixed
- `_verbose_console` is now silent-aware at the source: a `_SilentAwareConsole`
  subclass no-ops every `.print(...)` when `is_verbose()` is False, so the
  remaining `conductor --silent` stderr leaks (dashboard-failed-to-start and
  log-file-open warnings, workflow-hash mismatch, "Press Esc to interrupt",
  "Event log written to…", "Log written to…", `_print_resume_instructions`,
  and the replay command's "Press Ctrl+C to exit" / "Replay stopped"
  banners) no longer reach stderr. The app-wide `console` remains
  unchanged because it carries real error messages; the two replay prints
  are gated per-call. `conductor --silent replay <log>` now produces zero
  bytes on stderr
  ([#223](https://github.com/microsoft/conductor/pull/223),
  closes [#209](https://github.com/microsoft/conductor/issues/209)).
- Parse-exhaustion `ProviderError` (after all in-session recovery attempts
  are spent) is now marked `is_retryable=False` in both Copilot and Claude
  providers. Previously Copilot marked it `is_retryable=True`, causing the
  outer retry loop to re-run the entire agent up to 3× on deterministic
  parse failures — burning tokens with no chance of success.
- Parse-exhaustion error messages now include the first 500 characters of the
  model's response (up from 200) and suggest `output_mode: raw` as a fix.
- `parse_json_output` and the Copilot provider's `_extract_json` now use a
  two-stage fenced-block extraction (non-greedy `re.findall` + per-candidate
  try-parse, then a greedy single-capture fallback) so JSON whose string
  fields contain triple-backtick substrings no longer matches prematurely
  and falls into parse-recovery loops, while responses with multiple
  fenced JSON blocks still pick the first valid one. Resolves a recurring
  failure mode for agents emitting Markdown-bearing JSON
  (external-workflow-friction Issue #1)
  ([#232](https://github.com/microsoft/conductor/pull/232)).
- `conductor run --web-bg` and `conductor resume --web-bg` now abort before
  forking when the workflow contains a `human_gate` agent (including gates
  nested in `for_each.agent`) and `--skip-gates` is not set, with a message
  listing the four supported options. `resume --web-bg` also recovers the
  workflow path from the checkpoint when invoked without an explicit
  workflow argument so the guard still fires. Previously the detached
  child crashed with `EOFError` and the parent only reported
  "Background process exited immediately with code 1" (Issue #8).

### Documentation
- New "Choosing whether to declare `output:`" section in
  [docs/workflow-syntax.md](docs/workflow-syntax.md) describing when to declare
  a schema versus consuming raw `<agent>.output.result` for prose or large
  JSON. Closes a documentation gap that contributed to misconfiguration of
  agents emitting large payloads (Issue #2).
- `docs/cli-reference.md` `--web-bg` section now documents the `human_gate`
  incompatibility and the new pre-fork validation behavior.

### Added
- Workflow `limits.budget_usd` and `limits.budget_mode` (`audit` | `enforce`)
  cap cumulative LLM cost across a run. `audit` (default) emits a
  `budget_exceeded` event and continues so users can profile costs before
  enforcing; `enforce` saves a checkpoint and stops with
  `BudgetExceededError`. Resuming with `conductor resume` starts a fresh
  budget window (cumulative spend resets to $0), so the remaining work runs
  under a full budget — raising `budget_usd` first is optional. Sub-workflow
  spend is merged into the parent so a parent budget accounts for delegated
  cost. Schema, engine enforcement at all five existing limit-check points,
  resume parity for restored budget state, and the new `BudgetExceededError`
  type are wired end-to-end. See
  [docs/workflow-syntax.md](docs/workflow-syntax.md#cost-budget) and
  [docs/configuration.md](docs/configuration.md) for the graduation path.

## [0.1.17](https://github.com/microsoft/conductor/compare/v0.1.16...v0.1.17) - 2026-05-21

### Added
- Script agents can now declare an `output:` schema using the same
  OutputField syntax as LLM agents. When declared, the engine parses
  stdout as JSON and validates it against the schema before emitting
  `script_completed`; missing fields, wrong types, non-JSON stdout,
  empty stdout, and JSON arrays/scalars all raise `ValidationError` and
  emit `script_failed` (with stdout/stderr/exit_code) instead of
  completing. Validation runs on the **merged** output dict so declared
  `stdout` / `stderr` / `exit_code` fields validate the value
  downstream actually sees (matching the PR #122 shadowing contract).
  An explicit `output: {}` opts into strict JSON-object mode with zero
  required fields. Without a declared schema, the legacy best-effort
  JSON-stdout auto-merge from PR #122 is fully preserved, so this is
  purely additive. Routing conditions can now reference declared fields
  (e.g. `when: "phase == 'planning'"`) rather than opaque exit codes
  ([#206](https://github.com/microsoft/conductor/pull/206),
  [#118](https://github.com/microsoft/conductor/issues/118)).
- `conductor validate` now warns on undeclared `agent.output` references
  and field-level mismatches in `explicit` context mode, closing two
  follow-up gaps left by PR #125 that still produced the runtime
  `TemplateError: 'dict object' has no attribute 'X'` from issue #105.
  The validator now tracks declared fields per agent root (`a.output.foo`
  vs `a.output.bar`), so a prompt that references an undeclared field on
  an otherwise-declared agent surfaces a warning instead of a runtime
  failure; the same logic applies to static parallel groups
  (`pg.outputs.member.field`). Output-vs-error namespaces are tracked
  independently so `input: ["pg.errors"]` no longer silently suppresses
  warnings for `{{ pg.outputs.* }}` references, and the AST walker now
  filters inner-link `Getattr` nodes (no more spurious whole-output
  refs from `{{ a.output.bar }}` chains), detects method-call nodes
  (`{% for k,v in a.output.items() %}` registers as a whole-output ref),
  and degrades gracefully on `TemplateAssertionError`. For-each groups
  remain skipped (whole-member copy makes field precision a false
  positive); `human_gate` is now correctly excluded from `agent.output`
  warnings since the engine renders gate prompts in accumulate mode
  ([#208](https://github.com/microsoft/conductor/pull/208), refs #105).

### Changed
- Copilot provider verbose log lines (tool calls, reasoning, processing
  indicators, idle/parse recovery) are now prefixed with the originating
  agent name in parallel and for-each runs, eliminating the
  un-attributable interleaved output that made the for-each case
  unreadable (every iteration previously shared the same agent name).
  An optional `agent_name` parameter is plumbed through
  `_execute_sdk_call` → `_send_and_wait` → `_log_event_verbose` and
  rendered as a magenta `[agent_name]` tag between the tree icon and
  event content (continuation lines tagged too). For-each iterations
  additionally get a `model_copy()` of the per-iteration agent with
  `name = f"{name}[{key}]"` so each iteration produces a distinct tag;
  the original `AgentDef` is untouched and context lookups still use
  the unqualified name. Static parallel groups are unaffected — each
  agent already has a unique name. The `_item_callback` merge order is
  flipped so the wrapper's `agent_name`/`item_key` win over any
  qualified name the provider emits, preserving the dashboard/JSONL
  event contract (`agent_name` = for-each group name; `item_key`
  disambiguates iterations). Backward compatible: `agent_name` defaults
  to `None` for sequential agents
  ([#207](https://github.com/microsoft/conductor/pull/207), closes #16).

### Fixed
- `conductor resume … --web` and `--web-bg` no longer open an empty
  dashboard. Checkpoints now record the original `run_id` and JSONL
  `event_log_path`. On resume the dashboard's history is seeded BEFORE
  it accepts clients: the CLI prepends a fresh `workflow_started` event
  built from the current YAML (so historical events apply to the
  correct topology), then replays the original JSONL log line-by-line
  (or, when no log file is available, synthesises minimal
  `*_started`/`*_completed` pairs from the restored execution history).
  The resumed engine's own `workflow_started` emit is suppressed so the
  dashboard sees exactly one root start — no `wfDepth` double-counting.
  Root-level `workflow_completed` / `workflow_failed` /
  `checkpoint_saved` events from the original run are filtered out on
  replay; subworkflow lifecycle events are preserved so the frontend's
  context tracking stays balanced. The resumed `EventLogSubscriber`
  appends to the original log, preserving `run_id` across resume
  generations so log/timeline correlation tools see one continuous run
  (#167).
- `--web-bg` startup crashes on Windows are no longer silent
  ([#116](https://github.com/microsoft/conductor/issues/116)). Three
  changes work together to make any crash forensically traceable:
  - `conductor.cli.bg_runner` now captures the detached child's stdout
    and stderr to log files in `$TMPDIR/conductor/` (named to match the
    existing `.events.jsonl` filename) instead of discarding them with
    `subprocess.DEVNULL`. A Python traceback or `faulthandler` dump from
    the child now survives the parent's exit. The captured stderr path
    is printed alongside the dashboard URL and is included in every
    background-launch failure message so users always know where to
    look.
  - `conductor/__init__.py` enables `faulthandler` at import time
    (writing to `sys.__stderr__`), so a native crash — segfault, abort,
    fatal Python error — dumps a Python-level stack trace into the
    captured stderr log.
  - `WorkflowEngine._execute_loop` now catches `BaseException` (in
    addition to the existing `KeyboardInterrupt` / `ConductorError` /
    `Exception` arms) and emits a `workflow_failed` event with
    `is_base_exception: true` before re-raising. A bare `SystemExit` or
    other non-`Exception` failure between `agent_started` and
    `agent_prompt_rendered` now leaves a structured failure event in the
    JSONL log instead of an unexplained two-event truncation. An
    explicit `except asyncio.CancelledError: raise` arm sits in front of
    it so a normal dashboard-stop or parent cancellation is not
    mis-reported as an unexpected failure.
  Two new env vars (`CONDUCTOR_RUN_ID`, `CONDUCTOR_BG_STDERR_LOG`,
  `CONDUCTOR_BG_STDOUT_LOG`) propagate the parent-chosen run id and log
  paths to the child so the bg log files and the child's events JSONL
  share an 8-hex run id in their filenames, and `workflow_started`
  system metadata surfaces both bg log paths to the dashboard. The root
  cause of the underlying intermittent Windows crash is still pending —
  this change makes it diagnosable rather than invisible.

- Workflows that configure `reasoning.effort` (or workflow-wide
  `runtime.default_reasoning_effort`) on the Copilot provider were broken
  for **every named Copilot model** when running against
  `github-copilot-sdk` 0.3.0. The SDK's `models.list` response includes a
  `billing` object on every model, but none of them currently ship the
  `multiplier` field that the SDK's `ModelBilling.from_dict` parser
  treats as required — so every model in the response triggers
  `ValueError("Missing required field 'multiplier' in ModelBilling")`,
  which kills the entire `list_models()` call. The error then leaked
  through the narrow `except` tuple in
  `_validate_reasoning_effort_for_model` (and `get_max_prompt_tokens`),
  poisoned the retry loop, and surfaced as `Dialog turn failed: …` after
  three wasted attempts. (`get_max_prompt_tokens` was rescued by the
  engine's outer `except Exception`, so context-window metadata was
  silently unavailable rather than fatal.)
  Both metadata methods now catch any `Exception` raised at the SDK
  boundary and treat the failure as "metadata unavailable" — validation
  is skipped permissively and the configured `reasoning_effort` is
  forwarded to `create_session` as before.
  `asyncio.CancelledError`/`KeyboardInterrupt`/`SystemExit` (all
  `BaseException` subclasses) still propagate.
- `conductor resume --web-bg` (and `--web`) no longer exit silently when
  a workflow exceeds `max_iterations`. The bg child was forked with
  `--no-interactive` and `stdin=subprocess.DEVNULL`, so when the engine
  hit the limit, `IntPrompt.ask` raised `EOFError`, got coerced to `0`
  (stop), and the workflow ended with no way to recover. The
  max-iterations gate can now be resolved from the dashboard. New
  resolution policy: `skip_gates` auto-stops (unchanged); no web
  dashboard uses the legacy CLI prompt (unchanged); web dashboard +
  bg/non-TTY stdin uses a **web-only** wait (the CLI prompt is
  deliberately NOT raced because it would synchronously `EOFError` and
  win every dashboard click), with `dashboard.wait_for_stop()` racing
  so `POST /api/stop` can terminate the wait when no dashboard tab is
  open; web dashboard + TTY foreground races CLI vs web. Each
  `iteration_limit_reached` payload carries a uuid4 `gate_id` that the
  dashboard must echo back in `iteration_limit_response`, and the
  server matches/discards stale responses so a delayed double-click
  cannot be misapplied to a later gate. `iteration_limit_resolved`
  includes the same `gate_id` so subscribers can correlate the pair.
  New top-level `IterationLimitModal` (parallel-group gates can't
  attach to a per-agent panel) shows iteration count, recent agent
  history, and number input; it is hidden when `skip_gates` is true
  and does not close on Escape so the workflow can't be accidentally
  orphaned ([#202](https://github.com/microsoft/conductor/pull/202),
  fixes #198).
- `conductor run --web-bg` and `conductor resume --web-bg` no longer
  get killed within ~10 seconds when launched from a shell wrapper
  that runs commands inside a Windows job object with
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (GitHub Actions runners, VS Code
  integrated terminal, JetBrains IDE terminals, GitHub Copilot CLI
  shell tool). The detached child previously inherited the parent's
  job and died with it; users saw a dashboard URL but the workflow
  never made progress. The `Popen` call now requests
  `CREATE_BREAKAWAY_FROM_JOB` in addition to
  `CREATE_NEW_PROCESS_GROUP` so the child fully detaches. In hardened
  CI environments that clear `JOB_OBJECT_LIMIT_BREAKAWAY_OK`,
  `CreateProcess` raises `ERROR_ACCESS_DENIED`; in that case a visible
  stderr warning is emitted (so the user understands bg mode may not
  survive shell exit) and the spawn is retried without the breakaway
  flag. Other `OSError`s propagate unchanged so the existing
  `RuntimeError` wrapper still surfaces them cleanly. Refactors the
  two near-identical detachment+Popen blocks in `launch_background`
  and `launch_background_resume` into a single `_spawn_detached`
  helper; constants are resolved via `getattr` so the module remains
  importable on POSIX hosts and tests can patch `sys.platform` to
  `"win32"` from Linux/macOS
  ([#200](https://github.com/microsoft/conductor/pull/200)).
- `conductor run --web-bg --log-file auto` now produces a log file
  with a real provider-side trace. `bg_runner.launch_background()` /
  `launch_background_resume()` already redirect the child's
  stdout/stderr/stdin to `subprocess.DEVNULL`, so silence is enforced
  at the OS level — but they also passed `--silent` to the child,
  which flipped `verbose_mode=False` and gated more than console
  prints (the Copilot provider's `_log_event_verbose()`,
  `_log_parse_recovery()`, and `_log_recovery_attempt()` all became
  no-ops, dropping events from the log file too). Both synthesized
  commands now omit `--silent`; console output still goes to DEVNULL
  via the Popen kwargs. Side benefit: the synthesized command is now
  reproducible by hand without learning that `--silent` was being
  injected behind the scenes
  ([#199](https://github.com/microsoft/conductor/pull/199),
  [#196](https://github.com/microsoft/conductor/issues/196)).
- `--web-bg` and other `--silent` invocations no longer leak the
  dashboard URL banner to stdout. Several `console.print` /
  `typer.echo` calls in `cli/run.py` were unconditionally writing the
  bg-launch URL, stderr log path, and `conductor stop` hint even with
  `--silent` / `is_verbose() == False`. Remaining unguarded URL prints
  are now gated behind `is_verbose()` so `--silent` is honored end to
  end ([#203](https://github.com/microsoft/conductor/pull/203),
  [#211](https://github.com/microsoft/conductor/pull/211)).
- `conductor validate <registry-workflow>` now succeeds for workflows
  that `conductor run` already executed successfully. The validator's
  `_resolve_subworkflow_ref_for_validation` was missing the step that
  the engine's `_resolve_subworkflow_path` already had: when a parent
  workflow lives inside a registry SHA cache and references a sibling
  via a relative path (e.g. `../document-review/workflow.yaml`), the
  engine auto-fetches the sibling from the same registry+SHA cache via
  `auto_fetch_relative_workflow`. The validator only checked the
  filesystem and reported "sub-workflow file not found". Validation and
  execution now agree on which refs are resolvable
  ([#197](https://github.com/microsoft/conductor/pull/197)).
- Registry cache now mirrors the source repository layout so
  repo-relative references between workflows in the same registry repo
  resolve correctly. Previously each workflow was isolated under
  `<base>/<registry>/<workflow_name>/<sha[:12]>/<filename>`, so
  `sdd-plan/plan.yaml` referencing `../document-review/workflow.yaml`
  resolved to a path that never existed in the cache and forced manual
  workarounds. The cache now stores workflows from the same
  registry+SHA under a shared per-SHA root
  (`<base>/<registry>/<sha[:12]>/<repo_path>`); metadata lives in a
  sibling `_meta/<sha[:12]>/` tree so it can never collide with real
  repo paths (e.g. a repo's own `.conductor/` directory). Per-workflow
  readiness sentinels are written **last** so readers never observe a
  partially populated workflow; per-file `os.replace()` stays
  intra-filesystem for atomic promotion; `_safe_repo_path()` rejects
  `..`, absolute paths, NUL bytes, and empty paths from any
  index/sibling entry; `_resolve_within()` adds defense-in-depth that
  resolved targets stay under the SHA root; `source.json` carries
  `cache_layout_version`, `registry_type`, `source`, and `full_sha` so
  cache hits require all four to match (stale metadata triggers
  re-fetch); the registry index is cached on disk so cache hits avoid
  a network round-trip. Sub-workflow refs from the same registry are
  auto-fetched when not yet present (gated to file-path-looking
  candidates with no `@`). `add_registry()` now rejects names
  containing `/`, `\`, the empty string, or the reserved `_adhoc` /
  `_meta` namespaces
  ([#194](https://github.com/microsoft/conductor/pull/194)).

## [0.1.16](https://github.com/microsoft/conductor/compare/v0.1.15...v0.1.16) - 2026-05-14

### Added
- `type: workflow` agents now accept registry references
  (`workflow[@registry][#ref]`) in the `workflow:` field, not just local file
  paths. Resolution prefers a local file when one exists relative to the
  parent workflow directory (preserves backward compatibility for
  extensionless local refs); otherwise the value is parsed as a registry
  reference, fetched via the registry cache, and executed from the cached
  location. `conductor validate` now recursively validates fetched
  sub-workflows with cycle detection (inode-based identity, so case-variant
  paths on macOS/Windows collapse correctly) and a depth cap of 10 — when
  the cap is hit a warning surfaces so users know validation was truncated
  rather than silently clean. Mutable registry refs (`name@registry#main`,
  or no `#ref`) may resolve to a different commit on `conductor resume` if
  the upstream branch has moved; pinned tags or commit SHAs guarantee
  deterministic resume
  ([#188](https://github.com/microsoft/conductor/pull/188)).
- Conductor now ships as a Claude Code plugin marketplace at the repo root.
  Users can install the conductor skill directly from `microsoft/conductor`
  with `/plugin marketplace add microsoft/conductor` followed by
  `/plugin install conductor@conductor`. The plugin ships markdown only
  (no `bin/`, hooks, MCP servers, or executables), keeping the trust
  surface minimal. The same `SKILL.md` remains usable via
  `gh skill install microsoft/conductor conductor` for Copilot CLI users.
  The previous `.claude/skills/conductor` location was removed — the
  plugin is now the single home for the skill; for local development on
  the skill itself, use `claude --plugin-dir plugins/conductor`
  ([#186](https://github.com/microsoft/conductor/pull/186)).

### Changed
- The bundled Conductor skill (`SKILL.md` + references) was refreshed to
  reflect the current CLI, schema, and feature set: `show` / `replay` /
  `--metadata` / `--workspace-instructions` quick-reference entries; new
  `type: workflow`, `dialog`, `retry`, `hooks`, `metadata`, `instructions`,
  `timeout_seconds`, and `openai-agents` provider concepts; corrected
  `update` behavior (default prints the install-script one-liner,
  `--apply` launches the installer); `CONDUCTOR_NO_UPDATE_CHECK`;
  registry `latest = branch HEAD` and `#ref` syntax; sub-workflow agents
  and dialog mode authoring guidance; script JSON-stdout auto-merge;
  `workflow.dir` / `workflow.file` template variables; and unknown-fields
  rejection in schema validation
  ([#187](https://github.com/microsoft/conductor/pull/187)).
- README "Why Conductor?" rewritten around three pillars — repeatable
  execution, deterministic routing, and version-controlled YAML
  workflows — and now leads with the real differentiator (zero-token
  orchestration) using concrete use-case examples
  ([#185](https://github.com/microsoft/conductor/pull/185)).

## [0.1.15](https://github.com/microsoft/conductor/compare/v0.1.14...v0.1.15) - 2026-05-13

### Added
- Per-agent `timeout_seconds` field for hard wall-clock timeouts on agent
  execution. Wraps execution in `asyncio.wait_for()` at the engine level so a
  slow agent no longer blocks the rest of the workflow. Effective timeout is
  `min(agent.timeout_seconds, remaining_workflow_timeout)` — when the workflow
  timeout is stricter it owns the error so attribution is never mislabeled.
  Raises a new `AgentTimeoutError` (subclass of `TimeoutError`) honored by
  existing `fail_fast` / `continue_on_error` semantics in parallel and
  for-each groups, and emits an `agent_timeout` event (with elapsed time
  and limit) for console + dashboard subscribers. Scoped to provider-backed
  agents; rejected on `script`, `human_gate`, and `workflow` types
  ([#150](https://github.com/microsoft/conductor/pull/150)).
- Auto-discovery of `.github/instructions/**/*.instructions.md` workspace
  conventions, matching GitHub Copilot's documented semantics. Files marked
  `applyTo: "**"` in their frontmatter are loaded into the workspace preamble
  alongside `AGENTS.md` / `CLAUDE.md` / `.github/copilot-instructions.md`;
  scoped (`applyTo: "<glob>"`) and absent-`applyTo` files are skipped per
  the convention's manual-attach default. The internal
  `CONVENTION_FILES: list[str]` table is refactored to a polymorphic
  `CONVENTIONS: list[Convention]` (`ConventionFile | ConventionDirectory`)
  so adding new conventions (Cursor rules, Cline rules, etc.) becomes one
  filter function plus one list entry; a `CONVENTION_FILES` module-level
  alias preserves backward compatibility for downstream imports
  ([#169](https://github.com/microsoft/conductor/pull/169)).

### Fixed
- `agent.system_prompt` is now rendered and forwarded to providers. The
  executor was rendering `agent.system_prompt` only to discard the result
  (`_ = self.renderer.render(...)`), so providers that forward
  `system_prompt` — notably the Copilot provider, which concatenates it
  into the prompt — received the un-rendered Jinja template. Agents whose
  instructions lived in `system_prompt` sent literal `{{ ... }}` placeholders
  to the model and got back "the prompt template contains unfilled variables"
  refusals. Also adds a `conductor validate` warning for agents that define
  `system_prompt` but no `prompt:` (a portability hazard since the Claude
  provider drops `system_prompt` entirely, and almost always a missing-`prompt:`
  typo) ([#179](https://github.com/microsoft/conductor/pull/179)).
- `conductor update` on Windows no longer attempts an in-process self-upgrade.
  The previous flow tried to re-install into the same venv the running
  `python.exe` lives in, producing "Access is denied" failures that earlier
  mitigations only papered over. `conductor update` now checks for a newer
  version and prints the OS-appropriate `install.ps1` / `install.sh`
  one-liner, and the install scripts become the single upgrade path: they
  detect other running conductor processes (auto-stopping under `-Yes`),
  sweep stale `*.exe.old` files, retry with backoff (2s / 5s / 10s), and —
  when uv can't remove the `conductor-cli` tool dir because of file locks —
  rename the whole dir aside and retry. `install.sh` reaches parity with
  `--yes` / `--force` / `--source` flags, retry-with-backoff, running-process
  detection, and a post-install `conductor --version` verify
  ([#171](https://github.com/microsoft/conductor/pull/171)).
- `install.ps1` is now stored without a UTF-8 BOM. The documented one-liner
  `irm https://aka.ms/conductor/install.ps1 | iex` returns the script body
  as a single string with the BOM surviving as U+FEFF at index 0; PowerShell's
  in-memory `iex` parser then trips on the `[CmdletBinding()]` attribute with
  `Unexpected attribute 'CmdletBinding'`. Both fresh installs via `irm | iex`
  and `conductor update --apply` (which re-runs the same command in a
  spawned console) now succeed. Direct `powershell.exe -File install.ps1`
  invocations were unaffected, which is why prior file-based integration
  tests didn't catch it ([#178](https://github.com/microsoft/conductor/pull/178)).
- `conductor stop` (including `--all` and `--port`) no longer crashes on
  Windows when a PID file exists in `~/.conductor/runs/`. The Unix idiom
  `os.kill(pid, 0)` for liveness probing is *not* a no-op on Windows — any
  signal other than `CTRL_C_EVENT` / `CTRL_BREAK_EVENT` routes through
  `TerminateProcess` and can raise `OSError` subclasses outside
  `ProcessLookupError` / `PermissionError` (e.g. `WinError 11 /
  ERROR_BAD_FORMAT`), and even "successful" calls would actually terminate
  the target with exit code 0. `_is_process_alive()` now dispatches to a
  Windows-specific implementation using `OpenProcess` +
  `GetExitCodeProcess` for a truly non-destructive liveness check
  ([#176](https://github.com/microsoft/conductor/pull/176)).

## [0.1.14](https://github.com/microsoft/conductor/compare/v0.1.13...v0.1.14) - 2026-05-06

### Fixed
- `conductor update` no longer reports its own launching shim as another
  running Conductor process. On Windows the `conductor.exe` shim is a
  separate process from the Python interpreter that runs the update
  command, so excluding only `os.getpid()` caused a false "1 other
  Conductor process is running" warning. The check now walks the full
  ancestor PID chain (via `wmic` on Windows, `ps` elsewhere) and excludes
  every process along the way, falling back to `{getpid(), getppid()}`
  if the parent map cannot be built.
  [#164](https://github.com/microsoft/conductor/pull/164)

## [0.1.13](https://github.com/microsoft/conductor/compare/v0.1.12...v0.1.13) - 2026-05-06

### Added
- `conductor resume` is now at flag parity with `conductor run`. New flags:
  `--provider` / `-p` (runtime provider override), `--metadata` / `-m` (CLI
  metadata merged on top of YAML metadata), `--web` (real-time dashboard for
  the resumed run), `--web-port`, and `--web-bg` (fork a detached resume +
  dashboard process). `--web` and `--web-bg` are mutually exclusive, matching
  `run`. The dashboard only shows events from the resumed agent forward —
  agent runs that completed before the checkpoint were emitted in the original
  process and are not replayed. `--input`, `--workspace-instructions`,
  `--instructions`, and `--dry-run` are intentionally not mirrored
  ([#158](https://github.com/microsoft/conductor/pull/158)).
- Reasoning effort (`low` / `medium` / `high` / `xhigh`) is now displayed in
  the web dashboard under each agent's metadata, right after `Model`. Effective
  value is per-agent `reasoning.effort` if set, otherwise
  `runtime.default_reasoning_effort`, otherwise omitted. Backed by a new
  `reasoning_effort` field on the `workflow_started` event payload, so older
  event log JSONL files replay gracefully (the row simply doesn't render)
  ([#160](https://github.com/microsoft/conductor/pull/160)).
- New `iteration_limit_reached` and `iteration_limit_resolved` events are
  emitted when a workflow hits its `max_iterations` cap. Previously the
  console showed an interactive `IntPrompt` while the web dashboard went
  silently dark; the dashboard now renders the prompt state and the chosen
  resolution. The `iteration_limit_reached` payload includes a `possible_loop`
  heuristic flag (set when the last 3 history entries are the same agent) so
  subscribers can call out stuck review loops
  ([#162](https://github.com/microsoft/conductor/pull/162)).

### Changed
- Workflow registry references now resolve `latest` (and bare `name@registry`
  refs) to the **default branch HEAD** instead of the newest git tag.
  Previously, the moment a registry repo got its first tag, bare references
  silently froze at that tag and stopped picking up commits to `main`. Tags
  remain first-class — pin explicitly via `workflow#v1.2.3` for releases. Also
  saves one GitHub API call on the hot path of bare-name fetches
  ([#157](https://github.com/microsoft/conductor/pull/157)).

### Fixed
- Schema validation now rejects unknown fields on `AgentDef`, `ParallelGroup`,
  `ForEachDef`, and `WorkflowConfig` instead of silently dropping them.
  Misnesting `parallel:` or `for_each:` inside an `agents:` item — or typos
  like `prmpt:` — used to fall through to a runtime
  `Model "gpt-4o" is not available` error three layers downstream. They now
  fail at parse time with a clear Pydantic error pointing at the offending
  location. `conductor validate` also gained "Parallel Groups" and "For-each
  Groups" rows in its summary table so missing groups are immediately visible
  ([#159](https://github.com/microsoft/conductor/pull/159)).
- Tool arguments and results are now pretty-printed in dashboard / JSONL /
  verbose-console events. Copilot tool results no longer leak the full
  `Result(content=..., contents=None, detailed_content=..., kind=None)` repr
  with literal `\\n` escapes and doubled `\\\\` Windows paths, and tool
  arguments render as JSON (`{"k": "v"}`) instead of Python dict repr
  (`{'k': 'v'}`). Both providers share a new
  `src/conductor/providers/_event_format.py` helper for parity
  ([#161](https://github.com/microsoft/conductor/pull/161)).
- `install.ps1` on Windows now captures full `uv tool install` stdout AND
  stderr via `Start-Process -RedirectStandardOutput -RedirectStandardError`
  to temp files. Previously, with `$ErrorActionPreference = 'Stop'`,
  PowerShell treated uv's stderr as a terminating error and threw before
  the assignment completed, so install failures showed `(no output captured)`
  with no way to diagnose them
  ([#156](https://github.com/microsoft/conductor/pull/156)).

## [0.1.12](https://github.com/microsoft/conductor/compare/v0.1.11...v0.1.12) - 2026-05-05

### Added
- Unified `reasoning.effort` configuration for per-agent and workflow-wide
  control of model reasoning / extended-thinking effort. Set
  `runtime.default_reasoning_effort` (`low` | `medium` | `high` | `xhigh`) for a
  workflow-wide default, or override per agent with a `reasoning.effort` block.
  Translates to `reasoning_effort` on the Copilot session and to extended
  `thinking` budget on Claude (low=2048, medium=8192, high=16384, xhigh=32768
  tokens, with `temperature` coerced to 1.0 and `max_tokens` bumped to fit).
  Validates against each model's supported efforts/capabilities and surfaces
  thinking content via `agent_reasoning` events. See
  [`examples/reasoning-effort.yaml`](examples/reasoning-effort.yaml)
  ([#152](https://github.com/microsoft/conductor/pull/152)).
- Tag-based versioning for the workflow registry. Versions are now
  auto-discovered from git tags instead of being explicitly listed in
  `registry.yaml`, and refs accept any tag, branch, or SHA via the new
  `workflow#ref` syntax (e.g. `sdd/plan#v3.0.0`, `sdd/plan#main`,
  `sdd/plan#abc1234`). Stale CDN content is bypassed via cache-busting
  query parameters so registry updates are visible immediately
  ([#151](https://github.com/microsoft/conductor/pull/151)).

### Fixed
- `conductor update` reliability on Windows. Adds a pre-flight check for
  other running Conductor processes (which hold file locks on
  `%LOCALAPPDATA%\uv\tools\conductor-cli\` and cause `uv tool install
  --force` to fail with "Access is denied"), retries the install up to 3
  times to absorb transient Windows Defender failures, surfaces full uv
  stdout AND stderr on failure with Defender-exclusion guidance, broadens
  the Windows entrypoint rename to cover the uv tool venv `Scripts/`
  directory in `%LOCALAPPDATA%` and `%APPDATA%`, and adds a new
  `conductor update --force` flag to skip the pre-flight check
  ([#155](https://github.com/microsoft/conductor/pull/155)).
- Dashboard layout for workflows with `human_gate` options or multiple
  loop-back routes (e.g. revision loops). The `workflow_started` event now
  emits routes from `human_gate` `options[].route` so gate edges aren't
  silently dropped, and the frontend pre-classifies back-edges via DFS from
  `$start` and feeds them to Dagre in reversed direction so cycles no
  longer scramble rank assignment. Workflows like `sdd/plan-v3.yaml` now
  render as a coherent top-to-bottom DAG instead of disconnected columns
  with long diagonal edges
  ([#153](https://github.com/microsoft/conductor/pull/153)).
- Windows install failures now surface useful diagnostics. `install.ps1`
  prints captured `uv` stdout/stderr on failure instead of swallowing it,
  and uses the correct Microsoft Defender cmdlet so the install path is
  exclusion-friendly ([#149](https://github.com/microsoft/conductor/pull/149)).

## [0.1.11](https://github.com/microsoft/conductor/compare/v0.1.10...v0.1.11) - 2026-05-04

### Added
- `metadata` dict on workflow definitions, settable statically in YAML or
  dynamically via `--metadata` / `-m` CLI flags. Merged metadata is
  included in the `workflow_started` event for downstream consumers
  ([#107](https://github.com/microsoft/conductor/pull/107)).
- `input_mapping` field on `type: workflow` agents, enabling Jinja2-templated
  per-call inputs to sub-workflows evaluated against the parent context.
  When omitted, the parent's `workflow.input.*` is forwarded as before
  ([#109](https://github.com/microsoft/conductor/pull/109)).
- `type: workflow` agents are now allowed inside `for_each` groups, enabling
  dynamic fan-out to sub-workflows with per-iteration `input_mapping`. Each
  iteration emits its own `subworkflow_started` / `subworkflow_completed`
  events ([#110](https://github.com/microsoft/conductor/pull/110)).
- Self-referential sub-workflows are now allowed; depth is bounded by the
  global `MAX_SUBWORKFLOW_DEPTH` plus an optional per-agent `max_depth`
  field on `AgentDef` ([#111](https://github.com/microsoft/conductor/pull/111)).
- `workflow.dir`, `workflow.file`, and `workflow.name` template variables are
  now available in all agent contexts (regardless of context mode). Lets
  registry-hosted workflows reference co-located scripts and assets without
  depending on the caller's working directory
  ([#121](https://github.com/microsoft/conductor/pull/121)).
- Script agent stdout that is valid JSON is auto-parsed and merged into
  the agent's output dict alongside `stdout`, `stderr`, and `exit_code`,
  enabling field-based `when:` route conditions instead of opaque exit-code
  matching ([#122](https://github.com/microsoft/conductor/pull/122)).
- `conductor validate` now performs semantic validation in addition to
  YAML schema checks, catching stale agent references, missing workflow
  inputs, and undeclared explicit-mode dependencies before runtime in
  `prompt`, `system_prompt`, `command`, `args`, `working_dir`,
  `input_mapping`, parallel-group inputs, and workflow `output:`
  templates ([#125](https://github.com/microsoft/conductor/pull/125)).
- Web dashboard: breadcrumb navigation, double-click dive-in to
  sub-workflow graphs, isolated subworkflow contexts (no node-status
  bleed across repeated runs), and reliable Stop button during
  subworkflows ([#113](https://github.com/microsoft/conductor/pull/113),
  follow-up fixes in [#146](https://github.com/microsoft/conductor/pull/146)).
- Dialog mode for agents: multi-turn conversational interactions
  driven by a `dialog` gate with conditional transitions, full
  Copilot and Claude provider support, and dedicated dashboard UI
  (`DialogDetail`, `DialogEngagementPrompt`, `DialogOverlay`)
  ([#130](https://github.com/microsoft/conductor/pull/130)).
- Markdown rendering and auto-linkification in human gate prompts.
  Gate prompts render through Rich Markdown in the terminal and as
  GitHub-Flavored Markdown in the dashboard. Bare file paths and URLs
  in gate prompts are converted to clickable links; relative paths
  open a sandboxed `FileViewer` modal served via a path-traversal-safe
  `GET /api/files/{path}` endpoint
  ([#131](https://github.com/microsoft/conductor/pull/131)).
- Workspace instructions support: `--workspace-instructions` and
  `--instructions` CLI flags plus a YAML-level `instructions:` field on
  the workflow. Auto-discovers `AGENTS.md`, `CLAUDE.md`, and
  `.github/copilot-instructions.md` by walking from CWD to the git root,
  prepends them to every agent's prompt, inherits into sub-workflows,
  and persists in checkpoints
  ([#141](https://github.com/microsoft/conductor/pull/141)).

### Changed
- The dashboard's "context window remaining" bar now sources
  `context_window_max` from each provider's SDK at runtime instead of a
  hand-maintained static table. Values now reflect the actual cap the SDK
  enforces (e.g. `claude-opus-4.6` reports 200K rather than the theoretical
  1M; `gpt-5.x` reports 128K rather than 400K). The `context_window` field
  on `ModelPricing` has been removed; pricing data continues to be
  hand-maintained for cost calculation only
  ([#144](https://github.com/microsoft/conductor/pull/144)).

### Fixed
- Pass `streaming=True` to the Copilot SDK's `create_session` to prevent
  silent truncation of large tool-call arguments. In non-streaming mode
  the model's per-turn output budget is exhausted mid-JSON for large
  arguments (e.g., `create` with multi-KB `file_text`), the CLI executes
  the partial tool call, and the agent loops on the broken call until
  the wall-clock session limit fires ([#129](https://github.com/microsoft/conductor/pull/129)).
- Build the Copilot prompt schema recursively from nested `output:`
  definitions instead of flattening to top-level fields only. Nested object
  properties, required keys, and array item schemas are now included in the
  prompt-facing schema used for initial guidance and parse recovery
  ([#100](https://github.com/microsoft/conductor/pull/100)).
- Coerce Python literal `"True"` / `"False"` / `"None"` strings produced by
  Jinja's default `str(bool)` rendering into native Python types when
  building workflow output. Previously, `output: { matched: "{{ a == b }}" }`
  produced the string `"False"` (truthy), causing downstream `when:`
  comparisons against `false` to silently misbehave
  ([#139](https://github.com/microsoft/conductor/pull/139)).
- Pricing fuzzy match no longer silently inherits values across model
  families. Names sharing a textual prefix with a known key (e.g.
  `claude-opus-4.7` previously matched `claude-opus-4`) now require a `-`
  delimiter; non-matching names return `None` and the dashboard hides the
  cost field. A one-time warning is emitted per requested name on any
  non-exact match ([#143](https://github.com/microsoft/conductor/pull/143)).
- Run `uv tool update-shell` after `uv tool install` in both `install.ps1`
  and `install.sh` so `conductor` is available on PATH in new shells, CI
  agents, and IDE extensions after a fresh install
  ([#142](https://github.com/microsoft/conductor/pull/142)).
- In explicit context mode, `workflow.input` is now always available to
  `script` and `type: workflow` agent templates regardless of the agent's
  declared `input:` list. The explicit-mode contract still applies to LLM
  agents (no undeclared inputs in prompts to control token cost)
  ([#119](https://github.com/microsoft/conductor/pull/119)).
- Optional workflow inputs without an explicit `default:` now resolve to
  type-appropriate zero values (`""`, `0`, `false`, `[]`, `{}`) instead of
  Python `None`, so templates like
  `{{ workflow.input.optional | default("fallback") }}` render the fallback
  rather than the literal string `"None"`
  ([#123](https://github.com/microsoft/conductor/pull/123)).
- Web dashboard: events without an engine-supplied `subworkflow_path`
  stamp (e.g., `for_each_item_started` for a parent for_each over
  `type: workflow` agents) now route strictly to the root context
  instead of falling back to the user's currently-viewed path. This
  fixes two related symptoms: dashboards opened during a run with
  sub-workflows no longer auto-land inside an iteration, and a parent
  for_each panel now displays every iteration rather than silently
  dropping the middle ones into a sibling sub-workflow's context
  ([#148](https://github.com/microsoft/conductor/pull/148)).

## [0.1.10](https://github.com/microsoft/conductor/compare/v0.1.9...v0.1.10) - 2026-04-30

### Added
- Sub-workflow composition support: `workflow`-type agents can now be used
  inside `for_each` groups, with dynamic per-iteration `input_mapping`
  ([#101](https://github.com/microsoft/conductor/pull/101), [#102](https://github.com/microsoft/conductor/pull/102)).

### Changed
- Bumped `github-copilot-sdk` to `>=0.3.0`. The SDK ships a bundled `copilot`
  CLI binary used for JSON-RPC `session.create` calls; `0.2.2` bundled CLI
  `1.0.21`, which rejected newer model IDs locally with
  `JSON-RPC -32603: Model "<id>" is not available`. `0.3.0` bundles CLI
  `1.0.36-0`, which accepts the current Copilot model catalog (including
  `claude-opus-4.7*` variants).

### Fixed
- Suppressed noisy PowerShell stderr output from `uv tool install` during
  Windows self-update ([#99](https://github.com/microsoft/conductor/pull/99)).
