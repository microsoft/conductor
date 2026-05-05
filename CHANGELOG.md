# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased](https://github.com/microsoft/conductor/compare/v0.1.11...HEAD)

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
