# Contributor development guides

This directory holds Conductor's on-demand contributor guides: the rules, invariants and change
checklists for each subsystem. This file is the entry point: setup and commands, changelog and
release rules, how coding agents load instructions, and how to keep these guides healthy.

## When to read

- You are setting up a development environment or running the test, lint or type-check gates.
- You are about to change code and need the guide that owns that area (see the index below).
- You are adding a changelog fragment or preparing a release.
- You are editing the root `AGENTS.md` or `CLAUDE.md`, or any file under `docs/development/`.

### Where documentation lives

- Root `AGENTS.md` is the always-loaded router: project summary, essential commands, links
  here. `CLAUDE.md` exists only so Claude Code loads the same router.
- `docs/development/` holds the contributor guides. Nothing loads them automatically; read the
  guide for the area you are changing.
- Top-level user docs under docs/ (for example [workflow syntax](../workflow-syntax.md) and
  [CLI reference](../cli-reference.md)) is user documentation, canonical for user-visible
  behavior. Guides link to it rather than restating it.
- `docs/projects/` and `docs/design/` are historical design records. When they disagree with
  the code, the code wins.

### Guide index

| Guide | Read when |
| --- | --- |
| [README.md](README.md) | Setting up, running gates, changelog, releasing, instruction loading |
| [architecture.md](architecture.md) | Adding a module or package, crossing layers, adding an optional dependency |
| [testing.md](testing.md) | Writing or changing any test, markers, `tests/conftest.py` |
| [console-output.md](console-output.md) | Anything printed through Rich or Textual, Typer help text |
| [workflow-engine.md](workflow-engine.md) | Schema, loader, validator, engine loop, executors, routing, checkpoints, gates |
| [providers.md](providers.md) | Any provider, capability declarations, provider parity |
| [providers/copilot.md](providers/copilot.md) | The Copilot provider in depth |
| [providers/claude-agent-sdk.md](providers/claude-agent-sdk.md) | The claude-agent-sdk provider in depth |
| [cli-and-runs.md](cli-and-runs.md) | CLI commands, run/resume parity, `--web-bg`, run records, stop, update, registries |
| [fleet-tui.md](fleet-tui.md) | `conductor fleet`, history, retention, the Textual TUI |
| [dashboard.md](dashboard.md) | Web dashboard server, auth, React frontend, replay |
| [mcp-server.md](mcp-server.md) | `conductor mcp serve` |
| [plugins-and-skills.md](plugins-and-skills.md) | Skills, plugins, plugin sources, the bundled skill |
| [execution-backends.md](execution-backends.md) | Script-step backends, Docker, environments, bundles, secrets |

## Required invariants

### Setup and commands

Prerequisites: Python 3.12+ and [uv](https://github.com/astral-sh/uv). Commands below are the
ones in `Makefile`; run them from the repository root.

```bash
make dev                    # uv sync --group dev (pytest, xdist, ruff, ty, textual)
make install                # uv sync (runtime dependencies only)
make test                   # isolated suite, 4 xdist workers (see below)
PYTEST_WORKERS=0 make test  # same selection, run serially (debugging)
uv run pytest tests/test_engine/test_limits.py                            # one file
uv run pytest tests/test_engine/test_limits.py::TestWorkflowEngineLimits  # one class/test
uv run pytest -k "parallel"                                               # by name
make test-cov               # make test plus --cov=conductor --cov-report=term-missing
make lint                   # ruff check + ruff format --check on src and tests
make format                 # ruff format, then ruff check --fix
make typecheck              # uv run ty check src
make check                  # lint + typecheck
make validate-examples      # conductor validate on every examples/*.yaml
make build-frontend         # npm install + build into src/conductor/web/static/
make test-frontend          # dashboard Vitest suite
make test-install-scripts   # opt-in install.sh/install.ps1 end-to-end tests (slow, serial)
```

- `make test` runs `uv run pytest -n $(PYTEST_WORKERS) --dist loadfile -m "not real_api and not
  install_scripts and not performance and not docker_integration"`. `PYTEST_WORKERS` defaults
  to `4`; `0` disables xdist. `--dist loadfile` keeps all tests of one file on one worker.
- A bare `uv run pytest` applies no `-m` filter. `real_api` and `install_scripts` tests still
  auto-skip (hook in `tests/conftest.py`), `docker_integration` tests skip when no Docker daemon
  answers, but `performance` tests run. Use `make test` or pass `-m` for the full suite; target
  paths with `path::Class::test` for quick loops. See [testing.md](testing.md) for markers.
- Windows has no `make`; use uv directly:

```powershell
uv sync --group dev   # make dev; `uv sync --all-extras` also installs every optional extra
uv run pytest -n 4 --dist loadfile -m "not real_api and not install_scripts and not performance and not docker_integration"
uv run pytest -n 0 -m "not real_api and not install_scripts and not performance and not docker_integration"
uv run ruff check src tests; uv run ruff format --check src tests
uv run ty check src
```

The Windows Copilot CLI path caveat is in the [README](../../README.md#windows).

### Code style

- Python 3.12+ (`requires-python = ">=3.12"`; CI tests 3.12 and 3.13).
- Ruff for lint and format, line length 100 (`[tool.ruff]` in `pyproject.toml`).
- Type hints are required and checked with ty (`make typecheck`).
- Google-style docstrings; Pydantic v2 for data validation; async/await for all provider
  operations.
- NEVER interpolate a runtime value into a string Rich parses as markup. Follow
  [console-output.md](console-output.md); `tests/test_cli/test_markup_guards.py` enforces it.

### Changelog

[`changelog.d/README.md`](../../changelog.d/README.md) is the canonical contract (towncrier).
The five rules every contributor and agent follows:

1. Every PR with a user-facing change MUST add exactly one fragment under `changelog.d/`.
2. NEVER edit `CHANGELOG.md` in a feature or fix PR; towncrier compiles it at release time.
3. Naming: `<issue>.<category>.md` when the issue is known (towncrier appends `(#issue)`),
   otherwise `+<slug>.<category>.md`. Category is one of `added`, `fixed`, `changed`,
   `removed`. NEVER rename a fragment after opening the PR.
4. The `Changelog` workflow (`.github/workflows/changelog.yml`) validates fragment presence and
   syntax on every PR.
5. Exemptions need the maintainer-applied `changelog-not-required` label, which waives both the
   fragment requirement and the `CHANGELOG.md` prohibition. Dependabot PRs waive only the
   fragment requirement. Any fragment that is present is still validated.

### Releasing

Follow [the release checklist](../release-checklist.md). In short: a release-prep PR
(`chore(release): cut X.Y.Z`) bumps `version` in `pyproject.toml`, runs
`make changelog-build VERSION=X.Y.Z`, and re-locks with `uv lock`; after it merges, tag the
merge commit on `main` as `vX.Y.Z` and push the tag. `.github/workflows/release.yml` then lints,
type-checks, tests on Python 3.12 and 3.13, builds, and publishes the GitHub Release with notes
extracted from `CHANGELOG.md`. The version lives only in `pyproject.toml` (read at runtime via
`importlib.metadata`); there is no `__version__` to edit. Default bump is the patch number.

### How agents load instructions

| Client | Loads | Notes |
| --- | --- | --- |
| Copilot CLI | Root `AGENTS.md` and `CLAUDE.md` | File references written with the at-sign are expanded eagerly |
| Claude Code | Root `CLAUDE.md` | `CLAUDE.md` imports `AGENTS.md`, so the router loads too |
| Codex | Root `AGENTS.md` | Truncated at `project_doc_max_bytes` (32 KiB by default) |
| Conductor `--workspace-instructions` | `AGENTS.md`, `.github/copilot-instructions.md`, `CLAUDE.md`, plus `.github/instructions/**/*.instructions.md` filtered by `applyTo` | Walks from the cwd up to the git root, closest file wins per convention; reads raw text and does not expand imports or follow links |

- Conductor implements discovery in `src/conductor/config/instructions.py` and prepends the
  assembled preamble to every agent prompt; `--print-loaded-instructions` shows what was
  found. Checkpoints persist the preamble and `conductor resume` restores it verbatim
  (`src/conductor/engine/checkpoint.py`).
- No client loads `docs/development/` on its own. A workflow that should follow a guide must
  include it explicitly, either with `--instructions docs/development/providers.md` on the CLI
  (path relative to the cwd, repeatable, a missing file is an error) or in YAML:

```yaml
workflow:
  instructions:
    - !file ../docs/development/providers.md
```

- A `!file` path resolves against the directory of the file containing the tag (here the
  workflow file) and is inserted verbatim (`src/conductor/config/loader.py`). The loader then
  applies environment-variable substitution to that content
  (`src/conductor/config/loader.py::_resolve_env_vars_recursive`), so a dollar-brace placeholder
  in an included guide is substituted, and one naming an unset variable without a default fails
  the load. `--instructions` files are read raw. See
  [workspace instructions](../cli-reference.md#workspace-instructions).

### Maintaining the instructions

- Root `AGENTS.md` is a router. tests/test_agent_instructions.py caps it at 12 KiB and 180
  lines (target 6-8 KiB), keeps `CLAUDE.md` a bridge that only imports it, and caps the
  preamble `--workspace-instructions` assembles from the repository's default files at 16 KiB
  (checked from several start directories). It also checks guide links, anchors and
  backticked paths.
- Put a new behavior rule in the guide that owns the area, not in the root. The root may gain
  at most a link.
- Keep guides rules-first and in the shared template: scope, When to read, Required
  invariants, Change checklist, Rationale and evidence, Related references.
- Cite code as full repo-relative paths in backticks (optionally with `::symbol`); every cited
  path must exist. Link docs with relative Markdown links.
- NEVER add at-sign import directives to a guide or to `AGENTS.md`; `CLAUDE.md`'s single import
  of `AGENTS.md` is the only one.
- NEVER name a file `CLAUDE.md`, `AGENTS.md`, `GEMINI.md` (or the `.local`/`.override`
  variants), in any case, outside the repository root. Agent clients and Conductor's own
  discovery load such files automatically for anyone working below that directory. The only
  allowlisted exception is the tiny inert pointer `docs/providers/claude.md`, kept for old
  links; it must stay under 512 bytes with no imports.
- Do not write a dollar-brace sequence in a guide (see the `!file` note above).
- When user-visible behavior changes, update the user doc and link to it; do not copy it.

## Change checklist

- [ ] Run `make lint`, `make typecheck` and the relevant tests (or `make test`) before pushing.
- [ ] Add a changelog fragment for any user-facing change.
- [ ] Update the owning guide when you add or change an invariant, and the user doc when
      behavior visible to users changes.
- [ ] Run `make validate-examples` after changing the schema or anything under `examples/`.
- [ ] After editing `src/conductor/web/frontend/`, run `make build-frontend` and commit the
      regenerated `src/conductor/web/static/`.
- [ ] When adding a Makefile target or marker, update the commands section here and
      [testing.md](testing.md).
- [ ] When adding a guide, add it to the index above and keep the root router's links current.

## Rationale and evidence

- Root router: the previous root `AGENTS.md` had grown to about 238 KB (roughly 62k tokens),
  paid by every agent session and silently truncated by clients with byte limits (#574). The
  size caps keep it from growing back; detail lives in guides read on demand.
- Commands: `real_api` tests spawn real Copilot/Claude processes (#326) and `install_scripts`
  tests mutate host-wide install state, so both are opt-in by default, and `make test`/CI also
  deselect them explicitly as defense in depth.
- Changelog fragments exist so concurrent PRs never conflict on `CHANGELOG.md`.

## Related references

- [Changelog fragment contract](../../changelog.d/README.md)
- [Release checklist](../release-checklist.md)
- [Project README, Development section](../../README.md#development)
- [CLI reference](../cli-reference.md)
- [Workflow syntax](../workflow-syntax.md)
- [Architecture](architecture.md), [testing](testing.md), [console output](console-output.md)
