## Why

The `examples/openspec-superpowers/` folder contains an extended OpenSpec pipeline that integrates Superpowers skills into the spec-driven change lifecycle. Unlike its sibling `examples/openspec/`, it has no README, leaving new users without guidance on what the pipeline does, how it differs from the base pipeline, or how to run it. Adding a README closes this gap and makes the superpowers pipeline self-documenting and immediately usable.

## What Changes

**New file: `examples/openspec-superpowers/README.md`**
- From: No documentation exists for the superpowers pipeline
- To: A README covering purpose, prerequisites, usage commands, phase walkthrough, skill_directories wiring, and a comparison table with the base OpenSpec pipeline
- Reason: Users need to understand the extended lifecycle (brainstorm → propose → specs → design → tasks → plan → apply → verify → retrospective) and the bundled-skills approach before they can adopt it
- Impact: Non-breaking, documentation only

## Capabilities

### New Capabilities
- `openspec-superpowers-readme`: README documentation for the `examples/openspec-superpowers/` pipeline covering purpose, prerequisites, full usage commands, phase-by-phase walkthrough, comparison with the base OpenSpec pipeline, and notes on `skill_directories`, `interactive_input`, and the `superpowers:` skill namespace

### Modified Capabilities

_(none)_

## Impact

- **Files added**: `examples/openspec-superpowers/README.md`
- **No code changes**: purely documentation
- **No API or dependency changes**
- **Affects**: developers discovering or evaluating the superpowers pipeline for the first time
