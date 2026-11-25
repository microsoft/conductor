"""Tests for MCPManager.call_tool_structured.

Covers the structured envelope contract consumed by the ``type: mcp`` workflow
step (executor layer): content blocks as JSON-safe dicts, the strictly
``dict | None`` ``structured`` slot, the ``is_error`` flag, the per-result
text budget with spill-to-file, and the value-free logging contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor.config.schema import ToolOutputConfig


def _make_manager(tool_output: ToolOutputConfig | None = None) -> Any:
    """Build an MCPManager with a single mocked server session."""
    with patch("conductor.mcp.manager.MCP_SDK_AVAILABLE", True):
        from conductor.mcp.manager import MCPManager

        manager = MCPManager(tool_output=tool_output)
    manager.sessions["server"] = AsyncMock()
    return manager


def _make_result(
    content: list[Any],
    structured: Any = None,
    is_error: bool = False,
) -> Any:
    """Build a real mcp CallToolResult (1.x field names)."""
    from mcp.types import CallToolResult

    return CallToolResult(content=content, structuredContent=structured, isError=is_error)


def _text_block(text: str) -> Any:
    """Build a real mcp TextContent block."""
    from mcp.types import TextContent

    return TextContent(type="text", text=text)


@pytest.mark.asyncio
async def test_envelope_shape_happy_path() -> None:
    # Requirement: a successful call returns {"content", "structured", "is_error"}
    # where content blocks are model_dump(mode="json") dicts, structured is the
    # structuredContent payload, and is_error mirrors the result flag.
    manager = _make_manager()
    session = manager.sessions["server"]
    session.call_tool.return_value = _make_result(
        content=[_text_block("hello")],
        structured={"rows": [1, 2]},
        is_error=False,
    )

    envelope = await manager.call_tool_structured("server", "my_tool", {"a": 1})

    session.call_tool.assert_awaited_once_with("my_tool", arguments={"a": 1})
    assert envelope["content"][0]["type"] == "text"
    assert envelope["content"][0]["text"] == "hello"
    assert envelope["structured"] == {"rows": [1, 2]}
    assert envelope["is_error"] is False


@pytest.mark.asyncio
async def test_is_error_true_is_passed_through() -> None:
    # Requirement: the envelope's is_error flag reflects a tool-level error
    # reported by the server (the call itself succeeded).
    manager = _make_manager()
    manager.sessions["server"].call_tool.return_value = _make_result(
        content=[_text_block("bad input")],
        is_error=True,
    )

    envelope = await manager.call_tool_structured("server", "my_tool", {})

    assert envelope["is_error"] is True
    assert envelope["structured"] is None


@pytest.mark.asyncio
async def test_call_error_becomes_chained_runtime_error() -> None:
    # Requirement: errors from session.call_tool surface as a chained
    # RuntimeError("MCP tool call failed: ..."), mirroring call_tool's contract.
    manager = _make_manager()
    session = manager.sessions["server"]
    boom = ConnectionError("transport died")
    session.call_tool.side_effect = boom

    with pytest.raises(RuntimeError, match="MCP tool call failed: my_tool") as exc_info:
        await manager.call_tool_structured("server", "my_tool", {})

    assert exc_info.value.__cause__ is boom


@pytest.mark.asyncio
async def test_unknown_server_raises_value_error() -> None:
    # Requirement: calling a server the manager has no record of is a
    # ValueError (the server name itself is invalid), not a call failure.
    manager = _make_manager()

    with pytest.raises(ValueError, match="Unknown server: nope"):
        await manager.call_tool_structured("nope", "my_tool", {})


@pytest.mark.asyncio
async def test_missing_session_raises_runtime_error() -> None:
    # Requirement: a known server without a live session raises RuntimeError
    # ("No session for server"), matching the call_tool lookup contract.
    manager = _make_manager()
    manager.sessions["server"] = None

    with pytest.raises(RuntimeError, match="No session for server: server"):
        await manager.call_tool_structured("server", "my_tool", {})


@pytest.mark.asyncio
async def test_non_dict_structured_is_malformed_response() -> None:
    # Requirement: the envelope contract for "structured" is strictly dict | None;
    # any other shape is a malformed MCP response and raises RuntimeError
    # instead of returning an envelope with a wrong-typed slot.
    manager = _make_manager()
    mock_result = MagicMock()
    mock_result.content = [_text_block("ok")]
    mock_result.structured_content = ["not", "a", "dict"]
    mock_result.isError = False
    manager.sessions["server"].call_tool.return_value = mock_result

    with pytest.raises(RuntimeError, match="malformed"):
        await manager.call_tool_structured("server", "my_tool", {})


@pytest.mark.asyncio
async def test_block_without_model_dump_uses_fallback_dict() -> None:
    # Requirement: content blocks that are not pydantic models degrade to a
    # {"type", "text": str(block)} dict instead of failing the envelope build.

    class _PlainBlock:
        type = "image"

        def __str__(self) -> str:
            return "<binary payload>"

    manager = _make_manager()
    mock_result = MagicMock()
    mock_result.content = [_PlainBlock()]
    mock_result.structured_content = None
    mock_result.structuredContent = None
    mock_result.isError = False
    mock_result.is_error = False
    manager.sessions["server"].call_tool.return_value = mock_result

    envelope = await manager.call_tool_structured("server", "my_tool", {})

    assert envelope["content"] == [{"type": "image", "text": "<binary payload>"}]


@pytest.mark.asyncio
async def test_text_budget_truncates_in_order_and_spills_full_text(
    tmp_path: Path,
) -> None:
    # Requirement: when the combined text length exceeds max_chars, blocks are
    # walked in order with a shared remaining budget; every truncated block
    # keeps a prefix, gets "truncated": true, and a spill_path whose file holds
    # the block's FULL original text.
    config = ToolOutputConfig(enabled=True, max_chars=1000, spill_to_file=True)
    manager = _make_manager(tool_output=config)
    first, second = "a" * 600, "b" * 600
    manager.sessions["server"].call_tool.return_value = _make_result(
        content=[_text_block(first), _text_block(second)],
        structured={"k": "v"},
    )

    with patch("conductor.mcp.manager.tempfile.gettempdir", return_value=str(tmp_path)):
        envelope = await manager.call_tool_structured("server", "my_tool", {})

    blocks = envelope["content"]
    # First block fits the initial budget and is untouched.
    assert blocks[0]["text"] == first
    assert "truncated" not in blocks[0]
    # Second block is cut to the remaining 400 chars and flagged with a spill.
    assert blocks[1]["text"] == "b" * 400
    assert blocks[1]["truncated"] is True
    spill_file = Path(blocks[1]["spill_path"])
    assert spill_file.read_text() == second
    assert spill_file.name.startswith("mcp-server-my_tool-")


@pytest.mark.asyncio
async def test_text_budget_not_applied_when_disabled() -> None:
    # Requirement: with tool_output disabled, text blocks pass through
    # untruncated regardless of size.
    config = ToolOutputConfig(enabled=False, max_chars=1000, spill_to_file=True)
    manager = _make_manager(tool_output=config)
    full = "x" * 5000
    manager.sessions["server"].call_tool.return_value = _make_result(content=[_text_block(full)])

    envelope = await manager.call_tool_structured("server", "my_tool", {})

    assert envelope["content"][0]["text"] == full
    assert "truncated" not in envelope["content"][0]


@pytest.mark.asyncio
async def test_server_supplied_truncation_fields_are_stripped() -> None:
    # Requirement: ``truncated`` / ``spill_path`` on a content block are
    # Conductor-local metadata — a server returning extension fields of
    # those names must not have them forwarded as trusted markers (a forged
    # ``spill_path`` would leak result values into ``mcp_completed`` events
    # and break the frontend's str type for the field). This holds even with
    # spilling disabled, so the two can never be confused.
    from mcp.types import TextContent

    config = ToolOutputConfig(enabled=False)
    manager = _make_manager(tool_output=config)
    block = TextContent.model_construct(
        type="text",
        text="x",
        truncated=True,
        spill_path={"private_result": "value"},
    )
    manager.sessions["server"].call_tool.return_value = _make_result(content=[block])

    envelope = await manager.call_tool_structured("server", "my_tool", {})

    [dumped] = envelope["content"]
    assert dumped["type"] == "text"
    assert dumped["text"] == "x"
    assert "truncated" not in dumped
    assert "spill_path" not in dumped


@pytest.mark.asyncio
async def test_structured_is_never_truncated(tmp_path: Path) -> None:
    # Requirement: the per-result text budget applies to text blocks only;
    # the structured payload passes through untouched regardless of its size.
    config = ToolOutputConfig(enabled=True, max_chars=1000, spill_to_file=False)
    manager = _make_manager(tool_output=config)
    big_structured = {"data": "y" * 5000}
    manager.sessions["server"].call_tool.return_value = _make_result(
        content=[_text_block("x" * 2500)],
        structured=big_structured,
    )

    envelope = await manager.call_tool_structured("server", "my_tool", {})

    assert envelope["content"][0]["truncated"] is True
    assert len(envelope["content"][0]["text"]) == 1000
    assert envelope["structured"] == big_structured


@pytest.mark.asyncio
async def test_no_log_record_contains_argument_or_exception_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Requirement (logging contract): the new method must not emit any log
    # record containing argument values, result values, or exception text, at
    # any level — neither on the success path nor on the failure path.
    manager = _make_manager()
    session = manager.sessions["server"]
    session.call_tool.side_effect = ValueError("very specific leak text")

    with (
        caplog.at_level("DEBUG"),
        pytest.raises(RuntimeError, match="MCP tool call failed"),
    ):
        await manager.call_tool_structured("server", "my_tool", {"secret_arg": "shh"})

    assert "shh" not in caplog.text
    assert "very specific leak text" not in caplog.text
    assert "secret_arg" not in caplog.text


@pytest.mark.parametrize("kind", ["end", "broken", "closed", "connection", "rpc_closed"])
async def test_transport_signal_is_classified_only_at_tool_await(kind) -> None:
    # Requirement: declared stream failures provide a typed, value-free invalidation signal.
    import anyio
    from mcp import McpError
    from mcp.types import CONNECTION_CLOSED, ErrorData

    errors = {
        "end": anyio.EndOfStream(),
        "broken": anyio.BrokenResourceError(),
        "closed": anyio.ClosedResourceError(),
        "connection": ConnectionError("private transport value"),
        "rpc_closed": McpError(ErrorData(code=CONNECTION_CLOSED, message="private RPC value")),
    }
    manager = _make_manager()
    manager._safe_transport_diagnostics["server"] = True
    manager.sessions["server"].call_tool.side_effect = errors[kind]
    with pytest.raises(RuntimeError) as caught:
        await manager.call_tool_structured("server", "my_tool", {})
    assert type(caught.value).__name__ == "McpTransportError"
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("kind", ["protocol", "timeout", "cancel"])
async def test_protocol_and_control_errors_preserve_type_and_session(kind) -> None:
    # Requirement: ordinary protocol failures and control flow never invalidate a healthy session.
    import asyncio

    from mcp import McpError
    from mcp.types import ErrorData

    errors = {
        "protocol": McpError(ErrorData(code=-32602, message="invalid parameters")),
        "timeout": TimeoutError(),
        "cancel": asyncio.CancelledError(),
    }
    manager = _make_manager()
    session = manager.sessions["server"]
    failure = errors[kind]
    session.call_tool.side_effect = failure
    with pytest.raises(type(failure)) as caught:
        await manager.call_tool_structured("server", "my_tool", {})
    assert caught.value is failure
    session.call_tool.side_effect = None
    session.call_tool.return_value = _make_result(content=[])
    assert (await manager.call_tool_structured("server", "my_tool", {}))["is_error"] is False
    assert manager.sessions["server"] is session


@pytest.mark.parametrize("stage", ["normalization", "spill"])
async def test_transport_signal_excludes_normalization_and_spilling(stage, monkeypatch) -> None:
    # Requirement: a connection-shaped exception outside the tool await keeps its original type.
    failure = ConnectionError("post-exchange failure")
    manager = _make_manager(ToolOutputConfig(enabled=True, max_chars=1000, spill_to_file=True))
    session = manager.sessions["server"]

    class FailedContent:
        @property
        def content(self):
            raise failure

    def failed_spill(*args, **kwargs):
        raise failure

    with monkeypatch.context() as patcher:
        if stage == "normalization":
            session.call_tool.return_value = FailedContent()
        else:
            session.call_tool.return_value = _make_result([_text_block("x" * 2000)])
            patcher.setattr(manager, "_spill_full_output", failed_spill)
        with pytest.raises(ConnectionError) as caught:
            await manager.call_tool_structured("server", "my_tool", {})
        assert caught.value is failure
    session.call_tool.return_value = _make_result([])
    assert (await manager.call_tool_structured("server", "my_tool", {}))["content"] == []
