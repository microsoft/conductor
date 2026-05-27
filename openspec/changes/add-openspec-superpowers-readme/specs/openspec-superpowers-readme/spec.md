## ADDED Requirements

### Requirement: README file exists
The `examples/openspec-superpowers/` directory MUST contain a `README.md` file that documents the superpowers pipeline.

#### Scenario: README is present in the directory
- **WHEN** a user navigates to `examples/openspec-superpowers/`
- **THEN** a `README.md` file SHALL be present and readable

---

### Requirement: Purpose section
The README MUST include a section explaining the purpose of the superpowers pipeline and how it differs from the base `examples/openspec/` pipeline.

#### Scenario: User reads purpose section
- **WHEN** a user opens the README
- **THEN** they SHALL find a clear explanation of what the superpowers pipeline does and why it extends the base OpenSpec pipeline

#### Scenario: Comparison with base pipeline
- **WHEN** a user reads the README
- **THEN** they SHALL find a comparison (e.g., table or prose) that highlights the differences between `examples/openspec/` and `examples/openspec-superpowers/`

---

### Requirement: Prerequisites section
The README MUST document all prerequisites required before using the superpowers pipeline.

#### Scenario: User checks prerequisites
- **WHEN** a user reads the prerequisites section
- **THEN** they SHALL find a complete list of tools, configurations, and environment conditions required to run the pipeline

---

### Requirement: Usage commands section
The README MUST provide full, copy-pasteable usage commands for running each phase of the superpowers pipeline.

#### Scenario: User runs a pipeline phase
- **WHEN** a user reads the usage section
- **THEN** they SHALL find complete CLI commands for invoking each pipeline phase without needing to consult external documentation

---

### Requirement: Phase walkthrough section
The README MUST include a phase-by-phase walkthrough of the extended lifecycle: brainstorm → propose → specs → design → tasks → plan → apply → verify → retrospective.

#### Scenario: User follows the phase walkthrough
- **WHEN** a user reads the phase walkthrough section
- **THEN** they SHALL find each phase described in order with its purpose and the skill or command that executes it

#### Scenario: All phases are documented
- **WHEN** a user counts the phases listed in the walkthrough
- **THEN** all nine phases (brainstorm, propose, specs, design, tasks, plan, apply, verify, retrospective) SHALL be present

---

### Requirement: skill_directories wiring explanation
The README MUST explain how `skill_directories` is configured in the superpowers pipeline and how it enables the `superpowers:` skill namespace.

#### Scenario: User wants to understand skill loading
- **WHEN** a user reads the README
- **THEN** they SHALL find an explanation of the `skill_directories` field and how it wires bundled skills into the workflow

---

### Requirement: interactive_input explanation
The README MUST document the role of `interactive_input` in the superpowers pipeline.

#### Scenario: User encounters interactive_input in the YAML
- **WHEN** a user reads the README
- **THEN** they SHALL find an explanation of what `interactive_input` does and when it is triggered during pipeline execution

---

### Requirement: superpowers skill namespace explanation
The README MUST explain the `superpowers:` skill namespace and how skills within it are referenced and invoked.

#### Scenario: User sees a superpowers: skill reference
- **WHEN** a user reads the README
- **THEN** they SHALL find an explanation of the `superpowers:` namespace prefix and how to reference skills within it
