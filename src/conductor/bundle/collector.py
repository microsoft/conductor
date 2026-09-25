"""Collect the complete, statically knowable file closure of a workflow.

The collector is intentionally a composition layer. Workflow loading, skill
discovery, plugin resolution, and registry acquisition remain owned by their
existing modules; this module calls those APIs and turns their resolved paths
into one deterministic bundle namespace.
"""

from __future__ import annotations

import glob
import hashlib
import os
import posixpath
import stat
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal

from jinja2 import Environment, nodes

from conductor.bundle.errors import (
    BundleCapsError,
    BundleCycleError,
    BundleDynamicTemplateError,
    BundleError,
    BundleRootEscapeError,
    BundleSymlinkEscapeError,
    BundleUnfetchedError,
)
from conductor.bundle.model import (
    BundleDescriptor,
    BundleEntry,
    BundleEnvironmentLink,
    BundleManifest,
    BundleProvenance,
    GitProvenance,
    PluginProvenance,
    RegistryProvenance,
    compute_bundle_digest,
)
from conductor.config.environment import ResolvedEnvironment
from conductor.config.loader import IncludedFilesGraph, load_config_with_graph, resolve_env_vars
from conductor.config.schema import (
    AgentDef,
    HumanGateStepDef,
    QuestionsStepDef,
    WorkflowConfig,
    WorkflowStepDef,
)
from conductor.file_string import FileString
from conductor.filesystem import is_dir_strict, is_file_strict, stat_or_none
from conductor.plugins.errors import PluginFetchError, PluginSourceUnavailableError
from conductor.plugins.manifest import (
    PLUGIN_MANIFESTS,
    find_manifest,
    manifest_flavor,
)
from conductor.plugins.registry import ResolvedPlugin, resolve_plugins
from conductor.plugins.resolution import ResolvedSource, marketplaces_from, resolve_plugin_sources
from conductor.providers.capabilities import plugin_flavor_for
from conductor.registry.cache import (
    _meta_dir,
    _read_source_metadata,
    auto_fetch_relative_workflow,
    find_registry_cache_location,
    resolve_and_fetch,
)
from conductor.registry.errors import RegistryError
from conductor.registry.resolver import ResolvedRef, resolve_ref
from conductor.skills import ResolvedSkill, resolve_effective_skills

WarningSink = Callable[[str], None]

# Keep this equal to engine/workflow.py's MAX_SUBWORKFLOW_DEPTH and
# config/validator.py's _MAX_SUBWORKFLOW_VALIDATION_DEPTH.
MAX_SUBWORKFLOW_DEPTH = 10
MAX_BUNDLE_ENTRIES = 10_000
MAX_BUNDLE_BYTES = 512 * 1024 * 1024

_OriginKind = Literal[
    "workflow",
    "include",
    "jinja_include",
    "subworkflow",
    "subworkflow_registry",
    "skill",
    "plugin",
    "asset",
]


@dataclass(frozen=True)
class CollectedBundle:
    """A bundle manifest, descriptor, and materialization payload."""

    entries: tuple[BundleEntry, ...]
    manifest: BundleManifest
    descriptor: BundleDescriptor
    files: Mapping[str, bytes]
    links: Mapping[str, str]


@dataclass(frozen=True)
class _AdditionalRoot:
    path: Path
    index: int
    namespace: str


@dataclass(frozen=True)
class _WorkflowNode:
    path: Path
    config: WorkflowConfig
    graph: IncludedFilesGraph
    registry_ref: str | None


class _Collector:
    def __init__(
        self,
        workflow_path: Path,
        environment: ResolvedEnvironment | None,
        allow_network: bool,
        on_warning: WarningSink,
    ) -> None:
        self.root_workflow = Path(os.path.abspath(os.path.normpath(workflow_path.expanduser())))
        self.root_dir = self.root_workflow.parent
        self.environment = environment
        self.allow_network = allow_network
        self.on_warning = on_warning
        self.entries: dict[str, BundleEntry] = {}
        self.files: dict[str, bytes] = {}
        self.links: dict[str, str] = {}
        self.host_paths: dict[Path, str] = {}
        self.skills_topology: dict[str, str] = {}
        self.skill_claims: dict[
            str, tuple[Literal["declared", "discovered", "plugin"], ResolvedSkill]
        ] = {}
        self.plugins_topology: dict[str, str] = {}
        self.registry_provenance: dict[str, RegistryProvenance] = {}
        self.plugin_provenance: dict[str, PluginProvenance] = {}
        self.incomplete: list[str] = []
        self.warnings: list[str] = []
        self.nodes: list[_WorkflowNode] = []
        self.additional_roots: list[_AdditionalRoot] = []
        self.dependency_roots: set[Path] = set()
        self._total_bytes = 0

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        self.on_warning(message)

    def collect(self) -> CollectedBundle:
        root_config, root_graph = load_config_with_graph(self.root_workflow)
        root_stat = stat_or_none(self.root_workflow)
        if root_stat is None:
            raise BundleError(f"Root workflow file does not exist: {self.root_workflow}")
        self.nodes.append(_WorkflowNode(self.root_workflow, root_config, root_graph, None))
        self._walk_subworkflows(
            self.nodes[0],
            depth=0,
            chain=((root_stat.st_dev, root_stat.st_ino),),
            names=(self.root_workflow.name,),
        )
        self._prepare_additional_roots()

        for index, node in enumerate(self.nodes):
            self._collect_loader_graph(node, root=index == 0)
            self._collect_jinja(node)
            self._collect_assets(node)
            self._collect_agent_dependencies(node)

        discovered = sorted(
            detail.removeprefix("skill-discovery:")
            for entry in self.entries.values()
            if (detail := entry.origin_detail).startswith("skill-discovery:")
        )
        if discovered:
            self.warn(
                "Bundle closure contains machine-dependent discovered skills: "
                + ", ".join(discovered)
            )

        ordered = tuple(sorted(self.entries.values(), key=lambda item: item.logical_path))
        digest = compute_bundle_digest(ordered, self.skills_topology, self.plugins_topology)
        manifest = BundleManifest(
            version=1,
            bundle_digest=digest,
            entries=ordered,
            skills_topology=dict(sorted(self.skills_topology.items())),
            plugins_topology=dict(sorted(self.plugins_topology.items())),
        )
        root_record = self.nodes[0].graph[0]
        descriptor = BundleDescriptor(
            bundle_digest=digest,
            run_manifest_digest=None,
            workflow_digest=root_record.digest,
            environment=(
                BundleEnvironmentLink(
                    name=self.environment.name,
                    source=self.environment.source,
                    digest=self.environment.digest,
                )
                if self.environment is not None
                else None
            ),
            provenance=BundleProvenance(
                git=self._git_provenance(),
                registry=list(self.registry_provenance.values()),
                plugins=list(self.plugin_provenance.values()),
            ),
            incomplete=sorted(set(self.incomplete)),
            warnings=list(self.warnings),
        )
        return CollectedBundle(
            entries=ordered,
            manifest=manifest,
            descriptor=descriptor,
            files=MappingProxyType(dict(self.files)),
            links=MappingProxyType(dict(self.links)),
        )

    def _walk_subworkflows(
        self,
        node: _WorkflowNode,
        *,
        depth: int,
        chain: tuple[tuple[int, int], ...],
        names: tuple[str, ...],
    ) -> None:
        for step in self._workflow_steps(node.config):
            if depth >= MAX_SUBWORKFLOW_DEPTH:
                raise BundleError(
                    f"Sub-workflow depth cap ({MAX_SUBWORKFLOW_DEPTH}) exceeded at "
                    f"{step.workflow!r}. Reduce workflow nesting."
                )
            sub_path, resolved = self._resolve_subworkflow(step.workflow, node.path.parent)
            info = stat_or_none(sub_path)
            if info is None:
                raise BundleError(f"Sub-workflow file does not exist: {sub_path}")
            identity = (info.st_dev, info.st_ino)
            if identity in chain:
                cycle = " -> ".join((*names, sub_path.name))
                raise BundleCycleError(f"Circular sub-workflow reference detected: {cycle}")
            config, graph = load_config_with_graph(sub_path)
            registry_ref = step.workflow if resolved.kind in {"registry", "adhoc"} else None
            child = _WorkflowNode(sub_path, config, graph, registry_ref)
            self.nodes.append(child)
            if registry_ref is not None:
                self._record_registry_provenance(registry_ref, sub_path)
            self._walk_subworkflows(
                child,
                depth=depth + 1,
                chain=(*chain, identity),
                names=(*names, sub_path.name),
            )

    @staticmethod
    def _workflow_steps(config: WorkflowConfig) -> Iterable[WorkflowStepDef]:
        for step in config.agents:
            if isinstance(step, WorkflowStepDef):
                yield step
        for group in config.for_each:
            if isinstance(group.agent, WorkflowStepDef):
                yield group.agent

    def _resolve_subworkflow(self, reference: str, base_dir: Path) -> tuple[Path, ResolvedRef]:
        candidate = Path(os.path.abspath(os.path.normpath(base_dir / reference)))
        if is_file_strict(candidate):
            return candidate, ResolvedRef(kind="file", path=candidate)

        looks_like_file = "@" not in reference and (
            "/" in reference or "\\" in reference or candidate.suffix.lower() in {".yaml", ".yml"}
        )
        if looks_like_file:
            if not self.allow_network:
                if find_registry_cache_location(candidate) is not None:
                    raise BundleUnfetchedError(
                        f"Sub-workflow {reference!r} is missing from the registry cache and "
                        "network access is disabled.",
                        suggestion=(
                            "Fetch the parent workflow with network access to prime its siblings."
                        ),
                    )
            else:
                try:
                    fetched = auto_fetch_relative_workflow(candidate)
                except RegistryError as exc:
                    raise BundleUnfetchedError(
                        f"Failed to fetch relative sub-workflow {reference!r}: {exc}"
                    ) from exc
                if fetched is not None and is_file_strict(fetched):
                    return fetched, ResolvedRef(kind="file", path=fetched)

        try:
            resolved = resolve_ref(reference)
            if resolved.kind == "file":
                raise BundleError(f"Sub-workflow file not found: {candidate}")
            return resolve_and_fetch(resolved, allow_network=self.allow_network), resolved
        except RegistryError as exc:
            raise BundleUnfetchedError(
                f"Sub-workflow {reference!r} is not available for bundling: {exc}"
            ) from exc

    def _record_registry_provenance(self, reference: str, path: Path) -> None:
        location = find_registry_cache_location(path)
        if location is None:
            return
        metadata = _read_source_metadata(_meta_dir(location.registry_name, location.sha))
        resolved_sha = metadata.full_sha if metadata is not None else location.sha
        self.registry_provenance.setdefault(
            reference, RegistryProvenance(ref=reference, resolved_sha=resolved_sha)
        )

    def _prepare_additional_roots(self) -> None:
        declaration_index = 0
        for node in self.nodes:
            bundle = node.config.workflow.bundle
            for authored in bundle.additional_roots if bundle is not None else ():
                expanded = Path(authored).expanduser()
                if not expanded.is_absolute():
                    expanded = node.path.parent / expanded
                root = Path(os.path.abspath(os.path.normpath(expanded)))
                if not is_dir_strict(root):
                    raise BundleError(
                        f"Declared workflow.bundle.additional_roots entry {authored!r} "
                        f"does not exist or is not a directory: {root}"
                    )
                self.additional_roots.append(
                    _AdditionalRoot(
                        path=root,
                        index=declaration_index,
                        namespace=f"tree/roots/{declaration_index:02d}",
                    )
                )
                declaration_index += 1

    def _collect_loader_graph(self, node: _WorkflowNode, *, root: bool) -> None:
        for index, record in enumerate(node.graph):
            if index == 0:
                if root:
                    kind: _OriginKind = "workflow"
                    detail = "workflow:root"
                elif find_registry_cache_location(record.path) is not None:
                    kind = "subworkflow_registry"
                    detail = f"subworkflow_registry:{node.registry_ref or record.path.name}"
                else:
                    kind = "subworkflow"
                    detail = f"subworkflow:{record.path.name}"
            else:
                kind = "include"
                detail = f"include:{record.logical_ref}"
            anchored_path = record.anchored_path or record.path
            if anchored_path != record.path:
                self._collect_symlink_components(anchored_path, kind, detail)
                if anchored_path.is_symlink():
                    self._collect_local_path(anchored_path, kind, detail, recursive=True)
                else:
                    self._stage_local(record.path, kind, detail)
            else:
                self._stage_local(record.path, kind, detail)

    def _collect_jinja(self, node: _WorkflowNode) -> None:
        visited: set[tuple[Path, Path]] = set()
        for value in self._file_strings(node.config):
            records = [
                record
                for record in node.graph
                if record.tag == "file" and record.path == value.source_path
            ]
            if not records:
                root = Path(os.path.abspath(os.path.normpath(value.source_path.parent)))
                self._scan_template(value.source_path, root, visited)
                continue
            for record in records:
                anchored_path = record.anchored_path or record.path
                self._scan_template(record.path, anchored_path.parent, visited)

    @staticmethod
    def _file_strings(config: WorkflowConfig) -> Iterable[FileString]:
        steps = [*config.agents, *(group.agent for group in config.for_each)]
        for step in steps:
            if isinstance(step, AgentDef):
                for value in (step.prompt, step.system_prompt):
                    if isinstance(value, FileString):
                        yield value
            elif isinstance(step, (HumanGateStepDef, QuestionsStepDef)) and isinstance(
                step.prompt, FileString
            ):
                yield step.prompt

    def _scan_template(
        self, path: Path, search_root: Path, visited: set[tuple[Path, Path]]
    ) -> None:
        normalized = Path(os.path.abspath(os.path.normpath(path)))
        identity = (
            Path(os.path.realpath(normalized)),
            Path(os.path.realpath(Path(os.path.abspath(os.path.normpath(search_root))))),
        )
        if identity in visited:
            return
        visited.add(identity)
        try:
            source = resolve_env_vars(normalized.read_bytes().decode("utf-8"))
        except OSError as exc:
            raise BundleError(f"Jinja prompt file could not be read: {normalized}: {exc}") from exc
        tree = Environment().parse(source)
        for node in tree.find_all((nodes.Extends, nodes.Import, nodes.FromImport, nodes.Include)):
            construct = type(node).__name__.lower()
            targets = self._template_targets(node, normalized, construct)
            existing: list[Path] = []
            for target in targets:
                candidate = Path(os.path.abspath(os.path.normpath(search_root / target)))
                if is_file_strict(candidate):
                    existing.append(candidate)
            ignore_missing = isinstance(node, nodes.Include) and node.ignore_missing
            if not existing and not ignore_missing:
                rendered = ", ".join(repr(target) for target in targets)
                raise BundleError(
                    f"Jinja {construct} in prompt {normalized} references missing template "
                    f"{rendered}."
                )
            for candidate in existing:
                self._stage_local(
                    candidate,
                    "jinja_include",
                    f"jinja_include:{candidate.relative_to(search_root).as_posix()}",
                )
                self._scan_template(candidate, search_root, visited)

    @staticmethod
    def _template_targets(
        node: nodes.Extends | nodes.Import | nodes.FromImport | nodes.Include,
        prompt: Path,
        construct: str,
    ) -> list[str]:
        template = node.template
        if isinstance(template, nodes.Const) and isinstance(template.value, str):
            return [template.value]
        if isinstance(node, nodes.Include) and isinstance(template, (nodes.List, nodes.Tuple)):
            values: list[str] = []
            for item in template.items:
                if not isinstance(item, nodes.Const) or not isinstance(item.value, str):
                    break
                values.append(item.value)
            else:
                return values
        raise BundleDynamicTemplateError(
            f"Dynamic Jinja {construct} in prompt {prompt} at line {node.lineno} cannot be "
            "bundled statically.",
            suggestion="Use a literal template name or a static list of literal names.",
            file_path=str(prompt),
            line_number=node.lineno,
        )

    def _collect_assets(self, node: _WorkflowNode) -> None:
        bundle = node.config.workflow.bundle
        if bundle is None:
            return
        for pattern_index, pattern in enumerate(bundle.assets):
            detail = f"asset:{pattern_index}"
            expanded = glob.glob(
                str(node.path.parent / pattern), recursive=True, include_hidden=False
            )
            for raw in sorted(expanded):
                path = Path(raw)
                if path.is_symlink():
                    self._collect_local_path(path, "asset", detail, recursive=True)
                elif is_file_strict(path):
                    self._stage_local(path, "asset", detail)
                elif is_dir_strict(path) and not glob.has_magic(pattern):
                    self._collect_local_path(path, "asset", detail, recursive=True)

    def _collect_agent_dependencies(self, node: _WorkflowNode) -> None:
        runtime = node.config.workflow.runtime
        source_results: dict[str, ResolvedSource] = {}
        unavailable: set[str] = set()
        for name, source in runtime.plugin_sources.items():
            try:
                source_results.update(
                    resolve_plugin_sources(
                        {name: source},
                        base_dir=node.path.parent,
                        allow_network=self.allow_network,
                        on_warning=self.warn,
                    )
                )
            except PluginFetchError as exc:
                if self.allow_network:
                    raise
                unavailable.add(name)
                self.incomplete.append(f"plugin-source:{name}")
                self.warn(f"Plugin source {name!r} is not cached: {exc}")

        marketplaces = marketplaces_from(source_results)
        steps = [*node.config.agents, *(group.agent for group in node.config.for_each)]
        for step in steps:
            if not isinstance(step, AgentDef):
                continue
            flavor = plugin_flavor_for(step.provider or runtime.provider.name)
            if step.skills is None:
                skill_entries = list(runtime.skills)
                discovery = runtime.skill_discovery
                sources = tuple(discovery.sources)
                exclude = tuple(discovery.exclude)
            else:
                skill_entries = list(step.skills)
                sources = ()
                exclude = ()
            if skill_entries or sources:
                skills = resolve_effective_skills(
                    skill_entries,
                    sources=sources,
                    exclude=exclude,
                    base_dir=node.path.parent,
                    on_warning=self.warn,
                )
                for skill in skills:
                    self._collect_skill(skill)

            plugin_entries = (
                list(step.plugins) if step.plugins is not None else list(runtime.plugins)
            )
            if unavailable:
                plugin_entries = [
                    entry
                    for entry in plugin_entries
                    if not self._uses_unavailable_source(entry.name, unavailable)
                ]
            if not plugin_entries:
                continue
            try:
                plugins = resolve_plugins(
                    plugin_entries,
                    base_dir=node.path.parent,
                    marketplaces=marketplaces,
                    declared_sources=unavailable,
                    flavor=flavor,
                    on_warning=self.warn,
                )
            except PluginSourceUnavailableError:
                continue
            for plugin in plugins:
                self._collect_plugin(plugin, flavor, source_results)

    @staticmethod
    def _uses_unavailable_source(entry: str, unavailable: set[str]) -> bool:
        return "@" in entry and entry.rsplit("@", 1)[1] in unavailable

    def _collect_skill(self, skill: ResolvedSkill) -> None:
        namespace = f"tree/skills/{skill.name}"
        self.dependency_roots.add(skill.directory)
        detail = f"skill-discovery:{skill.name}" if skill.discovered else f"skill:{skill.name}"
        self._collect_dependency_tree(
            skill.directory,
            namespace,
            mapping_root=skill.directory,
            mapping_namespace=namespace,
            kind="skill",
            detail=detail,
        )
        topology = f"{namespace}/SKILL.md"
        previous = self.skill_claims.get(skill.name)
        if previous is not None and previous[0] == "plugin":
            plugin_skill = previous[1]
            if plugin_skill.directory != skill.directory:
                if skill.discovered:
                    return
                self.warn(
                    f"skill {plugin_skill.name!r} from plugin {plugin_skill.source!r} is "
                    f"shadowed by the declared skill {skill.source!r} and was not enabled."
                )
                self.skills_topology[skill.name] = topology
                self.skill_claims[skill.name] = ("declared", skill)
                return
        self._set_topology(self.skills_topology, skill.name, topology, "skill")
        self.skill_claims[skill.name] = (
            "discovered" if skill.discovered else "declared",
            skill,
        )

    def _collect_plugin(
        self,
        plugin: ResolvedPlugin,
        requested_flavor: Literal["copilot", "claude"] | None,
        sources: Mapping[str, ResolvedSource],
    ) -> None:
        manifest = find_manifest(plugin.root, prefer=requested_flavor)
        if manifest is None:
            raise BundleError(f"Resolved plugin {plugin.name!r} has no manifest at {plugin.root}")
        actual_flavor = manifest_flavor(manifest, plugin.root)
        namespace = f"tree/plugins/{plugin.name}"
        self.dependency_roots.add(plugin.root)
        matching_source = next(
            (
                source
                for source in sources.values()
                if plugin.root.is_relative_to(source.marketplace.root)
            ),
            None,
        )
        if matching_source is not None:
            origin = "cache" if matching_source.sha is not None else "path"
            sha = matching_source.sha
        elif plugin.source.startswith((".", "~", "/")) or "\\" in plugin.source:
            origin, sha = "path", None
        else:
            origin, sha = "installed", None
        origin_detail = f"plugin-{origin}:{plugin.name}"
        selected: set[Path] = set()
        for relative in PLUGIN_MANIFESTS:
            candidate = plugin.root / relative
            if is_file_strict(candidate):
                selected.add(candidate)
        if plugin.mcp_servers and plugin.mcp_source is not None:
            selected.add(plugin.mcp_source)
            if not plugin.mcp_source.is_relative_to(plugin.root):
                self.dependency_roots.add(plugin.mcp_source)
        selected.update(agent.path for agent in plugin.agents)
        for path in sorted(selected, key=lambda item: item.as_posix()):
            relative = os.path.relpath(path, plugin.root).replace("\\", "/")
            logical = PurePosixPath(posixpath.normpath(f"{namespace}/{relative}")).as_posix()
            if not logical.startswith("tree/"):
                raise BundleRootEscapeError(
                    f"Plugin dependency {path} cannot be mapped inside the bundle tree."
                )
            mapping_root, mapping_namespace = self._plugin_dependency_mapping(
                path, plugin.root, namespace, logical
            )
            self._collect_dependency_tree(
                path,
                logical,
                mapping_root=mapping_root,
                mapping_namespace=mapping_namespace,
                kind="plugin",
                detail=origin_detail,
            )
        for skill in plugin.skills:
            skill_namespace = f"{namespace}/{skill.directory.relative_to(plugin.root).as_posix()}"
            self._collect_dependency_tree(
                skill.directory,
                skill_namespace,
                mapping_root=plugin.root,
                mapping_namespace=namespace,
                kind="plugin",
                detail=origin_detail,
            )
        manifest_logical = f"{namespace}/{manifest.relative_to(plugin.root).as_posix()}"
        self._set_topology(self.plugins_topology, plugin.name, manifest_logical, "plugin")
        for skill in plugin.skills:
            self._set_plugin_skill_topology(
                skill,
                f"{namespace}/{skill.directory.relative_to(plugin.root).as_posix()}/SKILL.md",
            )

        self.plugin_provenance.setdefault(
            plugin.name,
            PluginProvenance(
                name=plugin.name,
                flavor=actual_flavor,
                origin=origin,
                sha=sha,
            ),
        )

    @staticmethod
    def _plugin_dependency_mapping(
        path: Path, plugin_root: Path, plugin_namespace: str, logical_path: str
    ) -> tuple[Path, str]:
        normalized = Path(os.path.abspath(os.path.normpath(path)))
        normalized_root = Path(os.path.abspath(os.path.normpath(plugin_root)))
        if normalized.is_relative_to(normalized_root):
            return plugin_root, plugin_namespace
        relative = PurePosixPath(os.path.relpath(normalized, normalized_root).replace("\\", "/"))
        parent_steps = sum(1 for part in relative.parts if part == "..")
        namespace_parts = PurePosixPath(plugin_namespace).parts
        namespace = PurePosixPath(
            *namespace_parts[: len(namespace_parts) - parent_steps]
        ).as_posix()
        root = plugin_root
        for _ in range(parent_steps):
            root = root.parent
        return root, namespace

    def _set_plugin_skill_topology(self, skill: ResolvedSkill, path: str) -> None:
        previous = self.skill_claims.get(skill.name)
        if previous is None:
            self.skills_topology[skill.name] = path
            self.skill_claims[skill.name] = ("plugin", skill)
            return
        previous_kind, previous_skill = previous
        if previous_skill.directory == skill.directory:
            return
        if previous_kind == "discovered":
            self.warn(
                f"skill {skill.name!r} discovered in {previous_skill.source!r} was superseded "
                f"by the copy from plugin {skill.source!r}, which this workflow names explicitly."
            )
            self.skills_topology[skill.name] = path
            self.skill_claims[skill.name] = ("plugin", skill)
            return
        if previous_kind == "declared":
            self.warn(
                f"skill {skill.name!r} from plugin {skill.source!r} is shadowed by the "
                f"declared skill {previous_skill.source!r} and was not enabled."
            )
            return
        self._set_topology(self.skills_topology, skill.name, path, "skill")

    @staticmethod
    def _set_topology(table: dict[str, str], name: str, path: str, kind: str) -> None:
        previous = table.get(name)
        if previous is not None and previous != path:
            raise BundleError(
                f"Two resolved {kind}s named {name!r} map to different bundle paths: "
                f"{previous!r} and {path!r}."
            )
        table[name] = path

    def _collect_dependency_tree(
        self,
        path: Path,
        logical_path: str,
        *,
        mapping_root: Path,
        mapping_namespace: str,
        kind: _OriginKind,
        detail: str,
        active_directories: tuple[Path, ...] = (),
    ) -> None:
        normalized = Path(os.path.abspath(os.path.normpath(path)))
        if normalized.is_symlink():
            target = Path(os.path.realpath(normalized))
            if not self._inside_authorized_root(target):
                raise BundleSymlinkEscapeError(
                    f"Symlink {normalized} points outside every authorized root to {target}.",
                    suggestion="Move the target inside an authorized root or declare its root.",
                )
            self._stage(
                normalized,
                logical_path,
                kind,
                detail,
                mapping_root=mapping_root,
                mapping_namespace=mapping_namespace,
            )
            try:
                target_relative = target.relative_to(Path(os.path.realpath(mapping_root)))
            except ValueError as exc:
                raise BundleRootEscapeError(
                    f"Bundle symlink target {target} has no relocatable mapping under "
                    f"dependency root {mapping_root}.",
                    file_path=str(target),
                ) from exc
            self._collect_dependency_tree(
                target,
                f"{mapping_namespace}/{target_relative.as_posix()}",
                mapping_root=mapping_root,
                mapping_namespace=mapping_namespace,
                kind=kind,
                detail=detail,
                active_directories=active_directories,
            )
            return

        if is_dir_strict(normalized):
            identity = Path(os.path.realpath(normalized))
            if identity in active_directories:
                chain = " -> ".join(str(item) for item in (*active_directories, identity))
                raise BundleCycleError(f"Circular bundle directory traversal detected: {chain}")
            active = (*active_directories, identity)
            try:
                children = sorted(normalized.iterdir(), key=lambda item: item.name)
            except OSError as exc:
                raise BundleError(
                    f"Bundle directory could not be read: {normalized}: {exc}"
                ) from exc
            for child in children:
                self._collect_dependency_tree(
                    child,
                    f"{logical_path}/{child.name}",
                    mapping_root=mapping_root,
                    mapping_namespace=mapping_namespace,
                    kind=kind,
                    detail=detail,
                    active_directories=active,
                )
            return

        self._stage(normalized, logical_path, kind, detail)

    def _collect_local_path(
        self,
        path: Path,
        kind: _OriginKind,
        detail: str,
        *,
        recursive: bool,
        active_directories: tuple[Path, ...] = (),
    ) -> None:
        """Stage a local dependency and the closure reached through symlinks."""
        normalized = Path(os.path.abspath(os.path.normpath(path)))
        if normalized.is_symlink():
            target = Path(os.path.realpath(normalized))
            if not self._inside_authorized_root(target):
                raise BundleSymlinkEscapeError(
                    f"Symlink {normalized} points outside every authorized root to {target}.",
                    suggestion="Move the target inside an authorized root or declare its root.",
                )
            self._stage_local(normalized, kind, detail)
            if is_dir_strict(target):
                if target in active_directories:
                    chain = " -> ".join(str(item) for item in (*active_directories, target))
                    raise BundleCycleError(f"Circular bundle symlink detected: {chain}")
                if recursive:
                    self._collect_local_path(
                        target,
                        kind,
                        detail,
                        recursive=True,
                        active_directories=active_directories,
                    )
            elif is_file_strict(target):
                self._stage_local(target, kind, detail)
            else:
                raise BundleError(f"Bundle symlink target does not exist: {target}")
            return

        if is_dir_strict(normalized):
            if not recursive:
                return
            identity = Path(os.path.realpath(normalized))
            if identity in active_directories:
                chain = " -> ".join(str(item) for item in (*active_directories, identity))
                raise BundleCycleError(f"Circular bundle directory traversal detected: {chain}")
            active = (*active_directories, identity)
            try:
                children = sorted(normalized.iterdir(), key=lambda item: item.name)
            except OSError as exc:
                raise BundleError(
                    f"Bundle directory could not be read: {normalized}: {exc}"
                ) from exc
            for child in children:
                self._collect_local_path(
                    child,
                    kind,
                    detail,
                    recursive=True,
                    active_directories=active,
                )
            return

        self._stage_local(normalized, kind, detail)

    def _collect_symlink_components(self, path: Path, kind: _OriginKind, detail: str) -> None:
        normalized = Path(os.path.abspath(os.path.normpath(path)))
        roots = [self.root_dir, *(root.path for root in self.additional_roots)]
        containing = [root for root in roots if normalized.is_relative_to(root)]
        if not containing:
            return
        root = max(containing, key=lambda candidate: len(candidate.parts))
        current = root
        for part in normalized.relative_to(root).parts[:-1]:
            current /= part
            if current.is_symlink():
                self._collect_local_path(current, kind, detail, recursive=True)

    def _stage_local(self, path: Path, kind: _OriginKind, detail: str) -> None:
        logical, root_detail = self._local_logical_path(path)
        if root_detail is not None:
            detail = f"{detail};additional_root={root_detail}"
        self._stage(path, logical, kind, detail)

    def _local_logical_path(self, path: Path) -> tuple[str, str | None]:
        normalized = Path(os.path.abspath(os.path.normpath(path)))
        location = find_registry_cache_location(normalized)
        if location is not None:
            relative = normalized.relative_to(location.sha_root).as_posix()
            return f"tree/registry/{location.registry_name}/{location.sha}/{relative}", None
        try:
            relative = normalized.relative_to(self.root_dir).as_posix()
            return f"tree/main/{relative}", None
        except ValueError:
            pass
        for root in self.additional_roots:
            try:
                relative = normalized.relative_to(root.path).as_posix()
                return f"{root.namespace}/{relative}", root.namespace.removeprefix("tree/roots/")
            except ValueError:
                continue
        raise BundleRootEscapeError(
            f"Bundle dependency {normalized} escapes every authorized root.",
            suggestion="Declare the root in workflow.bundle.additional_roots.",
            file_path=str(normalized),
        )

    def _stage(
        self,
        path: Path,
        logical_path: str,
        origin_kind: _OriginKind,
        origin_detail: str,
        *,
        mapping_root: Path | None = None,
        mapping_namespace: str | None = None,
    ) -> None:
        normalized = Path(os.path.abspath(os.path.normpath(path)))
        self._assert_authorized(normalized)
        logical = PurePosixPath(logical_path).as_posix()
        if normalized.is_symlink():
            resolved_target = Path(os.path.realpath(normalized))
            if not self._inside_authorized_root(resolved_target):
                raise BundleSymlinkEscapeError(
                    f"Symlink {normalized} points outside every authorized root to "
                    f"{resolved_target}.",
                    suggestion="Move the target inside an authorized root or declare its root.",
                )
            target_logical = self._logical_target_for_symlink(
                normalized,
                logical,
                resolved_target,
                mapping_root=mapping_root,
                mapping_namespace=mapping_namespace,
            )
            target = PurePosixPath(
                posixpath.relpath(target_logical, PurePosixPath(logical).parent.as_posix())
            ).as_posix()
            entry = BundleEntry.for_symlink(
                logical,
                target,
                origin_kind=origin_kind,
                origin_detail=origin_detail,
            )
            payload: bytes | str = target
        else:
            info = stat_or_none(normalized)
            if info is None or not stat.S_ISREG(info.st_mode):
                raise BundleError(f"Bundle dependency is not a regular file: {normalized}")
            raw = normalized.read_bytes()
            entry = BundleEntry(
                logical_path=logical,
                kind="file",
                digest=f"sha256:{hashlib.sha256(raw).hexdigest()}",
                size=len(raw),
                executable=bool(info.st_mode & stat.S_IXUSR),
                link_target=None,
                origin_kind=origin_kind,
                origin_detail=origin_detail,
            )
            payload = raw
        previous = self.entries.get(logical)
        if previous is not None:
            if previous.digest != entry.digest or previous.kind != entry.kind:
                raise BundleError(
                    f"Bundle logical path {logical!r} has contradictory content: "
                    f"{previous.digest} versus {entry.digest}."
                )
            return
        self.entries[logical] = entry
        self.host_paths[normalized] = logical
        if entry.kind == "file":
            assert isinstance(payload, bytes)
            self.files[logical] = payload
            self._total_bytes += len(payload)
        else:
            assert isinstance(payload, str)
            self.links[logical] = payload
        self._check_caps()

    def _logical_target_for_symlink(
        self,
        link: Path,
        logical: str,
        target: Path,
        *,
        mapping_root: Path | None = None,
        mapping_namespace: str | None = None,
    ) -> str:
        if mapping_root is not None and mapping_namespace is not None:
            try:
                relative = target.relative_to(Path(os.path.realpath(mapping_root)))
            except ValueError:
                pass
            else:
                return f"{mapping_namespace}/{relative.as_posix()}"
        try:
            return self._local_logical_path(target)[0]
        except BundleRootEscapeError:
            pass
        for root in sorted(self.dependency_roots, key=lambda item: len(item.parts), reverse=True):
            try:
                link_relative = link.relative_to(root)
                target_relative = target.relative_to(Path(os.path.realpath(root)))
            except ValueError:
                continue
            logical_parts = PurePosixPath(logical).parts
            prefix_length = len(logical_parts) - len(link_relative.parts)
            prefix = PurePosixPath(*logical_parts[:prefix_length])
            return (prefix / PurePosixPath(target_relative.as_posix())).as_posix()
        raise BundleRootEscapeError(
            f"Bundle symlink target {target} has no relocatable logical mapping.",
            file_path=str(target),
        )

    def _assert_authorized(self, path: Path) -> None:
        if find_registry_cache_location(path) is not None:
            return
        candidate = path if path.is_symlink() else Path(os.path.realpath(path))
        roots = [
            self.root_dir,
            *(root.path for root in self.additional_roots),
            *self.dependency_roots,
        ]
        if not any(candidate.is_relative_to(root) for root in roots):
            raise BundleRootEscapeError(
                f"Bundle dependency {path} escapes every authorized root.",
                suggestion="Declare the root in workflow.bundle.additional_roots.",
            )

    def _inside_authorized_root(self, path: Path) -> bool:
        target = Path(os.path.realpath(path))
        if find_registry_cache_location(target) is not None:
            return True
        roots = [
            self.root_dir,
            *(root.path for root in self.additional_roots),
            *self.dependency_roots,
        ]
        return any(target.is_relative_to(Path(os.path.realpath(root))) for root in roots)

    def _check_caps(self) -> None:
        if len(self.entries) > MAX_BUNDLE_ENTRIES:
            raise BundleCapsError(
                f"Bundle entry cap exceeded: maximum {MAX_BUNDLE_ENTRIES}, "
                f"actual {len(self.entries)}."
            )
        if self._total_bytes > MAX_BUNDLE_BYTES:
            raise BundleCapsError(
                f"Bundle byte cap exceeded: maximum {MAX_BUNDLE_BYTES}, actual {self._total_bytes}."
            )

    def _git_provenance(self) -> GitProvenance | None:
        root_text = self._run_git("rev-parse", "--show-toplevel")
        if root_text is None:
            return None
        git_root = Path(root_text)
        head = self._run_git("rev-parse", "HEAD", cwd=git_root)
        remote = self._run_git("remote", "get-url", "origin", cwd=git_root)
        candidates: dict[str, str] = {}
        for host, logical in self.host_paths.items():
            try:
                candidates[host.relative_to(git_root).as_posix()] = logical
            except ValueError:
                continue
        dirty: list[str] = []
        if candidates:
            output = self._run_git(
                "status", "--porcelain=v1", "-z", "--", *sorted(candidates), cwd=git_root
            )
            if output is not None:
                records = output.split("\0")
                index = 0
                modified: set[str] = set()
                while index < len(records):
                    record = records[index]
                    index += 1
                    if not record:
                        continue
                    status_code = record[:2]
                    modified.add(record[3:])
                    if status_code[0] in {"R", "C"} and index < len(records):
                        modified.add(records[index])
                        index += 1
                dirty = sorted(candidates[path] for path in modified if path in candidates)
        return GitProvenance(head_sha=head, remote=remote, dirty=dirty)

    def _run_git(self, *args: str, cwd: Path | None = None) -> str | None:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=cwd or self.root_dir,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.rstrip("\n")


def collect_bundle(
    workflow_path: Path,
    *,
    environment: ResolvedEnvironment | None,
    allow_network: bool,
    on_warning: WarningSink,
) -> CollectedBundle:
    """Collect a workflow's complete statically knowable file closure.

    Args:
        workflow_path: Root workflow YAML file.
        environment: Resolved execution environment link, if one was selected.
        allow_network: Whether registry and plugin-source cache misses may fetch.
        on_warning: Sink receiving every non-fatal diagnostic.

    Returns:
        Immutable entries/models plus regular-file and symlink payload maps.

    Raises:
        BundleError: If the closure is incomplete for any reason other than
            an offline plugin-source cache miss.
    """
    return _Collector(workflow_path, environment, allow_network, on_warning).collect()
