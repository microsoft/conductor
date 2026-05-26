## Context

OpenSpec provides a rigorous, spec-driven workflow for software changes, but conductor currently lacks native support to orchestrate the full OpenSpec lifecycle as a pipeline. Integrating OpenSpec with conductor will allow teams to automate, validate, and manage spec-driven changes end-to-end, increasing reliability and developer velocity. This change impacts conductor’s CLI, engine, dashboard, and its integration with OpenSpec APIs and spec storage. Stakeholders include conductor and OpenSpec maintainers, as well as end users seeking automated, spec-driven workflows.

## Goals / Non-Goals

**Goals:**
- Enable conductor to orchestrate the complete OpenSpec workflow (spec creation, validation, implementation, verification, archiving) as a native pipeline.
- Provide programmatic interfaces for spec and changeset management within conductor workflows.
- Integrate artifact generation, verification, and archiving into conductor pipelines.
- Surface OpenSpec pipeline status, errors, and results in conductor’s dashboard and logs.
- Expose CLI and API interfaces for initiating and monitoring OpenSpec pipelines.

**Non-Goals:**
- Redesigning OpenSpec’s internal APIs or storage mechanisms.
- Supporting non-OpenSpec spec formats.
- Implementing fine-grained access control or multi-tenancy for OpenSpec artifacts (beyond current conductor capabilities).

## Decisions

- **Integration Approach:**
  - *Decision:* Implement an `openspec-pipeline` workflow type in conductor, mapping each OpenSpec stage to a conductor workflow step.
  - *Rationale:* Keeps conductor’s workflow engine as the orchestrator, leveraging existing parallelism, error handling, and dashboard features. Avoids duplicating orchestration logic in OpenSpec.
  - *Alternatives Considered:* Embedding OpenSpec as a sub-process or external service; rejected due to loss of visibility and control in conductor.

- **API Integration:**
  - *Decision:* Use OpenSpec’s Python APIs (or CLI as fallback) for all spec, artifact, and changeset operations.
  - *Rationale:* Ensures tight coupling, better error handling, and richer status reporting. CLI fallback ensures compatibility if APIs are unavailable.

- **Status and Error Surfacing:**
  - *Decision:* Extend conductor’s event system and dashboard to recognize and display OpenSpec pipeline stages, errors, and results.
  - *Rationale:* Provides real-time visibility and traceability for users. Reuses conductor’s existing event/logging infrastructure.

- **Data Model Changes:**
  - *Decision:* Extend workflow schema to support `openspec-pipeline` type, with explicit steps for each OpenSpec stage and artifact references in context.
  - *Rationale:* Keeps pipeline definition declarative and auditable. Allows future extension for custom OpenSpec stages.

- **CLI/API Extensions:**
  - *Decision:* Add new CLI commands/flags (e.g., `conductor run --openspec-pipeline`) and API endpoints for pipeline initiation and monitoring.
  - *Rationale:* Ensures discoverability and ease of use for both CLI and programmatic users.

- **Security:**
  - *Decision:* Reuse conductor’s existing authentication and authorization mechanisms for pipeline initiation and artifact access.
  - *Rationale:* Avoids duplicating security logic; leverages existing controls.

## Risks / Trade-offs

- [Risk] OpenSpec API changes may break integration → *Mitigation:* Version pinning and compatibility checks; CLI fallback.
- [Risk] Increased complexity in conductor’s workflow engine → *Mitigation:* Modularize OpenSpec integration logic; comprehensive tests.
- [Risk] Error propagation between OpenSpec and conductor may be lossy → *Mitigation:* Standardize error mapping and enrich event payloads.
- [Risk] Performance bottlenecks if OpenSpec operations are slow → *Mitigation:* Support async execution and progress reporting in dashboard.
- [Risk] Security exposure if OpenSpec artifacts are not properly protected → *Mitigation:* Enforce artifact access via conductor’s authz layer.

## Migration Plan

1. Implement OpenSpec integration behind a feature flag.
2. Add new workflow schema and CLI/API extensions.
3. Incrementally roll out to early adopters; monitor logs and dashboard for issues.
4. Provide rollback by disabling the feature flag and reverting to previous workflow types.
5. Document new capabilities and update onboarding materials.

## Open Questions

- What is the minimum OpenSpec API version required for stable integration?
- Should artifact storage location be configurable per pipeline?
- How should partial pipeline failures (e.g., artifact verification fails) be surfaced and retried?
- Are there additional security or audit requirements for OpenSpec artifact access?
