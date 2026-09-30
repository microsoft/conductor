#!/usr/bin/env bash
# Enforce the changelog fragment contract for a pull request.
#
# Every PR adds a towncrier fragment in changelog.d/ instead of editing
# CHANGELOG.md (feature/fix PRs), or compiles the fragments into CHANGELOG.md
# (release-prep PRs, detected by a pyproject.toml version bump). See
# changelog.d/README.md for the naming contract this script enforces.
#
# Environment:
#   PR_LABELS       JSON array of the PR's label names, e.g. '["changelog-not-required"]'
#   PR_AUTHOR_LOGIN pull request author's GitHub login, used to recognize
#                   Dependabot-authored pull requests.
#   BASE_REF        base branch name (e.g. "main"); diffs run against
#                   "origin/$BASE_REF". Set CHANGELOG_BASE to a full ref to
#                   override (the test harness uses this to point at a local
#                   branch in a temporary repository).
#   RUNNER_TEMP     scratch directory for intermediate files (defaults to a
#                   private mktemp directory).
#
# Towncrier 25.8.0 behaviors that shape this script (verified against its
# source and pinned by tests/test_integration/test_changelog_gate.py and
# test_extract_release_notes.py):
#   (a) `towncrier check` validates the WHOLE fragments directory, not just
#       the new files (find_fragments runs with strict=True). A broken
#       fragment name that reaches main (e.g. merged via the
#       `changelog-not-required` label path) breaks `check` for every
#       subsequent PR until it is removed — so added fragments are validated
#       even when the label waives the feature-mode requirements.
#   (b) `towncrier build` silently ignores broken names (exit 0). Only
#       `towncrier check` protects the contract; release compilation alone
#       can never be relied on to surface a bad name.
#   (c) `towncrier check` exits 1 with "No new newsfragments found on this
#       branch" whenever the branch ADDS no fragment — even for a perfectly
#       valid deletion-only change — and exits 0 early ("Checks SKIPPED")
#       when CHANGELOG.md itself changed. It therefore runs only when the
#       PR adds a fragment, and fragment-name validation is owned by this
#       script's own scan of the resulting changelog.d/ tree, not by
#       towncrier.
#
# This script is executed by .github/workflows/changelog.yml and by
# tests/test_integration/test_changelog_gate.py, which runs it against
# temporary git repositories.

set -euo pipefail

fail() {
  echo "::error::$1" >&2
  exit 1
}

BASE="${CHANGELOG_BASE:-origin/${BASE_REF:?BASE_REF or CHANGELOG_BASE must be set}}"
PR_LABELS="${PR_LABELS:-[]}"
PR_AUTHOR_LOGIN="${PR_AUTHOR_LOGIN:-}"

WORK_DIR=$(mktemp -d "${RUNNER_TEMP:-/tmp}/changelog-gate.XXXXXX")
trap 'rm -rf "$WORK_DIR"' EXIT

# Single source of truth for the target version: pyproject.toml.
BASE_PYPROJECT="$WORK_DIR/base-pyproject.toml"
if ! git show "$BASE:pyproject.toml" > "$BASE_PYPROJECT" 2>/dev/null; then
  fail "Release check could not read pyproject.toml from base branch $BASE"
fi

BASE_VERSION=$(grep -m1 '^version = ' "$BASE_PYPROJECT" 2>/dev/null | cut -d'"' -f2 || true)
if [ -z "$BASE_VERSION" ]; then
  fail "Could not determine package version from $BASE:pyproject.toml"
fi

VERSION=$(grep -m1 '^version = ' pyproject.toml 2>/dev/null | cut -d'"' -f2 || true)
if [ -z "$VERSION" ]; then
  fail "Could not determine package version from pyproject.toml"
fi

VERSION_RE=$(printf '%s' "$VERSION" | sed 's/[][\.^$*+?{}()|]/\\&/g')

CHANGED_FILES="$WORK_DIR/changed-files.txt"
git diff --name-only "$BASE"...HEAD > "$CHANGED_FILES"

if [ "$BASE_VERSION" != "$VERSION" ]; then
  # RELEASE MODE: this PR bumps the package version (release-prep).
  echo "Release mode: version bump to $VERSION detected in pyproject.toml."

  # 1. A release-prep PR must compile the fragments into CHANGELOG.md.
  if ! grep -qx 'CHANGELOG.md' "$CHANGED_FILES"; then
    fail "Release mode: pyproject.toml was bumped to $VERSION but CHANGELOG.md is not changed in this PR. Compile the fragments with: make changelog-build VERSION=$VERSION — then commit CHANGELOG.md and the emptied changelog.d/. Contract: changelog.d/README.md"
  fi

  # 2. Exactly one new section for the bumped version. A duplicate section
  #    means the branch was compiled twice — towncrier refuses an existing
  #    same-day header and duplicates it on a later date — so the remedy is
  #    the restore-then-recompile runbook, not another build on top.
  section_count=$(grep -cE "^## \[${VERSION_RE}\] - " CHANGELOG.md || true)
  if [ "$section_count" -eq 0 ]; then
    fail "Release mode: expected exactly one '## [$VERSION] - <date>' section in CHANGELOG.md (found none). Generate it with: make changelog-build VERSION=$VERSION. Contract: changelog.d/README.md"
  elif [ "$section_count" -gt 1 ]; then
    fail "Release mode: found $section_count '## [$VERSION] - <date>' sections in CHANGELOG.md — the branch was compiled more than once. Do not re-run the build on top (towncrier refuses an existing header); restore the pre-compile state and compile the full fragment set in one pass per docs/release-checklist.md ('Release-prep PR blocked by the Changelog workflow'). Contract: changelog.d/README.md"
  fi

  # 3. The towncrier insertion marker must survive compilation.
  marker_count=$(grep -cFx -- '<!-- towncrier release notes start -->' CHANGELOG.md || true)
  if [ "$marker_count" -ne 1 ]; then
    fail "Release mode: the '<!-- towncrier release notes start -->' marker must appear exactly once in CHANGELOG.md (found $marker_count). Never edit the marker by hand — re-run: make changelog-build VERSION=$VERSION. Contract: changelog.d/README.md"
  fi

  # 4. The merge result must not retain any fragment: towncrier
  #    consumes them; README.md is the only permanent resident.
  leftovers=$(git ls-tree -r --name-only HEAD -- changelog.d/ | grep -v '^changelog.d/README.md$' || true)
  if [ -n "$leftovers" ]; then
    fail "Release mode: fragments must be consumed by 'make changelog-build VERSION=$VERSION' but these remain in the merge result: $(echo "$leftovers" | tr '\n' ' ') Most often another PR merged a new fragment after this branch was built. Do not just re-run the build — towncrier refuses a version whose header already exists. Restore the pre-compile state and compile the full set in one pass: git rebase $BASE && git checkout $BASE -- CHANGELOG.md changelog.d/ && make changelog-build VERSION=$VERSION && uv lock — then commit and push. Contract: changelog.d/README.md"
  fi

  # 5. Rehearse the release-notes extraction with the trusted base
  #    branch script so a PR cannot execute code it introduced.
  if git cat-file -e "$BASE:.github/scripts/extract-release-notes.sh" 2>/dev/null; then
    git show "$BASE:.github/scripts/extract-release-notes.sh" > "$WORK_DIR/extract-release-notes.sh"
  else
    fail "Release mode: .github/scripts/extract-release-notes.sh is missing on the base branch $BASE — it must land on main before release-prep PRs can pass."
  fi
  if ! bash "$WORK_DIR/extract-release-notes.sh" "$VERSION" CHANGELOG.md "$WORK_DIR/notes.md"; then
    fail "Release mode: the base-branch release-notes extractor could not extract the '## [$VERSION] - YYYY-MM-DD' section from CHANGELOG.md. Run: make changelog-build VERSION=$VERSION and commit the result. Contract: changelog.d/README.md"
  fi

  # 6. uv.lock must pin OUR package at the bumped version. uv normalizes
  #    PEP 440 prerelease spellings when locking (pyproject's 0.2.0-beta.1
  #    becomes 0.2.0b1 in uv.lock), so compare both sides as parsed versions,
  #    not raw strings — a string compare rejects a correctly-synced lockfile
  #    and re-running `uv lock` cannot change the outcome. The authored
  #    spelling stays authoritative for the tag and the CHANGELOG.md header.
  lock_rc=0
  lock_version=$(uv run --no-project --with packaging python - "$VERSION" <<'PY'
import sys
import tomllib

from packaging.version import Version

try:
    authored = Version(sys.argv[1])
    with open("uv.lock", "rb") as f:
        lock = tomllib.load(f)
except Exception as exc:
    print(f"version-parse error: {exc}", file=sys.stderr)
    sys.exit(3)

for package in lock.get("package", []):
    if package.get("name") == "conductor-cli" and package.get("version"):
        print(package["version"])
        sys.exit(0 if Version(package["version"]) == authored else 1)
sys.exit(2)
PY
  ) || lock_rc=$?
  case "$lock_rc" in
    0) ;;
    1) fail "Release mode: uv.lock pins conductor-cli at $lock_version, which does not match pyproject.toml's $VERSION under PEP 440 normalization. Re-lock with: uv lock — then commit uv.lock. Contract: changelog.d/README.md" ;;
    2) fail "Release mode: uv.lock contains no usable conductor-cli package entry. Re-lock with: uv lock — then commit uv.lock. Contract: changelog.d/README.md" ;;
    *) fail "Release mode: could not verify the uv.lock pin for conductor-cli (version-parser exit $lock_rc): ${lock_version:-no output}. Re-lock with: uv lock — then commit uv.lock. Contract: changelog.d/README.md" ;;
  esac

  echo "Release mode: changelog contract satisfied for $VERSION."
else
  # FEATURE MODE: a regular feature/fix PR.
  echo "Feature mode: no version bump in pyproject.toml."

  # A maintainer exemption waives both feature-mode requirements. Dependabot
  # authorship waives only the fragment requirement; it still may not edit the
  # compiled CHANGELOG.md. Fragment changes remain validated under either path.
  # GitHub label names are unique case-insensitively; match the same way so
  # a label created as e.g. 'Changelog-Not-Required' still waives.
  label_waived=0
  if grep -qi '"changelog-not-required"' <<< "$PR_LABELS"; then
    label_waived=1
    echo "Feature-mode requirements waived by the 'changelog-not-required' label."
  fi
  fragment_waived="$label_waived"
  if [ "$PR_AUTHOR_LOGIN" = "dependabot[bot]" ]; then
    fragment_waived=1
    echo "Changelog fragment requirement waived for a Dependabot-authored PR."
  fi

  # 1. CHANGELOG.md is compiled only at release time unless a maintainer
  #    explicitly waived the requirement.
  if [ "$label_waived" -eq 0 ] && grep -qx 'CHANGELOG.md' "$CHANGED_FILES"; then
    fail "Feature mode: CHANGELOG.md must not be edited in a feature/fix PR — it is compiled only during release preparation. Describe your change as a fragment instead: add changelog.d/+describe-your-change.added.md — or <issue>.added.md if you have an issue number. A maintainer may exempt this PR with the 'changelog-not-required' label. Contract: changelog.d/README.md"
  fi

  # 2. At least one new fragment, unless this PR is exempt. README.md is
  #    permanent contract documentation, not a changelog fragment.
  NEW_FRAGMENTS_FILE="$WORK_DIR/new-fragments.txt"
  git diff --name-only --diff-filter=A "$BASE"...HEAD -- changelog.d/ ':(exclude)changelog.d/README.md' > "$NEW_FRAGMENTS_FILE" || true
  new_fragments=$(cat "$NEW_FRAGMENTS_FILE")
  if [ -z "$new_fragments" ] && [ "$fragment_waived" -eq 0 ]; then
    fail "No changelog fragment found. Add one, e.g.: changelog.d/+describe-your-change.added.md — or <issue>.added.md if you have an issue number (categories: added|fixed|changed|removed). Contract: changelog.d/README.md"
  fi

  # 3. Whenever the PR changed anything under changelog.d/, every fragment
  #    name in the RESULTING tree must satisfy the naming contract — added
  #    files, rename destinations, and names inherited from the base branch
  #    alike (see the header comment, fact (a)). This scan replaces the
  #    whole-directory half of `towncrier check`, which cannot run on a
  #    change that adds no fragment (it exits "No new newsfragments found"
  #    even though the directory is fine — reintroducing the very fragment
  #    requirement the exemption label waives for a deletion-only PR).
  #    The ignore list mirrors towncrier's own (find_fragments skips these
  #    basenames, case-insensitively). The scan is deliberately stricter
  #    than towncrier for names — the contract requires '+' for non-numeric
  #    slugs, towncrier does not — but the load-bearing direction holds: any
  #    name the scan accepts, towncrier also accepts, so nothing it
  #    green-lights can break a later 'towncrier check' on main.
  if ! git diff --quiet "$BASE"...HEAD -- changelog.d/ ':(exclude)changelog.d/README.md'; then
    while IFS= read -r fragment; do
      [ -n "$fragment" ] || continue
      if [ "$(dirname "$fragment")" != "changelog.d" ]; then
        fail "Fragment '$fragment' is not directly under changelog.d/. Towncrier reads a flat directory: a nested entry makes 'towncrier check' fail for every later PR (towncrier tries to parse the subdirectory's name as a fragment). Move it: git mv \"$fragment\" changelog.d/ — contract: changelog.d/README.md"
      fi
      name=$(basename "$fragment")
      case "$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]')" in
        .gitignore | .gitkeep | .keep | readme | readme.md | readme.rst) continue ;;
      esac
      if ! grep -qE '^(\+[^/]+|[0-9]+)\.(added|fixed|changed|removed)(\.[0-9]+)?\.md$' <<< "$name"; then
        fail "Invalid fragment name '$name'. Expected '+<slug>.<category>.md' (mandatory '+' prefix for non-numeric slugs) or '<issue>.<category>.md' with category added|fixed|changed|removed (optional .<seq> suffix). Fix, e.g.: git mv \"$fragment\" changelog.d/+describe-your-change.added.md — contract: changelog.d/README.md"
      fi
    done < <(git ls-tree -r --name-only HEAD -- changelog.d/)

    # 4. `towncrier check` remains the ground-truth validation (it parses
    #    the directory with towncrier's own strict finder against the real
    #    [tool.towncrier] config), but only when the PR ADDS at least one
    #    fragment — otherwise it fails spuriously per the header comment.
    if [ -n "$new_fragments" ]; then
      if ! uvx --from towncrier==25.8.0 towncrier check --compare-with "$BASE"; then
        fail "towncrier check rejected the fragments under changelog.d/ (see its output above). Contract: changelog.d/README.md"
      fi
    else
      echo "No new fragments; skipping 'towncrier check' (it requires one)."
    fi
  else
    echo "No changes under changelog.d/; skipping fragment validation."
  fi

  echo "Feature mode: changelog contract satisfied."
fi
