"""Path-component ownership boundary for SDK diagnostic records."""

import os

from conductor.mcp import diagnostics


def test_diagnostic_source_component_windows_and_symlink_boundary(tmp_path):
    # Requirement: source ownership uses components, native symlinks and Windows case semantics.
    root = tmp_path / "mcp"
    root.mkdir()
    inside = root / "client" / "session.py"
    assert diagnostics._is_sdk_source(str(inside), str(root))
    if os.name == "nt":
        assert diagnostics._is_sdk_source(str(inside).replace("\\", "/"), str(root))
    for path in (tmp_path / "mcp_extra/session.py", tmp_path / "mcp.cmd", tmp_path / "session.py"):
        assert not diagnostics._is_sdk_source(str(path), str(root))
    if os.name != "nt":
        literal = tmp_path / "mcp\\session.py"
        literal.write_text("# application source outside the SDK\n")
        assert not diagnostics._is_sdk_source(str(literal), str(root))
        alias = tmp_path / "sdk-alias"
        alias.symlink_to(root, target_is_directory=True)
        assert diagnostics._is_sdk_source(str(alias / "session.py"), str(root))
        outside = tmp_path / "external"
        outside.mkdir()
        (root / "escape").symlink_to(outside, target_is_directory=True)
        assert not diagnostics._is_sdk_source(str(root / "escape/session.py"), str(root))
        assert diagnostics._is_sdk_source(r"C:\SDK\MCP\client\session.py", r"c:\sdk\mcp")
        assert not diagnostics._is_sdk_source(r"C:\SDK\MCP_extra\session.py", r"c:\sdk\mcp")
