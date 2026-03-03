# Solution Design: Auto-Update System for Conductor

| Field | Value |
|-------|-------|
| **Status** | Draft |
| **Author** | Copilot |
| **Revision** | 2 — Address technical review feedback |
| **Source** | `docs/projects/releases/release-management.brainstorm.md` |

---

## Executive Summary

This design introduces a complete auto-update lifecycle for Conductor: (1) a tag-triggered GitHub Actions workflow that builds and publishes GitHub Releases on `v*` tag pushes, (2) a lightweight update-check system that queries GitHub's releases API on every CLI invocation (cached 24 hours, 2-second timeout, TTY-only hints, zero new dependencies), and (3) a `conductor update` command that self-upgrades by pinning to the detected release tag via `uv tool install --force git+...@v{version}`. Together these ensure users are notified of new versions non-intrusively and can upgrade with a single command, while maintainers get a one-step release process backed by CI quality gates.

---

## Background

### Current State

- Conductor is distributed as a `uv` tool installed from GitHub: `uv tool install git+https://github.com/microsoft/conductor.git`.
- The version is hardcoded as `__version__ = "0.1.0"` in `src/conductor/__init__.py` and mirrored in `pyproject.toml`.
- There are no git tags, no GitHub Releases, and no mechanism to notify users of updates.
- The existing CI workflow (`.github/workflows/ci.yml`) runs lint, typecheck, tests, and build on push/PR to `main`.
- The CLI already uses `~/.conductor/` for runtime state (PID files in `~/.conductor/runs/`), establishing a precedent for user-level state.
- The CLI has a `--silent` flag and outputs progress to stderr via a Rich `Console(stderr=True)`.

### Why Now

The project is approaching its first meaningful release cycle. Without update notifications, early adopters will silently fall behind, creating support burden and fragmented bug reports. Adding this now — before the user base grows — means the mechanism is in place for every future release.

---

## Problem Statement

1. **No release process**: There is no automated way to build, test, and publish a release. Maintainers must manually create GitHub Releases.
2. **No update awareness**: Users have no way to know when a new version is available. The only option is to manually check the repository.
3. **No upgrade command**: Even if a user discovers a new version, they must remember the full `uv tool install --force git+...` incantation.

---

## Goals and Non-Goals

### Goals

1. **Automated releases**: Pushing a `v*` tag triggers a CI workflow that runs quality gates and creates a GitHub Release with build artifacts.
2. **Passive update notification**: Every CLI invocation checks for updates (cached, non-blocking, TTY-only) and prints a one-line hint when a newer version exists.
3. **One-command upgrade**: `conductor update` fetches the latest version and self-upgrades.
4. **Zero new dependencies**: All network and comparison logic uses Python stdlib (`urllib.request`, tuple comparison).
5. **Non-intrusive**: Update checks never block the CLI (2s timeout), never print in piped/scripted usage, and respect `--silent`.

### Non-Goals

- **PyPI publishing**: Not in scope. Distribution remains via GitHub.
- **Auto-update without user action**: The system hints; the user must run `conductor update`.
- **Changelog generation tooling**: Release notes are generated from `git log` in the workflow; no separate changelog tool is added.
- **Pre-release channel management**: Pre-release tags are supported (marked as `--prerelease` in the GitHub Release) but there is no opt-in/opt-out mechanism for pre-release notifications.
- **Windows-specific testing**: The workflow and update system target Unix-like systems; Windows is best-effort.

---

## Requirements

### Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-1 | A GitHub Actions workflow triggers on `v*` tag push and creates a GitHub Release. |
| FR-2 | The workflow runs tests, lint, and build as quality gates before creating the release. |
| FR-3 | Pre-release tags (e.g., `v0.2.0-beta.1`) produce a pre-release GitHub Release. |
| FR-4 | Release notes are auto-generated from commit history since the previous tag. |
| FR-5 | Build artifacts (`.whl`, `.tar.gz`) are attached to the release. |
| FR-6 | `check_for_update_hint()` is called on every CLI invocation in the `@app.callback`. |
| FR-7 | The update check result is cached at `~/.conductor/update-check.json` for 24 hours. |
| FR-8 | Update hints are only displayed when stderr is a TTY and verbosity is not `SILENT`. |
| FR-9 | `is_newer(remote, local)` performs semver-aware comparison using tuple logic. |
| FR-10 | `conductor update` runs `uv tool install --force git+https://github.com/microsoft/conductor.git@v{version}` pinned to the detected release tag, shows before/after versions, and clears the cache. |

### Non-Functional Requirements

| ID | Requirement |
|----|-------------|
| NFR-1 | The GitHub API fetch has a 2-second timeout and fails silently on any error. |
| NFR-2 | No new runtime dependencies are added. |
| NFR-3 | Update check adds < 50ms overhead when cache is fresh. |
| NFR-4 | All new code passes `make lint`, `make typecheck`, and `make test`. |

---

## Proposed Design

### Architecture Overview

```
┌───────────────────────────────────────────────────────┐
│                    GitHub Actions                       │
│  ┌─────────────────────────────────────────────────┐   │
│  │ release.yml (on push tags: v*)                  │   │
│  │  lint → typecheck → test → build → gh release   │   │
│  └─────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────┐
│                    CLI (app.py)                         │
│                                                        │
│  @app.callback main()                                  │
│    └── check_for_update_hint(console)                  │
│        (skipped when subcommand is 'update')           │
│                                                        │
│  @app.command update                                   │
│    └── run_update(console)                             │
└───────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────┐
│              update.py (new module)                     │
│                                                        │
│  ┌─────────────┐  ┌──────────────────┐                │
│  │ Cache Layer  │  │  GitHub API      │                │
│  │ read_cache() │  │  fetch_latest()  │                │
│  │ write_cache()│  │  urllib.request   │                │
│  │ get_cache_   │  │  2s timeout      │                │
│  │   path()     │  └──────────────────┘                │
│  └─────────────┘                                      │
│                                                        │
│  ┌──────────────┐  ┌──────────────────┐               │
│  │ Comparison    │  │  Update Action   │               │
│  │ is_newer()    │  │  run_update()    │               │
│  │ parse_version │  │  subprocess.run  │               │
│  │ has_prerelease│  │  uv tool install │               │
│  └──────────────┘  │  @v{tag_name}    │               │
│                     └──────────────────┘               │
│                                                        │
│  check_for_update_hint(console)                        │
│    → read_cache or fetch → compare → print hint        │
└───────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────┐
│           ~/.conductor/update-check.json               │
│  {                                                     │
│    "latest_version": "0.3.0",                          │
│    "tag_name": "v0.3.0",                               │
│    "release_url": "https://github.com/...",            │
│    "checked_at": "2026-03-03T22:00:00Z"                │
│  }                                                     │
└───────────────────────────────────────────────────────┘
```

### Key Components

#### 1. GitHub Release Workflow (`.github/workflows/release.yml`)

**Responsibilities:**
- Trigger on `v*` tag pushes
- Run full CI quality gates (lint, typecheck, test)
- Extract version from the git tag
- Build the package with `uv build`
- Generate release notes from git log (commits since previous tag)
- Create a GitHub Release via `gh release create`, attaching build artifacts
- Mark pre-release tags appropriately

**Key design decisions:**
- Reuses the same Python/uv setup pattern as `ci.yml` for consistency.
- Uses `gh release create` (GitHub CLI) rather than the `actions/create-release` action, which is archived. `gh` is pre-installed on GitHub-hosted runners.
- Release notes are generated with `gh release create --generate-notes` which uses GitHub's built-in release notes generation.
- Pre-release detection uses a simple pattern match: if the tag contains `-` after the version (e.g., `v0.2.0-beta.1`), it's a pre-release.

#### 2. Update Check Module (`src/conductor/cli/update.py`)

**Responsibilities:**
- Cache management (read/write/expiry at `~/.conductor/update-check.json`)
- GitHub API fetching (latest release version)
- Version comparison (semver tuple comparison)
- Hint display (one-line Rich-formatted message)
- Update execution (subprocess calling `uv tool install --force` pinned to release tag)

**Functions:**

| Function | Signature | Description |
|----------|-----------|-------------|
| `get_cache_path()` | `() -> Path` | Returns `~/.conductor/update-check.json` |
| `read_cache()` | `() -> dict | None` | Read cache, return `None` if missing/expired (>24h) |
| `write_cache(version, tag_name, url)` | `(str, str, str) -> None` | Write version, tag_name, and timestamp to cache |
| `fetch_latest_version()` | `() -> tuple[str, str, str] | None` | GET GitHub API, 2s timeout, returns `(version, tag_name, url)` or `None` on any error |
| `parse_version(version_str)` | `(str) -> tuple[int, ...]` | Parse `"0.2.0"` → `(0, 2, 0)`, strips leading `v` and pre-release suffix |
| `has_prerelease(version_str)` | `(str) -> bool` | Returns `True` if version contains a pre-release suffix (e.g., `-beta.1`) |
| `is_newer(remote, local)` | `(str, str) -> bool` | Compare version strings via parsed tuples; also returns `True` when tuples are equal but local has a pre-release suffix and remote does not |
| `check_for_update_hint(console)` | `(Console) -> None` | Main entry: cache-or-fetch → compare → print hint |
| `run_update(console)` | `(Console) -> None` | Fetch latest, compare, run `uv tool install --force git+...@{tag_name}`, show result, clear cache |

**Cache format (`~/.conductor/update-check.json`):**

```json
{
  "latest_version": "0.3.0",
  "tag_name": "v0.3.0",
  "release_url": "https://github.com/microsoft/conductor/releases/tag/v0.3.0",
  "checked_at": "2026-03-03T22:00:00+00:00"
}
```

**Cache expiry:** 24 hours based on `checked_at` timestamp.

#### 3. CLI Integration (`src/conductor/cli/app.py`)

**Changes:**
- Import and call `check_for_update_hint(console)` at the end of the `main()` callback, guarded by: `console.is_terminal and console_verbosity.get() != ConsoleVerbosity.SILENT`. Additionally, skip the check when the invoked subcommand is `update` (detected via `sys.argv`) to avoid showing "Update available!" immediately before updating.
- Register a new `update` command that calls `run_update(console)`.

### Data Flow

#### Update Hint Flow (every CLI invocation)

```
main() callback
  ├── Set verbosity (existing)
  └── check_for_update_hint(console)
        ├── Guard: is stderr a TTY? Is verbosity != SILENT? → skip if no
        ├── Guard: is subcommand 'update'? → skip if yes
        ├── read_cache()
        │     ├── File missing → None
        │     ├── JSON invalid → None
        │     └── checked_at > 24h ago → None
        │     └── Valid → {latest_version, tag_name, release_url, checked_at}
        ├── If cache is None → fetch_latest_version()
        │     ├── GET https://api.github.com/repos/microsoft/conductor/releases/latest
        │     │     Headers: Accept: application/vnd.github.v3+json
        │     │     Timeout: 2s
        │     ├── Parse response JSON → tag_name, html_url
        │     ├── write_cache(version, tag_name, url)
        │     └── Return (version, tag_name, url) or None on any error
        ├── Compare: is_newer(remote_version, __version__)
        │     ├── Parse both to tuples, compare numerically
        │     └── If tuples equal: return True if local has prerelease suffix and remote does not
        └── If newer → console.print("💡 Conductor vX.Y.Z available ...")
```

#### Update Command Flow

```
conductor update
  ├── Print current version
  ├── fetch_latest_version() (always fresh, bypass cache)
  │     └── On failure → print error, exit
  │     └── Returns (version, tag_name, url)
  ├── is_newer(remote, local)
  │     └── If not newer → "Already up to date!", exit
  ├── Print "Updating to vX.Y.Z..."
  ├── subprocess.run(["uv", "tool", "install", "--force",
  │                    "git+https://github.com/microsoft/conductor.git@{tag_name}"])
  │     └── tag_name is the raw tag from the API (e.g., "v0.3.0")
  │     └── On failure → print error, exit 1
  ├── Print "Updated successfully! vOLD → vNEW"
  └── Clear cache (delete update-check.json)
```

### Design Decisions

| Decision | Rationale |
|----------|-----------|
| **`urllib.request` over `httpx`/`requests`** | Zero new dependencies. The single GET request is trivial; stdlib is sufficient. |
| **Tuple comparison over `packaging.version`** | Avoids adding `packaging` as a dependency. Conductor uses simple `X.Y.Z` semver; tuple comparison is correct and adequate. Pre-release suffixes (e.g., `-beta.1`) are stripped for the numeric comparison. When tuples are equal but the local version has a pre-release suffix and the remote does not, the remote is treated as newer (pre-release → release upgrade). |
| **Version-pinned install (`@{tag_name}`)** | `run_update()` pins the install to the exact release tag (e.g., `git+...@v0.3.0`) rather than installing from main HEAD. This ensures the installed version matches the detected release, avoiding unreleased commits. The raw `tag_name` from the GitHub API is used directly. |
| **24-hour cache TTL** | Balances freshness with API rate limits. GitHub's unauthenticated rate limit is 60 req/hour; once per 24h is negligible. |
| **2-second network timeout** | Prevents blocking the CLI. If the API is slow or unreachable, the user's workflow is unaffected. |
| **TTY-only + non-silent guard** | Piped/scripted usage (CI, automation) should never see update hints. `--silent` explicitly opts out of all progress output. |
| **Skip update hint for `update` subcommand** | Running `conductor update` should not first print "Update available!" before proceeding to update. The subcommand is detected via `sys.argv` in the `main()` callback. |
| **`gh release create --generate-notes`** | GitHub's built-in release notes generation produces categorized PR-based notes. Avoids custom commit parsing. |
| **Pre-release detection via `-` in tag** | Semver spec: pre-release versions have a hyphen after the patch number. Simple string check is sufficient. |
| **Cache in `~/.conductor/`** | Consistent with existing PID file storage in `~/.conductor/runs/`. User-level, not project-level. |
| **`detect_install_method()` deferred** | The brainstorm document includes install method detection (`uv tool list` parsing). This is deferred from v1: `conductor update` will attempt `uv tool install --force` and print a clear error on failure. If users report issues with non-git installs, the detection can be added as a fast follow. |

---

## Dependencies

### External Dependencies

| Dependency | Type | Notes |
|------------|------|-------|
| GitHub API (`api.github.com`) | Service | Unauthenticated; 60 req/hour rate limit; only used once per 24h |
| `gh` CLI | CI tool | Pre-installed on GitHub-hosted runners; used in release workflow |
| `uv` | CLI tool | Required for `conductor update`; already a prerequisite for installation |

### Internal Dependencies

| Component | Dependency |
|-----------|-----------|
| `update.py` | `conductor.__version__` for local version |
| `update.py` | `~/.conductor/` directory (created by `get_cache_path()`) |
| `app.py` changes | `update.py` functions |
| Release workflow | Existing CI checks (lint, typecheck, test, build) |

### Sequencing Constraints

- The release workflow (Epic 1) can be developed independently.
- The update module (Epic 2) can be developed independently.
- CLI integration (Epic 3) depends on Epic 2.
- Docs/skill updates (Epic 4) depend on Epics 2-3.
- Tests (within each epic) are developed alongside implementation.

---

## Impact Analysis

### Components Affected

| Component | Impact |
|-----------|--------|
| `src/conductor/cli/app.py` | Add `update` command + `check_for_update_hint()` call in `main()` callback |
| `src/conductor/cli/` | New `update.py` module |
| `.github/workflows/` | New `release.yml` workflow |
| `AGENTS.md` | Add `conductor update` to common commands |
| `.claude/skills/conductor/SKILL.md` | Add `conductor update` to Quick Reference |
| `.claude/skills/conductor/references/execution.md` | Add `conductor update` section |
| `tests/test_cli/` | New `test_update.py` |

### Backward Compatibility

- **Fully backward compatible.** No existing behavior changes.
- The update hint is additive (only appears on TTY, non-silent).
- The `update` command is a new subcommand; no existing commands are affected.
- The release workflow triggers only on tag push; it doesn't affect existing CI.

### Performance Implications

- **Cache hit path**: ~10-20ms (file read + JSON parse + timestamp comparison). Negligible.
- **Cache miss path**: Up to 2s network request, but this happens at most once per 24 hours per machine.
- **No impact on workflow execution**: The check runs in the `main()` callback before any subcommand, and it's synchronous but fast.

---

## Security Considerations

- **No authentication tokens stored.** The GitHub API request is unauthenticated.
- **No code execution from remote.** The update command runs a fixed `uv tool install` command with a hardcoded repository URL. It does not download and execute arbitrary code.
- **Cache file permissions.** The cache file is written to `~/.conductor/` with default user permissions. No sensitive data is stored (only version string and URL).
- **HTTPS only.** All GitHub API requests use HTTPS.

---

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| GitHub API rate limiting (60 req/hour unauthenticated) | Low | Low | 24h cache ensures at most 1 request/day/machine |
| GitHub API unavailable | Low | Low | 2s timeout + silent failure; CLI works normally |
| `uv` not available on PATH during `conductor update` | Low | Medium | If user installed Conductor, `uv` is available. Print clear error if not. |
| Cache file corruption | Low | Low | Treat invalid JSON same as missing cache; re-fetch |
| Version comparison edge cases (pre-release, non-semver) | Low | Low | `parse_version` strips pre-release suffixes; `has_prerelease` handles pre→release upgrades; non-parseable versions return `(0,)` |
| Non-git install breaks `conductor update` | Low | Medium | `uv tool install --force git+...` may fail if user installed via pip/other. Print clear error message. `detect_install_method()` deferred to v2 if reports emerge. |

---

## Open Questions

| # | Question | Status |
|---|----------|--------|
| 1 | Should `conductor update` support installing a specific version (e.g., `conductor update v0.2.0`)? | **Deferred** — not in initial scope; can be added later. |
| 2 | Should pre-release versions trigger update hints for stable users? | **No** — only the `/releases/latest` endpoint is queried, which excludes pre-releases by default. |
| 3 | Should there be a `--no-update-check` flag or environment variable to disable hints globally? | **Deferred** — `--silent` covers the immediate need. Can add `CONDUCTOR_NO_UPDATE_CHECK=1` later if requested. |
| 4 | Should `detect_install_method()` be included in v1? | **No** — deferred. The brainstorm document includes `detect_install_method()` to parse `uv tool list` and detect if Conductor was installed via git. For v1, `conductor update` assumes `uv tool install --force git+...` and prints a clear error on failure. If users report issues with non-git installs, this function can be added as a fast follow to warn before attempting the upgrade. |

---

## Implementation Phases

### Phase 1: Release Workflow
**Exit criteria:** Pushing a `v*` tag to the repository triggers a workflow that runs quality gates and creates a GitHub Release with artifacts.

### Phase 2: Update Check Module
**Exit criteria:** `update.py` module exists with full cache, fetch, compare, hint, and update logic. All functions are unit-tested.

### Phase 3: CLI Integration
**Exit criteria:** `conductor update` command works. Update hints appear on TTY, non-silent CLI runs when a newer version is cached/fetched. Existing tests still pass.

### Phase 4: Documentation & Skill Updates
**Exit criteria:** `AGENTS.md`, skill files, and execution docs reflect the new `conductor update` command.

---

## Files Affected

### New Files

| File Path | Purpose |
|-----------|---------|
| `.github/workflows/release.yml` | Tag-triggered release workflow |
| `src/conductor/cli/update.py` | Update check, version comparison, and self-update logic |
| `tests/test_cli/test_update.py` | Tests for all update module functionality |

### Modified Files

| File Path | Changes |
|-----------|---------|
| `src/conductor/cli/app.py` | Add `update` command; call `check_for_update_hint()` in `main()` callback |
| `AGENTS.md` | Add `conductor update` to Common Commands section |
| `.claude/skills/conductor/SKILL.md` | Add `conductor update` to Quick Reference |
| `.claude/skills/conductor/references/execution.md` | Add `conductor update` CLI command section |

### Deleted Files

| File Path | Reason |
|-----------|--------|
| *(none)* | |

---

## Implementation Plan

### Epic 1: GitHub Release Workflow

**Status:** DONE

**Goal:** Create a tag-triggered CI/CD workflow that produces GitHub Releases.

**Prerequisites:** None.

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E1-T1 | IMPL | Create `.github/workflows/release.yml` with tag trigger (`v*`), Python + uv setup, lint/typecheck/test jobs, build step, version extraction from tag, pre-release detection, and `gh release create` with `--generate-notes` and artifact upload. | `.github/workflows/release.yml` | DONE |

**Acceptance Criteria:**
- [x] Workflow YAML is valid and follows the patterns established in `ci.yml`
- [x] Workflow triggers only on `v*` tag pushes (not branches)
- [x] Quality gates (lint, typecheck, test) run before release creation
- [x] Pre-release tags produce pre-release GitHub Releases
- [x] Build artifacts (`.whl`, `.tar.gz`) are attached to the release

---

### Epic 2: Update Check Module

**Goal:** Implement the core update-check logic with cache, fetch, comparison, hint display, and update execution.

**Prerequisites:** None (can be developed in parallel with Epic 1).

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E2-T1 | IMPL | Create `src/conductor/cli/update.py` with: `get_cache_path()` returning `~/.conductor/update-check.json`; `read_cache()` that returns cached data or `None` if missing/expired/invalid; `write_cache(version, tag_name, url)` that writes JSON with `tag_name` and `checked_at` timestamp. | `src/conductor/cli/update.py` | TO DO |
| E2-T2 | IMPL | Add `fetch_latest_version()` using `urllib.request.urlopen` with 2s timeout to GET `api.github.com/repos/microsoft/conductor/releases/latest`, parse JSON response for `tag_name` and `html_url`, strip leading `v` for version, return `(version, tag_name, url)` 3-tuple or `None` on any error. | `src/conductor/cli/update.py` | TO DO |
| E2-T3 | IMPL | Add `parse_version(version_str)` that strips leading `v`, splits on `-` to remove pre-release suffix, splits on `.`, converts to `tuple[int, ...]`. Add `has_prerelease(version_str)` that returns `True` if the version contains a `-` after the numeric portion. Add `is_newer(remote, local)` that compares parsed tuples; additionally, if tuples are equal but local has a pre-release suffix and remote does not, return `True` (pre-release → release upgrade). | `src/conductor/cli/update.py` | TO DO |
| E2-T4 | IMPL | Add `check_for_update_hint(console)` that reads cache (or fetches if stale), compares versions with `is_newer()`, and prints a one-line Rich hint: `💡 Conductor vX.Y.Z available (you have vCURRENT). Run 'conductor update' to upgrade.` | `src/conductor/cli/update.py` | TO DO |
| E2-T5 | IMPL | Add `run_update(console)` that fetches latest version (bypassing cache), compares with local, runs `subprocess.run(["uv", "tool", "install", "--force", "git+https://github.com/microsoft/conductor.git@{tag_name}"])` where `{tag_name}` is the raw tag from the API (e.g., `v0.3.0`), prints before/after versions, and deletes the cache file. | `src/conductor/cli/update.py` | TO DO |
| E2-T6 | TEST | Create `tests/test_cli/test_update.py` with tests for: `get_cache_path()` returns correct path; `read_cache()` returns `None` for missing/expired/invalid files and valid data for fresh cache; `write_cache()` creates valid JSON with `tag_name` field; `parse_version()` handles `"0.1.0"`, `"v0.2.0"`, `"0.3.0-beta.1"`; `has_prerelease()` returns correct results; `is_newer()` for various version pairs including pre-release → release upgrade (e.g., `is_newer("0.3.0", "0.3.0-beta.1")` → `True`). | `tests/test_cli/test_update.py` | TO DO |
| E2-T7 | TEST | Add tests for `fetch_latest_version()` with mocked `urllib.request.urlopen` (success returns 3-tuple, timeout, HTTP error, malformed JSON). Add tests for `check_for_update_hint()` with mocked fetch and cache (fresh cache newer, fresh cache same, stale cache triggers fetch, non-TTY skips, silent mode skips, `update` subcommand skips). | `tests/test_cli/test_update.py` | TO DO |
| E2-T8 | TEST | Add tests for `run_update()` with mocked subprocess (success with version-pinned install, failure, already up to date). Verify the subprocess command includes `@{tag_name}` suffix. Verify cache is cleared on success. Verify before/after version display. | `tests/test_cli/test_update.py` | TO DO |

**Acceptance Criteria:**
- [ ] All functions in `update.py` have docstrings and type hints
- [ ] Cache read/write/expiry logic works correctly; cache includes `tag_name` field
- [ ] `fetch_latest_version()` handles all error cases silently and returns 3-tuple
- [ ] `is_newer()` correctly compares semver versions including pre-release → release upgrade
- [ ] `has_prerelease()` correctly identifies pre-release version strings
- [ ] `check_for_update_hint()` respects TTY and verbosity guards, skips when subcommand is `update`
- [ ] `run_update()` runs `uv tool install --force git+...@{tag_name}` with version-pinned install and reports results
- [ ] All tests pass; no new dependencies added
- [ ] `make lint` and `make typecheck` pass

---

### Epic 3: CLI Integration

**Goal:** Wire the update module into the CLI app.

**Prerequisites:** Epic 2.

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E3-T1 | IMPL | In `app.py` `main()` callback, add a call to `check_for_update_hint(console)` at the end, guarded by `console.is_terminal` and `console_verbosity.get() != ConsoleVerbosity.SILENT`. Skip when the invoked subcommand is `update` (check `sys.argv`). Use deferred import to avoid startup overhead. | `src/conductor/cli/app.py` | TO DO |
| E3-T2 | IMPL | In `app.py`, add a new `@app.command() def update()` command that imports and calls `run_update(console)`, wrapping errors in `print_error()` and `typer.Exit(code=1)`. | `src/conductor/cli/app.py` | TO DO |
| E3-T3 | TEST | Add CLI-level tests using `CliRunner` to verify: `conductor update` invokes `run_update`; update hint appears in non-silent TTY mode; update hint does not appear in silent mode; update hint does not appear when subcommand is `update`. | `tests/test_cli/test_update.py` | TO DO |

**Acceptance Criteria:**
- [ ] `conductor update` is a registered command visible in `conductor --help`
- [ ] Update hints appear in TTY, non-silent mode when a newer version is cached
- [ ] Update hints do NOT appear in `--silent` mode or when piped
- [ ] Update hints do NOT appear when the subcommand is `update`
- [ ] All existing tests still pass
- [ ] `make lint` and `make typecheck` pass

---

### Epic 4: Documentation & Skill Updates

**Goal:** Update all documentation and skill files to reflect the new `conductor update` command.

**Prerequisites:** Epics 2-3.

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E4-T1 | IMPL | Add `conductor update` to `AGENTS.md` Common Commands section, after the `conductor stop` entries. | `AGENTS.md` | TO DO |
| E4-T2 | IMPL | Add `conductor update` to `.claude/skills/conductor/SKILL.md` Quick Reference section. | `.claude/skills/conductor/SKILL.md` | TO DO |
| E4-T3 | IMPL | Add a `### conductor update` section to `.claude/skills/conductor/references/execution.md` after the `### conductor stop` section, documenting the command, its behavior, and examples. | `.claude/skills/conductor/references/execution.md` | TO DO |

**Acceptance Criteria:**
- [ ] `AGENTS.md` lists `conductor update` in Common Commands
- [ ] Skill Quick Reference includes `conductor update`
- [ ] Execution reference documents the `update` command with examples

---

## References

- [Brainstorm document](./release-management.brainstorm.md) — original design notes
- [GitHub REST API — Latest Release](https://docs.github.com/en/rest/releases/releases#get-the-latest-release) — API endpoint used for version checks
- [`gh release create` docs](https://cli.github.com/manual/gh_release_create) — GitHub CLI release creation
- [Semver spec](https://semver.org/) — versioning standard
- [Existing CI workflow](../../.github/workflows/ci.yml) — pattern reference for the release workflow
- [PID file utilities](../../src/conductor/cli/pid.py) — precedent for `~/.conductor/` usage
