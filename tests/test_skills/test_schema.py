"""Tests for the ``skills`` field on :class:`AgentDef` and :class:`RuntimeConfig`."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conductor.config.schema import AgentDef, GateOption, RuntimeConfig


class TestAgentDefSkills:
    def test_defaults_to_none(self) -> None:
        agent = AgentDef(name="a", model="gpt-4", prompt="Hello")
        assert agent.skills is None

    def test_empty_list_means_opt_out(self) -> None:
        agent = AgentDef(name="a", model="gpt-4", prompt="Hello", skills=[])
        assert agent.skills == []

    def test_explicit_list(self) -> None:
        agent = AgentDef(name="a", model="gpt-4", prompt="Hello", skills=["conductor"])
        assert agent.skills == ["conductor"]

    def test_unknown_skill_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Unknown skill"):
            AgentDef(name="a", model="gpt-4", prompt="Hello", skills=["does-not-exist"])

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValidationError, match="non-empty strings"):
            AgentDef(name="a", model="gpt-4", prompt="Hello", skills=[""])

    def test_forbidden_on_script_agent(self) -> None:
        with pytest.raises(ValidationError, match="script agents cannot have 'skills'"):
            AgentDef(name="s", type="script", command="echo hi", skills=["conductor"])

    def test_forbidden_on_workflow_agent(self) -> None:
        with pytest.raises(ValidationError, match="workflow agents cannot have 'skills'"):
            AgentDef(name="w", type="workflow", workflow="sub.yaml", skills=["conductor"])

    def test_forbidden_on_human_gate(self) -> None:
        with pytest.raises(ValidationError, match="human_gate agents cannot have 'skills'"):
            AgentDef(
                name="g",
                type="human_gate",
                prompt="Choose:",
                options=[GateOption(label="Yes", value="y", route="next")],
                skills=["conductor"],
            )

    def test_forbidden_on_wait_agent(self) -> None:
        with pytest.raises(ValidationError, match="wait agents cannot have 'skills'"):
            AgentDef(name="w", type="wait", duration="1s", skills=["conductor"])

    def test_forbidden_on_set_agent(self) -> None:
        with pytest.raises(ValidationError, match="set agents cannot have 'skills'"):
            AgentDef(name="s", type="set", value="hello", skills=["conductor"])

    def test_forbidden_on_terminate_agent(self) -> None:
        with pytest.raises(ValidationError, match="terminate agents cannot have 'skills'"):
            AgentDef(
                name="t",
                type="terminate",
                status="success",
                reason="done",
                skills=["conductor"],
            )

    def test_allowed_on_default_type_agent(self) -> None:
        agent = AgentDef(name="r", model="gpt-4", prompt="p", skills=["conductor"])
        assert agent.skills == ["conductor"]
        assert agent.type is None

    def test_allowed_on_explicit_agent_type(self) -> None:
        agent = AgentDef(name="r", type="agent", model="gpt-4", prompt="p", skills=["conductor"])
        assert agent.skills == ["conductor"]


class TestRuntimeConfigSkills:
    def test_defaults_to_empty_list(self) -> None:
        config = RuntimeConfig()
        assert config.skills == []

    def test_can_be_set(self) -> None:
        config = RuntimeConfig(skills=["conductor"])
        assert config.skills == ["conductor"]

    def test_unknown_skill_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Unknown skill"):
            RuntimeConfig(skills=["does-not-exist"])

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValidationError, match="non-empty strings"):
            RuntimeConfig(skills=[""])


class TestPathEntriesAtSchemaLevel:
    """Path entries (issue #350) need the workflow file's directory to
    resolve, which the schema does not have — so they are shape-checked
    here and resolved in ``conductor validate`` / ``AgentExecutor``.

    Bare *names* keep their eager check, so an unknown built-in still fails
    at load time exactly as it did before paths existed.
    """

    @pytest.mark.parametrize(
        "entry",
        ["./team-skills/acme", "../shared/acme", "~/scratch/skills", "/abs/acme", "team/acme"],
    )
    def test_path_entries_are_accepted_unresolved(self, entry: str) -> None:
        assert AgentDef(name="r", prompt="p", skills=[entry]).skills == [entry]
        assert RuntimeConfig(skills=[entry]).skills == [entry]

    def test_unknown_bare_name_still_fails_at_load_time(self) -> None:
        """The pre-existing error timing must not regress: a typo'd built-in
        name needs no base directory to detect."""
        with pytest.raises(ValidationError, match="Unknown skill"):
            AgentDef(name="r", prompt="p", skills=["conductorr"])

    def test_unknown_name_error_mentions_the_path_form(self) -> None:
        with pytest.raises(ValidationError, match=r"\./team-skills/my-skill"):
            AgentDef(name="r", prompt="p", skills=["nope"])

    def test_names_and_paths_can_be_mixed(self) -> None:
        entries = ["conductor", "./team-skills/acme"]
        assert AgentDef(name="r", prompt="p", skills=entries).skills == entries

    def test_whitespace_only_entry_still_rejected(self) -> None:
        with pytest.raises(ValidationError, match="non-empty strings"):
            AgentDef(name="r", prompt="p", skills=["   "])

    def test_path_entries_still_forbidden_on_non_provider_steps(self) -> None:
        with pytest.raises(ValidationError, match="cannot have 'skills'"):
            AgentDef(name="s", type="script", command="echo hi", skills=["./a/b"])
