"""Host path spelling stays separate from Docker workspace spelling."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from conductor.exceptions import ConfigurationError
from conductor.execution.docker_paths import map_agent_paths
from conductor.execution.types import AgentSpec, BundleRef


def _bundle(tmp_path: Path, *, skill: Path | None = None) -> BundleRef:
    store = tmp_path / "bundle"
    (store / "tree/main").mkdir(parents=True)
    logical = "tree/skills/review/SKILL.md"
    entries = []
    if skill is not None:
        entries.append(
            {
                "logical_path": logical,
                "digest": "sha256:" + hashlib.sha256((skill / "SKILL.md").read_bytes()).hexdigest(),
            }
        )
    (store / "bundle.json").write_text(
        json.dumps(
            {
                "entries": entries,
                "skills_topology": {"review": logical} if skill is not None else {},
                "plugins_topology": {},
            }
        ),
        encoding="utf-8",
    )
    return BundleRef("sha256:test", str(store))


def _spec() -> AgentSpec:
    return AgentSpec(
        name="agent",
        execution_id="exec-1",
        model_provider="copilot",
        model=None,
        rendered_prompt="work",
    )


def test_skill_host_case_uses_platform_normcase(tmp_path: Path) -> None:
    # Requirement: Windows case aliases resolve, while POSIX paths remain case-sensitive.
    skill = tmp_path / "SkillReview"
    skill.mkdir()
    (skill / "SKILL.md").write_text("skill", encoding="utf-8")
    bundle = _bundle(tmp_path, skill=skill)
    spec = replace(_spec(), skill_directories=(str(skill),))
    exact = replace(bundle, agent_paths=((str(skill), "skills/review"),))
    assert map_agent_paths(spec, exact).skill_directories == ("/workspace/skills/review",)

    alias = str(skill).swapcase()
    differently_cased = replace(bundle, agent_paths=((alias, "skills/review"),))
    if os.path.normcase(alias) == os.path.normcase(str(skill)):
        assert map_agent_paths(spec, differently_cased).skill_directories == (
            "/workspace/skills/review",
        )
    else:
        with pytest.raises(ConfigurationError, match="not present in the staged bundle"):
            map_agent_paths(spec, differently_cased)


def test_relative_working_dir_uses_host_separators(tmp_path: Path) -> None:
    # Requirement: host separators resolve before the path is emitted as a POSIX workspace path.
    bundle = _bundle(tmp_path)
    staged = Path(bundle.store_path) / "tree/main"
    (staged / "src/pkg").mkdir(parents=True)
    assert map_agent_paths(replace(_spec(), working_dir="src/pkg"), bundle).working_dir == (
        "/workspace/main/src/pkg"
    )

    authored = r"src\pkg"
    parts = Path(os.path.normpath(authored)).parts
    staged.joinpath(*parts).mkdir(parents=True, exist_ok=True)
    assert map_agent_paths(replace(_spec(), working_dir=authored), bundle).working_dir == (
        "/workspace/main/" + "/".join(parts)
    )
