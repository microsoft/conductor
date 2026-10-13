"""Structural guards for the agent-instruction layout (issue #574).

Root ``AGENTS.md`` is loaded into every agent session (Copilot CLI, Claude
Code through ``CLAUDE.md``'s ``@AGENTS.md`` import, Codex) and into every
``conductor run --workspace-instructions`` prompt. Detail lives in ordinary
Markdown guides under ``docs/development/`` that agents read on demand. These
tests keep that split from eroding:

* the root and the assembled Conductor preamble stay within budget;
* nothing eagerly imports a guide (``@path`` imports load at startup, so an
  import would quietly restore the old context cost);
* no file outside the repository root is named like an instruction file in
  any letter case (``docs/providers/claude.md`` was auto-discovered as
  ``CLAUDE.md`` on case-insensitive filesystems);
* every route, relative link, anchor and backticked repository path in the
  guides resolves, with exact letter case;
* every guide is reachable from the root within two links;
* a guide can be pinned with ``instructions: [!file ...]`` without the
  loader's environment-variable substitution rewriting or rejecting it.

They check structure only. Whether an agent follows a rule is not something a
unit test can prove. Each checker is a pure function with a negative control
below, so a checker that quietly matches nothing cannot report "all clear".
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

import pytest

from conductor.config.instructions import (
    build_instructions_preamble,
    discover_workspace_instructions,
)
from conductor.config.loader import ENV_VAR_PATTERN

REPO = Path(__file__).resolve().parents[1]
DEV_DIR = REPO / "docs" / "development"

# Target is 6-8 KiB; the ceiling is headroom, not a goal. Raising it needs a
# reviewed change explaining why the material must be loaded every session.
ROOT_MAX_BYTES = 12 * 1024
ROOT_MAX_LINES = 180
BRIDGE_MAX_BYTES = 256
# Repository-default auto-discovered preamble only; not a limit on what a user
# passes with --instructions or a workflow's `instructions:`.
PREAMBLE_MAX_BYTES = 16 * 1024

# Kept so historical links to the old filename still land somewhere. On a
# case-insensitive filesystem it is still discovered as `CLAUDE.md`, which is
# why it must stay tiny and inert.
POINTER = "docs/providers/claude.md"
POINTER_MAX_BYTES = 512
POINTER_MAX_LINES = 10

RESERVED_NAMES = frozenset(
    {"agents.md", "agents.override.md", "claude.md", "claude.local.md", "gemini.md"}
)
ALLOWED_RESERVED_PATHS = frozenset({"AGENTS.md", "CLAUDE.md", POINTER})

# Documents whose links and anchors must resolve. User docs are included only
# where this change moved or added links into them.
LINK_CHECKED = (
    "AGENTS.md",
    POINTER,
    "docs/providers/experimental.md",
    "docs/cli-reference.md",
    "docs/workflow-syntax.md",
)

_FENCE_RE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.MULTILINE | re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_IMPORT_RE = re.compile(r"(?:^|(?<=\s))@([^\s`)\]]*[./][^\s`)\]]*)")
_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_CODE_REF_RE = re.compile(
    r"`((?:src|tests|docs|examples|plugins)/[^`\s:]+)(?:::([A-Za-z_][\w.]*))?`"
)


def _strip_code(text: str) -> str:
    return _INLINE_CODE_RE.sub("", _FENCE_RE.sub("", text))


def size_problems(text: str, max_bytes: int, max_lines: int) -> list[str]:
    problems = []
    size = len(text.encode("utf-8"))
    if size > max_bytes:
        problems.append(f"{size} bytes > {max_bytes}")
    lines = len(text.splitlines())
    if lines > max_lines:
        problems.append(f"{lines} lines > {max_lines}")
    return problems


def import_directives(text: str) -> list[str]:
    """Return eager ``@path`` imports outside code, as Claude Code and Copilot read them.

    Only tokens that look like paths count, so `@user` mentions and e-mail
    addresses are not imports.
    """
    return _IMPORT_RE.findall(_strip_code(text))


def reserved_name_violations(paths: Iterable[str]) -> list[str]:
    return sorted(
        p
        for p in paths
        if Path(p).name.lower() in RESERVED_NAMES and p not in ALLOWED_RESERVED_PATHS
    )


def _slug(heading: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading).strip().lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def heading_slugs(text: str) -> set[str]:
    """GitHub-style anchors, including the ``-1``, ``-2`` suffixes of repeats."""
    seen: dict[str, int] = {}
    slugs = set()
    for heading in _HEADING_RE.findall(_FENCE_RE.sub("", text)):
        base = _slug(heading)
        count = seen.get(base, 0)
        seen[base] = count + 1
        slugs.add(base if count == 0 else f"{base}-{count}")
    return slugs


def exact_case_target(path: Path, root: Path) -> Path | None:
    """Resolve ``path`` inside ``root`` only if every component matches by exact case.

    ``Path.exists()`` is not enough: on macOS and Windows it accepts a
    case-only typo that breaks on Linux and on GitHub.
    """
    rel = os.path.relpath(os.path.normpath(path), root)
    if rel == "." or rel.startswith(".."):
        return root if rel == "." else None
    current = root
    for part in Path(rel).parts:
        try:
            names = os.listdir(current)
        except OSError:
            return None
        if part not in names:
            return None
        current = current / part
    return current


def link_problems(md_file: Path, root: Path) -> list[str]:
    text = md_file.read_text(encoding="utf-8")
    problems = []
    for target in _LINK_RE.findall(_strip_code(text)):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target):
            continue  # external URL, mailto:, etc.
        path_part, _, anchor = target.partition("#")
        resolved = md_file if not path_part else exact_case_target(md_file.parent / path_part, root)
        if resolved is None:
            problems.append(f"{target}: target does not exist (exact case)")
            continue
        if anchor and resolved.suffix == ".md":
            slugs = heading_slugs(resolved.read_text(encoding="utf-8"))
            if anchor not in slugs:
                problems.append(f"{target}: no heading with anchor #{anchor}")
    return problems


def code_ref_problems(md_file: Path, root: Path) -> list[str]:
    """Backticked repo paths (optionally ``::symbol``) must exist; symbols must appear."""
    problems = []
    for rel, symbol in _CODE_REF_RE.findall(md_file.read_text(encoding="utf-8")):
        if any(ch in rel for ch in "*<>{}$"):
            continue  # a pattern or placeholder, not a concrete path
        resolved = exact_case_target(root / rel.rstrip("/"), root)
        if resolved is None:
            problems.append(f"{rel}: path does not exist (exact case)")
            continue
        if symbol and resolved.is_file():
            body = resolved.read_text(encoding="utf-8", errors="replace")
            for name in symbol.split("."):
                if not re.search(rf"\b{re.escape(name)}\b", body):
                    problems.append(f"{rel}::{symbol}: '{name}' not found in file")
    return problems


def unreachable(start: Path, nodes: set[Path], root: Path, max_hops: int = 2) -> set[Path]:
    """Return ``nodes`` not reachable from ``start`` by following at most ``max_hops`` links."""
    reached: set[Path] = set()
    frontier = {start}
    for _ in range(max_hops):
        nxt: set[Path] = set()
        for doc in frontier:
            for target in _LINK_RE.findall(_strip_code(doc.read_text(encoding="utf-8"))):
                path_part = target.partition("#")[0]
                if not path_part or re.match(r"^[a-z][a-z0-9+.-]*:", target):
                    continue
                resolved = exact_case_target(doc.parent / path_part, root)
                if resolved in nodes and resolved not in reached:
                    nxt.add(resolved)
        reached |= nxt
        frontier = nxt
    return nodes - reached


def _dev_guides() -> list[Path]:
    return sorted(DEV_DIR.rglob("*.md"))


def _repo_files() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=REPO,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return out.splitlines()


# ---------------------------------------------------------------------------
# The repository
# ---------------------------------------------------------------------------


class TestBudgets:
    def test_root_agents_md_within_budget(self) -> None:
        text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
        assert size_problems(text, ROOT_MAX_BYTES, ROOT_MAX_LINES) == []

    def test_claude_md_is_only_the_import_bridge(self) -> None:
        text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
        assert len(text.encode("utf-8")) <= BRIDGE_MAX_BYTES
        assert import_directives(text) == ["AGENTS.md"]

    def test_old_claude_guide_path_is_a_tiny_pointer(self) -> None:
        text = (REPO / POINTER).read_text(encoding="utf-8")
        assert size_problems(text, POINTER_MAX_BYTES, POINTER_MAX_LINES) == []
        assert import_directives(text) == []
        assert "claude-provider.md" in text

    @pytest.mark.parametrize(
        "start",
        [
            ".",
            "src/conductor/providers",
            "src/conductor/web/frontend",
            "tests/test_engine",
            "docs/providers",
            "docs/development",
        ],
    )
    def test_assembled_workspace_preamble_within_budget(self, start: str) -> None:
        start_dir = REPO / start
        discovered = discover_workspace_instructions(start_dir)
        allowed = [REPO / p for p in ALLOWED_RESERVED_PATHS]
        for path in discovered:
            assert any(path.samefile(a) for a in allowed), f"unexpected instruction file {path}"

        preamble = build_instructions_preamble(auto_discover_dir=start_dir) or ""
        assert len(preamble.encode("utf-8")) <= PREAMBLE_MAX_BYTES
        root_text = (REPO / "AGENTS.md").read_text(encoding="utf-8").strip()
        assert preamble.count(root_text) == 1


class TestNoEagerImports:
    def test_root_agents_md_imports_nothing(self) -> None:
        assert import_directives((REPO / "AGENTS.md").read_text(encoding="utf-8")) == []

    def test_guides_import_nothing(self) -> None:
        offenders = {
            str(g.relative_to(REPO)): found
            for g in _dev_guides()
            if (found := import_directives(g.read_text(encoding="utf-8")))
        }
        assert offenders == {}


class TestFileNames:
    def test_no_instruction_file_names_outside_the_root(self) -> None:
        assert reserved_name_violations(_repo_files()) == []


class TestLinks:
    @pytest.mark.parametrize(
        "rel",
        list(LINK_CHECKED) + [str(g.relative_to(REPO)) for g in _dev_guides()],
    )
    def test_links_and_anchors_resolve(self, rel: str) -> None:
        assert link_problems(REPO / rel, REPO) == []

    @pytest.mark.parametrize(
        "rel", ["AGENTS.md"] + [str(g.relative_to(REPO)) for g in _dev_guides()]
    )
    def test_backticked_repo_paths_exist(self, rel: str) -> None:
        assert code_ref_problems(REPO / rel, REPO) == []

    def test_every_guide_is_within_two_links_of_the_root(self) -> None:
        guides = set(_dev_guides())
        assert guides, "docs/development/ has no guides"
        assert unreachable(REPO / "AGENTS.md", guides, REPO) == set()

    def test_guides_lead_with_when_to_read(self) -> None:
        missing = [
            str(g.relative_to(REPO))
            for g in _dev_guides()
            if g.name != "README.md" and "## When to read" not in g.read_text(encoding="utf-8")
        ]
        assert missing == []


class TestSafeToInclude:
    """A workflow may pin a guide with ``instructions: [!file ...]``.

    The loader substitutes environment variables in ``!file`` content, so a
    placeholder in a guide would be rewritten or, when unset, fail the load.
    """

    def test_no_env_placeholders(self) -> None:
        offenders = [
            str(p.relative_to(REPO))
            for p in [REPO / "AGENTS.md", *_dev_guides()]
            if ENV_VAR_PATTERN.search(p.read_text(encoding="utf-8"))
        ]
        assert offenders == []

    def test_negative_control(self) -> None:
        assert ENV_VAR_PATTERN.search("use `${OPENAI_API_KEY}` here")
        assert not ENV_VAR_PATTERN.search("an environment-variable reference")


# ---------------------------------------------------------------------------
# Negative controls: each checker must fail on input it exists to reject.
# ---------------------------------------------------------------------------


class TestCheckersRejectBadInput:
    def test_oversized_text(self) -> None:
        assert size_problems("x" * 20, 10, 5)
        assert size_problems("a\n" * 6, 100, 5)
        assert size_problems("ok\n", 10, 5) == []

    def test_import_detection(self) -> None:
        assert import_directives("See @docs/development/providers.md") == [
            "docs/development/providers.md"
        ]
        assert import_directives("@AGENTS.md") == ["AGENTS.md"]
        # Not imports: mentions, e-mail addresses, and anything inside code.
        text = "Ask @lesandiz or opencode@microsoft.com.\n`@AGENTS.md`\n```\n@x/y.md\n```\n"
        assert import_directives(text) == []

    def test_reserved_names_in_any_case(self) -> None:
        paths = ["AGENTS.md", POINTER, "docs/foo/Claude.md", "src/AGENTS.md", "x/gemini.MD"]
        assert reserved_name_violations(paths) == [
            "docs/foo/Claude.md",
            "src/AGENTS.md",
            "x/gemini.MD",
        ]

    def test_missing_target_anchor_and_case_typo(self, tmp_path: Path) -> None:
        (tmp_path / "Guide.md").write_text("# Title\n\n## Real Section\n")
        doc = tmp_path / "index.md"
        doc.write_text(
            "[ok](Guide.md#real-section) [gone](missing.md) "
            "[anchor](Guide.md#no-such) [case](guide.md) [self](#nope)\n# Index\n"
        )
        problems = link_problems(doc, tmp_path)
        assert [p.split(":")[0] for p in problems] == [
            "missing.md",
            "Guide.md#no-such",
            "guide.md",
            "#nope",
        ]

    def test_code_refs(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "mod.py").write_text("def real():\n    pass\n")
        doc = tmp_path / "g.md"
        doc.write_text(
            "`src/mod.py::real` `src/mod.py::gone` `src/Mod.py` `src/missing.py` `src/*.py`\n"
        )
        assert code_ref_problems(doc, tmp_path) == [
            "src/mod.py::gone: 'gone' not found in file",
            "src/Mod.py: path does not exist (exact case)",
            "src/missing.py: path does not exist (exact case)",
        ]

    def test_disconnected_guide(self, tmp_path: Path) -> None:
        root = tmp_path / "root.md"
        a, b, c, orphan = (tmp_path / n for n in ("a.md", "b.md", "c.md", "orphan.md"))
        root.write_text("[a](a.md)\n")
        a.write_text("[b](b.md)\n")
        b.write_text("[c](c.md)\n")
        c.write_text("end\n")
        orphan.write_text("nobody links here\n")
        assert unreachable(root, {a, b, c, orphan}, tmp_path) == {c, orphan}

    def test_heading_slugs_match_github(self) -> None:
        text = "# Debugging `--web-bg` failures\n## Text Includes (`!file`)\n## Same\n## Same\n"
        assert heading_slugs(text) == {
            "debugging---web-bg-failures",
            "text-includes-file",
            "same",
            "same-1",
        }
