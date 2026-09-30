# Changelog Fragments (`changelog.d/`)

Conductor uses [towncrier](https://towncrier.readthedocs.io/) to manage changelog entries without merge conflicts. Each pull request adds a small fragment file in this directory instead of editing `CHANGELOG.md` directly. At release time, towncrier compiles these fragments into `CHANGELOG.md`.

---

## Contract (DOs and DON'Ts)

### DO
- **DO** add exactly one fragment file per user-facing change under `changelog.d/`.
- **DO** use one of the four valid categories: `added`, `fixed`, `changed`, or `removed`.
- **DO** write ready Markdown for the fragment body without bullet prefixes or continuation indentation.
- **DO** wrap long lines at ~80 columns manually.

### DON'T
- **DON'T** edit `CHANGELOG.md` in a feature or fix pull request (`CHANGELOG.md` is compiled only during release preparation).
- **DON'T** include a leading bullet (`- ` or `* `) in the fragment body (towncrier adds bullets automatically).
- **DON'T** include issue references like `(#123)` in the fragment text if using an issue-numbered fragment filename (towncrier appends `(#123)` automatically).
- **DON'T** indent continuation lines in the fragment body (towncrier indents continuation lines automatically with 2 spaces).
- **DON'T** rename a fragment after opening a PR (fragment filenames are purely cosmetic once validated).

---

## Naming Rules

Fragment filenames must follow towncrier syntax:

`[name].[category].md`

1. **Numeric name (recommended when issue number is known):**
   - Pattern: `<issue>.<category>.md`
   - Example: `392.added.md`
   - Towncrier automatically appends `(#<issue>)` to the compiled entry, rendering a clickable link on GitHub.

2. **Non-numeric name (when no issue number is available):**
   - Pattern: `+<slug>.<category>.md` (non-numeric filenames **MUST** start with a `+`)
   - Example: `+otel-mcp-spans.added.md`
   - You do not need to look up or wait for a PR number. Use any descriptive slug.

3. **Multiple entries for the same issue or slug:**
   - Pattern: `<issue_or_slug>.<category>.<seq>.md`
   - Example: `450.fixed.1.md`, `450.fixed.2.md`

### Valid Categories
- `added` — New features and capabilities
- `fixed` — Bug fixes
- `changed` — Changes in existing functionality
- `removed` — Removed features or deprecated functionality

---

## Fragment Body Format

- Write plain Markdown describing the change.
- No leading `- `.
- No trailing `(#issue)` if the filename is numeric.
- No manual leading whitespace on wrapped continuation lines.
- Fragment bodies are compiled verbatim into `CHANGELOG.md` at the repository root — write relative links as they should appear from the root (e.g. `docs/workflow-syntax.md`, not `../docs/...`).

### Good Fragment Example
```markdown
**Direct MCP workflow steps (`type: mcp`)**: calls a tool on a
configured `runtime.mcp_servers` stdio server directly without an LLM.
Arguments are rendered recursively with Jinja2 and auto-coerced to
JSON-native types.
```

### Bad Fragment Example (DO NOT DO THIS)
```markdown
- **Direct MCP workflow steps (`type: mcp`)** (#392): calls a tool on a
  configured `runtime.mcp_servers` stdio server directly without an LLM.
```

---

## Copy-Paste Examples

### Example 1: Feature with an issue number
File: `changelog.d/392.added.md`
```markdown
**Direct MCP workflow steps (`type: mcp`)**: calls a tool on a
configured `runtime.mcp_servers` stdio server directly without an LLM.
Arguments are rendered recursively with Jinja2 and auto-coerced to
JSON-native types.
```

### Example 2: Fix with a non-numeric slug (`+slug`)
File: `changelog.d/+pydantic-final-result.fixed.md`
```markdown
**Pydantic AI structured-output agents explicitly require `final_result`** —
the generated output tool now tells models that they must call it before
finishing and that plain-text responses are not accepted.
```

### Example 3: Multiple entries for one issue
Files: `changelog.d/412.fixed.1.md` and `changelog.d/412.changed.2.md`

`changelog.d/412.fixed.1.md`:
```markdown
**Context window metric**: fixed multi-turn token reporting to use
`last_call_input_tokens` rather than cumulative prompt totals.
```

`changelog.d/412.changed.2.md`:
```markdown
**Context bar styling**: updated warning threshold visualization in the
dashboard when context usage exceeds 80%.
```

---

## CI Enforcement

The [`Changelog` workflow](../.github/workflows/changelog.yml) validates pull requests in CI:
- Rejects PRs that modify `CHANGELOG.md` unless exempted by the maintainer-applied `changelog-not-required` label.
- Requires at least one valid fragment in `changelog.d/` unless exempted by the same label or the pull request was authored by Dependabot.
- Validates the filename of every fragment present in the resulting tree, and additionally runs `towncrier check` whenever the PR adds at least one fragment (towncrier exits "No new newsfragments found" when a branch adds none, so a deletion-only change is covered by the filename scan alone).
- The maintainer-applied `changelog-not-required` label provides a full exemption that waives both the fragment requirement and the `CHANGELOG.md` edit prohibition. Dependabot authorship waives only the fragment requirement. If an exempt PR changes `changelog.d/`, the resulting fragment tree is still validated as above. Dependabot is recognized from the pull request author's GitHub login rather than its labels so lockfile-only security updates are covered even when that ecosystem has no matching `dependabot.yml` entry.
- Note that the check is designed for `pull_request` triggers and is not merge-queue-compatible (if a merge queue is ever enabled, this check must be excluded from merge-queue required checks or extended with a separate `merge_group` job).

---

## Release preparation

Release-prep pull requests bump `pyproject.toml` and compile all pending fragments into `CHANGELOG.md`. CI validates the following release-mode contract:
- **Compile fragments:** Run `make changelog-build VERSION=X.Y.Z` to compile pending fragments into `CHANGELOG.md` and consume the fragment files.
- **Single version section:** Exactly one `## [X.Y.Z] - YYYY-MM-DD` section must exist for the new version in `CHANGELOG.md`.
- **Release notes extraction rehearsal:** CI rehearses notes extraction using the base branch's `extract-release-notes.sh`, which requires the new section to sit below the single towncrier marker and contain non-heading content; the release-prep PR passes only if this rehearsal succeeds.
- **Towncrier marker:** The `<!-- towncrier release notes start -->` marker must survive compilation exactly once.
- **No leftover fragments:** No fragment files may remain under `changelog.d/` except `README.md`.
- **Lockfile update:** `uv.lock` must pin `conductor-cli` at the bumped version (run `uv lock`). The comparison is PEP 440-normalized: `0.2.0-beta.1` in `pyproject.toml` is locked as `0.2.0b1` and matches.
- See [`docs/release-checklist.md`](../docs/release-checklist.md) for the complete step-by-step release process.
