"""Real end-to-end integration test for issue #544: `conductor mcp serve`
launches a workflow from the Copilot host's cwd, not the server's own
inherited (or plugin-install) directory.

Unlike the rest of ``tests/test_mcp/test_serve_*.py``, this file mocks
nothing but the process-inspection seam (and only for the simulated
ancestry case) -- it spawns a real ``conductor mcp serve`` subprocess over
real stdio, dispatches a real ``tools/call`` through the SDK's own
``ClientSession``, and reads the resulting *real* detached workflow
child's own JSONL event log to confirm what directory it actually
launched into. That is the assertion nothing else in this test suite can
make: a mocked ``launch_background`` call proves what argument the code
passed, not what directory a real launched process actually ran in.

``examples/wait-smoke.yaml`` is used because it is CI's existing
provider-free smoke fixture (`--web-bg` launcher tests already reuse it) --
no provider credentials, and it finishes in about a second.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from conductor.cli.pid import is_process_alive, terminate_process
from conductor.fleet.records import read_run_record

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WAIT_SMOKE_SOURCE = _REPO_ROOT / "examples" / "wait-smoke.yaml"

_STARTUP_TIMEOUT_SECONDS = 30.0
"""Bound for the MCP session handshake plus the tool dispatch -- generous
for a cold Windows CI runner, matching `wait-smoke.yaml`'s own comment
about multi-second process overhead there."""

_EVENT_POLL_TIMEOUT_SECONDS = 30.0
"""Bound for polling the detached child's own JSONL event log for its
`workflow_started` event -- this is the real end-to-end assertion, so it
tolerates real process-startup latency, not just the ~1s of actual wait
time `wait-smoke.yaml` itself performs."""

_POLL_INTERVAL_SECONDS = 0.2


def _isolated_subprocess_env(*, home: Path, conductor_home: Path, tmp_dir: Path) -> dict[str, str]:
    """Build a subprocess environment isolated from the developer's real
    home / `~/.conductor` / OS temp directory.

    Pytest's in-process `monkeypatch.setenv` never crosses a subprocess
    boundary, so every directory the launched child (and the server
    itself) could touch is overridden explicitly here instead.
    """
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)  # Windows equivalent of $HOME
    env["CONDUCTOR_HOME"] = str(conductor_home)
    # `tempfile.gettempdir()` checks TMPDIR/TEMP/TMP, in that order, on
    # every platform -- set all three so the isolation holds regardless of
    # which one the local `tempfile` module implementation prefers.
    for name in ("TMPDIR", "TEMP", "TMP"):
        env[name] = str(tmp_dir)
    env["CONDUCTOR_NO_UPDATE_CHECK"] = "1"
    return env


def _write_wait_smoke_copy(workflow_dir: Path) -> None:
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "wait-smoke.yaml").write_text(
        _WAIT_SMOKE_SOURCE.read_text(encoding="utf-8"), encoding="utf-8"
    )


def _find_root_workflow_started(event_log_path: Path) -> dict[str, Any] | None:
    """Return the first `workflow_started` event's payload from
    *event_log_path*, or ``None`` if the file doesn't exist yet or
    contains no such event (yet)."""
    if not event_log_path.exists():
        return None
    try:
        text = event_log_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "workflow_started":
            return event
    return None


def _event_log_glob(tmp_dir: Path, run_id: str) -> list[Path]:
    return sorted((tmp_dir / "conductor").glob(f"conductor-wait-smoke-*-{run_id}.events.jsonl"))


_SIMULATED_HOST_CWD_ENV_VAR = "_CONDUCTOR_TEST_SIMULATED_HOST_CWD"


def _write_detection_override_wrapper(tmp_path: Path) -> Path:
    """Write a wrapper script that runs the real ``conductor`` CLI with
    ``detect_copilot_ancestor_cwd`` patched to return whatever
    :data:`_SIMULATED_HOST_CWD_ENV_VAR` names, or ``None`` when that env
    var is unset or empty.

    Real ancestor process names can't be faked cross-platform from a
    pytest fixture, and isolating ``HOME``/``CONDUCTOR_HOME``/the temp
    dir does not isolate process ancestry -- so this is the one seam
    mocked, inside a real, separate subprocess, while everything else
    (the real server, the real stdio transport, the real detached
    workflow child, the real event log) stays unmocked. Used both to
    simulate a Copilot ancestor and to force "no ancestor detected"
    deterministically, regardless of whether the test runner itself
    happens to have a real Copilot ancestor.
    """
    wrapper_script = tmp_path / "detection_override_wrapper.py"
    wrapper_script.write_text(
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "from unittest.mock import patch\n"
        "\n"
        f'raw = os.environ.get("{_SIMULATED_HOST_CWD_ENV_VAR}")\n'
        "host_cwd = Path(raw) if raw else None\n"
        "with patch(\n"
        '    "conductor.mcp.serve.launch_dir.detect_copilot_ancestor_cwd",\n'
        "    return_value=host_cwd,\n"
        "):\n"
        "    from conductor.cli.app import app\n"
        "\n"
        "    app(sys.argv[1:])\n",
        encoding="utf-8",
    )
    return wrapper_script


async def _wait_for_root_workflow_started(
    *, tmp_dir: Path, run_id: str, deadline: float
) -> dict[str, Any]:
    """Poll for the detached child's own event log and its root
    `workflow_started` event, bounded by *deadline* (a `time.monotonic()`
    timestamp)."""
    while True:
        matches = _event_log_glob(tmp_dir, run_id)
        if matches:
            event = _find_root_workflow_started(matches[0])
            if event is not None:
                return event
        if time.monotonic() >= deadline:
            pytest.fail(
                f"Timed out waiting for run {run_id}'s workflow_started event under {tmp_dir}"
            )
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


def _cleanup_run(run_id: str) -> None:
    """Best-effort cleanup for the real detached child this test launched.

    `wait-smoke.yaml` finishes in about a second, so this is almost always
    a no-op by the time it runs -- it only forcefully terminates a
    survivor, and only one this test itself is responsible for (looked up
    by the `run_id` this test's own tool call returned).
    """
    record = read_run_record(run_id)
    if record is None:
        return
    if is_process_alive(record.pid):
        terminate_process(record.pid, timeout=5.0)


async def _launch_wait_smoke_and_get_run_id(params: StdioServerParameters) -> str:
    """Connect to the server named by *params*, dispatch the generated
    `wait_smoke` tool with `_wait_seconds: 0` (an immediate, non-blocking
    return), and return the launched run's `run_id`.

    Bounded by :data:`_STARTUP_TIMEOUT_SECONDS` so a broken server startup
    fails this test loudly instead of hanging it indefinitely.
    """

    async def _dispatch() -> str:
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            # `middle_duration_ms` has a workflow-level default, but the
            # generated schema marks every declared input `required`
            # regardless -- supply it explicitly rather than relying on
            # the SDK's `validate_input` accepting an omission.
            result = await session.call_tool(
                "wait_smoke", {"middle_duration_ms": 100, "_wait_seconds": 0}
            )
            assert result.isError is not True, result
            assert result.structuredContent is not None
            return str(result.structuredContent["run_id"])

    return await asyncio.wait_for(_dispatch(), timeout=_STARTUP_TIMEOUT_SECONDS)


class TestRealStdioLaunchUsesTheConfiguredDirectory:
    """Spawns a real `conductor mcp serve` subprocess, dispatches a real
    tool call over real stdio, and reads the real detached child's own
    event log."""

    async def test_default_invocation_launches_from_the_servers_own_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No `--launch-dir` and no Copilot ancestor detected: the launched
        child's cwd is the server's own startup cwd.

        Detection is patched to return `None` (via the same
        simulated-host wrapper-script pattern used below) rather than
        relying on there being no real Copilot ancestor of the test
        runner -- isolating `HOME`/`CONDUCTOR_HOME`/the temp dir does not
        isolate process ancestry, so this test would otherwise fail (or
        pass for the wrong reason) whenever it happens to run underneath
        a real Copilot session.
        """
        home = tmp_path / "home"
        conductor_home = tmp_path / "conductor_home"
        isolated_tmp = tmp_path / "isolated_tmp"
        server_cwd = tmp_path / "server-cwd"
        workflow_dir = tmp_path / "workflows"
        for directory in (home, conductor_home, isolated_tmp, server_cwd):
            directory.mkdir(parents=True, exist_ok=True)
        _write_wait_smoke_copy(workflow_dir)

        wrapper_script = _write_detection_override_wrapper(tmp_path)

        env = _isolated_subprocess_env(
            home=home, conductor_home=conductor_home, tmp_dir=isolated_tmp
        )
        # Force "no ancestor detected" deterministically -- see
        # `_write_detection_override_wrapper`'s docstring for why relying
        # on there being no *real* Copilot ancestor of the test runner is
        # not safe.
        env[_SIMULATED_HOST_CWD_ENV_VAR] = ""
        # `_cleanup_run` reads run records via `conductor.fleet.records`,
        # which resolves `CONDUCTOR_HOME` from this (pytest) process's own
        # environment -- point it at the same isolated directory the
        # subprocess writes its records to.
        monkeypatch.setenv("CONDUCTOR_HOME", str(conductor_home))
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                str(wrapper_script),
                "mcp",
                "serve",
                "--workflow-dir",
                str(workflow_dir),
            ],
            env=env,
            cwd=str(server_cwd),
        )

        run_id = None
        try:
            run_id = await _launch_wait_smoke_and_get_run_id(params)

            deadline = time.monotonic() + _EVENT_POLL_TIMEOUT_SECONDS
            event = await _wait_for_root_workflow_started(
                tmp_dir=isolated_tmp, run_id=run_id, deadline=deadline
            )
            # No `--launch-dir`, and no Copilot ancestor to detect in this
            # isolated environment: the launched child's cwd is the
            # server's own startup cwd.
            assert event["data"]["system"]["cwd"] == str(server_cwd)
        finally:
            if run_id is not None:
                _cleanup_run(run_id)

    async def test_explicit_launch_dir_override_is_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        conductor_home = tmp_path / "conductor_home"
        isolated_tmp = tmp_path / "isolated_tmp"
        server_cwd = tmp_path / "server-cwd"
        explicit_dir = tmp_path / "explicit-launch-dir"
        workflow_dir = tmp_path / "workflows"
        for directory in (home, conductor_home, isolated_tmp, server_cwd, explicit_dir):
            directory.mkdir(parents=True, exist_ok=True)
        _write_wait_smoke_copy(workflow_dir)

        env = _isolated_subprocess_env(
            home=home, conductor_home=conductor_home, tmp_dir=isolated_tmp
        )
        monkeypatch.setenv("CONDUCTOR_HOME", str(conductor_home))
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "conductor",
                "mcp",
                "serve",
                "--workflow-dir",
                str(workflow_dir),
                "--launch-dir",
                str(explicit_dir),
            ],
            env=env,
            cwd=str(server_cwd),
        )

        run_id = None
        try:
            run_id = await _launch_wait_smoke_and_get_run_id(params)

            deadline = time.monotonic() + _EVENT_POLL_TIMEOUT_SECONDS
            event = await _wait_for_root_workflow_started(
                tmp_dir=isolated_tmp, run_id=run_id, deadline=deadline
            )
            # `--launch-dir` wins even though it differs from the server's
            # own inherited cwd.
            assert event["data"]["system"]["cwd"] == str(explicit_dir)
            assert event["data"]["system"]["cwd"] != str(server_cwd)
        finally:
            if run_id is not None:
                _cleanup_run(run_id)

    async def test_simulated_copilot_ancestry_wins_over_the_servers_own_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The server's own cwd (`plugin_cwd`, standing in for a plugin's
        install directory) and the detected Copilot host's cwd
        (`host_cwd`) are deliberately different: only ancestor detection
        winning over the naive server-cwd default proves this feature
        rather than a coincidence.

        Real ancestor process names can't be faked cross-platform from a
        pytest fixture, so this mocks *only* `detect_copilot_ancestor_cwd`
        -- inside a small wrapper script executed as the real, separate
        subprocess -- while every other part of the run (the real server,
        the real stdio transport, the real detached workflow child, the
        real event log) is unmocked.
        """
        home = tmp_path / "home"
        conductor_home = tmp_path / "conductor_home"
        isolated_tmp = tmp_path / "isolated_tmp"
        plugin_cwd = tmp_path / "plugin-install-dir"
        host_cwd = tmp_path / "actual-project"
        workflow_dir = tmp_path / "workflows"
        for directory in (home, conductor_home, isolated_tmp, plugin_cwd, host_cwd):
            directory.mkdir(parents=True, exist_ok=True)
        _write_wait_smoke_copy(workflow_dir)

        wrapper_script = _write_detection_override_wrapper(tmp_path)

        env = _isolated_subprocess_env(
            home=home, conductor_home=conductor_home, tmp_dir=isolated_tmp
        )
        monkeypatch.setenv("CONDUCTOR_HOME", str(conductor_home))
        env[_SIMULATED_HOST_CWD_ENV_VAR] = str(host_cwd)
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                str(wrapper_script),
                "mcp",
                "serve",
                "--workflow-dir",
                str(workflow_dir),
            ],
            env=env,
            cwd=str(plugin_cwd),
        )

        run_id = None
        try:
            run_id = await _launch_wait_smoke_and_get_run_id(params)

            deadline = time.monotonic() + _EVENT_POLL_TIMEOUT_SECONDS
            event = await _wait_for_root_workflow_started(
                tmp_dir=isolated_tmp, run_id=run_id, deadline=deadline
            )
            assert event["data"]["system"]["cwd"] == str(host_cwd)
            assert event["data"]["system"]["cwd"] != str(plugin_cwd)
        finally:
            if run_id is not None:
                _cleanup_run(run_id)
