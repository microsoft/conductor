"""Tests for event_callback wiring in the Copilot provider.

Mirrors ``test_claude_event_callback.py`` so both providers verify that
``agent_tool_start`` / ``agent_tool_complete`` payloads are formatted as
human-readable strings (JSON for arguments, unwrapped text for results)
rather than Python ``repr()`` of structured SDK objects.

Related issues:
- https://github.com/microsoft/conductor/issues/93 (formatting)
- https://github.com/microsoft/conductor/issues/39 (provider parity)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

from conductor.providers.copilot import CopilotProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(event_type: str, **data_attrs: Any) -> MagicMock:
    """Create a mock SDK event with the given event_type and data attributes."""
    event = MagicMock()
    event.event_type = event_type
    event.data = MagicMock(spec_set=list(data_attrs.keys()))
    for key, value in data_attrs.items():
        setattr(event.data, key, value)
    return event


@dataclass
class _FakeResult:
    """Stand-in for the Copilot SDK's structured Result object."""

    content: str | None = None
    contents: object | None = None
    detailed_content: str | None = None
    kind: object | None = None


# ---------------------------------------------------------------------------
# tool.execution_start  →  agent_tool_start
# ---------------------------------------------------------------------------


class TestForwardToolStart:
    """Verify _forward_event maps tool.execution_start correctly."""

    def test_dict_arguments_render_as_json(self) -> None:
        """Dict args must serialize as JSON, not Python repr (issue #93)."""
        events: list[tuple[str, dict[str, Any]]] = []

        event = _make_event(
            "tool.execution_start",
            tool_name="my_tool",
            arguments={"path": r"C:\Users\dev", "limit": 10},
        )
        CopilotProvider._forward_event(
            "tool.execution_start", event, lambda t, d: events.append((t, d))
        )

        assert len(events) == 1
        evt_type, evt_data = events[0]
        assert evt_type == "agent_tool_start"
        assert evt_data["tool_name"] == "my_tool"
        # JSON quoting (double quotes, not single quotes from Python repr).
        assert evt_data["arguments"] is not None
        assert '"path"' in evt_data["arguments"]
        assert "'path'" not in evt_data["arguments"]
        # Windows path single-escaped (JSON escapes once: \\ source = \ display).
        assert r'"C:\\Users\\dev"' in evt_data["arguments"]

    def test_arguments_truncated_with_ellipsis(self) -> None:
        """Long arguments must be truncated and end with the … sentinel."""
        events: list[tuple[str, dict[str, Any]]] = []

        event = _make_event(
            "tool.execution_start",
            tool_name="my_tool",
            arguments={"data": "x" * 600},
        )
        CopilotProvider._forward_event(
            "tool.execution_start", event, lambda t, d: events.append((t, d))
        )

        assert len(events) == 1
        arguments = events[0][1]["arguments"]
        assert arguments is not None
        # Truncated to 500 chars + a single-char "…" ellipsis when long.
        assert len(arguments) <= 501
        assert arguments.endswith("…")

    def test_args_attribute_alias_supported(self) -> None:
        """The SDK may emit either .arguments or .args; both must work."""
        events: list[tuple[str, dict[str, Any]]] = []

        event = _make_event(
            "tool.execution_start",
            tool_name="my_tool",
            args={"alias": True},
        )
        CopilotProvider._forward_event(
            "tool.execution_start", event, lambda t, d: events.append((t, d))
        )

        assert events[0][1]["arguments"] is not None
        assert '"alias"' in events[0][1]["arguments"]

    def test_missing_arguments_yields_none(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []

        event = _make_event("tool.execution_start", tool_name="my_tool", arguments=None)
        CopilotProvider._forward_event(
            "tool.execution_start", event, lambda t, d: events.append((t, d))
        )

        assert events[0][1]["arguments"] is None


# ---------------------------------------------------------------------------
# tool.execution_complete  →  agent_tool_complete
# ---------------------------------------------------------------------------


class TestForwardToolComplete:
    """Verify _forward_event maps tool.execution_complete correctly."""

    def test_structured_result_unwraps_content(self) -> None:
        """SDK Result objects must be unwrapped to plain text (issue #93)."""
        events: list[tuple[str, dict[str, Any]]] = []

        result = _FakeResult(
            content="line one\nline two",
            detailed_content="line one\nline two (full)",
        )
        event = _make_event(
            "tool.execution_complete",
            tool_name="my_tool",
            result=result,
        )
        CopilotProvider._forward_event(
            "tool.execution_complete", event, lambda t, d: events.append((t, d))
        )

        assert len(events) == 1
        evt_type, evt_data = events[0]
        assert evt_type == "agent_tool_complete"
        assert evt_data["tool_name"] == "my_tool"
        # Real newline preserved, not literal backslash-n.
        assert evt_data["result"] == "line one\nline two"
        # Crucially NOT the Python repr of the Result wrapper.
        assert "_FakeResult(" not in evt_data["result"]
        assert "\\n" not in evt_data["result"]

    def test_falls_back_to_detailed_content(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []

        result = _FakeResult(content=None, detailed_content="full payload")
        event = _make_event(
            "tool.execution_complete",
            tool_name="my_tool",
            result=result,
        )
        CopilotProvider._forward_event(
            "tool.execution_complete", event, lambda t, d: events.append((t, d))
        )

        assert events[0][1]["result"] == "full payload"

    def test_result_truncated_with_ellipsis(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []

        result = _FakeResult(content="y" * 600)
        event = _make_event(
            "tool.execution_complete",
            tool_name="my_tool",
            result=result,
        )
        CopilotProvider._forward_event(
            "tool.execution_complete", event, lambda t, d: events.append((t, d))
        )

        result_text = events[0][1]["result"]
        assert result_text is not None
        # Truncated to 500 chars + a single-char "…" ellipsis when long.
        assert len(result_text) <= 501
        assert result_text.endswith("…")

    def test_output_attribute_alias_supported(self) -> None:
        """The SDK may emit either .result or .output; both must work."""
        events: list[tuple[str, dict[str, Any]]] = []

        event = _make_event(
            "tool.execution_complete",
            tool_name="my_tool",
            output="aliased text",
        )
        CopilotProvider._forward_event(
            "tool.execution_complete", event, lambda t, d: events.append((t, d))
        )

        assert events[0][1]["result"] == "aliased text"

    def test_missing_result_yields_none(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []

        event = _make_event(
            "tool.execution_complete",
            tool_name="my_tool",
            result=None,
        )
        CopilotProvider._forward_event(
            "tool.execution_complete", event, lambda t, d: events.append((t, d))
        )

        assert events[0][1]["result"] is None
