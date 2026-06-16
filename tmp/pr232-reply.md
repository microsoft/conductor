Thanks for the review! Pushed 41d18d4 addressing all four inline comments:

**1. `output.py` line 122 + `copilot.py` line 1104 — multi-fence regression**

Went with your suggestion #2 (`re.findall` + per-candidate try-parse) since it has no behavior trade-off. Two-stage strategy:
- First: non-greedy `re.findall` over fenced blocks, try-parse each in order, **first valid wins** — handles the multi-block case from your repro.
- Fallback: greedy single capture — handles the backticks-in-string case (where non-greedy splits the JSON at the inner fence and no individual candidate parses).

Both parsers kept in parity. Added regression tests in both `test_output.py` and `test_copilot.py` that pin first-valid-wins on your exact repro input.

**2. `app.py` line 177 — `_abort_web_bg_if_human_gate` coverage gap (for_each)**

Applied your suggested fix verbatim — the check now walks both `config.agents` and `config.for_each[*].agent`. Parallel groups remain excluded because `config/validator.py:483` (PE-2.7) already rejects `human_gate` there.

**3. `app.py` line 876 — resume coverage gap (checkpoint-only path)**

When the user runs `conductor resume --from <ckpt> --web-bg` without a workflow argument, the gate guard was previously skipped. Now reads `workflow_path` from the checkpoint JSON and runs the same check. Falls through silently if the checkpoint is unreadable so the normal resume path still surfaces the real error.

Regression tests added for both #2 and #3 in `tests/test_cli/test_web_flags.py`.

**Verification:** `pytest tests/test_executor tests/test_cli tests/test_providers -m "not performance"` → 959 passed, 3 skipped (954 prior + 5 new). `ruff check` and `ruff format --check` clean.
