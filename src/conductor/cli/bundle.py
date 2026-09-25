"""Typer subcommand group for run bundles.

``conductor bundle build`` is the CI priming verb for the run-bundle
feature: it resolves a workflow (file path or registry reference), collects
its complete statically knowable file closure, compiles the run-invariant
execution manifest, and publishes everything into the content-addressed
bundle store. Network access is legal here — acquisition is the point of
the command, unlike ``conductor validate`` which stays offline.

Everything under ``conductor.bundle`` (and the other heavy imports this
module needs) is imported lazily inside the command body:
``conductor/cli/app.py`` imports every ``cli/*.py`` sub-app module on every
``conductor`` invocation, and ``conductor.bundle`` pulls in the collector,
which in turn imports Jinja2, the plugin/skill registries, and the registry
cache. A top-level import here would pay that cost for every command, not
just ``bundle build`` — the same reasoning documented in ``cli/mcp.py``.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NoReturn

import typer

from conductor.console import make_console, styled

if TYPE_CHECKING:
    from conductor.bundle.model import BundleDescriptor, BundleEntry
    from conductor.registry.resolver import ResolvedRef

console = make_console(stderr=True)
output_console = make_console()

bundle_app = typer.Typer(
    name="bundle",
    help="Build content-addressed run bundles for workflows.",
    no_args_is_help=True,
)


def _fail(exc: Exception) -> NoReturn:
    """Print a hard error the markup-safe way and exit with code 1."""
    console.print(styled("[bold red]Error:[/bold red] {}", exc))
    raise typer.Exit(code=1) from None


@bundle_app.command("build")
def build_bundle(
    workflow: Annotated[
        str,
        typer.Argument(
            help=(
                "Workflow YAML file path or registry reference "
                "(name, name@registry, name@registry#ref)."
            ),
        ),
    ],
    environment: Annotated[
        str | None,
        typer.Option(
            "--environment",
            help=(
                "Execution environment name or path to an environment document. "
                "Default: the built-in local environment."
            ),
        ),
    ] = None,
) -> None:
    """Build a content-addressed run bundle for a workflow.

    Collects the workflow's complete statically knowable file closure —
    includes, Jinja template closures, sub-workflows, skills, plugins, and
    declared assets — and publishes it into the bundle store under its
    content digest. The same content always produces the same digest, so a
    rebuilt bundle reuses the existing store directory.

    The printed report is ephemeral: the descriptor's link fields (run
    manifest digest, workflow digest, environment identity) and provenance
    are never stored in the content-addressed store.

    Plugin sources declared under ``runtime.plugin_sources`` are acquired
    up front, which is what makes this command the CI priming step: after a
    ``bundle build``, no run of the workflow needs the network for plugins.

    \b
    Examples:
        conductor bundle build workflow.yaml
        conductor bundle build workflow.yaml --environment demo
        conductor bundle build 'qa-bot@official#v1.2.3'
    """
    from conductor.registry.cache import resolve_and_fetch
    from conductor.registry.errors import RegistryError
    from conductor.registry.resolver import resolve_ref

    try:
        resolved_ref = resolve_ref(workflow)
        workflow_path = Path(resolve_and_fetch(resolved_ref))
    except RegistryError as exc:
        _fail(exc)

    _build_impl(
        workflow_path,
        authored_ref=workflow,
        resolved_ref=resolved_ref,
        environment_name=environment,
    )


def _build_impl(
    workflow_path: Path,
    *,
    authored_ref: str,
    resolved_ref: ResolvedRef,
    environment_name: str | None,
) -> None:
    """Execute the build after the workflow reference has been resolved."""
    from conductor.bundle import collect_bundle, publish_bundle
    from conductor.bundle.errors import BundleError
    from conductor.bundle.store import bundle_store_path
    from conductor.config.environment import builtin_local_environment, resolve_environment
    from conductor.config.loader import load_config
    from conductor.digest import canonical_json_digest
    from conductor.engine.run_manifest import compile_run_manifest
    from conductor.exceptions import ConfigurationError
    from conductor.plugins.errors import PluginError
    from conductor.plugins.resolution import resolve_plugin_sources

    warnings: list[str] = []
    publish_warnings: list[str] = []
    try:
        # Explicit root-config load (rather than ``load_workflow``): the
        # config object feeds both the plugin-source prefetch and the
        # run-manifest compilation below.
        config = load_config(workflow_path)

        resolved_env = (
            resolve_environment(environment_name, workflow_dir=workflow_path.parent)
            if environment_name is not None
            else builtin_local_environment()
        )

        declared = config.workflow.runtime.plugin_sources
        if declared:
            # Network acquisition is legal here: build is the CI priming
            # verb, mirroring ``cli/run.py::_prefetch_plugin_sources``.
            resolve_plugin_sources(
                declared,
                base_dir=workflow_path.resolve().parent,
                allow_network=True,
                on_warning=warnings.append,
            )

        collected = collect_bundle(
            workflow_path,
            environment=resolved_env,
            allow_network=True,
            on_warning=warnings.append,
        )

        run_manifest = compile_run_manifest(
            config,
            workflow_path=workflow_path,
            environment=resolved_env,
        )
    except (BundleError, ConfigurationError, PluginError, OSError) as exc:
        # OSError too: the plugin/bundle caches are created during
        # acquisition, so an unwritable home or a full disk surfaces here
        # as a bare errno rather than a typed error.
        _fail(exc)

    run_manifest_digest = canonical_json_digest(run_manifest.model_dump(mode="json"))
    descriptor = _with_root_registry_provenance(
        collected.descriptor, authored_ref, resolved_ref, workflow_path
    )
    # The descriptor is printed in the report only — it is never stored in
    # the content-addressed store, so setting the link field here cannot
    # leak provenance into the digest inputs.
    descriptor = descriptor.model_copy(update={"run_manifest_digest": run_manifest_digest})

    # The store directory uses the portable digest key from ``bundle/store.py``. "Reused" is
    # reported only when the pre-existing directory's readiness sentinel
    # survives publish untouched — an invalid directory that is quarantined
    # and rebuilt gets a fresh sentinel (new inode/mtime) and must not be
    # billed as reused.
    store_dir = bundle_store_path(descriptor.bundle_digest)
    sentinel_before = _sentinel_fingerprint(store_dir)
    try:
        final_dir = publish_bundle(
            collected.manifest,
            collected.files,
            collected.links,
            on_warning=publish_warnings.append,
        )
    except OSError as exc:
        _fail(exc)
    reused = sentinel_before is not None and _sentinel_fingerprint(final_dir) == sentinel_before

    _print_report(
        descriptor,
        entries=collected.manifest.entries,
        final_dir=final_dir,
        reused=reused,
        warnings=publish_warnings + warnings,
    )


def _sentinel_fingerprint(store_dir: Path) -> tuple[int, int, int] | None:
    """Fingerprint the readiness sentinel, or ``None`` when there is none.

    ``(st_ino, st_mtime_ns, st_size)`` of ``bundle.json``: a rebuild writes
    a fresh sentinel, so any change in the triple proves the directory was
    not reused. ``st_ino`` may be ``0`` on some platforms; the mtime and
    size still discriminate.
    """
    try:
        stat = (store_dir / "bundle.json").stat()
    except OSError:
        return None
    return (stat.st_ino, stat.st_mtime_ns, stat.st_size)


def _with_root_registry_provenance(
    descriptor: BundleDescriptor,
    authored_ref: str,
    resolved_ref: ResolvedRef,
    workflow_path: Path,
) -> BundleDescriptor:
    """Record the root workflow's own registry resolution in the descriptor.

    The collector only records registry references it resolves itself
    (sub-workflows); the root reference was resolved by this command, so
    the provenance of *how the root was reached* is added here. Only
    registry/adhoc refs carry this: a file path has no resolution to record.
    """
    if resolved_ref.kind not in ("registry", "adhoc"):
        return descriptor

    from conductor.bundle.model import RegistryProvenance
    from conductor.registry.cache import (
        _meta_dir,
        _read_source_metadata,
        find_registry_cache_location,
    )

    location = find_registry_cache_location(workflow_path)
    if location is None:
        return descriptor
    metadata = _read_source_metadata(_meta_dir(location.registry_name, location.sha))
    resolved_sha = metadata.full_sha if metadata is not None else location.sha
    provenance = RegistryProvenance(ref=authored_ref, resolved_sha=resolved_sha)
    if any(entry.ref == authored_ref for entry in descriptor.provenance.registry):
        return descriptor
    registry = [provenance, *descriptor.provenance.registry]
    return descriptor.model_copy(
        update={"provenance": descriptor.provenance.model_copy(update={"registry": registry})}
    )


def _print_report(
    descriptor: BundleDescriptor,
    *,
    entries: tuple[BundleEntry, ...],
    final_dir: Path,
    reused: bool,
    warnings: list[str],
) -> None:
    """Print the ephemeral build report to stdout."""
    counts = Counter(entry.origin_kind for entry in entries)
    total_size = sum(entry.size for entry in entries)

    output_console.print(styled("[bold green]✓[/bold green] Bundle build complete"))
    output_console.print(styled("  [bold]Bundle digest:[/bold]  {}", descriptor.bundle_digest))
    store_line = styled("  [bold]Store:[/bold]         {}", final_dir)
    if reused:
        store_line = styled("{} [dim](reused)[/dim]", store_line)
    output_console.print(store_line)
    output_console.print(styled("  [bold]Archive:[/bold]       {}", final_dir / "bundle.tar.gz"))
    output_console.print(
        styled("  [bold]Files:[/bold]         {} entries, {} bytes", len(entries), total_size)
    )
    for origin_kind in sorted(counts):
        output_console.print(styled("    - {}: {}", origin_kind, counts[origin_kind]))

    git = descriptor.provenance.git
    if git is None:
        output_console.print(styled("  [bold]Git:[/bold]         not a work tree"))
    else:
        head = git.head_sha[:12] if git.head_sha else "unknown"
        remote = git.remote or "no remote"
        output_console.print(styled("  [bold]Git:[/bold]         {} ({})", head, remote))
        if git.dirty:
            output_console.print(
                styled(
                    "  [yellow]⚠[/yellow] {} modified file(s) are part of the bundle",
                    len(git.dirty),
                )
            )

    output_console.print(styled("  [bold]Run manifest:[/bold] {}", descriptor.run_manifest_digest))
    output_console.print(
        styled("  [bold]Workflow:[/bold]     {}", descriptor.workflow_digest or "—")
    )
    env = descriptor.environment
    if env is None:
        output_console.print(styled("  [bold]Environment:[/bold] —"))
    else:
        output_console.print(styled("  [bold]Environment:[/bold] {} ({})", env.name, env.source))
        output_console.print(styled("    digest: {}", env.digest))

    for warning in warnings:
        output_console.print(styled("  [yellow]⚠[/yellow] {}", warning))
    for name in descriptor.incomplete:
        output_console.print(styled("  [yellow]⚠[/yellow] bundle incomplete: {}", name))


__all__ = ["bundle_app"]
