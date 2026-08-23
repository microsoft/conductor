"""Tests for the ``workflow.mcp:`` block (E6, DD4, FR11).

Covers:
- ``McpConfig`` defaults.
- ``extra="forbid"`` rejects a misspelled key.
- ``mode`` rejects an unknown value.
- ``estimated_minutes`` rejects zero and negative values.
- An absent ``mcp:`` block is identical to an explicit default block.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conductor.config.schema import McpConfig, WorkflowDef


class TestMcpConfigDefaults:
    """Defaults match the E6-T1 field set."""

    def test_defaults(self) -> None:
        config = McpConfig()
        assert config.expose is True
        assert config.mode == "async"
        assert config.read_only is False
        assert config.destructive is False
        assert config.estimated_minutes is None

    def test_explicit_values_round_trip(self) -> None:
        config = McpConfig(
            expose=False,
            mode="sync",
            read_only=True,
            destructive=True,
            estimated_minutes=8,
        )
        assert config.expose is False
        assert config.mode == "sync"
        assert config.read_only is True
        assert config.destructive is True
        assert config.estimated_minutes == 8

    @pytest.mark.parametrize("mode", ["async", "sync", "auto"])
    def test_all_valid_modes(self, mode: str) -> None:
        config = McpConfig(mode=mode)  # type: ignore[arg-type]
        assert config.mode == mode


class TestMcpConfigRejectsBadInput:
    """``extra="forbid"``, unknown ``mode``, and ``estimated_minutes`` bounds."""

    def test_extra_forbid_rejects_a_typo(self) -> None:
        with pytest.raises(ValidationError):
            McpConfig(expse=False)  # type: ignore[call-arg]

    def test_unknown_mode_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            McpConfig(mode="eventually")  # type: ignore[arg-type]

    def test_estimated_minutes_zero_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            McpConfig(estimated_minutes=0)

    def test_estimated_minutes_negative_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            McpConfig(estimated_minutes=-5)

    def test_estimated_minutes_positive_is_accepted(self) -> None:
        config = McpConfig(estimated_minutes=1)
        assert config.estimated_minutes == 1


class TestWorkflowDefMcpField:
    """An absent ``mcp:`` block behaves identically to a default one."""

    def test_absent_block_equals_default_block(self) -> None:
        without = WorkflowDef(name="wf", entry_point="a")
        with_default = WorkflowDef(name="wf", entry_point="a", mcp=McpConfig())
        assert without.mcp == with_default.mcp

    def test_absent_block_has_default_field_values(self) -> None:
        wf = WorkflowDef(name="wf", entry_point="a")
        assert wf.mcp.expose is True
        assert wf.mcp.mode == "async"
        assert wf.mcp.read_only is False
        assert wf.mcp.destructive is False
        assert wf.mcp.estimated_minutes is None

    def test_explicit_block_is_wired_through(self) -> None:
        wf = WorkflowDef(
            name="wf",
            entry_point="a",
            mcp=McpConfig(expose=False, mode="sync", destructive=True, estimated_minutes=3),
        )
        assert wf.mcp.expose is False
        assert wf.mcp.mode == "sync"
        assert wf.mcp.destructive is True
        assert wf.mcp.estimated_minutes == 3

    def test_workflow_extra_forbid_still_applies_to_mcp_typo(self) -> None:
        # A typo inside the nested `mcp:` block must be a schema error
        # (raised by McpConfig's own extra="forbid"), not silently ignored.
        with pytest.raises(ValidationError):
            WorkflowDef(name="wf", entry_point="a", mcp={"expse": False})  # type: ignore[arg-type]
