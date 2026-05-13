# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased](https://github.com/microsoft/conductor/compare/v0.1.15...HEAD)

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
