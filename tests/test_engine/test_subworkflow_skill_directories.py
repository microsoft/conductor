"""Tests for sub-workflow skill_directories resolution and provider augmentation.

Tests cover:
- Sub-workflow skill_directories resolved relative to sub-workflow YAML path
- Provider skill_directories augmented during child execution and restored after
- Sub-workflow without skill_directories leaves provider unchanged
- Multiple sub-workflows apply and restore independently
- Deduplication when parent and child have overlapping directories
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    LimitsConfig,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.providers.copilot import CopilotProvider


@pytest.fixture
def tmp_workflow_dir(tmp_path: Path) -> Path:
    """Create a temporary directory with sub-workflow files."""
    return tmp_path


def _write_yaml(path: Path, content: str) -> Path:
    """Write YAML content to a file and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


class TestSubWorkflowSkillDirectoriesResolution:
    """Tests for _resolve_subworkflow_skill_directories."""

    @pytest.mark.asyncio
    async def test_relative_dirs_resolved_from_subworkflow_path(
        self, tmp_workflow_dir: Path
    ) -> None:
        """Relative skill_directories in sub-workflow are resolved relative to its YAML."""
        # Create skills directory
        skills_dir = tmp_workflow_dir / "phases" / "skills"
        skills_dir.mkdir(parents=True)

        # Sub-workflow with relative skill_directories
        _write_yaml(
            tmp_workflow_dir / "phases" / "sub.yaml",
            """\
            workflow:
              name: sub-workflow
              entry_point: worker
              runtime:
                provider: copilot
                skill_directories:
                  - ./skills
              limits:
                max_iterations: 5
            agents:
              - name: worker
                prompt: "Do work"
                routes:
                  - to: "$end"
            output:
              result: "{{ worker.output.result }}"
            """,
        )

        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="phase",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="phase",
                    type="workflow",
                    workflow="phases/sub.yaml",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ phase.output.result }}"},
        )

        # Track skill_directories seen at execution time
        seen_skill_dirs: list[list[str]] = []

        def mock_handler(agent: Any, prompt: str, context: dict) -> dict:
            return {"result": "done"}

        provider = CopilotProvider(mock_handler=mock_handler)

        # Monkey-patch to capture skill_directories at session time
        original_execute = provider.execute

        async def capturing_execute(*args: Any, **kwargs: Any) -> Any:
            seen_skill_dirs.append(provider.get_skill_directories())
            return await original_execute(*args, **kwargs)

        provider.execute = capturing_execute  # type: ignore[assignment]

        engine = WorkflowEngine(config, provider, workflow_path=parent_path)
        await engine.run({})

        # Skill directories should have been set during sub-workflow execution
        assert len(seen_skill_dirs) == 1
        assert seen_skill_dirs[0] == [str(skills_dir.resolve())]

    @pytest.mark.asyncio
    async def test_absolute_dirs_kept_unchanged(self, tmp_workflow_dir: Path) -> None:
        """Absolute skill_directories in sub-workflow are preserved as-is."""
        abs_skills = "/opt/shared/skills"

        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            f"""\
            workflow:
              name: sub-workflow
              entry_point: worker
              runtime:
                provider: copilot
                skill_directories:
                  - {abs_skills}
              limits:
                max_iterations: 5
            agents:
              - name: worker
                prompt: "Do work"
                routes:
                  - to: "$end"
            output:
              result: "{{{{ worker.output.result }}}}"
            """,
        )

        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="phase",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="phase",
                    type="workflow",
                    workflow="sub.yaml",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ phase.output.result }}"},
        )

        seen_skill_dirs: list[list[str]] = []

        def mock_handler(agent: Any, prompt: str, context: dict) -> dict:
            return {"result": "done"}

        provider = CopilotProvider(mock_handler=mock_handler)
        original_execute = provider.execute

        async def capturing_execute(*args: Any, **kwargs: Any) -> Any:
            seen_skill_dirs.append(provider.get_skill_directories())
            return await original_execute(*args, **kwargs)

        provider.execute = capturing_execute  # type: ignore[assignment]

        engine = WorkflowEngine(config, provider, workflow_path=parent_path)
        await engine.run({})

        assert seen_skill_dirs[0] == [abs_skills]


class TestSubWorkflowSkillDirectoriesRestoration:
    """Tests for provider skill_directories restoration after sub-workflow."""

    @pytest.mark.asyncio
    async def test_provider_restored_after_subworkflow(
        self, tmp_workflow_dir: Path
    ) -> None:
        """Provider's skill_directories are restored after sub-workflow completes."""
        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            """\
            workflow:
              name: sub-workflow
              entry_point: worker
              runtime:
                provider: copilot
                skill_directories:
                  - ./child_skills
              limits:
                max_iterations: 5
            agents:
              - name: worker
                prompt: "Do work"
                routes:
                  - to: "$end"
            output:
              result: "{{ worker.output.result }}"
            """,
        )

        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="phase",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="phase",
                    type="workflow",
                    workflow="sub.yaml",
                    routes=[RouteDef(to="post")],
                ),
                AgentDef(
                    name="post",
                    prompt="Post-processing",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ post.output.result }}"},
        )

        parent_skills = ["/parent/skills"]

        def mock_handler(agent: Any, prompt: str, context: dict) -> dict:
            return {"result": "done"}

        provider = CopilotProvider(
            mock_handler=mock_handler,
            skill_directories=parent_skills,
        )

        engine = WorkflowEngine(config, provider, workflow_path=parent_path)
        await engine.run({})

        # After execution, provider should be back to original directories
        assert provider.get_skill_directories() == parent_skills

    @pytest.mark.asyncio
    async def test_no_skill_dirs_leaves_provider_unchanged(
        self, tmp_workflow_dir: Path
    ) -> None:
        """Sub-workflow without skill_directories doesn't modify the provider."""
        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            """\
            workflow:
              name: sub-workflow
              entry_point: worker
              runtime:
                provider: copilot
              limits:
                max_iterations: 5
            agents:
              - name: worker
                prompt: "Do work"
                routes:
                  - to: "$end"
            output:
              result: "{{ worker.output.result }}"
            """,
        )

        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="phase",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="phase",
                    type="workflow",
                    workflow="sub.yaml",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ phase.output.result }}"},
        )

        parent_skills = ["/parent/skills"]

        def mock_handler(agent: Any, prompt: str, context: dict) -> dict:
            return {"result": "done"}

        provider = CopilotProvider(
            mock_handler=mock_handler,
            skill_directories=parent_skills,
        )

        engine = WorkflowEngine(config, provider, workflow_path=parent_path)
        await engine.run({})

        # Provider should still have its original directories
        assert provider.get_skill_directories() == parent_skills

    @pytest.mark.asyncio
    async def test_deduplication_when_parent_and_child_overlap(
        self, tmp_workflow_dir: Path
    ) -> None:
        """Overlapping directories between parent and child are deduplicated."""
        shared_dir = str((tmp_workflow_dir / "shared_skills").resolve())

        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            f"""\
            workflow:
              name: sub-workflow
              entry_point: worker
              runtime:
                provider: copilot
                skill_directories:
                  - {shared_dir}
                  - ./extra_skills
              limits:
                max_iterations: 5
            agents:
              - name: worker
                prompt: "Do work"
                routes:
                  - to: "$end"
            output:
              result: "{{{{ worker.output.result }}}}"
            """,
        )

        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="phase",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="phase",
                    type="workflow",
                    workflow="sub.yaml",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ phase.output.result }}"},
        )

        seen_skill_dirs: list[list[str]] = []

        def mock_handler(agent: Any, prompt: str, context: dict) -> dict:
            return {"result": "done"}

        provider = CopilotProvider(
            mock_handler=mock_handler,
            skill_directories=[shared_dir],
        )
        original_execute = provider.execute

        async def capturing_execute(*args: Any, **kwargs: Any) -> Any:
            seen_skill_dirs.append(provider.get_skill_directories())
            return await original_execute(*args, **kwargs)

        provider.execute = capturing_execute  # type: ignore[assignment]

        engine = WorkflowEngine(config, provider, workflow_path=parent_path)
        await engine.run({})

        # shared_dir should appear only once (not duplicated)
        extra = str((tmp_workflow_dir / "extra_skills").resolve())
        assert seen_skill_dirs[0] == [shared_dir, extra]

    @pytest.mark.asyncio
    async def test_restored_after_subworkflow_failure(
        self, tmp_workflow_dir: Path
    ) -> None:
        """Provider skill_directories are restored even if sub-workflow fails."""
        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            """\
            workflow:
              name: sub-workflow
              entry_point: worker
              runtime:
                provider: copilot
                skill_directories:
                  - ./child_skills
              limits:
                max_iterations: 5
            agents:
              - name: worker
                prompt: "Do work"
                routes:
                  - to: "$end"
            output:
              result: "{{ worker.output.result }}"
            """,
        )

        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="phase",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="phase",
                    type="workflow",
                    workflow="sub.yaml",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ phase.output.result }}"},
        )

        parent_skills = ["/parent/skills"]

        def failing_handler(agent: Any, prompt: str, context: dict) -> dict:
            raise RuntimeError("Simulated failure")

        provider = CopilotProvider(
            mock_handler=failing_handler,
            skill_directories=parent_skills,
        )

        engine = WorkflowEngine(config, provider, workflow_path=parent_path)

        with pytest.raises(Exception):
            await engine.run({})

        # Even after failure, provider should be restored
        assert provider.get_skill_directories() == parent_skills


class TestSubWorkflowSkillDirectoriesSequential:
    """Tests for sequential sub-workflows with different skill_directories."""

    @pytest.mark.asyncio
    async def test_sequential_subworkflows_apply_and_restore(
        self, tmp_workflow_dir: Path
    ) -> None:
        """Each sequential sub-workflow gets its own dirs, restored between phases."""
        _write_yaml(
            tmp_workflow_dir / "phase1.yaml",
            """\
            workflow:
              name: phase1
              entry_point: agent1
              runtime:
                provider: copilot
                skill_directories:
                  - ./skills1
              limits:
                max_iterations: 5
            agents:
              - name: agent1
                prompt: "Phase 1"
                routes:
                  - to: "$end"
            output:
              result: "{{ agent1.output.result }}"
            """,
        )

        _write_yaml(
            tmp_workflow_dir / "phase2.yaml",
            """\
            workflow:
              name: phase2
              entry_point: agent2
              runtime:
                provider: copilot
                skill_directories:
                  - ./skills2
              limits:
                max_iterations: 5
            agents:
              - name: agent2
                prompt: "Phase 2"
                routes:
                  - to: "$end"
            output:
              result: "{{ agent2.output.result }}"
            """,
        )

        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="p1",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="p1",
                    type="workflow",
                    workflow="phase1.yaml",
                    routes=[RouteDef(to="p2")],
                ),
                AgentDef(
                    name="p2",
                    type="workflow",
                    workflow="phase2.yaml",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ p2.output.result }}"},
        )

        seen_skill_dirs: list[list[str]] = []

        def mock_handler(agent: Any, prompt: str, context: dict) -> dict:
            return {"result": "done"}

        provider = CopilotProvider(mock_handler=mock_handler)
        original_execute = provider.execute

        async def capturing_execute(*args: Any, **kwargs: Any) -> Any:
            seen_skill_dirs.append(provider.get_skill_directories())
            return await original_execute(*args, **kwargs)

        provider.execute = capturing_execute  # type: ignore[assignment]

        engine = WorkflowEngine(config, provider, workflow_path=parent_path)
        await engine.run({})

        # Phase 1 should have skills1, phase 2 should have skills2
        skills1 = str((tmp_workflow_dir / "skills1").resolve())
        skills2 = str((tmp_workflow_dir / "skills2").resolve())
        assert seen_skill_dirs[0] == [skills1]
        assert seen_skill_dirs[1] == [skills2]

        # Provider should be back to empty after both phases
        assert provider.get_skill_directories() == []


class TestCopilotProviderSkillDirectoriesAccessors:
    """Tests for get/set_skill_directories on CopilotProvider."""

    def test_get_returns_copy(self) -> None:
        """get_skill_directories returns a copy, not the internal list."""
        provider = CopilotProvider(
            mock_handler=lambda a, p, c: {},
            skill_directories=["/a", "/b"],
        )
        dirs = provider.get_skill_directories()
        dirs.append("/c")
        assert provider.get_skill_directories() == ["/a", "/b"]

    def test_set_replaces_list(self) -> None:
        """set_skill_directories replaces the internal list."""
        provider = CopilotProvider(
            mock_handler=lambda a, p, c: {},
            skill_directories=["/old"],
        )
        provider.set_skill_directories(["/new1", "/new2"])
        assert provider.get_skill_directories() == ["/new1", "/new2"]

    def test_default_empty(self) -> None:
        """Default skill_directories is empty list."""
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        assert provider.get_skill_directories() == []
