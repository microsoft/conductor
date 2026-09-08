"""Schema tests for ``AgentDef.settings_dir``.

``settings_dir`` names the directory whose Claude Code *project* settings
tier an agent loads skills from. It exists because ``working_dir`` was doing
two unrelated jobs at once: the CLI advertises its cwd as its sole MCP root,
so narrowing cwd onto a target repository to pick up that repository's
skills also narrowed what the agent's MCP servers were permitted to read.

These tests pin the field's shape and, more importantly, that it stays
*independent* of ``working_dir`` -- a schema that coupled them, or that
silently accepted the field on a step type with no LLM session to apply it
to, would reintroduce the confusion the split removes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from conductor.config.schema import (
    AgentDef,
    GateOption,
    OutputField,
    ProviderSettings,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.config.validator import validate_workflow_config
from conductor.exceptions import ConfigurationError


class TestSettingsDirAccepted:
    """Provider-backed agents take the field, alone or alongside cwd."""

    def test_accepted_on_a_plain_llm_agent(self) -> None:
        agent = AgentDef(name="judge", prompt="review", settings_dir="/repo")
        assert agent.settings_dir == "/repo"

    def test_defaults_to_none(self) -> None:
        """Omitting it must add no directory -- the tier is opt-in."""
        assert AgentDef(name="judge", prompt="review").settings_dir is None

    def test_independent_of_working_dir(self) -> None:
        """The point of the field: both set, to different directories.

        A wide cwd keeps every path the agent's MCP servers must reach inside
        the single negotiated root, while the narrow settings_dir supplies the
        target repository's conventions.
        """
        agent = AgentDef(
            name="judge", prompt="review", working_dir="/wide", settings_dir="/wide/repo"
        )
        assert (agent.working_dir, agent.settings_dir) == ("/wide", "/wide/repo")

    def test_accepts_a_template(self) -> None:
        """The directory a step reviews is normally an upstream step's output,
        so the raw value is a Jinja template the engine renders."""
        agent = AgentDef(
            name="judge", prompt="review", settings_dir="{{ setup.output.worktree_path }}"
        )
        assert agent.settings_dir == "{{ setup.output.worktree_path }}"


class TestSettingsDirRejectedOnNonProviderSteps:
    """Every step type with no LLM session rejects it.

    Accepting it silently is the failure mode that matters here: an author
    would see a green ``conductor validate`` and conclude the target
    repository's conventions were loaded when nothing had been.
    """

    @pytest.mark.parametrize(
        ("kwargs",),
        [
            ({"type": "wait", "duration": "1s"},),
            ({"type": "set", "value": "x"},),
            ({"type": "terminate", "status": "success", "reason": "done"},),
            ({"type": "script", "command": "echo hi"},),
            ({"type": "workflow", "workflow": "child.yaml"},),
            (
                {
                    "type": "human_gate",
                    "prompt": "ok?",
                    "options": [GateOption(label="OK", value="ok", route="$end")],
                },
            ),
        ],
    )
    def test_rejected(self, kwargs: dict) -> None:
        with pytest.raises(ValidationError, match="cannot have 'settings_dir'"):
            AgentDef(name="bad", settings_dir="/repo", **kwargs)

    def test_error_names_the_step_type(self) -> None:
        """So the message says which step to fix, not merely that one is wrong."""
        with pytest.raises(ValidationError, match="wait agents cannot have 'settings_dir'"):
            AgentDef(name="bad", type="wait", duration="1s", settings_dir="/repo")


class TestSettingsDirValidation:
    """``conductor validate`` must not report success on a silent no-op.

    Every case here was green before these checks existed, which is the point:
    the step-type rejections above guard authoring mistakes nobody makes, while
    the three below are the ones an author actually makes -- wrong provider,
    forgotten `setting_sources`, typo'd upstream step name. A green validate
    for any of them tells the author their target repository's conventions
    loaded when nothing did.
    """

    @staticmethod
    def _config(provider: object, settings_dir: str, tmp_path: Path) -> WorkflowConfig:
        return WorkflowConfig(
            workflow=WorkflowDef(
                name="w",
                entry_point="a",
                runtime=RuntimeConfig(provider=provider),  # type: ignore[arg-type]
            ),
            agents=[
                AgentDef(
                    name="a",
                    prompt="hi",
                    settings_dir=settings_dir,
                    output={"r": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                )
            ],
            output={"r": "{{ a.output.r }}"},
        )

    def test_rejected_on_a_provider_that_cannot_apply_it(self, tmp_path: Path) -> None:
        """Only ``claude-agent-sdk`` has an ``add_dirs`` to put it in.

        Same class as ``working_dir``, whose capability docstring gives the
        reason: silently ignoring the directory runs the agent against the
        wrong repository while reporting success.
        """
        config = self._config("copilot", str(tmp_path), tmp_path)

        with pytest.raises(ConfigurationError, match="does not apply it"):
            validate_workflow_config(config)

    def test_accepted_on_claude_agent_sdk_with_setting_sources(self, tmp_path: Path) -> None:
        """The supported combination raises nothing."""
        config = self._config(
            ProviderSettings(name="claude-agent-sdk", setting_sources=["project"]),
            str(tmp_path),
            tmp_path,
        )

        validate_workflow_config(config)  # no raise

    def test_warns_when_no_settings_tier_is_enabled(self, tmp_path: Path) -> None:
        """A warning, not an error, and the distinction is load-bearing.

        Without ``setting_sources`` no ``project`` tier exists, so the skills
        half -- the reason the field is normally set -- is a no-op. The
        filesystem half still applies, so the workflow is not broken; erroring
        would refuse a configuration that does something.
        """
        config = self._config("claude-agent-sdk", str(tmp_path), tmp_path)

        warnings = validate_workflow_config(config)

        assert any("setting_sources" in w for w in warnings), warnings
        assert any("no skills will be discovered" in w for w in warnings), warnings

    def test_template_referencing_an_unknown_step_is_caught(self, tmp_path: Path) -> None:
        """The field is normally templated from an upstream step, so a typo'd
        step name is the likely authoring error. It must fail at validate, as
        the same typo in ``working_dir`` already does, rather than at run
        time."""
        config = self._config(
            ProviderSettings(name="claude-agent-sdk", setting_sources=["project"]),
            "{{ nonexistent_step.output.path }}",
            tmp_path,
        )

        with pytest.raises(ConfigurationError, match="nonexistent_step"):
            validate_workflow_config(config)
