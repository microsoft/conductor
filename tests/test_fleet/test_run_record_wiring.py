"""Tests wiring the Fleet Manager run record into every execution path
(Fleet Manager E2 — closing the design's blocking problem: "Only
``--web-bg`` runs write a run record; foreground runs are invisible").

Covers ``run_workflow_async`` (fg, fg-web, bg) and ``resume_workflow_async``
(mirroring the same three: fg, fg-web, bg), asserting for each:

- A record is discoverable *while the workflow is executing* (checked from
  inside a mocked ``WorkflowEngine.run``/``resume`` side effect, since the
  record is written before the engine runs and removed in the ``finally``
  after it returns).
- ``mode`` matches the execution path (``fg`` / ``fg-web`` / ``bg``).
- ``port`` is populated only when a dashboard is actually present, and
  ``None`` for a plain foreground run.
- ``event_log_path`` matches the real ``EventLogSubscriber`` path used for
  that run (the JSONL subscriber is not mocked — this is a genuine,
  non-mocked I/O detail of the wiring under test).
- The record is removed once the run finishes, whether by clean
  completion, an explicit ``WorkflowTerminated``, or an unexpected
  exception -- all three routes converge on the same ``finally`` block
  (asserted for both ``run_workflow_async`` and ``resume_workflow_async``).
- A resumed run replaces its original record (same ``run_id``) instead of
  creating a second one.

Everything except the JSONL event log and the run-record read/write/remove
primitives themselves is mocked (config loading, provider registry, the
workflow engine, and — for the web-enabled cases — the dashboard), so these
are fast, deterministic unit tests of the wiring rather than full end-to-end
agent execution (already covered by ``tests/test_cli/test_e2e.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor.config.schema import ProviderSettings
from conductor.exceptions import WorkflowTerminated
from conductor.fleet.records import read_run_records, read_terminal_record, read_terminal_records

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def fleet_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect both the new run-record directory (via ``CONDUCTOR_HOME``)
    and the legacy ``.pid`` directory (via ``cli.pid.pid_dir``) to isolated
    temporary directories, mirroring ``tests/test_fleet/test_records.py``'s
    fixture of the same name -- without this, ``read_run_records()`` would
    also pick up any real legacy ``.pid`` files under the developer's actual
    ``~/.conductor/runs/``.

    Also strips any ambient ``CONDUCTOR_WEB_BG``/``CONDUCTOR_RUN_ID`` from
    the *real* process environment: this test session may itself be running
    inside a ``--web-bg`` child (e.g. a background Copilot CLI agent), whose
    real env vars would otherwise leak into ``_derive_run_mode`` and
    ``EventLogSubscriber``'s own env-var fallback, corrupting the ``mode``
    and ``run_id`` these tests assert on.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))

    legacy_dir = tmp_path / "legacy_runs"
    legacy_dir.mkdir()
    monkeypatch.setattr("conductor.cli.pid.pid_dir", lambda: legacy_dir)

    monkeypatch.delenv("CONDUCTOR_WEB_BG", raising=False)
    monkeypatch.delenv("CONDUCTOR_RUN_ID", raising=False)

    return home


def _mock_config(name: str = "wiring-test", agent_names: list[str] | None = None) -> MagicMock:
    """Build the same minimal ``WorkflowConfig`` stand-in already proven to
    let ``run_workflow_async``/``resume_workflow_async`` execute up through
    ``engine.run``/``engine.resume`` without error (mirrors the mock used in
    ``tests/test_cli/test_web_flags.py::test_dashboard_start_oserror_is_non_fatal``).

    ``agent_names`` populates ``config.agents`` with mock agents exposing a
    ``.name`` (a plain ``MagicMock(name=...)`` would NOT do this --
    ``name=`` on ``MagicMock`` sets the *mock's* repr name, not a ``.name``
    attribute). Only ``resume_workflow_async`` checks membership
    (``cp.current_agent in all_names``); ``run_workflow_async`` never
    inspects ``config.agents`` beyond ``len()``.
    """
    mock_config = MagicMock()
    mock_config.workflow.name = name
    mock_config.workflow.entry_point = "agent1"
    agents = []
    for agent_name in agent_names or []:
        agent_mock = MagicMock()
        agent_mock.name = agent_name
        agents.append(agent_mock)
    mock_config.agents = agents
    mock_config.parallel = []
    mock_config.for_each = []
    mock_config.workflow.runtime.provider = ProviderSettings(name="copilot")
    mock_config.workflow.limits.max_iterations = 50
    mock_config.workflow.limits.timeout_seconds = None
    mock_config.workflow.limits.budget_usd = None
    mock_config.workflow.limits.budget_mode = "hard"
    mock_config.workflow.cost.show_summary = False
    mock_config.tools = None
    mock_config.mcp_servers = []
    return mock_config


def _mock_dashboard(port: int) -> MagicMock:
    """A ``WebDashboard`` stand-in with a fixed resolved port.

    ``wait_for_stop`` is assigned a plain async function (not ``AsyncMock``)
    that blocks forever, mirroring the pattern in
    ``tests/test_cli/test_resume_command.py::TestExecuteWithStopSignal`` so
    ``_execute_with_stop_signal`` always resolves via the (mocked) engine
    coroutine finishing first, never via the dashboard "stop" race.
    """
    dashboard = MagicMock()
    dashboard.port = port
    dashboard.url = f"http://127.0.0.1:{port}"
    dashboard.start = AsyncMock()
    dashboard.stop = AsyncMock()
    dashboard.wait_for_clients_disconnect = AsyncMock()

    async def _never_stop() -> None:
        await asyncio.Event().wait()

    dashboard.wait_for_stop = _never_stop
    return dashboard


def _write_workflow(tmp_path: Path) -> Path:
    wf = tmp_path / "wf.yaml"
    wf.write_text("workflow: {name: wiring-test, entry_point: a}\nagents: []\n")
    return wf


# ---------------------------------------------------------------------------
# run_workflow_async: fg / fg-web / bg
# ---------------------------------------------------------------------------


class TestRunWorkflowAsyncRunRecordWiring:
    """``run_workflow_async`` writes a discoverable record on every path."""

    async def test_fg_record_has_no_port_and_is_removed_on_completion(
        self, tmp_path: Path, fleet_env: Path
    ) -> None:
        from conductor.cli.run import run_workflow_async

        wf_path = _write_workflow(tmp_path)
        mock_config = _mock_config()
        seen: dict[str, Any] = {}

        async def _fake_run(inputs: dict[str, Any]) -> dict[str, Any]:
            # Assert the record is discoverable *while the run is in flight*.
            records = read_run_records()
            assert len(records) == 1
            record = records[0]
            seen["record"] = record
            return {"result": "done"}

        mock_engine = MagicMock()
        mock_engine.run = _fake_run
        mock_engine.get_execution_summary.return_value = {}

        with (
            patch("conductor.cli.run.load_config", return_value=mock_config),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
            patch("conductor.cli.run.ProviderRegistry") as mock_registry,
            patch(
                "conductor.cli.run._build_mcp_servers",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            mock_registry.return_value.__aenter__ = AsyncMock(return_value=mock_registry)
            mock_registry.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await run_workflow_async(wf_path, {})

        assert result == {"result": "done"}
        record = seen["record"]
        assert record.mode == "fg"
        assert record.port is None
        assert record.workflow_path == str(wf_path)
        # ``workflow_name`` must be the workflow *file's* stem (matching
        # ``CheckpointManager``'s checkpoint-filename convention), not the
        # YAML-declared ``config.workflow.name`` -- the mock config's name
        # ("wiring-test") deliberately differs from the file's stem ("wf")
        # so this assertion actually exercises the distinction.
        assert record.workflow_name == wf_path.stem == "wf"
        assert Path(record.event_log_path).exists()
        assert record.checkpoint_dir is not None

        # Removed once the run finishes.
        assert read_run_records() == []

    async def test_fg_web_record_carries_dashboard_port(
        self, tmp_path: Path, fleet_env: Path
    ) -> None:
        """``web=True`` without ``web_bg`` is a foreground run that keeps
        the dashboard open after the workflow finishes -- ``run_workflow_async``
        deliberately blocks on ``await asyncio.Event().wait()`` until the
        user Ctrl+C's out (real production behavior, so a human watching the
        dashboard doesn't lose it the instant the workflow completes). This
        test drives that via a cancellable task rather than awaiting the
        coroutine directly, which would hang the test suite.
        """
        from conductor.cli.run import run_workflow_async

        wf_path = _write_workflow(tmp_path)
        mock_config = _mock_config()
        seen: dict[str, Any] = {}
        dashboard = _mock_dashboard(port=8123)

        async def _fake_run(inputs: dict[str, Any]) -> dict[str, Any]:
            records = read_run_records()
            assert len(records) == 1
            seen["record"] = records[0]
            return {"result": "done"}

        mock_engine = MagicMock()
        mock_engine.run = _fake_run
        mock_engine.get_execution_summary.return_value = {}

        mock_web_module = MagicMock()
        mock_web_module.WebDashboard.return_value = dashboard

        import sys

        with (
            patch("conductor.cli.run.load_config", return_value=mock_config),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
            patch("conductor.cli.run.ProviderRegistry") as mock_registry,
            patch(
                "conductor.cli.run._build_mcp_servers",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.dict(sys.modules, {"conductor.web.server": mock_web_module}),
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = False
            mock_registry.return_value.__aenter__ = AsyncMock(return_value=mock_registry)
            mock_registry.return_value.__aexit__ = AsyncMock(return_value=None)

            task = asyncio.ensure_future(
                run_workflow_async(wf_path, {}, web=True, no_interactive=True)
            )
            # Wait until the record has been observed mid-run, then cancel
            # the "keep dashboard open" wait -- ``run_workflow_async``
            # suppresses the resulting CancelledError and proceeds straight
            # to its normal ``finally`` cleanup (record removal, dashboard
            # stop), so ``await task`` still resolves to the real result.
            for _ in range(200):
                if "record" in seen:
                    break
                await asyncio.sleep(0.01)
            assert "record" in seen, "engine.run side effect never ran"
            # Let the task unwind past the handful of quick, non-blocking
            # awaits between the mocked engine call returning and the final
            # "keep dashboard open" wait (e.g. ``_execute_with_stop_signal``
            # cancelling and draining its own losing "stop" task) before
            # cancelling. Cancelling too early can otherwise land the
            # ``CancelledError`` on one of those earlier awaits instead of
            # the one this test intends to interrupt, since only the final
            # wait is wrapped in ``contextlib.suppress(asyncio.CancelledError)``.
            for _ in range(20):
                await asyncio.sleep(0)
            task.cancel()
            result = await task

        assert result == {"result": "done"}
        record = seen["record"]
        assert record.mode == "fg-web"
        assert record.port == 8123
        assert read_run_records() == []

    async def test_bg_record_has_mode_bg(self, tmp_path: Path, fleet_env: Path) -> None:
        """``--web-bg`` (or the ``CONDUCTOR_WEB_BG`` env var set on a
        detached child) always records ``mode="bg"``, even though a
        dashboard is also present -- D1 must never prompt for stop
        confirmation on these."""
        from conductor.cli.run import run_workflow_async

        wf_path = _write_workflow(tmp_path)
        mock_config = _mock_config()
        seen: dict[str, Any] = {}
        dashboard = _mock_dashboard(port=8124)

        async def _fake_run(inputs: dict[str, Any]) -> dict[str, Any]:
            records = read_run_records()
            assert len(records) == 1
            seen["record"] = records[0]
            return {"result": "done"}

        mock_engine = MagicMock()
        mock_engine.run = _fake_run
        mock_engine.get_execution_summary.return_value = {}

        mock_web_module = MagicMock()
        mock_web_module.WebDashboard.return_value = dashboard

        import sys

        with (
            patch("conductor.cli.run.load_config", return_value=mock_config),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
            patch("conductor.cli.run.ProviderRegistry") as mock_registry,
            patch(
                "conductor.cli.run._build_mcp_servers",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.dict(sys.modules, {"conductor.web.server": mock_web_module}),
        ):
            mock_registry.return_value.__aenter__ = AsyncMock(return_value=mock_registry)
            mock_registry.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await run_workflow_async(
                wf_path, {}, web=True, web_bg=True, no_interactive=True
            )

        assert result == {"result": "done"}
        record = seen["record"]
        assert record.mode == "bg"
        assert record.port == 8124
        assert read_run_records() == []

    async def test_record_removed_on_workflow_terminated(
        self, tmp_path: Path, fleet_env: Path
    ) -> None:
        """A ``status: failed`` terminate step re-raises ``WorkflowTerminated``
        (see ``docs/workflow-syntax.md``'s Terminate Steps section); the run
        record must still be removed via the shared ``finally``."""
        from conductor.cli.run import run_workflow_async

        wf_path = _write_workflow(tmp_path)
        mock_config = _mock_config()

        terminate_exc = WorkflowTerminated(
            "bye",
            output={},
            reason="bye",
            terminated_by="term",
        )

        async def _fake_run(inputs: dict[str, Any]) -> dict[str, Any]:
            # The record exists while the run is executing...
            assert len(read_run_records()) == 1
            raise terminate_exc

        mock_engine = MagicMock()
        mock_engine.run = _fake_run
        mock_engine.get_execution_summary.return_value = {}

        with (
            patch("conductor.cli.run.load_config", return_value=mock_config),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
            patch("conductor.cli.run.ProviderRegistry") as mock_registry,
            patch(
                "conductor.cli.run._build_mcp_servers",
                new_callable=AsyncMock,
                return_value=None,
            ),
            pytest.raises(WorkflowTerminated),
        ):
            mock_registry.return_value.__aenter__ = AsyncMock(return_value=mock_registry)
            mock_registry.return_value.__aexit__ = AsyncMock(return_value=None)

            await run_workflow_async(wf_path, {})

        # ...and is gone once the exception propagates out.
        assert read_run_records() == []

    async def test_record_removed_on_unexpected_exception(
        self, tmp_path: Path, fleet_env: Path
    ) -> None:
        from conductor.cli.run import run_workflow_async

        wf_path = _write_workflow(tmp_path)
        mock_config = _mock_config()

        async def _fake_run(inputs: dict[str, Any]) -> dict[str, Any]:
            assert len(read_run_records()) == 1
            raise RuntimeError("boom")

        mock_engine = MagicMock()
        mock_engine.run = _fake_run
        mock_engine.get_execution_summary.return_value = {}

        with (
            patch("conductor.cli.run.load_config", return_value=mock_config),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
            patch("conductor.cli.run.ProviderRegistry") as mock_registry,
            patch(
                "conductor.cli.run._build_mcp_servers",
                new_callable=AsyncMock,
                return_value=None,
            ),
            pytest.raises(RuntimeError, match="boom"),
        ):
            mock_registry.return_value.__aenter__ = AsyncMock(return_value=mock_registry)
            mock_registry.return_value.__aexit__ = AsyncMock(return_value=None)

            await run_workflow_async(wf_path, {})

        assert read_run_records() == []


class TestRunWorkflowAsyncTerminalRecordWiring:
    """``run_workflow_async`` writes a *terminal* record (MCP server plan
    E2) describing the outcome, on every exit path: clean completion, an
    explicit ``WorkflowTerminated``, and an unexpected exception."""

    async def test_terminal_record_appears_after_clean_run(
        self, tmp_path: Path, fleet_env: Path
    ) -> None:
        from conductor.cli.run import run_workflow_async

        wf_path = _write_workflow(tmp_path)
        mock_config = _mock_config()

        async def _fake_run(inputs: dict[str, Any]) -> dict[str, Any]:
            return {"result": "done"}

        mock_engine = MagicMock()
        mock_engine.run = _fake_run
        mock_engine.get_execution_summary.return_value = {
            "usage": {"total_tokens": 321, "total_cost_usd": 0.12, "unpriced_agent_count": 2}
        }

        with (
            patch("conductor.cli.run.load_config", return_value=mock_config),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
            patch("conductor.cli.run.ProviderRegistry") as mock_registry,
            patch(
                "conductor.cli.run._build_mcp_servers",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            mock_registry.return_value.__aenter__ = AsyncMock(return_value=mock_registry)
            mock_registry.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await run_workflow_async(wf_path, {})

        assert result == {"result": "done"}
        records = read_terminal_records()
        assert len(records) == 1
        record = records[0]
        assert record.status == "success"
        assert record.output == {"result": "done"}
        assert record.error_type is None
        assert record.error_message is None
        assert record.total_tokens == 321
        assert record.total_cost_usd == 0.12
        assert record.unpriced_agent_count == 2
        assert record.workflow_path == str(wf_path)
        assert record.workflow_name == wf_path.stem
        assert Path(record.event_log_path).exists()

        # The live record is gone (already covered above); the terminal
        # one is a permanent artifact this epic is responsible for -- its
        # eventual pruning is `fleet.retention`'s job, not this one's.
        assert read_run_records() == []

    async def test_terminal_record_appears_after_workflow_terminated(
        self, tmp_path: Path, fleet_env: Path
    ) -> None:
        from conductor.cli.run import run_workflow_async

        wf_path = _write_workflow(tmp_path)
        mock_config = _mock_config()

        terminate_exc = WorkflowTerminated(
            "bye now",
            output={"reason_code": "done"},
            reason="bye now",
            terminated_by="term",
        )

        async def _fake_run(inputs: dict[str, Any]) -> dict[str, Any]:
            raise terminate_exc

        mock_engine = MagicMock()
        mock_engine.run = _fake_run
        mock_engine.get_execution_summary.return_value = {}

        with (
            patch("conductor.cli.run.load_config", return_value=mock_config),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
            patch("conductor.cli.run.ProviderRegistry") as mock_registry,
            patch(
                "conductor.cli.run._build_mcp_servers",
                new_callable=AsyncMock,
                return_value=None,
            ),
            pytest.raises(WorkflowTerminated),
        ):
            mock_registry.return_value.__aenter__ = AsyncMock(return_value=mock_registry)
            mock_registry.return_value.__aexit__ = AsyncMock(return_value=None)

            await run_workflow_async(wf_path, {})

        records = read_terminal_records()
        assert len(records) == 1
        record = records[0]
        assert record.status == "failed"
        assert record.error_type == "WorkflowTerminated"
        assert record.error_message == "bye now"
        assert record.output == {"reason_code": "done"}

    async def test_terminal_record_appears_after_unexpected_exception(
        self, tmp_path: Path, fleet_env: Path
    ) -> None:
        from conductor.cli.run import run_workflow_async

        wf_path = _write_workflow(tmp_path)
        mock_config = _mock_config()

        async def _fake_run(inputs: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("boom")

        mock_engine = MagicMock()
        mock_engine.run = _fake_run
        mock_engine.get_execution_summary.return_value = {}

        with (
            patch("conductor.cli.run.load_config", return_value=mock_config),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
            patch("conductor.cli.run.ProviderRegistry") as mock_registry,
            patch(
                "conductor.cli.run._build_mcp_servers",
                new_callable=AsyncMock,
                return_value=None,
            ),
            pytest.raises(RuntimeError, match="boom"),
        ):
            mock_registry.return_value.__aenter__ = AsyncMock(return_value=mock_registry)
            mock_registry.return_value.__aexit__ = AsyncMock(return_value=None)

            await run_workflow_async(wf_path, {})

        records = read_terminal_records()
        assert len(records) == 1
        record = records[0]
        assert record.status == "failed"
        assert record.error_type == "RuntimeError"
        assert record.error_message == "boom"
        # No `output:` was ever rendered on this path.
        assert record.output == {}


# ---------------------------------------------------------------------------
# resume_workflow_async: mirrors run_workflow_async's wiring
# ---------------------------------------------------------------------------


def _write_checkpoint_and_config(tmp_path: Path) -> tuple[Path, Any]:
    """Build a minimal real ``Checkpoint`` (not mocked -- ``resume_workflow_async``
    reconstructs ``WorkflowContext``/``LimitEnforcer`` straight from its
    fields) plus the workflow file it references.

    Writes a (near-empty) real event log file and points the checkpoint's
    ``event_log_path`` at it: ``EventLogSubscriber`` only reuses
    ``existing_run_id`` when it can actually append to an existing,
    readable ``existing_path`` (see ``engine/event_log.py``) -- without a
    real file here, it would silently fall through to a fresh random
    ``run_id``, defeating the "resume replaces, not duplicates" test this
    helper exists for.
    """
    from conductor.engine.checkpoint import CheckpointManager
    from conductor.engine.context import WorkflowContext
    from conductor.engine.limits import LimitEnforcer

    wf_path = _write_workflow(tmp_path)
    context = WorkflowContext()
    limits = LimitEnforcer(max_iterations=50)

    event_log_path = tmp_path / "original.events.jsonl"
    event_log_path.write_text("")

    cp_path = CheckpointManager.save_checkpoint(
        workflow_path=wf_path,
        context=context,
        limits=limits,
        current_agent="a",
        error=RuntimeError("prior failure"),
        inputs={},
        run_id="deadbeef",
        event_log_path=str(event_log_path),
    )
    return wf_path, cp_path


class TestResumeWorkflowAsyncRunRecordWiring:
    """``resume_workflow_async`` writes/replaces a record the same way."""

    async def test_resume_replaces_rather_than_duplicates_the_record(
        self, tmp_path: Path, fleet_env: Path
    ) -> None:
        """Resume reuses the checkpoint's ``run_id`` (T3): writing the
        record again for the same ``run_id`` must replace the prior file,
        never create a second one."""
        from conductor.cli.run import resume_workflow_async
        from conductor.fleet.records import RunRecord, write_run_record

        wf_path, cp_path = _write_checkpoint_and_config(tmp_path)
        mock_config = _mock_config(agent_names=["a"])

        # Simulate a stale/prior record for this same run_id already on
        # disk (e.g. left over from the original failed run, or a previous
        # resume generation) with an obviously-wrong pid so we can prove
        # the resumed process's write overwrote it rather than adding a
        # second file.
        write_run_record(
            RunRecord(
                run_id="deadbeef",
                pid=999999,
                workflow_path=str(wf_path),
                workflow_name="wiring-test",
                started_at="2020-01-01T00:00:00",
                event_log_path="/tmp/old.jsonl",
                port=None,
                mode="fg",
                checkpoint_dir=None,
            )
        )

        seen: dict[str, Any] = {}

        async def _fake_resume(current_agent: str) -> dict[str, Any]:
            records = read_run_records()
            assert len(records) == 1
            seen["record"] = records[0]
            return {"result": "resumed"}

        mock_engine = MagicMock()
        mock_engine.resume = _fake_resume
        mock_engine.set_context = MagicMock()
        mock_engine.set_limits = MagicMock()
        mock_engine.get_execution_summary.return_value = {}

        with (
            patch("conductor.cli.run.load_config", return_value=mock_config),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
            patch("conductor.cli.run.ProviderRegistry") as mock_registry,
            patch(
                "conductor.cli.run._build_mcp_servers",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            mock_registry.return_value.__aenter__ = AsyncMock(return_value=mock_registry)
            mock_registry.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await resume_workflow_async(workflow_path=wf_path)

        assert result == {"result": "resumed"}
        record = seen["record"]
        # Same run_id: replaced, not duplicated.
        assert record.run_id == "deadbeef"
        assert record.pid != 999999
        assert record.mode == "fg"
        assert record.port is None

        # Removed once the resumed run finishes.
        assert read_run_records() == []

    async def test_resume_record_removed_on_workflow_terminated(
        self, tmp_path: Path, fleet_env: Path
    ) -> None:
        from conductor.cli.run import resume_workflow_async

        wf_path, cp_path = _write_checkpoint_and_config(tmp_path)
        mock_config = _mock_config(agent_names=["a"])

        terminate_exc = WorkflowTerminated(
            "bye",
            output={},
            reason="bye",
            terminated_by="term",
        )

        async def _fake_resume(current_agent: str) -> dict[str, Any]:
            assert len(read_run_records()) == 1
            raise terminate_exc

        mock_engine = MagicMock()
        mock_engine.resume = _fake_resume
        mock_engine.set_context = MagicMock()
        mock_engine.set_limits = MagicMock()
        mock_engine.get_execution_summary.return_value = {}

        with (
            patch("conductor.cli.run.load_config", return_value=mock_config),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
            patch("conductor.cli.run.ProviderRegistry") as mock_registry,
            patch(
                "conductor.cli.run._build_mcp_servers",
                new_callable=AsyncMock,
                return_value=None,
            ),
            pytest.raises(WorkflowTerminated),
        ):
            mock_registry.return_value.__aenter__ = AsyncMock(return_value=mock_registry)
            mock_registry.return_value.__aexit__ = AsyncMock(return_value=None)

            await resume_workflow_async(workflow_path=wf_path)

        assert read_run_records() == []

    async def test_resume_record_removed_on_unexpected_exception(
        self, tmp_path: Path, fleet_env: Path
    ) -> None:
        """Mirrors ``TestRunWorkflowAsyncRunRecordWiring``'s equivalent test:
        an unrelated exception raised from ``engine.resume`` must still
        remove the run record via the shared ``finally``."""
        from conductor.cli.run import resume_workflow_async

        wf_path, cp_path = _write_checkpoint_and_config(tmp_path)
        mock_config = _mock_config(agent_names=["a"])

        async def _fake_resume(current_agent: str) -> dict[str, Any]:
            assert len(read_run_records()) == 1
            raise RuntimeError("boom")

        mock_engine = MagicMock()
        mock_engine.resume = _fake_resume
        mock_engine.set_context = MagicMock()
        mock_engine.set_limits = MagicMock()
        mock_engine.get_execution_summary.return_value = {}

        with (
            patch("conductor.cli.run.load_config", return_value=mock_config),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
            patch("conductor.cli.run.ProviderRegistry") as mock_registry,
            patch(
                "conductor.cli.run._build_mcp_servers",
                new_callable=AsyncMock,
                return_value=None,
            ),
            pytest.raises(RuntimeError, match="boom"),
        ):
            mock_registry.return_value.__aenter__ = AsyncMock(return_value=mock_registry)
            mock_registry.return_value.__aexit__ = AsyncMock(return_value=None)

            await resume_workflow_async(workflow_path=wf_path)

        assert read_run_records() == []

    async def test_resume_fg_web_record_carries_dashboard_port(
        self, tmp_path: Path, fleet_env: Path
    ) -> None:
        """``resume --web`` (without ``--web-bg``) mirrors
        ``run_workflow_async``'s fg-web case: the record carries the
        dashboard's actual resolved port and ``mode == "fg-web"``. Like the
        ``run_workflow_async`` counterpart, the post-resume dashboard-keep-
        alive wait is driven via a cancellable task rather than awaited
        directly."""
        from conductor.cli.run import resume_workflow_async

        wf_path, cp_path = _write_checkpoint_and_config(tmp_path)
        mock_config = _mock_config(agent_names=["a"])
        seen: dict[str, Any] = {}
        dashboard = _mock_dashboard(port=8223)

        async def _fake_resume(current_agent: str) -> dict[str, Any]:
            records = read_run_records()
            assert len(records) == 1
            seen["record"] = records[0]
            return {"result": "resumed"}

        mock_engine = MagicMock()
        mock_engine.resume = _fake_resume
        mock_engine.set_context = MagicMock()
        mock_engine.set_limits = MagicMock()
        mock_engine.get_execution_summary.return_value = {}
        mock_engine.clear_web_dashboard = MagicMock()
        mock_engine.build_workflow_started_data = AsyncMock(return_value={})
        mock_engine.suppress_workflow_started_emit = MagicMock()

        mock_web_module = MagicMock()
        mock_web_module.WebDashboard.return_value = dashboard

        import sys

        with (
            patch("conductor.cli.run.load_config", return_value=mock_config),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
            patch("conductor.cli.run.ProviderRegistry") as mock_registry,
            patch(
                "conductor.cli.run._build_mcp_servers",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.dict(sys.modules, {"conductor.web.server": mock_web_module}),
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = False
            mock_registry.return_value.__aenter__ = AsyncMock(return_value=mock_registry)
            mock_registry.return_value.__aexit__ = AsyncMock(return_value=None)

            task = asyncio.ensure_future(
                resume_workflow_async(workflow_path=wf_path, web=True, no_interactive=True)
            )
            for _ in range(200):
                if "record" in seen:
                    break
                await asyncio.sleep(0.01)
            assert "record" in seen, "engine.resume side effect never ran"
            # See the matching comment in
            # ``test_fg_web_record_carries_dashboard_port`` -- give the task
            # a few more turns to unwind past the quick, non-blocking awaits
            # between the mocked ``engine.resume`` returning and the final
            # "keep dashboard open" wait before cancelling it.
            for _ in range(20):
                await asyncio.sleep(0)
            task.cancel()
            result = await task

        assert result == {"result": "resumed"}
        record = seen["record"]
        assert record.mode == "fg-web"
        assert record.port == 8223
        assert read_run_records() == []

    async def test_resume_bg_record_has_mode_bg(self, tmp_path: Path, fleet_env: Path) -> None:
        """``resume --web-bg`` (or the ``CONDUCTOR_WEB_BG`` env var set on a
        detached child) always records ``mode="bg"``."""
        from conductor.cli.run import resume_workflow_async

        wf_path, cp_path = _write_checkpoint_and_config(tmp_path)
        mock_config = _mock_config(agent_names=["a"])
        seen: dict[str, Any] = {}
        dashboard = _mock_dashboard(port=8224)

        async def _fake_resume(current_agent: str) -> dict[str, Any]:
            records = read_run_records()
            assert len(records) == 1
            seen["record"] = records[0]
            return {"result": "resumed"}

        mock_engine = MagicMock()
        mock_engine.resume = _fake_resume
        mock_engine.set_context = MagicMock()
        mock_engine.set_limits = MagicMock()
        mock_engine.get_execution_summary.return_value = {}
        mock_engine.clear_web_dashboard = MagicMock()
        mock_engine.build_workflow_started_data = AsyncMock(return_value={})
        mock_engine.suppress_workflow_started_emit = MagicMock()

        mock_web_module = MagicMock()
        mock_web_module.WebDashboard.return_value = dashboard

        import sys

        with (
            patch("conductor.cli.run.load_config", return_value=mock_config),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
            patch("conductor.cli.run.ProviderRegistry") as mock_registry,
            patch(
                "conductor.cli.run._build_mcp_servers",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.dict(sys.modules, {"conductor.web.server": mock_web_module}),
        ):
            mock_registry.return_value.__aenter__ = AsyncMock(return_value=mock_registry)
            mock_registry.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await resume_workflow_async(
                workflow_path=wf_path, web=True, web_bg=True, no_interactive=True
            )

        assert result == {"result": "resumed"}
        record = seen["record"]
        assert record.mode == "bg"
        assert record.port == 8224
        assert read_run_records() == []


# ---------------------------------------------------------------------------
# resume_workflow_async: terminal record replaces rather than duplicates
# ---------------------------------------------------------------------------


class TestResumeWorkflowAsyncTerminalRecordWiring:
    """``resume_workflow_async`` writes a terminal record too, replacing a
    prior generation's for the same ``run_id`` rather than duplicating it
    (MCP server plan E2 — mirrors the live-record replacement behavior
    already covered by ``TestResumeWorkflowAsyncRunRecordWiring``)."""

    async def test_resume_terminal_record_replaces_rather_than_duplicates(
        self, tmp_path: Path, fleet_env: Path
    ) -> None:
        from conductor.cli.run import resume_workflow_async
        from conductor.fleet.records import TerminalRunRecord, write_terminal_record

        wf_path, cp_path = _write_checkpoint_and_config(tmp_path)
        mock_config = _mock_config(agent_names=["a"])

        # A stale terminal record for this same run_id, e.g. left over from
        # an earlier resume generation that also eventually failed.
        write_terminal_record(
            TerminalRunRecord(
                run_id="deadbeef",
                workflow_path=str(wf_path),
                workflow_name="wf",
                started_at="2020-01-01T00:00:00+00:00",
                ended_at="2020-01-01T00:05:00+00:00",
                status="failed",
                output={},
                error_type="RuntimeError",
                error_message="prior failure",
                total_tokens=None,
                total_cost_usd=None,
                unpriced_agent_count=0,
                event_log_path="/tmp/old.jsonl",
                bg_stderr_log=None,
                bg_stdout_log=None,
            )
        )

        async def _fake_resume(current_agent: str) -> dict[str, Any]:
            return {"result": "resumed"}

        mock_engine = MagicMock()
        mock_engine.resume = _fake_resume
        mock_engine.set_context = MagicMock()
        mock_engine.set_limits = MagicMock()
        mock_engine.get_execution_summary.return_value = {
            "usage": {"total_tokens": 55, "total_cost_usd": 0.01, "unpriced_agent_count": 0}
        }

        with (
            patch("conductor.cli.run.load_config", return_value=mock_config),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
            patch("conductor.cli.run.ProviderRegistry") as mock_registry,
            patch(
                "conductor.cli.run._build_mcp_servers",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            mock_registry.return_value.__aenter__ = AsyncMock(return_value=mock_registry)
            mock_registry.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await resume_workflow_async(workflow_path=wf_path)

        assert result == {"result": "resumed"}
        records = read_terminal_records()
        # Replaced, not duplicated: still exactly one terminal record for
        # this run_id, reflecting the new (successful) outcome.
        assert len(records) == 1
        record = records[0]
        assert record.run_id == "deadbeef"
        assert record.status == "success"
        assert record.output == {"result": "resumed"}
        assert record.error_type is None
        assert record.total_tokens == 55


# ---------------------------------------------------------------------------
# The documented boundary: a kill -9-style exit leaves no terminal record
# ---------------------------------------------------------------------------


class TestKillStyleExitLeavesNoTerminalRecord:
    """Documents and asserts the limitation called out in the MCP server
    design's *Key Components → 4*: the terminal record is written in
    ``cli/run.py``'s ``finally`` block, so a process that is ``kill -9``'d
    (or otherwise dies before that ``finally`` runs) leaves no terminal
    record behind — only the live record, later prunable as any dead-``pid``
    record already is.

    A real ``SIGKILL`` cannot be observed by any Python-level exception
    handler, so the only faithful way to exercise this is to actually fork
    a child process, let it reach the point of writing its own *live* run
    record (proving it genuinely started), and kill it with ``SIGKILL``
    from the parent — a synchronous (non-``asyncio``) test, since forking
    from inside a running event loop is unsafe.
    """

    @pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
    @pytest.mark.skipif(os.name == "nt", reason="os.fork()/SIGKILL is POSIX-only")
    def test_kill_minus_9_leaves_no_terminal_record(
        self, tmp_path: Path, fleet_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wf_path = _write_workflow(tmp_path)
        mock_config = _mock_config()
        # Pin the run_id so the parent can look it up after the kill,
        # mirroring how `cli.bg_runner` propagates a chosen run_id to a
        # detached child (see `engine/event_log.py`).
        monkeypatch.setenv("CONDUCTOR_RUN_ID", "deadbeef")

        async def _fake_run(inputs: dict[str, Any]) -> dict[str, Any]:
            # Blocks forever: the parent SIGKILLs this process before this
            # coroutine -- and therefore `run_workflow_async`'s `finally`,
            # where the terminal record would be written -- ever resumes.
            await asyncio.Event().wait()
            return {"result": "unreachable"}  # pragma: no cover

        mock_engine = MagicMock()
        mock_engine.run = _fake_run
        mock_engine.get_execution_summary.return_value = {}

        pid: int | None = None
        try:
            with (
                patch("conductor.cli.run.load_config", return_value=mock_config),
                patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
                patch("conductor.cli.run.ProviderRegistry") as mock_registry,
                patch(
                    "conductor.cli.run._build_mcp_servers",
                    new_callable=AsyncMock,
                    return_value=None,
                ),
            ):
                mock_registry.return_value.__aenter__ = AsyncMock(return_value=mock_registry)
                mock_registry.return_value.__aexit__ = AsyncMock(return_value=None)

                pid = os.fork()
                if pid == 0:
                    # Child process: run the (mocked) workflow, which
                    # blocks forever in `_fake_run` above. `os._exit` (not
                    # `sys.exit`) below skips the `with` block's own
                    # `__exit__`/unpatching and any other Python-level
                    # cleanup -- this branch is expected to be reached
                    # only if `asyncio.run` unexpectedly returns/raises,
                    # since the parent kills this process first.
                    from conductor.cli.run import run_workflow_async

                    try:
                        asyncio.run(run_workflow_async(wf_path, {}))
                    finally:
                        os._exit(1)

            # Parent: wait (bounded) for the child to reach the point of
            # writing its own live run record -- proving it is genuinely
            # mid-execution -- before killing it.
            deadline = time.time() + 10
            child_started = False
            while time.time() < deadline:
                if any(r.run_id == "deadbeef" for r in read_run_records()):
                    child_started = True
                    break
                time.sleep(0.02)

            assert pid is not None
            os.kill(pid, signal.SIGKILL)
            _, status = os.waitpid(pid, 0)

            assert child_started, "forked child never wrote its live run record"
            assert os.WIFSIGNALED(status)
            assert os.WTERMSIG(status) == signal.SIGKILL
        finally:
            # Best-effort: make sure no child is left behind even if an
            # assertion above failed before the kill/reap. The child may
            # already have been killed and reaped by the code above, in
            # which case a second kill/waitpid legitimately raises either
            # of these.
            if pid:
                with contextlib.suppress(ProcessLookupError, ChildProcessError):
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)

        # The one artifact this epic adds is written only in `finally` --
        # which a real SIGKILL never reaches -- so no terminal record
        # exists for this run, even though it was genuinely mid-execution.
        assert read_terminal_record("deadbeef") is None
