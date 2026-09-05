"""Resolve a working install command for one of Conductor's optional extras.

Conductor ships three optional extras (``tui``, ``aca``,
``claude-agent-sdk``) and every hint that pointed at one used to hardcode
``pip install 'conductor-cli[<extra>]'``. That command cannot work on the
documented install path: ``install.sh`` / ``install.ps1`` run ``uv tool
install`` against a git reference, and a uv tool venv is not pip-managed —
pip has nothing there to resolve ``conductor-cli`` against, because it is
not published to PyPI (``release.yml`` attaches artifacts to a GitHub
Release and has no publish step). See issue #441.

The command is therefore resolved from the *detected* install context:

==============================  ==================  ========================
signal                          context             command
==============================  ==================  ========================
``<prefix>/uv-receipt.toml``    uv tool install     ``uv tool install --force``
``dir_info.editable`` in        source checkout     ``uv sync --inexact``
``direct_url.json``
neither                         anything else       ``pip install``
==============================  ==================  ========================

The pip form survives as the last-resort fallback because it *does* work
wherever pip can already see an installed ``conductor-cli`` — a wheel from
a GitHub Release, for instance, where pip resolves the extra against the
installed distribution. Where the install came from a git URL,
``direct_url.json`` still holds that URL and it is put back into the
command, so a ``pip``/``pipx``-from-git user gets something that resolves
rather than the dead form this module exists to delete.

Three properties of the rendered command are load-bearing:

* **Extras already installed are carried forward.** ``uv tool install
  --force`` replaces the tool's whole requirement set, and ``uv sync`` is
  exact unless given ``--inexact``, so a command naming only the requested
  extra *uninstalls* the others. This module exists to add an extra, not to
  trade one for another.
* **The install source is preserved.** The receipt records what the tool
  was actually installed from — a fork, a local checkout, a wheel — so
  reusing it stops the hint silently redirecting a developer's build at
  upstream's released tag.
* **Failures are visible.** A receipt that exists but cannot be read is not
  the same as a bare install, even though both yield "no extras found".
  Reporting the first as the second produces a confident, copy-pasteable
  command that deletes the user's TUI. That case renders a command carrying
  an inline warning instead.

The install scripts read the same receipt for the same reason, so an
upgrade preserves extras too — see ``install.sh``'s ``receipt_extras`` and
``install.ps1``'s ``Get-ReceiptExtras``.

This is a stdlib-only leaf module (like :mod:`conductor.duration` and
:mod:`conductor.console`) because ``providers/`` needs it and must not
import from ``cli/``.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import PackageNotFoundError, distribution, version
from pathlib import Path
from typing import Any, Final, Literal

logger = logging.getLogger(__name__)

#: The distribution name on the index — *not* the ``conductor`` command.
DISTRIBUTION: Final = "conductor-cli"

#: Clone URL used when no install source can be recovered.
REPO_GIT_URL: Final = "https://github.com/microsoft/conductor.git"

_UV_RECEIPT_NAME: Final = "uv-receipt.toml"

#: The extras declared in ``pyproject.toml``'s ``[project.optional-dependencies]``.
ExtraName = Literal["tui", "aca", "claude-agent-sdk", "telemetry"]

# A PEP 508 extra name. Checked at the parse boundary because these values
# are interpolated into a single-quoted command the user is told to paste,
# and this module cannot know what wrote the receipt. Mirrors
# `fleet/records.py::is_valid_run_id`, which exists for the same reason.
_EXTRA_NAME_RE: Final = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")

# PEP 503 normalization, so a receipt recording `conductor_cli` still
# matches. uv writes the canonical form today; this costs one regex and
# removes a way for the match to fail silently.
_NAME_SEPARATORS: Final = re.compile(r"[-_.]+")


def _canonical(name: str) -> str:
    """Return *name* in PEP 503 canonical form."""
    return _NAME_SEPARATORS.sub("-", name).lower()


class InstallContext(StrEnum):
    """How the running Conductor was installed."""

    UV_TOOL = "uv-tool"
    """Installed by ``uv tool install`` — the documented install-script path."""

    EDITABLE = "editable"
    """An editable install from a source checkout (``uv sync``)."""

    UNKNOWN = "unknown"
    """Anything else: a wheel, ``pip``/``pipx`` from git, a system package."""


def uv_receipt_path(prefix: Path | None = None) -> Path:
    """Return the path where ``uv tool install`` records its requirements.

    Args:
        prefix: Environment prefix whose receipt to locate. Defaults to
            ``sys.prefix``.

    Returns:
        ``<prefix>/uv-receipt.toml``, which only exists for a uv tool venv.
    """
    return (Path(sys.prefix) if prefix is None else prefix) / _UV_RECEIPT_NAME


def _direct_url() -> dict[str, Any]:
    """Return the parsed PEP 610 ``direct_url.json``, or ``{}`` if unavailable.

    ``Distribution.read_text`` suppresses only a handful of ``OSError``
    subclasses, so a file that is present but not valid UTF-8 raises
    ``UnicodeDecodeError`` — a ``ValueError``. That and ``JSONDecodeError``
    are both caught here, so this never raises into the error path it is
    called from.
    """
    try:
        raw = distribution(DISTRIBUTION).read_text("direct_url.json")
    except PackageNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("Could not read direct_url.json for %s", DISTRIBUTION, exc_info=True)
        return {}
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("Unparseable direct_url.json for %s", DISTRIBUTION, exc_info=True)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _is_editable() -> bool:
    """Return ``True`` when this distribution was installed in editable mode.

    Reads the PEP 610 ``direct_url.json`` recorded in the ``.dist-info``
    directory. A uv tool install also writes that file (with ``vcs_info``
    rather than ``dir_info``), which is why the receipt is checked first.
    """
    dir_info = _direct_url().get("dir_info")
    return isinstance(dir_info, dict) and bool(dir_info.get("editable"))


def _direct_url_source() -> str | None:
    """Return an installable source recovered from ``direct_url.json``.

    A ``pip``/``pipx`` install from git records the clone URL and the ref that
    was asked for, which is exactly what a working reinstall command needs.

    PEP 610 makes ``vcs_info`` / ``dir_info`` / ``archive_info`` mutually
    exclusive, and the branch is chosen on which is present rather than on
    what the URL looks like. That distinction matters for ``archive_info``: a
    wheel or sdist is one *artifact*, usually a download that no longer
    exists, so pinning to it produces a command that fails with ``No such
    file or directory``. Returning ``None`` there lets the caller emit a bare
    name, which pip resolves against the installed distribution.
    """
    info = _direct_url()
    url = info.get("url")
    if not isinstance(url, str) or not url:
        return None

    vcs_info = info.get("vcs_info")
    if isinstance(vcs_info, dict):
        vcs = vcs_info.get("vcs") or "git"
        # `requested_revision` is what the user asked to install; prefer it
        # over `commit_id` so an install from a branch stays on that branch
        # rather than being re-pinned to a single commit.
        ref = vcs_info.get("requested_revision") or vcs_info.get("commit_id")
        source = f"{vcs}+{url}"
        return f"{source}@{ref}" if isinstance(ref, str) and ref.strip() else source

    # A directory is a project that is still there; an archive is not.
    return url if isinstance(info.get("dir_info"), dict) else None


@dataclass(frozen=True)
class ReceiptContents:
    """What this install's uv receipt says about ``conductor-cli``.

    ``readable`` is separate from an empty ``extras`` on purpose: a receipt
    that could not be read is indistinguishable from a bare install by its
    contents alone, and the two must not produce the same command — one of
    them silently uninstalls the user's extras.
    """

    extras: frozenset[str] = frozenset()
    source: str | None = None
    readable: bool = True


def _requirement_source(req: dict[str, Any]) -> str | None:
    """Recover an installable source from one uv receipt requirement entry.

    uv records the origin under a key naming its kind — ``git`` (with the
    ref in a ``?rev=`` query), ``directory``/``path``/``editable`` for a
    local install, or ``url``. Reusing it is what keeps the hint pointed at
    the fork or local checkout the tool was actually built from.
    """
    git = req.get("git")
    if isinstance(git, str) and git:
        url, _, query = git.partition("?")
        for part in query.split("&"):
            key, _, value = part.partition("=")
            if key in {"rev", "tag", "branch"} and value:
                return f"git+{url}@{value}"
        return f"git+{url}"

    for key in ("directory", "path", "editable", "url"):
        value = req.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def read_receipt(prefix: Path | None = None) -> ReceiptContents:
    """Read this install's uv tool receipt.

    Never raises. A *missing* receipt reports ``readable=True`` with no
    extras — there is nothing to lose. A receipt that exists but cannot be
    read or understood reports ``readable=False``, which the rendered
    command turns into a visible warning rather than a silent omission.

    Args:
        prefix: Environment prefix whose receipt to read. Defaults to
            ``sys.prefix``.

    Returns:
        The extras and install source recorded for this distribution.
    """
    receipt = uv_receipt_path(prefix)
    if not receipt.is_file():
        return ReceiptContents()

    try:
        data = tomllib.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Permission denied, an I/O error, a half-written file, an encoding
        # change: each would otherwise render as "no extras", i.e. as a
        # command that removes them.
        logger.warning("Could not read uv receipt at %s", receipt, exc_info=True)
        return ReceiptContents(readable=False)

    tool = data.get("tool")
    requirements = tool.get("requirements") if isinstance(tool, dict) else None
    if not isinstance(requirements, list):
        logger.warning("uv receipt at %s has no [tool] requirements list", receipt)
        return ReceiptContents(readable=False)

    for req in requirements:
        # A tool venv can carry several requirements (`uv tool install
        # --with ...`); only this distribution's entry belongs in the spec.
        if not isinstance(req, dict):
            continue
        name = req.get("name")
        if not isinstance(name, str) or _canonical(name) != DISTRIBUTION:
            continue
        raw_extras = req.get("extras")
        extras = frozenset(
            e
            for e in (raw_extras if isinstance(raw_extras, list) else [])
            if isinstance(e, str) and _EXTRA_NAME_RE.match(e)
        )
        return ReceiptContents(extras=extras, source=_requirement_source(req))

    logger.warning("uv receipt at %s records no %s requirement", receipt, DISTRIBUTION)
    return ReceiptContents(readable=False)


def installed_extras(prefix: Path | None = None) -> frozenset[str]:
    """Return the extras recorded in this install's uv tool receipt.

    ``uv tool install --force`` replaces the tool's entire requirement set,
    so any extra missing from the reinstall command is removed. These are
    the extras that have to be carried forward.

    Never raises; an unreadable receipt yields an empty set. Callers that
    must distinguish "none recorded" from "could not tell" should use
    :func:`read_receipt` instead.

    Args:
        prefix: Environment prefix whose receipt to read. Defaults to
            ``sys.prefix``.

    Returns:
        The recorded extra names, or an empty set.
    """
    return read_receipt(prefix).extras


def installed_ref() -> str | None:
    """Return a git ref to pin a reinstall to when no source was recorded.

    Only reached when neither the uv receipt nor ``direct_url.json`` names
    an install source, so this is a best-effort guess: ``v<version>`` from
    package metadata assumes a matching tag exists. Returns ``None`` when
    even that is unavailable, so the caller emits an unpinned spec rather
    than a broken one.

    Returns:
        A ref such as ``"v1.2.3"``, or ``None``.
    """
    try:
        return f"v{version(DISTRIBUTION)}"
    except PackageNotFoundError:
        logger.debug("No installed metadata for %s; emitting an unpinned spec", DISTRIBUTION)
        return None


def extras_spec(extras: frozenset[str] | set[str]) -> str:
    """Render a sorted, deduplicated PEP 508 extras spec.

    Args:
        extras: The extra names to include.

    Returns:
        Something like ``"conductor-cli[aca,tui]"``.
    """
    return f"{DISTRIBUTION}[{','.join(sorted(extras))}]"


@dataclass(frozen=True)
class InstallEnvironment:
    """Everything about the current install that the hint depends on.

    Separating this from :func:`render_install_command` keeps the branch
    logic a pure function: every context can be unit-tested by constructing
    one of these, with no fake ``sys.prefix``, no fake ``dist-info``, and no
    real install.
    """

    context: InstallContext
    """How Conductor was installed."""

    extras: frozenset[str] = frozenset()
    """Extras already recorded for this install, which must be carried forward."""

    source: str | None = None
    """Where it was installed from — the right-hand side of a PEP 508 ``@``."""

    extras_known: bool = True
    """``False`` when a receipt exists but could not be read or understood."""

    receipt: str | None = None
    """The receipt consulted, named in the warning when it could not be read."""

    def __post_init__(self) -> None:
        # `uv sync` names neither extras nor a source, so carrying them on an
        # editable install would be state the renderer silently ignores. The
        # type should not be able to hold a value that means nothing.
        if self.context is InstallContext.EDITABLE:
            object.__setattr__(self, "extras", frozenset())
            object.__setattr__(self, "source", None)
            object.__setattr__(self, "extras_known", True)
            object.__setattr__(self, "receipt", None)


def detect_environment(prefix: Path | None = None) -> InstallEnvironment:
    """Inspect the running install and describe it.

    The uv receipt is checked first because a uv tool install *also* records
    a ``direct_url.json``; only the receipt distinguishes a managed tool venv
    from an ordinary one.

    Note *prefix* scopes the receipt lookup only — the editable check reads
    the running distribution's own metadata, which no prefix can redirect.

    Args:
        prefix: Environment prefix whose receipt to read. Defaults to
            ``sys.prefix``.

    Returns:
        The detected :class:`InstallEnvironment`.
    """
    receipt_path = uv_receipt_path(prefix)
    if receipt_path.is_file():
        receipt = read_receipt(prefix)
        return InstallEnvironment(
            InstallContext.UV_TOOL,
            extras=receipt.extras,
            source=receipt.source or _direct_url_source(),
            extras_known=receipt.readable,
            receipt=str(receipt_path),
        )
    if _is_editable():
        return InstallEnvironment(InstallContext.EDITABLE)
    return InstallEnvironment(InstallContext.UNKNOWN, source=_direct_url_source())


def _fallback_source() -> str:
    """Return a ``git+`` source for an install that recorded none."""
    ref = installed_ref()
    return f"git+{REPO_GIT_URL}@{ref}" if ref is not None else f"git+{REPO_GIT_URL}"


def render_install_command(extra: ExtraName, env: InstallEnvironment) -> str:
    """Render the install command for *extra* under *env*. Pure.

    Args:
        extra: The extra to install, e.g. ``"tui"`` or ``"aca"``.
        env: The install context to render for.

    Returns:
        A single shell command. Quoting suits both POSIX shells and
        PowerShell, the two shells the install scripts target.
    """
    match env.context:
        case InstallContext.UV_TOOL:
            # Union, not replace: `--force` rewrites the whole requirement
            # set, so omitting an already-installed extra uninstalls it.
            spec = f"{extras_spec(env.extras | {extra})} @ {env.source or _fallback_source()}"
            command = f"uv tool install --force '{spec}'"
            if not env.extras_known:
                # A shell comment, so the line stays safe to paste while
                # saying plainly that the extras list may be incomplete —
                # running it unedited could remove one.
                named = f" {env.receipt}" if env.receipt else ""
                command += (
                    f"  # WARNING: could not read{named} —"
                    " add any other extras you have before running this"
                )
            return command
        case InstallContext.EDITABLE:
            # `uv sync` is exact by default, so without --inexact this would
            # remove whatever another extra had installed — the same damage
            # the union above exists to prevent.
            return f"uv sync --inexact --extra {extra}"
        case InstallContext.UNKNOWN:
            spec = extras_spec({extra})
            # `python -m pip`, not a bare `pip`: the PATH pip does not manage
            # a pipx venv (or any venv this interpreter is not on the PATH
            # of), so a bare `pip install` there *succeeds* while installing a
            # second copy the user never runs -- a loud failure turned into a
            # silent wrong outcome. Naming this interpreter targets the
            # environment conductor is actually installed in.
            pip = f"{sys.executable} -m pip install"
            # A pip/pipx install from git records the URL it came from, and
            # putting it back is what makes the command resolve at all.
            # Without one, pip resolves the extra against the already-
            # installed distribution, which works for a wheel install.
            return f"{pip} '{spec} @ {env.source}'" if env.source else f"{pip} '{spec}'"


def install_command(extra: ExtraName, prefix: Path | None = None) -> str:
    """Return a copy-pasteable command that installs *extra* for this install.

    Never raises. This runs while an error message is being built — for a
    missing provider dependency, or a missing TUI — so an exception here
    would replace the diagnosis the user needs with a traceback.

    Args:
        extra: The extra to install, e.g. ``"tui"`` or ``"aca"``.
        prefix: Environment prefix whose receipt to read. Defaults to
            ``sys.prefix``.

    Returns:
        A single shell command.
    """
    try:
        return render_install_command(extra, detect_environment(prefix))
    except Exception:  # noqa: BLE001 - a hint must never mask the error it explains
        logger.warning("Could not resolve an install command for %r", extra, exc_info=True)
        if sys.platform == "win32":
            return (
                "$env:CONDUCTOR_INSTALL_EXTRAS = "
                f"'{extra}'; irm https://aka.ms/conductor/install.ps1 | iex"
            )
        return f"curl -sSfL https://aka.ms/conductor/install.sh | sh -s -- --extras {extra}"
