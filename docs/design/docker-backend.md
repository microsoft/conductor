# Docker Execution Backend

Status: **Implemented**
Related: Architecture step 6 of #527 (Containerized script execution)

## Summary

The Docker runner backend executes workflow script steps and direct stdio MCP
servers in containers against a run-scoped, shared named volume. Agent steps
use a separate runner image as described in
[agent realms](./agent-realms.md).

Workflows declare execution profiles that select the `docker` backend. Conductor collects the workflow file closure into an immutable, content-addressed run bundle (step 4), compiles execution specifications into a deterministic run manifest (step 5), and stages the bundle into a named Docker volume before step execution. Containers share workspace state across steps within the run. Conductor finalizes managed resources when the run concludes, subject to the retention and ownership checks below.

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
  * `agent = True`: Supports Docker agent execution.
  * `interrupt`: Available when at least one runner exists and all runners report interrupt support.
  * `mcp_stdio = True`: Supports direct MCP servers over a Docker stdio target.
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

Direct MCP servers use the same mapping for the rendered
`runtime.working_dir`. The directory may have been created by an earlier
Docker step. Conductor doesn't probe the host for that directory or fall
back to host execution.

### Direct MCP target

A direct MCP step selects a Docker profile through the ordinary step,
workflow, and environment precedence chain. Its stdio server runs through
`docker run --rm -i` in `docker.image`, with the profile's container
options and the shared volume mounted at `/workspace`. The configured
server command must be present in that image. An agent `runner_image` isn't
required. The engine pools matching server, profile, resolved execution,
lease incarnation, and mapped working directory combinations. See
[MCP steps](../workflow-syntax.md#mcp-steps) for reuse and call semantics.

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
* **Secret injection hygiene**: The effective container environment is written to a protected temporary file (mode `0600` on POSIX) and passed with `--env-file`. Script steps use their declared `env` and secret deliveries; direct MCP servers use their resolved server `env` and server-scoped secret deliveries. The host snapshot is included in the container payload only when the profile enables inheritance. This delivery doesn't overlay the payload into the Docker CLI environment or put its values in Docker arguments. The CLI keeps the operator's ambient Docker, SSH, proxy, and credential-helper context, including any secret sources already present there. Conductor attempts to remove script env-files after container creation and a direct MCP env-file when its target closes; an unconfirmed MCP env-file cleanup is reported. Names must not be empty, must not contain `=`, a line break, or NUL, and must not start with `#`; values must not contain NUL or a line break. An invalid entry is rejected without printing its value. Names outside the shell-identifier shape, such as `CommonProgramFiles(x86)`, are representable.
* **Direct MCP diagnostics**: Docker server stderr is suppressed, SDK transport diagnostics are sanitized, and private failure diagnostics contain metadata instead of raw exception text or tracebacks. These rules don't guarantee redaction of arbitrary successful tool results or files that a workflow writes.
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
* **Direct MCP container**: `conductor-mcp-<run_id[:8]>-<uuid>`
* **MCP fence**: `conductor-mcp-fence-<sha256(volume name)[:24]>`

The run incarnation is carried as a label rather than a name segment, and the
step identity travels as a hash so step names cannot inject unsafe characters
into a container name. Newly created resources carry tracking labels:
* `io.conductor.managed = "true"`
* `io.conductor.run_id = "<run_id>"`
* `io.conductor.workspace = "<run_id>"`
* `io.conductor.resource = "exec" | "scratch" | "runner" | "mcp" | "mcp-fence" | "workspace"`
* `io.conductor.incarnation = "<incarnation>"`

An attached legacy non-MCP volume can lack the workspace label.

Execution containers additionally carry:
* `io.conductor.step = "<step name or command>"`
* `io.conductor.attempt = "<attempt>"`

The fence also carries an `io.conductor.owner` token. It contains no server
payload, doesn't mount the workspace, and is never started.

### Direct MCP fence and cleanup

When a run includes a Docker direct MCP step, the backend ensures the selected
image is available, then atomically acquires a stopped guard with the
deterministic fence name. Preparation and retained-workspace attachment use
the same name before volume verification or staging. A conflicting,
unverifiable, or foreign guard isn't adopted or removed. A fenced attachment
can fail while checking the image, guard, volume, or existing container
inventory; an empty MCP inventory alone doesn't prove the workspace is safe.

The root owns preparation, staging, SDK connections, and targets through
cleanup, including partial startup and cancellation. It seals admission,
closes each SDK owner and target, and waits for all entries and staging
operations within the shared cleanup deadline. The backend releases its
guard by verified immutable container ID and owner token only after that
proof and resource finalization. Unconfirmed owner cleanup or inventory
blocks workspace removal and leaves the guard in place, so a later attachment
is refused even if no MCP container is listed. A late acquisition after
admission is sealed can't begin workspace work.

When the finalizer receives `retain=True`, it doesn't request volume removal.
With `retain=False`, after the owner and inventory proofs, it can request
removal of the verified volume before releasing the guard. Once a Docker
removal request has been issued, an unconfirmed response doesn't prove that
the resource still exists. An ephemeral volume can already have been removed
when a later guard release fails. Inspect the resources independently before
recovery.

The `RunSpec` fence intent is internal. It doesn't add workflow or
environment fields, change `WorkspaceIdentity`, or change the checkpoint
schema. There is no automatic force recovery.

### Finalization Protocol

When a workflow run concludes (whether successfully, on failure, or upon cancellation):

1. The engine closes direct MCP owners and targets before Docker finalization.
2. `DockerRunnerBackend.finalize_run()` inspects and cleans up verified lease resources.
3. The workspace is retained or removed according to the configured policy.
4. A fenced run releases its own verified guard last, only after the required
   owner and staging proofs. Unconfirmed inventory or ownership blocks
   workspace removal. The outcome of a failed removal request can be unknown.

Cleanup is fail-closed. Resource labels and names, and the immutable IDs used
for direct MCP targets and guards, must verify before removal.

### Orphaned Resource Cleanup

An abruptly terminated controller can leave containers, a guard, or a
workspace volume behind. For a fenced or uncertain run, first stop the
original controllers and independently confirm that staging, SDK, and
native owners have all reached a terminal state. Then independently verify
the exact resource ownership labels and immutable container IDs before any
manual recovery. A run ID or an empty container listing isn't sufficient
proof of ownership or completion.

Don't remove a guard or volume blindly, sweep resources by run ID, or use
daemon-wide pruning as a recovery shortcut. A delayed owner can still affect
the workspace, and a shared daemon can contain resources belonging to other
runs. Conductor doesn't automatically adopt a foreign guard or provide a
force-recovery command.

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

1. **Local bind-mount optimization**: Mounting local workspace directories directly for local development.
2. **Read-only `/workspace/source` mount**: Providing a separate immutable view of workflow source files alongside a writable output directory.
3. **Volume snapshot resumption and quotas (Step 8)**: Checkpointing volume states between steps and enforcing volume size limits.
4. **Configurable pull policy**: Explicit `pull_policy` settings (`always`, `missing`, `never`).
