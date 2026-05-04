"""Workspace instruction file discovery and loading.

This module discovers and loads workspace instruction files (AGENTS.md,
CLAUDE.md, copilot-instructions.md, etc.) that provide context about a
repository's conventions, coding style, and architecture to workflow agents.

The primary use case is enabling conductor workflows (which may live in
distant skill directories) to automatically pick up the target repository's
instruction files when invoked from within that repo.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Convention instruction files to discover, in deterministic order.
# Each entry is a relative path from a directory to check.
CONVENTION_FILES: list[str] = [
    "AGENTS.md",
    ".github/copilot-instructions.md",
    "CLAUDE.md",
]

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


def discover_workspace_instructions(start_dir: Path) -> list[Path]:
    """Discover convention instruction files by walking up to the git root.

    Searches from ``start_dir`` up to the git repository root for known
    convention files (AGENTS.md, .github/copilot-instructions.md, CLAUDE.md).
    Files closer to ``start_dir`` take precedence when the same filename
    exists at multiple levels.

    Args:
        start_dir: Directory to start discovery from (typically CWD).

    Returns:
        List of discovered instruction file paths in deterministic order,
        grouped by convention file type.
    """
    git_root = _find_git_root(start_dir)
    stop_at = git_root if git_root is not None else start_dir.resolve()

    # Track which convention file names we've already found (closest wins).
    found_names: set[str] = set()
    discovered: dict[str, Path] = {}

    current = start_dir.resolve()
    while True:
        for convention_file in CONVENTION_FILES:
            if convention_file in found_names:
                continue
            candidate = current / convention_file
            if candidate.is_file():
                found_names.add(convention_file)
                discovered[convention_file] = candidate
                logger.debug("Discovered instruction file: %s", candidate)

        if current == stop_at or current.parent == current:
            break
        current = current.parent

    # Return in the deterministic order defined by CONVENTION_FILES
    return [discovered[name] for name in CONVENTION_FILES if name in discovered]


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
            content = path.read_text(encoding="utf-8").strip()
            if content:
                sections.append(
                    f"# Instructions from: {path.name}\n\n{content}"
                )
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


def build_instructions_preamble(
    *,
    auto_discover_dir: Path | None = None,
    yaml_instructions: list[str] | None = None,
    cli_instruction_paths: list[str] | None = None,
) -> str | None:
    """Combine all instruction sources into a single preamble string.

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
        Combined preamble string to prepend to agent prompts, or None if no
        instructions were found from any source.
    """
    parts: list[str] = []

    # 1. Auto-discovered workspace files
    if auto_discover_dir is not None:
        discovered = discover_workspace_instructions(auto_discover_dir)
        if discovered:
            content = load_instruction_files(discovered)
            if content:
                parts.append(content)
            logger.info(
                "Auto-discovered %d workspace instruction file(s)", len(discovered)
            )

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
            raise FileNotFoundError(
                f"Instruction file(s) not found: {', '.join(missing)}"
            )
        if cli_paths:
            content = load_instruction_files(cli_paths)
            if content:
                parts.append(content)

    if not parts:
        return None

    preamble = "\n\n---\n\n".join(parts)

    return (
        "<workspace_instructions>\n"
        "The following workspace instructions describe the conventions, patterns, "
        "and practices for the repository you are working in. Follow them when "
        "writing code, reviewing changes, or designing solutions.\n\n"
        f"{preamble}\n"
        "</workspace_instructions>\n\n"
    )
