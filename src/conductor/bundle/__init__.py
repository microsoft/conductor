"""Run bundles: content-addressed, self-contained workflow closures.

A *run bundle* is the immutable, content-addressed artifact holding every
file a workflow run depends on — the workflow itself, its includes and
sub-workflows, Jinja template closures, skills, plugins, and declared
assets — laid out under logical POSIX paths inside a uniform ``tree/``
namespace. The bundle digest is computed from the content zone only
(entries and topologies); provenance and link fields live in the ephemeral
:class:`BundleDescriptor` and are never stored in the content-addressed
store.

This package is not a leaf (the collector, store, and archive modules live
alongside ``model``), so re-exports are allowed here and there are no
import cycles.
"""

from conductor.bundle.archive import write_bundle_archive
from conductor.bundle.collector import CollectedBundle, WarningSink, collect_bundle
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
    serialize_manifest,
)
from conductor.bundle.store import (
    bundle_store_base,
    bundle_store_key,
    bundle_store_path,
    publish_bundle,
)

__all__ = [
    "BundleDescriptor",
    "BundleCapsError",
    "BundleCycleError",
    "BundleDynamicTemplateError",
    "BundleEntry",
    "BundleError",
    "BundleEnvironmentLink",
    "BundleManifest",
    "BundleProvenance",
    "BundleRootEscapeError",
    "BundleSymlinkEscapeError",
    "BundleUnfetchedError",
    "CollectedBundle",
    "GitProvenance",
    "PluginProvenance",
    "RegistryProvenance",
    "WarningSink",
    "bundle_store_base",
    "bundle_store_key",
    "bundle_store_path",
    "collect_bundle",
    "compute_bundle_digest",
    "publish_bundle",
    "serialize_manifest",
    "write_bundle_archive",
]
