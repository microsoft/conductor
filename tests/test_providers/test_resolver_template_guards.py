"""Defense-in-depth: provider resolvers reject a template that reached them
unresolved (#262 / #263 review).

In normal flow ``AgentExecutor`` renders ``reasoning.effort`` / ``context_tier``
before the provider runs, so the resolvers only ever see concrete literals. The
guards exercised here are the backstop for any path that reaches a resolver
without that render step: they fail loudly instead of casting a raw template
straight to the SDK.
"""

from __future__ import annotations

import pytest

from conductor.config.schema import AgentDef, ReasoningConfig
from conductor.providers.context_tier import resolve_context_tier
from conductor.providers.reasoning import resolve_reasoning_effort


def test_resolve_reasoning_effort_rejects_unrendered_expression_template() -> None:
    agent = AgentDef(
        name="test",
        prompt="Do something",
        output=None,
        reasoning=ReasoningConfig(effort="{{ workflow.input.eff }}"),
    )
    with pytest.raises(ValueError, match="reasoning.effort reached the provider unresolved"):
        resolve_reasoning_effort(agent, runtime_default=None)


def test_resolve_reasoning_effort_rejects_unrendered_statement_template() -> None:
    agent = AgentDef(
        name="test",
        prompt="Do something",
        output=None,
        reasoning=ReasoningConfig(effort="{% if x %}high{% endif %}"),
    )
    with pytest.raises(ValueError, match="unresolved"):
        resolve_reasoning_effort(agent, runtime_default=None)


def test_resolve_reasoning_effort_passes_literal_through() -> None:
    agent = AgentDef(
        name="test",
        prompt="Do something",
        output=None,
        reasoning=ReasoningConfig(effort="high"),
    )
    assert resolve_reasoning_effort(agent, runtime_default=None) == "high"


def test_resolve_context_tier_rejects_unrendered_expression_template() -> None:
    agent = AgentDef(
        name="test",
        prompt="Do something",
        output=None,
        context_tier="{{ workflow.input.tier }}",
    )
    with pytest.raises(ValueError, match="context_tier reached the provider unresolved"):
        resolve_context_tier(agent, runtime_default=None)


def test_resolve_context_tier_rejects_unrendered_statement_template() -> None:
    agent = AgentDef(
        name="test",
        prompt="Do something",
        output=None,
        context_tier="{% if x %}long_context{% endif %}",
    )
    with pytest.raises(ValueError, match="unresolved"):
        resolve_context_tier(agent, runtime_default=None)


def test_resolve_context_tier_passes_literal_through() -> None:
    agent = AgentDef(
        name="test",
        prompt="Do something",
        output=None,
        context_tier="long_context",
    )
    assert resolve_context_tier(agent, runtime_default=None) == "long_context"
