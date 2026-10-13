# Execution Backends, Bundles, Runner Protocol and Secrets

How executable steps reach a runner backend (local subprocess or Docker), how a run's workspace
leases and content-addressed bundle are prepared, the wire contract for remote agent runtimes, and
how environment-document secrets are resolved and redacted. This is the canonical guide for
secret resolution and redaction; the [workflow engine](workflow-engine.md) guide links here.

## When to read

- Editing `src/conductor/execution/` (contract types, `LocalRunnerBackend`,
  `DockerRunnerBackend`) or `src/conductor/executor/script.py`.
- Editing `src/conductor/engine/execution_resolution.py`, `src/conductor/engine/run_manifest.py`,
  `src/conductor/engine/bundle_prep.py` or `src/conductor/engine/secrets.py`.
- Editing `src/conductor/bundle/`, `src/conductor/digest.py`, `src/conductor/runner/protocol.py`,
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
- Staging symlinks (`_copy_tree_preserving_symlinks`): a link escaping its declared root is an
  `ExecutionSpecError`. The directory bit comes from the lexically resolved target
  (`_resolve_symlink_within_root`), never by following the link, so a Linux-authored target stages
  verbatim on Windows; an unstat-able target is treated as a non-directory.
- Real-daemon tests are marked `docker_integration`
  (`tests/test_execution/test_docker_integration.py`) and excluded from `make test`; unit tests use
  `tests/test_execution/docker_fake.py`.

### Resolver session and leases (`src/conductor/engine/execution_resolution.py`)

- `ExecutionResolverSession` owns run-shared backend instances (`BACKEND_FACTORIES`: `local`,
  `docker`) and one workspace lease per distinct backend; `ExecutionResolver` is the per-engine
  view (root and each sub-workflow).
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
- An injected `(secret_cache, redactor)` pair must belong together (the cache's redactor); a
  mismatched pair is rejected at construction.

### Run manifest and bundle preparation

- `src/conductor/engine/run_manifest.py::compile_run_manifest` pins every executable step of the
  root config against the environment document into a `ResolvedRunManifest`: the run's audit
  record, run-invariant and byte-deterministic. In-memory aids are `exclude=True` so the dump
  stays byte-identical.
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

- This is the serialized contract between the CLI host and a remote agent runtime that deploys
  independently; `conductor.execution` is the in-process seam. Do not merge them.
- Request-side models (`RunnerAgentRequest`, `RunnerAgentPayload`) are `extra="forbid"`;
  response-side models (`RunnerEventFrame`, `RunnerAgentResult`, `RunnerErrorData`,
  `RunnerHealthResponse`) are `extra="ignore"`. Evolution is additive only; bump
  `RUNNER_PROTOCOL_VERSION` only for an incompatible change.
- Credentials in `inner_provider_settings` are `SecretStr` everywhere except
  `request_to_wire_body`, the one place plaintext is produced.
- `src/conductor/runner/__init__.py` re-exports nothing; import from `conductor.runner.protocol`.
  `src/conductor/providers/aca_protocol.py` is a deprecated shim that warns on import.

### Environment documents and secrets

- `src/conductor/config/environment.py`: project documents at `.conductor/environments/<name>.yaml`
  on any ancestor up to the git root, then `$CONDUCTOR_HOME/environments/<name>.yaml`.
  Whole-document shadowing, first match wins: NO merge and NO inheritance. Documents are plain
  YAML with NO environment-variable expansion; secrets are typed references, never values. Import performs no
  I/O.
- The environment digest pops `None` optional fields (`secrets`, per-profile
  `inherit_control_environment`, `docker`) so adding an optional field never changes existing
  digests. Do the same for any new optional field.
- `src/conductor/engine/secrets.py::SecretValueCache` is the only run-scoped secret state. It
  enforces each binding's `allow` consumer-class policy on EVERY resolve, including cache hits,
  and registers every value into the run's `RunRedactor`. Errors name the reference, source
  variable and consumer, never the value.
- `SecretUseIndex` / `index_config` store references and delivery targets only, never plaintext;
  each engine (root and child) gets its own immutable index over one shared cache.
- Static checks (scope placement, delivery transport, collisions, `allow` policies) live in
  `src/conductor/config/validator.py::_validate_secret_references`; Docker profile checks
  (non-script Docker steps, mixed backends, multi-user staging, `inherit_control_environment`,
  `read_only` without tmpfs, `network: host`) in `::_validate_docker_profiles`. Because
  `conductor run` skips the validator, the manifest and cache re-check what they depend on.

### Redaction

- `src/conductor/redaction.py::RunRedactor` scrubs recursively without mutating its input
  (str, bytes, dict, list, tuple), replacing exact string and UTF-8 byte matches longest-first
  with `REDACTED_MARKER`. Transformed values (base64, split, hashed) are out of scope. It is
  single-loop, unlocked.
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
- [ ] Kill/teardown changes keep the shielded drain and absorbed-cancellation re-raise.
- [ ] Bundle content changes keep provenance out of the digest; determinism tests still pass.
- [ ] Store changes keep lock, sentinel-last and atomic rename ordering.
- [ ] Protocol change is additive and keeps request `forbid` / response `ignore`.
- [ ] New optional environment field is popped from the digest when `None`.
- [ ] New sink or serialized artifact scrubs through the run redactor; no secret in messages.
- [ ] Run `uv run pytest tests/test_execution tests/test_bundle tests/test_runner` plus
      `tests/test_redaction.py` and `tests/test_engine/test_secrets_resolver.py`.

## Rationale and evidence

**Shielded drain and kill-and-reap.** Cancelling `communicate()` paused the pipe transports and a
bare kill + `wait()` wedged behind full buffers even after the child died (observed on the
Windows CI fleet, PR #541 review). On Windows the `docker.cmd` shim kept grandchild pipe handles
open, which showed up as 30-second `timed_out` results. Tests:
`tests/test_execution/test_local_backend.py`, `tests/test_execution/test_docker_backend.py`.

**Data-shaped outcomes.** Routing on exit codes requires a non-zero exit to be ordinary data,
while a cancelled run must never look like a completed step.

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
- [Run bundle design](../design/run-bundle.md)
- [Runner protocol design](../design/runner-protocol.md)
- [Secret bindings design](../design/secret-bindings.md)
- [Workflow syntax: execution profiles](../workflow-syntax.md#execution-profiles),
  [secret bindings](../workflow-syntax.md#secret-bindings)
- [CLI reference: conductor bundle build](../cli-reference.md#conductor-bundle-build)
- [Workflow engine](workflow-engine.md), [CLI and runs](cli-and-runs.md),
  [providers](providers.md) (ACA runner), [testing](testing.md)
