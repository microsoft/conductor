# Plan: Auto-Update System for Conductor

## Context

Conductor is distributed as a `uv tool` installed from GitHub (`uv tool install git+https://github.com/microsoft/conductor.git`). There is currently **no mechanism** to check for updates, notify users, or upgrade. The version is hardcoded at `0.1.0` with no git tags or GitHub releases. Users who installed once will never know when improvements land.

**Goal:** Add a complete update lifecycle:
1. Tag-triggered GitHub Release workflow (à la Octane)
2. Proactive update-check on every CLI run (cached 24h)
3. `conductor update` CLI command
4. Skill awareness for Claude Code users

---

## Part 1: Tag-Triggered Release Workflow

**New file: `.github/workflows/release.yml`**

Triggered on push of tags matching `v*` (e.g., `v0.2.0`). Based on the Octane pattern:

```yaml
on:
  push:
    tags: ['v*']
```

The workflow will:
1. Checkout code, set up Python + uv
2. Run tests and lint (gate the release)
3. Extract version from tag (`${GITHUB_REF#refs/tags/v}`)
4. Build the package (`uv build`)
5. Generate release notes from git log since previous tag
6. Create a GitHub Release with `gh release create` + the built artifacts
7. Support prerelease tags (`v0.2.0-beta.1` → `--prerelease` flag)

**Release process for maintainers:**
```bash
# 1. Bump version in __init__.py and pyproject.toml
# 2. Commit and push
git tag v0.2.0
git push origin v0.2.0
# GitHub Actions creates the release automatically
```

---

## Part 2: Update Check System

**New file: `src/conductor/cli/update.py`**

### Functions

| Function | Description |
|----------|-------------|
| `get_cache_path()` | Returns `~/.conductor/update-check.json` |
| `read_cache()` | Read cached version info, `None` if missing or older than 24h |
| `write_cache(version, url)` | Write latest version + timestamp to cache |
| `fetch_latest_version()` | `GET api.github.com/repos/microsoft/conductor/releases/latest` via `urllib.request`, 2s timeout. Returns `(version, url)` or `None` |
| `is_newer(remote, local)` | Simple semver comparison (split on `.`, compare tuples) |
| `check_for_update_hint(console)` | Called from `main()` callback. Reads cache, fetches if stale, prints one-line hint if outdated |
| `detect_install_method()` | Parse `uv tool list` to see if installed from git or other source |
| `run_update(console)` | Execute upgrade: `uv tool install --force git+https://github.com/microsoft/conductor.git`, show before/after versions, clear cache |

### Behavior

**On every CLI run** (in the `@app.callback` `main()` function):
- Only if stderr is a TTY and not `--silent` mode
- Read cache → if fresh + version matches local → nothing
- Read cache → if fresh + version is newer → print hint:
  ```
  💡 Conductor v0.3.0 available (you have v0.1.0). Run `conductor update` to upgrade.
  ```
- Cache stale/missing → fetch from GitHub API (2s timeout, fail silently) → cache → maybe print hint

**`conductor update` command:**
- Shows current version
- Fetches latest from GitHub API
- If already up to date, says so and exits
- If update available, runs `uv tool install --force git+https://github.com/microsoft/conductor.git`
- Shows before/after version
- Clears update cache

---

## Part 3: Skill + Docs Updates

Update skill and documentation to reflect the new command:
- `.claude/skills/conductor/SKILL.md` — add `conductor update` to Quick Reference
- `.claude/skills/conductor/references/execution.md` — add `conductor update` section
- `AGENTS.md` — add `update` to common commands

---

## Files to Create/Modify

| File | Action | Description |
|------|--------|-------------|
| `.github/workflows/release.yml` | **Create** | Tag-triggered release workflow 
| `src/conductor/cli/update.py` | **Create** | Update check + self-update logic |
| `src/conductor/cli/app.py` | **Modify** | Add `update` command + call `check_for_update_hint()` in `main()` |
| `.claude/skills/conductor/SKILL.md` | **Modify** | Add `conductor update` to Quick Reference |
| `.claude/skills/conductor/references/execution.md` | **Modify** | Add `conductor update` docs section |
| `AGENTS.md` | **Modify** | Add update command to common commands |
| `tests/test_cli/test_update.py` | **Create** | Tests for cache logic, version comparison, hint display, update command |

### Key design decisions
- **No new dependencies** — uses `urllib.request` (stdlib) for HTTP, simple tuple comparison for semver
- **Cache at `~/.conductor/update-check.json`** — not inside the project
- **2s network timeout** — never block the user's workflow
- **TTY-only hints** — no noise in piped/scripted usage
- **`--silent` suppresses hints** — respects the existing verbosity system

---

## Verification

1. **`make test`** — all existing tests pass
2. **`make check`** — lint + typecheck pass
3. **New tests** in `tests/test_cli/test_update.py`:
   - Cache read/write/expiry
   - Version comparison (`is_newer`)
   - Hint display with mocked fetch
   - `conductor update` with mocked subprocess
4. **Manual testing:**
   - `conductor update` — runs the upgrade flow
   - Write a fake stale cache → run any command → see hint
   - `conductor --silent run ...` → no hint
   - Pipe output → no hint
5. **Release workflow:** Push a `v0.1.0` tag to a fork to verify the workflow runs
