# Docker Execution Backend

Status: **Implemented**
Related: Architecture step 6 of #527 (Containerized script execution)

## Summary

This design introduces the Docker runner backend for Conductor. The backend executes workflow script steps inside short-lived containers against a run-scoped, shared named volume.

Workflows declare execution profiles that select the `docker` backend. Conductor collects the workflow file closure into an immutable, content-addressed run bundle (step 4), compiles execution specifications into a deterministic run manifest (step 5), and stages the bundle into a named Docker volume before step execution. Containers run with short-lived lifecycles and share workspace state across steps within the run. Conductor then finalizes and cleans up all managed containers and volumes when the run concludes.

## Motivation and Purpose

Workflow steps frequently run shell scripts, data processing utilities, or build tools. Running these commands directly on the host machine presents several challenges:

* **Environment drift**: Local tool versions, system libraries, and operating system quirks cause scripts to succeed on one developer machine and fail in CI.
* **Host pollution**: Script steps can inadvertently leave modified files or temporary data across the host filesystem.
* **Security posture**: Running uncontained script commands with ambient host access risks accidental damage or unauthorized file access.

The Docker backend addresses these challenges by isolating script step execution inside container environments. It builds upon Conductor's existing execution abstractions:

1. **Run Bundles (Step 4)**: The complete file closure of the workflow is packaged into a deterministic content-addressed store.
2. **Secret Bindings and Environments (Step 5)**: Execution environment documents configure profile options and bind credentials, while run manifests record the resolved configuration.
3. **Execution Backend Seam**: The `RunnerBackend` interface executes commands and manages workspace leases without coupling orchestration to container internals.

## Backend Contract

The Docker backend implements the `RunnerBackend` contract defined in `conductor.execution.types`:

* **Interface implementation**: `DockerRunnerBackend` in `conductor.execution.docker`.
* **Capabilities**:
  * `batch = True`: Supports one-shot command execution via `run_command()`.
  * `sessions = False`: Interactive persistent sessions are not supported.
  * `shared_workspace = True`: Steps within a run share the same named volume workspace.
  * `retained_workspace = True`: Supports retaining and re-attaching named volumes across runs (see [Retained Workspace Lifecycle](./workspace-lifecycle.md)).
  * `snapshots = False`: Mid-run workspace checkpointing is deferred.
* **CLI transport**: Uses the system `docker` CLI executable directly via `asyncio.subprocess` rather than a Docker Python SDK. This keeps the execution path compatible with the operator's active Docker context, credential helpers, environment variables, and remote `DOCKER_HOST` configurations.
* **Engine floor**: Requires Docker Engine 20.10 or newer (Docker API 1.41+).

## Workspace Model v1

The Docker runner backend uses a single named volume per workflow run:

```
conductor-ws-<run_id>
  └── /workspace/
        ├── <root>/      # Root workflow logical directory: main/ for a local
        │                # workflow, registry/<registry>/<sha>/ for a
        │                # registry-hosted root workflow
        ├── roots/       # Additional declared roots
        │     ├── 00/    # First additional root
        │     └── ...
        ├── registry/    # Registry-cached sub-workflows (per registry and sha)
        ├── plugins/     # Collected plugin namespaces
        └── skills/      # Collected skill namespaces
```

### Directory Layout

Conductor stages the complete content-addressed bundle `tree/` into the named volume root `/workspace`.

* `/workspace/<root>/`: The root workflow's logical bundle directory — `main/` for a local workflow, `registry/<registry>/<sha>/` for a registry-hosted root workflow. Contains the root workflow file and assets relative to the workflow directory.
* `/workspace/roots/<NN>/`: Contains external directories declared in `workflow.bundle.additional_roots`.
* `/workspace/registry/<registry>/<sha>/`: Registry-cached sub-workflows referenced by the root workflow.
* `/workspace/plugins/`, `/workspace/skills/`: Plugin and skill namespaces collected into the bundle.
* **Symlink integrity**: The volume layout mirrors the content-addressed store `tree/` structure byte for byte. Relative symlinks pointing from `<root>/` to `../roots/<NN>/` or a sibling namespace resolve correctly inside the container without path translation.

### Working Directory Resolution

Step working directories resolve against the container volume:

| Value in YAML | Resolved Container Path | Behavior |
|---|---|---|
| `None` (omitted) | `/workspace` | Defaults to the workspace root. |
| Relative path (e.g. `src`) | `/workspace/<root>/src` | Anchored inside the root workflow's logical directory (`main/` for local workflows, `registry/<registry>/<sha>/` for registry-hosted roots). Traversal escaping `<root>/` with `..` is rejected. |
| POSIX absolute path (e.g. `/app`) | `/app` | Passed verbatim to the container. |

Windows drive letters, backslashes, and empty path strings are rejected during validation and preflight.

### Temporary File Storage (`tmpfs`)

Containerized workloads often write temporary files to `/tmp`. When the container root filesystem is mounted read-only, `/tmp` must be backed by memory.

The `tmpfs` option under `docker` profile settings controls this mount:
* `false` (default): No tmpfs mount is added.
* `true`: Mounts a standard tmpfs at `/tmp`.
* String size (e.g. `"1g"`, `"512m"`): Mounts a tmpfs at `/tmp` with the specified size limit.

## Staging Mechanism and Ownership

Conductor stages the run bundle before executing the first Docker step in a run.

### The Staging Protocol

1. **Bundle preparation**: The engine verifies the run bundle in the local cache, building or publishing it if necessary.
2. **Volume creation**: `docker volume create conductor-ws-<run_id>` initializes the volume with Conductor tracking labels.
3. **Scratch container**: Conductor creates a stopped scratch container mounting the volume:
   ```bash
   docker create --name conductor-stage-<run_id[:8]>-<uuid6> \
     --user <resolved_user> \
     -v conductor-ws-<run_id>:/workspace \
     --entrypoint /conductor-staging-placeholder \
     <image>
   ```
   The placeholder entrypoint is never executed (the scratch container is
   removed without being started); it exists so images with no `CMD` or
   `ENTRYPOINT` of their own are accepted by `docker create`.
4. **Archive copy**: Conductor runs `docker cp -a <staged_tree>/. <container>:/workspace` to transfer files into the volume.
5. **Scratch cleanup**: Conductor immediately removes the scratch container with `docker rm -f`.

### Spike Findings and Ownership Mechanism

During development, an ordered spike evaluated three candidate mechanisms for staging file ownership:

* **Candidate 1 (Plain `docker cp` + scratch container `--user 65532:65532`)**: Failed. The Docker daemon unpacked the archive with default root ownership, leaving non-root container users unable to write to `/workspace` (exit code 1, `Permission denied`).
* **Candidate 2 (`docker cp -a` + scratch container `--user 65532:65532`)**: Succeeded. The `-a` archive flag instructed the daemon to copy ownership and permissions matching the scratch container's configured user (exit code 0, writable).
* **Candidate 3 (`chmod -R a+rwX` + plain `docker cp`)**: Succeeded, but required mutating the source files before copy.

Candidate 2 was selected as the staging mechanism (`_STAGING_COPY_MODE = "archive-to-container-user"`).

Benefits of this approach:
* **Distroless compatible**: Does not require `tar`, `chown`, or a shell inside the container image.
* **Remote daemon safe**: Operates entirely through standard Docker CLI streaming protocols without host filesystem mounts.
* **Non-root support**: Non-root container users can write to the staged volume immediately.

### Multi-User Staging Caveat

The workspace volume is populated once per run under the user configured on the first executed Docker profile. If an environment defines multiple Docker profiles with different explicit `user` values, subsequent steps running as different users may lack write permissions in `/workspace`. Conductor's static validator detects this condition and warns operators to align user settings or split workloads across separate environments.

## Security Model and Trust Boundaries

The Docker runner backend provides isolation for script steps while following platform-native conventions.

### Two Profile Recipes

Conductor provides two primary profile configurations:

#### 1. Frictionless Default Recipe

Matches standard Docker CLI behavior out of the box.

```yaml
profiles:
  build:
    backend: docker
    docker:
      image: node:20
```

* Image tag or digest accepted.
* Platform auto-resolved by Docker daemon.
* Container user defaults to the image `USER` instruction.
* Writable container root filesystem (`read_only: false`).
* Standard bridge or host network.
* Uncapped host resource access until limits are set.

#### 2. Hardened Production Recipe

Provides strict containment for untrusted or multi-tenant workloads.

```yaml
profiles:
  hardened:
    backend: docker
    docker:
      image: alpine@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc
      platform: linux/amd64
      user: "65532:65532"
      network: none
      read_only: true
      cap_drop_all: true
      no_new_privileges: true
      tmpfs: "512m"
      resources:
        cpu: 2.0
        memory: "1g"
        pids: 256
```

* Pinned image digest ensures reproducible execution.
* Explicit target platform.
* Explicit non-root user and group ID.
* Network access completely disabled (`network: none`).
* Read-only root filesystem with memory-backed `/tmp`.
* All Linux capabilities dropped (`--cap-drop=ALL`).
* Privilege escalation blocked (`--security-opt=no-new-privileges`).
* Explicit CPU, memory, and process count ceilings.

### Analysis of Default Footguns

Operators using default settings should account for several standard Docker behaviors:

1. **Stale image tags**: Conductor does not force `docker pull` before every run in v1. A local tag like `python:3.12` reuses the daemon's cached image unless updated manually. Pin image digests for reproducible runs.
2. **Shared daemon resource starvation**: Containers without configured `resources` share host CPU, memory, and PIDs unconstrained. On shared CI runners, configure `resources.cpu`, `resources.memory`, and `resources.pids` to prevent runaway processes.
3. **Writable root filesystem lifecycle**: Any file written outside `/workspace` or `/tmp` lives only for the duration of that single script step. When the container exits, uncommitted root filesystem changes are discarded.
4. **Init process usage (`init: true`)**:
   * **When to use**: Enable `init: true` for workloads that spawn background subprocesses that may leave zombie processes.
   * **When forbidden**: Do not enable `init: true` on container images that provide their own init system (such as `s6-overlay` or images where `tini` must run as PID 1). Conductor kills and reaps processes at the CLI level regardless.
5. **Host networking (`network: host`)**: Removes container network isolation and exposes host network interfaces. Host mode is not portable to Docker Desktop or remote daemons.
6. **Default container user**: Images without an explicit `USER` instruction run as `root` inside the container.

### Hard Security Bans

To prevent container breakouts and system compromise, Conductor explicitly forbids:
* Privileged mode (`--privileged`).
* Host PID and IPC namespace sharing (`--pid=host`, `--ipc=host`).
* Host device passthrough (`--device`).
* Mounting the Docker daemon socket (`/var/run/docker.sock`).

The profile schema enforces these exclusions with strict Pydantic models (`extra="forbid"`).

### Operator Trust and Credentials

* **Docker group membership**: Access to the Docker daemon socket grants root-equivalent control over the host system.
* **Secret injection hygiene**: The effective container environment (declared `env:` overrides merged over the host snapshot only when the step inherits the control environment) is written to a protected temporary file (mode `0600` on POSIX) and passed to `docker create` via `--env-file`. The Docker CLI control environment stays identical for every invocation, so payload variables such as `DOCKER_HOST`, `DOCKER_CONTEXT`, `DOCKER_CONFIG`, or `HOME` can never redirect container creation to a different daemon or alter client configuration. Plaintext secret values never appear in command-line arguments, process argv, or log output, and the temporary file is removed once `create` completes. Names must not be empty, must not contain `=`, a line break, or NUL, and must not start with `#` (which Docker's env-file parser treats as a comment and silently drops); values must not contain NUL or a line break. These are exactly the entries the env-file line format cannot represent, and they are rejected before `create` names the variable (never its value). Names outside the shell-identifier shape — including host variables every Windows machine sets, such as `CommonProgramFiles(x86)` — are representable and pass through to the container unchanged.
* **Inspect visibility**: Environment variables passed to containers are visible in `docker inspect` output to authorized daemon administrators.
* **Volume quotas**: Enforcing disk storage quotas on `/workspace` named volumes is the responsibility of the host operator and storage driver.

### Windows Environment Variable Case Semantics

Environment variable names are case-insensitive on a Windows host but case-sensitive inside the Linux container. When a script step's `env:` key differs from an inherited host variable only by case (for example `path` versus `PATH`), Conductor's environment overlay drops the host variable from the **container payload environment** (the content of the temporary `--env-file`) before applying the step's value, so the declared key deterministically wins the collision. The Docker CLI subprocess environment itself is never modified. Two consequences follow:

* Both case variants can never reach the same container from a Windows host — the declared `env:` key replaces the host variable rather than coexisting with it the way the two names would inside the container.
* The same workflow on a POSIX host forwards both variants independently, so a workflow relying on case-distinct variable names behaves differently across host platforms. Declare `env:` keys in the exact casing the container payload reads, and avoid case-only duplicates of host variables.

## Lifecycle, Finalization, and Garbage Collection

Conductor manages the complete lifecycle of temporary containers and volumes created during a workflow run.

### Resource Naming and Labels

All Docker resources created by Conductor use predictable naming patterns and metadata labels:

* **Named volume**: `conductor-ws-<run_id>`
* **Execution container**: `conductor-<run_id[:8]>-<sha1(step name or command)[:8]>-<attempt>`
* **Scratch container**: `conductor-stage-<run_id[:8]>-<uuid6>`

The run incarnation is carried as a label rather than a name segment, and the
step identity travels as a hash so step names cannot inject unsafe characters
into a container name. Every resource carries tracking labels:
* `io.conductor.managed = "true"`
* `io.conductor.run_id = "<run_id>"`
* `io.conductor.workspace = "<run_id>"`
* `io.conductor.resource = "exec" | "scratch" | "workspace"`
* `io.conductor.incarnation = "<incarnation>"`

Execution containers additionally carry:
* `io.conductor.step = "<step name or command>"`
* `io.conductor.attempt = "<attempt>"`

### Finalization Protocol

When a workflow run concludes (whether successfully, on failure, or upon cancellation):

1. `DockerRunnerBackend.finalize_run()` is invoked by the engine.
2. Conductor inspects all containers associated with the lease, stops running containers, and deletes them.
3. Conductor removes the named volume `conductor-ws-<run_id>`.
4. Finalization is fail-closed: it verifies exact label matches and name prefixes before issuing removal commands.

Cleanup is idempotent by `run_id` at three lifecycle levels: attached step cleanup,
backend-native run finalization, and manual recovery of orphaned resources. Only
the orphaned level needs an operator recipe; never sweep resources belonging to
other runs on a shared daemon.

### Orphaned Resource Cleanup

If a workflow process is killed abruptly with `SIGKILL`, running containers and
the named workspace volume may remain. First confirm that this particular run
has been abandoned and will not be resumed or finalized by a live process.
While it is live, `conductor status --json` exposes its identifier under
`running[].run_id` (alongside `workflow`, `pid`, and `event_log`); record that
value in the CI job before the process exits. For an already-dead run, recover
the identifier from its event log filename under `$TMPDIR/conductor/`:
`conductor-<workflow_name>-<YYYYMMDD-HHMMSS>-<run_id>.events.jsonl`.
Confirm the identifier against the intended run before setting `RUN_ID`; do
not infer it from a list of unrelated runs. A resumed run may reuse the same
identifier, so check that no resumed process still owns it.

Set `RUN_ID` to that known, confirmed-abandoned identifier, then list, kill,
and remove only its containers (including stopped scratch containers), remove
its named volume explicitly, and verify that both are gone:

```bash
set -euo pipefail
: "${RUN_ID:?Set RUN_ID to the confirmed-abandoned run id}"
docker ps -a --filter label=io.conductor.managed=true \
  --filter "label=io.conductor.run_id=${RUN_ID}"
docker ps -q --filter label=io.conductor.managed=true \
  --filter "label=io.conductor.run_id=${RUN_ID}" |
  while IFS= read -r container; do docker kill "$container"; done
docker ps -aq --filter label=io.conductor.managed=true \
  --filter "label=io.conductor.run_id=${RUN_ID}" |
  while IFS= read -r container; do docker rm -f "$container"; done
if docker volume inspect "conductor-ws-${RUN_ID}" >/dev/null 2>&1; then
  docker volume rm -f "conductor-ws-${RUN_ID}"
fi

# Both checks must succeed: no containers and no named workspace volume.
test -z "$(docker ps -aq --filter label=io.conductor.managed=true \
  --filter "label=io.conductor.run_id=${RUN_ID}")"
if docker volume inspect "conductor-ws-${RUN_ID}" >/dev/null 2>&1; then
  printf '%s\n' 'Workspace volume still exists' >&2
  exit 1
fi
```

Do not substitute `docker container prune` or `docker volume prune` for this
recipe: container prune removes only stopped containers, leaving a running
orphan; on Docker API 1.42+, volume prune without `--all` removes only
anonymous volumes, while this backend creates named workspace volumes.
Label-wide `managed=true` removal and daemon-wide pruning can destroy OTHER
Conductor runs' state on a shared daemon; use daemon-wide cleanup only on a
dedicated, disposable daemon with no other runs.

#### Sample CI Cleanup Step

Before teardown, save the job's `running[].run_id` from `conductor status
--json` while its run record still exists: match the record to this job's
workflow and PID rather than selecting the first entry on a shared runner.
Persist the value as `CONFIRMED_ABANDONED_RUN_ID` only after confirming that
the recorded run has terminated and will not be resumed; `if: always()` alone
does not establish abandonment. Supply that variable to the step below.

```yaml
- name: Clean up Conductor Docker resources
  if: always() && env.CONFIRMED_ABANDONED_RUN_ID != ''
  env:
    RUN_ID: ${{ env.CONFIRMED_ABANDONED_RUN_ID }}
  run: |
    set -euo pipefail
    : "${RUN_ID:?Missing confirmed-abandoned run id}"
    docker ps -aq --filter label=io.conductor.managed=true \
      --filter "label=io.conductor.run_id=${RUN_ID}" |
      while IFS= read -r container; do docker rm -f "$container"; done
    if docker volume inspect "conductor-ws-${RUN_ID}" >/dev/null 2>&1; then
      docker volume rm -f "conductor-ws-${RUN_ID}"
    fi
    test -z "$(docker ps -aq --filter label=io.conductor.managed=true \
      --filter "label=io.conductor.run_id=${RUN_ID}")"
    if docker volume inspect "conductor-ws-${RUN_ID}" >/dev/null 2>&1; then
      printf '%s\n' 'Workspace volume still exists' >&2
      exit 1
    fi
```

## Resume Semantics and Seeding Asymmetry

When a workflow is resumed using `conductor resume`:

1. **Workspace attachment or fresh materialization**: If the workflow configured a retained workspace policy (`persistence: durable` or `on-failure`), the Docker backend verifies and re-attaches the existing volume using `attach_run` without re-staging files. If the policy is `ephemeral`, Conductor provisions a fresh workspace lease and stages the bundle into a new volume. Full details live in [Retained Workspace Lifecycle](./workspace-lifecycle.md).
2. **Resume seeding asymmetry**: When resuming from a checkpoint, the CLI seeds the initial `workflow_started` event from the existing checkpoint context before initializing the engine. Consequently, the seeded `workflow_started` event does not contain the newly prepared `system.bundle` metadata. The bundle digest remains visible in verbose execution diagnostics and the engine preparation logs.

## Mixed-Backend Workflow Semantics

Workflows can combine local and Docker script steps across different profiles:

* **Bundle boundary warning**: Docker script steps execute inside the staged bundle volume, while local script steps execute on the host filesystem. Files created or modified by a local script step on the host are not automatically mirrored into the Docker volume during the run.
* **Validator warning**: When a workflow assigns script steps to both `local` and `docker` backends within the same environment, `conductor validate` prints a warning disclosing the bundle snapshot boundary.

## Deferred Capabilities

The following capabilities are deferred to future architecture milestones:

1. **Agent execution realms (Step 7)**: Running LLM agent tool loops and MCP servers inside container sandboxes.
2. **Local bind-mount optimization**: Mounting local workspace directories directly for local development.
3. **Read-only `/workspace/source` mount**: Providing a separate immutable view of workflow source files alongside a writable output directory.
4. **Volume snapshot resumption and quotas (Step 8)**: Checkpointing volume states between steps and enforcing volume size limits.
5. **Configurable pull policy**: Explicit `pull_policy` settings (`always`, `missing`, `never`).
