"""Typed failures raised while collecting a run bundle."""

from __future__ import annotations

from conductor.exceptions import ConfigurationError


class BundleError(ConfigurationError):
    """Base class for failures that prevent a complete run bundle."""


class BundleDynamicTemplateError(BundleError):
    """Raised when a Jinja dependency cannot be determined statically."""


class BundleRootEscapeError(BundleError):
    """Raised when local content lies outside every authorized root."""


class BundleSymlinkEscapeError(BundleError):
    """Raised when a symlink resolves outside every authorized root."""


class BundleCapsError(BundleError):
    """Raised when the collected closure exceeds an entry or byte cap."""


class BundleCycleError(BundleError):
    """Raised when the sub-workflow graph contains an inode cycle."""


class BundleUnfetchedError(BundleError):
    """Raised when an offline registry dependency is not cached."""
