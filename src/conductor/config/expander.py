"""Stage expansion for staged agents.

This module expands agents with ``stages`` definitions into synthetic
``AgentDef`` instances at config load time, so the workflow engine sees
only regular agents and requires minimal changes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conductor.config.schema import WorkflowConfig

logger = logging.getLogger(__name__)


def expand_stages(config: WorkflowConfig) -> WorkflowConfig:
    """Expand staged agents into synthetic AgentDef instances.

    For each agent with a non-None ``stages`` dict:
    1. Creates a ``agent:default`` synthetic agent from the base definition.
    2. Creates ``agent:stage`` synthetic agents for each stage, applying
       per-stage overrides (prompt, input, output, routes, description).
    3. Rewrites bare route targets pointing to staged agents to use
       ``agent:default``.
    4. Rewrites ``entry_point`` if it references a staged agent.

    Called after Pydantic validation, before cross-reference validation.

    Args:
        config: The validated WorkflowConfig with potential staged agents.

    Returns:
        The same config, mutated in place, with synthetic agents added
        and route targets rewritten.
    """
    from conductor.config.schema import AgentDef

    staged_agents = [a for a in config.agents if a.stages]
    if not staged_agents:
        return config

    staged_agent_names = {a.name for a in staged_agents}
    existing_names = {a.name for a in config.agents}
    synthetic_agents: list[AgentDef] = []

    for agent in staged_agents:
        assert agent.stages is not None  # guaranteed by filter above
        # Validate no name collisions with existing agents
        for stage_name in agent.stages:
            synthetic_name = f"{agent.name}:{stage_name}"
            if synthetic_name in existing_names:
                from conductor.exceptions import ConfigurationError

                raise ConfigurationError(
                    f"Name collision: agent '{synthetic_name}' already exists, "
                    f"conflicts with stage '{stage_name}' of agent '{agent.name}'"
                )
        default_name = f"{agent.name}:default"
        if default_name in existing_names:
            from conductor.exceptions import ConfigurationError

            raise ConfigurationError(
                f"Name collision: agent '{default_name}' already exists, "
                f"conflicts with default stage of agent '{agent.name}'"
            )

        # Create the default synthetic agent from the base definition
        default_agent = agent.model_copy(deep=True)
        default_agent.name = default_name
        default_agent.stages = None
        synthetic_agents.append(default_agent)

        # Create one synthetic agent per stage
        for stage_name, stage_def in agent.stages.items():
            stage_agent = agent.model_copy(deep=True)
            stage_agent.name = f"{agent.name}:{stage_name}"
            stage_agent.stages = None

            # Override fields from StageDef
            if stage_def.prompt is not None:
                stage_agent.prompt = stage_def.prompt
            if stage_def.input is not None:
                stage_agent.input = stage_def.input
            if stage_def.output is not None:
                stage_agent.output = stage_def.output
            if stage_def.routes is not None:
                stage_agent.routes = stage_def.routes
            if stage_def.description is not None:
                stage_agent.description = stage_def.description

            synthetic_agents.append(stage_agent)

    # Add synthetic agents to config
    config.agents.extend(synthetic_agents)

    # Rewrite bare route targets for staged agents
    _rewrite_routes(config, staged_agent_names)

    # Rewrite entry_point
    if config.workflow.entry_point in staged_agent_names:
        config.workflow.entry_point = f"{config.workflow.entry_point}:default"

    return config


def _rewrite_routes(config: WorkflowConfig, staged_agent_names: set[str]) -> None:
    """Rewrite bare route targets pointing to staged agents.

    Any route ``to: "agent_name"`` where ``agent_name`` has stages is
    rewritten to ``to: "agent_name:default"``.

    Args:
        config: The WorkflowConfig to modify.
        staged_agent_names: Set of agent names that have stages.
    """
    # Rewrite agent routes (including synthetic agents already added)
    for agent in config.agents:
        if agent.stages is not None:
            continue  # Skip original staged agents
        for route in agent.routes:
            if route.to in staged_agent_names:
                route.to = f"{route.to}:default"

    # Rewrite parallel group routes
    for pg in config.parallel:
        for route in pg.routes:
            if route.to in staged_agent_names:
                route.to = f"{route.to}:default"

    # Rewrite for-each group routes
    for fe in config.for_each:
        for route in fe.routes:
            if route.to in staged_agent_names:
                route.to = f"{route.to}:default"
