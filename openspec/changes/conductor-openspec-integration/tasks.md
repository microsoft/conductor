## 1. Data Model & Schema

- [x] 1.1 Extend workflow schema to support `openspec-pipeline` type (spec: Orchestrate OpenSpec pipeline)
- [x] 1.2 Add artifact references and OpenSpec context fields to workflow schema (spec: Programmatic spec and changeset management)
- [x] 1.3 Implement feature flag for OpenSpec integration (design: Migration Plan)

## 2. OpenSpec API Integration

- [ ] 2.1 Integrate OpenSpec Python APIs for spec, artifact, and changeset operations (design: API Integration)
- [ ] 2.2 Implement CLI fallback for OpenSpec operations if APIs unavailable (design: API Integration)
- [ ] 2.3 Add compatibility/version checks for OpenSpec API (design: Risks)

## 3. Pipeline Orchestration Logic

- [ ] 3.1 Implement `openspec-pipeline` workflow type in engine (spec: Orchestrate OpenSpec pipeline)
- [ ] 3.2 Map OpenSpec stages (create, validate, implement, verify, archive) to workflow steps (spec: Orchestrate OpenSpec pipeline)
- [ ] 3.3 Support async execution and progress reporting for OpenSpec steps (design: Risks)
- [ ] 3.4 Standardize error mapping and enrich event payloads (design: Risks)
- [ ] 3.5 Enforce artifact access via conductor’s authz layer (design: Security)

## 4. CLI & API Extensions

- [ ] 4.1 Add `conductor run --openspec-pipeline` CLI flag (spec: CLI and API interfaces for OpenSpec pipelines)
- [ ] 4.2 Implement API endpoints for pipeline initiation and monitoring (spec: CLI and API interfaces for OpenSpec pipelines)
- [ ] 4.3 Add artifact storage location configuration (design: Open Questions)

## 5. Dashboard & Logging

- [ ] 5.1 Extend event system to recognize OpenSpec pipeline stages (spec: Dashboard and log integration)
- [ ] 5.2 Display OpenSpec status, errors, and results in dashboard (spec: Dashboard and log integration)
- [ ] 5.3 Log OpenSpec pipeline events with relevant details (spec: Dashboard and log integration)

## 6. Testing

- [ ] 6.1 Write unit tests for schema and data model changes
- [ ] 6.2 Write integration tests for OpenSpec pipeline execution (spec: Orchestrate OpenSpec pipeline)
- [ ] 6.3 Test error propagation and recovery scenarios (design: Risks)
- [ ] 6.4 Test CLI and API interfaces for pipeline control (spec: CLI and API interfaces for OpenSpec pipelines)
- [ ] 6.5 Test dashboard and log event surfacing (spec: Dashboard and log integration)

## 7. Documentation & Onboarding

- [ ] 7.1 Update conductor and OpenSpec user documentation for new pipeline features
- [ ] 7.2 Document migration plan and feature flag usage (design: Migration Plan)
- [ ] 7.3 Update onboarding materials for OpenSpec-driven workflows
