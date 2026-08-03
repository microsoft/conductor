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

import os
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

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
from conductor.exceptions import ConfigurationError, ExecutionError
from conductor.executor.agent import AgentExecutor
from conductor.skills import load_skill_content
from tests.test_skills.test_injection_budget import _EagerProvider

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

    def test_relative_paths_are_skipped_without_a_workflow_path(self, tmp_path: Path) -> None:
        """Mirrors ``_validate_subworkflow_refs``: with no base directory a
        relative path cannot be resolved, so it is not reported as missing.
        Absolute entries are unaffected — see the test below.

        Paired with a positive control — the same config *with* a workflow path
        must raise — so deleting the skill check outright cannot pass this.
        """
        config = _workflow(agent_skills=["./nope"])
        _validate(config, None)
        with pytest.raises(ConfigurationError, match="does not exist"):
            _validate(config, _wf_path(tmp_path))

    def test_absolute_paths_still_validate_without_a_workflow_path(self, tmp_path: Path) -> None:
        """An absolute entry needs no base directory, so the skip must not
        swallow it either.

        ``~``-prefixed entries take the same branch, since ``expanduser()``
        makes them absolute.
        """
        _make_skill(tmp_path / "acme")
        _validate(_workflow(agent_skills=[str(tmp_path / "acme")]), None)
        with pytest.raises(ConfigurationError, match="does not exist"):
            _validate(_workflow(agent_skills=[str(tmp_path / "nope")]), None)

    def test_builtin_names_still_validate_without_a_workflow_path(self) -> None:
        """Built-in names need no base directory, so the skip must not swallow
        them.

        The control is that an unknown *name* is caught earlier still — at
        config construction, by ``AgentDef.validate_skills`` — which is the
        pre-#350 error timing this change deliberately preserves.
        """
        _validate(_workflow(agent_skills=["conductor"]), None)
        with pytest.raises(PydanticValidationError, match="Unknown skill"):
            _workflow(agent_skills=["not-a-real-skill"])

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

    def test_unreadable_reference_is_reported_not_raised(self, tmp_path: Path) -> None:
        """``read_skill_frontmatter`` only opens ``SKILL.md``, so a broken
        ``references/*.md`` first surfaces here, inside ``load_skill_content``.

        Without a guard it escaped ``validate_workflow_config`` as a bare
        traceback — the one file class in a skill directory whose failure was
        reported differently from every other.
        """
        skill = _make_skill(tmp_path / "acme", filler=10)
        unreadable = skill / "references" / "big.md"
        unreadable.chmod(0o000)
        try:
            if os.access(unreadable, os.R_OK):
                pytest.skip("running as a user that bypasses file permissions")
            with pytest.raises(ConfigurationError, match="could not be read"):
                _validate(
                    _workflow(provider="claude", agent_skills=["./acme"]),
                    _wf_path(tmp_path),
                )
        finally:
            unreadable.chmod(0o644)


class TestStaticAndRuntimeBudgetAgree:
    """`conductor validate` and `conductor run` compute the injected size
    independently, in `_check_skill_injection_budget` and
    `AgentExecutor._enforce_injection_budget`. If they drift, a workflow
    passes one command and fails the other — which is the exact class of
    validate/run disagreement `_reject_unsupported_skills` exists to close.

    Both tests deliberately exercise the two paths together so neither can be
    changed in isolation.
    """

    @staticmethod
    def _bytes_reported(message: str) -> str:
        match = re.search(r"([\d,]+) bytes", message)
        assert match is not None, f"no byte count in: {message}"
        return match.group(1)

    @staticmethod
    def _runtime_executor(tmp_path: Path, limits: SkillInjectionConfig) -> AgentExecutor:
        return AgentExecutor(_EagerProvider(), workflow_dir=tmp_path, skill_injection=limits)

    def test_both_paths_report_the_same_byte_count(self, tmp_path: Path) -> None:
        _make_skill(tmp_path / "acme", filler=5000)
        limits = SkillInjectionConfig(warn_bytes=100, max_bytes=1000)

        with pytest.raises(ConfigurationError) as static_exc:
            _validate(
                _workflow(provider="claude", agent_skills=["./acme"], skill_injection=limits),
                _wf_path(tmp_path),
            )
        with pytest.raises(ExecutionError) as runtime_exc:
            self._runtime_executor(tmp_path, limits)._build_prompt_prefix(
                AgentDef(name="worker", prompt="p", skills=["./acme"])
            )

        assert self._bytes_reported(str(static_exc.value)) == self._bytes_reported(
            str(runtime_exc.value)
        )

    @pytest.mark.parametrize(
        ("delta", "should_reject"),
        [(0, False), (-1, True)],
        ids=["limit-exactly-at-size", "limit-one-byte-under"],
    )
    def test_limit_at_the_exact_rendered_size(
        self, tmp_path: Path, delta: int, should_reject: bool
    ) -> None:
        """Catches both envelope drift (measuring raw file bytes instead of the
        rendered string) and a `>` / `>=` comparison flip, on both paths."""
        skill = _make_skill(tmp_path / "acme", filler=5000)
        exact = len(load_skill_content([("acme", skill)]).encode("utf-8"))
        limits = SkillInjectionConfig(warn_bytes=None, max_bytes=exact + delta)
        config = _workflow(provider="claude", agent_skills=["./acme"], skill_injection=limits)
        agent = AgentDef(name="worker", prompt="p", skills=["./acme"])

        if should_reject:
            with pytest.raises(ConfigurationError, match="max_bytes"):
                _validate(config, _wf_path(tmp_path))
            with pytest.raises(ExecutionError, match="max_bytes"):
                self._runtime_executor(tmp_path, limits)._build_prompt_prefix(agent)
        else:
            _validate(config, _wf_path(tmp_path))
            assert self._runtime_executor(tmp_path, limits)._build_prompt_prefix(agent)
