"""Skill resolution through a real :class:`WorkflowEngine` (issue #350).

``AgentExecutor`` cannot resolve a relative skill path on its own — it needs
``workflow_dir``, which only the engine knows. Tests that build an executor
directly pass that argument themselves, so they cannot detect the engine
failing to supply it: the whole feature silently degrades to "relative paths
never resolve" with every other skill test still green.

These tests drive the engine end to end for that reason. The same applies to
``runtime.skill_injection`` — an executor built by hand gets whatever limits
the test hands it, not the ones the workflow declared.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from conductor.config.schema import (
    AgentDef,
    OutputField,
    RuntimeConfig,
    SkillInjectionConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.exceptions import ExecutionError
from conductor.providers.base import AgentOutput, AgentProvider, EventCallback
from conductor.skills import SkillNotFoundError


class _CapturingProvider(AgentProvider, abstract=True):
    """Records what the executor forwarded on the last ``execute`` call."""

    native = True

    def __init__(self) -> None:
        self.skill_directories: list[str] | None = None
        self.rendered_prompt: str = ""

    @property
    def supports_native_skills(self) -> bool:
        return self.native

    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None = None,
        interrupt_signal: asyncio.Event | None = None,
        event_callback: EventCallback | None = None,
        skill_directories: list[str] | None = None,
    ) -> AgentOutput:
        self.skill_directories = skill_directories
        self.rendered_prompt = rendered_prompt
        return AgentOutput(content={"result": "done"}, raw_response="{}")

    async def validate_connection(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class _EagerProvider(_CapturingProvider, abstract=True):
    native = False


def _write_skill(directory: Path, filler: int = 0) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {directory.name}\ndescription: A test skill.\n---\nSkill body text\n"
    )
    if filler:
        (directory / "references").mkdir(exist_ok=True)
        (directory / "references" / "big.md").write_text("x" * filler)
    return directory


def _config(
    skills: list[str], skill_injection: SkillInjectionConfig | None = None
) -> WorkflowConfig:
    runtime_kwargs: dict[str, Any] = {"provider": "copilot", "skills": skills}
    if skill_injection is not None:
        runtime_kwargs["skill_injection"] = skill_injection
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="wf", entry_point="worker", runtime=RuntimeConfig(**runtime_kwargs)
        ),
        agents=[
            AgentDef(
                name="worker",
                prompt="Do the thing.",
                output={"result": OutputField(type="string")},
            )
        ],
        output={"result": "{{ worker.output.result }}"},
    )


def _run(config: WorkflowConfig, provider: AgentProvider, workflow_path: Path | None) -> None:
    engine = WorkflowEngine(config, provider, workflow_path=workflow_path)
    asyncio.run(engine.run({}))


class TestRelativeSkillPathsThroughTheEngine:
    def test_relative_path_resolves_against_the_workflow_file(self, tmp_path: Path) -> None:
        """Fails if the engine stops passing ``workflow_dir`` to the executor."""
        skill = _write_skill(tmp_path / "team-skills" / "acme")
        path = tmp_path / "wf.yaml"
        path.write_text("# placeholder\n")
        provider = _CapturingProvider()
        _run(_config(["./team-skills/acme"]), provider, path)
        assert provider.skill_directories == [str(skill)]

    def test_resolution_does_not_depend_on_the_process_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A workflow must resolve its own skills identically from any cwd."""
        skill = _write_skill(tmp_path / "flows" / "team-skills" / "acme")
        path = tmp_path / "flows" / "wf.yaml"
        path.write_text("# placeholder\n")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        provider = _CapturingProvider()
        _run(_config(["./team-skills/acme"]), provider, path)
        assert provider.skill_directories == [str(skill)]

    def test_relative_path_reaches_eager_injection_too(self, tmp_path: Path) -> None:
        skill = _write_skill(tmp_path / "team-skills" / "acme")
        path = tmp_path / "wf.yaml"
        path.write_text("# placeholder\n")
        provider = _EagerProvider()
        _run(_config(["./team-skills/acme"]), provider, path)
        assert provider.skill_directories is None
        assert '<skill name="acme">' in provider.rendered_prompt
        assert "Skill body text" in provider.rendered_prompt
        assert skill.exists()

    def test_builtin_names_work_without_a_workflow_path(self) -> None:
        provider = _CapturingProvider()
        _run(_config(["conductor"]), provider, None)
        assert provider.skill_directories is not None
        assert provider.skill_directories[0].endswith("conductor")

    def test_unresolvable_relative_path_fails_the_run(self, tmp_path: Path) -> None:
        """Surfaces as ``SkillNotFoundError``, which the CLI renders as a titled
        error panel rather than a traceback — verified manually against
        ``conductor run``."""
        path = tmp_path / "wf.yaml"
        path.write_text("# placeholder\n")
        with pytest.raises(SkillNotFoundError, match="does not exist"):
            _run(_config(["./nope"]), _CapturingProvider(), path)


class TestInjectionBudgetThroughTheEngine:
    def test_workflow_limits_reach_the_executor(self, tmp_path: Path) -> None:
        """Fails if the engine stops forwarding ``runtime.skill_injection``."""
        _write_skill(tmp_path / "acme", filler=5000)
        path = tmp_path / "wf.yaml"
        path.write_text("# placeholder\n")
        config = _config(
            ["./acme"], skill_injection=SkillInjectionConfig(warn_bytes=100, max_bytes=1000)
        )
        with pytest.raises(ExecutionError, match="max_bytes"):
            _run(config, _EagerProvider(), path)

    def test_generous_limits_allow_the_same_workflow(self, tmp_path: Path) -> None:
        _write_skill(tmp_path / "acme", filler=5000)
        path = tmp_path / "wf.yaml"
        path.write_text("# placeholder\n")
        config = _config(
            ["./acme"], skill_injection=SkillInjectionConfig(warn_bytes=None, max_bytes=None)
        )
        provider = _EagerProvider()
        _run(config, provider, path)
        assert '<skill name="acme">' in provider.rendered_prompt
