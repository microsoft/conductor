"""Tests for notification-related schema and validator behavior.

Lightweight surface coverage: positive case + the negative cases that
are most likely to bite authors (undeclared type, payload mismatch,
unknown correlation key, bad namespace).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from conductor.config.schema import (
    AgentDef,
    InputDef,
    LimitsConfig,
    NotificationsConfig,
    NotificationTypeDef,
    OutputField,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.config.validator import validate_workflow_config
from conductor.exceptions import ConfigurationError


def _make_config(
    *,
    notifications: NotificationsConfig | None,
    agents: list[AgentDef],
    inputs: dict[str, InputDef] | None = None,
) -> WorkflowConfig:
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="test",
            entry_point=agents[0].name,
            runtime=RuntimeConfig(provider="copilot"),
            limits=LimitsConfig(max_iterations=10),
            input=inputs or {},
            notifications=notifications,
        ),
        agents=agents,
    )


class TestNotificationSchema:
    def test_valid_notification_step(self) -> None:
        agent = AgentDef(
            name="announce",
            type="notification",
            notification="pr_ready",
            payload={"pr_url": "https://x/1"},
        )
        assert agent.notification == "pr_ready"
        assert agent.payload == {"pr_url": "https://x/1"}

    def test_notification_step_without_notification_field_raises(self) -> None:
        with pytest.raises(PydanticValidationError, match="notification"):
            AgentDef(name="bad", type="notification", payload={"x": "y"})

    def test_notification_step_without_payload_raises(self) -> None:
        with pytest.raises(PydanticValidationError, match="payload"):
            AgentDef(name="bad", type="notification", notification="pr_ready")

    def test_notification_step_with_prompt_raises(self) -> None:
        with pytest.raises(PydanticValidationError, match="cannot have 'prompt'"):
            AgentDef(
                name="bad",
                type="notification",
                notification="pr_ready",
                payload={"x": "y"},
                prompt="no",
            )

    def test_notification_fields_on_non_notification_step_raises(self) -> None:
        with pytest.raises(PydanticValidationError, match="notification"):
            AgentDef(name="bad", prompt="hi", notification="pr_ready")

    def test_invalid_namespace_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="dotted lowercase"):
            NotificationsConfig(namespace="Bad-Name", types={})


class TestNotificationValidator:
    def _types(self) -> dict[str, NotificationTypeDef]:
        return {
            "pr_ready": NotificationTypeDef(
                payload={
                    "pr_url": OutputField(type="string"),
                    "pr_id": OutputField(type="number"),
                }
            )
        }

    def test_valid_workflow_passes(self) -> None:
        config = _make_config(
            inputs={"apex_id": InputDef(type="string")},
            notifications=NotificationsConfig(
                namespace="ns",
                correlation=["apex_id"],
                types=self._types(),
            ),
            agents=[
                AgentDef(
                    name="announce",
                    type="notification",
                    notification="pr_ready",
                    payload={"pr_url": "{{ workflow.input.apex_id }}", "pr_id": "1"},
                    routes=[RouteDef(to="$end")],
                )
            ],
        )
        validate_workflow_config(config)

    def test_undeclared_type_rejected(self) -> None:
        config = _make_config(
            notifications=NotificationsConfig(types=self._types()),
            agents=[
                AgentDef(
                    name="bad",
                    type="notification",
                    notification="not_declared",
                    payload={},
                    routes=[RouteDef(to="$end")],
                )
            ],
        )
        with pytest.raises(ConfigurationError, match="undeclared notification type"):
            validate_workflow_config(config)

    def test_payload_field_mismatch_rejected(self) -> None:
        config = _make_config(
            notifications=NotificationsConfig(types=self._types()),
            agents=[
                AgentDef(
                    name="bad",
                    type="notification",
                    notification="pr_ready",
                    payload={"pr_url": "x"},  # missing pr_id
                    routes=[RouteDef(to="$end")],
                )
            ],
        )
        with pytest.raises(ConfigurationError, match="missing field"):
            validate_workflow_config(config)

    def test_unknown_correlation_key_rejected(self) -> None:
        config = _make_config(
            notifications=NotificationsConfig(
                correlation=["not_an_input"],
                types=self._types(),
            ),
            agents=[
                AgentDef(
                    name="announce",
                    type="notification",
                    notification="pr_ready",
                    payload={"pr_url": "x", "pr_id": "1"},
                    routes=[RouteDef(to="$end")],
                )
            ],
        )
        with pytest.raises(ConfigurationError, match="correlation key"):
            validate_workflow_config(config)

    def test_notification_step_without_block_rejected(self) -> None:
        config = _make_config(
            notifications=None,
            agents=[
                AgentDef(
                    name="bad",
                    type="notification",
                    notification="pr_ready",
                    payload={},
                    routes=[RouteDef(to="$end")],
                )
            ],
        )
        with pytest.raises(ConfigurationError, match="no 'workflow.notifications' block"):
            validate_workflow_config(config)
