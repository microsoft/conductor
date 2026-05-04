# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased](https://github.com/microsoft/conductor/compare/v0.1.10...HEAD)

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
