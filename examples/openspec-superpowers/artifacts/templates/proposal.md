## Why

<!--
Explain the motivation for this change. What problem does this solve? Why now?

Hard limits: 50 ≤ characters ≤ 1000 (validated by the OpenSpec zod schema)
- Too short: triggers `Why section must be at least 50 characters` error
- Too long: triggers `Why section should not exceed 1000 characters` error

Suggested structure: current pain point → why address it now → expected benefit (1-2 sentences each)
-->

## What Changes

<!--
Describe what will change. Be specific about new capabilities, modifications, or removals.

For behavioral changes with a clear before/after, use the From/To format (markdown has no inline diff):

**<Section or Behavior Name>**
- From: <current state / requirement>
- To: <future state / requirement>
- Reason: <why this change is needed>
- Impact: <breaking / non-breaking, who's affected>

Repeat this block for multiple changes; pure additions or removals can use a simple list.
-->

## Capabilities

### New Capabilities
<!--
Capabilities being introduced. Replace <name> with a kebab-case identifier.
Naming convention: use compound nouns (at least 2 words),
e.g. `user-auth`, `data-export`, `api-rate-limiting` — not single words.
Each creates specs/<name>/spec.md
-->
- `<name>`: <brief description of what this capability covers>

### Modified Capabilities
<!--
Existing capabilities whose REQUIREMENTS are changing (not just implementation).
Only list here if spec-level behavior changes. Each needs a delta spec file.
Use existing spec names from openspec/specs/. Leave empty if no requirement changes.
-->
- `<existing-name>`: <what requirement is changing>

## Impact

<!-- Affected code, APIs, dependencies, systems -->
