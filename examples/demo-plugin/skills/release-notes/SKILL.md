---
name: release-notes
description: |
  Turn a list of merged changes into user-facing release notes. Triggers: release notes, changelog, what changed, ship notes.
---

# Release notes

Write notes for the people who *use* the software, not the people who wrote it.

## Process

1. Group changes by what a user would notice: new capabilities, fixed
   behaviour, and things that now work differently.
2. Drop anything with no user-visible effect — refactors, test changes,
   dependency bumps that changed nothing observable.
3. Lead each entry with the effect, not the mechanism. "Workflows resume
   after a dashboard stop" beats "added handle_dashboard_stop".

## Delegating

When the notes need a second opinion on tone or padding, hand the draft to
the `demo:editor` subagent rather than revising in place. It is tuned to cut
filler, and asking it is the point of enabling this plugin — the skill and
the subagent ship together.

## Output

A short intro line, then grouped bullets. No version numbers unless you were
given one.
