"""Tests for ``WorkflowEngine._build_pricing_overrides`` layering."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from conductor.config.schema import (
    AgentDef,
    CostConfig,
    PricingOverride,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.config.user_pricing import USER_PRICING_ENV_VAR
from conductor.engine.workflow import WorkflowEngine


def _make_engine(
    workflow_pricing: dict[str, PricingOverride] | None = None,
) -> WorkflowEngine:
    config = WorkflowConfig(
        workflow=WorkflowDef(
            name="t",
            entry_point="a",
            cost=CostConfig(pricing=workflow_pricing or {}),
        ),
        agents=[AgentDef(name="a", model="gpt-4o", prompt="hi")],
    )
    return WorkflowEngine(config=config, provider=MagicMock())


@pytest.fixture
def user_pricing_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Provide an isolated user-pricing file via CONDUCTOR_PRICING_FILE."""
    target = tmp_path / "user.yaml"
    monkeypatch.setenv(USER_PRICING_ENV_VAR, str(target))
    return target


def test_no_overrides_returns_none(user_pricing_file: Path) -> None:
    engine = _make_engine()
    assert engine._build_pricing_overrides() is None


def test_user_only(user_pricing_file: Path) -> None:
    user_pricing_file.write_text(
        "pricing:\n  custom:\n    input_per_mtok: 1\n    output_per_mtok: 2\n",
        encoding="utf-8",
    )
    overrides = _make_engine()._build_pricing_overrides()
    assert overrides is not None
    assert overrides["custom"].input_per_mtok == 1.0


def test_workflow_only(user_pricing_file: Path) -> None:
    overrides = _make_engine(
        workflow_pricing={"wf": PricingOverride(input_per_mtok=10, output_per_mtok=20)},
    )._build_pricing_overrides()
    assert overrides is not None
    assert overrides["wf"].input_per_mtok == 10.0


def test_workflow_overrides_user_for_same_model(user_pricing_file: Path) -> None:
    user_pricing_file.write_text(
        "pricing:\n  m:\n    input_per_mtok: 1\n    output_per_mtok: 2\n",
        encoding="utf-8",
    )
    overrides = _make_engine(
        workflow_pricing={"m": PricingOverride(input_per_mtok=99, output_per_mtok=99)},
    )._build_pricing_overrides()
    assert overrides is not None
    assert overrides["m"].input_per_mtok == 99.0


def test_distinct_models_merge(user_pricing_file: Path) -> None:
    user_pricing_file.write_text(
        "pricing:\n  user-only:\n    input_per_mtok: 1\n    output_per_mtok: 2\n",
        encoding="utf-8",
    )
    overrides = _make_engine(
        workflow_pricing={
            "wf-only": PricingOverride(input_per_mtok=10, output_per_mtok=20),
        },
    )._build_pricing_overrides()
    assert overrides is not None
    assert set(overrides) == {"user-only", "wf-only"}


def test_malformed_user_file_raises(user_pricing_file: Path) -> None:
    user_pricing_file.write_text("pricing:\n  bad: : :", encoding="utf-8")
    from conductor.exceptions import ConfigurationError

    with pytest.raises(ConfigurationError):
        _make_engine()
