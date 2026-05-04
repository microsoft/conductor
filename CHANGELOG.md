# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.10] - 2026-04-30

### Added
- Sub-workflow composition support: `workflow`-type agents can now be used
  inside `for_each` groups, with dynamic per-iteration `input_mapping`
  ([#101], [#102]).

### Changed
- Bumped `github-copilot-sdk` to `>=0.3.0`. The SDK ships a bundled `copilot`
  CLI binary used for JSON-RPC `session.create` calls; `0.2.2` bundled CLI
  `1.0.21`, which rejected newer model IDs locally with
  `JSON-RPC -32603: Model "<id>" is not available`. `0.3.0` bundles CLI
  `1.0.36-0`, which accepts the current Copilot model catalog (including
  `claude-opus-4.7*` variants).

### Fixed
- Suppressed noisy PowerShell stderr output from `uv tool install` during
  Windows self-update ([#99]).

[#99]: https://github.com/microsoft/conductor/pull/99
[#101]: https://github.com/microsoft/conductor/pull/101
[#102]: https://github.com/microsoft/conductor/pull/102
[0.1.10]: https://github.com/microsoft/conductor/compare/v0.1.9...v0.1.10
