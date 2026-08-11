# Experimental Providers

Conductor ships **stable** providers that uphold every parity rule in
`AGENTS.md`, and **experimental** providers that delegate part of the
agentic loop to an upstream SDK or framework and therefore cannot honor
every rule. This page documents what "experimental" means, what carve-outs
are allowed, and how a provider moves from experimental to stable.

## Why a separate tier?

Every provider declares a `ProviderCapabilities` descriptor (see
`src/conductor/providers/capabilities.py`). `conductor validate`
cross-checks workflow features against those declarations and surfaces
mismatches before runtime. Experimental providers can declare specific
capabilities as `False` without breaking the validator — but their tier
label is visible everywhere they're used so operators are never surprised
by missing features.

When you run a workflow that uses an experimental provider, the CLI
prints a one-time banner per provider:

```text
┌─────────────────────────────────────────────────────────────────────┐
│ ⚠ Experimental provider in use: claude-agent-sdk                    │
│   (claude-agent-sdk>=0.2.82) maintained by @lesandiz (best-effort)  │
│ Limitations: no per-agent tools allowlist, reasoning_effort         │
│   ignored, structured output via prompt injection, no checkpoint    │
│   resume.                                                           │
│ See docs/providers/experimental.md for stability policy.            │
└─────────────────────────────────────────────────────────────────────┘
```

The web dashboard surfaces the same information as an **exp** badge on
every agent node whose resolved provider has `tier: experimental`.

## Allowed carve-outs

An experimental provider MAY declare any of the following capabilities
as `False` / `None`. Each carve-out is surfaced via the banner and the
validator so the operator can plan accordingly.

| Capability | Carve-out meaning |
|---|---|
| `mcp_tools` | Provider does not forward `runtime.mcp_servers`. Workflows that declare MCP servers against this provider fail validation. |
| `workflow_tools_passthrough` | Provider does not honor per-agent `tools:` allowlists. Workflows that declare a non-empty allowlist against this provider fail validation. |
| `streaming_events` | Provider emits events only at completion (not incrementally). |
| `agent_reasoning_events` | Provider does not surface thinking/reasoning content. |
| `reasoning_effort` | Provider has no reasoning-effort concept; an agent declaring `reasoning.effort: <level>` fails validation. |
| `structured_output: "prompt_injection"` | Schema is enforced via prompt injection rather than a native JSON mode. Validation emits a warning (not an error) for experimental providers; stable providers are silent. |
| `interrupt` | Provider does not monitor `interrupt_signal`. Esc/Ctrl+G still aborts at iteration boundaries but cannot return partial output mid-call. |
| `max_session_seconds` | Provider does not enforce a wall-clock session timeout. Agents that set `max_session_seconds` fail validation. |
| `checkpoint_resume` | Provider session state does not survive `conductor resume` (re-runs the agent from scratch). |
| `working_dir` | Provider does not apply the resolved working directory to its session/subprocess cwd. Workflows that set `working_dir` against this provider fail validation. |
| `session_continuity` | Provider does not honor a per-agent `session_key`; every execution starts a fresh session. Agents that set `session_key` fail validation. |

## Non-negotiable rules

Experimental tier does NOT exempt a provider from:

- The `AgentProvider` lifecycle: `validate_connection()`, `execute()`,
  `close()`.
- Returning an `AgentOutput` of the expected shape (even when individual
  fields like `model` or token counts are `None`).
- Raising real exceptions on real failures — no silent error swallowing.
- Declaring **accurate** `ProviderCapabilities`. Lying in the descriptor
  defeats the whole framework. If behavior cannot be honored under all
  conditions, declare the weaker capability value.
- Providing a smoke test (`tests/test_providers/test_<name>.py`) that
  exercises construct + execute paths against a mocked SDK.
- Maintaining `concurrent_safe: true` *or* failing validation when used
  in parallel/for_each groups with `max_concurrent > 1`.

## Promotion criteria: experimental → stable

To prevent the tier from becoming permanent purgatory, every promotion
requires ALL of:

1. Full parity capabilities declared — no carve-outs in active use across
   the test suite.
2. Named maintainer with a track record of responding to issues.
3. ≥6 months of green CI on a real-API integration test (behind a
   pytest marker, run nightly or on release).
4. Upstream is ≥1.0 with a stated stability promise, or is a long-stable
   0.x with no breaking minor releases for ≥6 months.
5. At least one non-trivial workflow in `examples/` that exercises the
   provider end-to-end.
6. AGENTS.md "Experimental Providers" section updated to remove the
   provider from the experimental table.

## Stability disclaimer

The YAML surface area for an experimental provider may change between
minor Conductor releases. Pin Conductor when relying on one.

Optional-dependency extras (`pip install conductor[<provider>]`) isolate
each experimental provider's upstream dependency graph so that
adopting one does not inflate the install surface for others.

## Current experimental providers

| Provider | Upstream pin | Maintainer | Capability carve-outs |
|---|---|---|---|
| `claude-agent-sdk` | `claude-agent-sdk>=0.2.82` | `@lesandiz (best-effort)` | no `workflow_tools_passthrough`, no `reasoning_effort`, `prompt_injection` structured output, no `checkpoint_resume` (agents without a `session_key` carry no session state across a resume). Supports `mcp_tools` as of [#335](https://github.com/microsoft/conductor/issues/335), except that a narrowing per-server `tools:` filter is refused (no SDK equivalent). Supports `working_dir` as of [#348](https://github.com/microsoft/conductor/issues/348); the CLI would load `CLAUDE.md` and `.claude/settings*.json` from that directory, but `setting_sources` is pinned empty as of [#352](https://github.com/microsoft/conductor/issues/352) so ambient instructions, settings, hooks, and skills are not inherited. Declares `session_continuity`: an agent with a `session_key` reuses one Claude session across executions, and the session map survives `conductor resume` — see [Session Continuity](../workflow-syntax.md#session-continuity-session_key). |
| `hermes` | `hermes-agent` | `(community contribution)` | no `mcp_tools`, `prompt_injection` structured output, no `working_dir` |
| `aca` | `azure-identity>=1.19.0` | `(unassigned)` | no `workflow_tools_passthrough` (the wrapped in-container `CopilotProvider` never applies the `tools:` allowlist to the SDK session), no `working_dir` (only the separate, container-relative `sandbox.working_dir` is honored — not the generic host-resolved field), `prompt_injection` structured output (inherits the inner Copilot provider), no `checkpoint_resume` (ephemeral sandbox sessions, no volume mount). Declares `interrupt`/`max_session_seconds` as `True`, but the shipped runner MVP doesn't fully back either yet — see [Known Gaps](./aca.md#known-gaps-runner-mvp). |

## See also

- `AGENTS.md` — "Provider Parity" section (the rules experimental providers carve out from) and "Experimental Providers" section (rules they must still uphold)
- `src/conductor/providers/capabilities.py` — `ProviderCapabilities` schema
- Issue [#241](https://github.com/microsoft/conductor/issues/241) — design rationale
- [`docs/providers/aca.md`](./aca.md) — `aca` provider documentation (issue [#284](https://github.com/microsoft/conductor/issues/284))
