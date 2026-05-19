"""Tests for the NotificationExecutor envelope builder.

Covers the parts of the envelope that downstream consumers (Polyphony,
JSONL tail subscribers) will depend on: the deterministic ``emission_id``,
the namespaced ``schema_id``, payload rendering + type validation, and
correlation propagation.
"""

from __future__ import annotations

import pytest

from conductor.config.schema import (
    AgentDef,
    NotificationsConfig,
    NotificationTypeDef,
    OutputField,
)
from conductor.exceptions import ValidationError
from conductor.executor.notification import (
    NotificationExecutor,
    build_emission_id,
    slug_namespace,
)


@pytest.fixture
def executor() -> NotificationExecutor:
    return NotificationExecutor()


@pytest.fixture
def pr_ready_config() -> NotificationsConfig:
    return NotificationsConfig(
        namespace="polyphony.feature_pr",
        correlation=["apex_id"],
        types={
            "pr_ready": NotificationTypeDef(
                version=1,
                payload={
                    "pr_url": OutputField(type="string"),
                    "pr_id": OutputField(type="number"),
                },
            ),
        },
    )


def _step(name: str = "announce_ready") -> AgentDef:
    return AgentDef(
        name=name,
        type="notification",
        notification="pr_ready",
        payload={
            "pr_url": "{{ workflow.input.pr_url }}",
            "pr_id": "{{ workflow.input.pr_id }}",
        },
    )


class TestEnvelope:
    def test_envelope_shape(
        self, executor: NotificationExecutor, pr_ready_config: NotificationsConfig
    ) -> None:
        env = executor.build_envelope(
            _step(),
            pr_ready_config,
            context={"workflow": {"input": {"pr_url": "https://x/42", "pr_id": "42"}}},
            workflow_name="feature-pr",
            run_id="run123",
            subworkflow_path=[],
            iteration=1,
            correlation={"apex_id": "apex-1"},
            workflow_metadata={},
        )

        assert env["schema_id"] == "polyphony.feature_pr.pr_ready@1"
        assert env["namespace"] == "polyphony.feature_pr"
        assert env["notification_type"] == "pr_ready"
        assert env["version"] == 1
        assert env["emission_id"] == "run123:announce_ready:1"
        assert env["source_agent"] == "announce_ready"
        assert env["correlation"] == {"apex_id": "apex-1"}
        # number field rendered from a string template gets json-coerced
        assert env["payload"] == {"pr_url": "https://x/42", "pr_id": 42}

    def test_emission_id_includes_subworkflow_path(
        self, executor: NotificationExecutor, pr_ready_config: NotificationsConfig
    ) -> None:
        env = executor.build_envelope(
            _step(),
            pr_ready_config,
            context={"workflow": {"input": {"pr_url": "x", "pr_id": "1"}}},
            workflow_name="feature-pr",
            run_id="r1",
            subworkflow_path=["wave_dispatch", "dispatch_items.3"],
            iteration=2,
            correlation={},
            workflow_metadata={},
        )
        assert env["emission_id"] == "r1:wave_dispatch/dispatch_items.3/announce_ready:2"
        assert env["subworkflow_path"] == ["wave_dispatch", "dispatch_items.3"]

    def test_namespace_defaults_to_slugified_workflow_name(
        self, executor: NotificationExecutor
    ) -> None:
        config = NotificationsConfig(
            types={
                "pr_ready": NotificationTypeDef(
                    payload={"pr_url": OutputField(type="string")},
                )
            }
        )
        env = executor.build_envelope(
            AgentDef(
                name="n",
                type="notification",
                notification="pr_ready",
                payload={"pr_url": "x"},
            ),
            config,
            context={},
            workflow_name="My Feature PR!",
            run_id="r",
            subworkflow_path=[],
            iteration=1,
            correlation={},
            workflow_metadata={},
        )
        assert env["namespace"] == slug_namespace("My Feature PR!")
        assert env["schema_id"].startswith(env["namespace"] + ".pr_ready@")

    def test_wrong_payload_type_raises(
        self, executor: NotificationExecutor, pr_ready_config: NotificationsConfig
    ) -> None:
        bad = AgentDef(
            name="bad",
            type="notification",
            notification="pr_ready",
            payload={"pr_url": "ok", "pr_id": "not-a-number"},
        )
        with pytest.raises(ValidationError, match="pr_id"):
            executor.build_envelope(
                bad,
                pr_ready_config,
                context={},
                workflow_name="w",
                run_id="r",
                subworkflow_path=[],
                iteration=1,
                correlation={},
                workflow_metadata={},
            )

    def test_undeclared_type_raises(
        self, executor: NotificationExecutor, pr_ready_config: NotificationsConfig
    ) -> None:
        bad = AgentDef(
            name="bad",
            type="notification",
            notification="not_a_type",
            payload={},
        )
        with pytest.raises(ValidationError, match="undeclared notification type"):
            executor.build_envelope(
                bad,
                pr_ready_config,
                context={},
                workflow_name="w",
                run_id="r",
                subworkflow_path=[],
                iteration=1,
                correlation={},
                workflow_metadata={},
            )


class TestHelpers:
    def test_slug_namespace(self) -> None:
        assert slug_namespace("feature-pr") == "feature_pr"
        assert slug_namespace("Polyphony.Feature_PR") == "polyphony.feature_pr"
        # leading digit gets prefixed
        assert slug_namespace("2nd").startswith("_")

    def test_emission_id_format(self) -> None:
        assert build_emission_id("r1", "a/b", 3) == "r1:a/b:3"
