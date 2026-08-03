"""Static skill validation at ``conductor validate`` time (issue #350).

``conductor run`` does **not** call :func:`validate_workflow_config`, so
resolution failures are also enforced inside ``resolve_skills`` (covered in
``tests/test_skills/test_path_entries.py``). What these tests cover is the
part that only exists statically: reporting every problem before a run
starts, and the two provider-specific checks that need the resolved
provider — ``claude-agent-sdk``'s plugin requirement and the eager-injection
budget.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from conductor.config.schema import (
    AgentDef,
    ForEachDef,
    OutputField,
    RuntimeConfig,
    SkillInjectionConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.config.validator import validate_workflow_config
from conductor.exceptions import ConfigurationError

_FRONTMATTER = "---\nname: {name}\ndescription: A test skill.\n---\nBody\n"


def _make_skill(directory: Path, *, frontmatter: str | None = None, filler: int = 0) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        frontmatter if frontmatter is not None else _FRONTMATTER.format(name=directory.name)
    )
    if filler:
        (directory / "references").mkdir(exist_ok=True)
        (directory / "references" / "big.md").write_text("x" * filler)
    return directory


def _make_plugin(root: Path, plugin_name: str, skill_name: str) -> Path:
    """Build a Claude Code plugin tree and return its skill directory."""
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(f'{{"name": "{plugin_name}"}}')
    return _make_skill(root / "skills" / skill_name)


def _workflow(
    *,
    provider: str = "copilot",
    agent_skills: list[str] | None = None,
    runtime_skills: list[str] | None = None,
    skill_injection: SkillInjectionConfig | None = None,
    for_each_skills: list[str] | None = None,
) -> WorkflowConfig:
    runtime_kwargs: dict[str, Any] = {"provider": provider}
    if runtime_skills is not None:
        runtime_kwargs["skills"] = runtime_skills
    if skill_injection is not None:
        runtime_kwargs["skill_injection"] = skill_injection

    agents = [
        AgentDef(
            name="worker",
            prompt="Do the thing.",
            skills=agent_skills,
            output={"result": OutputField(type="string")},
        )
    ]
    for_each = (
        [
            ForEachDef(
                name="fan_out",
                type="for_each",
                source="worker.output.result",
                **{"as": "item"},
                agent=AgentDef(name="inner", prompt="Handle {{ item }}", skills=for_each_skills),
            )
        ]
        if for_each_skills is not None
        else []
    )
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="wf", entry_point="worker", runtime=RuntimeConfig(**runtime_kwargs)
        ),
        agents=agents,
        for_each=for_each,
        output={"result": "{{ worker.output.result }}"},
    )


def _validate(config: WorkflowConfig, workflow_path: Path | None) -> list[str]:
    """Run validation, returning warnings. Errors raise."""
    return validate_workflow_config(config, workflow_path=workflow_path)


def _wf_path(tmp_path: Path) -> Path:
    path = tmp_path / "wf.yaml"
    path.write_text("# placeholder; validation reads the parsed config, not this file\n")
    return path


class TestPathResolution:
    def test_valid_path_skill_passes(self, tmp_path: Path) -> None:
        _make_skill(tmp_path / "team-skills" / "acme")
        _validate(_workflow(agent_skills=["./team-skills/acme"]), _wf_path(tmp_path))

    def test_missing_path_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="does not exist"):
            _validate(_workflow(agent_skills=["./nope"]), _wf_path(tmp_path))

    def test_error_names_the_agent(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="Agent 'worker'"):
            _validate(_workflow(agent_skills=["./nope"]), _wf_path(tmp_path))

    def test_relative_paths_resolve_against_the_workflow_file(self, tmp_path: Path) -> None:
        """Not the process cwd — a workflow must validate the same from anywhere."""
        nested = tmp_path / "flows"
        nested.mkdir()
        _make_skill(nested / "acme")
        path = nested / "wf.yaml"
        path.write_text("# placeholder\n")
        _validate(_workflow(agent_skills=["./acme"]), path)

    def test_relative_paths_are_skipped_without_a_workflow_path(self) -> None:
        """Mirrors ``_validate_subworkflow_refs``: with no base directory a
        relative path cannot be resolved, so it is not reported as missing."""
        _validate(_workflow(agent_skills=["./nope"]), None)

    def test_builtin_names_still_validate_without_a_workflow_path(self) -> None:
        _validate(_workflow(agent_skills=["conductor"]), None)

    def test_runtime_skills_are_validated(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="does not exist"):
            _validate(_workflow(runtime_skills=["./nope"]), _wf_path(tmp_path))

    def test_agent_opt_out_skips_inherited_runtime_skills(self, tmp_path: Path) -> None:
        """``skills: []`` overrides the workflow default, so a broken default
        must not be charged to an agent that opted out."""
        _validate(_workflow(runtime_skills=["./nope"], agent_skills=[]), _wf_path(tmp_path))

    def test_for_each_inline_agents_are_validated(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="Agent 'inner'"):
            _validate(_workflow(for_each_skills=["./nope"]), _wf_path(tmp_path))


class TestFrontmatterValidation:
    def test_unparseable_frontmatter_is_an_error(self, tmp_path: Path) -> None:
        """The exact trap from issue #350, which both CLIs skip in silence."""
        _make_skill(
            tmp_path / "acme",
            frontmatter="---\nname: acme\ndescription: Does things. Triggers: a, b\n---\n",
        )
        with pytest.raises(ConfigurationError) as exc_info:
            _validate(_workflow(agent_skills=["./acme"]), _wf_path(tmp_path))
        assert "invalid YAML frontmatter" in str(exc_info.value)
        assert "description: |" in str(exc_info.value), "the fix must be shown"

    def test_missing_description_is_an_error(self, tmp_path: Path) -> None:
        _make_skill(tmp_path / "acme", frontmatter="---\nname: acme\n---\n")
        with pytest.raises(ConfigurationError, match="no usable 'description'"):
            _validate(_workflow(agent_skills=["./acme"]), _wf_path(tmp_path))

    def test_directory_without_skill_md_is_an_error(self, tmp_path: Path) -> None:
        (tmp_path / "acme").mkdir()
        with pytest.raises(ConfigurationError, match="neither a SKILL.md nor"):
            _validate(_workflow(agent_skills=["./acme"]), _wf_path(tmp_path))


class TestClaudeAgentSdkPluginRequirement:
    """The SDK has no bare skill-directory option — only ``plugins`` +
    ``skills``. A skill outside a plugin is unreachable there, so it is
    refused statically instead of failing mid-run."""

    def test_skill_outside_a_plugin_is_refused(self, tmp_path: Path) -> None:
        _make_skill(tmp_path / "acme")
        with pytest.raises(ConfigurationError) as exc_info:
            _validate(
                _workflow(provider="claude-agent-sdk", agent_skills=["./acme"]), _wf_path(tmp_path)
            )
        message = str(exc_info.value)
        assert "not inside a Claude Code plugin" in message
        assert "copilot" in message, "the message must offer a provider that works"

    def test_skill_inside_a_plugin_is_accepted(self, tmp_path: Path) -> None:
        skill = _make_plugin(tmp_path / "plug", "acme", "widgets")
        _validate(
            _workflow(provider="claude-agent-sdk", agent_skills=[str(skill)]), _wf_path(tmp_path)
        )

    def test_broken_plugin_manifest_reports_the_reason(self, tmp_path: Path) -> None:
        skill = _make_plugin(tmp_path / "plug", "acme", "widgets")
        (tmp_path / "plug" / ".claude-plugin" / "plugin.json").write_text("{ not json")
        with pytest.raises(ConfigurationError, match="could not be read"):
            _validate(
                _workflow(provider="claude-agent-sdk", agent_skills=[str(skill)]),
                _wf_path(tmp_path),
            )

    def test_builtin_skill_is_accepted(self) -> None:
        """The bundled skill ships inside a plugin, which is how it loads there."""
        _validate(_workflow(provider="claude-agent-sdk", agent_skills=["conductor"]), None)

    def test_copilot_accepts_the_same_non_plugin_skill(self, tmp_path: Path) -> None:
        """The restriction is provider-specific, not a property of the skill."""
        _make_skill(tmp_path / "acme")
        _validate(_workflow(provider="copilot", agent_skills=["./acme"]), _wf_path(tmp_path))


class TestInjectionBudget:
    def test_oversized_injection_is_an_error(self, tmp_path: Path) -> None:
        _make_skill(tmp_path / "acme", filler=5000)
        with pytest.raises(ConfigurationError, match="max_bytes"):
            _validate(
                _workflow(
                    provider="claude",
                    agent_skills=["./acme"],
                    skill_injection=SkillInjectionConfig(warn_bytes=100, max_bytes=1000),
                ),
                _wf_path(tmp_path),
            )

    def test_between_thresholds_is_a_warning(self, tmp_path: Path) -> None:
        _make_skill(tmp_path / "acme", filler=5000)
        warnings = _validate(
            _workflow(
                provider="claude",
                agent_skills=["./acme"],
                skill_injection=SkillInjectionConfig(warn_bytes=1000, max_bytes=100_000),
            ),
            _wf_path(tmp_path),
        )
        assert any("warn_bytes" in warning for warning in warnings)

    def test_bundled_skill_on_claude_warns_but_validates(self) -> None:
        """``skills: [conductor]`` on ``claude`` works today; defaults must not
        turn an existing workflow into a validation failure."""
        warnings = _validate(_workflow(provider="claude", agent_skills=["conductor"]), None)
        assert any("progressive disclosure" in warning for warning in warnings)

    def test_native_providers_are_not_budgeted(self, tmp_path: Path) -> None:
        """Copilot loads on demand, so a large skill costs nothing up front."""
        _make_skill(tmp_path / "acme", filler=5000)
        warnings = _validate(
            _workflow(
                provider="copilot",
                agent_skills=["./acme"],
                skill_injection=SkillInjectionConfig(warn_bytes=10, max_bytes=100),
            ),
            _wf_path(tmp_path),
        )
        assert not any("skill content" in warning for warning in warnings)

    def test_hermes_is_budgeted(self, tmp_path: Path) -> None:
        """Hermes gained ``skills=True`` in this change; it injects eagerly, so
        it must be bounded like ``claude``."""
        _make_skill(tmp_path / "acme", filler=5000)
        with pytest.raises(ConfigurationError, match="max_bytes"):
            _validate(
                _workflow(
                    provider="hermes",
                    agent_skills=["./acme"],
                    skill_injection=SkillInjectionConfig(warn_bytes=100, max_bytes=1000),
                ),
                _wf_path(tmp_path),
            )
