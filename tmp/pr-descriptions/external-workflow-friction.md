# fix: external workflow friction — minimal evidence-anchored fixes

**Branch:** `fix/external-workflow-friction` → `main`
**Type:** Bug fixes (3) + documentation
**Risk:** Low — two narrow bug fixes, one CLI pre-fork validation, one new docs section. No schema or API changes.
**Plan:** [docs/projects/usability-features/external-workflow-friction.plan.md](docs/projects/usability-features/external-workflow-friction.plan.md)
**Brainstorm:** [docs/projects/usability-features/external-workflow-friction.brainstorm.md](docs/projects/usability-features/external-workflow-friction.brainstorm.md)

## Background

A single external contributor running two real-world workflows against Conductor v0.1.16 hit seven failed runs before reaching a successful end-to-end execution. The companion brainstorm identified nine candidate issues. Pre-design code review confirmed **four** of those issues are real, root-caused, and have a clear minimal fix; the remaining five either could not be reproduced from the cited evidence, propose speculative configurability, or address unobserved failure modes.

This PR ships three of the four (the fourth, scripts-inside-parallel validation, was already on main per pre-flight check). Five issues are explicitly deferred in the plan §6 with thresholds-to-reopen.

## Commits

| Commit | Issue | Plan Item |
|---|---|---|
| `505caee fix(parser): use greedy fence regex for JSON with embedded backticks` | Brainstorm #1 | Item 1 (FR-1, FR-2, FR-3) |
| `69f2dd5 fix(cli): abort --web-bg before fork when workflow has human_gate` | Brainstorm #8 | Item 4 (FR-6) |
| `4e5c07b docs: omit-output guidance, --web-bg gate constraint, brainstorm + plan` | Brainstorm #2 + docs gaps | Item 3 (FR-5) + plan/brainstorm in-tree |

## Issue 1: Fence regex strips JSON containing triple-backticks

**Failure mode:** `parse_json_output` (executor/output.py) and `_extract_json` (providers/copilot.py) both used a non-greedy fenced-block regex:

```
r"```(?:json)?\s*\n?(.*?)\n?```"
```

When an agent emitted JSON whose string values contained triple-backtick substrings (common for Markdown- or shell-snippet-bearing payloads), the first inner ` ``` ` closed the match prematurely. The extracted substring was invalid JSON, triggering parse-recovery loops and burning tokens — the exact failure observed in the brainstorm timeline.

**Fix:** switch both call sites to a greedy capture with `re.DOTALL`:

```
r"```(?:json)?\s*\n(.*)\n```"
```

With `DOTALL` the greedy `.*` terminates at the last triple-backtick in the string, which is the correct boundary. The existing first-`{`/`[` heuristic and brace-match fallback remain as final attempts; the canonical `json.loads` at the end is the unchanged failure point for genuinely malformed JSON.

**Test:** `tests/test_executor/test_output.py::test_parse_json_with_triple_backticks_inside_string` fails on `main` and passes after this commit. The malformed-input test is retained as a regression guard for the unchanged error message.

## Issue 4: --web-bg crashes silently when workflow contains human_gate

**Failure mode:** `conductor run --web-bg` (and `resume --web-bg`) forked a detached background process. When the workflow reached a `human_gate` step, `Prompt.ask()` read from the closed stdin and the child crashed with `EOFError`. The parent only saw `"Background process exited immediately with code 1"` — nothing pointed at the actual incompatibility, the `--skip-gates` workaround, or the foreground `--web` alternative.

**Fix:** `_abort_web_bg_if_human_gate()` helper in `cli/app.py` loads the workflow, walks `config.agents`, and if any `type: human_gate` is present (and `--skip-gates` is not set) aborts with this guidance message before `launch_background()` forks anything:

```
--web-bg is incompatible with workflows that contain human_gate steps
because the detached process has no stdin to prompt on.

Options:
  1. Use --web (foreground) instead of --web-bg
  2. Add --skip-gates to auto-accept the first option
  3. Remove human_gate steps from the workflow
  4. Wait for CLI gate-resolution support (planned follow-up)
```

Call sites added to both `run` and `resume` per the run/resume parity rule in `AGENTS.md`.

**Tests:** `tests/test_cli/test_web_flags.py::TestWebBgHumanGateValidation`:

- `test_web_bg_with_human_gate_aborts_before_fork`
- `test_web_bg_with_human_gate_and_skip_gates_proceeds`
- `test_resume_web_bg_with_human_gate_aborts_before_fork`

All three mock `launch_background`, run in-process via `CliRunner`, and produce no subprocess. Deterministic.

## Issue 2 (docs-only): when to declare `output:` vs omit it

**Failure mode:** the external contributor declared `output:` on a synthesizer agent that produced 80 KB of nested JSON. This injected a schema instruction that the model partially complied with, fell into parse-recovery, and burned cost. The brainstorm proposed adding an `output_mode` field; the plan §2 NG1 rejected that as API bloat — the "raw" behavior already exists as "omit `output:`". The fix is docs.

**Change:** new "Choosing whether to declare `output:`" section in `docs/workflow-syntax.md` describing the trade-off in one sentence, the two clear cases (small structured JSON → declare; prose or large JSON → omit and read `<agent>.output.result`), and the YAML for both. Cross-linked from the `output:` reference subsection.

## Additional documentation

- **`docs/cli-reference.md` `--web-bg` section** — now documents the `human_gate` incompatibility and the four supported options matching the new pre-fork validation. Closes a gap noticed while implementing Issue 4.
- **`CHANGELOG.md` [Unreleased]** — entries under `Fixed` and `Documentation` for each change.
- **Plan and brainstorm in-tree at `docs/projects/usability-features/`** — kept so future contributors can answer "why was issue X not done?" without spelunking PR history. The plan §6 table lists each deferred issue with the threshold that would justify reopening it.

## What this PR does NOT do

Plan §2 NG1-NG7 (also §6) enumerate the five brainstorm issues explicitly out of scope:

- **Issue #2 `output_mode` field** — synonym for "absence of `output:`"; docs fix (Item 3) likely sufficient.
- **Issue #3 Windows path normalization** — no Python-only reproduction; likely shell/YAML environmental.
- **Issue #4 env-var regex rewrite** — inspection shows the current regex already accepts colons in defaults; reported failure is likely YAML quoting or PowerShell variable expansion.
- **Issue #5 dashboard keepalive, CLI gate command, dashboard auth, webhooks** — out of scope by user decision; revisit in a follow-up plan.
- **Issue #6 configurable retry budget** — no observed demand.
- **Issue #9 `command:` vs `args:` parity** — author labels anecdotal.

Each entry in the plan table includes a re-open threshold so the next person who hits one of these knows what evidence would justify reopening.

## Verification

- `make check` (ruff + ruff format + ty) — passes.
- `uv run pytest tests/test_executor/test_output.py tests/test_cli/test_web_flags.py -q` — 41 / 41 pass.
- Full suite passes (modulo the 11 pre-existing failures on `main` unrelated to this branch).

## Reviewer guidance

- **Item 1 is the highest-value change** — the brainstorm timeline shows it was the proximate cause of the multi-hour debugging session. Review the regex carefully; the test specifically guards the "string field containing ` ``` `" case that the old regex failed on.
- **Item 4 is pre-fork on purpose** — crashing the child after fork loses observability; failing fast at load is cheaper and clearer. Both `run` and `resume` get the check by necessity (run/resume parity rule in AGENTS.md), not as gold-plating.
- **The docs/plan files are intentionally verbose** — they enumerate explicitly-deferred work with reopen thresholds so this PR doesn't quietly become "the time someone shipped fixes 1, 3, 4 and #5 was never reconsidered."
