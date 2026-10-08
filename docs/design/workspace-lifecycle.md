# Retained Workspace Lifecycle

Status: **Implemented**
Related: Architecture step 6 of #527 (Containerized script execution)

## Summary

This design introduces the retained workspace lifecycle for Conductor. The lifecycle governs how workspace state persists across step executions, across failures, and across workflow resumption attempts.

Earlier execution backend designs created a single run-scoped workspace volume and destroyed it unconditionally when the run finished. While that model works well for clean, ephemeral runs, it limits debugging and recovery when complex script workloads fail midway. Users had no way to inspect generated files in a failing container volume or resume a workflow against the exact directory tree left by prior steps.

The retained workspace lifecycle solves this problem. Workflows declare retention policies under `workflow.workspace`. When a step fails, Conductor preserves the underlying storage volume based on the configured policy. On resume, the engine acquires a local advisory claim, verifies the workspace identity fail-closed, validates bundle and environment digests through an honest resume gate, and re-attaches the existing volume without clobbering prior changes.

## Motivation and Purpose

Containerized workflows often perform expensive preparation work: compiling code, pulling dependencies, or populating test databases. When a subsequent step fails, throwing away the entire container volume creates several obstacles:

* **Lost debugging forensics**: Developers cannot inspect generated build artifacts, intermediate logs, or core dumps left inside the container filesystem.
* **Costly restart cycles**: Resuming an interrupted workflow from scratch wastes time and compute repeating heavy setup steps that already succeeded.
* **Inconsistent retry semantics**: Without explicit restart policies, users cannot control whether an interrupted command should re-run from scratch or abort fail-closed.

The retained workspace lifecycle addresses these issues by formalizing the storage contract between the orchestration engine and execution backends.

## The Contract

The workspace lifecycle is defined by three core abstractions in `conductor.execution.types`:

1. **`WorkspaceIdentity`**: A serializable, frozen dataclass that proves the existence and ownership of a retained workspace resource. It records the backend name (`backend`), stable lease identifier (`lease_id`), incarnation token (`incarnation`), and optional backend-specific location hint (`location`). It never serializes mutable backend process state.
2. **Verify-Only Fail-Closed `attach_run`**: A runner backend method that accepts a previously recorded `WorkspaceIdentity`. Instead of provisioning a fresh workspace, the backend verifies that the specified volume exists, carries expected Conductor metadata labels, and matches the current run context. If any verification check fails, `attach_run` raises `WorkspaceAttachError` immediately. It never creates a new volume or mutates existing storage during attach.
3. **`retained_workspace` Capability**: A boolean capability flag on `RunnerBackend`. Backends that support persistent multi-run storage (such as `DockerRunnerBackend`) declare `retained_workspace = True`. Backends that execute purely in host temporary directories (such as `LocalRunnerBackend`) declare `retained_workspace = False`.

The execution engine inspects `retained_workspace` during profile resolution. It maps persistence settings per capability, preventing non-supporting backends from receiving retained workspace specifications.

## Promise Boundary

Conductor separates workspace verification into two distinct phases to preserve correctness:

* **Attach Verification**: Occurs during `attach_run` before any step action begins. The backend confirms volume presence, verifies tracking labels, and locks ownership.
* **Staged Verification**: Occurs before the first command executes on that specific backend. Steps executed between engine start and the first backend command are engine-local (such as Jinja2 template rendering or input validation).

Verifying volume identity early prevents half-initialized workflows from executing host logic against missing volumes. Verifying bundle staging right before command execution ensures that container volumes remain consistent even when early engine steps take time to process.

## Marker Protocol v2

To ensure files staged into a Docker volume match the expected run bundle, Conductor uses an explicit marker protocol (`.conductor-staged`).

### Marker Publishing

1. **Tree Copy**: Conductor creates a stopped scratch container mounting the workspace volume. It copies the entire content-addressed bundle tree into `/workspace` using `docker cp`.
2. **Marker Publication**: Only after the tree copy finishes successfully, Conductor publishes `.conductor-staged` into `/workspace` in a separate `docker cp` operation.
3. **Atomicity**: The marker file contains the canonical `bundle_digest` string (`sha256:<hex>`). If copying the bundle tree fails, the marker is never published. If publishing the marker fails, the volume is considered un-staged.

### Marker Probing and Verification

When attaching to an existing volume or running a command on an attached lease:

* **Marker Probe via `cp-out`**: The backend probes `.conductor-staged` by copying the single file out of the scratch container. This is the only copy operation permitted during probe and attach verification.
* **Digest Mismatch Refusal**: If the marker exists but its contents do not match the workflow's required bundle digest, Conductor raises `WorkspaceAttachError` fail-closed. A mismatch is never treated as a missing marker. It indicates storage corruption, bundle tampering, or a foreign write.
* **First-Use Staging**: Staging via two sequential `cp-in` operations (tree copy, then marker publication) is permitted on an attached lease only when the marker is absent and no prior execution has taken place on the volume (`expect_staged = False`).
* **Prior Execution Protection**: If previous commands have already run on the volume and the marker is missing, the backend raises `WorkspaceAttachError`. It never silently re-stages files over live workspace state.
* **Single-Flight Reset**: Volume preparation runs as a single-flight operation per task. If an in-flight preparation fails, the backend clears the cached task so subsequent attempts can retry cleanly.

## YAML Surface

Workflows configure workspace lifecycle rules using two top-level blocks in YAML.

### `workflow.workspace`

Configures run-wide workspace persistence and isolation:

```yaml
workflow:
  name: build-and-test
  workspace:
    mode: shared           # Storage mode: shared (default) or isolated (reserved)
    persistence: on-failure # Persistence: ephemeral (default), durable, on-failure
```

* **`mode`**: Governs whether steps share a single volume. Defaults to `shared`. The value `isolated` is reserved for future per-step sandbox isolation.
* **`persistence`**: Governs volume retention when execution concludes:
  * `ephemeral`: The volume is removed when the workflow run finishes, regardless of outcome. This is the default.
  * `durable`: The volume is retained after the run finishes, whether the workflow succeeds, fails, or is cancelled.
  * `on-failure`: The volume is retained if the workflow fails or is cancelled. If the workflow completes cleanly, the volume is removed.
* **Inheritance Rules**: The root workflow establishes the workspace configuration. Sub-workflows automatically inherit the root workspace policy. If a sub-workflow defines its own `workspace:` block, validation rejects it with an error.

### `restart:`

Configures step restart semantics when resuming from a failure checkpoint:

```yaml
# Workflow-level default:
workflow:
  defaults:
    restart:
      mode: rerun   # rerun (default), fail, or reuse (reserved)

# Per-step override:
agents:
  - name: compile_assets
    type: script
    restart:
      mode: fail    # Fail immediately if resumed while this step was in-flight
```

* **`rerun`**: If the workflow was interrupted while this step was running, re-run the step from the beginning upon resume. This is the default.
* **`fail`**: If the workflow was interrupted while this step was running, fail immediately upon resume. This protects non-idempotent steps (such as publishing a package or charging a card) from double execution.
* **`reuse`**: Reserved for future step-level output reuse.
* **Step Group Scope**: For parallel groups and for-each groups, `restart:` applies to the entire group. Resuming an interrupted group re-runs all group members.

## Checkpoint Contract

Conductor checkpoints preserve full lifecycle context so resumption can verify storage state safely.

### Checkpoint Fields

When saving a checkpoint, the engine includes three lifecycle structures:

1. **`workspace`**: A dictionary containing workspace retention state:
   * `policy`: The configured retention policy string (`"durable"` or `"on-failure"`).
   * `identities`: A dictionary mapping backend names to serialized `WorkspaceIdentity` data (recording `backend`, `lease_id`, `incarnation`, and optional `location`).
   * `executed_backends`: A sorted list of backend names that executed commands during the run.
2. **`resume_contract`**: A dictionary containing cryptographic digests required for honest resume gating:
   * `workflow_digest`: The content-addressed hash of the root workflow document.
   * `environment_name`: The resolved execution environment name.
   * `environment_digest`: The canonical digest of the resolved environment document.
   * `manifest_digest`: The canonical semantic digest of the compiled execution manifest, computed by `manifest_semantic_digest()` in `src/conductor/engine/run_manifest.py`.
   * `bundle_digest`: The content-addressed bundle digest (or `null` if no bundle was built).
3. **`interrupted_step`**: Details about the active step when execution halted:
   * `name`: The name of the in-flight step.
   * `status`: Marked as `"unknown"` because external container effects cannot be proven after interruption.
   * `attempt_id`: The execution attempt identifier if one was active.

### Legacy Compatibility

* Checkpoints generated by older versions lack these lifecycle blocks. When loading a checkpoint, missing lifecycle fields default safely to `None`.
* Serializing checkpoints with `None` values omits the keys entirely. This ensures byte-level parity with older checkpoint formats for workflows that don't use retained workspaces.

## Digest-Gate

When resuming a workflow that specifies retained workspace persistence via `conductor resume`, the engine enforces an honest digest gate before attaching or altering the retained Docker volume.

### Integrity Checks and Boundaries

The digest gate is conditional on retained workspace persistence (`durable` or `on-failure`). When active, the engine checks the checkpoint contract against the current run context:

1. **Manifest and Environment Integrity**: Verifies `workflow_digest`, `environment_name`, `environment_digest`, and `manifest_digest` (computed by `manifest_semantic_digest()` in `src/conductor/engine/run_manifest.py`). If any of these four fields differs between checkpoint and current run context, resume is refused fail-closed with a recorded versus current value mismatch error.
2. **Bundle Digest Integrity**: Compared only when `bundle_digest` was recorded in `resume_contract`. If the checkpoint has `bundle_digest: null` and a bundle is computed for the resumed run, the comparison does not reject execution. When a recorded bundle digest is present and differs from the current bundle digest, resume is refused.
3. **Workspace Policy Match**: Verifies that the checkpoint `workspace.policy` matches the active retention persistence policy.

### Coverage Boundaries

The digest gate provides strong guarantees, but operators should understand its exact boundaries:

* **Guaranteed (when recorded in `resume_contract`)**: Static file contents declared in the workflow bundle (compared only when the checkpoint carries a non-null `bundle_digest`), Jinja2 template closures, included assets, environment profile parameters, container image names, and secret binding references.
* **Not Proven (Dynamic Sub-workflow Closures)**: Sub-workflows resolved dynamically at runtime cannot be proven at compilation time.
* **Not Proven (Floating Container Tags)**: If a profile specifies `image: python:latest` instead of an immutable image digest (`image: python@sha256:...`), the remote registry could change underlying image layers without altering the manifest digest.
* **Not Proven (Mutable Workspace Content)**: Files modified or created by script steps during execution are not hashed. The gate proves that the configuration matches and, when the checkpoint records a non-null `bundle_digest`, that the input bundle matches; it does not prove that container filesystem modifications remain intact.

## Disposition Table

When a workflow run concludes, the backend disposes of workspace storage according to this matrix:

| Configured Policy | Workflow Outcome | Explicit Terminate Hit | Disposition Action |
|---|---|---|---|
| `ephemeral` | Succeeded | Any | Volume removed |
| `ephemeral` | Failed | Any | Volume removed |
| `ephemeral` | Cancelled / Interrupted | Any | Volume removed |
| `on-failure` | Succeeded | No | Volume removed |
| `on-failure` | Succeeded (`status: success`) | Yes | Volume removed |
| `on-failure` | Failed (Error / Crash) | No | **Volume retained** |
| `on-failure` | Failed (`status: failed`) | Yes | Volume removed (author intentional exit) |
| `on-failure` | Cancelled / Interrupted | No | **Volume retained** |
| `durable` | Any outcome | Any | **Volume retained** |

### Safety Rules

* **Run-Scoped Reset**: `RunSpec.workspace_persistence` is re-evaluated per run. A retained volume from a prior run is not automatically kept forever unless subsequent runs also declare retention.
* **Partial-Prepare Safety**: If a run fails during preparation after the volume has been attached, the backend never removes the volume prematurely. The disposition policy evaluates the failure and keeps the volume for inspection.
* **Explicit Terminate Distinction**: When a workflow reaches a `type: terminate` step with `status: failed`, this represents an intentional author-controlled exit rather than an unexpected system crash. On `on-failure` policies, intentional terminations remove the volume.

## Claim Fencing

Retained workspaces persist on the host filesystem or Docker daemon across process boundaries. To prevent two Conductor processes from modifying the same retained workspace simultaneously, the engine applies local-machine claim fencing.

### Fencing Protocol

1. **Advisory Lock**: The engine serializes claim operations using a stable advisory file lock (`.conductor-claim.lock`) located in the Conductor runtime directory.
2. **Owner Token**: Each claim records an owner token containing:
   * Process ID (PID)
   * Claim creation timestamp in UTC (`created_at`)
   * Unique UUID token
3. **Cross-Process Liveness**: Before claiming a workspace, the acquiring process checks whether the current claim holder is still alive. If the holding process died without releasing the claim, the new process reclaims the lock safely.
4. **Windows Bounded-Locking Protocol**: On Windows systems, claim locking uses `msvcrt.locking` with `LK_NBLCK` and `LK_UNLCK` on a one-byte range (`offset=0, length=1`) with a five-second deadline loop, providing reliable cross-process synchronization without external packages. On POSIX systems, it uses `fcntl.flock`. Cross-process PID liveness detection on Windows uses `OpenProcess` via stdlib `ctypes`.

Claim fencing operates strictly within a single host machine. It does not provide distributed synchronization across multiple machines.

## Garbage Collection (GC) via Docker Labels

Retained Docker volumes can accumulate over time if failing runs are not cleaned up manually. Conductor tags all created volumes with tracking labels to simplify automated garbage collection.

### Volume Label Schema

Conductor tags Docker resources with a consistent set of `io.conductor.*` labels:

* `io.conductor.attempt=<attempt_index>`: Recorded on execution containers to track step attempts.
* `io.conductor.attempt_id=<attempt_id>`: Recorded on execution containers when an attempt ID is assigned.
* `io.conductor.incarnation=<token>`: Identifies the specific workspace incarnation.
* `io.conductor.managed=true`: Marks the resource as managed by Conductor.
* `io.conductor.resource=<workspace|scratch|exec>`: Distinguishes between persistent workspace volumes (`workspace`), bundle staging scratch containers (`scratch`), and step command execution containers (`exec`).
* `io.conductor.retention=<ephemeral|on-failure|durable>`: Records the configured retention policy on workspace volumes.
* `io.conductor.run_id=<run_id>`: Identifies the workflow run that created the resource.
* `io.conductor.step=<step_name>`: Recorded on execution containers to identify the active step.
* `io.conductor.workspace=<lease_id>`: Identifies the workspace lease ID on initial volume creation.

### Sample CI Cleanup Job

In continuous integration environments, old retained volumes can be pruned using standard Docker CLI filtering against the real labels and Docker's native volume creation timestamp:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Find all Conductor workspace volumes created more than 24 hours ago
CUTOFF_SECONDS=$(date -d "24 hours ago" +%s 2>/dev/null || date -v -24H +%s)

echo "Scanning for orphaned Conductor workspace volumes..."
docker volume ls \
  --filter "label=io.conductor.managed=true" \
  --filter "label=io.conductor.resource=workspace" \
  --format "{{.Name}}" | while read -r vol; do
  CREATED=$(docker volume inspect "$vol" --format '{{.CreatedAt}}')
  if [ -n "$CREATED" ]; then
    CREATED_SECONDS=$(date -d "$CREATED" +%s 2>/dev/null || date -j -f "%Y-%m-%dT%H:%M:%SZ" "$CREATED" +%s 2>/dev/null || echo 0)
    if [ "$CREATED_SECONDS" -lt "$CUTOFF_SECONDS" ] && [ "$CREATED_SECONDS" -gt 0 ]; then
      echo "Pruning expired workspace volume: $vol (created $CREATED)"
      docker volume rm "$vol" || true
    fi
  fi
done
```

## Limitations

1. **Local Backend Ephemeral-Only**: The `local` execution backend does not support retained workspaces. Workflows configuring `durable` or `on-failure` retention with local execution fail validation with an explicit error.
2. **ACA Unsupported**: Azure Container Apps (ACA) sandboxes do not support volume retention across sessions.
3. **Script Steps Only Until Agent Realm Delivery**: Until the agent-execution-realm delivery, retained workspaces are available only for Docker script steps. LLM agent steps execute outside the container volume.
4. **No Content Revisioning**: Mutable workspace contents are not versioned or snapshotted. Modifications made by script steps directly mutate the volume.
5. **Orphaned Storage on Hard Crashes**: If the host machine powers off or the Conductor process receives an uncatchable `SIGKILL`, `finally` cleanup blocks cannot execute, leaving volumes on disk.
6. **No Cross-Host Exclusivity**: Claim fencing protects against concurrent runs on the same machine. It cannot detect or prevent concurrent access across distinct hosts sharing a remote Docker daemon.
7. **Post-Create Race Detection**: Checking labels after volume creation detects external creation races, but does not prevent another process from attempting creation at the same instant.
