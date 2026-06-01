"""Workspace instruction file discovery and loading.

This module discovers and loads workspace instruction files that provide
context about a repository's conventions, coding style, and architecture
to workflow agents.

Conductor recognises two convention shapes (see :data:`CONVENTIONS`):

* **File conventions** (:class:`ConventionFile`) — single files at known paths,
  e.g. ``AGENTS.md``, ``.github/copilot-instructions.md``, ``CLAUDE.md``.
* **Directory conventions** (:class:`ConventionDirectory`) — directories that
  contain multiple instruction files matching a glob, optionally filtered by
  a frontmatter predicate. Today the only directory convention is GitHub
  Copilot's ``.github/instructions/*.instructions.md`` (recursive, with
  ``applyTo`` frontmatter filtering).

The primary use case is enabling conductor workflows (which may live in
distant skill directories) to automatically pick up the target repository's
instruction files when invoked from within that repo.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Convention types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConventionFile:
    """A single-file convention.

    Examples: ``AGENTS.md``, ``.github/copilot-instructions.md``, ``CLAUDE.md``.

    Discovery: walk CWD → git root, closest-wins per ``path``. The match key
    is local to this convention only — it does not collide with directory
    convention keys.
    """

    path: str  # relative path from a candidate directory, e.g. "AGENTS.md"


@dataclass(frozen=True)
class ConventionDirectory:
    """A directory-style convention.

    Example: GitHub Copilot's ``.github/instructions/*.instructions.md``.

    Discovery: walk CWD → git root. At each level, look for the convention
    directory. If found, find files matching ``pattern`` (recursively when
    ``recursive=True``), apply the optional ``include_file`` and
    ``extract_scope`` filters, and track surviving files keyed by their
    *relative path within the convention directory*. Closest-wins per
    relative-path key — local to this convention.

    Filter chain (applied in order, short-circuit on first reject):

    1. ``include_file(file)`` — coarse eligibility gate. Retained for
       backward-compat and for conventions whose eligibility logic is
       orthogonal to scope (e.g., "skip files without frontmatter").
       Convention authors writing new scoped conventions should typically
       reach for ``extract_scope`` instead.
    2. ``extract_scope(file)`` — returns the file's scope glob, or ``None``
       to opt out of auto-discovery for this file. The conductor core
       interprets the return value:

       * ``None`` → opt out (file is *not* loaded by auto-discovery,
         matching today's behavior for ``.github/instructions/`` files with
         absent or non-string ``applyTo``).
       * ``ALWAYS_ON_SCOPE`` (the string ``"**"``) → always include,
         regardless of CWD. Convention authors should use the constant
         rather than the literal so the contract is greppable.
       * Any other string → treat as a path glob and run a bidirectional
         overlap test against the user's CWD (relative to the convention
         directory's owner directory). The file is included if the glob's
         matched subtree overlaps with the CWD subtree in *either*
         direction. Multi-glob values are supported (separators ``;`` and
         ``,``); the file is included if *any* sub-glob overlaps.

    The overlap test is owned by conductor core (single source of truth),
    not by individual convention authors — so adding a second scoped
    convention later (e.g., Cursor's ``.cursor/rules/*.mdc`` with a
    ``globs:`` frontmatter field) only requires implementing its own
    ``extract_scope`` callable, not re-implementing the overlap semantic.

    Symlink policy: directory traversal does NOT follow symlinked directories
    (``os.walk(followlinks=False)``) to avoid loops and out-of-tree expansion.
    Symlinked instruction *files* are read like regular files via
    :meth:`Path.read_text` (which follows symlinks at the leaf).
    """

    path: str
    pattern: str
    include_file: Callable[[Path], bool] | None = None
    extract_scope: Callable[[Path], str | None] | None = None
    recursive: bool = True


Convention = ConventionFile | ConventionDirectory


# Convention-author-facing sentinel for "include this file regardless of CWD".
# When ``ConventionDirectory.extract_scope`` returns this exact string, the
# overlap test is short-circuited and the file is always loaded. Defined as a
# module constant so convention authors can grep for usages instead of
# scattering string literals.
ALWAYS_ON_SCOPE = "**"


@dataclass(frozen=True)
class DiscoveredInstruction:
    """A workspace instruction file discovered by ``--workspace-instructions``,
    annotated with its filtering provenance.

    Used by :func:`discover_workspace_instructions_detailed` so that consumers
    (e.g., ``--print-loaded-instructions``) can report *why* each file was
    included without re-parsing the filesystem.
    """

    path: Path
    # The convention's path identifier (e.g. ``"AGENTS.md"``,
    # ``".github/instructions"``), useful for grouping in output.
    source: str
    # The scope value that was extracted from the file, if any. ``None`` for
    # conventions with no scope concept (single-file conventions; directory
    # conventions without ``extract_scope``).
    scope: str | None
    # Stable, machine-readable reason for inclusion. One of:
    #
    # * ``"file-convention"`` — single-file convention, no scope concept.
    # * ``"always-on"`` — scoped convention but file's scope is
    #   :data:`ALWAYS_ON_SCOPE` (or the convention has no ``extract_scope``).
    # * ``"scope-overlap"`` — scoped convention; file's scope glob overlapped
    #   with the user's CWD subtree.
    reason: str


# ---------------------------------------------------------------------------
# Frontmatter parser for GitHub Copilot's `.github/instructions/` convention
# ---------------------------------------------------------------------------

# Tolerant regex: handles CRLF line endings, optional trailing whitespace on
# delimiters, and a closing `---` at EOF without a trailing newline.
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)

# Module-level safe-mode YAML parser. ruamel.yaml's `typ="safe"` disables
# custom tag constructors (no arbitrary code execution from user content).
_yaml = YAML(typ="safe")


def _parse_frontmatter(path: Path) -> dict[str, object] | None:
    """Parse the YAML frontmatter block from a Markdown file, robustly.

    This is a reusable primitive intended for any directory convention whose
    files use ``--- ... ---`` YAML frontmatter (GitHub Copilot's
    ``.github/instructions/``, hypothetical Cursor's ``.cursor/rules/``, etc.).

    Returns:
        - The parsed frontmatter as a ``dict`` if the file has valid YAML
          frontmatter that parses to a mapping.
        - ``None`` in every other case: missing frontmatter, unreadable file,
          malformed YAML (logged at WARNING), or YAML that parses to a
          non-mapping (list, scalar, empty).

    Edge cases handled:
        - CRLF line endings (Windows-authored files)
        - UTF-8 BOM (transparently stripped via ``utf-8-sig`` encoding)
        - Closing ``---`` at EOF without trailing newline
        - Non-dict YAML (returns ``None`` rather than raising AttributeError)
    """
    try:
        # 'utf-8-sig' transparently strips a leading UTF-8 BOM if present
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        logger.debug("Cannot read %s for frontmatter check: %s", path, e)
        return None

    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None

    try:
        fm = _yaml.load(match.group(1))
    except YAMLError as e:
        logger.warning("Failed to parse frontmatter in %s: %s; skipping", path, e)
        return None

    # YAML may parse to a non-dict (empty doc, list, scalar). Caller-defined
    # filters typically only make sense on mappings; normalise here.
    if not isinstance(fm, dict):
        return None

    return fm


def _extract_apply_to(path: Path) -> str | None:
    """Extract the ``applyTo`` glob from a ``.github/instructions/*.instructions.md``
    file's YAML frontmatter.

    Wired to :class:`ConventionDirectory` as the ``extract_scope`` callable for
    GitHub Copilot's instructions convention. Conductor core interprets the
    return value (see :class:`ConventionDirectory` docstring for the full
    contract):

    * ``None`` → file is opted out of auto-discovery (no parsable frontmatter,
      missing ``applyTo``, or ``applyTo`` is not a string). Matches the
      GitHub spec for "not applied automatically" files.
    * Any string → conductor core decides what to do (``"**"`` = always include;
      anything else = scoped glob, evaluated against CWD).

    Notes on what we *don't* parse:

    * GitHub's spec permits ``applyTo`` to be a single string; YAML lists are
      not part of the documented convention. Authors expressing multiple
      patterns use semicolon- or comma-separated strings — which conductor's
      overlap test splits natively. A list value here returns ``None``
      (opt-out) rather than silently joining; that mirrors the convention's
      stated shape.
    """
    fm = _parse_frontmatter(path)
    if fm is None:
        return None
    apply_to = fm.get("applyTo")
    if not isinstance(apply_to, str):
        return None
    return apply_to


# Multi-glob separators observed in real-world ``applyTo`` values: both ``;``
# and ``,`` appear in production usage (see the Azure Chaos Studio sample in
# microsoft/conductor#231). Splitting on either keeps the helper compatible
# with authors' actual writing. NOTE: this does NOT support brace expansion
# inside a single glob (e.g., ``{src,tests}/**``) — the inner comma would be
# treated as a separator and the brace would never close. We don't see brace
# expansion in real-world data; if it becomes a pain point, swap this regex
# for a brace-aware splitter.
_MULTI_GLOB_SEP_RE = re.compile(r"[;,]")

# Characters that signal the start of a wildcard segment in a path glob.
# Anything before the first segment containing one of these is a literal
# directory prefix, which is what we use for the overlap approximation.
_GLOB_WILDCARD_RE = re.compile(r"[*?\[]")


def _normalize_scope_path(p: str) -> str:
    """Normalize a path-glob or CWD-relative fragment to a comparable form.

    * Converts backslashes (Windows-authored values) to forward slashes.
    * Strips repeated ``./`` prefixes and leading ``/`` (some authors write
      ``"/docs/eng.ms/**"`` — observed in real-world data).
    * Strips a trailing ``/``.
    * Normalises ``.`` (current dir) to the empty string so "workspace root"
      has a single canonical representation.
    """
    n = p.replace("\\", "/").strip()
    while n.startswith("./"):
        n = n[2:]
    n = n.lstrip("/").rstrip("/")
    if n == ".":
        n = ""
    return n


def _single_glob_overlaps(glob: str, cwd_norm: str) -> bool:
    """Return True if a single normalized glob could match any file under
    ``cwd_norm``, OR if the glob's target subtree is itself under ``cwd_norm``.

    Uses a literal-prefix approximation: the glob's directory parts up to the
    first wildcard segment form a "minimum prefix" subtree the glob touches.
    Two subtrees overlap if either is contained in the other (or they're
    identical).

    This is intentionally conservative — it can over-include for globs like
    ``"src/*/tests/**"`` evaluated against ``"src/foo/bar"`` (literal prefix
    ``"src"`` overlaps ``"src/foo/bar"`` even though the glob can't actually
    match a non-test file under ``bar/``). Over-inclusion is the safer failure
    mode for this codepath: the alternative is silently dropping instructions
    a user reasonably expects to be loaded.
    """
    g = _normalize_scope_path(glob)

    prefix_parts: list[str] = []
    for part in g.split("/"):
        if _GLOB_WILDCARD_RE.search(part):
            break
        prefix_parts.append(part)
    prefix = "/".join(prefix_parts)

    # Empty prefix: glob's first segment is a wildcard (e.g. ``**/*.cs``),
    # so it can match anywhere in the tree → overlaps any CWD.
    if not prefix:
        return True
    # Empty CWD: user is at workspace root; everything is "inside" it.
    if not cwd_norm:
        return True
    # Either subtree contains the other.
    return (
        cwd_norm == prefix or cwd_norm.startswith(prefix + "/") or prefix.startswith(cwd_norm + "/")
    )


def _scope_overlaps(scope: str, cwd_rel: str) -> bool:
    """Return True if the ``scope`` value (a single or multi-glob string)
    overlaps with the ``cwd_rel`` subtree.

    Handles multi-glob values where authors separate sub-globs with ``;`` or
    ``,`` (both observed in production ``applyTo`` strings). Empty sub-globs
    (e.g., trailing separator) are skipped silently.

    See :func:`_single_glob_overlaps` for the per-glob semantics; this just
    short-circuits on the first matching sub-glob.
    """
    cwd_norm = _normalize_scope_path(cwd_rel)
    for sub in _MULTI_GLOB_SEP_RE.split(scope):
        sub = sub.strip()
        if sub and _single_glob_overlaps(sub, cwd_norm):
            return True
    return False


# ---------------------------------------------------------------------------
# Convention registry
# ---------------------------------------------------------------------------

# The single source of truth for what conductor auto-discovers when
# ``--workspace-instructions`` is enabled. Order is preserved in the discovery
# output: file conventions first (in declaration order), then directory
# conventions (each directory's files sorted by relative-path-within-dir).
CONVENTIONS: list[Convention] = [
    ConventionFile("AGENTS.md"),
    ConventionFile(".github/copilot-instructions.md"),
    ConventionFile("CLAUDE.md"),
    ConventionDirectory(
        path=".github/instructions",
        pattern="*.instructions.md",
        extract_scope=_extract_apply_to,
        recursive=True,
    ),
]


# Backward-compat alias: ``CONVENTION_FILES`` was module-public (no leading
# underscore) prior to the polymorphic refactor. It is not in ``__all__`` and
# was always documented as an implementation detail, but downstream code
# could import it directly. Keep it as an unconditional alias projecting only
# the file conventions, so any such import keeps working.
CONVENTION_FILES: list[str] = [c.path for c in CONVENTIONS if isinstance(c, ConventionFile)]

# Warn when total instruction content exceeds this threshold (bytes).
INSTRUCTION_SIZE_WARNING_THRESHOLD = 50 * 1024  # 50 KB


def _find_git_root(start_dir: Path) -> Path | None:
    """Find the git repository root by walking up from start_dir.

    Looks for a `.git` directory or file (worktrees use a `.git` file
    pointing to the main repo's git dir).

    Args:
        start_dir: Directory to start searching from.

    Returns:
        The git root directory, or None if not in a git repo.
    """
    current = start_dir.resolve()
    while True:
        git_path = current / ".git"
        if git_path.exists():
            return current
        parent = current.parent
        if parent == current:
            # Reached filesystem root
            return None
        current = parent


def _walk_directory_convention(
    base_dir: Path,
    convention: ConventionDirectory,
    cwd_rel: str = "",
) -> Iterator[tuple[str, Path, str | None]]:
    """Yield ``(relative_path, absolute_path, scope)`` for files in ``base_dir``
    matching ``convention.pattern`` and surviving the configured filter chain.

    The relative path is computed against ``base_dir`` and uses POSIX
    separators for cross-platform deterministic ordering.

    The ``scope`` value carried in each tuple is whatever
    ``convention.extract_scope`` returned for the file (or ``None`` when the
    convention has no ``extract_scope`` callable). Consumers can use it to
    build :class:`DiscoveredInstruction` records without re-parsing each
    file's frontmatter.

    Filter chain (per file, short-circuit on first reject):

    1. ``convention.include_file(file)`` if set.
    2. ``convention.extract_scope(file)`` if set — yields ``None``? Skip.
       Returns :data:`ALWAYS_ON_SCOPE` (``"**"``)? Include unconditionally.
       Returns any other glob string? Include only if it overlaps
       ``cwd_rel``.

    ``cwd_rel`` is interpreted as a POSIX path relative to the convention's
    *owner directory* (the directory containing ``convention.path`` —
    typically the git root, but may be a nested project dir when the
    convention directory was discovered at a non-root level). Callers that
    discover the convention at multiple levels should pass the appropriate
    per-level ``cwd_rel`` so a nested ``.github/instructions/foo.md`` with
    ``applyTo: "src/**"`` resolves ``src`` relative to that nested project,
    not the workspace root.

    Symlinked directories are NOT traversed (``followlinks=False``); symlinked
    files inside the tree are listed and read like regular files.
    """
    if convention.recursive:
        for dirpath, _dirnames, filenames in os.walk(base_dir, followlinks=False):
            for fname in fnmatch.filter(filenames, convention.pattern):
                file_path = Path(dirpath) / fname
                accepted_scope = _apply_convention_filters(convention, file_path, cwd_rel)
                if accepted_scope is _REJECT:
                    continue
                rel = file_path.relative_to(base_dir).as_posix()
                yield rel, file_path, accepted_scope
    else:
        try:
            entries = list(os.scandir(base_dir))
        except OSError as e:
            logger.debug("Cannot scan %s: %s", base_dir, e)
            return
        for entry in entries:
            # follow_symlinks=True so symlinked files are treated as regular files
            try:
                is_file = entry.is_file(follow_symlinks=True)
            except OSError:
                continue
            if not is_file:
                continue
            if not fnmatch.fnmatch(entry.name, convention.pattern):
                continue
            file_path = Path(entry.path)
            accepted_scope = _apply_convention_filters(convention, file_path, cwd_rel)
            if accepted_scope is _REJECT:
                continue
            yield entry.name, file_path, accepted_scope


# Sentinel returned by :func:`_apply_convention_filters` to signal "this file
# was rejected by one of the filters; skip it." Distinct from ``None``, which
# is a legitimate ``scope`` value meaning "no scope concept; the convention
# has no ``extract_scope`` callable."
_REJECT = object()


def _apply_convention_filters(
    convention: ConventionDirectory, file_path: Path, cwd_rel: str
) -> object:
    """Apply :class:`ConventionDirectory`'s filter chain to a candidate file.

    Returns either:

    * :data:`_REJECT` — the file failed a filter and must be skipped.
    * The scope value (``None`` for conventions without ``extract_scope``,
      otherwise whatever ``extract_scope`` returned) — the file passed.
    """
    if convention.include_file is not None and not convention.include_file(file_path):
        return _REJECT
    if convention.extract_scope is None:
        return None  # convention has no scope concept; file passes
    scope = convention.extract_scope(file_path)
    if scope is None:
        return _REJECT  # file opted out
    if scope == ALWAYS_ON_SCOPE:
        return scope  # always-on, no overlap test
    if not _scope_overlaps(scope, cwd_rel):
        return _REJECT
    return scope


def discover_workspace_instructions(start_dir: Path) -> list[Path]:
    """Discover convention instruction files by walking up to the git root.

    See :func:`discover_workspace_instructions_detailed` for the underlying
    discovery algorithm and per-file filtering provenance. This wrapper
    projects the result to just the file paths for callers that don't need
    the metadata (e.g., :func:`load_instruction_files`).
    """
    return [d.path for d in discover_workspace_instructions_detailed(start_dir)]


def discover_workspace_instructions_detailed(start_dir: Path) -> list[DiscoveredInstruction]:
    """Discover convention instruction files, returning structured metadata.

    Walks from ``start_dir`` up to the git repository root for each entry in
    :data:`CONVENTIONS`. Files closer to ``start_dir`` take precedence when
    the same logical match key exists at multiple levels (closest-wins).
    Each convention has its own discovery state, so keys do not collide
    across conventions (e.g., a hypothetical ``.cursor/rules/style.md`` would
    not shadow ``.github/instructions/style.instructions.md``).

    For :class:`ConventionDirectory` entries with a configured
    ``extract_scope`` callable, each candidate file's scope is evaluated
    against the user's CWD *relative to the convention's owner directory*
    (the directory containing the convention's path at the level where it
    was found). This means a nested
    ``services/GW/.github/instructions/foo.md`` with
    ``applyTo: "src/**"`` resolves ``src`` relative to ``services/GW`` —
    matching the "closest-wins / local-convention" semantic that makes
    nested conventions meaningful.

    Returns:
        List of :class:`DiscoveredInstruction` records. File conventions
        appear first in :data:`CONVENTIONS` declaration order; directory
        conventions follow with their files sorted by relative path within
        the convention directory.
    """
    start_resolved = start_dir.resolve()
    git_root = _find_git_root(start_dir)
    stop_at = git_root if git_root is not None else start_resolved

    result: list[DiscoveredInstruction] = []
    for convention in CONVENTIONS:
        # Per-convention state: closest-wins is local to this convention only.
        # Value carries the scope-at-discovery-time so we don't re-parse later.
        discovered: dict[str, tuple[Path, str | None]] = {}

        current = start_resolved
        while True:
            # cwd_rel is relative to the *current* walk level — that is, the
            # directory that owns the convention at this iteration. For a
            # root-level convention this equals start-dir-relative-to-git-root;
            # for a nested convention it's start-dir-relative-to-the-nested-
            # project, which is what the convention's `applyTo` globs are
            # naturally written against.
            try:
                cwd_rel = start_resolved.relative_to(current).as_posix()
            except ValueError:
                # current is somehow not an ancestor of start_resolved (e.g.,
                # symlink shenanigans). Fall back to empty CWD which makes
                # the overlap test permissive — consistent with our
                # "over-include rather than silently skip" bias.
                cwd_rel = ""
            if cwd_rel == ".":
                cwd_rel = ""

            if isinstance(convention, ConventionFile):
                if convention.path not in discovered:
                    candidate = current / convention.path
                    if candidate.is_file():
                        discovered[convention.path] = (candidate, None)
                        logger.debug("Discovered instruction file: %s", candidate)
            else:  # ConventionDirectory
                base_dir = current / convention.path
                if base_dir.is_dir():
                    for rel, abs_path, scope in _walk_directory_convention(
                        base_dir, convention, cwd_rel
                    ):
                        if rel not in discovered:
                            discovered[rel] = (abs_path, scope)
                            logger.debug("Discovered instruction file: %s", abs_path)

            if current == stop_at or current.parent == current:
                break
            current = current.parent

        # Append this convention's discoveries in deterministic order.
        if isinstance(convention, ConventionFile):
            if convention.path in discovered:
                path, _ = discovered[convention.path]
                result.append(
                    DiscoveredInstruction(
                        path=path,
                        source=convention.path,
                        scope=None,
                        reason="file-convention",
                    )
                )
        else:  # ConventionDirectory
            for rel in sorted(discovered.keys()):
                path, scope = discovered[rel]
                reason = _discovery_reason(convention, scope)
                result.append(
                    DiscoveredInstruction(
                        path=path,
                        source=convention.path,
                        scope=scope,
                        reason=reason,
                    )
                )

    return result


def _discovery_reason(convention: ConventionDirectory, scope: str | None) -> str:
    """Compute the human-readable inclusion reason for a discovered file."""
    if convention.extract_scope is None:
        # Convention with no scope concept (or include_file-only); the file
        # passed the eligibility gate and that's all there is to say.
        return "always-on"
    if scope == ALWAYS_ON_SCOPE or scope is None:
        return "always-on"
    return "scope-overlap"


def load_instruction_files(paths: list[Path]) -> str:
    """Read and concatenate instruction files into a single text block.

    Each file's content is wrapped with a source header for traceability.
    Files that cannot be read are skipped with a warning.

    Args:
        paths: List of instruction file paths to load.

    Returns:
        Concatenated instruction content, or empty string if no files loaded.
    """
    sections: list[str] = []

    for path in paths:
        try:
            # 'utf-8-sig' transparently strips a leading UTF-8 BOM. Without
            # this, BOM-authored files (common from some Windows editors)
            # would inject a leading \ufeff into the agent prompt.
            content = path.read_text(encoding="utf-8-sig").strip()
            if content:
                sections.append(f"# Instructions from: {path.name}\n\n{content}")
                logger.debug("Loaded instruction file: %s (%d bytes)", path, len(content))
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("Failed to read instruction file %s: %s", path, e)

    combined = "\n\n---\n\n".join(sections)

    if combined and len(combined.encode("utf-8")) > INSTRUCTION_SIZE_WARNING_THRESHOLD:
        size_kb = len(combined.encode("utf-8")) / 1024
        logger.warning(
            "Total workspace instructions are %.1fKB (threshold: %dKB). "
            "Large instructions consume tokens on every agent call.",
            size_kb,
            INSTRUCTION_SIZE_WARNING_THRESHOLD // 1024,
        )

    return combined


def build_inner_instructions(
    *,
    auto_discover_dir: Path | None = None,
    yaml_instructions: list[str] | None = None,
    cli_instruction_paths: list[str] | None = None,
) -> str | None:
    """Combine all instruction sources into raw (unwrapped) content.

    This returns the inner text without ``<workspace_instructions>`` tags.
    Use :func:`build_instructions_preamble` for the fully wrapped version,
    or call this directly when merging multiple instruction sources before
    wrapping once at the outermost layer (e.g. sub-workflow merging).

    Sources are combined in this order:
    1. Auto-discovered workspace files (if ``auto_discover_dir`` is provided)
    2. Workflow YAML ``instructions`` field entries
    3. CLI ``--instructions`` file paths

    Args:
        auto_discover_dir: Directory to start auto-discovery from (typically CWD).
            If None, auto-discovery is skipped.
        yaml_instructions: Instruction entries from the workflow YAML ``instructions``
            field. Each entry can be inline text or content already loaded via ``!file``.
        cli_instruction_paths: File paths provided via ``--instructions`` CLI flag.

    Returns:
        Combined raw instruction text, or None if no instructions were found.
    """
    parts: list[str] = []

    # 1. Auto-discovered workspace files
    if auto_discover_dir is not None:
        discovered = discover_workspace_instructions(auto_discover_dir)
        if discovered:
            content = load_instruction_files(discovered)
            if content:
                parts.append(content)
            logger.info("Auto-discovered %d workspace instruction file(s)", len(discovered))

    # 2. Workflow YAML instructions field
    if yaml_instructions:
        for entry in yaml_instructions:
            text = entry.strip()
            if text:
                parts.append(text)

    # 3. CLI --instructions file paths
    if cli_instruction_paths:
        cli_paths = []
        missing: list[str] = []
        for path_str in cli_instruction_paths:
            p = Path(path_str)
            if p.is_file():
                cli_paths.append(p)
            else:
                missing.append(path_str)
        if missing:
            raise FileNotFoundError(f"Instruction file(s) not found: {', '.join(missing)}")
        if cli_paths:
            content = load_instruction_files(cli_paths)
            if content:
                parts.append(content)

    if not parts:
        return None

    return "\n\n---\n\n".join(parts)


def _wrap_preamble(inner: str) -> str:
    """Wrap raw instruction content in workspace_instructions tags."""
    return (
        "<workspace_instructions>\n"
        "The following workspace instructions describe the conventions, patterns, "
        "and practices for the repository you are working in. Follow them when "
        "writing code, reviewing changes, or designing solutions.\n\n"
        f"{inner}\n"
        "</workspace_instructions>\n\n"
    )


_OPEN_TAG = "<workspace_instructions>\n"
_CLOSE_TAG = "\n</workspace_instructions>\n\n"

# The preamble header text inserted after the opening tag
_HEADER = (
    "The following workspace instructions describe the conventions, patterns, "
    "and practices for the repository you are working in. Follow them when "
    "writing code, reviewing changes, or designing solutions.\n\n"
)


def _unwrap_preamble(preamble: str) -> str:
    """Extract inner content from a wrapped preamble string.

    Strips the ``<workspace_instructions>`` tags and header text,
    returning only the raw instruction content.

    Args:
        preamble: A preamble string produced by :func:`_wrap_preamble`.

    Returns:
        The inner instruction content without wrapper tags.
    """
    inner = preamble
    if inner.startswith(_OPEN_TAG):
        inner = inner[len(_OPEN_TAG) :]
    if inner.startswith(_HEADER):
        inner = inner[len(_HEADER) :]
    if inner.endswith(_CLOSE_TAG):
        inner = inner[: -len(_CLOSE_TAG)]
    elif inner.endswith("</workspace_instructions>\n\n"):
        inner = inner[: -len("</workspace_instructions>\n\n")]
    return inner.strip()


def build_instructions_preamble(
    *,
    auto_discover_dir: Path | None = None,
    yaml_instructions: list[str] | None = None,
    cli_instruction_paths: list[str] | None = None,
) -> str | None:
    """Combine all instruction sources into a single wrapped preamble string.

    Sources are combined in this order:
    1. Auto-discovered workspace files (if ``auto_discover_dir`` is provided)
    2. Workflow YAML ``instructions`` field entries
    3. CLI ``--instructions`` file paths

    The combined content is wrapped in ``<workspace_instructions>`` tags.
    Use :func:`build_inner_instructions` to get the unwrapped content
    (e.g. for merging with a parent preamble before wrapping once).

    Args:
        auto_discover_dir: Directory to start auto-discovery from (typically CWD).
            If None, auto-discovery is skipped.
        yaml_instructions: Instruction entries from the workflow YAML ``instructions``
            field. Each entry can be inline text or content already loaded via ``!file``.
        cli_instruction_paths: File paths provided via ``--instructions`` CLI flag.

    Returns:
        Combined preamble string to prepend to agent prompts, or None if no
        instructions were found from any source.
    """
    inner = build_inner_instructions(
        auto_discover_dir=auto_discover_dir,
        yaml_instructions=yaml_instructions,
        cli_instruction_paths=cli_instruction_paths,
    )
    if inner is None:
        return None
    return _wrap_preamble(inner)
