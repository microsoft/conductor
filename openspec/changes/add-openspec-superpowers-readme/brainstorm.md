# Brainstorm: Add README to examples/openspec-superpowers/

## Background

The `examples/openspec-superpowers/` folder contains an extended OpenSpec pipeline
that integrates Superpowers skills (brainstorming, writing-plans, subagent-driven-development,
etc.) into the spec-driven change lifecycle. Like its sibling `examples/openspec/`, it
needs a README so new users understand what it is and how to run it.

## Decision Chain

**Q1: What should the README cover?**
The README should explain: the full lifecycle (brainstorm → propose → specs → design →
tasks → plan → apply → verify → retrospective), how skills are bundled under
`artifacts/skills/` so no Superpowers plugin install is needed, how the `skill_directories`
feature wires the skills, and how to run the pipeline with examples.

**Q2: Should it explain the difference from examples/openspec/?**
Yes — a comparison table showing the extra phases (brainstorm, plan, verify, retrospective)
and the skills integration would help users choose which pipeline fits their needs.

**Q3: What format?**
Same format as `examples/openspec/README.md`: purpose, prerequisites, usage, phase
walkthrough, and a troubleshooting/notes section.

## Validated Design

Create `examples/openspec-superpowers/README.md` covering:
- What the pipeline is and how it differs from the spec-driven pipeline
- Prerequisites (no plugin install required; Copilot provider)
- Full usage commands (standalone phases + full pipeline run)
- Phase-by-phase walkthrough with descriptions
- Notes on skill_directories, interactive_input, and the superpowers: namespace
