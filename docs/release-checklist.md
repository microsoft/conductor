# Release Checklist

This is the step-by-step process for cutting a Conductor release. Releases are
fully automated once a `v*` tag is pushed: the
[`release.yml`](../.github/workflows/release.yml) workflow runs the quality
gates (lint, typecheck, tests on Python 3.12 + 3.13), builds the package, and
creates a GitHub Release with build artifacts and auto-generated notes.

The maintainer's job is therefore to (1) prepare a small release-prep PR that
bumps the version and finalizes the changelog, and (2) tag the merge commit.

## TL;DR

```bash
# 1. Pick the next version (default: bump the third/"build" number).
#    e.g. 0.1.19 -> 0.1.20

# 2. Edit CHANGELOG.md  (Unreleased -> versioned section + a fresh Unreleased)
# 3. Edit pyproject.toml  (version = "X.Y.Z")
uv lock                  # 4. Re-lock so uv.lock records the new version
make check && make test  # 5. Quality gates locally

# 6. Open + merge the release-prep PR:  chore(release): cut X.Y.Z
# 7. After merge, tag the merge commit on main and push:
git checkout main && git pull
git tag vX.Y.Z
git push origin vX.Y.Z   # triggers release.yml

# 8. Verify the Release workflow is green and the GitHub Release exists.
```

## Versioning

Conductor follows [Semantic Versioning](https://semver.org/): `major.minor.patch`
(the third number is what you may think of as the "build" number).

- **Default — patch bump** (`0.1.19 → 0.1.20`): bug fixes and
  backwards-compatible changes. This is the normal case.
- **Minor bump** (`0.1.x → 0.2.0`): new, backwards-compatible features.
- **Major bump** (`0.x → 1.0.0`): breaking changes. While the project is `0.x`,
  breaking changes are conventionally signalled by a minor bump.
- **Pre-release** (`0.2.0-beta.1`): any tag with a hyphen after the version is
  automatically marked as a GitHub pre-release. See
  [Pre-releases](#pre-releases) below.

The version lives in exactly one source of truth: the `version` field in
`pyproject.toml`. The CLI reads it at runtime via
`importlib.metadata.version("conductor-cli")` (see `src/conductor/__init__.py`),
so there is **no** separate `__version__` string to edit.

## Step-by-step

### 1. Confirm you're starting clean

- [ ] On an up-to-date `main`: `git checkout main && git pull`.
- [ ] Working tree is clean: `git status`.
- [ ] Decide the next version per [Versioning](#versioning) above.

### 2. Update `CHANGELOG.md`

The changelog follows [Keep a Changelog](https://keepachangelog.com/). The top
of the file has an `## [Unreleased]` section that accumulates entries between
releases. To cut release `X.Y.Z` (dated today):

- [ ] Rename the `[Unreleased]` heading to the new version with today's date,
      and point its compare link at the new tag. For example, releasing
      `0.1.20`:

  ```diff
  -## [Unreleased](https://github.com/microsoft/conductor/compare/v0.1.19...HEAD)
  +## [0.1.20](https://github.com/microsoft/conductor/compare/v0.1.19...v0.1.20) - 2026-06-26
  ```

- [ ] Add a **fresh, empty** `[Unreleased]` section above it, comparing the new
      tag to `HEAD`:

  ```markdown
  ## [Unreleased](https://github.com/microsoft/conductor/compare/v0.1.20...HEAD)
  ```

- [ ] Review the entries under the now-versioned section. Keep the
      `Added` / `Changed` / `Fixed` / `Removed` subsections that have content;
      drop the empty ones. Ensure each entry links its PR/issue.

### 3. Bump the version in `pyproject.toml`

- [ ] Edit the `version` field under `[project]`:

  ```diff
  -version = "0.1.19"
  +version = "0.1.20"
  ```

### 4. Re-lock `uv.lock`

The lockfile records the project version, so it must be regenerated after the
bump (CI's constraints step runs `uv export --frozen` and will fail on a stale
lock).

- [ ] Run `uv lock` (or `uv sync`) and confirm the only change is the
      `conductor-cli` version: `git diff uv.lock`.

> If this release also changes a dependency floor in `pyproject.toml`, the
> lockfile diff will be larger — that's expected. Re-run the full test suite in
> that case.

### 5. Run the quality gates locally

Mirror what `release.yml` will run so a tag push doesn't fail after the fact.

- [ ] `make check` (ruff lint + format check + `ty` typecheck).
- [ ] `make test` (or `uv run pytest -m "not real_api and not performance"`,
      which matches the CI/release filter).
- [ ] Optionally `make validate-examples` if this release touched schema or
      example workflows.

### 6. Open the release-prep PR

- [ ] Commit on a branch (not `main`). Use the established message convention:

  ```
  chore(release): cut X.Y.Z
  ```

  The commit should contain only `CHANGELOG.md`, `pyproject.toml`, and
  `uv.lock` (plus any deliberate dependency-floor change).

- [ ] Open the PR and let CI (`ci.yml`) go green.
- [ ] Get review/approval and **merge** it. The tag must point at a commit that
      already contains the version bump, so the bump has to land on `main`
      first.

### 7. Tag the merge commit and push

The release workflow extracts the version from the **tag name** (`v` stripped),
and the GitHub Release is built from the tagged commit — so the tag must match
the `pyproject.toml` version exactly and point at the merged release-prep
commit.

- [ ] Sync `main`:

  ```bash
  git checkout main && git pull
  ```

- [ ] Confirm the version on `main` matches the tag you're about to create:

  ```bash
  grep '^version' pyproject.toml      # must read X.Y.Z (no leading v)
  ```

- [ ] Create and push the tag (this is what triggers the release):

  ```bash
  git tag vX.Y.Z
  git push origin vX.Y.Z
  ```

### 8. Verify the release

- [ ] The **Release** workflow run for `vX.Y.Z` is green:
      `gh run list --workflow release.yml` /
      [Actions](https://github.com/microsoft/conductor/actions/workflows/release.yml).
- [ ] The GitHub Release exists with auto-generated notes and attached
      artifacts (`.whl`, `.tar.gz`, `constraints.txt`, `constraints.txt.sha256`):
      `gh release view vX.Y.Z`.
- [ ] Smoke-test the published install (in a clean shell):

  ```bash
  curl -sSfL https://aka.ms/conductor/install.sh | sh
  conductor --version          # prints Conductor vX.Y.Z
  ```

  The installer resolves the **latest** GitHub Release tag dynamically, so no
  install-script edits are needed per release.

## Pre-releases

To ship a pre-release, use a tag with a hyphen after the version, e.g.
`v0.2.0-beta.1`. The workflow detects the hyphen and marks the GitHub Release as
a **pre-release** automatically (`--prerelease`).

- Set `pyproject.toml` to the matching version (`0.2.0-beta.1`) and re-lock.
- The `conductor update` hint and install script track the latest **stable**
  release semantics; pre-releases are opt-in for testers who pull the tag
  directly.

## If something goes wrong

- **Release workflow failed before creating the Release**: fix the cause on
  `main` via a normal PR, then delete and re-push the tag:

  ```bash
  git push origin :refs/tags/vX.Y.Z   # delete remote tag
  git tag -d vX.Y.Z                   # delete local tag
  # ...land the fix on main, pull, then re-tag the new commit...
  ```

- **Release was created but is broken**: do **not** rewrite a published tag.
  Cut a new patch release (`X.Y.Z+1`) following this checklist. Optionally mark
  the bad GitHub Release as a pre-release or add a warning to its notes.

## What the automation does (and doesn't)

| Step | Owner |
|------|-------|
| Bump version, finalize changelog, re-lock | **You** (release-prep PR) |
| Lint, typecheck, test (3.12 + 3.13) | `release.yml` |
| Build `.whl` / `.tar.gz`, generate constraints | `release.yml` |
| Create GitHub Release + upload artifacts | `release.yml` |
| Generate release notes from commit history | `release.yml` (`--generate-notes`) |
| Publish to PyPI | _Not configured_ — distribution is via GitHub + the install script |
