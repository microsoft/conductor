# Mahler — RFC Design: `retry:` Route Action (Conductor Phase 2)

**Date:** 2026-05-31  
**Author:** Mahler (Conductor Expert)  
**Status:** RFC — not yet implementation  
**Companion:** conductor PR #229 (Phase 1 — `on_error:` routing, DRAFT)  
**Polyphony work item:** AB#3257  
**Upstream RFC issue:** _see Phase C section below_

---

## Phase A — Reconnaissance

### PR #229 status (as of 2026-05-31)

| Field | Value |
|---|---|
| State | **DRAFT** |
| Mergeable | **CONFLICTING** |
| Author | PolyphonyRequiem (Daniel / us) |
| Head | `PolyphonyRequiem/conductor:feature/error-routing` |
| Last commit | `f13791be5919` |
| Reviews | 0 |

The context.py sentinel conflict documented on 2026-05-28 is still the blocker —
`mergeable: CONFLICTING` confirms this. No new review comments since 2026-05-28.

**No retry-related branches or issues exist upstream.** The only partial match in
open issues is #220 (`validator: block with retry-once`) which is a different
concept (output validation retry, not route-level error retry). No one else has
started this work.

**No RFC process exists** in `microsoft/conductor`. There is no `docs/rfcs/` or
`docs/proposals/` directory. The existing RFC mechanism is a DRAFT PR with
"RFC:" or "brainstorm" in the title (see PR #227 "RFC: first-class on_error
routing (brainstorm)"). We will follow that convention: file a new DRAFT issue
as `[RFC]`.

### Current state of retry in PR #229

`retry:` is completely absent from the PR #229 codebase. The PR body explicitly
scopes it out: *"`retry` / `halt` / `propagate` route actions" are "reserved for
Phase 2/3."* The current `RouteDef` in the PR #229 branch has:

```python
class RouteDef(BaseModel):
    to: str                                     # required
    when: str | None = None
    output: dict[str, str] | None = None
    on_error: bool | str | list[str] | None = None
```

The `_handle_leaf_error` → `_evaluate_routes` → `router._evaluate_error` pipeline
iterates error-bucket routes in document order and raises `UnhandledNodeError`
when nothing matches. The call site in `workflow.py` catches that and raises
`UnhandledWorkflowError`. There are no loop points for retry anywhere. **`retry:`
is 100% greenfield.**

The existing `RetryPolicy` on `AgentDef` (`max_attempts`, `backoff`,
`delay_seconds`, `retry_on`) is agent-provider-level only. It cannot be used on
script nodes or to express workflow-level retry logic. It is **not** the same
thing we are designing.

---

## Phase B — Design

### 1. Schema (`config/schema.py`)

#### 1a. New `RetryDef` model

```python
class RetryDef(BaseModel):
    """Per-route retry policy for error recovery.

    When set on an error route (``on_error`` populated), the engine will
    re-execute the *same node* up to ``max`` additional times before
    allowing the route-match loop to continue to the next candidate.

    ``max`` counts **re-runs only** — not the first attempt. So ``max: 3``
    means 4 total executions (1 original + 3 retries).

    Example YAML::

        routes:
          - to: next_step                          # success path
          - on_error: true                         # retry up to 3 times
            retry:
              max: 3
              backoff: exponential
              initial_seconds: 5
          - to: abort_run                          # exhaustion fallback
            on_error: true

    """

    model_config = ConfigDict(extra="forbid")

    max: int = Field(default=3, ge=1, le=10)
    """Number of re-runs (not counting the first attempt). Range: 1–10."""

    backoff: Literal["fixed", "exponential"] = "exponential"
    """
    Delay strategy between retries.

    - ``exponential``: delay doubles each attempt: initial, initial×2,
      initial×4, … (plus jitter).
    - ``fixed``: every delay equals ``initial_seconds`` (plus jitter).
    """

    initial_seconds: float = Field(default=5.0, ge=0.0, le=300.0)
    """Base delay in seconds before attempt 2 (the first retry)."""

    jitter: float = Field(default=0.25, ge=0.0, le=1.0)
    """
    Fractional random jitter applied to each computed delay.

    A delay of ``d`` is sent as ``d * uniform(1 - jitter, 1 + jitter)``.
    Default 0.25 = ±25%. Set to 0.0 to disable jitter entirely.
    """
```

#### 1b. Changes to `RouteDef`

Two changes:
1. `to:` becomes optional (required only when `retry:` is not set).
2. `retry:` field added.

```python
class RouteDef(BaseModel):
    """Definition for a routing rule."""

    model_config = ConfigDict(extra="forbid")

    to: str | None = None
    """Target agent name, '$end', or human gate name.

    Required unless ``retry`` is set. When ``retry`` is set and ``to`` is
    omitted, the route has no explicit next-target after exhaustion — the
    engine's fallthrough to the next matching error route handles dispatch.
    When ``retry`` is set and ``to`` is also set, ``to`` is ignored (the
    engine never follows it; exhaustion falls to the next error route).

    .. note:: Providing ``to`` alongside ``retry`` is a lint warning, not
       an error, to ease migration from routes that previously had both.
    """

    when: str | None = None
    output: dict[str, str] | None = None
    on_error: bool | str | list[str] | None = None
    retry: RetryDef | None = None
    """Per-route retry policy. Only valid when ``on_error`` is set."""

    @model_validator(mode="after")
    def validate_retry_and_to(self) -> "RouteDef":
        if self.retry is not None and self.on_error is None:
            raise ValueError(
                "retry: is only valid on error routes (on_error must be set)"
            )
        if self.retry is None and self.to is None:
            raise ValueError(
                "to: is required unless retry: is set"
            )
        return self
```

#### Design decision — `to:` on retry routes

**Decision: `to:` is optional on retry routes; recommended to omit.**

Rationale: a retry route's job is to *re-run the current node*, not route
somewhere else. Making `to:` optional avoids forcing authors to pick a dummy
target. After exhaustion the engine falls through document-order to the next
matching `on_error:` route (which MUST provide `to:`). The linter warns if
`to:` is present alongside `retry:` to catch copy-paste mistakes.

Alternative considered: `to: $retry` sentinel on a required `to:` field.
Rejected: adds a magic string; harder to explain; "omit `to:` when retrying"
is cleaner.

#### 1c. Validator additions (`config/validator.py`)

1. **Retry-only route lacks a fallback:** if a node has a retry route but no
   subsequent `on_error:` catch-all (or kind match), warn: "retry exhaustion
   may propagate as UnhandledWorkflowError". This is a warning, not an error
   — the pattern is valid (propagate to parent is sometimes desired).

2. **`retry:` on non-error route:** already caught by `model_validator`, but
   the validator should emit a user-friendly lint error rather than a pydantic
   ValidationError.

3. **`retry:` on `type: workflow`, `type: human_gate`:** Currently `on_error:`
   on these is a hard validation error in Phase 1. Phase 2 should lift this
   restriction for `type: workflow` nodes (see §3). `human_gate` retry remains
   a validation error (gates don't fail, they produce output).

---

### 2. Engine Behavior (`engine/workflow.py` + `engine/router.py`)

#### 2a. Where retry lands

Retry lives in `_handle_leaf_error`, specifically inside the call to
`router._evaluate_error`. The router currently iterates error routes and
raises `UnhandledNodeError` on no match. We extend this so that when a
matching error route has `retry:` set, the router returns a special
`RouteResult` that signals "retry requested" rather than a forward-routing
target.

**Option A: Router returns a sentinel `RouteResult`**

```python
@dataclass
class RouteResult:
    target: str           # existing field
    output_transform: ... # existing field
    matched_rule: ...     # existing field
    retry_policy: RetryDef | None = None  # NEW: set when the matched route has retry:
```

The call site in `_handle_leaf_error` checks `route_result.retry_policy`.
If set, it enters the retry loop *before* returning to the outer dispatch
loop.

**Option B: Retry loop inside `_handle_leaf_error`**

The `_handle_leaf_error` method calls `_evaluate_routes` in a loop. On each
failed attempt the node is re-executed (the call site is the main dispatch
loop in `run()`), with a delay computed from the policy.

**Design decision: Option A + a retry loop in `_handle_leaf_error`'s call
site (the main dispatch loop), not in `_handle_leaf_error` itself.** This
keeps `_handle_leaf_error` a pure "normalize + route" step. The retry loop
belongs in the `run()` dispatch body where execution state is already managed.

#### 2b. Retry loop in `run()` (pseudocode)

```python
# In run() main dispatch body, where leaf-error path currently is:

if script_output.error or agent raised:
    envelope = script_output.error  # or agent envelope
    route_result = self._handle_leaf_error(agent, envelope, output_content)
    
    # NEW: handle retry sentinel
    if route_result.retry_policy is not None:
        policy = route_result.retry_policy
        attempt = 0
        while attempt < policy.max:
            attempt += 1
            delay = _compute_delay(policy, attempt)
            await asyncio.sleep(delay)
            
            # expose attempt count to the node
            self.context.set_retry_attempt(agent.name, attempt)
            
            # re-execute the same node
            try:
                retry_output = await self._execute_node(agent)
            except ExecutionError as exc:
                # node failed again — keep looping
                retry_envelope = exc.envelope
                route_result = self._handle_leaf_error(agent, retry_envelope, {})
                if route_result.retry_policy is None:
                    # matched a non-retry error route — stop retrying, follow it
                    break
                # still a retry route (shouldn't happen with well-formed YAML) — keep looping
                continue
            else:
                # node succeeded — exit retry loop, continue on success path
                current_agent_name = self._route_success(agent, retry_output)
                break
        else:
            # exhausted: fall through to the NEXT matching on_error route
            route_result = self._handle_leaf_error_skip_retry(
                agent, retry_envelope, output_content
            )
    
    # follow route_result normally
    current_agent_name = route_result.target
```

`_handle_leaf_error_skip_retry` is a variant that skips the first matching
retry route and evaluates subsequent error routes. Implementation: pass a
`skip_retry: bool` flag to `_evaluate_error` which skips routes with
`retry:` set.

#### 2c. `CONDUCTOR_RETRY_ATTEMPT` env var

During retry attempts, the engine exposes:
- `CONDUCTOR_RETRY_ATTEMPT=N` (1-indexed, 0 = first/original attempt) as an
  env var for script nodes
- `{{ conductor.retry_attempt }}` in template context (an integer, 0 on the
  first run, 1+ on retries)

This allows scripts to log attempt number, use different timeouts, or detect
they are in a retry context for idempotency checks.

#### 2d. Interaction with `on_error:` route ordering

```
Route evaluation order (error path):
  1. Iterate routes in document order, error-bucket only.
  2. First route that (a) kind-matches on_error: AND (b) when: passes:
     a. If route has retry: → enter retry loop for up to max iterations
        - On each retry failure: re-evaluate error routes from the TOP
          but with retry routes excluded from matching (exhaustion flag).
        - On retry success: exit loop, continue on success path.
     b. If route has no retry: → follow to: target immediately.
  3. After retry exhaustion: re-evaluate routes from step 1 with
     retry-exhausted flag set (skip all retry routes).
     The first non-retry match wins. If nothing matches, raise
     UnhandledWorkflowError.
```

**Decision: retry fires BEFORE matching non-retry `on_error:` routes.** The
route's position in the list determines whether it is tried. A retry route
earlier in the list "preempts" a non-retry route later in the list — this
is intentional and follows the existing first-match semantics. After
exhaustion, the retry route is "consumed" and the engine continues to the
next matching route.

#### 2e. Interaction with `raises:` / `internal.script_error`

No change from Phase 1. `internal.script_error` is synthesized on non-zero
exit when the node opts in (`raises:` or any `on_error:` route present).
A retry route on a node that could produce `internal.script_error` does opt
in. On each retry attempt, the engine checks the exit code / envelope as
normal. If attempt N succeeds, `internal.script_error` is NOT synthesized.

#### 2f. Delay computation

```python
import math, random

def _compute_delay(policy: RetryDef, attempt: int) -> float:
    """Compute sleep duration before retry attempt N (1-indexed)."""
    if policy.backoff == "exponential":
        base = policy.initial_seconds * (2 ** (attempt - 1))
    else:  # fixed
        base = policy.initial_seconds
    jitter_factor = random.uniform(1 - policy.jitter, 1 + policy.jitter)
    return max(0.0, base * jitter_factor)
```

`asyncio.sleep` is used (non-blocking). Maximum delay is inherently capped by
the workflow's `timeout_seconds` — if the sleep itself causes timeout, the
normal timeout path fires.

---

### 3. Sub-Workflow Interaction

**Can `retry:` re-run a `type: workflow` node?** **Yes, and it's the right design.**

A `type: workflow` node that fails surfaces as `UnhandledWorkflowError` →
wrapped as `internal.script_error` (Phase 1 behavior). With `retry:`, the
engine re-executes the sub-workflow from its beginning. This is safe for the
polyphony use case: sub-workflows are designed to be re-entrant (they check
existing state before proceeding).

**Phase 2 envelope propagation** (`subworkflow.*` kinds) is **orthogonal** to
`retry:`. `retry:` can be used today with `on_error: true` to catch any
sub-workflow failure. Once Phase 2 propagation lands, `retry:` can be
targeted at specific sub-workflow error kinds:

```yaml
- on_error: ["subworkflow.external.git.push_failed"]  # Phase 2 kinds
  retry: { max: 3, backoff: exponential, initial_seconds: 10 }
- to: abort_run
  on_error: true  # catch everything else
```

**Phase 1 (today, with `on_error:` on workflow nodes blocked):** The validator
currently hard-errors on `on_error:` on `type: workflow` nodes. Phase 2 must
lift this restriction to enable sub-workflow retry.

**Phase 2 ordering:** Sub-workflow retry (`type: workflow` + `retry:`) requires
the validator restriction to be lifted. This is independent of envelope
propagation. We can ship the validator unlock in the same PR as `retry:`.

---

### 4. `for_each` Interaction

**What should retry do when a `for_each` body fails for item N?**

**Decision: per-iteration retry (re-execute the failed item only).**

This is what polyphony's batch dispatch needs — a transient network error on
item 47 of 200 should retry item 47, not restart from item 1.

Implementation: the `for_each` engine already executes each body as an
isolated node graph. When a body raises (or its sub-workflow raises), the
engine currently applies the `failure_mode` policy. With `retry:`:

1. `on_error:` on `for_each` groups is currently a hard validation error
   (Phase 1 restriction). This must be lifted alongside `type: workflow`.
2. The per-iteration error is routed through the same `_handle_leaf_error`
   path as any other node failure.
3. A `retry:` route on the `for_each` node retries **the failed iteration's
   body** only. The iteration count is tracked per-item.
4. `{{ conductor.retry_attempt }}` is scoped to the current iteration.
5. `failure_mode: continue_on_error` + `retry:` on the body: retries the
   item N times; if still failing after exhaustion, the item is marked
   failed and `continue_on_error` resumes the loop. The outer `routes`
   block fires after all iterations complete.

**Open question for Daniel:** Should a `for_each` retry route be placed on
the group node itself, or on the body nodes? Current proposal is on the
group node (consistent with `on_error:` placement). But the body is a
sub-workflow-like scope — this needs harness validation before committing.

---

### 5. Test Plan

New file: `tests/test_engine/test_retry.py`

#### Group 1: Basic retry mechanics

| ID | Test | What it validates |
|---|---|---|
| `test_retry_succeeds_on_attempt_2` | Node fails once, succeeds on second attempt. Assert route to `next_step` taken, no fallback. | Happy path |
| `test_retry_succeeds_on_attempt_N` | Parametrized: N = 1, 2, 3 (max). | Retry up to boundary |
| `test_retry_exhausts_falls_through` | Node fails max+1 times. Assert fallback `on_error: true` route to `abort_run` taken. | Exhaustion fallthrough |
| `test_retry_exhausts_no_fallback_raises` | Node has only retry route, no fallback. Assert `UnhandledWorkflowError` raised on exhaustion. | No-fallback safety |
| `test_retry_max_1` | `max: 1` — one retry only. | Boundary |

#### Group 2: Backoff timing

| ID | Test | What it validates |
|---|---|---|
| `test_retry_exponential_backoff_delays` | Mock `asyncio.sleep`. Assert call count = max, delay sequence matches `initial × 2^(n-1)`. | Exponential math |
| `test_retry_fixed_backoff_delays` | Mock sleep. Assert all delays equal `initial_seconds` (±jitter band). | Fixed strategy |
| `test_retry_jitter_range` | Run 100 delay computations with `jitter=0.25`. Assert all in `[0.75d, 1.25d]`. | Jitter bounds |
| `test_retry_jitter_zero_no_spread` | `jitter=0.0`. Assert all delays exactly equal computed base. | Jitter disable |

#### Group 3: Kind matching + raises:

| ID | Test | What it validates |
|---|---|---|
| `test_retry_kind_specific_match` | `on_error: "external.git.push_failed"` with retry. Node raises that kind. Assert retry fires. | Kind-specific retry |
| `test_retry_kind_no_match_skips_retry` | Node raises `external.ado.rate_limited`; retry route matches `external.git.*`. Assert retry skipped, falls to next route. | Non-matching kind |
| `test_retry_raises_contract_preserved` | Node has `raises: ["external.git.push_failed"]`; raises declared kind during retry attempt. Assert kind forwarded correctly, retry continues. | raises: compat |
| `test_retry_undeclared_kind_wraps` | Node with `raises:` raises undeclared kind during retry. Assert `internal.undeclared_kind` wraps; retry continues on original wrapped kind. | Undeclared kind |

#### Group 4: Script and agent nodes

| ID | Test | What it validates |
|---|---|---|
| `test_retry_script_node_nonzero_exit` | Script exits nonzero, has `on_error: true` with retry. Assert `internal.script_error` synthesized; retry fires. | Script opt-in |
| `test_retry_script_env_var_exposed` | On retry attempt N, assert `CONDUCTOR_RETRY_ATTEMPT=N` is in the script's env. | Env var |
| `test_retry_agent_node_provider_error` | Agent node raises provider error; retry fires up to max. | Agent retry |
| `test_retry_template_context_attempt` | `{{ conductor.retry_attempt }}` evaluates to current attempt count. | Template context |

#### Group 5: Sub-workflow and for_each

| ID | Test | What it validates |
|---|---|---|
| `test_retry_subworkflow_reruns_child` | `type: workflow` node fails; parent has retry route. Assert child re-executed from start. | Sub-workflow retry |
| `test_retry_subworkflow_exhausts_fallback` | Child fails all attempts. Assert parent fallback route taken. | Sub-workflow exhaustion |
| `test_retry_foreach_per_item` | for_each body fails for item 2 only. Assert only item 2 retried; items 1, 3 not re-run. | Per-item retry |

---

## Phase C — Action Taken

**Path chosen: Path 2 — New RFC issue in `microsoft/conductor`.**

Rationale:
- PR #229 explicitly scopes `retry:` out as Phase 2/3. Adding it to the PR
  body would be confusing scope expansion for reviewers.
- PR #229 is CONFLICTING — touching it for a Phase 2 design note adds risk.
- A separate RFC issue is searchable, linkable, and follows the existing
  conductor RFC convention (PR #227 was a "brainstorm" DRAFT).

**Issue filed:** _see filed issue link in summary section below_

Issue title: `[RFC] retry: route action — Phase 2 of error-routing (companion to #229)`

---

## Open Questions for Daniel

1. **`to:` on retry routes:** Proposed: make optional (omit when using retry).
   Validator warns if present alongside `retry:`. Daniel should confirm this
   is acceptable UX — some authors may want to explicitly declare the next
   target even on retry routes.

2. **`for_each` retry placement:** Body node vs. group node? Current proposal
   is group node (parallel with `on_error:` semantics), but it needs harness
   validation. Wagner should weigh in — this is a YAML authoring question.

3. **context.py conflict:** The `retry:` implementation needs PR #229 to merge
   first. The context.py sentinel conflict is still unresolved. Does Daniel
   want Mahler to propose a specific resolution (sentinel pattern), or wait
   for the upstream conductor team to weigh in on PR #229?

4. **`CONDUCTOR_RETRY_ATTEMPT` exposure for agent nodes:** Script nodes get
   an env var. Agent nodes get template context. Are both needed? Can we just
   use `{{ conductor.retry_attempt }}` for both (env var is harder for LLM
   prompts)?

5. **Max retry cap:** Currently proposed at 10 as a hard schema limit. Is this
   too low for any polyphony use case? (The 14 idempotent gates only need 3
   retries; 10 is conservative headroom.)

---

## Blocking Issues

- **PR #229 context.py conflict must resolve before any Phase 2 work ships.**
  This is Daniel's call: propose sentinel pattern to upstream or wait for
  the conductor team.
- **Conductor team must merge PR #229 to `origin/main`** before polyphony
  can use `on_error:` in CI (currently pinned to `@main` which is v0.1.18,
  no `on_error:` support).
- Phase 2 (`retry:`) cannot be implemented until Phase 1 (`on_error:`) is in.
  Implementation sequence: Phase 1 merge → `retry:` RFC approval → Phase 2 PR.
