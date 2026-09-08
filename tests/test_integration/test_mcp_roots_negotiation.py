"""Pins the MCP Roots rule that governs what a filesystem MCP server permits.

``settings_dir`` governs skill discovery while cwd alone governs what a
filesystem MCP server permits, and the two cannot be collapsed into one
option. Deriving ``ClaudeAgentOptions.add_dirs`` from the directory arguments
of every stdio MCP server looks like it would widen that server's scope back;
it cannot, and the reason is a property of the *server*, not of Conductor:

``@modelcontextprotocol/server-filesystem`` uses the directories in its argv
only while the connected client does not support MCP Roots. A client that
advertises the ``roots`` capability is asked for its roots at
post-initialization, and whatever it answers **replaces** the argv
directories outright. The Claude CLI advertises Roots and offers exactly one
root -- its cwd -- so a server declared with two directories ends up
permitting one, and ``--add-dir`` cannot put the others back because it takes
no part in that negotiation.

This test pins the rule itself with a hand-rolled JSON-RPC client and no LLM:
two runs, byte-identical but for the client's ``roots`` capability. That is
what isolates the cause to Roots negotiation rather than to cwd derivation,
to ``--add-dir`` handling, or to any Conductor code path -- and what would
fail if a future server version changed the precedence, which is the
assumption ``settings_dir`` rests on.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

_SERVER = "@modelcontextprotocol/server-filesystem"
_ADOPTED = "Updated allowed directories from MCP roots"

# ``real_api`` because this fetches @modelcontextprotocol/server-filesystem from
# npm: it pins *upstream's* negotiation behaviour rather than Conductor's own
# code, so an npm outage or a new server release must not redden an unrelated
# PR. The repo's convention for a test reaching an external service is an
# opt-in marker (cf. ``real_api`` / ``install_scripts`` / ``performance`` in
# pyproject.toml); CI runs ``-m "not real_api and not performance"``.
pytestmark = [
    pytest.mark.real_api,
    pytest.mark.skipif(
        shutil.which("npx") is None,
        reason="npx not available; needs the real filesystem MCP server",
    ),
]


def _list_allowed_directories(
    *, root_dirs: list[str], cwd: str, roots: list[dict[str, str]] | None
) -> str:
    """Call ``list_allowed_directories`` on a real server and return its text.

    ``roots=None`` declares no ``roots`` capability, so the server is never
    asked and keeps its argv directories. A list declares the capability and
    is what the server receives when it asks.
    """
    # ``shutil.which`` finds ``npx.cmd`` on Windows but ``CreateProcess`` only
    # appends ``.exe``, so a bare "npx" would fail to launch there while the
    # skipif above says it is present. Pass the resolved path.
    npx = shutil.which("npx")
    assert npx is not None  # guarded by the module-level skipif
    # A plain file rather than NamedTemporaryFile: the stderr poll below reopens
    # it by name, which is unsupported while the handle is open on Windows.
    err_path = Path(tempfile.mkdtemp(prefix="mcp-roots-")) / "server.err"
    with err_path.open("w") as errf:
        proc = subprocess.Popen(  # noqa: S603
            [npx, "-y", _SERVER, *root_dirs],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errf,
            text=True,
            cwd=cwd,
        )
        assert proc.stdin is not None and proc.stdout is not None

        def send(payload: dict) -> None:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()

        def call_tool() -> None:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "list_allowed_directories", "arguments": {}},
                }
            )

        def server_stderr() -> str:
            errf.flush()
            return err_path.read_text(errors="replace")

        try:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"roots": {"listChanged": False}} if roots else {},
                        "clientInfo": {"name": "conductor-roots-probe", "version": "1"},
                    },
                }
            )
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                line = proc.stdout.readline()
                if not line:
                    pytest.fail(f"server exited early; stderr:\n{server_stderr()}")
                message = json.loads(line)

                if message.get("id") == 1:
                    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                    # A client with no roots capability is never asked for
                    # roots, so nothing else will arrive to sequence against.
                    if roots is None:
                        call_tool()
                elif message.get("method") == "roots/list":
                    send({"jsonrpc": "2.0", "id": message["id"], "result": {"roots": roots}})
                    # The server swaps its allowlist in the continuation of its
                    # own ``listRoots()`` await, so a tool call sent straight
                    # after this reply races it and reads the pre-swap list --
                    # which is exactly the false negative that made an earlier
                    # version of this probe report "argv roots survive".
                    swap = time.monotonic() + 30
                    while _ADOPTED not in server_stderr() and time.monotonic() < swap:
                        time.sleep(0.05)
                    call_tool()
                elif message.get("id") == 2:
                    return str(message["result"]["content"][0]["text"])
            pytest.fail(f"timed out; stderr:\n{server_stderr()}")
        finally:
            proc.kill()
            proc.wait(timeout=30)
    raise AssertionError("unreachable")


@pytest.fixture
def roots_tree(tmp_path: Path) -> dict[str, str]:
    """Two declared server roots and a cwd that is neither, nor kin to either."""
    for name in ("rootA", "rootB", "cwdC"):
        (tmp_path / name).mkdir()
    (tmp_path / "rootB" / "target.txt").write_text("hello-from-rootB\n")
    return {n: str(tmp_path / n) for n in ("rootA", "rootB", "cwdC")}


def test_argv_roots_honoured_when_client_declares_no_roots(roots_tree: dict[str, str]) -> None:
    """The server is not broken and cwd is irrelevant to it.

    Without the capability the argv directories are the allowlist, in full --
    the baseline that makes the contrast below attributable to negotiation.
    """
    allowed = _list_allowed_directories(
        root_dirs=[roots_tree["rootA"], roots_tree["rootB"]],
        cwd=roots_tree["cwdC"],
        roots=None,
    )

    assert roots_tree["rootA"] in allowed
    assert roots_tree["rootB"] in allowed
    assert roots_tree["cwdC"] not in allowed


def test_single_advertised_root_replaces_every_argv_root(roots_tree: dict[str, str]) -> None:
    """The defect, in one assertion: a client's sole root wins outright.

    Identical argv and identical cwd to the test above. Declaring ``roots``
    and answering with cwd alone -- what the Claude CLI does -- discards both
    declared directories. No value of ``add_dirs`` changes this, which is why
    ``settings_dir`` governs skill discovery and cwd alone governs MCP scope.
    """
    allowed = _list_allowed_directories(
        root_dirs=[roots_tree["rootA"], roots_tree["rootB"]],
        cwd=roots_tree["cwdC"],
        roots=[{"uri": f"file://{roots_tree['cwdC']}", "name": "cwd"}],
    )

    assert roots_tree["cwdC"] in allowed
    assert roots_tree["rootA"] not in allowed
    assert roots_tree["rootB"] not in allowed


def test_a_root_containing_both_declared_roots_permits_both(roots_tree: dict[str, str]) -> None:
    """Why a single root is sufficient, and the basis of the recommended fix.

    One advertised root still permits everything beneath it, so an agent whose
    cwd is a common parent reaches both declared roots. That is what makes
    "keep cwd wide, select conventions with ``settings_dir``" work rather than
    needing a server per root.
    """
    parent = str(Path(roots_tree["rootA"]).parent)

    allowed = _list_allowed_directories(
        root_dirs=[roots_tree["rootA"], roots_tree["rootB"]],
        cwd=parent,
        roots=[{"uri": f"file://{parent}", "name": "cwd"}],
    )

    assert parent in allowed
    assert os.path.commonpath([parent, roots_tree["rootB"]]) == parent
