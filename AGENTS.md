# AGENTS.md

Conductor is a Python 3.12+ CLI that runs multi-agent workflows defined in YAML
(parallel and for-each groups, conditional routing, loop-backs, human gates)
on several LLM providers. This file is the always-loaded entry point for
agents working **on** Conductor. It holds the rules that apply to every change
and routes you to the guide that owns your task.

To **author or run** a workflow, use the bundled Conductor skill
(`plugins/conductor/skills/conductor/SKILL.md`) and the user docs in `docs/`.

## Before you change anything

- Read every guide the [task router](#task-router) lists for the paths you
  will touch. They hold the invariants, change checklists and regression
  tests for that area; this file does not repeat them.
- If a required guide cannot be read (no file access, missing file), say so
  and ask for its content. Do not guess a contract.

## Universal rules

- **Enforce at run time, not only in `conductor validate`.** `conductor run`
  never calls the static validator (`src/conductor/config/validator.py`), so a
  new restriction must also be checked on the execution path.
- **Capabilities are contracts.** A provider's `ProviderCapabilities` must
  describe what it actually does; if a claim cannot always be honored, declare
  the weaker value. A behavior or contract change to one provider applies to
  every provider where it is meaningful.
- **Keep `run` and `resume` in parity.** A flag added to `run` is added to
  `resume` unless it cannot apply to a resumed run, and both paths wire the
  same emitters, logs and dashboard lifecycle.
- **Never silently widen access.** Tool allowlists, MCP servers, credentials,
  settings tiers and filesystem grants are honored exactly; refuse a
  configuration you cannot honor rather than granting more than was declared.
- **Surface failures.** Do not swallow exceptions or report a problem the user
  must act on only at debug level. Optional diagnostics (records, logs, labels)
  must never abort a run, but their failure must be visible.
- **Rich output is markup-safe.** Never interpolate a runtime value into a
  string Rich parses as markup. Build consoles with
  `conductor.console.make_console()`, style with `styled()`, and pass `Text`
  to panel titles and prompts. Rules:
  [console output](docs/development/console-output.md).
- **Tests are hermetic.** Never touch the developer's real home directory,
  temp directory, running workflows, network or CLI credentials. The autouse
  fixtures in `tests/conftest.py` isolate these; `real_api` and
  `install_scripts` tests run only when selected with `-m`.
- **Style.** Ruff (line length 100), Google-style docstrings, type hints
  checked by `ty`, Pydantic v2, async/await for provider operations. Comment
  only to explain why.

## Commands

```bash
make dev                                   # install with dev dependencies (uv)
uv run pytest tests/test_engine/test_workflow.py -k parallel   # targeted tests
make test           # parallel suite (4 workers); excludes real_api, install_scripts,
                    # performance and docker_integration tests
PYTEST_WORKERS=0 make test                 # serial, for debugging
make check          # ruff lint + format check, ty type check
make format         # auto-fix and format
make validate-examples                     # validate every examples/*.yaml
make build-frontend && make test-frontend  # dashboard; static/ is generated
uv run conductor validate examples/simple-qa.yaml
```

Run the smallest test selection that covers your change, then widen if it
fails. Windows equivalents and other targets:
[development index](docs/development/README.md).

## Changelog and releases

- Every PR with a user-facing change adds exactly one towncrier fragment in
  `changelog.d/` (`<issue>.<category>.md` or `+<slug>.<category>.md`; category
  `added`, `fixed`, `changed` or `removed`). Contract:
  [`changelog.d/README.md`](changelog.d/README.md).
- Never edit `CHANGELOG.md` in a feature or fix PR; it is compiled at release.
  Only a maintainer's `changelog-not-required` label exempts a PR. CI enforces
  both.
- The version lives only in `pyproject.toml`. Releases are tag-triggered; see
  [`docs/release-checklist.md`](docs/release-checklist.md).
- Commits use Conventional Commits (`fix(scope): ...`, `feat: ...`).

## Task router

All guides are in [`docs/development/`](docs/development/README.md). Read the
rows that match the files or behavior you are changing; a cross-cutting change
reads every matching row.

| You are changing | Read |
| --- | --- |
| Package layout, a new module, imports between layers | [architecture](docs/development/architecture.md) |
| Any test, fixture or test marker | [testing](docs/development/testing.md) + the subject guide |
| Console, table, panel or prompt output | [console output](docs/development/console-output.md) |
| `config/`, `engine/`, `executor/`, `gates/`, `interrupt/`: schema, routing, steps, checkpoints, validators, guidance | [workflow engine](docs/development/workflow-engine.md) |
| `providers/`, capabilities, usage, pricing, billing, compaction, telemetry | [providers](docs/development/providers.md) |
| `providers/copilot.py` | [providers](docs/development/providers.md) + [Copilot](docs/development/providers/copilot.md) |
| `providers/claude_agent_sdk.py` | [providers](docs/development/providers.md) + [Claude Agent SDK](docs/development/providers/claude-agent-sdk.md) |
| `cli/`, run records, background launch, `stop`/`status`, install and update scripts, retention | [CLI and runs](docs/development/cli-and-runs.md) |
| `fleet/tui/` and its pilot tests | [Fleet TUI](docs/development/fleet-tui.md) |
| `web/` server, auth, replay, dashboard frontend | [dashboard](docs/development/dashboard.md) |
| `mcp/serve/` (`conductor mcp serve`) | [MCP server](docs/development/mcp-server.md) |
| `skills/`, `plugins/`, plugin sources and discovery | [plugins and skills](docs/development/plugins-and-skills.md) |
| `execution/`, `bundle/`, `runner/`, environments, secrets, redaction | [execution backends](docs/development/execution-backends.md) |
| Agent instructions, docs layout | [development index](docs/development/README.md) |

## Maintaining these instructions

- Keep this file small: it is loaded into every agent session and every
  `conductor run --workspace-instructions` prompt. `tests/test_agent_instructions.py`
  caps it at 12 KiB and 180 lines.
- Put a new rule in the guide that owns the code, next to its regression test.
  Add it here only if it applies to nearly every change.
- Do not add `@path` imports (they load eagerly), nested `AGENTS.md` or
  `CLAUDE.md` files, or any other file with those names in any letter case.
