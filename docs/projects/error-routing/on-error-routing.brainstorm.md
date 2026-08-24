# First-Class `on_error` Routing

**For:** conductor engineering (hi Jason)
**From:** polyphony — a workflow-heavy downstream consumer (15 workflows, 296 nodes)
**Status:** Brainstorm / RFC. Ready for design after a few decisions land.
**Polyphony tracking:** AB#3257 (parent epic: AB#3253)

---

## TL;DR

Teach conductor's `routes:` table about errors.

A node raises a typed error envelope; outgoing routes match on either
success or error; four route actions (`to`, `retry`, `halt`, `propagate`)
cover the full control-flow surface; sub-workflow errors propagate by
default. Replaces the per-author `{success: bool, error: "..."}` +
branch + `human_gate` idiom that workflow authors fall back to today.

```yaml
routes:
  - to: next_node                                  # success path (unchanged)
  - on_error: external.git.drift
    retry: { max: 5, backoff: exponential, initial_seconds: 2 }
  - on_error: external.git.drift
    to: drift_recovery_gate                        # post-exhaustion fallback
  - on_error: provider.exhausted
    halt: { message: "model down: {{ error.message }}" }
  - on_error: "*"
    propagate: true                                # uncaught -> parent workflow
```

Everything in the deep dives below this fold expands one of those lines.

---

## Thesis

> **Routes are conductor's expressive heart. Make them complete.**

Today `routes:` is "where do I go next when this node succeeds?" Promote
it to "where does control flow next, regardless of what happened?" — and
the rest of the failure-handling vocabulary (retry, halt, propagate)
falls into the same table as route actions instead of needing four
separate schema concepts.

A smaller alternative (sibling fields for retry, routes unchanged) is
described in the deep dive; same end-state behavior, two surfaces
instead of one. This brief recommends the unified shape.

---

## What this enables (the payoff)

Polyphony has ~40 `human_gate` nodes across its workflows whose sole
purpose is to absorb script-level failures (`{success: false}` lands
here, human picks retry / skip / abort). They exist because conductor
has no other primitive that can catch a node-level failure.

With this brief shipped:

- Most of those 40 gates collapse to one or two route lines on the
  failing node. The handful that genuinely want a human-in-the-loop
  decision stay, but as targets of an `on_error` route — not as
  hand-rolled discriminator branches.
- Sub-workflow failures stop silently wedging apex runs. Today a child
  workflow halting at one of these gates appears to the parent as
  "didn't return"; with propagation, the parent's routes table sees it.
- Provider outages stop being silent halts. `on_error: provider.exhausted`
  lets workflows degrade gracefully — switch model, wait, escalate.

The point isn't error handling per se. It's that conductor gains *one*
universal control-flow primitive in the place authors already look for
control flow.

---

## Top decisions baked into this proposal

These are the choices the brief makes opinionatedly. Push back on any of
them and the design adjusts cleanly.

1. **`routes:` is the surface, not a new `on_failure:` block.** Errors
   become a matcher on the existing route table (`on_error: <kind>`).
   The alternative — a parallel block like `on_failure: [{when, action}]`
   — is described in the deep dive and explicitly not recommended.

2. **Four route actions, exactly one per entry: `to` / `retry` / `halt` /
   `propagate`.** Today `to:` is implicit; promote it. The other three
   subsume what would otherwise need separate sibling schemas. This is
   the bigger swing; the smaller alternative keeps just `to:` and moves
   retry to a sibling field. Both shapes detailed in the deep dive.

3. **Typed error envelope is the load-bearing primitive.**
   `{conductor_error: true, kind, message, details}` — flat dotted-string
   kinds, equality match only in v1. The script-side contract is
   language-neutral: conductor sets `$CONDUCTOR_ERROR_OUT` to a path; the
   script writes the JSON to that path and exits 0. No new bespoke
   commands required in any engine — three lines in pwsh, bash, python,
   node, or dotnet. Optional helpers shipped per engine for ergonomics.
   Agents emit the envelope as their JSON response. Direct prior art:
   GitHub Actions' `$GITHUB_OUTPUT` env-var-file convention.

4. **Sub-workflow errors propagate by default.** Crossing a `type:
   workflow` boundary accumulates a frame trail; the parent's routes
   table gets a chance to catch with the same `on_error` rules. The
   alternative — halt-on-unhandled in v1, propagation in v2 — was
   considered and rejected: without propagation, polyphony can't delete
   the gates, so the proposal isn't compelling enough to land.

5. **Provider transport exhaustion is a routable kind.** When the
   existing 3× provider retries (`copilot.py:69-91`, `claude.py:90-111`)
   give up, raise `provider.exhausted` instead of halting. Highest-
   leverage routable kind for polyphony; backward-compatible (workflows
   without that route still halt as today).

---

## Critical open questions for you

These are the calls conductor engineering owns. The brief includes a
recommendation for each in the deep dive, but I'd rather not lock them
without your read.

1. **Post-retry-exhaustion routing semantics.** When a `retry` action's
   budget is spent, how does control reach the next handler? Three
   shapes considered:
   - *Implicit:* the next matching error route in document order takes
     over. Reads naturally, but document order becomes load-bearing.
   - *Explicit flag:* error routes get a `when: retry_exhausted`
     predicate (or sibling boolean) to distinguish pre-/post-exhaustion.
   - *Synthetic kind:* exhausted retry re-raises as `retry.exhausted`
     with the original nested under `details.cause`.

   No strong preference from polyphony. Your call.

2. **Route actions (decision 2 above) vs. the smaller alternative.**
   The unified four-action table is the boldest shape and the one that
   makes conductor architecturally complete. The smaller alternative
   (routes keep only `to:`; retry moves to a `retry_on_error:` sibling
   field; default flips from halt to propagate) lands identical
   author-facing behavior with one extra schema field. Pick the
   architecture you want to live with.

3. **Reserved-kind namespace and the relationship to `RetryPolicy`.**
   Proposal reserves `internal.*`, `halt.*`, `precondition.*`,
   `provider.*`, `retry.*`. The existing `RetryPolicy`
   (`schema.py:360`) is transport-level (agent-only, `retry_on:
   provider_error | timeout`); the new route-level `retry` action is
   semantic. Brief keeps them strictly separate — confirm you want it
   that way and not unified.

---

## What I want back from you

Not a yes or no on the whole thing yet. Specifically:

- A read on the three open questions above.
- A read on the thesis: is "routes table as universal control flow" the
  right north star for conductor, or am I projecting?
- If yes-in-principle, your preference on phasing (the brief proposes
  three independently shippable PRs; would happily slice differently).

After that, polyphony can write the typed-kind taxonomy it'll raise,
prep a regression workflow with the legacy pattern for back-compat
testing, and we coordinate on `workflow.version` bumps for any workflow
that adopts the new fields.

---

---

# Deep dives

The rest of this document is the design at full depth. Skim or skip;
the TL;DR + top decisions + open questions above are the parts that
need your eyes.

---

## Context

This brief comes from polyphony's audit of its own 296 nodes across 15
workflows (census in Appendix B). The pattern described emerged from
that audit; whether it belongs in conductor or stays as a polyphony-side
pattern is conductor's call.

Conductor recognizes node failure only in narrow runtime senses:

- `script` node: non-zero exit / unhandled subprocess error (engine-agnostic; conductor spawns subprocesses without knowing the language)
- `agent` node: LLM response violates `output:` schema
- Provider transport: rate-limit / timeout (auto-retried 3× —
  `providers/copilot.py:69-91`, `providers/claude.py:90-111`)
- Workflow: `limits.max_iterations` exceeded (`engine/limits.py:49`)

There is no concept of "the node ran without throwing, but its result is
a semantic failure," and there is no propagation of any failure across a
`type: workflow` boundary. Workflow authors fill the gap with
`{success: false, error: "..."}` returns + a routes-table branch + a
`human_gate` to absorb the failure case. The downstream symptoms:

- Error-recovery gates accumulate (~40 across polyphony's workflows) —
  every script that can fail needs a landing pad and `human_gate` is the
  only available primitive.
- Sub-workflow failures do not surface to the caller. A child workflow
  halting at one of these gates appears to the parent as "didn't return."
  Apex runs wedge silently and require event-log forensics to diagnose.
- The `{success: bool}` discriminator is per-author convention. There is
  no standard shape, no standard place to catch on it, no standard place
  it lands when uncaught.

All symptoms of the same missing primitive: the routes table doesn't
know about errors.

---

## Goal and non-goals

**Goal.** A node — `script`, `agent`, or `workflow` — can raise typed
errors. The routes table matches errors by kind, runs route actions
(`to`, `retry`, `halt`, `propagate`), and propagates uncaught errors
across `workflow:` boundaries with a frame trail. Provider transport
exhaustion surfaces as a routable kind. Existing workflows continue to
work unchanged.

**Non-goals.**

- No new loop or switch primitive. The graph-cycle-with-counter pattern
  stands; separate brief if there's appetite.
- No change to provider-layer transient retries — those already work;
  the brief only adds a routable kind when they're exhausted.
- No change to checkpoint / resume semantics. A failed run cannot be
  resumed; it can only be re-run.
- No removal or restriction of `human_gate`. The primitive stays; this
  brief reduces the workarounds that have been built on top of it.
- No hierarchical kind matching in v1 (flat string equality).
- No JSON-schema versioning of kinds in v1 (start flat strings).
- No `requires:` / preconditions surface — separate brief, parallel work.
- **No inference of kinds from exit codes, stderr patterns, or any other
  runtime signal.** Kinds are author-chosen at the failure site
  (intentional classification). Without intentional authorship, conductor
  surfaces a synthetic `internal.script_error` / `internal.schema_violation`
  and that's all anyone can know.

---

## Design

### D1. Typed error envelope

An error is a structured record with three required fields and one optional:

```json
{
  "conductor_error": true,
  "kind":    "<dotted.lowercase.identifier>",
  "message": "<human-readable>",
  "details": { "arbitrary": "json" }
}
```

`kind` is a flat dotted string for v1 (`external.git.drift`,
`precondition.missing`, `internal.script_error`). Equality match only.

The envelope is exposed in template scope to downstream nodes as
`{{ <node>.error }}` (`kind`, `message`, `details`), parallel to the
existing `{{ <node>.output }}`.

#### D1.1 The script-side contract is engine-agnostic

Conductor's script executor (`executor/script.py`) spawns subprocesses
via `asyncio.create_subprocess_exec` and doesn't know what language
they're written in. The error contract follows the same posture: it's a
file-format + env-var convention that any language can implement in a
few lines.

**The contract, in full:**

1. Before spawning the script, the runtime generates a unique temp file
   path and sets `CONDUCTOR_ERROR_OUT=<that path>` in the child's
   environment. Conductor already passes `env=...` explicitly today
   (`script.py:98-99`), so this is one added line.
2. The script may write the envelope JSON to that path and exit `0`.
3. After the subprocess exits, conductor checks the path:
   - **File present, parses as envelope** → node is treated as errored
     with that kind.
   - **File absent, exit 0** → success (today's behavior).
   - **File absent, exit non-zero** → synthetic
     `internal.script_error`, `message` = stderr tail.

That's the entire contract. No language-specific commands, no stdout
sentinels, no parsing of script output beyond what conductor already
does (capture stdout/stderr as text).

**Direct prior art.** GitHub Actions uses exactly this pattern for its
modern workflow commands: `$GITHUB_OUTPUT`, `$GITHUB_STEP_SUMMARY`,
`$GITHUB_ENV` are all env-var-named file paths the runner sets, and
actions in any language append to them. GHA explicitly migrated *away*
from stdout sentinels (`::set-output::`) toward this shape, citing
stdout pollution, parser fragility, and security. The pattern runs
identically on Windows and Linux runners — the env var is just a postal
address; the OS difference (`CreateProcessW` vs. `execve`) is hidden by
the subprocess layer.

Other relevant prior art surveyed: Azure DevOps Pipelines logging
commands (stdout sentinel; same caveats as old GHA), RFC 7807 Problem
Details (JSON-shape inspiration but HTTP-flavored), JSON-RPC error
object (numeric codes — user-unfriendly), POSIX sysexits.h (only ~15
codes; no message field), systemd `sd_notify` (Linux-only, socket-
based), compiler diagnostic JSON formats (per-tool, not a standard).
None of these fit conductor's shape better than the env-var-file
convention GHA settled on.

#### D1.2 The same envelope, in every engine

Three to five lines in any language. Nothing is required beyond writing
the JSON file. Conductor docs ship these side-by-side:

**pwsh**
```pwsh
'{"conductor_error":true,"kind":"external.git.drift","message":"SHA mismatch"}' |
  Set-Content -Encoding utf8 $env:CONDUCTOR_ERROR_OUT
exit 0
```

**bash / sh**
```bash
cat > "$CONDUCTOR_ERROR_OUT" <<'JSON'
{"conductor_error":true,"kind":"external.git.drift","message":"SHA mismatch"}
JSON
exit 0
```

**python**
```python
import json, os, pathlib
pathlib.Path(os.environ["CONDUCTOR_ERROR_OUT"]).write_text(json.dumps({
    "conductor_error": True,
    "kind": "external.git.drift",
    "message": "SHA mismatch",
}))
```

**node**
```js
require("fs").writeFileSync(process.env.CONDUCTOR_ERROR_OUT, JSON.stringify({
  conductor_error: true, kind: "external.git.drift", message: "SHA mismatch"
}));
```

**dotnet / C#**
```csharp
File.WriteAllText(
    Environment.GetEnvironmentVariable("CONDUCTOR_ERROR_OUT")!,
    JsonSerializer.Serialize(new {
        conductor_error = true,
        kind = "external.git.drift",
        message = "SHA mismatch"
    }));
```

#### D1.3 Optional shipped helpers per engine

To remove even that friction for common engines, conductor ships a
small `helpers/error/` directory with one-file convenience modules.
None are required; they're sugar over the contract above.

| Engine | Helper file | Surface |
|---|---|---|
| pwsh | `Conductor.Error.psm1` | `Write-ConductorError -Kind x.y -Message m [-Details @{...}]` |
| bash / sh | `conductor-error.sh` | `conductor_error x.y "message" '{"k":"v"}'` (sourced) |
| python | `conductor_error.py` | `conductor_error.raise_kind("x.y", "message", details={...})` |
| node | `conductor-error.mjs` | `raiseError({kind: "x.y", message: "m", details: {}})` |
| dotnet | `ConductorError.cs` | `ConductorError.Raise("x.y", "message", new {...})` |

Each helper is 5–15 lines. They exist to make the common path read
naturally; authors who don't want them never see them. New engines
don't need a helper to use the contract — they just write the JSON.

#### D1.4 How agent nodes raise the envelope

Agent nodes emit the envelope as their JSON response. The runtime
detects the `conductor_error: true` discriminator *before* `output:`
schema validation and routes through the error path instead. A helper
shape may be added to `output:` schema validation so authors can declare
both their happy-path schema and the kinds they may raise (see D1.6
below).

#### D1.5 How workflow nodes raise the envelope

A `type: workflow` node raises whatever its child raises. The frame
trail accumulates at the boundary crossing — see D4 for propagation
semantics.

#### D1.6 Where do kinds come from?

The runtime never *infers* a kind. Inferring from exit codes or stderr
patterns would be fragile and create an implicit API that any tool
update could break. Conductor's contract is that classification is
*intentional, at the failure site:*

| Source | Who picks the kind | Result |
|---|---|---|
| Script writes envelope to `$CONDUCTOR_ERROR_OUT` | Script author | `kind` = whatever string the script wrote (runtime trusts verbatim) |
| Agent emits `{conductor_error: true, kind: "..."}` in JSON | Prompt author | Same — runtime trusts the kind string |
| Script exits non-zero without writing the envelope | Nobody | `internal.script_error`, `message` = stderr tail. *"Something failed"* — no semantic info. |
| Agent JSON violates `output:` schema | Nobody | `internal.schema_violation`. Same — pure "something failed." |
| Provider transport retries exhausted | Conductor | `provider.exhausted` (synthetic, runtime-owned — see D5) |
| `limits.max_iterations` hit, timeout | Conductor | Halts. Not routable in v1 (runaway signal). |

If a script silently dies, the route author can match on
`internal.script_error` (or `on_error: true` catch-all) but learns
nothing about *why*. If the script explicitly raised
`external.git.drift`, it's because the author wrote a classification
line at the place they detected the drift. Same contract Python has
with exceptions: nothing infers `FileNotFoundError` for you — `open()`
raises it because someone wrote `raise FileNotFoundError(...)` at the
source.

**How does the route author know what kinds a node raises?** Three
answers, in order of formality:

1. **Convention / docs.** The script's `--help` or its repo docs lists
   the kinds it may raise. Workflow author reads, writes routes. Same
   model as reading a library's exception spec. For polyphony, this
   lives inside the verb taxonomy already maintained.

2. **Catch-all + iterate.** Author starts with `on_error: true,
   to: error_gate`, runs the workflow, sees `error.kind` in the gate
   or event log, splits the catch-all into specific kinds as they
   emerge. Pragmatic; pairs with `errors.jsonl` being grep-friendly.

3. **Optional `raises:` declaration on the node** (see D1.7).

#### D1.7 Optional `raises:` declaration

Nodes may declare the kinds they intend to raise, for self-doc + lint:

```yaml
- name: stamp_facets
  type: script
  command: pwsh
  args: [...]
  raises:                              # OPTIONAL — declared kind contract
    - external.git.drift
    - precondition.missing
  routes:
    - to: next_node
    - on_error: external.git.drift     # lint: must be in `raises` or be `*`/true
      to: drift_recovery
    - on_error: precondition.missing
      to: precondition_gate
```

Two benefits when declared:

- **Lint.** `on_error: <kind>` clauses on the same node's routes must
  reference a kind in `raises:` (or be a catch-all). Catches typos like
  `external.git.drft`.
- **Self-doc.** A workflow author looking at the node sees the contract
  at the top of the YAML, not by archaeology through the script body.

If a node raises a kind not in its declared `raises:`, the runtime
treats it as `internal.undeclared_kind` (still routable via catch-all,
still visible — but flagged as a workflow bug at runtime and in the
event log). Declaration is purely opt-in; nodes without `raises:` work
fine, you just don't get the lint or the runtime check.

#### D1.8 Wrapping third-party CLIs

For tools the workflow author doesn't own (`git`, `gh`, `dotnet test`,
arbitrary vendor CLIs), conductor can't classify failures
automatically — the tool doesn't know about conductor. The author
wraps:

```bash
if ! git fetch origin 2> /tmp/err; then
  cat > "$CONDUCTOR_ERROR_OUT" <<JSON
{"conductor_error":true,"kind":"external.git.fetch_failed",
 "message":"git fetch failed: $(head -1 /tmp/err)",
 "details":{"remote":"origin","exit":$?}}
JSON
  exit 0
fi
```

Or in pwsh:

```pwsh
$out = & git fetch origin 2>&1
if ($LASTEXITCODE -ne 0) {
  Write-ConductorError -Kind external.git.fetch_failed `
    -Message "git fetch failed: $($out | Select-Object -First 1)" `
    -Details @{ remote = "origin"; exit = $LASTEXITCODE }
  exit 0
}
```

The wrapping *is* the classification work — there's no way around it.
Polyphony already wraps things this way (the `{success: bool}` idiom is
exactly this with a less-standard envelope); this proposal just
standardizes the shape.

### D2. `on_error` on `RouteDef`

`RouteDef` gains a single new field, `on_error`, which makes the route
match an error instead of a success:

```yaml
- name: stamp_facets
  type: script
  command: pwsh
  args: [-NoProfile, -Command, "..."]
  routes:
    - to: next_node                                  # success route (unchanged)
    - to: drift_recovery_gate
      on_error: external.git.drift                   # exact kind
    - to: missing_branch_gate
      on_error: precondition.missing
    - to: generic_recovery
      on_error: true                                 # catch-all for any kind
```

Matching rules:

- A route without `on_error` is a **success route**. Evaluated only when
  the node succeeds. First match wins (today's behavior, unchanged).
- A route with `on_error` is an **error route**. Evaluated only when the
  node errors. First match wins.
- Success and error routes are disjoint; they never compete.
- `on_error: <kind>` — exact equality on the raised envelope's `kind`.
- `on_error: [<kind>, <kind>]` — kind list, matches any.
- `on_error: true` — catches any kind. Idiomatic catch-all.
- `when:` predicates still apply on top, with `{{ error.* }}` in scope.
  Lets authors discriminate by `details`:

  ```yaml
  - on_error: external.git.drift
    when: "{{ error.details.branch == 'main' }}"
    to: protected_branch_recovery
  - on_error: external.git.drift
    to: standard_drift_recovery
  ```

**Default behavior when no error route matches:** the run halts with the
unhandled error written to `<run-dir>/errors.jsonl` and a
`workflow.failed` event emitted (see D4). Backward-compatible: workflows
with no `on_error` routes behave identically to today.

### D3. Route actions

Today, every route entry is implicitly a "go to" action via `to:`.
Promote this to an explicit four-action vocabulary. Exactly one action
per route entry; cross-checked at load time.

```yaml
routes:
  - to: success_node                                  # ACTION 1: to
    # default success path, unchanged from today

  - on_error: external.git.drift                      # ACTION 2: retry
    retry: { max: 5, backoff: exponential, initial_seconds: 2 }

  - on_error: external.git.drift                      # ACTION 1 again
    to: drift_recovery_gate
    # post-exhaustion fallback — see "Open decisions" for semantics

  - on_error: provider.exhausted                      # ACTION 3: halt
    halt: { message: "model down: {{ error.message }}" }

  - on_error: "*"                                     # ACTION 4: propagate
    propagate: true
```

| Action | Where it's legal | Semantics |
|---|---|---|
| `to: <target>` | Success or error routes | Today's behavior. Pass control to `target`. Downstream sees `{{ <node>.output }}` (success) or `{{ <node>.error }}` (error). |
| `retry: { max, backoff, initial_seconds }` | Error routes only | Re-run the node up to `max` times. `backoff` ∈ `{ none, linear, exponential }`. Counts against `limits.max_iterations`. On exhaustion, control falls to the next matching error route (see "Open decisions"). |
| `halt: { message }` | Success or error routes | Terminate the run *here* with kind `halt.intentional`. No propagation, no human gate, no resume. The `message` is rendered with `{{ error }}` and `{{ output }}` in scope. |
| `propagate: true` | Error routes only | Re-raise to the parent `workflow:` invocation. Default for any error not matched by an `on_error` route. |

Load-time validation:

- Exactly one of `to` / `retry` / `halt` / `propagate` per route entry.
- `retry` and `propagate` are illegal on success routes.
- `propagate: true` is illegal at the top-level workflow (nothing to
  propagate to); raise a lint error pointing at the use site.

### D4. Sub-workflow propagation

An error raised inside a `type: workflow` invocation propagates up the
call stack exactly like an exception, accumulating a frame trail:

```json
{
  "conductor_error": true,
  "kind": "external.git.drift",
  "message": "feature branch SHA does not match expected lease",
  "details": { "branch": "feature/123", "expected": "abc", "actual": "def" },
  "raised_at":   { "workflow": "feature-pr", "node": "integrate_target_drift" },
  "propagated":  [
    { "workflow": "implement-merge-group", "node": "open_feature_pr" },
    { "workflow": "plan-level",            "node": "do_implementation" },
    { "workflow": "apex-driver",           "node": "build_worklist" }
  ]
}
```

At each `workflow:` boundary, the calling node's routes table gets a
chance to catch the propagated error with the same `on_error` matching
rules as any local error. The frame trail is appended on every boundary
crossing, regardless of whether the parent catches.

An error that reaches the root unhandled:

1. Halts the run with a distinct exit status (reserved for "workflow-
   defined error," separate from "runtime error" and "max-iterations").
2. Writes the error record to a durable per-run location next to the
   manifest (proposed: `<run-dir>/errors.jsonl`, one record per error;
   append-only so multiple unhandled errors in a parallel group are all
   recorded).
3. Emits a final event-log entry of type `workflow.failed` with the
   error record inline so existing event-log consumers see it without a
   separate read.

### D5. Provider exhaustion as a routable kind

After conductor's existing 3× provider-transport retries are exhausted
(`providers/copilot.py:69-91`, `providers/claude.py:90-111`), the agent
currently raises and the run halts. Promote this to a routable error:

```yaml
- name: triage
  type: agent
  # ...
  routes:
    - to: $end
    - on_error: provider.exhausted
      to: degraded_path_gate
      # ^ human picks: switch model, wait, escalate
    - on_error: true
      propagate: true
```

The envelope's `details` carries provider/model/attempt count and the
last underlying error. Authors who don't add an `on_error:
provider.exhausted` route get today's halt behavior — backward-compatible.

This is the highest-leverage routable kind for polyphony specifically;
every long-running apex run today fails the same way (model goes down →
silent wedge) and there is no good place to land that signal.

---

## Smaller alternative (if D3 is too big a swing)

Same end-state behavior, two surfaces instead of one:

- Routes keep only the `to:` action. `on_error` is added to `RouteDef`
  exactly as in D2.
- Retry moves to a sibling node-level field:

  ```yaml
  retry_on_error:
    - when: external.git.drift
      max: 5
      backoff: exponential
      initial_seconds: 2
  ```

  The node runs the retry loop transparently; exhausted retries fall
  through to the routes table.
- `halt` is replaced by routing to a synthetic `$halt` target with a
  templated `output:` field.
- `propagate` becomes the default behavior when no error route matches
  (i.e. the default flips from "halt" to "propagate" once the brief
  lands).

The author-facing behavior is identical; the schema cost is one extra
top-level node field. The architectural cost is that "what happens when
this node errors" is now spread across `routes:` (matching) and
`retry_on_error:` (retry budget) instead of a single table.

This brief recommends D3 (one universal control-flow table). If you
land the smaller alternative, the Phase 2 acceptance criteria translate
cleanly.

---

## Phasing

Land in three independently shippable PRs. Each phase is fully backward-
compatible — existing workflows continue to work unchanged.

### Phase 1 — Envelope + `on_error` routes + halt-on-unhandled

- Script `CONDUCTOR_ERROR_OUT` env-var-file contract (language-neutral,
  per D1.1)
- Agent JSON `conductor_error: true` discriminator
- `RouteDef.on_error` field (true / `<kind>` / `[<kind>]`)
- Optional `raises: [<kind>]` declaration on nodes + lint pass (per D1.7)
- `{{ <node>.error }}` template scope
- Unhandled error at root → `errors.jsonl` + `workflow.failed` event +
  distinct exit code
- Shipped helpers per common engine in `helpers/error/` (pwsh, bash,
  python, node, dotnet — all optional sugar over the contract)

Acceptance:

1. A script in any language that writes a valid envelope JSON to
   `$CONDUCTOR_ERROR_OUT` and exits 0 causes the node to be marked
   errored. Cross-platform tests cover at least pwsh-on-Windows,
   bash-on-Linux, and python on both.
2. An LLM agent emitting `{conductor_error: true, kind: "x.y", message: "m"}`
   does the same; `output:` schema validation does not run on the error
   shape.
3. A route `{ to: gate, on_error: x.y }` is taken when the node raises
   `x.y` and not taken otherwise.
4. A route `{ to: gate, on_error: true }` is taken on any kind.
5. `{{ failing_node.error.kind }}` is in scope on the routed-to node.
6. A `when:` clause on an error route still applies; first match wins.
7. With no matching error route, the run halts with the reserved exit
   code, `errors.jsonl` contains the envelope, and the event log
   includes `workflow.failed`.
8. A script that exits non-zero without writing the envelope still
   works, surfacing kind `internal.script_error` with stderr tail as
   `message`.
9. A node with `raises: [x.y]` and a route `on_error: x.z` fails
   load-time lint (typo / undeclared kind).
10. A node with `raises: [x.y]` that actually raises `x.z` at runtime
    surfaces as `internal.undeclared_kind` and is logged.

### Phase 2 — Sub-workflow propagation + route actions

- Schema additions: `retry:`, `halt:`, `propagate:` on `RouteDef`;
  load-time validation that exactly one action per entry.
- `propagate` becomes the implicit default for any error route-match miss.
- Propagated errors append to `propagated[]` frame trail at every
  `workflow:` boundary crossing.
- Calling workflow's routes table sees propagated errors with the same
  `on_error` matching rules.
- Retry counter integrates with `limits.max_iterations` (a retry is a
  node execution).

Acceptance:

1. `retry: { max: 3, backoff: exponential, initial_seconds: 1 }` re-runs
   the node up to 3 times on the matched kind, with delays 1s, 2s, 4s.
2. `retry.max` exhaustion routes to the next matching error route (or
   halts if none).
3. `halt: { message: "..." }` terminates the run with kind
   `halt.intentional`, nesting the original error under `details.cause`,
   and renders `message` with `{{ error }}` in scope.
4. `propagate: true` re-raises to the parent `workflow:` invocation;
   `propagated[]` gains a frame for the parent's call site.
5. A 3-deep nested `workflow:` invocation with no `on_error` routes
   anywhere surfaces the deepest error at the root with `propagated[]`
   containing two intermediate frames in bottom-up order.
6. A `propagate` action at the top-level workflow is a load-time error.
7. Existing workflows with no `on_error` routes and no `propagate`
   actions continue to halt-on-error exactly as today.

### Phase 3 — Provider exhaustion as routable kind

- Provider transport-retry exhaustion (existing 3× behavior) raises kind
  `provider.exhausted` with `details: { provider, model, attempts,
  last_error }`.
- Wired identically in `providers/copilot.py` and `providers/claude.py`
  per the provider-parity rule.
- Routable like any other kind; default unhandled behavior unchanged
  (halt → propagate per Phase 2).

Acceptance:

1. After 3 transport-retry attempts fail in copilot provider, the agent
   raises envelope with `kind: "provider.exhausted"`.
2. Same behavior in claude provider; identical `details` shape.
3. A route `{ on_error: provider.exhausted, to: degraded_gate }` is taken.
4. With no such route, behavior matches today (halt with a now-routable
   kind in `errors.jsonl`).

---

## Touch points

File references below come from polyphony's read of conductor mechanics
docs and may have drifted; engineering agent should confirm against
current source.

- `config/schema.py:89` — `RouteDef`; add `on_error: bool | str |
  list[str]` and (Phase 2) `retry`, `halt`, `propagate` action fields
  with exactly-one-of validation.
- `config/schema.py` (`AgentDef`) — add optional `raises: list[str]`
  field on nodes for declared kind contract + lint (per D1.7).
- `config/schema.py:347` — `HooksConfig.on_error` (workflow-level
  lifecycle hook) is *unrelated* and stays; document the distinction so
  authors don't conflate them.
- `config/schema.py:360` — `RetryPolicy` is transport-level (agent-only,
  `retry_on: provider_error | timeout`). Stays as-is. The new route-
  level `retry` action is semantic, not transport, and lives only in
  routes.
- `config/validator.py` — cross-validate that `on_error: <kind>` values
  on a node are either in that node's declared `raises:` or are
  catch-alls; that exactly one action exists per route entry; that
  reserved-prefix kinds aren't user-declared.
- `engine/router.py` — split route evaluation into success-bucket and
  error-bucket; first-match within each.
- `engine/workflow.py:563-676` — sub-workflow invocation; propagation
  crosses here (Phase 2).
- `engine/workflow.py:629-639` — input coercion path; envelope coercion
  is parallel work.
- `engine/limits.py:49` — retry-as-iteration accounting.
- `engine/checkpoint.py:100-114, 117-128` — failed runs should not be
  resumable; checkpoint behavior on failure needs explicit decision
  (see all open decisions below).
- `executor/agent.py` — detect `conductor_error: true` discriminator
  before `output:` schema validation.
- `executor/script.py:98-105` — runtime allocates a temp file path
  (`tempfile.mkstemp()` with `delete=False` so Windows file-locking
  doesn't bite; conductor reads only after subprocess exit so there's
  no read/write overlap), sets `CONDUCTOR_ERROR_OUT` in the spawned
  env, reads the file after `await process.communicate()`, deletes it,
  coerces to envelope. Helpers ship in a new top-level `helpers/error/`
  directory (per D1.3); not auto-loaded, no PATH manipulation.
- `providers/copilot.py:69-91, 281-340` and
  `providers/claude.py:90-111, 529-555` — Phase 3 surfaces transport
  exhaustion as `provider.exhausted`; verify both providers handle
  identically per the provider-parity rule in `AGENTS.md`.

---

## Test plan

Each phase ships with:

- **Unit tests** at the points the brief calls out per phase (envelope
  round-trip, route bucket splitting, retry counting, propagation frame
  trail accumulation, etc.).
- **Workflow integration tests** in `tests/test_integration/` using
  minimal multi-node YAMLs that exercise the propagation paths end-to-
  end. At least one case per route action.
- **Cross-engine script tests** — confirm the `CONDUCTOR_ERROR_OUT`
  contract works identically across pwsh, bash, and python on at least
  Windows and Linux. Same workflow YAML, same envelope shape, three
  language implementations of the writing script.
- **A regression workflow** that uses the legacy `{success: false}` +
  routes + gate pattern to prove backward compatibility.
- **A parallel-group test** showing per-node error routes compose under
  the group's `failure_mode` (a child node recovering its own error
  means the group never sees a failure at all).

---

## All open decisions (full list)

The three critical ones are surfaced at the top of the brief. Including
the complete list here so nothing is buried.

1. **Post-retry-exhaustion routing semantics** *(critical)*.
   Implicit-by-order vs. explicit `when: retry_exhausted` predicate vs.
   synthetic `retry.exhausted` kind. Brief assumes implicit ordering;
   polyphony has no strong preference.

2. **D3 (route actions) vs. the smaller alternative** *(critical)*.
   Unified four-action table is the recommendation. Smaller alternative
   lands identical behavior with one extra schema field.

3. **Reserved kind namespace** *(critical)*. `internal.*`, `halt.*`,
   `precondition.*`, `provider.*`, `retry.*` are reserved by conductor;
   document and lint-enforce that user-declared kinds may not use these
   prefixes.

4. **Exit code reserved for workflow-defined error.** Pick a code
   distinct from existing runtime-error and max-iterations codes.
   Document in the same place existing exit codes are documented.

5. **Backoff base.** Phase 2 spec says `initial_seconds` and `backoff:
   exponential`. Confirm whether multiplier is fixed at 2 or configurable
   in v1 (recommend fixed at 2; matches existing `RetryPolicy`
   convention).

6. **`errors.jsonl` format.** One JSON object per line is the proposal.
   Confirm against any existing conductor log conventions.

7. **Checkpoint behavior on failure.** Recommendation: delete or
   mark-invalid the checkpoint on `workflow.failed`. Document the choice;
   this is a small back-compat issue for any tooling that consumes
   checkpoints.

8. **Parallel-group error aggregation.** When a `parallel:` block has
   multiple children error, which one propagates? Recommendation:
   first-errored wins for propagation; *all* error records still
   appended to `errors.jsonl`. Confirm against existing `failure_mode:`
   semantics on `parallel:` / `for_each:` blocks.

---

## Open questions for the brief author (polyphony)

Surface back with answers before Phase 1 lands:

1. **Migration shape on the polyphony side.** Confirm the ~40
   error-recovery gates polyphony proposes to remove can be reproduced
   in a fixture workflow for the regression test.
2. **Specific `kind` taxonomy polyphony wants.** Concretely: what's the
   initial list of `kind`s polyphony's scripts will raise? (Influences
   the helper docs and any kind-naming guidance the brief publishes.)
3. **Version bump coordination.** Polyphony's `workflow.version` field
   is bumped in batch (per repo convention). Coordinate the version
   bump for any workflow that adopts `on_error` routes.

---

## Appendix A: how polyphony's gates collapse

The current `{success: bool} + branch + human_gate` idiom looks like:

```yaml
- name: stamp_facets
  type: script
  command: pwsh
  args: [-NoProfile, -Command, "..."]
  # script always exits 0; emits {success: bool, error: "..."}
  output:
    success: bool
    error:   string?
  routes:
    - to: stamp_facets_error_gate
      when: "not {{ stamp_facets.output.success }}"
    - to: next_node

- name: stamp_facets_error_gate
  type: human_gate
  prompt: |
    `stamp_facets` failed: {{ stamp_facets.output.error }}
    [retry] [skip] [abort]
  routes:
    - to: stamp_facets
      when: "{{ stamp_facets_error_gate.choice == 'retry' }}"
    - to: next_node
      when: "{{ stamp_facets_error_gate.choice == 'skip' }}"
    - to: $end
```

Two nodes, an output discriminator, and a hand-rolled branch. The
equivalent under this proposal:

```yaml
- name: stamp_facets
  type: script
  command: pwsh
  args: [-NoProfile, -Command, "..."]
  routes:
    - to: next_node
    - on_error: external.git.drift
      retry: { max: 3, backoff: exponential, initial_seconds: 2 }
    - on_error: true
      to: stamp_facets_error_gate

- name: stamp_facets_error_gate
  type: human_gate
  prompt: |
    `stamp_facets` failed: {{ stamp_facets.error.message }}
    Kind: {{ stamp_facets.error.kind }}
    [retry] [skip] [abort]
  routes:
    - to: stamp_facets
      when: "{{ stamp_facets_error_gate.choice == 'retry' }}"
    - to: next_node
      when: "{{ stamp_facets_error_gate.choice == 'skip' }}"
    - to: $end
```

The gate node stays (polyphony genuinely wants a human in the loop on
some kinds); the *bespoke* discriminator and branch are gone. Many of
the 40 gates have no real "human decides" value — they exist purely as
landing pads — and those collapse to a single `on_error: true,
to: $end` line or to a `propagate: true` action.

---

## Appendix B: polyphony data referenced above

| Workflow | agent | script | gate | wf | for_each | total |
|---|---:|---:|---:|---:|---:|---:|
| plan-level | 3 | 41 | 26 | 4 | 1 | 75 |
| implement-merge-group | 3 | 31 | 9 | 0 | 0 | 43 |
| apex-driver | 0 | 23 | 7 | 1 | 1 | 32 |
| ado-pr | 3 | 19 | 7 | 0 | 0 | 29 |
| github-pr | 4 | 16 | 5 | 0 | 0 | 25 |
| feature-pr | 3 | 13 | 5 | 3 | 0 | 24 |
| actionable | 2 | 9 | 4 | 0 | 0 | 15 |
| apex-item-dispatch | 0 | 10 | 0 | 4 | 0 | 14 |
| reset-apex | 0 | 7 | 1 | 0 | 0 | 8 |
| research | 4 | 2 | 1 | 0 | 0 | 7 |
| apex-wave-dispatch | 0 | 5 | 0 | 0 | 1 | 6 |
| root-fallback-gate | 0 | 5 | 1 | 0 | 0 | 6 |
| cascade-remedy | 0 | 2 | 2 | 0 | 1 | 5 |
| remedy-stale-descendant | 0 | 3 | 2 | 0 | 0 | 5 |
| close-out | 1 | 1 | 0 | 0 | 0 | 2 |
| **Total** | **23** | **187** | **70** | **12** | **4** | **296** |

Of the 70 `human_gate` nodes, ~40 (name-pattern `*_error`, `*_failure`,
`*_gate` in the recovery sense) currently absorb script-level failures —
the use case this brief lets polyphony express differently. The
remaining ~30 are approval, scope-decision, and external-state-
acquisition gates. Polyphony's plans for either set are out of scope for
this brief.

Generated 2026-05-20 from `.conductor/registry/workflows/*.yaml`.
