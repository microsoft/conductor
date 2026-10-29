# Execution Backends, Bundles, Runner Protocol and Secrets

How executable steps reach a runner backend (local subprocess, Docker, or ACA), how a run's workspace
leases and content-addressed bundle are prepared, the wire contract for remote agent runtimes, and
how environment-document secrets are resolved and redacted. This is the canonical guide for
secret resolution and redaction; the [workflow engine](workflow-engine.md) guide links here.

## When to read

- Editing `src/conductor/execution/` (contract types, `LocalRunnerBackend`,
  `DockerRunnerBackend`, `docker_agent.py`, `docker_paths.py`, `agent_wire.py`),
  `src/conductor/executor/script.py` or `src/conductor/executor/agent.py`.
- Editing `src/conductor/engine/execution_resolution.py`, `src/conductor/engine/aca_execution.py`,
  `src/conductor/engine/run_manifest.py`, `src/conductor/engine/bundle_prep.py` or
  `src/conductor/engine/secrets.py`.
- Editing `src/conductor/bundle/`, `src/conductor/digest.py`, `src/conductor/runner/protocol.py`,
  `src/conductor/runner/bridge.py`, `src/conductor/aca_runner/server.py`,
  `src/conductor/redaction.py` or `src/conductor/config/environment.py`.
- Adding a sink that writes run data (events, checkpoints, logs, terminal records, diagnostics).

## Required invariants

### Execution seam (`src/conductor/execution/`)

- The package is stdlib-only (it may import only the stdlib and `conductor.execution.*`). Bundle
  materialization lives in `src/conductor/engine/bundle_prep.py` precisely so the seam stays
  transport-neutral.
- Outcomes are data-shaped (`src/conductor/execution/types.py::CommandResult`): a non-zero exit is
  `outcome="completed"` with `exit_code` (data for routing), a timeout is `"timed_out"`.
  Cancellation is NEVER an outcome: it propagates as `asyncio.CancelledError`, only after the
  backend has killed and reaped its child.
- `run_command` drains the child with a shielded `communicate()` task
  (`asyncio.wait_for(asyncio.shield(task), timeout)`). Both teardown paths (timeout and outer
  cancellation) MUST go through `_kill_and_reap`, which kills the child, then awaits the
  still-running drain and a shielded `process.wait()`. Never cancel `communicate()` and then
  `wait()`: cancelling pauses the pipe transports and a dead child with full pipe buffers can
  wedge the reap.
- `_kill_and_reap` returns whether it absorbed a racing cancellation; the caller MUST re-raise it
  (or convert a `timed_out` result back into `CancelledError`). Never swallow cancellation into a
  data-shaped outcome. Both `src/conductor/execution/local.py` and
  `src/conductor/execution/docker.py` implement this.
- Docker only, Windows only: `src/conductor/execution/docker.py::_kill_and_reap` first runs a
  best-effort `taskkill /PID <pid> /T /F` bounded by `_WINDOWS_TREE_KILL_TIMEOUT_SECONDS`, because
  a wrapper shim (`docker.cmd` via `cmd.exe`) leaves the workload in a grandchild that holds the
  pipes open. The local backend has no tree kill.
- `ScriptExecutor` (`src/conductor/executor/script.py`) requires the backend's `batch` capability
  at construction and per call. It keeps `import os` / `import shutil` as patch anchors for
  legacy tests; do not remove them as unused.
- The `run_agent` seam on `RunnerBackend` (`src/conductor/execution/backend.py`) executes agent steps
  across backends using `AgentSpec` and `AgentResult` contract types (`src/conductor/execution/types.py`).
  Backends declare capability flags via `RunnerCapabilities.agent` and `RunnerCapabilities.interrupt`.
- `AgentExecutor` (`src/conductor/executor/agent.py`) dispatches agent calls through `RunnerBackend.run_agent`
  across main loop, parallel, for-each, and guidance paths. Remote profiles reject dialog
  until evaluation and conversation turns have a backend contract.
- Wire serialization stays stdlib-only in `src/conductor/execution/agent_wire.py` (`agent_spec_to_wire_body`),
  unwrapping credentials only at transport.
- For Docker agent realms, `src/conductor/execution/docker_agent.py` streams execution over
  `src/conductor/runner/bridge.py` (`conductor.runner.bridge`) without host port exposure, while
  `src/conductor/execution/docker_paths.py` maps declared skill and plugin directories to staged
  `/workspace` locations.
- Verbose output goes through a neutral diagnostics callback; the seam never imports the CLI.

### Docker backend (`src/conductor/execution/docker.py::DockerRunnerBackend`)

- Uses the Docker CLI via subprocess, not an SDK, so the operator's context, credential helpers
  and remote `DOCKER_HOST` keep working. Requires Docker Engine 20.10+ (API 1.41).
- Each run gets one named volume `conductor-ws-<lease_id>`; commands run in short-lived
  containers over it. Containers and volumes carry deterministic `io.conductor.*` labels.
- The bundle is staged with `docker cp -a` under the scratch container's resolved `--user`
  (`_STAGING_COPY_MODE = "archive-to-container-user"`); plain `cp` left uid 65532 unable to
  write. Change this only with a new real-daemon spike.
- Finalization is fail-closed: only resources whose labels (and container name) verify are
  removed; anything unverifiable is left in place with a warning that prints the manual cleanup
  command. Cleanup is shielded and re-raises an absorbed cancellation afterward.
- Retained volumes use marker protocol v2: publish `.conductor-staged` with the bundle digest
  in a separate `docker cp` only after copying the tree. On attached leases, verify the volume
  identity and marker before execution; a missing or mismatched marker after prior execution
  must fail closed, never restage or replace the retained volume.
- Staging symlinks (`_copy_tree_preserving_symlinks`): a link escaping its declared root is an
  `ExecutionSpecError`. The directory bit comes from the lexically resolved target
  (`_resolve_symlink_within_root`), never by following the link, so a Linux-authored target stages
  verbatim on Windows; an unstat-able target is treated as a non-directory.
- Docker agent realms execute in lease-lived runner containers when `runner_image` is configured
  in `DockerProfileOptions` (`docker.runner_image`). Each active call owns a runner; idle runners
  can be reused, and cancellation retires only its runner. Script steps keep using `image`.
- Path mapping in `src/conductor/execution/docker_paths.py` checks that host skill and plugin
  directories exist in the bundle manifest or additional roots, translating host paths to staged
  locations under `/workspace`. Unbundled dependencies raise actionable configuration errors.
- Runner communication uses `docker exec -i` running
  `src/conductor/runner/bridge.py` (`conductor.runner.bridge`), piping stdin and stdout to
  loopback (`127.0.0.1:8080`) without publishing a port. With Docker host networking,
  that loopback address is shared with the host; do not assume network isolation.
- Real-daemon tests are marked `docker_integration`
  (`tests/test_execution/test_docker_integration.py`) and excluded from `make test`; unit tests use
  `tests/test_execution/docker_fake.py`.

### Resolver session and leases (`src/conductor/engine/execution_resolution.py`)

- `ExecutionResolverSession` owns run-shared backend instances (`BACKEND_FACTORIES`: `local`,
  `docker`, and lazy-importing `aca`) and one workspace lease per distinct backend;
  `ExecutionResolver` is the per-engine view (root and each sub-workflow).
- `BACKEND_CAPABILITY_PROVIDERS` exposes static capability descriptors without eagerly importing
  optional cloud or HTTP SDKs.
- `AcaRunnerBackend` (`src/conductor/engine/aca_execution.py`) implements `RunnerBackend` for Azure
  Container Apps dynamic sessions, managing lease-owned salt, slot reservation, and gateway transport
  shared with the legacy compatibility facade.
- Only the root engine prepares leases (in `run()` / `resume()`, mirroring the root-only periodic
  checkpoint gate) and finalizes them in the matching `finally` with
  `succeeded`/`failed`/`cancelled`.
- `ensure_backends` lazily creates a backend first seen in a child workflow and leases it against
  the root run spec, under one lock spanning construction, defensive bundle materialization and
  `prepare_run`, so concurrent for-each children cannot duplicate side effects.
- `finalize_leases` detaches leases from session state BEFORE cleanup (a repeated `run()` must
  prepare fresh leases, never reuse finalized ones), finalizes each backend once, shields each
  `finalize_run` from cancellation, logs (never raises) backend failures so the run outcome is not
  masked, and re-raises cancellation only after every backend finalized.
- Retained workspace policy is passed only to backends declaring `retained_workspace`; technical
  local leases in Docker-backed runs must receive `workspace_persistence=None` in both
  `prepare_leases` and `ensure_backends`. On resume, retained identities go through
  `attach_run`, never a fresh `prepare_run` for the same backend.
- Agent dispatch, including grading and retries, records a retained backend through
  `run_agent(on_dispatch=...)` after staging and handshake, immediately before the
  execute bridge starts. A checkpoint must retain that history even when the agent
  fails, so resume refuses a missing staging marker instead of overwriting its workspace.
- `src/conductor/engine/workspace_claim.py::WorkspaceClaim` fences concurrent resume on the
  local machine. Its advisory lock serializes acquisition and token-checked release; Windows
  locking covers the same fixed one-byte range on both operations.
- An injected `(secret_cache, redactor)` pair must belong together (the cache's redactor); a
  mismatched pair is rejected at construction.

### Run manifest and bundle preparation

- `src/conductor/engine/run_manifest.py::compile_run_manifest` pins every executable step of the
  root config against the environment document into a `ResolvedRunManifest`: the run's audit
  record, run-invariant and byte-deterministic. Step placement is recorded in `ResolvedStepProfile`
  with `realm_image` and `inner_provider` for remote agent steps. In-memory aids are `exclude=True`
  so the dump stays byte-identical.
- Capability checks fail fast during compilation: `_require_script_backend_capability` verifies
  the `batch` capability for script steps. `_require_agent_backend_capability` verifies the
  `agent` capability, checks supported inner providers for remote realms (`copilot`, `openai`,
  `claude`), requires `docker.runner_image` on Docker agent profiles, and validates ACA constraints
  (rejects `sandbox.identifier_scope` on steps, rejects skills/plugins/discovery since ACA sandboxes
  cannot read host paths).
- `script_step_backends` restricts mixed-backend script warnings to actual script steps.
- `prepare_run_bundle` checks every manifest profile for Docker, including agent-only
  workflows, before scanning unresolved sub-workflows and collecting the bundle.
- `src/conductor/engine/bundle_prep.py` scans the statically knowable sub-workflow closure
  offline (`scan_subworkflow_docker_usage`) so a Docker-only child has its bundle before execution
  starts (`prepare_run_bundle`); the session repeats materialization defensively for late-found
  backends. Docker script steps need a workflow file on disk. Every resume prepares a fresh lease
  and reuses or republishes the content-addressed bundle.

### Bundles (`src/conductor/bundle/`)

- Content zone vs provenance: `compute_bundle_digest` covers only version, sorted entries and the
  skills/plugins topologies. Git, registry and plugin provenance and environment links live only
  in the ephemeral `BundleDescriptor`, which is never stored or passed to `publish_bundle`.
  Identical content MUST digest identically regardless of commit, time or machine.
- `src/conductor/bundle/collector.py::collect_bundle` gathers the include graph, Jinja2
  `{% include %}` closures, local and registry sub-workflows, skills, plugins and declared assets
  within authorized roots (`workflow.bundle.additional_roots`). Caps: `MAX_BUNDLE_ENTRIES`
  (10,000) and `MAX_BUNDLE_BYTES` (512 MiB). Failures are typed `BundleError` subclasses
  (`src/conductor/bundle/errors.py`: dynamic template, root escape, symlink escape, caps, cycle,
  unfetched). `conductor validate --environment` calls it with `allow_network=False`.
- Store publication (`src/conductor/bundle/store.py::publish_bundle`) holds a cross-process
  per-digest lock across validation, repair and publication; stages the tree, writes the
  deterministic `bundle.tar.gz`, writes `bundle.json` LAST inside staging as the readiness
  sentinel, then atomically renames into `sha256-<hex>/` (hyphen, not colon, for Windows). Reuse
  is a parse-only sentinel digest check; an invalid directory is warned about, quarantined and
  rebuilt; a lost publish race keeps the valid winner.
- `src/conductor/bundle/archive.py::write_bundle_archive` is byte-reproducible: POSIX-sorted
  members, `uid=gid=0`, empty owner names, `mtime=0` in tar and gzip, modes from the manifest's
  executable set (`tests/test_bundle/test_archive.py::TestDeterminism`).
- `src/conductor/digest.py::canonical_json_digest` (sorted keys, compact separators,
  `ensure_ascii=True`, `sha256:` prefix) is the only canonical digest; use it for environment,
  bundle and manifest digests, never a local variant.
- `src/conductor/cli/bundle.py` imports `conductor.bundle` lazily inside the command; see
  [CLI and runs](cli-and-runs.md).

### Runner wire protocol (`src/conductor/runner/protocol.py`)

- This is the serialized wire contract between the CLI host and a remote agent runtime (protocol version 2,
  `RUNNER_PROTOCOL_VERSION = 2`) that deploys independently; `conductor.execution` is the in-process seam.
  Do not merge them.
- Request-side models (`RunnerAgentRequest`, `RunnerAgentPayload`) are `extra="forbid"`;
  response-side models (`RunnerEventFrame`, `RunnerAgentResult`, `RunnerErrorData`,
  `RunnerHealthResponse`) are `extra="ignore"`. Evolution is additive only; bump
  `RUNNER_PROTOCOL_VERSION` only for an incompatible change.
- Wire v2 adds `execution_id`, `env_overlay`, `skill_directories`, and `custom_agents` to request payloads.
  The `/interrupt` endpoint targets specific invocations by `execution_id` without restarting the runner.
- The Docker interrupt bridge exits 0 for an accepted signal and 3 for HTTP 409;
  other failures remain errors. The host uses these machine outcomes, not stderr
  text, and observes delivery plus one absolute drain deadline through stdout EOF,
  process exit, and stderr completion. Terminal receipt stops new interrupts but
  does not discard an already in-flight delivery outcome.
- Credentials in `inner_provider_settings` are `SecretStr` everywhere except
  `request_to_wire_body`, the one place plaintext is produced.
- In-container execution uses `src/conductor/runner/bridge.py` over `docker exec -i`,
  piping stdin/stdout to the runner loopback address for health checks, targeted interrupts,
  and streaming without a published port. Under `network: host`, that loopback address is
  shared with the host and reachable by other host processes.
- `src/conductor/aca_runner/server.py` is the in-container HTTP runner server. It exposes `GET /health` with
  protocol version advertisement, `POST /execute` streaming NDJSON event frames, and `POST /interrupt` for
  targeted mid-turn pause. It includes a multi-provider factory (`copilot`, `openai`, `claude`), pre-flight
  stdio binary checks (`_check_stdio_binaries`), and per-call `env_overlay` injection applied solely to
  stdio MCP child process spawn environments without leaking into `os.environ`.
- `src/conductor/runner/__init__.py` re-exports nothing; imports go via `from conductor.runner.protocol import ...`.

### Environment documents and secrets

- `src/conductor/config/environment.py`: project documents at `.conductor/environments/<name>.yaml`
  on any ancestor up to the git root, then `$CONDUCTOR_HOME/environments/<name>.yaml`.
  Whole-document shadowing, first match wins: NO merge and NO inheritance. Documents are plain
  YAML with NO environment-variable expansion; secrets are typed references, never values. Import performs no
  I/O.
- Profile definition: `ProfileDefinition` defines `backend`, optional `inherit_control_environment`,
  `docker` (`DockerProfileOptions`), and `aca` (`AcaProfileOptions`). `DockerProfileOptions` accepts
  `image` and optional `runner_image`.
- The environment digest pops `None` optional fields (`secrets`, per-profile
  `inherit_control_environment`, `docker`, `runner_image` under docker, `aca`) so adding an optional field
  never changes existing digests. Do the same for any new optional field.
- `src/conductor/engine/secrets.py::SecretValueCache` is the only run-scoped secret state. It
  enforces each binding's `allow` consumer-class policy on EVERY resolve, including cache hits,
  and registers every value into the run's `RunRedactor`. Errors name the reference, source
  variable and consumer, never the value.
- Remote secrets: agent-scope secrets deliver to remote realms via per-call `env_overlay` for stdio MCP tool
  processes. Names in `RESERVED_AGENT_ENV_NAMES` (`base_url`, `api_key`, `bearer_token`, `github_token`,
  `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.) are rejected by `agent_secret_name_error` to keep MCP delivery
  disjoint from provider credentials. Local agents reject agent-scope secret delivery (`LOCAL_AGENT_SCOPE_ERROR`)
  because local runtimes inherit the control environment.
- `SecretUseIndex` / `index_config` store references and delivery targets only, never plaintext;
  each engine (root and child) gets its own immutable index over one shared cache.
- Static checks (scope placement, delivery transport, collisions, `allow` policies) live in
  `src/conductor/config/validator.py::_validate_secret_references`; Docker and ACA profile checks
  (capability checks, non-script Docker steps without `runner_image`, provider cross-checks allowing copilot,
  openai, and claude on remote realms, reserved environment names, mixed backends, multi-user staging,
  `inherit_control_environment`, `read_only` without tmpfs, `network: host`) in
  `src/conductor/config/validator.py::_validate_docker_profiles`. Because `conductor run` skips the
  validator, the manifest and cache re-check what they depend on.

### Redaction

- `src/conductor/redaction.py::RunRedactor` scrubs recursively without mutating its input
  (str, bytes, dict, list, tuple), replacing exact string and UTF-8 byte matches longest-first
  with `REDACTED_MARKER`. Transformed values (base64, split, hashed) are out of scope. It is
  single-loop, unlocked.
- `AgentSpec.env_overlay` carries plaintext with `repr=False` until transport; the wire
  request wraps overlay and inner-provider credential values in `SecretStr`, and
  `request_to_wire_body` unwraps them only for transmission. Never log either payload.
- Redaction happens before sinks: `src/conductor/events.py::WorkflowEventEmitter.set_redactor`
  scrubs event data before dispatch; checkpoints scrub values but never rename
  `_CHECKPOINT_PROTOCOL_KEYS` (see [workflow engine](workflow-engine.md)).
- The CLI owns the run pair: `src/conductor/cli/run.py::_prepare_run_secrets` creates cache and
  redactor, attaches the redactor to the emitter and indexes the root config before input
  display, MCP construction, guidance, seeding and direct JSONL writes, so a bad reference fails
  early. Results and caught exceptions are scrubbed at the CLI boundary
  (`_scrub_exception_fields`, `_scrub_workflow_terminated`). The context redactor is reset (token
  discipline, never `set_current(None)`) only after final sinks drain.
- Any new sink that writes run data MUST scrub through the current redactor.

## Change checklist

- [ ] No non-stdlib import added to `src/conductor/execution/`.
- [ ] New backend: data-shaped outcomes, cancellation propagated after kill + reap, fail-closed
      finalize, registered in `BACKEND_FACTORIES`, contract tests in `tests/test_execution/`.
- [ ] Agent realm backend implements `run_agent` seam using `AgentSpec` and `AgentResult`,
      declaring `agent` and `interrupt` capability flags.
- [ ] Docker runner changes keep bridge isolation without exposing host ports.
- [ ] Kill/teardown changes keep the shielded drain and absorbed-cancellation re-raise.
- [ ] Bundle content changes keep provenance out of the digest; determinism tests still pass.
- [ ] Store changes keep lock, sentinel-last and atomic rename ordering.
- [ ] Protocol change is additive and keeps request `forbid` / response `ignore`.
- [ ] Targeted interrupt uses execution ID routing over `/interrupt`.
- [ ] Remote secrets enforce reserved name checks and deliver via `env_overlay` to stdio MCP tools only.
- [ ] New optional environment field is popped from the digest when `None`.
- [ ] New sink or serialized artifact scrubs through the run redactor; no secret in messages.
- [ ] Run `uv run pytest tests/test_execution tests/test_bundle tests/test_runner tests/test_aca_runner` plus
      `tests/test_redaction.py`, `tests/test_engine/test_secrets_resolver.py`, and
      `tests/test_engine/test_aca_execution.py`.

## Rationale and evidence

**Shielded drain and kill-and-reap.** Cancelling `communicate()` paused the pipe transports and a
bare kill + `wait()` wedged behind full buffers even after the child died (observed on the
Windows CI fleet, PR #541 review). On Windows the `docker.cmd` shim kept grandchild pipe handles
open, which showed up as 30-second `timed_out` results. Tests:
`tests/test_execution/test_local_backend.py`, `tests/test_execution/test_docker_backend.py`.

**Data-shaped outcomes.** Routing on exit codes requires a non-zero exit to be ordinary data,
while a cancelled run must never look like a completed step.

**Agent realm seam.** Routing agent steps through `run_agent` provides unified lifecycle,
workspace leasing, and secret delivery across local, Docker, and ACA backends. Manifest
compilation validates backend capabilities before run start. Tests:
`tests/test_execution/test_agent_realm_parity.py`, `tests/test_execution/test_docker_agent_realm.py`.

**In-container runner bridge.** Running `conductor.runner.bridge` over `docker exec -i`
communicates with the runner loopback address without exposing container ports to the host network.
Tests: `tests/test_execution/test_runner_bridge.py`.

**Targeted interrupts and wire v2.** Wire protocol version 2 includes `execution_id` so the host
can pause specific executions via `/interrupt` without terminating the container. Tests:
`tests/test_runner/test_protocol.py`, `tests/test_aca_runner/test_server.py`.

**Disjoint MCP secret delivery.** Disallowing provider credential names in `delivery.env`
prevents agent-scope MCP secrets from clashing with provider credentials delivered to remote
runners. Tests: `tests/test_engine/test_agent_realm_mcp_secrets.py`,
`tests/test_engine/test_agent_realm_validation.py`.

**Lease detachment.** Detaching before cleanup prevents a reused engine from finalizing or
reusing a dead handle; shielding finalize keeps a racing cancellation from leaking containers or
volumes. Tests: `tests/test_execution/test_resolver.py`,
`tests/test_execution/test_engine_integration.py`.

**Fail-closed Docker cleanup.** Removing by label alone could delete another run's or a user's
resources; verifying labels and names first and otherwise warning with a manual command keeps
deletion safe.

**Content zone (#527).** Keeping provenance out of the digest makes cache reuse depend only on
content. Writing the sentinel last and renaming atomically means a reader that sees
`bundle.json` can trust the whole directory (same convention as plugin fetch). Tests:
`tests/test_bundle/test_model.py`, `tests/test_bundle/test_store.py`,
`tests/test_bundle/test_collector.py`.

**Protocol strictness.** A strict request side stops a sender smuggling undeclared fields; a
lenient response side lets an older host read a newer runner. `SecretStr` until the wire step
keeps credentials out of reprs and logs (`tests/test_runner/test_protocol.py`).

**No environment-variable expansion and no merging in environment documents.** Expanding host variables or merging
partial documents would reintroduce exactly the machine-dependent ambient state environment
documents exist to make explicit.

**Allow policy on every resolve.** Checking only on first resolution would let a cached value
reach a consumer class its binding forbids (`tests/test_engine/test_secrets_resolver.py`,
`tests/test_engine/test_secrets_wiring.py`, `tests/test_redaction.py`,
`tests/test_engine/test_checkpoint_redaction.py`).

## Related references

- [Docker backend design](../design/docker-backend.md)
- [Agent realms design](../design/agent-realms.md)
- [Run bundle design](../design/run-bundle.md)
- [Runner protocol design](../design/runner-protocol.md)
- [Secret bindings design](../design/secret-bindings.md)
- [ACA provider docs](../providers/aca.md)
- [Workflow syntax: execution profiles](../workflow-syntax.md#execution-profiles),
  [secret bindings](../workflow-syntax.md#secret-bindings)
- [CLI reference: conductor bundle build](../cli-reference.md#conductor-bundle-build)
- [Workflow engine](workflow-engine.md), [CLI and runs](cli-and-runs.md),
  [providers](providers.md) (ACA runner), [testing](testing.md)
