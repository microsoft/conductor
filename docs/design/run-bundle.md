# Run Bundle

Status: **Implemented**

## Summary

A run bundle is an immutable, content-addressed archive holding every file a
workflow run depends on. This includes the root workflow document, external
file includes (`!file` and `!yamlfile`), Jinja2 template closures (`{% include %}`),
sub-workflows (local and registry-backed), skills, plugins, and declared static
assets.

The bundle packages all dependencies into a unified, relocatable POSIX `tree/`
namespace stored in a content-addressed store (CAS) at
`$CONDUCTOR_HOME/cache/bundles/sha256-<hex>/`. Manifest metadata retains the
canonical `sha256:<hex>` digest spelling; only the filesystem key uses a hyphen
so it is portable to Windows.

## Motivation

Conductor workflows often depend on files scattered across the filesystem:

* Prompt templates and schema files included with `!file` or `!yamlfile`.
* Shared Jinja2 partials referenced in prompts.
* Sub-workflow YAML documents in local directories or git registries.
* Reusable skills from local roots or plugin packages.
* Agent definitions and MCP configurations shipped by plugins.
* Scripts, configuration files, and data fixtures needed by script steps.

When executing locally in the same process, the workflow engine reads these
files directly from disk. However, supporting remote agent runtimes, isolated
containers, and distributed execution backends (step 4 of issue #527) requires
packaging the complete file closure into a single relocatable artifact.

The run bundle solves this problem. It computes the full dependency closure
statically, verifies that all dependencies stay within authorized boundaries,
and produces a deterministic, content-addressed bundle.

## Goals

* Collect the complete, statically knowable file closure of a workflow.
* Create a deterministic content digest (`bundle_digest`) that depends only on
  file contents and logical paths.
* Package dependencies into a relocatable, POSIX-normalized directory layout.
* Build reproducible archives (`bundle.tar.gz`) with pinned timestamps and
  normalized metadata.
* Enforce security boundaries by restricting bundled files to authorized root
  directories.
* Keep provenance and environment linkage separate from the content-addressed
  manifest.
* Provide an offline validation report and a CI priming command.

## Non-goals (v1)

* Remote execution or automatic container staging (deferred to step 6).
* Dynamic file discovery during runtime execution.
* Automatic garbage collection for the bundle cache directory.
* Secret scanning or credential scrubbing inside bundled assets.
* Cross-filesystem namespace translation for paths outside declared roots.

## Content Zone vs. Provenance and Link Fields

A core architectural invariant separates content data from ephemeral metadata.

### Content Zone (`BundleManifest`)

The content zone represents the immutable artifact. It consists of:

* `version`: Schema version of the manifest (`1`).
* `bundle_digest`: The canonical SHA-256 digest of the bundle content.
* `entries`: The sorted list of `BundleEntry` objects describing every file and
  symlink in the bundle.
* `skills`: The mapping of agent names to their resolved skill identifiers.
* `plugins`: The list of active plugin names.

The content zone is saved in the CAS as `bundle.json` and inside the archive as
`.bundle/manifest.json`.

Two bundles with identical file contents and logical structures produce the exact
same `bundle_digest`, even if they were built on different machines, at different
git commits, or in different execution environments.

### Provenance and Link Fields (`BundleDescriptor`)

The `BundleDescriptor` captures context about how and where the bundle was built:

* `provenance`: Git repository status, commit SHA, dirty file status, registry
  source references, and plugin checkout information.
* `run_manifest_digest`: The SHA-256 digest of the resolved execution manifest.
* `workflow_digest`: The SHA-256 digest of the root workflow document.
* `environment`: The name, source, and document digest of the execution environment.

These fields are ephemeral. Conductor prints them in CLI reports but never stores
them in `bundle.json` or the CAS. Storing provenance in the CAS would cause two
identical content trees at different commits to produce different digests,
breaking caching and content deduplication.

## Digest Calculation

Conductor uses `canonical_json_digest` from `conductor.digest` to compute
deterministic SHA-256 digests.

The `bundle_digest` is computed over a canonical JSON payload containing:

1. `version`: Literal integer `1`.
2. `entries`: A sorted array of entry dictionaries, ordered by `logical_path`.
   Each entry contains `logical_path`, `kind`, `digest`, `size`, `executable`,
   and `link_target`.
3. `skills`: A dictionary mapping each agent name to its sorted list of skill names.
4. `plugins`: A sorted list of active plugin names.

Formatting rules ensure cross-platform reproducibility:

* JSON keys are sorted alphabetically.
* Whitespace separators are compact: `","` and `":"`.
* `ensure_ascii=True` avoids host encoding differences.
* Result format is prefixed: `sha256:<64-hex-characters>`.

## Allowed Roots and Layout

To prevent unintended files from leaking into bundles, collection is restricted
to explicitly authorized filesystem roots.

### Authorized Roots

1. **Workflow Directory (Default):** The directory containing the root workflow
   file. Mapped to `tree/main/`.
2. **Additional Roots:** Explicitly authorized directories declared in
   `workflow.bundle.additional_roots`. Each root is assigned a stable declaration
   index and mapped to `tree/roots/<NN>/`; host paths and basenames never enter
   digest-bearing metadata.
3. **Pre-Authorized Caches:**
   * Registry cache: `$CONDUCTOR_HOME/cache/registries/` mapped to
     `tree/registry/<registry>/<sha12>/`.
   * Plugin checkouts: `$CONDUCTOR_HOME/cache/plugins/` mapped to
     `tree/plugins/<name>/`.
   * Built-in and installed skills: mapped to `tree/skills/<name>/`.

Any reference attempting to escape these roots triggers a `BundleRootEscapeError`.

### Namespace Structure

Inside the bundle store directory and archive, all files reside under the `tree/`
prefix:

```
tree/
  main/                               # Root workflow directory
    workflow.yaml
    prompts/
      review.md
      _shared.md
    scripts/
      check.sh
  roots/
    00/                               # Additional root 0
      helper.py
  registry/
    official/
      a1b2c3d4e5f6/                   # Sub-workflow from official registry
        subflow.yaml
  plugins/
    code-review/                      # Plugin dependencies
      agents/
        reviewer.agent.md
  skills/
    conductor/                        # Bundled skill dependencies
      SKILL.md
```

## Symlinks, Executable Bits, and Cross-Platform Rules

Bundles preserve essential POSIX file metadata while remaining relocatable.

### Symlinks

A symlink's content is defined as its normalized link target string (`link_target`):

* `size` is always `0`.
* `executable` is always `False`.
* `digest` is the SHA-256 of the UTF-8 encoded target string.
* Targets are collected recursively, including directory contents, so staged
  links are never dangling and target bytes contribute to the bundle digest.
* Link targets are rewritten relative to their logical bundle locations.
* Target paths must stay within an authorized root. Escapes trigger
  `BundleSymlinkEscapeError`, and recursive link cycles trigger
  `BundleCycleError`.

Conductor follows POSIX-first semantics. If a checkout materializes a symlink
as a regular file (for example, on Windows without symlink privileges), it is
bundled as a regular file according to local disk reality.

### Executable Bits

The executable bit (`chmod +x` / mode `0o755`) is detected during file collection
and stored in `BundleEntry.executable`. The archive writer applies mode `0o755`
for executable files, `0o644` for standard files, and `0o777` for symlinks.

### Deterministic Tar Creation

The archive generator (`write_bundle_archive`) produces byte-identical `.tar.gz`
files:

* Entries are sorted strictly by their POSIX path names.
* User ID and Group ID are set to `0`.
* User name and Group name are cleared to empty strings.
* Tar modification time (`mtime`) is set to `0` (Unix epoch).
* Gzip header timestamp is set to `0` with an empty filename.

Rebuilding the same staged tree on any machine yields the exact same archive bytes.

## Caps and Safety Limits

To guard against runaway file graphs or oversized assets, the collector enforces
hard bounds:

* **Maximum Entries:** 10,000 files and symlinks (`MAX_BUNDLE_ENTRIES`).
* **Maximum Size:** 512 MiB total uncompressed content (`MAX_BUNDLE_BYTES`).
* **Sub-workflow Depth:** Maximum nesting depth of 10 levels.
* **Cycle Detection:** Recursive sub-workflows and include loops fail with
  `BundleCycleError`.

Exceeding these limits raises `BundleCapsError`.

## Reproducibility Recommendations

### Discovery Content vs. Declared Dependencies

Ambient skill discovery (`runtime.skill_discovery: [personal, project]`) and
ambient plugin detection scan user directories (`~/.copilot/skills`, `~/.claude/skills`).
Because personal directories vary across developer machines and CI agents,
ambient discovery can alter bundle contents.

For deterministic CI builds, declare skills and plugin sources explicitly in the
workflow YAML (`runtime.skills`, `runtime.plugins`, `runtime.plugin_sources`).

### Environment Variables in File Resolution

Environment variable expansion (`${VAR:-default}`) occurs during workflow loading.
If include paths depend on dynamic variables without defaults, different
environments may resolve different files.

Use literal paths or fixed fallback defaults for file includes in shared workflows.

## Boundary with MCP Server Workflow Pinning

Conductor uses different hashing strategies for different purposes:

* **`conductor.mcp.serve.pinning`:** Computes a SHA-256 hash of the root workflow
  file only. This lightweight check verifies that an MCP tool definition has not
  changed on disk since server startup.
* **Run Bundle:** Computes a comprehensive content digest across the entire
  transitive dependency closure, including all partial templates, sub-workflows,
  skills, and assets.

## Relationship to `ResolvedRunManifest`

The execution manifest (`ResolvedRunManifest`) pins how logical execution
profiles map to concrete runner backends.

The `BundleDescriptor` records `run_manifest_digest` to link the file bundle with
its corresponding execution environment. In future work (step 6 of issue #527),
the execution manifest will include `bundle_digest` as a mandatory field,
completing the bidirectional link between execution configuration and bundled
code.

## Storage and Operational Lifecycle

Bundles are published to `$CONDUCTOR_HOME/cache/bundles/sha256-<hex>/`:

```
~/.conductor/cache/bundles/
  sha256-3a1f4b.../                   # Filesystem-safe form
    bundle.json                       # Manifest sentinel (written last)
    bundle.tar.gz                     # Deterministic archive
    tree/                             # Staged file hierarchy
```

### Publication Flow

1. Files are collected in memory and validated.
2. The entry hierarchy is staged in a temporary directory.
3. `bundle.tar.gz` is generated deterministically.
4. Under a per-digest cross-process lock, the temporary directory is renamed
   atomically into the final filesystem-safe digest path.
5. `bundle.json` is written last as the readiness sentinel.

If the target directory already exists and contains a valid `bundle.json`,
publishing is skipped and the cached bundle is reused immediately.

### Cache Growth

In v1, `$CONDUCTOR_HOME/cache/bundles/` grows without automated garbage
collection. Bundles remain cached until manually removed by an operator.
