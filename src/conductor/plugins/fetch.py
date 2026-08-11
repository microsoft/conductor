"""Acquire git-backed plugin sources into a local cache.

Conductor owns cloning. The Copilot SDK's ``SessionInstalledPlugin``
carries ``installed_at``, ``cache_path`` and ``version`` — all describing
an *already-completed* install — so ``installedPlugins`` declares existing
state rather than requesting acquisition.

Acquisition shells out to ``git`` rather than speaking a forge's HTTP API.
That is what makes existing SSH keys, credential helpers, ``insteadOf``
rules and self-hosted hosts work without reimplementing any of them; the
HTTP fetcher in :mod:`conductor.registry.github` is GitHub-only by
construction.

Pinning model
=============

A source's ref is either an immutable full SHA or floating:

* **pinned** (``owner/repo#9c4e1f2a…``) — fetched once, never re-checked.
* **floating** (a tag, a branch, or no ref) — ``git ls-remote`` on every
  run; a moved ref fetches the new commit.

There is no lockfile. The YAML *is* the lock, and pinning is a
one-character edit — which also means an unpinned source can change what
it ships between two runs, including gaining an MCP server. That is named
in the docs rather than hidden behind a file.

Cache layout
============

::

    <base>/<host>/<owner>/<repo>/<sha[:12]>/       # the checkout
    <base>/<host>/<owner>/<repo>/_refs/<slug>.json # last SHA a ref had
    <base>/<host>/<owner>/<repo>/<sha[:12]>.ready  # readiness sentinel

``<base>`` is ``$CONDUCTOR_HOME/cache/plugins`` (default
``~/.conductor/cache/plugins``), matching
:func:`conductor.registry.cache.get_cache_base` so there is one cache root
to reason about rather than two.

The ``_refs`` pointer is what makes the offline fallback possible: without
a record of which SHA a floating ref last resolved to, "use the cached
checkout" has no way to choose one. It is cache state, not a lockfile — it
lives under ``$CONDUCTOR_HOME`` and is never committed.

The sentinel is written **last**, after the clone is in place, so a
concurrent reader never observes a half-populated checkout — the same
guarantee, for the same reason, as
:mod:`conductor.registry.cache`'s ``.complete`` files.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from conductor.plugins.errors import PluginError, PluginFetchError, PluginSourceError
from conductor.plugins.sources import FULL_SHA, PluginSource, redact_credentials

logger = logging.getLogger(__name__)

# Per-invocation timeouts. A hung `git` on an unreachable host must not
# hang the workflow: `ls-remote` is a single round trip and `clone` is
# bounded by --depth 1, so neither has a legitimate reason to run long.
LS_REMOTE_TIMEOUT_SECONDS = 30
CLONE_TIMEOUT_SECONDS = 300

# Characters kept when turning a ref into a filename. A ref may contain
# '/' (``refs/heads/main``, ``release/1.x``), which would otherwise
# create directories inside _refs.
_REF_SLUG_UNSAFE: re.Pattern[str] = re.compile(r"[^A-Za-z0-9_.-]+")

_REFS_DIR = "_refs"

# Resolutions already performed in this process, keyed by (remote, ref).
# One workflow may name several plugins from one marketplace, and several
# agents may each resolve the same list; without this each would pay its
# own network round trip.
_resolution_memo: dict[tuple[str, str | None], str] = {}


@dataclass(frozen=True)
class FetchResult:
    """Outcome of acquiring one source."""

    source: PluginSource
    """The source that was acquired."""

    root: Path
    """Directory the marketplace should be read from."""

    sha: str
    """Full commit SHA of the checkout.

    Never ``None``: a local source is refused by :func:`fetch_source`
    and so never produces a result. The optionality is genuine one layer
    up, on :class:`~conductor.plugins.resolution.ResolvedSource`, where
    local sources do exist.
    """

    stale: bool = False
    """Whether the ref could not be re-checked and a cached checkout was used.

    Only ever true for a floating source: a pinned one is immutable, so
    reusing its cache is correct rather than stale.
    """

    fetched: bool = False
    """Whether this call performed a clone (as opposed to a cache hit)."""


def get_plugin_cache_base() -> Path:
    """Return the base directory plugin checkouts are cached under.

    Uses ``$CONDUCTOR_HOME/cache/plugins/`` or
    ``~/.conductor/cache/plugins/``.
    """
    home = os.environ.get("CONDUCTOR_HOME")
    base = Path(home) if home else Path.home() / ".conductor"
    return base / "cache" / "plugins"


def _repo_root(source: PluginSource) -> Path:
    """Return the per-repository cache directory for ``source``."""
    return get_plugin_cache_base() / Path(*source.cache_key.parts)


def _checkout_dir(source: PluginSource, sha: str) -> Path:
    """Return the per-SHA checkout directory."""
    return _repo_root(source) / sha[:12]


def _sentinel(source: PluginSource, sha: str) -> Path:
    """Return the readiness sentinel for a checkout."""
    return _repo_root(source) / f"{sha[:12]}.ready"


def _ref_slug(ref: str | None) -> str:
    """Turn a ref into a filename-safe, collision-free slug.

    The sanitised name alone is lossy — ``release/1.x`` and
    ``release_1.x`` are different refs that map to one filename, and a
    branch literally named ``_default`` collides with the no-ref case. A
    digest of the original settles all three, and is kept alongside the
    readable form so the directory is still browsable.
    """
    if not ref:
        return "_default"
    digest = hashlib.sha256(ref.encode("utf-8")).hexdigest()[:8]
    return f"{_REF_SLUG_UNSAFE.sub('_', ref)}-{digest}"


def _ref_pointer(source: PluginSource, ref: str | None) -> Path:
    """Return the file recording the last SHA a floating ref resolved to."""
    return _repo_root(source) / _REFS_DIR / f"{_ref_slug(ref)}.json"


def is_cached(source: PluginSource, sha: str) -> bool:
    """Whether a complete checkout of ``sha`` is already on disk."""
    try:
        return _sentinel(source, sha).is_file() and _checkout_dir(source, sha).is_dir()
    except OSError:
        return False


def _read_ref_pointer(source: PluginSource, ref: str | None) -> str | None:
    """Return the last SHA recorded for ``ref``, if any is still cached."""
    pointer = _ref_pointer(source, ref)
    try:
        if not pointer.is_file():
            return None
        payload = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sha = payload.get("sha") if isinstance(payload, dict) else None
    if not isinstance(sha, str) or not FULL_SHA.match(sha):
        return None
    return sha if is_cached(source, sha) else None


def _write_ref_pointer(source: PluginSource, ref: str | None, sha: str) -> None:
    """Record which SHA a ref resolved to, for the offline fallback.

    Best-effort: an unwritable cache costs a future offline run its
    fallback, which is not worth failing a working fetch over.
    """
    pointer = _ref_pointer(source, ref)
    try:
        pointer.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ref": ref, "sha": sha, "resolved_at": time.time()}
        temporary = pointer.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, pointer)
    except OSError as exc:
        logger.debug("Could not record ref pointer %s: %s", pointer, exc)


def _run_git(arguments: Sequence[str], *, timeout: int, context: str) -> str:
    """Run a ``git`` command and return its stdout.

    ``GIT_TERMINAL_PROMPT=0`` and ``SSH_ASKPASS`` suppression are the
    load-bearing part: without them a private repository the user has no
    credentials for blocks forever waiting for a username on a terminal
    that may not be attached, instead of failing with a message.

    ``protocol.ext.allow=never`` is defence in depth. ``git-remote-ext``
    treats its argument as a shell command, so an ``ext::sh -c '...'``
    source would be arbitrary code execution rather than a clone.
    :data:`~conductor.plugins.sources._SCP_STYLE` already refuses that
    shape, and modern git blocks the transport by default — but the
    default is configurable, and "declaring a source" is meant to consent
    to fetching a repository, not to running a command.

    Raises:
        PluginFetchError: If ``git`` is missing, times out, or exits
            non-zero.
    """
    environment = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "GCM_INTERACTIVE": "never",
    }
    try:
        completed = subprocess.run(  # noqa: S603
            ["git", "-c", "protocol.ext.allow=never", *arguments],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
            check=False,
        )
    except FileNotFoundError as exc:
        raise PluginFetchError(
            f"{context}: 'git' was not found on PATH. Git-backed plugin sources are "
            "cloned with git so your existing SSH keys and credential helpers apply; "
            "install git, or point 'source:' at a local path."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PluginFetchError(f"{context}: git timed out after {timeout}s.") from exc

    if completed.returncode != 0:
        failure = PluginFetchError(f"{context}: {_summarize_git_failure(completed)}")
        # The full output rides along so ``_clone`` can classify a shallow
        # refusal on every line git printed, not just the one summarised
        # into the message.
        failure.git_output = redact_credentials(completed.stderr or completed.stdout or "")
        raise failure
    return completed.stdout


def _summarize_git_failure(completed: subprocess.CompletedProcess[str]) -> str:
    """Pick the informative line out of a failed ``git`` invocation.

    git's stderr for an unreachable remote is five lines, of which the
    *first* names the cause and the last is prose:

    .. code-block:: text

        fatal: '/srv/nope' does not appear to be a git repository
        fatal: Could not read from remote repository.

        Please make sure you have the correct access rights
        and the repository exists.

    Taking the last line reported "and the repository exists." as the
    error — naming no cause and prescribing no remedy, so a user could
    not tell a typo from a dead host from expired credentials.

    Credentials are redacted here rather than at the call sites: git
    quotes the remote URL back on most failures, so its own output is the
    half most likely to leak a token into a bug report.
    """
    lines = (completed.stderr or completed.stdout or "").strip().splitlines()
    named = next((line for line in lines if line.startswith(("fatal:", "error:"))), None)
    message = named or (lines[0] if lines else f"git exited {completed.returncode}")
    return redact_credentials(message.strip())


def resolve_ref(source: PluginSource) -> str:
    """Resolve a source's ref to a full commit SHA.

    A pinned source short-circuits without touching the network — that is
    the whole point of pinning. Everything else asks the remote, so a
    moved tag or a new commit on a branch is picked up.

    Args:
        source: The parsed source. Must not be local.

    Returns:
        The full 40-character commit SHA.

    Raises:
        PluginFetchError: If the remote cannot be reached or the ref
            matches nothing there.
    """
    if source.is_pinned:
        assert source.ref is not None
        return source.ref

    key = (source.location, source.ref)
    memoized = _resolution_memo.get(key)
    if memoized is not None:
        return memoized

    context = f"Resolving {source.display!r}"
    arguments = ["ls-remote", source.location]
    if source.ref:
        # Both spellings, so a name that is a tag and a name that is a
        # branch each match without the caller having to say which. The
        # ``^{}`` variants are not optional: git only emits the
        # dereferenced line when the pattern explicitly asks for it, so
        # without them an annotated tag resolves to the tag *object* —
        # a SHA no checkout ever equals, which would key the cache on
        # one value and record another.
        arguments += [
            source.ref,
            f"{source.ref}^{{}}",
            f"refs/heads/{source.ref}",
            f"refs/tags/{source.ref}",
            f"refs/tags/{source.ref}^{{}}",
        ]
    else:
        arguments.append("HEAD")

    output = _run_git(arguments, timeout=LS_REMOTE_TIMEOUT_SECONDS, context=context)
    sha = _select_sha(output, source)
    _resolution_memo[key] = sha
    return sha


def _select_sha(output: str, source: PluginSource) -> str:
    """Pick the commit SHA from ``git ls-remote`` output.

    An annotated tag reports both the tag object and, as ``<ref>^{}``, the
    commit it points at. The dereferenced line is what a clone would check
    out, so it wins where both are present — otherwise pinning a release
    tag would record the tag object's SHA, which no checkout ever equals.

    Raises:
        PluginFetchError: If nothing matched the requested ref.
    """
    candidates: dict[str, str] = {}
    for line in output.splitlines():
        sha, _, name = line.partition("\t")
        sha = sha.strip()
        if FULL_SHA.match(sha) and name.strip():
            candidates[name.strip()] = sha

    if not candidates:
        named = f"ref {source.ref!r}" if source.ref else "a default branch"
        raise PluginFetchError(
            f"Source {source.display!r} has no {named}. Check the ref "
            "name, or drop '#ref' to use the repository's default branch."
        )

    for name, sha in candidates.items():
        if name.endswith("^{}"):
            return sha
    return next(iter(candidates.values()))


# Substrings git emits when a remote will not serve a bare commit — taken
# from the strings in the `git` binary itself rather than guessed:
#
#   "Server does not allow request for unadvertised object %s"
#   "git upload-pack: not our ref %s"
#   "no such remote ref %s"
#
# Only these enter the unshallowed retry. An auth failure, a DNS failure
# or a timeout is re-raised as-is: retrying doubles the time budget and
# then reports the second error, discarding the first — which is usually
# the one naming the actual problem.
_SHALLOW_REFUSED: tuple[str, ...] = (
    "unadvertised object",
    "not our ref",
    "no such remote ref",
    "protocol error",
)


def _refuses_shallow_sha(text: str) -> bool:
    """Whether git's output means "this remote won't serve a bare SHA".

    Matched against the *whole* stderr rather than the single summarised
    line: a transport that prints a trailing summary after the marker
    would otherwise silently skip the fallback, turning a working pinned
    fetch into a clone error.
    """
    lowered = text.lower()
    return any(marker in lowered for marker in _SHALLOW_REFUSED)


def _clone(source: PluginSource, sha: str, destination: Path) -> None:
    """Populate ``destination`` with ``source`` checked out at ``sha``.

    ``init`` + ``remote add`` + ``fetch --depth 1 <sha>`` rather than
    ``git clone``, because a clone cannot be pointed at a bare commit.
    Where the remote refuses to serve an arbitrary SHA shallowly — not
    every host enables ``uploadpack.allowReachableSHA1InWant`` — this
    falls back to fetching the default refspec and checking the commit
    out of that. Note the fallback is a full *fetch*, not a full clone,
    so a SHA reachable only from an unfetched ref (a PR head, say) fails
    at checkout rather than at fetch.

    The fallback is entered only for a failure that looks like that
    refusal. Retrying after an auth failure, a DNS failure or a timeout
    would spend the time budget twice and then report the *second* error,
    discarding the first — which is usually the one that says
    ``Permission denied (publickey)``.
    """
    destination.mkdir(parents=True, exist_ok=True)
    _run_git(
        ["init", "--quiet", str(destination)],
        timeout=LS_REMOTE_TIMEOUT_SECONDS,
        context=f"Preparing a checkout of {source.display!r}",
    )
    git_dir = ["-C", str(destination)]
    _run_git(
        [*git_dir, "remote", "add", "origin", source.location],
        timeout=LS_REMOTE_TIMEOUT_SECONDS,
        context=f"Preparing a checkout of {source.display!r}",
    )

    context = f"Fetching {source.display!r} at {sha[:12]}"
    try:
        _run_git(
            [*git_dir, "fetch", "--depth", "1", "--quiet", "origin", sha],
            timeout=CLONE_TIMEOUT_SECONDS,
            context=context,
        )
    except PluginFetchError as shallow_failure:
        if not _refuses_shallow_sha(getattr(shallow_failure, "git_output", "")):
            raise
        logger.debug(
            "Shallow SHA fetch refused for %s (%s); retrying unshallowed",
            source.display,
            shallow_failure,
        )
        try:
            _run_git(
                [*git_dir, "fetch", "--quiet", "origin"],
                timeout=CLONE_TIMEOUT_SECONDS,
                context=context,
            )
        except PluginFetchError as full_failure:
            raise PluginFetchError(
                f"{context}: shallow fetch failed ({shallow_failure}); fetching the "
                f"full history also failed ({full_failure})."
            ) from shallow_failure
    _run_git(
        [*git_dir, "checkout", "--quiet", "--detach", sha],
        timeout=CLONE_TIMEOUT_SECONDS,
        context=f"Checking out {source.display!r} at {sha[:12]}",
    )


def _publish(temporary: Path, destination: Path) -> None:
    """Move a completed checkout into place, tolerating a lost race.

    Two runs may fetch the same SHA concurrently. The loser discards its
    copy rather than overwriting: the content is identical (a SHA names
    exactly one tree), so whichever landed first is already correct.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(temporary, destination)
    except OSError as exc:
        # Only the lost-race errnos. Treating EACCES or ENOSPC as "someone
        # else got there first" would report a broken checkout as a
        # successful one, on the strength of the directory merely existing.
        #
        # Windows is the exception, and needs naming rather than adding
        # EACCES globally: replacing a directory that already exists raises
        # ERROR_ACCESS_DENIED (WinError 5, surfaced as EACCES) instead of
        # ENOTEMPTY, so the POSIX-only list never fired and a second source
        # resolving to the same SHA failed the whole fetch. Safe because the
        # readiness sentinel is written *after* this returns: a winner that
        # died mid-clone leaves no sentinel, `is_cached` reports a miss, and
        # the tree is re-fetched rather than read half-written.
        lost_race = exc.errno in (errno.ENOTEMPTY, errno.EEXIST) or (
            sys.platform == "win32" and getattr(exc, "winerror", None) == 5
        )
        if lost_race and destination.is_dir():
            shutil.rmtree(temporary, ignore_errors=True)
            return
        raise


def fetch_source(
    source: PluginSource,
    *,
    allow_network: bool = True,
    on_warning: Callable[[str], None] | None = None,
) -> FetchResult:
    """Acquire one source, returning where its marketplace should be read.

    Args:
        source: The parsed source.
        allow_network: When ``False``, resolve from cache only. This is
            what keeps ``conductor validate`` off the network.
        on_warning: Sink for non-fatal diagnostics — a ref that could not
            be re-checked, most importantly.

    Returns:
        The fetch result, whose ``root`` is the directory to read.

    Raises:
        PluginFetchError: If the source cannot be acquired and no cached
            checkout can stand in for it.
    """
    if source.is_local:
        raise PluginFetchError(
            f"Source {source.display!r} is a local path and is read in place, not fetched. "
            "This is a bug in the caller."
        )

    warn = on_warning if on_warning is not None else (lambda _message: None)

    if not allow_network:
        cached = _cached_sha(source)
        if cached is None:
            raise PluginFetchError(
                f"Source {source.display!r} has not been fetched on this machine. Run "
                "'conductor plugin fetch' to prime the cache."
            )
        return FetchResult(source=source, root=_checkout_dir(source, cached), sha=cached)

    try:
        sha = resolve_ref(source)
    except PluginFetchError as exc:
        # Offline, on a VPN, or credentials expired. A previously fetched
        # checkout is better than a failed run, so long as the user is
        # told the ref was not re-checked.
        fallback = _cached_sha(source)
        if fallback is None:
            raise
        warn(
            f"could not check {source.display!r} for updates ({exc}); using the cached "
            f"checkout at {fallback[:12]}."
        )
        return FetchResult(
            source=source, root=_checkout_dir(source, fallback), sha=fallback, stale=True
        )

    if is_cached(source, sha):
        _write_ref_pointer(source, source.ref, sha)
        return FetchResult(source=source, root=_checkout_dir(source, sha), sha=sha)

    destination = _checkout_dir(source, sha)
    staging = Path(tempfile.mkdtemp(prefix=f".{sha[:12]}.", dir=_ensure_repo_root(source)))
    try:
        _clone(source, sha, staging)
        _publish(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    # Sentinel last: until it exists, `is_cached` reports a miss, so a
    # concurrent reader re-fetches rather than reading a partial tree.
    try:
        _sentinel(source, sha).write_text(sha, encoding="utf-8")
    except OSError as exc:
        raise PluginFetchError(
            f"Fetched {source.display!r} but could not mark it complete: {exc}"
        ) from exc
    _write_ref_pointer(source, source.ref, sha)
    return FetchResult(source=source, root=destination, sha=sha, fetched=True)


def _ensure_repo_root(source: PluginSource) -> Path:
    """Create and return the per-repository cache directory."""
    root = _repo_root(source)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PluginFetchError(
            f"Plugin cache directory {root} could not be created: {exc}"
        ) from exc
    return root


def _cached_sha(source: PluginSource) -> str | None:
    """Return a usable cached SHA for ``source``, if one exists.

    A pinned source checks its own SHA; a floating one consults the ref
    pointer, which is the only record of what that ref last meant.
    """
    if source.is_pinned:
        assert source.ref is not None
        return source.ref if is_cached(source, source.ref) else None
    return _read_ref_pointer(source, source.ref)


def fetch_sources(
    sources: Sequence[PluginSource],
    *,
    allow_network: bool = True,
    on_warning: Callable[[str], None] | None = None,
    max_workers: int = 8,
) -> dict[str, FetchResult]:
    """Acquire several sources concurrently, keyed by ``PluginSource.raw``.

    Concurrency is the point: each floating source costs one ``ls-remote``
    round trip (roughly half a second), and a workflow naming three
    marketplaces should pay that once rather than three times in series.
    Threads rather than tasks because the work is a subprocess.

    Duplicate sources are submitted once. Two marketplace names may
    legitimately share a source string, and cloning the same commit twice
    only to discard one copy is wasted network — and the discarded
    future's exception would never be retrieved.

    Raises:
        PluginFetchError: If any source cannot be acquired. **Every**
            failure is reported, not just the first: a user fixing three
            unreachable remotes should not have to rediscover them one
            run at a time.
    """
    remote = list(dict.fromkeys(source for source in sources if not source.is_local))
    if not remote:
        return {}
    if len(remote) == 1:
        return {
            remote[0].raw: fetch_source(
                remote[0], allow_network=allow_network, on_warning=on_warning
            )
        }

    results: dict[str, FetchResult] = {}
    failures: list[str] = []
    fatal = False
    pool = ThreadPoolExecutor(max_workers=min(max_workers, len(remote)))
    try:
        futures = {
            pool.submit(
                fetch_source, source, allow_network=allow_network, on_warning=on_warning
            ): source
            for source in remote
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                results[source.raw] = future.result()
            except PluginFetchError as exc:
                failures.append(str(exc))
            except PluginError as exc:
                # A source fault, not a fetch fault. Kept aside so the
                # combined error can be raised as the stronger class —
                # callers treat a fetch failure as deferrable ("run
                # conductor plugin fetch") and a source failure as broken,
                # and collapsing the two would make the same fault
                # deferrable only when the workflow names one marketplace.
                failures.append(f"{source.display}: {exc}")
                fatal = True
            except OSError as exc:
                failures.append(f"{source.display}: {exc}")
                fatal = True
    finally:
        # ``cancel_futures`` drops the ones that have not started; the
        # ones already running are not interruptible (git is a subprocess
        # and ``ThreadPoolExecutor`` joins its workers at interpreter
        # exit), so ``wait=False`` buys the caller its error message
        # immediately rather than after the slowest clone.
        pool.shutdown(wait=False, cancel_futures=True)

    if failures:
        detail = failures[0] if len(failures) == 1 else "\n  - " + "\n  - ".join(sorted(failures))
        raise (PluginSourceError if fatal else PluginFetchError)(detail)
    return results


def clear_resolution_memo() -> None:
    """Forget this process's ref resolutions.

    Exists for tests and for ``conductor plugin fetch``, which should ask
    the remote rather than reuse an answer from earlier in the process.
    """
    _resolution_memo.clear()


__all__ = [
    "FetchResult",
    "clear_resolution_memo",
    "fetch_source",
    "fetch_sources",
    "get_plugin_cache_base",
    "is_cached",
    "resolve_ref",
]
