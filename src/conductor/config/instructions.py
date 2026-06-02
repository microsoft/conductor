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
    ``recursive=True``), apply the optional ``include_file`` predicate, and
    track surviving files keyed by their *relative path within the convention
    directory*. Closest-wins per relative-path key — local to this convention.

    Symlink policy: directory traversal does NOT follow symlinked directories
    (``os.walk(followlinks=False)``) to avoid loops and out-of-tree expansion.
    Symlinked instruction *files* are read like regular files via
    :meth:`Path.read_text` (which follows symlinks at the leaf).
    """

    path: str
    pattern: str
    include_file: Callable[[Path], bool] | None = None
    recursive: bool = True


Convention = ConventionFile | ConventionDirectory


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


def _is_always_on_instructions_file(path: Path) -> bool:
    """Return True iff a ``.github/instructions/*.instructions.md`` file is
    explicitly always-on per GitHub's documented convention semantics.

    Per
    https://docs.github.com/en/copilot/customizing-copilot/about-customizing-github-copilot-chat-responses
    and https://code.visualstudio.com/docs/copilot/customization/custom-instructions:

    * ``applyTo: "**"`` → "always applied" → INCLUDE in conductor preamble
    * ``applyTo: "<other glob>"`` → scoped to matched files in chat → SKIP
      (conductor has no per-agent file-scope concept)
    * ``applyTo`` absent → "not applied automatically; you can still add them
      manually to a chat request" → SKIP

    Conductor's preamble is always-on for every agent prompt. To honor the
    convention exactly, only files explicitly marked ``applyTo: "**"`` are
    loaded.
    """
    fm = _parse_frontmatter(path)
    if fm is None:
        return False
    return fm.get("applyTo") == "**"


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
        include_file=_is_always_on_instructions_file,
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
    base_dir: Path, convention: ConventionDirectory
) -> Iterator[tuple[str, Path]]:
    """Yield ``(relative_path, absolute_path)`` for files in ``base_dir``
    matching ``convention.pattern`` and (if set) ``convention.include_file``.

    The relative path is computed against ``base_dir`` and uses POSIX
    separators for cross-platform deterministic ordering.

    Symlinked directories are NOT traversed (``followlinks=False``); symlinked
    files inside the tree are listed and read like regular files.
    """
    if convention.recursive:
        for dirpath, _dirnames, filenames in os.walk(base_dir, followlinks=False):
            for fname in fnmatch.filter(filenames, convention.pattern):
                file_path = Path(dirpath) / fname
                if convention.include_file is not None and not convention.include_file(file_path):
                    continue
                rel = file_path.relative_to(base_dir).as_posix()
                yield rel, file_path
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
            if convention.include_file is not None and not convention.include_file(file_path):
                continue
            yield entry.name, file_path


def discover_workspace_instructions(start_dir: Path) -> list[Path]:
    """Discover convention instruction files by walking up to the git root.

    Searches from ``start_dir`` up to the git repository root for each entry
    in :data:`CONVENTIONS`. Files closer to ``start_dir`` take precedence
    when the same logical match key exists at multiple levels (closest-wins).

    Each convention has its own discovery state, so keys do not collide
    across conventions (e.g., a hypothetical ``.cursor/rules/style.md`` would
    not shadow ``.github/instructions/style.instructions.md``).

    Returns:
        List of discovered instruction file paths. File conventions appear
        first in their :data:`CONVENTIONS` declaration order; directory
        conventions follow with their files sorted by relative path within
        the convention directory.
    """
    git_root = _find_git_root(start_dir)
    stop_at = git_root if git_root is not None else start_dir.resolve()

    result: list[Path] = []
    for convention in CONVENTIONS:
        # Per-convention state: closest-wins is local to this convention only.
        discovered: dict[str, Path] = {}

        current = start_dir.resolve()
        while True:
            if isinstance(convention, ConventionFile):
                if convention.path not in discovered:
                    candidate = current / convention.path
                    if candidate.is_file():
                        discovered[convention.path] = candidate
                        logger.debug("Discovered instruction file: %s", candidate)
            else:  # ConventionDirectory
                base_dir = current / convention.path
                if base_dir.is_dir():
                    for rel, abs_path in _walk_directory_convention(base_dir, convention):
                        if rel not in discovered:
                            discovered[rel] = abs_path
                            logger.debug("Discovered instruction file: %s", abs_path)

            if current == stop_at or current.parent == current:
                break
            current = current.parent

        # Append this convention's discoveries in deterministic order.
        if isinstance(convention, ConventionFile):
            if convention.path in discovered:
                result.append(discovered[convention.path])
        else:  # ConventionDirectory
            for rel in sorted(discovered.keys()):
                result.append(discovered[rel])

    return result


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
