# feat: add cost budget enforcement with audit/enforce modes

**Branch:** `feature/budget-enforcement` → `main`
**Type:** Feature + hygiene cleanup + docs follow-up
**Risk:** Low — additive only; default behavior unchanged (no budget tracking unless `budget_usd` is set).

## Summary

Adds `budget_usd` and `budget_mode` to workflow `limits`, implementing LOA Pattern 2.4 (Token Budget Throttle) as a runtime safety mechanism for agentic workflows that can otherwise burn unbounded cost in loops or recursive sub-workflows.

The graduation path is the headline UX:

1. **No config (default)** — no budget tracking, no overhead, existing behavior unchanged.
2. **`budget_usd` + `audit` mode** — emits `budget_exceeded` event, logs a warning, workflow continues. Use this to discover cost profiles without breaking real workflows.
3. **`budget_usd` + `enforce` mode** — emits event, saves checkpoint, stops the workflow with `BudgetExceededError`. Resumable via `conductor resume` after raising the budget.

## Commits

| Commit | Purpose |
|---|---|
| `6975aaf feat: add cost budget enforcement with audit/enforce modes` | Schema, engine enforcement, exception, tests, configuration.md docs |
| `d3fb868 chore: fix pre-existing ruff errors in budget code` | Hygiene cleanup uncovered while reviewing the feature diff |
| `9bc92f3 docs(budget): document limits.budget_* in workflow-syntax + CHANGELOG` | Doc gap follow-up — the original commit updated configuration.md but missed workflow-syntax.md and CHANGELOG |

## What changed (feature commit)

- **`src/conductor/config/schema.py`** — `LimitsConfig` gains `budget_usd: float | None` (must be ≥ 0) and `budget_mode: Literal["audit", "enforce"]` (default `"audit"`). Pydantic v2 raises `ValidationError` for negative numbers or invalid mode strings.
- **`src/conductor/exceptions.py`** — new `BudgetExceededError` carrying `budget_usd`, `spent_usd`, and `current_agent` for diagnostics and workflow_failed enrichment.
- **`src/conductor/engine/limits.py`** — `LimitEnforcer.check_budget()` with a first-time-overshoot flag so audit mode emits exactly one event per run. `from_dict()` now accepts the new fields for resume parity.
- **`src/conductor/engine/workflow.py`** — `_check_budget()` helper called at all five existing limit-check points alongside `check_timeout()`. Emits `budget_exceeded` event. In enforce mode raises the new exception, which the workflow_failed handler enriches with budget context.
- **`src/conductor/cli/run.py`** — passes budget fields through `LimitEnforcer.from_dict()` on resume so a resumed workflow re-applies the cap from the original config.
- **`docs/configuration.md`** — full feature reference with graduation guidance.
- **`AGENTS.md`** — test fixture patterns + resume/checkpoint parity rules (the latter is generally applicable, not budget-specific, but was learned while wiring the resume path).
- **`tests/test_engine/test_budget.py`** — 21 tests covering schema defaults, validation rejection paths, `LimitEnforcer` unit behavior (zero, audit first-time flag, enforce raising), and all three graduation modes against a `CopilotProvider` with a mocked execute.

## Hygiene cleanup (separate commit)

Lint errors that pre-existed on the branch base or crept in during feature work. Split into its own commit (`d3fb868`) to keep the feature diff reviewable:

- `engine/limits.py`: remove unused `BudgetExceededError` import (F401).
- `engine/workflow.py`: collapse split f-string per `ruff format`.
- `tests/test_engine/test_budget.py`:
  - sort import block (I001),
  - remove unused `UsageTracker` import (F401),
  - narrow two `pytest.raises(Exception)` to `pytest.raises(ValidationError)` for schema validation tests (B017 — pydantic v2 is the precise exception these tests intend to catch),
  - drop two dead `original_execute = provider.execute` bindings (F841).

No behavior change. `make check` is clean on the full branch diff.

## Documentation follow-up (separate commit)

The feature commit updated `docs/configuration.md` but two surfaces were missed:

- **`docs/workflow-syntax.md` "Limits and Safety"** — the canonical syntax reference users skim when authoring workflows. It listed `max_iterations` and `timeout_seconds` but not `budget_usd` / `budget_mode`. Added to both the top-of-file snippet and the expanded section, plus a new "Cost Budget" subsection mirroring the graduation path.
- **`CHANGELOG.md` [Unreleased]** — had no entry for the feature.

Captured in commit `9bc92f3` rather than amending `6975aaf` so the timeline shows the gap was caught and closed, rather than rewriting published history.

## Verification

- `make check` (ruff + ruff format + ty) — passes.
- `uv run pytest tests/test_engine/test_budget.py -q` — 21 / 21 pass.
- Full suite (`uv run pytest`) — passes (modulo the 11 pre-existing failures on `main` unrelated to this branch: registry TOML parse + event-log tests).

## Backwards compatibility

Zero behavior change when `budget_usd` is unset. Existing workflows, tests, and CI pipelines need no modification.

## Reviewer guidance

- The 5 enforcement-point integration in `engine/workflow.py` is the load-bearing piece. Confirm `_check_budget()` is called at every `check_timeout()` site and that no new call sites were added without budget coverage.
- The audit-mode "first-time overshoot only" flag in `LimitEnforcer` is the source of the event-emission contract — if you change it, update `test_audit_mode_emits_event_and_continues` accordingly.
- Resume parity: `LimitEnforcer.from_dict()` accepts budget fields as transient config (sourced from the current workflow YAML at resume time, not the checkpoint). This is intentional per the AGENTS.md "Transient vs persistent" rule — users may want to raise the cap before resuming.
