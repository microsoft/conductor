# Workflow Registry

Status: **Proposed**
Supersedes: PR #88 (workflow template registry)

## Summary

Introduce a configurable, named **registry** system for distributing and
running shared workflows. A registry is either a GitHub repository or a local
directory, configured once and referenced by short name. Workflows can be run
directly from a registry by reference, with explicit versioning and local
caching. Replaces the existing local templates / `init` scaffolding feature
entirely.

## Motivation

Today, Conductor ships a small set of bundled YAML templates (`simple`,
`loop`, `human-gate`) accessed via `conductor init` and `conductor templates`.
PR #88 attempted to extend this with a hardcoded "remote template registry"
pointing at `microsoft/conductor-workflows`. That approach has several
problems:

- Mixes two concerns — initial scaffolding for new users vs. distribution of
  reusable workflows — into one feature.
- Hardcodes a single remote source.
- No versioning, no caching, fetched on every invocation.
- Uses naive string substitution instead of the existing Jinja2 path,
  inconsistent with how local workflows are loaded.
- The bundled templates feature itself is small and rarely the right
  starting point for real workflows; teams want to share their own workflows.

A proper registry mechanism replaces both, gives teams a way to share
workflows across repos and machines, and removes a maintenance burden.

## Goals

- Run a workflow from a remote source by short name and explicit version.
- Configure multiple named registries (org-wide official, team-internal,
  personal local dir) and switch between them with no command-line ceremony.
- Reproducible runs: a versioned reference resolves to the same workflow
  bytes every time.
- Preserve all existing local-file behavior — bare paths still work
  unchanged.
- Keep the registry mechanism out of the engine: it is a CLI/loader concern.

## Non-goals (v1)

- SemVer ranges (`^1.0`, `~1.2`). Exact-version matching only.
- Authenticated fetches for private GitHub repos. Public repos only in v1.
- Server-side validation, signing, or attestation of registry contents.
- A `conductor publish` command. Publishing is just `git push` + tag.
- A central, blessed registry maintained by the Conductor project. Users
  configure their own.

## User experience

### Registry configuration

Stored at `~/.conductor/registries.toml`:

```toml
default = "official"

[registries.official]
type = "github"
source = "myorg/conductor-workflows"

[registries.team]
type = "github"
source = "myorg/team-workflows"

[registries.local]
type = "path"
source = "/Users/jason/workflows"
```

Managed via:

```
conductor registry add <name> <source> [--type github|path] [--default]
conductor registry remove <name>
conductor registry set-default <name>
conductor registry list                  # list configured registries
conductor registry list <name>           # list workflows in a registry
conductor registry update [<name>]       # refresh index + re-resolve `latest`
conductor registry show <ref>            # show metadata + cached path
```

The `type` flag is optional; `add` infers `github` if `<source>` matches
`owner/repo`, otherwise `path`.

### Reference syntax

```
<workflow>[@<registry>][@<version>]
```

Resolution rules, in order:

1. If the argument exists as a file on disk, treat it as a local path.
2. Otherwise parse as a registry reference.
3. Missing `@<registry>` → use the configured default registry.
4. Missing `@<version>` → use `latest` (highest version listed in the
   registry index).

Examples:

```
conductor run ./my-workflow.yaml          # local file (unchanged)
conductor run qa-bot                      # latest from default registry
conductor run qa-bot@team                 # latest from `team` registry
conductor run qa-bot@team@1.2.3           # exact version from `team`
conductor run qa-bot@@1.2.3               # exact version from default registry
```

### Registry index

Each registry root must contain `index.yaml` (preferred) or `index.json`:

```yaml
workflows:
  qa-bot:
    description: "Simple Q&A workflow"
    path: workflows/qa-bot.yaml          # path relative to registry root
    versions: ["1.0.0", "1.1.0", "2.0.0"]
  code-review:
    description: "Multi-agent code review"
    path: workflows/code-review.yaml
    versions: ["0.3.0"]
```

For GitHub registries, versions correspond to git tags on the registry repo.
For local registries, the maintainer maintains the version list directly.

The index is the single source of truth for what workflows exist and what
versions are available. Conductor does not auto-discover YAML files in a
registry — the maintainer curates the index.

### Caching

Fetched workflows are cached at:

```
~/.conductor/cache/registries/<registry>/<workflow>/<version>/
```

- Cache is keyed by `(registry, workflow, version)`.
- Explicit versions are immutable: once cached, never re-fetched.
- `latest` is re-resolved on `conductor registry update`. Each resolved
  version is cached in its own directory.
- Index files are cached separately at
  `~/.conductor/cache/registries/<registry>/index.<yaml|json>` and refreshed
  on `update`.

This produces a stable on-disk path for every registry-fetched workflow,
which is required by:

- The YAML loader's `!file` tag, which resolves relative to the workflow file.
- The checkpoint system, which derives checkpoint identity from
  `workflow_path.stem`.

### Workflow assets

Workflows can reference sibling files (prompts, JSON schemas, scripts) via
relative paths or `!file`. The cache layer fetches the workflow YAML **plus
all sibling files in its containing directory** as part of a single fetch
operation. Registry maintainers should keep a workflow and its assets in the
same directory.

For GitHub registries, sibling fetch uses the Git Trees API to enumerate the
directory and `raw.githubusercontent.com` to download files at the pinned
ref (tag).

### Run / resume / validate

These commands accept either a local path or a registry reference:

```
conductor run qa-bot@team@1.2.3 --input question="What is X?"
conductor resume qa-bot@team@1.2.3
conductor validate qa-bot@team@1.2.3
```

The resolver runs first, returns a concrete `Path` to the cached file, and
the rest of the pipeline is unchanged.

## Removed in this design

- `conductor init` and the entire `src/conductor/cli/init.py` module.
- `conductor templates` command.
- `src/conductor/templates/` directory and its bundled YAML templates.
- `tests/test_cli/test_init.py`.
- All references in README and docs.

This is a breaking change for anyone using `init` or `templates`. Both are
recent, undocumented in many places, and supersede-able by a 5-line
`curl | tee` or by configuring the new registry.

## Implementation

### New package: `src/conductor/registry/`

| Module      | Responsibility                                                                                                                                                                                                       |
| ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `config.py` | Pydantic models for `registries.toml`. Atomic load/save. Handles missing-file case (returns empty config).                                                                                                            |
| `resolver.py` | Parses `name[@registry][@version]`. Decides file-vs-ref. Returns a `ResolvedRef` with registry name, workflow name, version, and the registry config.                                                                |
| `index.py`  | Loads and parses `index.yaml`/`index.json`. Validates structure. Resolves `latest` to a concrete version. Backed by either the local FS or `github.py`.                                                               |
| `cache.py`  | Manages `~/.conductor/cache/registries/`. `get_or_fetch(ref) -> Path`. Idempotent. Fetches sibling files. Knows when to refetch (`latest`) vs. reuse (explicit version).                                              |
| `github.py` | Public-only GitHub helpers: fetch a file at a ref via raw URL, list tags via the REST API for `latest`, list directory contents via Git Trees API for sibling enumeration. Uses `httpx`, no auth.                    |

### CLI: `src/conductor/cli/registry.py`

Implements the `conductor registry` Typer subcommand group. Replaces PR #88's
file of the same name.

### Modifications

- `src/conductor/cli/app.py`
  - Register the `registry` subcommand group.
  - Remove `init` and `templates` commands and their imports.
  - In `run`, `resume`, `validate`: pre-process the workflow argument through
    the resolver to produce a `Path`.
- `src/conductor/cli/run.py` — accept resolved `Path`, no other change.

### Tests

- `tests/test_registry/` — unit tests for `config`, `resolver`, `index`,
  `cache`, `github` (with `httpx` mocked).
- `tests/test_cli/test_registry_commands.py` — CLI surface tests using
  Typer's `CliRunner`.
- `tests/test_cli/test_app.py` — drop assertions about `init` / `templates`.
- Delete `tests/test_cli/test_init.py`.

### Docs

- `README.md` — remove templates section, add a brief registry section with
  link to this doc.
- `docs/cli-reference.md` — add `registry` subcommand reference, drop
  `init` / `templates`.
- New `docs/registry.md` (user-facing guide; this doc is the design rationale).

## Design decisions and rationale

| Decision                                  | Choice                                                  | Why                                                                                                  |
| ----------------------------------------- | ------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| Named registries vs always-inline source  | Named, configured once                                  | Mirrors npm/cargo. Short refs in `run` commands. Default registry makes the common case zero-config. |
| Versioning                                | Explicit, npm-style                                     | Reproducibility. Lockfile-friendly later. Matches existing GitHub tag conventions.                   |
| Local-registry layout                     | Directory + `index.yaml`                                | Consistent with GitHub registries. Maintainer controls what's exposed and at what versions.          |
| Caching strategy                          | Local cache, refresh on `registry update` or new version | Avoids per-run network. Stable on-disk paths needed for `!file` and checkpoints.                     |
| Reference syntax                          | `name@registry@version`                                 | Visually unambiguous. `@` parses cleanly. Supports either-or-both omissions.                         |
| Publish / publish validation              | Dropped                                                 | Distribution is `git push` + tag. Validation belongs in user CI, not the CLI.                        |
| Authenticated/private registries          | Out of scope v1                                         | Public raw URLs cover the common case. Token support can come later via a registry config field.    |
| SemVer ranges                             | Out of scope v1                                         | Adds resolver complexity for marginal benefit until ecosystems exist.                                |
| Index format                              | YAML primary, JSON fallback                             | Consistent with workflow files. JSON tolerated for tooling.                                          |

## Open questions

- **Sibling fetch scope for GitHub.** Should we fetch only files in the
  workflow's immediate directory, or recurse? Proposal: immediate directory
  only in v1. Workflows that need deeper assets can flatten their layout.
- **Cache size management.** Unbounded cache growth is fine for v1 (workflows
  are small text). A `conductor registry prune` command can come later.
- **Empty default registry.** Ship with no default configured. The first
  `conductor registry add ... --default` sets it. Avoids hardcoding a repo
  that may not exist or that the project doesn't want to bless.

## Migration

PR #88 is unmerged. There is no existing registry feature on `main` to
migrate. The `init` / `templates` removal is technically a breaking change
but those commands are recent and trivially replaced by users copying any
example YAML.

## Future work

- SemVer range matching.
- Authenticated GitHub fetch (token in config or via `gh auth token`).
- Other sources (HTTPS tarball, OCI artifacts).
- A lockfile (`conductor.lock`) capturing exact versions used by a project.
- Signed indexes or workflow content for trusted distribution.
