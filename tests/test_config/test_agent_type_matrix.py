"""Which optional ``AgentDef`` fields each step type accepts.

Fields that only mean something for provider-backed LLM agents are rejected
on the other step types at load time, so a typo surfaces in ``conductor
validate`` rather than being silently ignored at runtime.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from conductor.config.schema import AgentDef, GateOption


class TestSessionKeyTypeMatrix:
    """Requirement: ``session_key`` is allowed only on provider-backed LLM
    agents — every other step type has no provider session to continue."""

    def test_session_key_allowed_on_llm_agent(self) -> None:
        agent = AgentDef(name="llm", prompt="hi", session_key="investigation")
        assert agent.session_key == "investigation"

    def test_session_key_defaults_to_none(self) -> None:
        assert AgentDef(name="llm", prompt="hi").session_key is None

    def test_empty_session_key_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="session_key"):
            AgentDef(name="llm", prompt="hi", session_key="")

    @pytest.mark.parametrize(
        "kwargs,match",
        [
            (
                {"name": "sc", "type": "script", "command": "ls"},
                "script agents cannot have 'session_key'",
            ),
            (
                {"name": "q", "type": "questions", "questions": [{"id": "q1", "text": "Why?"}]},
                "questions agents cannot have 'session_key'",
            ),
            (
                {"name": "w", "type": "wait", "duration": "1s"},
                "wait agents cannot have 'session_key'",
            ),
            (
                {"name": "s", "type": "set", "value": "1"},
                "set agents cannot have 'session_key'",
            ),
            (
                {"name": "t", "type": "terminate", "status": "success", "reason": "done"},
                "terminate agents cannot have 'session_key'",
            ),
            (
                {
                    "name": "g",
                    "type": "human_gate",
                    "prompt": "Pick",
                    "options": [GateOption(label="Yes", value="yes", route="$end")],
                },
                "human_gate agents cannot have 'session_key'",
            ),
            (
                {"name": "wf", "type": "workflow", "workflow": "./sub.yaml"},
                "workflow agents cannot have 'session_key'",
            ),
        ],
        ids=["script", "questions", "wait", "set", "terminate", "human_gate", "workflow"],
    )
    def test_session_key_rejected(self, kwargs: dict, match: str) -> None:
        with pytest.raises(PydanticValidationError, match=match):
            AgentDef(**kwargs, session_key="investigation")  # type: ignore[arg-type]


class TestSessionKeyLiteral:
    """``session_key`` is never rendered, so a template must not pass silently."""

    @pytest.mark.parametrize(
        "value",
        ["item-{{ _key }}", "{{ workflow.input.id }}", "{% if x %}a{% endif %}"],
        ids=["expression", "input-ref", "statement"],
    )
    def test_template_rejected(self, value: str) -> None:
        with pytest.raises(PydanticValidationError, match="never rendered"):
            AgentDef(name="a", prompt="hi", session_key=value)

    def test_static_label_accepted(self) -> None:
        assert AgentDef(name="a", prompt="hi", session_key="investigation").session_key == (
            "investigation"
        )
