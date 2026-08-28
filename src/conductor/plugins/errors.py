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


class PluginAgentFrontmatterError(PluginManifestError):
    """Raised when an agent candidate file has no YAML frontmatter at all.

    A subclass of :class:`PluginManifestError` so every existing
    ``except PluginManifestError`` handler still catches it unchanged.
    Its own class exists so callers can tell "this file has no
    frontmatter" apart from every other kind of manifest failure — the
    one signal that distinguishes a plain-``.md`` doc file (a README, a
    changelog) from an actual agent definition. Every other failure
    (unparseable frontmatter, a missing ``name``, an empty body) stays a
    hard :class:`PluginManifestError`, because those files already
    claimed to be an agent by having *some* frontmatter.
    """


class PluginNotAnAgentError(PluginManifestError):
    """Raised when a ``*.md`` candidate reads as documentation, not an agent.

    A subclass of :class:`PluginManifestError` so every existing ``except
    PluginManifestError`` handler still catches it unchanged. Its own
    class exists so callers can tell "this is a doc file, not a broken
    agent" apart from every other kind of manifest failure. Under the
    Claude build's bare-``*.md`` candidate rule, an ordinary documentation
    file (a README, a Docusaurus/Jekyll/Hugo/MkDocs page) commonly has its
    *own* YAML frontmatter — a ``title:``, a ``sidebar_position:`` — so it
    is not distinguishable from a broken agent by "has frontmatter at
    all" the way :class:`PluginAgentFrontmatterError` distinguishes a
    file with none. It is distinguishable by declaring *neither* ``name``
    nor ``description``: a file declaring exactly one of the two has
    already claimed to be an agent and stays a hard
    :class:`PluginManifestError`.
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

    git_output: str = ""
    """Full redacted output of the failing ``git`` command, when there was
    one.

    The message carries a single summarised line, which is what a user
    should read; classifying a failure needs every line git printed.
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
