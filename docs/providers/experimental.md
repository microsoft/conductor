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
│   (claude-agent-sdk>=0.1.0) maintained by @lesandiz (best-effort)   │
│ Limitations: no MCP servers, no per-agent tools allowlist,          │
│   reasoning_effort ignored, no checkpoint resume.                   │
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
| `claude-agent-sdk` | `claude-agent-sdk>=0.1.0` | `@lesandiz (best-effort)` | no `mcp_tools`, no `workflow_tools_passthrough`, no `reasoning_effort`, `prompt_injection` structured output, no `checkpoint_resume` |
| `codex` | `openai-codex==0.1.0b3` | `@microsoft/conductor` | none declared; experimental because the upstream SDK/runtime is beta |

## See also

- `AGENTS.md` — "Provider Parity" section (the rules experimental providers carve out from) and "Experimental Providers" section (rules they must still uphold)
- `src/conductor/providers/capabilities.py` — `ProviderCapabilities` schema
- Issue [#241](https://github.com/microsoft/conductor/issues/241) — design rationale
