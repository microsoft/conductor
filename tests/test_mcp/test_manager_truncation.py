"""Tests for MCPManager tool output truncation and spill-to-file behavior."""

from __future__ import annotations

import os
import stat
from contextlib import suppress
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor.config.schema import ToolOutputConfig


class _TruncationManagerFixture:
    """Helper that builds a patched MCPManager with a mocked MCP session."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.session = AsyncMock()
        self.text_content = MagicMock()
        self.text_content.text = ""

    def make_manager(self, tool_output: ToolOutputConfig | None = None) -> Any:
        """Return an MCPManager with a single mocked server/session."""
        with patch("conductor.mcp.manager.MCP_SDK_AVAILABLE", True):
            from conductor.mcp.manager import MCPManager

            manager = MCPManager(tool_output=tool_output)
            manager.tool_to_server["server__tool"] = "server"
            manager.sessions["server"] = self.session
        return manager

    def set_result(self, text: str) -> None:
        """Configure the mocked session to return a text-only result."""
        mock_result = MagicMock()
        mock_result.content = [self.text_content]
        self.text_content.text = text
        mock_result.structuredContent = None
        self.session.call_tool.return_value = mock_result


@pytest.fixture
def fixture(tmp_path: Path) -> _TruncationManagerFixture:
    """Provide a reusable truncation test fixture."""
    return _TruncationManagerFixture(tmp_path)


@pytest.mark.asyncio
async def test_truncation_spills_to_file_with_marker(fixture: _TruncationManagerFixture) -> None:
    """Result above max_chars is truncated and the full text is spilled to disk."""
    full_text = "x" * 1200
    config = ToolOutputConfig(enabled=True, max_chars=1000, spill_to_file=True)
    manager = fixture.make_manager(config)
    fixture.set_result(full_text)

    with patch("conductor.mcp.manager.tempfile.gettempdir", return_value=str(fixture.tmp_path)):
        result = await manager.call_tool("server__tool", {})

    assert result.startswith("x" * 1000)
    assert "[output truncated: 1200 chars -> 1000 kept" in result
    assert "full output saved to:" in result
    assert "The full output was truncated; refine the tool arguments to return less data." in result

    # Extract spill path from marker and verify file contents and mode.
    marker_start = result.index("full output saved to: ") + len("full output saved to: ")
    marker_end = result.index(". The full output was truncated")
    spill_path = result[marker_start:marker_end]
    spill_file = Path(spill_path)
    assert spill_file.exists()
    assert spill_file.read_text() == full_text
    assert stat.S_IMODE(spill_file.stat().st_mode) == 0o600
    assert spill_file.name.startswith("mcp-server-tool-")
    assert spill_file.name.endswith(".txt")


@pytest.mark.asyncio
async def test_truncation_without_spill_has_no_path_marker(
    fixture: _TruncationManagerFixture,
) -> None:
    """When spill_to_file is False, the marker omits the saved path."""
    full_text = "x" * 1200
    config = ToolOutputConfig(enabled=True, max_chars=1000, spill_to_file=False)
    manager = fixture.make_manager(config)
    fixture.set_result(full_text)

    result = await manager.call_tool("server__tool", {})

    assert result.startswith("x" * 1000)
    assert "[output truncated: 1200 chars -> 1000 kept." in result
    assert "full output saved to:" not in result


@pytest.mark.asyncio
async def test_disabled_config_does_not_truncate(fixture: _TruncationManagerFixture) -> None:
    """When enabled is False, the full result is returned unchanged."""
    full_text = "x" * 1200
    config = ToolOutputConfig(enabled=False, max_chars=1000, spill_to_file=True)
    manager = fixture.make_manager(config)
    fixture.set_result(full_text)

    result = await manager.call_tool("server__tool", {})

    assert result == full_text
    assert "[output truncated:" not in result


@pytest.mark.asyncio
async def test_no_truncation_when_result_fits(fixture: _TruncationManagerFixture) -> None:
    """Results within the limit are returned without any marker."""
    full_text = "x" * 100
    config = ToolOutputConfig(enabled=True, max_chars=1000, spill_to_file=True)
    manager = fixture.make_manager(config)
    fixture.set_result(full_text)

    result = await manager.call_tool("server__tool", {})

    assert result == full_text
    assert "[output truncated:" not in result


@pytest.mark.asyncio
async def test_spill_os_error_falls_back_to_marker_without_path(
    fixture: _TruncationManagerFixture, tmp_path: Path
) -> None:
    """If the spill directory cannot be written, the result is truncated without a path."""
    full_text = "x" * 1200
    config = ToolOutputConfig(enabled=True, max_chars=1000, spill_to_file=True)
    manager = fixture.make_manager(config)
    fixture.set_result(full_text)

    readonly_dir = tmp_path / "readonly"
    readonly_dir.mkdir(mode=0o500)
    config.spill_dir = str(readonly_dir)

    result = await manager.call_tool("server__tool", {})

    assert result.startswith("x" * 1000)
    assert "[output truncated: 1200 chars -> 1000 kept." in result
    assert "full output saved to:" not in result

    # Cleanup: restore write permission so tmp_path can be removed.
    readonly_dir.chmod(0o700)


@pytest.mark.asyncio
async def test_spill_file_name_sanitization(fixture: _TruncationManagerFixture) -> None:
    """Unsafe server and tool name characters are replaced in the spill filename."""
    full_text = "x" * 1200
    config = ToolOutputConfig(enabled=True, max_chars=1000, spill_to_file=True)
    manager = fixture.make_manager(config)
    manager.tool_to_server["server/with spaces__tool:name!"] = "server/with spaces"
    manager.sessions["server/with spaces"] = fixture.session
    fixture.set_result(full_text)

    with patch("conductor.mcp.manager.tempfile.gettempdir", return_value=str(fixture.tmp_path)):
        result = await manager.call_tool("server/with spaces__tool:name!", {})

    assert "full output saved to:" in result
    marker_start = result.index("full output saved to: ") + len("full output saved to: ")
    marker_end = result.index(". The full output was truncated")
    spill_path = result[marker_start:marker_end]
    assert Path(spill_path).name.startswith("mcp-server_with_spaces-tool_name_-")


@pytest.mark.asyncio
async def test_marker_format_exactly_matches_specification(
    fixture: _TruncationManagerFixture,
) -> None:
    """The marker text must follow the exact single-line format with full output path."""
    full_text = "a" * 1500
    config = ToolOutputConfig(enabled=True, max_chars=1000, spill_to_file=True)
    manager = fixture.make_manager(config)
    fixture.set_result(full_text)

    with patch("conductor.mcp.manager.tempfile.gettempdir", return_value=str(fixture.tmp_path)):
        result = await manager.call_tool("server__tool", {})

    expected_marker = (
        "\n\n[output truncated: 1500 chars -> 1000 kept; full output saved to: "
        f"{fixture.tmp_path}/conductor/tool-output/mcp-server-tool-"
    )
    assert expected_marker in result
    assert "The full output was truncated; refine the tool arguments to return less data." in result


@pytest.mark.asyncio
async def test_marker_format_without_spill_exactly_matches_specification(
    fixture: _TruncationManagerFixture,
) -> None:
    """The marker text must follow the exact single-line format when spill is disabled."""
    full_text = "b" * 1500
    config = ToolOutputConfig(enabled=True, max_chars=1000, spill_to_file=False)
    manager = fixture.make_manager(config)
    fixture.set_result(full_text)

    result = await manager.call_tool("server__tool", {})

    expected_marker = (
        "\n\n[output truncated: 1500 chars -> 1000 kept. The full output was truncated"
    )
    assert expected_marker in result
    assert result.endswith("refine the tool arguments to return less data.]")


@pytest.mark.asyncio
async def test_spill_directory_mode_and_contents(fixture: _TruncationManagerFixture) -> None:
    """The spill directory is created with 0o700 and contains the full output."""
    full_text = "secret" * 200
    config = ToolOutputConfig(enabled=True, max_chars=1000, spill_to_file=True)
    manager = fixture.make_manager(config)
    fixture.set_result(full_text)

    with patch("conductor.mcp.manager.tempfile.gettempdir", return_value=str(fixture.tmp_path)):
        await manager.call_tool("server__tool", {})

    spill_dir = fixture.tmp_path / "conductor" / "tool-output"
    assert spill_dir.exists()
    assert stat.S_IMODE(spill_dir.stat().st_mode) == 0o700
    spill_files = list(spill_dir.glob("*.txt"))
    assert len(spill_files) == 1
    assert spill_files[0].read_text() == full_text


@pytest.mark.asyncio
async def test_spill_directory_is_created_when_spill_dir_is_relative(
    fixture: _TruncationManagerFixture, tmp_path: Path
) -> None:
    """A relative spill_dir is resolved against the process cwd and created."""
    full_text = "x" * 1500
    relative_dir = tmp_path / "relative-spill"
    config = ToolOutputConfig(
        enabled=True, max_chars=1000, spill_to_file=True, spill_dir=str(relative_dir)
    )
    manager = fixture.make_manager(config)
    fixture.set_result(full_text)

    result = await manager.call_tool("server__tool", {})

    assert "full output saved to:" in result
    assert relative_dir.exists()
    assert any(relative_dir.glob("*.txt"))

    # Marker path must be absolute so agents with a different cwd can find it.
    marker_start = result.index("full output saved to: ") + len("full output saved to: ")
    marker_end = result.index(". The full output was truncated")
    spill_path = result[marker_start:marker_end]
    assert Path(spill_path).is_absolute()


@pytest.mark.asyncio
async def test_relative_spill_dir_resolves_against_cwd(
    fixture: _TruncationManagerFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative spill_dir string is resolved to an absolute path via cwd."""
    full_text = "x" * 1500
    monkeypatch.chdir(tmp_path)
    config = ToolOutputConfig(
        enabled=True, max_chars=1000, spill_to_file=True, spill_dir="tool-output"
    )
    manager = fixture.make_manager(config)
    fixture.set_result(full_text)

    result = await manager.call_tool("server__tool", {})

    assert "full output saved to:" in result
    resolved_dir = tmp_path / "tool-output"
    assert resolved_dir.exists()
    assert any(resolved_dir.glob("*.txt"))

    marker_start = result.index("full output saved to: ") + len("full output saved to: ")
    marker_end = result.index(". The full output was truncated")
    spill_path = result[marker_start:marker_end]
    assert Path(spill_path).is_absolute()
    assert Path(spill_path).parent == resolved_dir


@pytest.mark.asyncio
async def test_spill_dir_symlink_is_rejected(
    fixture: _TruncationManagerFixture, tmp_path: Path
) -> None:
    """A pre-existing symlink as spill_dir is rejected; no file is written."""
    full_text = "x" * 1500
    real_dir = tmp_path / "real"
    symlink_dir = tmp_path / "link"
    real_dir.mkdir()
    symlink_dir.symlink_to(real_dir)
    config = ToolOutputConfig(
        enabled=True, max_chars=1000, spill_to_file=True, spill_dir=str(symlink_dir)
    )
    manager = fixture.make_manager(config)
    fixture.set_result(full_text)

    result = await manager.call_tool("server__tool", {})

    assert "full output saved to:" not in result
    assert "[output truncated: 1500 chars -> 1000 kept." in result
    assert not any(real_dir.glob("*.txt"))


@pytest.mark.asyncio
async def test_spill_dir_loose_permissions_are_tightened(
    fixture: _TruncationManagerFixture, tmp_path: Path
) -> None:
    """A pre-existing loose-permission spill_dir is chmod to 0o700 and used."""
    full_text = "x" * 1500
    loose_dir = tmp_path / "loose"
    loose_dir.mkdir(mode=0o777)
    config = ToolOutputConfig(
        enabled=True, max_chars=1000, spill_to_file=True, spill_dir=str(loose_dir)
    )
    manager = fixture.make_manager(config)
    fixture.set_result(full_text)

    result = await manager.call_tool("server__tool", {})

    assert "full output saved to:" in result
    assert stat.S_IMODE(loose_dir.stat().st_mode) == 0o700
    assert any(loose_dir.glob("*.txt"))


@pytest.mark.asyncio
async def test_spill_dir_chmod_failure_is_rejected(
    fixture: _TruncationManagerFixture, tmp_path: Path
) -> None:
    """If chmod on a loose spill_dir fails, no file is written."""
    full_text = "x" * 1500
    loose_dir = tmp_path / "loose"
    loose_dir.mkdir(mode=0o777)
    config = ToolOutputConfig(
        enabled=True, max_chars=1000, spill_to_file=True, spill_dir=str(loose_dir)
    )
    manager = fixture.make_manager(config)
    fixture.set_result(full_text)

    with patch("conductor.mcp.manager.os.chmod", side_effect=PermissionError("no")):
        result = await manager.call_tool("server__tool", {})

    assert "full output saved to:" not in result
    assert "[output truncated: 1500 chars -> 1000 kept." in result
    assert not any(loose_dir.glob("*.txt"))


@pytest.mark.asyncio
async def test_fdopen_failure_does_not_double_close(
    fixture: _TruncationManagerFixture, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """If os.fdopen fails after os.open, the error is logged once and no double-close occurs."""
    full_text = "x" * 1500
    config = ToolOutputConfig(
        enabled=True, max_chars=1000, spill_to_file=True, spill_dir=str(tmp_path)
    )
    manager = fixture.make_manager(config)
    fixture.set_result(full_text)

    with patch("conductor.mcp.manager.os.fdopen", side_effect=OSError("bad fd")):
        result = await manager.call_tool("server__tool", {})

    assert "full output saved to:" not in result
    assert "[output truncated: 1500 chars -> 1000 kept." in result
    assert "Failed to spill full MCP tool output" in caplog.text
    assert not list(tmp_path.glob("*.txt"))


@pytest.mark.asyncio
async def test_fdopen_failure_closes_raw_fd(
    fixture: _TruncationManagerFixture, tmp_path: Path
) -> None:
    """When os.fdopen raises, the raw fd from os.open is closed exactly once."""
    full_text = "x" * 1500
    config = ToolOutputConfig(
        enabled=True, max_chars=1000, spill_to_file=True, spill_dir=str(tmp_path)
    )
    manager = fixture.make_manager(config)
    fixture.set_result(full_text)

    captured_fd: int | None = None
    original_open = os.open

    def _capture_open(path, flags, mode):
        nonlocal captured_fd
        captured_fd = original_open(path, flags, mode)
        return captured_fd

    with (
        patch("conductor.mcp.manager.os.fdopen", side_effect=OSError("bad fd")),
        patch("conductor.mcp.manager.os.open", side_effect=_capture_open),
    ):
        await manager.call_tool("server__tool", {})

    assert captured_fd is not None
    with pytest.raises(OSError):
        os.fstat(captured_fd)
    assert not list(tmp_path.glob("*.txt"))


@pytest.mark.asyncio
async def test_unicode_payload_spills_with_utf8(fixture: _TruncationManagerFixture) -> None:
    """UTF-8 payloads (including emoji) round-trip through the spill file."""
    full_text = "🎉 hello émojis 中文 🔧" + "x" * 1500
    config = ToolOutputConfig(enabled=True, max_chars=1000, spill_to_file=True)
    manager = fixture.make_manager(config)
    fixture.set_result(full_text)

    with patch("conductor.mcp.manager.tempfile.gettempdir", return_value=str(fixture.tmp_path)):
        result = await manager.call_tool("server__tool", {})

    assert "full output saved to:" in result
    marker_start = result.index("full output saved to: ") + len("full output saved to: ")
    marker_end = result.index(". The full output was truncated")
    spill_path = result[marker_start:marker_end]
    assert Path(spill_path).read_text(encoding="utf-8") == full_text


@pytest.mark.asyncio
async def test_write_unicode_encode_error_cleans_up_and_degrades(
    fixture: _TruncationManagerFixture, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A UnicodeEncodeError during write removes the partial file and degrades gracefully."""
    full_text = "x" * 1500
    config = ToolOutputConfig(
        enabled=True, max_chars=1000, spill_to_file=True, spill_dir=str(tmp_path)
    )
    manager = fixture.make_manager(config)
    fixture.set_result(full_text)

    class _BadWriter:
        def __init__(self, fd: int) -> None:
            self._fd = fd

        def write(self, text: str) -> None:
            raise UnicodeEncodeError("utf-8", text, 0, 1, "can't encode")

        def __enter__(self) -> _BadWriter:
            return self

        def __exit__(self, exc_type, exc_val, exc_tb) -> None:
            with suppress(OSError):
                os.close(self._fd)

    def _bad_fdopen(fd, mode, encoding=None):
        return _BadWriter(fd)

    with patch("conductor.mcp.manager.os.fdopen", side_effect=_bad_fdopen):
        result = await manager.call_tool("server__tool", {})

    assert "full output saved to:" not in result
    assert "[output truncated: 1500 chars -> 1000 kept." in result
    assert "Failed to write full MCP tool output spill" in caplog.text
    assert not list(tmp_path.glob("*.txt"))


@pytest.mark.asyncio
async def test_default_spill_dir_symlink_is_rejected(
    fixture: _TruncationManagerFixture, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A pre-existing symlink at the default <tmp>/conductor/tool-output leaf is rejected."""
    full_text = "x" * 1500
    config = ToolOutputConfig(enabled=True, max_chars=1000, spill_to_file=True)
    manager = fixture.make_manager(config)
    fixture.set_result(full_text)

    target_dir = tmp_path / "target"
    target_dir.mkdir()

    def gettempdir_patched() -> str:
        return str(tmp_path)

    symlink_leaf = tmp_path / "conductor" / "tool-output"
    symlink_leaf.parent.mkdir(parents=True)
    symlink_leaf.symlink_to(target_dir)

    with patch("conductor.mcp.manager.tempfile.gettempdir", gettempdir_patched):
        result = await manager.call_tool("server__tool", {})

    assert "full output saved to:" not in result
    assert "[output truncated: 1500 chars -> 1000 kept." in result
    assert "contains a symlink" in caplog.text
    assert not list(target_dir.glob("*.txt"))


@pytest.mark.asyncio
async def test_default_spill_dir_ancestor_symlink_is_rejected(
    fixture: _TruncationManagerFixture, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A symlink at <tmp>/conductor (ancestor of the default leaf) is rejected."""
    full_text = "x" * 1500
    config = ToolOutputConfig(enabled=True, max_chars=1000, spill_to_file=True)
    manager = fixture.make_manager(config)
    fixture.set_result(full_text)

    target_dir = tmp_path / "target"
    target_dir.mkdir()
    symlink_ancestor = tmp_path / "conductor"
    symlink_ancestor.symlink_to(target_dir)

    def gettempdir_patched() -> str:
        return str(tmp_path)

    with patch("conductor.mcp.manager.tempfile.gettempdir", gettempdir_patched):
        result = await manager.call_tool("server__tool", {})

    assert "full output saved to:" not in result
    assert "[output truncated: 1500 chars -> 1000 kept." in result
    assert "contains a symlink" in caplog.text
    assert not list(target_dir.rglob("*.txt"))


@pytest.mark.asyncio
async def test_explicit_spill_dir_ancestor_symlink_is_rejected(
    fixture: _TruncationManagerFixture, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A symlink in the middle of an explicit spill_dir path is rejected."""
    full_text = "x" * 1500
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    middle_dir = tmp_path / "middle"
    middle_dir.mkdir()
    symlink_component = middle_dir / "link"
    symlink_component.symlink_to(target_dir)
    spill_dir = symlink_component / "tool-output"
    config = ToolOutputConfig(
        enabled=True, max_chars=1000, spill_to_file=True, spill_dir=str(spill_dir)
    )
    manager = fixture.make_manager(config)
    fixture.set_result(full_text)

    result = await manager.call_tool("server__tool", {})

    assert "full output saved to:" not in result
    assert "[output truncated: 1500 chars -> 1000 kept." in result
    assert "contains a symlink" in caplog.text
    assert not list(target_dir.rglob("*.txt"))


@pytest.mark.asyncio
async def test_spill_dir_with_null_byte_does_not_raise(
    fixture: _TruncationManagerFixture,
) -> None:
    """A spill_dir containing a NUL byte degrades gracefully; tool call still
    returns truncated result."""
    full_text = "x" * 1500
    config = ToolOutputConfig(
        enabled=True,
        max_chars=1000,
        spill_to_file=True,
        spill_dir="/tmp/foo\x00bar",
    )
    manager = fixture.make_manager(config)
    fixture.set_result(full_text)

    result = await manager.call_tool("server__tool", {})

    assert result.startswith("x" * 1000)
    assert "[output truncated: 1500 chars -> 1000 kept." in result
    assert "full output saved to:" not in result


@pytest.mark.asyncio
async def test_spill_dir_too_long_does_not_raise(
    fixture: _TruncationManagerFixture,
) -> None:
    """An over-long spill_dir path degrades gracefully; tool call still returns
    truncated result."""
    full_text = "x" * 1500
    config = ToolOutputConfig(
        enabled=True,
        max_chars=1000,
        spill_to_file=True,
        spill_dir="a" * 10000,
    )
    manager = fixture.make_manager(config)
    fixture.set_result(full_text)

    result = await manager.call_tool("server__tool", {})

    assert result.startswith("x" * 1000)
    assert "[output truncated: 1500 chars -> 1000 kept." in result
    assert "full output saved to:" not in result


@pytest.mark.asyncio
async def test_spill_dir_resolve_oserror_does_not_raise(
    fixture: _TruncationManagerFixture,
    tmp_path: Path,
) -> None:
    """If Path.resolve() raises OSError during spill setup, tool call still
    returns truncated result."""
    full_text = "x" * 1500
    config = ToolOutputConfig(
        enabled=True,
        max_chars=1000,
        spill_to_file=True,
        spill_dir=str(tmp_path),
    )
    manager = fixture.make_manager(config)
    fixture.set_result(full_text)

    with patch("conductor.mcp.manager.Path.resolve", side_effect=OSError("too many links")):
        result = await manager.call_tool("server__tool", {})

    assert result.startswith("x" * 1000)
    assert "[output truncated: 1500 chars -> 1000 kept." in result
    assert "full output saved to:" not in result
