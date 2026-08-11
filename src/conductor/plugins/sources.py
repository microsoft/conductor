"""Parse the ``plugin_sources:`` source-string grammar.

A source says *where a marketplace comes from*. The grammar is not
invented here — it is the one the Copilot CLI already accepts in
``PluginsMarketplacesAddRequest.source``, so a string a user has already
written for their CLI works unchanged in a workflow:

===========================  ==========================================
Form                         Example
===========================  ==========================================
``owner/repo``               ``acme/agent-plugins``
``owner/repo#ref``           ``acme/agent-plugins#v1.4.0``
http/https URL, opt. ``#``   ``https://gitlab.com/acme/p.git#main``
ssh URL, opt. ``#``          ``ssh://git@github.com/acme/p.git``
scp-style                    ``git@github.com:acme/p.git#3f2a1c9``
local path                   ``./vendor/plugins``, ``~/src/plugins``
===========================  ==========================================

The one trap worth naming: **this must not reuse**
:func:`~conductor.skills.registry.is_path_entry`. That helper answers a
different question ("is this ``skills:``/``plugins:`` entry a path?") and
returns ``True`` for anything containing ``/`` — which is every remote
form above. ``owner/repo`` would silently become a relative directory
lookup against the workflow file. Here a local path is recognised by its
*prefix* (``~``, ``.``, or absolute), and everything else is a remote.

A leaf module: imports only :mod:`conductor.plugins.errors`, so it can be
used from the config layer and the fetch layer without either depending
on the other.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from conductor.plugins.errors import PluginSourceError

# A full 40-character git object name, either case — a SHA copied from a
# web UI is often upper-case, and treating it as a floating ref would
# silently defeat the pin the user was reaching for. Only an
# unabbreviated SHA is treated as immutable: an abbreviation is ambiguous by construction (it
# may grow a collision as the repo does), and `git ls-remote` cannot
# expand one without a network round trip — which is exactly what pinning
# is meant to avoid.
FULL_SHA: re.Pattern[str] = re.compile(r"\A[0-9a-fA-F]{40}\Z")

# Explicit URL schemes. `git://` is included for completeness; `file://`
# matters because it is how the test-suite builds real repositories
# without a network.
_URL_SCHEMES: tuple[str, ...] = ("https://", "http://", "ssh://", "git://", "file://")

# scp-style remote: ``user@host:path``. Anchored on a ``:`` that is not
# part of a scheme and not a Windows drive letter — ``C:\src\plugins`` has
# a single-character "host", which no real hostname is. The path must not
# begin with ``:``, which excludes git's ``transport::argument`` form —
# ``ext::sh -c '...'`` would otherwise parse as host ``ext`` and hand git
# a remote helper that runs an arbitrary shell command.
_SCP_STYLE: re.Pattern[str] = re.compile(r"\A(?:[^/@:]+@)?(?P<host>[^/@:]{2,}):(?P<path>[^:].*)\Z")

# ``owner/repo`` GitHub shorthand: exactly two non-empty segments of
# ordinary repository characters.
_OWNER_REPO: re.Pattern[str] = re.compile(r"\A[\w.-]+/[\w.-]+\Z")

# Path components that would escape the cache root if they reached a
# cache key. ``[\w.-]+`` matches both, so the shorthand pattern above
# cannot be relied on to exclude them.
_UNSAFE_SEGMENTS: frozenset[str] = frozenset({".", ".."})

# Credentials embedded in a URL authority, for redaction in messages.
# Matches the ``scheme://`` prefix and everything up to the ``@``.
_CREDENTIAL: re.Pattern[str] = re.compile(r"(\w+://)[^/@]+@")

# Host recorded for a local-path source, so its cache key cannot collide
# with a real host. Not a valid DNS name, deliberately.
_LOCAL_HOST = "_local"

# Characters that are legal in a URL path segment but change what a path
# *means* on Windows, so a cache key built from them would not name the
# directory it appears to. ``:`` is the one that actually occurs: a
# ``file://C:/src/repo`` source derives the owner ``C:_src``, and Windows
# reads ``C:`` as a drive (or, mid-component, as an NTFS alternate data
# stream) rather than as part of the name. The rest are included because
# they fail the same way and cost nothing to cover.
#
# Substituted rather than refused, unlike ``..``: a Windows path is a
# legitimate source, so refusing it would make ``file://`` unusable there.
# Substituted on *every* platform so one workflow file resolves to the same
# cache layout everywhere.
_PATH_UNSAFE = str.maketrans(dict.fromkeys(':<>"|?*', "_"))

# Bound on one cache-key segment. Windows caps a path at 260 characters
# unless long-path support is enabled, and the segment is only one part of
# a path that also carries the cache root, the host, and the leaf.
_MAX_SEGMENT = 48


def redact_credentials(text: str) -> str:
    """Remove any URL-embedded credential from ``text``.

    Used on :attr:`PluginSource.display` and on ``git``'s own stderr,
    which quotes the remote URL back on most failures. Exported because
    the redaction must be identical in both places — a message that
    scrubbed Conductor's copy of a URL while echoing git's would be worse
    than not scrubbing at all.
    """
    return _CREDENTIAL.sub(r"\1***@", text)


@dataclass(frozen=True, kw_only=True)
class PluginSource:
    """A parsed ``plugin_sources:`` source.

    Either a local directory (``is_local``) or a git remote. The two are
    kept in one type because everything downstream — the resolution
    table, the catalog reader, the validate summary — cares only about
    "where do I read plugin roots from", and branching on a flag at the
    one place that fetches is simpler than two parallel hierarchies.
    """

    raw: str
    """The source exactly as written, for messages and cache metadata."""

    location: str
    """Git remote URL, or the path text for a local source.

    For a local source this is the *unresolved* text: anchoring a
    relative path needs the workflow file's directory, which the schema
    layer does not have.
    """

    ref: str | None = None
    """Git ref after ``#``, or ``None`` when the source named none.

    ``None`` means "the remote's default branch", resolved at fetch time
    rather than assumed to be ``main`` — a repo whose default is
    ``master`` or ``trunk`` is not an error.
    """

    is_local: bool = False
    """Whether this source is a directory on this machine."""

    host: str
    """Host component of the cache key (e.g. ``github.com``)."""

    owner: str
    """Owner/namespace component of the cache key."""

    repo: str
    """Repository component of the cache key."""

    @property
    def is_pinned(self) -> bool:
        """Whether the ref is an immutable full commit SHA.

        A pinned source is fetched once and never re-checked; anything
        else is floating and re-resolved on every run.
        """
        return self.ref is not None and bool(FULL_SHA.match(self.ref))

    @property
    def cache_key(self) -> PurePosixPath:
        """Cache path segment for this source, ``<host>/<owner>/<repo>``.

        A ``PurePosixPath`` rather than a string so callers join it
        instead of concatenating, and so the segment count is fixed no
        matter what the URL looked like.

        The repo segment carries a short digest of the remote URL, because
        the three-segment shape is lossy: ``_key_from_parts`` joins deeper
        path components with ``_``, so ``group/subgroup/repo`` and
        ``group_subgroup/repo`` would otherwise share a directory. Per-SHA
        checkouts would survive that, but the ref pointer beside them
        would not — two different repositories floating on ``main`` would
        overwrite each other's record, and an offline run would be handed
        the wrong repository's checkout with only a "using the cached
        checkout" notice.
        """
        return PurePosixPath(self.host) / self.owner / f"{self.repo}-{self._digest}"

    @property
    def _digest(self) -> str:
        """Short stable digest of the remote, disambiguating the cache key."""
        return hashlib.sha256(self.location.encode("utf-8")).hexdigest()[:8]

    @property
    def display(self) -> str:
        """The source as written, with any embedded credential redacted.

        Every message a user can see goes through this rather than
        :attr:`raw`. A source may legitimately carry a token
        (``https://x-access-token:ghp_…@github.com/acme/p.git``), and the
        failures that quote it — offline, expired credentials, a typo —
        are exactly the ones a user pastes into an issue or a chat.

        The cache key already discards credentials; this closes the other
        half.
        """
        return redact_credentials(self.raw)

    def __post_init__(self) -> None:
        """Enforce that no cache-key segment can escape the cache root.

        The key is joined into a filesystem path and the directory is
        created with ``parents=True``, so a ``..`` reaching it writes
        outside the plugin cache — into the sibling registry cache, for
        instance. Each parse branch checks its own inputs, but a
        documented-only invariant is the one that quietly stops holding;
        the same reasoning put a ``__post_init__`` on
        :class:`~conductor.plugins.manifest.PluginManifest` and
        :class:`~conductor.plugins.registry.ResolvedPlugin`.

        Deliberately no filesystem probe — establishing that is the
        producer's job, and state checked in a constructor is a TOCTOU
        illusion.

        Raises:
            PluginSourceError: If any key segment is empty or a
                relative-path component.
        """
        for part in self.cache_key.parts:
            if not part or part in _UNSAFE_SEGMENTS:
                raise PluginSourceError(
                    f"PluginSource cache key {self.cache_key} has an unusable segment "
                    f"{part!r} (it is joined into the plugin cache path), from {self.raw!r}"
                )

    def describe(self) -> str:
        """One-line description for validate output and error messages."""
        if self.is_local:
            return f"{self.location} (local path)"
        return self.display


def _split_ref(value: str) -> tuple[str, str | None]:
    """Split a trailing ``#ref`` off a source string.

    Splits on the **last** ``#``: a ref cannot contain one (git refuses
    it), while a URL conceivably can.
    """
    if "#" not in value:
        return value, None
    location, _, ref = value.rpartition("#")
    ref = ref.strip()
    if not location.strip():
        raise PluginSourceError(f"Source {value!r} has a ref but no location before the '#'.")
    if not ref:
        raise PluginSourceError(
            f"Source {value!r} ends with an empty '#ref'. Drop the '#' to use the "
            "repository's default branch."
        )
    return location.strip(), ref


def _is_local_path(location: str) -> bool:
    """Whether a source location denotes a directory on this machine.

    Prefix-based on purpose — see the module docstring. ``owner/repo``
    must not match, so containing a separator cannot be the test.
    """
    if location.startswith(("~", ".")):
        return True
    # Absolute in *either* convention, deliberately independent of the host
    # OS. A plugin source is a string in a workflow file, so the same file
    # must classify it the same way everywhere -- ``Path`` is the running
    # platform's flavour, so on Windows it called "/srv/p" relative and the
    # source was refused as unrecognised. ``PureWindowsPath`` also covers a
    # bare drive root ("C:\\") and UNC paths, which the posix flavour calls
    # relative; a drive-relative "C:" is absolute in neither, correctly.
    return PurePosixPath(location).is_absolute() or PureWindowsPath(location).is_absolute()


def _strip_git_suffix(name: str) -> str:
    """Drop a trailing ``.git`` from a repository name."""
    return name[: -len(".git")] if name.endswith(".git") else name


def _key_from_parts(raw: str, host: str, path: str) -> tuple[str, str, str]:
    """Derive ``(host, owner, repo)`` from a remote's host and path.

    An owner is not guaranteed — self-hosted forges nest arbitrarily
    deep, and some serve repositories at the root. Deeper paths collapse
    their intermediate segments into the owner slot with ``_`` so the
    cache key stays exactly three segments; a repository at the root gets
    a literal ``_`` owner rather than shifting the layout.

    Every segment is checked against ``.`` and ``..``. The key is joined
    into a filesystem path and the directory is created with
    ``parents=True``, so a source of ``owner/..`` would otherwise write
    outside the plugin cache root — into the sibling registry cache, for
    instance. Refused rather than sanitised: a repository genuinely named
    ``..`` does not exist, so this is always a malformed or hostile
    source.

    Raises:
        PluginSourceError: If the path is empty or any derived segment is
            a relative-path component.
    """
    segments = [segment for segment in path.strip("/").split("/") if segment]
    if not segments:
        raise PluginSourceError(f"Source {raw!r} names a host but no repository path.")
    repo = _strip_git_suffix(segments[-1])
    if not repo:
        raise PluginSourceError(f"Source {raw!r} has an empty repository name.")
    owner = "_".join(segments[:-1]) or "_"
    resolved = (host.lower(), owner, repo)
    if any(segment in _UNSAFE_SEGMENTS for segment in (*segments, *resolved)):
        raise PluginSourceError(
            f"Source {raw!r} contains a '.' or '..' path component, which would "
            "escape the plugin cache directory."
        )
    # Substitution happens after the '..' check, so a hostile segment is
    # still refused rather than quietly renamed into a harmless one.
    return tuple(_safe_segment(segment) for segment in resolved)  # type: ignore[return-value]


def _safe_segment(segment: str) -> str:
    """Make one cache-key segment safe to use as a directory name anywhere.

    Two problems, both only reachable via a local ``file://`` source, and
    both of which put the checkout somewhere other than where the key says.

    Characters are substituted because ``:`` changes what a path *means* on
    Windows: an owner of ``C:_src`` reads as a drive, or mid-component as an
    NTFS alternate data stream. The rest of the set fails the same way.

    Length is bounded because a local source flattens its whole directory
    path into the owner segment, and Windows still caps a path at 260
    characters by default -- a source under a deep directory produced a name
    long enough that ``git`` refused to create ``.git`` inside it. Replacing
    an over-long segment with a digest of itself costs nothing: the cache
    key's leaf already carries a digest of the full location, so the owner
    disambiguates nothing on its own.
    """
    segment = segment.translate(_PATH_UNSAFE)
    if len(segment) > _MAX_SEGMENT:
        digest = hashlib.sha256(segment.encode("utf-8")).hexdigest()[:12]
        return f"{segment[: _MAX_SEGMENT - len(digest) - 1]}-{digest}"
    return segment


def _parse_url(raw: str, location: str, ref: str | None) -> PluginSource:
    """Parse an explicit-scheme URL source."""
    scheme, _, remainder = location.partition("://")
    if scheme == "file":
        # A file:// URL has no host worth keying on, and its path is
        # already absolute. Treated as a remote (git can clone it) but
        # keyed under the local host so it cannot collide with a forge.
        #
        # Backslashes are folded to '/' first: this is the one URL form
        # that carries a native Windows path, and the splitter below only
        # knows '/'. Without this the whole of "C:\src\repo" arrives as a
        # single segment, so the cache key kept its separators and the
        # checkout landed at a drive-absolute path outside the plugin
        # cache entirely -- the same escape the '..' check exists to stop.
        host, owner, repo = _key_from_parts(raw, _LOCAL_HOST, remainder.replace("\\", "/"))
        return PluginSource(raw=raw, location=location, ref=ref, host=host, owner=owner, repo=repo)

    authority, _, path = remainder.partition("/")
    # Strip credentials and port: neither identifies the repository, and
    # a token in a URL must not end up as a directory name in the cache.
    hostname = authority.rpartition("@")[2].partition(":")[0]
    if not hostname:
        raise PluginSourceError(f"Source {raw!r} has no host after {scheme}://.")
    host, owner, repo = _key_from_parts(raw, hostname, path)
    return PluginSource(raw=raw, location=location, ref=ref, host=host, owner=owner, repo=repo)


def parse_plugin_source(value: str) -> PluginSource:
    """Parse one ``plugin_sources:`` source string.

    Args:
        value: The source as written in YAML.

    Returns:
        The parsed source, with its cache key derived.

    Raises:
        PluginSourceError: If the string is empty, or matches none of the
            recognised forms. Refused rather than guessed at: a
            mistyped source that fell through to "treat it as a relative
            path" would fail later with a message about a missing
            directory the author never wrote.
    """
    text = value.strip()
    if not text:
        raise PluginSourceError("plugin_sources entries must be non-empty strings.")

    location, ref = _split_ref(text)

    if location.startswith("-"):
        # The location is passed to ``git`` as its own argv element, so a
        # leading dash makes it an option rather than a remote
        # (``--upload-pack=...``, ``-oProxyCommand=...``). The scp-style
        # pattern below would otherwise accept both.
        raise PluginSourceError(
            f"Source {text!r} starts with '-', which git would read as an option "
            "rather than a repository."
        )

    if _is_local_path(location):
        if ref is not None:
            raise PluginSourceError(
                f"Source {text!r} is a local path with a '#{ref}' ref. A local source "
                "is read in place and has no ref to check out; point at a git remote "
                "to pin one, or drop the ref."
            )
        repo = Path(location).name or "_"
        if repo in _UNSAFE_SEGMENTS:
            raise PluginSourceError(
                f"Source {text!r} ends in a '.' or '..' path component, which would "
                "escape the plugin cache directory."
            )
        return PluginSource(
            raw=text,
            location=location,
            is_local=True,
            host=_LOCAL_HOST,
            owner="_",
            repo=repo,
        )

    if location.startswith(_URL_SCHEMES):
        return _parse_url(text, location, ref)

    if _OWNER_REPO.match(location):
        owner, _, repo = location.partition("/")
        repo = _strip_git_suffix(repo)
        if owner in _UNSAFE_SEGMENTS or repo in _UNSAFE_SEGMENTS:
            raise PluginSourceError(
                f"Source {text!r} contains a '.' or '..' path component, which would "
                "escape the plugin cache directory."
            )
        return PluginSource(
            raw=text,
            location=f"https://github.com/{owner}/{repo}.git",
            ref=ref,
            host="github.com",
            owner=owner,
            repo=repo,
        )

    scp = _SCP_STYLE.match(location)
    if scp is not None:
        host, owner, repo = _key_from_parts(raw=text, host=scp["host"], path=scp["path"])
        return PluginSource(raw=text, location=location, ref=ref, host=host, owner=owner, repo=repo)

    raise PluginSourceError(
        f"Source {text!r} is not a recognised plugin source. Expected 'owner/repo', "
        "'owner/repo#ref', an http/https/ssh URL, a git@host:path remote, or a local "
        "path starting with '.', '~', or '/'."
    )


__all__ = ["FULL_SHA", "PluginSource", "parse_plugin_source", "redact_credentials"]
