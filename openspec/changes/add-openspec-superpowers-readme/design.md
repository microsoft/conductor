## Context

The `examples/openspec-superpowers/` directory houses an extended OpenSpec pipeline that layers Superpowers skills (brainstorming, writing-plans, subagent-driven-development, etc.) on top of the base spec-driven change lifecycle. Its sibling `examples/openspec/` has a README that orients new users; the superpowers variant has none. Without documentation, users cannot determine what the pipeline does, how it differs from the base pipeline, or how to invoke it. This change adds a `README.md` that closes that gap. The change is documentation-only — no code, schema, or workflow YAML is modified.

## Goals / Non-Goals

**Goals:**
- Satisfy the `openspec-superpowers-readme` capability: a self-contained `README.md` in `examples/openspec-superpowers/` covering purpose, prerequisites, usage commands, phase walkthrough, and technical notes.
- Surface the key differentiators of the superpowers pipeline (bundled skills, `skill_directories`, `interactive_input`, `superpowers:` namespace, extended phases) so users can evaluate it without reading the YAML.
- Provide a comparison with `examples/openspec/` so users can choose the right pipeline for their needs.

**Non-Goals:**
- Modifying any workflow YAML, skill files, or source code.
- Documenting the base `examples/openspec/` pipeline (it already has a README).
- Explaining Superpowers internals beyond what is needed to run the pipeline.
- Adding automated tests for documentation content.

## Decisions

### D1: Match the structure of `examples/openspec/README.md`
- **Choice:** Follow the same section order (purpose, prerequisites, usage, phase walkthrough, notes) used in the sibling README.
- **Rationale:** Consistency lowers cognitive overhead for users who already read the base README. It also makes future maintenance predictable — contributors know what sections to expect in any `examples/*/README.md`.
- **Alternatives considered:** A free-form narrative README — rejected because it would be harder to scan and would diverge from the established pattern.

### D2: Include a comparison table (not prose) for pipeline differences
- **Choice:** Use a Markdown table to compare `examples/openspec/` and `examples/openspec-superpowers/` side-by-side.
- **Rationale:** The brainstorm confirmed users need to choose between the two pipelines. A table surfaces the extra phases (brainstorm, plan, verify, retrospective) and the skills integration at a glance, satisfying the *Comparison with base pipeline* scenario in the Purpose requirement.
- **Alternatives considered:** Prose comparison — rejected because a table is faster to scan and directly maps phase-to-phase differences.

### D3: Document all nine phases in order
- **Choice:** Dedicate a subsection to each phase in the sequence: brainstorm → propose → specs → design → tasks → plan → apply → verify → retrospective.
- **Rationale:** The specs explicitly require all nine phases to be present and described in order (*All phases are documented* scenario). A strict ordered walkthrough also mirrors the mental model users carry when executing the pipeline step-by-step.
- **Alternatives considered:** Grouping phases by type (planning, execution, validation) — rejected because it obscures the sequential dependency between phases.

### D4: Explain `skill_directories`, `interactive_input`, and the `superpowers:` namespace in a dedicated Notes section
- **Choice:** Consolidate the three technical explanations (`skill_directories` wiring, `interactive_input` behavior, `superpowers:` namespace prefix) into a single Notes section rather than scattering them across other sections.
- **Rationale:** These are cross-cutting implementation details that apply to multiple phases. Grouping them avoids repetition and gives users a single place to look when they encounter an unfamiliar YAML field. This satisfies three separate spec requirements (`skill_directories wiring explanation`, `interactive_input explanation`, `superpowers skill namespace explanation`).
- **Alternatives considered:** Inline explanations per phase — rejected because the same concepts recur across phases and inline repetition would bloat the walkthrough.

### D5: No Superpowers plugin install required — explain the bundled-skills approach
- **Choice:** State clearly in the prerequisites that no plugin install is needed because skills are bundled under `artifacts/skills/` and wired via `skill_directories`.
- **Rationale:** This is the primary UX advantage of the superpowers pipeline over a raw Superpowers install. Surfacing it in prerequisites prevents users from attempting an unnecessary install and explains the `skill_directories` design choice in a user-facing context.
- **Alternatives considered:** Mentioning plugin install as an alternative setup path — rejected because the pipeline is designed to be self-contained and mentioning an alternative path adds confusion.

## Risks / Trade-offs

[Risk] The README may drift from the actual workflow YAML if phases are added or renamed in future changes → Mitigation: The phase walkthrough section should be reviewed as part of any PR that modifies the superpowers workflow YAML. This is a social/process mitigation; no automated enforcement exists.

[Trade-off] Mirroring the `examples/openspec/README.md` structure means the superpowers README inherits any structural weaknesses of that document → Accepted because consistency across examples outweighs the marginal cost of any structural imperfection, and both READMEs can be improved together in a future change.

[Risk] Usage commands in the README may become stale if CLI flags change → Mitigation: Commands should use the stable `uv run conductor run` invocation pattern documented in AGENTS.md, which is unlikely to change. Flag-level details should be kept minimal.

## Migration Plan

N/A — this change adds a single documentation file and does not involve deployment, database, or API changes. Rollback is trivially `git revert`. Acceptance condition: `examples/openspec-superpowers/README.md` exists, is readable, and satisfies the scenarios in `specs/openspec-superpowers-readme/spec.md`.

## Open Questions

- None. All requirements are well-defined in the specs and the brainstorm decision chain is fully resolved.
