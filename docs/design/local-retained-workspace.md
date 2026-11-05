# Local Retained Workspaces

Retained local execution gives participating local steps one mutable copy of
the selected run bundle. It doesn't copy the checkout wholesale and isn't a
sandbox. The root workflow opts in using the existing policy:

```yaml
workflow:
  workspace:
    mode: shared
    persistence: durable
```

`on-failure` retains the workspace after an unexpected failure or cancellation,
then removes it after a successful resume. An explicit terminal failure is
non-resumable and follows the normal non-retained cleanup path. `durable`
keeps the workspace after success, failure, or cancellation, but still
releases runtime resources. Cleanup that cannot be confirmed leaves the tree
in place regardless of policy. Omitted or `ephemeral` persistence keeps the
ordinary local behavior. `isolated` mode is reserved.

## Bundle and filesystem boundaries

Conductor collects the workflow's statically knowable closure, including
referenced includes, sub-workflows, selected skills and plugins, and
`workflow.bundle.assets`. Assets are paths or patterns relative to their
workflow directory. `workflow.bundle.additional_roots` grants permission to
collect dependencies from outside the usual roots; it doesn't select all
files under those roots.

For an actual local script, agent, or direct MCP consumer, the bundle's
complete `tree/` is copied into one local workspace per run. There are no
hardlinks to the content-addressed store. Source files and the cached bundle
stay unchanged by writes to the copy, and there is no automatic copy-back.
The full copied tree preserves the collected namespaces, including `main/`
for the root workflow, `registry/<name>/<sha>/` for registry workflows,
`roots/<index>/` for additional roots, and selected `skills/` and `plugins/`
topologies. A skill associated with a plugin can also carry its selected
owning plugin topology. Not every namespace exists in every bundle.

The local root is currently
`$CONDUCTOR_HOME/workspaces/<run_id>/local/`, defaulting to
`~/.conductor/workspaces/<run_id>/local/`. Its `work/` directory contains the
mutable copy and `identity.json` sits outside it. These paths help locate a
retained run, but the marker format and internal directories aren't a public
storage API.

`shared` means one tree within a backend realm, not one tree across all
backends. A retained local tree and a Docker volume are independent. Validation
can warn about resolved local and Docker file consumers; at runtime, one
warning is emitted once both realms have dispatched file consumers. File
changes aren't synchronized, but context JSON outputs pass between steps
unchanged. Unused profiles and a technical local lease in a Docker-only run
aren't local file consumers.

## Directories and providers

The default local working directory is the staged directory of the current
workflow. For retained local scripts, the selected directory is the step's
`working_dir`, then `workflow.runtime.working_dir`, then that staged workflow
directory. Rendered directory fields are mapped into the live tree and
checked for existence and containment at dispatch time. A directory created
by an earlier step can therefore be used later. An unavailable or escaping
directory is refused; Conductor doesn't fall back to the host directory.

Only typed directory fields and selected dependency paths are mapped.
Commands, arguments, environment values, and prompts aren't rewritten as
paths. Relative file arguments work from the staged working directory;
authored absolute paths in ordinary strings remain as written. A process may
still access files allowed by the host. Retention doesn't expand settings
tiers, tool or MCP allowlists, credentials, or explicit filesystem grants.

Selected skill and plugin dependencies are resolved from the collected
topology rather than rediscovered from the host for each call. Local agent
providers must declare both `working_dir` and `managed_local_workspace`.
The current declarations enable managed local execution for `copilot`,
`claude`, `openai`, and `claude-agent-sdk`; `hermes` and `aca` don't declare
that capability. Managed Copilot requires an owned native stdio runtime.
An external runtime selected through `runtime_url` or
`COPILOT_PROVIDER_RUNTIME_URL`, and an externally injected Copilot client,
are refused for retained local execution. This restriction doesn't describe
ordinary unmanaged local execution.

## Ownership and shutdown

The root engine owns the run's workspace lease and the release barrier for
its local consumers, including script processes, direct and provider-owned
stdio MCP connections, and provider runtimes. The backend-issued
`WorkspaceLease` is an in-process handle; checkpoint `WorkspaceIdentity`
fields are serializable evidence, not a replacement handle.
`LocalWorkspaceBinding` and its runtime context are also in-process objects,
not checkpoint fields.

The engine requests targeted release through the participating provider or
`ProviderRegistry.release_workspace(workspace_root)`. It doesn't globally
close an injected provider or unrelated shared resources. A direct caller
still owns its provider and event loop. The backend requires the original
lease for `confirm_workspace_released(lease)` and
`finalize_run(lease, outcome, *, retain=False)`; reconstructing a lease from
serialized fields isn't a substitute for ownership. Release hooks alone
aren't native process proof.

A `released` result requires native proof that every managed process scope is
empty, its streams are drained, its custody processes are reaped, and all
relevant resource-owner tasks have joined. Only after the common barrier
does the backend mark the workspace idle and apply retention or verified
deletion. An SDK close error is still reported, but doesn't by itself imply
an active consumer if native proof and owner joins are complete.

`unconfirmed` is terminal for that cleanup attempt. A timeout, lost custody,
missing proof, or unfinished owner prevents a transition to idle, deletion,
and later attachment. Pending owners can remain quarantined with their errors
observed; their later completion doesn't silently change the result or
restart finalization. Closing a loop isn't proof of native release.

Cleanup uses a private monotonic deadline, not a YAML setting. A teardown
episode has at most 30 seconds from its first intent, subject to any earlier
process or invocation deadline. After teardown intent, a process gets up to
five seconds after its input closes; without input closure, the normal
teardown phase runs no later than 20 seconds from the episode start. The
force phase uses the remaining budget for cleanup, proof, and joins. A
completed command can start descendant cleanup immediately. Repeated
cancellation or another resource doesn't extend the original deadline.

These bounds assume a responsive event loop. For direct callers, an
unconfirmed engine return can leave Python owners pending on the caller's
loop; unrelated tasks aren't cancelled. For the synchronous CLI's
quarantined path, one additional 30-second episode is shared across remaining
tasks, async generators, and default executor shutdown. Blocking Python or C
code, non-daemon threads, and interpreter shutdown hooks can still prevent a
hard process exit.

## Resume and platform limits

Resume checks the recorded workflow, environment, manifest, policy, and,
when recorded, bundle digest before attaching. Changed declared source files
can change the bundle digest and cause refusal. A recorded null bundle digest
isn't compared as if it were a non-null digest. An executed retained backend
requires its original identity and resource; a missing tree or identity is
not recreated. The local marker must verify the same ready, idle incarnation.
An active or uncertain marker fences attachment, even if a checkpoint exists.
There is no automatic recovery of an unknown tree or automatic restaging
over retained edits.

An interrupted step's status is unknown. The default `restart.mode: rerun`
can repeat its side effects; use `restart.mode: fail` when that isn't safe.
After a confirmed release, subsequent durable resumes attach the same
incarnation and keep edits from earlier generations, rather than seeding the
original bundle again. See [resume](../cli-reference.md#conductor-resume).

Managed local execution currently requires Linux or Windows and an installed
`conductor-local-scope` helper in the active Python environment. Linux
custody requires subreaper and pidfd support. Windows uses a non-breakaway
Job Object and refuses a payload if it cannot assign the suspended process.
Windows staging also rejects unrepresentable or case-colliding names,
junctions and other non-symlink reparse points, and unsafe symlinks. Symlink
creation depends on the host's permissions. These are implementation restrictions,
not a claim that every native Windows or remote Docker configuration has been
tested.

Managed Windows commands resolve from the final process environment and working
directory, including arguments, environment and cwd supplied by an SDK to its
stdio facade. Environment names use Windows ordinal case-insensitive identity.
Explicit overlays replace inherited aliases; conflicting aliases within one
mapping are refused. MCP retains its SDK's filtered inheritance, with canonical
key spelling so the SDK's second merge cannot restore an overridden value.

Bare commands search only the declared `PATH`, in directory order. Missing or
empty `PATH` searches nothing. Empty segments are ignored; `.` explicitly
selects the supplied cwd, and ordinary relative entries are anchored there.
Ambiguous drive-relative or rooted-without-drive paths and malformed PATH
quotes are refused. Supported targets are `.exe`, `.com`, `.cmd` and `.bat`.
For a command without an extension, missing `PATHEXT` means
`.COM;.EXE;.BAT;.CMD`; present-empty means no extension completion. A supplied
list keeps the order of its supported extensions, with each directory checked
before the next. An explicit extension is never supplemented. Explicit native
paths retain the actual Windows loader's errors and native argument encoding.
There is no implicit parent, system-directory, current-directory or file
association fallback. A JavaScript CLI needs an explicitly selected native
interpreter, such as `node.exe`; `.js` and `.ps1` are not automatic targets.

Automatic `.cmd` and `.bat` invocation supports a limited literal argument
subset. Empty arguments, ordinary spaces, Unicode, internal backslashes, and
plain trailing-backslash tokens such as `C:\plain\` are supported. Quotes,
percent signs, exclamation marks, carets, shell operators, parentheses,
wildcards, control characters, unpaired surrogates and Unicode line/paragraph
separators are refused before payload dispatch. A trailing backslash that needs
quoting, such as `C:\with space\`, is also refused rather than rewritten.
The target path has the same character restrictions. These restrictions do
not apply to native `.exe` or `.com` arguments.

Batch target, cwd and interpreter paths must be drive-absolute; automatic UNC
and device-path adaptation is unsupported. An explicitly declared `ComSpec`
must be an unquoted absolute path to a compatible Microsoft command processor.
Only an absent `ComSpec` permits `<SystemRoot>\System32\cmd.exe`, using a
declared absolute `SystemRoot`. An empty, invalid or unavailable `ComSpec`
does not fall back. Invocation uses `/d /e:on /v:off /s /c`; it adds no `CALL`,
escapes, associations or extra grants. The complete command line including its
terminator, and each `name=value` environment entry including its terminator,
must fit 8191 UTF-16 code units. Oversized entries are refused, never dropped.

SDK-added arguments follow the same rules. For example, JSON schema arguments
or quoted prompts may require a native CLI instead of a batch wrapper.
Diagnostics retain the requested or resolved wrapper target, not `cmd.exe`.
A policy refusal, lookup miss, native startup error, and an already-started
batch command's nonzero exit remain different outcomes. A wrapper's own
expansion, parsing, subprocess lookup and failures remain its authored
behavior; this adapter does not intercept all descendant subprocesses.
