"""Common base for plugin resolution and manifest failures.

Mirrors :mod:`conductor.skills.errors`: a single base class so a call
site that can trigger more than one kind of plugin failure has one
correct thing to catch, and a ``ValueError`` subclass so these nest
cleanly inside Pydantic field validation.

Kept in a leaf module with no Conductor imports so
:mod:`conductor.plugins.manifest` can be imported from
:mod:`conductor.skills.registry` without closing an import cycle — see
the layering note in :mod:`conductor.plugins`.
"""

from __future__ import annotations


class PluginError(ValueError):
    """Base for every plugin resolution or manifest failure."""


class PluginNotFoundError(PluginError):
    """Raised when a ``plugins:`` entry names nothing resolvable.

    Covers both "no plugin of that name is installed" and "the name is
    ambiguous across marketplaces" — in each case the entry the author
    wrote does not identify exactly one plugin root.
    """


class PluginManifestError(PluginError):
    """Raised when a plugin root is present but unusable.

    A missing or unreadable ``plugin.json``, a manifest declaring no
    usable ``name``, or an ``mcpServers`` declaration that cannot be
    read. Distinct from :class:`PluginNotFoundError` so a user whose
    plugin is broken is told something different from a user whose
    plugin is absent.
    """


class PluginSourceError(PluginError):
    """Raised when a ``plugin_sources:`` entry cannot be understood.

    A source string matching none of the recognised forms, or a
    marketplace whose checkout holds neither a catalog nor a plugin. The
    author wrote something Conductor cannot turn into a place to fetch
    from — distinct from :class:`PluginFetchError`, where the source was
    understood and the fetch itself failed.
    """


class PluginFetchError(PluginError):
    """Raised when acquiring a declared source fails.

    ``git`` missing from ``PATH``, a clone or ``ls-remote`` that failed,
    or a ref that resolves to nothing. Separated from
    :class:`PluginSourceError` because the remedy is different: the
    workflow is right and the machine, network, or credentials are not.
    """


class PluginSourceUnavailableError(PluginNotFoundError):
    """Raised when a declared source has not been acquired on this machine.

    Its own class because it is the one plugin failure that is *not* a
    problem with the workflow. The source is declared, the reference is
    well-formed, and ``conductor run`` will fetch it — only ``conductor
    validate``, which never touches the network, cannot see it yet.
    Callers that would otherwise report a hard error downgrade this one
    to a warning, and say which checks they had to skip.
    """
