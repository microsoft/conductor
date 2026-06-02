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
<workflow>[@<registry>][#<ref>]
```

`@` separates the workflow name from the registry name. `#` separates the
ref (a git tag, branch name, or commit SHA) from the rest.

Resolution rules, in order:

1. If the argument exists as a file on disk, treat it as a local path.
2. Otherwise parse as a registry reference.
3. Missing `@<registry>` → use the configured default registry.
4. An empty registry between `@` and `#` (e.g. `name@#ref`) is allowed and
   means "use the default registry at this ref".
5. Missing `#<ref>` → use `latest`. `latest` resolves to the **default
   branch HEAD** of the registry repo (re-resolved to a fresh commit SHA
   on every fetch). To pin to a release, use `#<tag>` explicitly.
6. An empty ref after `#` (e.g. `name@reg#`) is a hard error.
7. Multiple `@` or multiple `#` in a single reference are hard errors.
8. Path-type registries do not support `#<ref>`. Passing
   `name@local#anything` against a path registry is a hard error.

Note: `#` is significant to most shells. Quote registry references in
shell commands (`conductor run 'qa-bot@team#v1.2.3'`) and avoid spaces
around the `#`.

Examples:

```
conductor run ./my-workflow.yaml          # local file (unchanged)
conductor run qa-bot                      # latest from default registry
conductor run qa-bot@team                 # latest from `team` registry
conductor run 'qa-bot@team#v1.2.3'        # tag v1.2.3 from `team`
conductor run 'qa-bot@#v1.2.3'            # tag v1.2.3 from default registry
conductor run 'qa-bot@team#main'          # default-branch HEAD of `team`
conductor run 'qa-bot@team#a1b2c3d'       # specific commit SHA
```

### Registry index

Each registry root must contain `index.yaml` (preferred) or `index.json`:

```yaml
workflows:
  qa-bot:
    description: "Simple Q&A workflow"
    path: workflows/qa-bot.yaml          # path relative to registry root
  code-review:
    description: "Multi-agent code review"
    path: workflows/code-review.yaml
```

The index is the single source of truth for what workflows exist and where
they live in the repo. Available versions are **not** listed in the index —
for GitHub registries they are auto-discovered from the registry repo's git
tags; for path registries no versioning exists. Conductor does not
auto-discover YAML files in a registry — the maintainer curates the index.

### Versioning

Versioning is automatic and tag-driven for GitHub registries:

- **Auto-discovery**: available versions are the registry repo's git tags,
  fetched on demand via the GitHub API for display in `registry list`/`show`.
  Maintainers do not list versions in `index.yaml`.
- **`latest` resolution**: `latest` (the default when no `#<ref>` is given)
  always resolves to the **default branch HEAD** of the registry repo.
  This means a bare `name@registry` reference always picks up the newest
  commit on the default branch — typical for development workflows.
  To pin to a tagged release, use an explicit ref: `name@registry#v1.2.3`.
- **Flexible refs**: any tag, branch, or commit SHA can be pinned via
  `#<ref>`. Branch refs are re-resolved to their current commit SHA at
  fetch time, so a branch ref always refers to the latest commit on that
  branch when a fresh fetch is performed.
- **SHA-based caching**: workflows are cached by the resolved commit SHA
  (`<cache>/<reg>/<workflow>/<sha[:12]>/`). When a branch advances, the
  cache key changes automatically — no manual invalidation needed for the
  next fresh fetch.
- **CDN bypass**: index fetches resolve the ref to a commit SHA via the
  GitHub API, then download from
  `raw.githubusercontent.com/<owner>/<repo>/<sha>/index.yaml`. The unique
  per-SHA URL bypasses Fastly's CDN cache, so you always see the current
  index for a given ref without needing a `--force` flag.
- **Path registries**: do not support refs at all. Local registries are
  always read directly from disk.

### Caching

Fetched workflows are cached at:

```
~/.conductor/cache/registries/<registry>/<workflow>/<sha[:12]>/
```

- Cache is keyed by `(registry, workflow, resolved-commit-sha)`.
- A given commit SHA is immutable, so cached entries are never re-fetched
  for the same SHA. Branch refs re-resolve to a (possibly new) SHA on each
  fresh fetch, which transparently invalidates the cache.
- Workflow files are first downloaded into a temp directory and then renamed
  atomically into the final cache path, so a partial fetch never leaves a
  half-populated entry visible to other commands.
- Index files are cached separately at
  `~/.conductor/cache/registries/<registry>/index.<yaml|json>` and refreshed
  on `update`. Index fetches always go through a SHA-pinned raw URL to
  bypass the CDN.

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
directory and SHA-pinned `raw.githubusercontent.com` URLs to download files
at the resolved commit SHA.

### Run / resume / validate

These commands accept either a local path or a registry reference:

```
conductor run 'qa-bot@team#v1.2.3' --input question="What is X?"
conductor resume 'qa-bot@team#v1.2.3'
conductor validate 'qa-bot@team#v1.2.3'
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
| `resolver.py` | Parses `name[@registry][#ref]`. Decides file-vs-ref. Returns a `ResolvedRef` with registry name, workflow name, ref, and the registry config. Rejects multiple `@`/`#`, empty `#`, and `#ref` against path registries.       |
| `index.py`  | Loads and parses `index.yaml`/`index.json`. Validates structure. Resolves `latest` to a concrete tag (or default-branch HEAD if no tags). Backed by either the local FS or `github.py`.                                |
| `cache.py`  | Manages `~/.conductor/cache/registries/`. `get_or_fetch(ref) -> Path`. Idempotent. Fetches sibling files. Cache is keyed by resolved commit SHA; writes are staged in a temp dir and renamed atomically.                |
| `github.py` | Public-only GitHub helpers: resolve a ref to a commit SHA via the GitHub API, fetch files at a SHA via SHA-pinned raw URLs (bypassing the CDN), list tags via the REST API for `latest`, list directory contents via Git Trees API for sibling enumeration. Uses `httpx`, no auth. |

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
| Versioning                                | Default branch HEAD when unpinned; pin any tag/branch/SHA via `#ref` | Bare names always follow the default branch (typical dev workflow). Tagged releases are opt-in via explicit `#<tag>`. Avoids the surprise of `latest` skipping past commits because a tag exists. Branches and SHAs are first-class refs. |
| Local-registry layout                     | Directory + `index.yaml`                                | Consistent with GitHub registries. Maintainer controls what's exposed. Local registries do not support refs.                                                                                |
| Caching strategy                          | Local cache keyed by resolved commit SHA, atomic writes | Avoids per-run network. SHA-based keys make branch refs self-invalidate on a fresh fetch. SHA-pinned raw URLs bypass the CDN, so no `--force` flag is needed.                              |
| Reference syntax                          | `name@registry#ref`                                     | Visually unambiguous: `@` selects the registry, `#` selects a git ref (tag, branch, or SHA). Both segments are independently optional.                                                       |
| Publish / publish validation              | Dropped                                                 | Distribution is `git push` + tag. Validation belongs in user CI, not the CLI.                        |
| Authenticated/private registries          | Out of scope v1                                         | Public raw URLs cover the common case. Token support can come later via a registry config field.    |
| SemVer ranges                             | Out of scope v1                                         | Adds resolver complexity for marginal benefit until ecosystems exist.                                |
| Index format                              | YAML primary, JSON fallback                             | Consistent with workflow files. JSON tolerated for tooling.                                          |

## Ad-hoc references

A **workflow reference without pre-configured registry** allows teams to compose
workflows across GitHub organizations and repositories without registry setup.

### Motivation

Configured registries are ideal for standard repos (org-wide workflows, team
templates). But ad-hoc cross-team composition is common: Team C wants to run a
workflow from Team A's repo in combination with Team B's workflow, without any
team having to register each other's repos. Ad-hoc references lower friction for
one-off usage.

### Syntax

```
workflow@owner/repo[#ref]
```

If the part after `@` contains `/`, it is treated as a literal `owner/repo`
GitHub reference (ad-hoc) and fetched directly. Otherwise it is looked up as a
configured registry name (existing behavior).

Examples:

```
analysis@myorg/team-a                # default branch HEAD of myorg/team-a
analysis@myorg/team-a#v1.0.0         # tag v1.0.0 of myorg/team-a
analysis@myorg/team-a#main           # main branch of myorg/team-a
analysis@myorg/team-a#abc1234        # specific commit SHA
```

### Disambiguation rule

At parse time, Conductor disambiguates between ad-hoc and registry references:

- `analysis@team` → registry name `team` (no `/` in the part after `@`)
- `analysis@myorg/team-a` → ad-hoc reference to `myorg/team-a` (contains `/`)

Registry names are configured by the user and cannot contain `/`, so there is
no ambiguity. Both forms coexist: configured registries are recommended for
frequently-used sources, ad-hoc references for occasional cross-team pulls.

### Caching

Ad-hoc workflows are cached at:

```
~/.conductor/cache/registries/_adhoc/<owner>/<repo>/<workflow>/<sha[:12]>/
```

This isolates ad-hoc caches from named registries, avoiding collisions when the
same workflow name exists in different sources.

### Reference resolution

Ad-hoc references follow the same resolution rules as registry references:

- Missing `#<ref>` → use the **default branch HEAD** (re-resolved on each fetch).
- Explicit `#<tag>` or `#<branch>` → pinned to that tag or the current HEAD of the branch.
- Explicit `#<sha>` → pinned to an exact commit.
- Multiple `@` or multiple `#` are hard errors.

### Authentication

Ad-hoc references use the same authentication as named GitHub registries:
- Public repos work automatically.
- Private repos use `gh auth token` if available, otherwise fail with a clear error.

### Usage

Ad-hoc references work everywhere registry references work:

```bash
conductor run 'analysis@myorg/team-a#v1.0.0' --input question="..."
conductor validate 'analysis@myorg/team-a#main'
conductor resume 'analysis@myorg/team-a#v1.0.0'
```

As a sub-workflow (see [Sub-workflows](#sub-workflows) in the workflow syntax guide):

```yaml
agents:
  - name: team_a_analysis
    type: workflow
    workflow: analysis@myorg/team-a#v1.0.0
    input_mapping:
      data: "{{ workflow.input.raw_data }}"
```

### Example: Cross-team composition

Team C's workflow references Team A's and Team B's workflows without any
pre-registry setup:

```yaml
agents:
  - name: team_a_pipeline
    type: workflow
    workflow: qa-bot@teamA/qa-workflows#main
    input_mapping:
      question: "{{ workflow.input.query }}"

  - name: team_b_pipeline
    type: workflow
    workflow: reviewer@teamB/review-workflows#v2.1.0
    input_mapping:
      content: "{{ team_a_pipeline.output.answer }}"
```

Both workflows are fetched and composed in Team C's workflow without any
registry configuration.

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
