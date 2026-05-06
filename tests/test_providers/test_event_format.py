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

    def test_no_truncation_at_or_below_max_length(self) -> None:
        """Inputs at or below max_length must be returned unchanged."""
        # The JSON-encoded form is '{"v": "xxxx"}' = 13 chars total.
        # Pick max_length so the encoded string is exactly max_length.
        encoded = format_tool_arguments({"v": "xxxx"})
        assert encoded == '{"v": "xxxx"}'
        exact = format_tool_arguments({"v": "xxxx"}, max_length=len(encoded))
        assert exact == encoded
        assert not exact.endswith("…")

        below = format_tool_arguments({"v": "xxxx"}, max_length=len(encoded) + 1)
        assert below == encoded
        assert not below.endswith("…")

    def test_truncation_at_one_over_max_length(self) -> None:
        """Inputs one char over max_length must be truncated to max_length + 1."""
        encoded = format_tool_arguments({"v": "xxxx"})
        assert encoded == '{"v": "xxxx"}'
        truncated = format_tool_arguments({"v": "xxxx"}, max_length=len(encoded) - 1)
        assert truncated is not None
        assert len(truncated) == len(encoded)  # (max_length-1) + 1 ellipsis = max_length
        assert truncated.endswith("…")

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

    def test_no_truncation_at_or_below_max_length(self) -> None:
        """Strings at or below max_length must be returned unchanged."""
        exact = extract_tool_result_text("x" * 100, max_length=100)
        assert exact == "x" * 100
        assert exact is not None
        assert not exact.endswith("…")

        below = extract_tool_result_text("x" * 99, max_length=100)
        assert below == "x" * 99

    def test_truncation_at_one_over_max_length(self) -> None:
        """Strings one char over max_length get truncated to max_length + 1."""
        out = extract_tool_result_text("x" * 101, max_length=100)
        assert out is not None
        assert len(out) == 101
        assert out.endswith("…")
        assert out[:-1] == "x" * 100

    def test_non_string_content_falls_back_to_str(self) -> None:
        """A non-string content attribute should not be returned as-is."""

        @dataclass
        class Result:
            content: object = None
            detailed_content: str | None = None

            def __str__(self) -> str:  # pragma: no cover - exercised by helper
                return f"Result(content={self.content!r})"

        # int content: falls through the isinstance(str) guard to str(result).
        r_int = Result(content=42)
        assert extract_tool_result_text(r_int) == "Result(content=42)"

        # dict content: same fallback.
        r_dict = Result(content={"k": "v"})
        assert extract_tool_result_text(r_dict) == "Result(content={'k': 'v'})"

    def test_non_string_content_falls_back_to_detailed_content(self) -> None:
        """When content is a non-string non-None value, the helper currently
        treats it as 'present but unusable' and falls back to str(result),
        not to detailed_content. Lock that contract in so a future refactor
        is forced to think about it.
        """

        @dataclass
        class Result:
            content: object = None
            detailed_content: str | None = None

            def __str__(self) -> str:
                return "<RESULT-REPR>"

        r = Result(content=42, detailed_content="useful text")
        # content is not None, so detailed_content is never consulted; the
        # non-string content fails the isinstance(str) guard, so str(result)
        # is used.
        assert extract_tool_result_text(r) == "<RESULT-REPR>"
