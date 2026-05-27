<!--
Delta spec template for a change.

This template demonstrates 4 delta section types — use only what applies:
- ADDED / MODIFIED / REMOVED / RENAMED
File location: openspec/changes/<change-name>/specs/<capability>/spec.md
(`<capability>` must match the openspec/specs/<capability>/ directory name)

Hard format rules (validated by OpenSpec):
- Requirement text MUST contain `SHALL` or `MUST`
- Every Requirement MUST have at least one `#### Scenario:`
- Scenarios MUST use level-4 headings (`####`); level-3 or bullets will fail silently
-->

## ADDED Requirements

<!-- New behavior. List new Requirements to be added to the capability by this change. -->

### Requirement: <!-- requirement name -->
<!-- requirement text — must contain SHALL or MUST -->

#### Scenario: <!-- scenario name -->
- **WHEN** <!-- condition -->
- **THEN** <!-- expected outcome -->

---

## MODIFIED Requirements

<!--
Modifying an existing Requirement. MUST use the exact normalized header from
openspec/specs/<capability>/spec.md (trimmed, case-sensitive match); otherwise
the delta apply during archive will fail because the requirement cannot be found.

MUST include the complete updated content (not just a diff), because OpenSpec
archive applies MODIFIED sections via full-text replacement.
-->

### Requirement: <!-- same header as in the existing spec -->
<!-- complete updated requirement text — must contain SHALL or MUST -->

#### Scenario: <!-- scenario name (may be new or updated) -->
- **WHEN** <!-- condition -->
- **THEN** <!-- expected outcome -->

---

## REMOVED Requirements

<!--
Removing an existing Requirement. MUST include Reason and Migration so reviewers
understand why it is being retired and how existing consumers should adapt.
-->

### Requirement: <!-- header exactly matching the existing spec -->

**Reason**: <!-- why this requirement is being removed -->

**Migration**: <!-- how existing callers / dependents should adapt -->

---

## RENAMED Requirements

<!--
Renaming a Requirement header. Fixed format: FROM / TO using code-fence headers.
If both the name and content are changing, list the name change here under RENAMED
AND write the full updated content under MODIFIED using the NEW header.

Archive apply order: RENAMED → REMOVED → MODIFIED → ADDED
-->

- FROM: `### Requirement: <Old Name>`
- TO: `### Requirement: <New Name>`
