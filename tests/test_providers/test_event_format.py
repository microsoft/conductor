"""Tests for tool-event payload formatting helpers (issue #93)."""

from __future__ import annotations

from dataclasses import dataclass

from conductor.providers._event_format import (
    extract_tool_result_text,
    format_tool_arguments,
)


class TestFormatToolArguments:
    """Tests for format_tool_arguments."""

    def test_returns_none_for_empty(self) -> None:
        assert format_tool_arguments(None) is None
        assert format_tool_arguments({}) is None
        assert format_tool_arguments("") is None

    def test_dict_renders_as_json(self) -> None:
        """Dict args render as JSON, not Python repr (issue #93)."""
        result = format_tool_arguments({"path": "/tmp/file", "limit": 10})
        assert result is not None
        # JSON uses double quotes; Python repr uses single quotes.
        assert '"path"' in result
        assert "'path'" not in result

    def test_windows_path_not_double_escaped(self) -> None:
        """Windows paths shouldn't show doubled backslashes."""
        result = format_tool_arguments({"path": r"C:\Users\dev"})
        # JSON escapes backslashes, but only once: \\ in source = \ on display
        assert result is not None
        assert r'"C:\\Users\\dev"' in result

    def test_truncation_appends_ellipsis(self) -> None:
        long_value = "x" * 600
        result = format_tool_arguments({"v": long_value}, max_length=100)
        assert result is not None
        assert len(result) == 101  # 100 chars + 1-char ellipsis "…"
        assert result.endswith("…")

    def test_non_serializable_falls_back_to_str(self) -> None:
        class Custom:
            def __str__(self) -> str:
                return "custom-repr"

        # Custom objects in the dict are stringified by default=str.
        result = format_tool_arguments({"obj": Custom()})
        assert result is not None
        assert "custom-repr" in result

    def test_string_argument_passes_through(self) -> None:
        # Some SDKs may pass string args; JSON-encode preserves them.
        result = format_tool_arguments("hello")
        assert result == '"hello"'


class TestExtractToolResultText:
    """Tests for extract_tool_result_text."""

    def test_returns_none_for_empty(self) -> None:
        assert extract_tool_result_text(None) is None
        assert extract_tool_result_text("") is None

    def test_plain_string_passes_through(self) -> None:
        """Claude's MCPManager already returns strings."""
        assert extract_tool_result_text("file contents") == "file contents"

    def test_extracts_content_from_structured_result(self) -> None:
        """The Copilot SDK's Result object exposes a .content field."""

        @dataclass
        class Result:
            content: str | None = None
            contents: object | None = None
            detailed_content: str | None = None
            kind: object | None = None

        r = Result(content="hello world", detailed_content="hello world (full)")
        out = extract_tool_result_text(r)
        # Prefers content over detailed_content
        assert out == "hello world"

    def test_falls_back_to_detailed_content(self) -> None:
        @dataclass
        class Result:
            content: str | None = None
            detailed_content: str | None = None

        r = Result(content=None, detailed_content="full payload")
        assert extract_tool_result_text(r) == "full payload"

    def test_newlines_preserved_not_escaped(self) -> None:
        """Real \\n stays as a newline, not a literal backslash-n (issue #93)."""

        @dataclass
        class Result:
            content: str | None = None

        r = Result(content="line one\nline two")
        out = extract_tool_result_text(r)
        assert out == "line one\nline two"
        # Crucially NOT the Python repr "Result(content='line one\\nline two')"
        assert "Result(" not in (out or "")
        assert "\\n" not in (out or "")

    def test_unknown_object_falls_back_to_str(self) -> None:
        class Mystery:
            def __str__(self) -> str:
                return "mystery-output"

        assert extract_tool_result_text(Mystery()) == "mystery-output"

    def test_truncation_appends_ellipsis(self) -> None:
        out = extract_tool_result_text("x" * 600, max_length=100)
        assert out is not None
        assert len(out) == 101
        assert out.endswith("…")
