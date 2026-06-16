## Summary

Follow-up to #232. This PR delivers EPICs 1–4 from the external-workflow-friction plan ([`docs/projects/usability-features/external-workflow-friction-v2.plan.md`](docs/projects/usability-features/external-workflow-friction-v2.plan.md)) — improvements that surfaced while running real-world workflows but were out of scope for the minimal evidence-anchored fixes in #232.

Rebased cleanly onto `main` after #232 landed.

## EPIC 1 — Cross-provider parity & `output_mode`

- New schema field `output_mode: raw | envelope` on agent configs. Default `envelope` keeps current behavior; `raw` returns the model's response verbatim (useful for prompts that already constrain the structure).
- Mutual-exclusion check: `output_mode: raw` cannot coexist with an `output:` schema block.
- Field is rejected on non-prompt agent types (`script`, `human_gate`, `workflow`, `wait`, `set`, `terminate`).
- Cross-provider parity fixes: aligned Claude and Copilot providers around `RetryConfig` wiring and corrected test assertions that had drifted between providers.
- Fixed parse-exhaustion retry so the engine reports `is_retryable=False` once all parse-recovery attempts are spent (instead of looping or surfacing a misleading retryable error).
- New tests: `tests/test_config/test_output_mode.py`, `tests/test_providers/test_output_mode.py`.

## EPIC 2 — Configurable `max_parse_recovery_attempts`

- Exposes `max_parse_recovery_attempts` in the YAML `retry:` policy. Previously hard-coded; now per-agent tunable for workflows that legitimately need more (or fewer) recovery rounds when models emit malformed structured output.

## EPIC 3 — `conductor gate-respond` CLI

- New subcommand for resolving `human_gate` agents from the terminal without opening the dashboard. Useful for scripted/CI flows and for the `--web-bg` case where the dashboard is the only other resolver.
- Hardened malformed-JSON handling in the underlying gate API; improved error messages when the dashboard is unreachable or no gate is waiting.
- Docs updated: [`docs/cli-reference.md`](docs/cli-reference.md), [`CHANGELOG.md`](CHANGELOG.md).

## EPIC 4 — Windows path normalization in script executor

- The script executor now normalizes path-shaped values when running on Windows so that `tmp/foo` / `tmp\foo` resolve identically, and forward-slash paths supplied via `args:` or `env:` no longer break downstream tools that expect native separators.

## Test status

- Lint: `uv run ruff check src tests` ✅
- Format: `uv run ruff format --check src tests` ✅
- Tests: `uv run pytest -m "not performance"` — **3192 passed**, 14 skipped. 11 failures are pre-existing on `main` (Windows-only TOML hex-escape and path-separator assertions in `test_registry/test_integration.py` and one `test_event_log` assertion) and reproduce identically on a clean `origin/main` checkout — not introduced by this PR.

## Plan reference

[`docs/projects/usability-features/external-workflow-friction-v2.plan.md`](docs/projects/usability-features/external-workflow-friction-v2.plan.md)
