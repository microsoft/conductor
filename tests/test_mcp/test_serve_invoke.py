"""Tests for the invocation layer: always-detached launches, `_wait_seconds`
resolution and bounding, the DD11 never-skip-gates guarantee, the E9-T3
metadata stamp, gate short-circuiting, progress notifications, and the R3
`--max-concurrent-runs` cap (FR4, FR5, G3, G4, DD2, DD11, R3, E9-T8).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from conductor.cli.bg_runner import BackgroundLaunch
from conductor.fleet.launch import LaunchError
from conductor.fleet.records import RunRecord, write_run_record
from conductor.fleet.summary import GateInfo, RunSummary
from conductor.mcp.serve.catalogue import build_catalogue
from conductor.mcp.serve.invoke import (
    ConcurrentRunLimitError,
    LaunchTracker,
    UnknownToolError,
    _await_terminal_or_gate,
    invoke_workflow_tool,
    resolve_wait_seconds,
)
from conductor.mcp.serve.options import ServeOptions
from conductor.registry.config import RegistriesConfig
from tests.test_mcp.conftest import write_path_registry

_REVIEW_PR_YAML = """\
workflow:
  name: review-pr
  description: Reviews a pull request.
  entry_point: worker
  input:
    pr_number:
      type: number
      required: true
    depth:
      type: string
      default: standard
  mcp:
    mode: async
agents:
  - name: worker
    prompt: "Review PR {{ pr_number }}"
    output:
      result:
        type: string
output:
  result: "{{ worker.output.result }}"
"""

_SYNC_JOB_YAML = """\
workflow:
  name: sync-job
  description: A workflow that defaults to synchronous invocation.
  entry_point: worker
  input:
    topic:
      type: string
      required: true
  mcp:
    mode: sync
agents:
  - name: worker
    prompt: "Work on {{ topic }}"
    output:
      result:
        type: string
output:
  result: "{{ worker.output.result }}"
"""


def _build_catalogue(tmp_path: Path) -> tuple[RegistriesConfig, Any]:
    entry = write_path_registry(
        tmp_path,
        name="official",
        workflows={"review-pr": _REVIEW_PR_YAML, "sync-job": _SYNC_JOB_YAML},
    )
    registries_config = RegistriesConfig(registries={"official": entry})
    catalogue = build_catalogue(ServeOptions(), registries_config=registries_config)
    return registries_config, catalogue


def _make_fake_launch_background(calls: list[dict[str, Any]]) -> Any:
    """A ``launch_background`` stand-in that records its kwargs and writes a
    real, discoverable :class:`RunRecord` -- mirroring the real function's
    D2 guarantee that a successful return means the run is already
    discoverable via ``read_run_record()`` before it returns.
    """

    def _fake(
        *,
        workflow_path: Path,
        inputs: dict[str, Any],
        provider_override: str | None = None,
        skip_gates: bool = False,
        web_port: int = 0,
        metadata: dict[str, str] | None = None,
        **_ignored: Any,
    ) -> BackgroundLaunch:
        calls.append(
            {
                "workflow_path": workflow_path,
                "inputs": inputs,
                "provider_override": provider_override,
                "skip_gates": skip_gates,
                "web_port": web_port,
                "metadata": metadata,
            }
        )
        run_id = f"run{len(calls):05d}"
        port = 9000 + len(calls)
        write_run_record(
            RunRecord(
                run_id=run_id,
                pid=os.getpid(),
                workflow_path=str(workflow_path),
                workflow_name=workflow_path.stem,
                started_at="2026-01-01T00:00:00+00:00",
                event_log_path="",
                port=port,
                mode="bg",
                checkpoint_dir=None,
            )
        )
        return BackgroundLaunch(
            url=f"http://127.0.0.1:{port}",
            stderr_log=Path(f"/tmp/conductor-x-{run_id}.bg.stderr.log"),
            stdout_log=Path(f"/tmp/conductor-x-{run_id}.bg.stdout.log"),
            run_id=run_id,
            workflow_started=True,
            still_running=True,
        )

    return _fake


# ---------------------------------------------------------------------------
# E9-T1: dispatch / unknown tool
# ---------------------------------------------------------------------------


class TestDispatch:
    async def test_unknown_tool_raises_instructive_error(self, conductor_home: Path) -> None:
        _, catalogue = _build_catalogue(conductor_home)
        with pytest.raises(UnknownToolError, match="Unknown tool"):
            await invoke_workflow_tool(
                "not_a_real_tool",
                {},
                catalogue=catalogue,
                options=ServeOptions(),
                tracker=LaunchTracker(),
            )

    async def test_missing_required_input_raises_launch_error(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registries_config, catalogue = _build_catalogue(conductor_home)
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "conductor.mcp.serve.invoke.launch_background", _make_fake_launch_background(calls)
        )
        with pytest.raises(LaunchError, match="pr_number"):
            await invoke_workflow_tool(
                "review_pr",
                {},
                catalogue=catalogue,
                options=ServeOptions(),
                tracker=LaunchTracker(),
                registries_config=registries_config,
            )
        assert calls == []


# ---------------------------------------------------------------------------
# G3/DD2/DD11/G4/E9-T3: always detached, never skip_gates, handle shape
# ---------------------------------------------------------------------------


class TestAlwaysDetached:
    async def test_detached_with_identical_launch_args_for_immediate_and_bounded_wait(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The run is detached in *every* mode: `launch_background` is
        called with the same kwargs whether `_wait_seconds` is 0 or a
        large bounded value (G3/DD2) -- `_wait_seconds` only changes
        whether *this call* waits, never whether the run itself runs
        inside this process."""
        registries_config, catalogue = _build_catalogue(conductor_home)
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "conductor.mcp.serve.invoke.launch_background", _make_fake_launch_background(calls)
        )
        # A completed status on the very first poll means the `_wait_seconds:
        # 120` call returns immediately too, without a real sleep.
        monkeypatch.setattr(
            "conductor.mcp.serve.invoke.derive_run_summary",
            lambda record: _summary(record.run_id, status="completed"),
        )

        for wait_seconds in (0, 120):
            content, structured = await invoke_workflow_tool(
                "review_pr",
                {"pr_number": 7, "_wait_seconds": wait_seconds},
                catalogue=catalogue,
                options=ServeOptions(),
                tracker=LaunchTracker(),
                registries_config=registries_config,
            )
            assert content and structured

        assert len(calls) == 2
        for call in calls:
            assert call["skip_gates"] is False
            assert call["web_port"] == 0
            assert call["provider_override"] is None
            assert call["metadata"] == {
                "conductor_mcp_server": "true",
                "conductor_mcp_tool": "review_pr",
            }
            assert call["inputs"] == {"pr_number": 7, "depth": "standard"}

    async def test_result_always_carries_a_dashboard_url(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G4: every invocation returns a dashboard URL, in every mode."""
        registries_config, catalogue = _build_catalogue(conductor_home)
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "conductor.mcp.serve.invoke.launch_background", _make_fake_launch_background(calls)
        )

        _, structured = await invoke_workflow_tool(
            "review_pr",
            {"pr_number": 1, "_wait_seconds": 0},
            catalogue=catalogue,
            options=ServeOptions(),
            tracker=LaunchTracker(),
            registries_config=registries_config,
        )
        assert structured["url"].startswith("http://127.0.0.1:")
        assert structured["status"] == "running"
        assert structured["workflow"] == {
            "name": "review-pr",
            "registry": "official",
            "pinned": structured["workflow"]["pinned"],
        }
        assert structured["workflow"]["pinned"].startswith("hash:")

    async def test_handle_carries_the_port_and_observation_commands(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A caller reporting a background run to a human quotes the port
        and a terminal command far more often than the whole URL, so both
        are first-class handle fields rather than something the caller has
        to parse back out of ``url``."""
        registries_config, catalogue = _build_catalogue(conductor_home)
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "conductor.mcp.serve.invoke.launch_background", _make_fake_launch_background(calls)
        )

        content, structured = await invoke_workflow_tool(
            "review_pr",
            {"pr_number": 1, "_wait_seconds": 0},
            catalogue=catalogue,
            options=ServeOptions(),
            tracker=LaunchTracker(),
            registries_config=registries_config,
        )

        port = structured["port"]
        assert isinstance(port, int)
        assert structured["url"].endswith(f":{port}")
        assert structured["observe"]["dashboard"] == structured["url"]
        assert structured["observe"]["fleet"] == "conductor fleet"
        assert structured["observe"]["status"] == "conductor status"
        assert structured["observe"]["stop"] == f"conductor stop --port {port}"
        assert set(structured["logs"]) == {"stderr", "stdout"}
        # The human-readable block names the port and the watch command too,
        # since that is the text a calling agent echoes back verbatim.
        text = content[0].text  # type: ignore[union-attr]
        assert f"port {port}" in text
        assert "conductor fleet" in text
        assert "background" in text

    async def test_immediate_next_action_does_not_lead_with_a_blocking_call(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard: the handle used to open with "call
        conductor_await_run(...)", which reads as an instruction to block
        on a run that is already detached. The blocking option is still
        offered, but only after the non-blocking ones and explicitly
        conditioned on the user having asked for it."""
        registries_config, catalogue = _build_catalogue(conductor_home)
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "conductor.mcp.serve.invoke.launch_background", _make_fake_launch_background(calls)
        )

        _, structured = await invoke_workflow_tool(
            "review_pr",
            {"pr_number": 1, "_wait_seconds": 0},
            catalogue=catalogue,
            options=ServeOptions(),
            tracker=LaunchTracker(),
            registries_config=registries_config,
        )

        next_action = structured["next"]
        assert not next_action.startswith("Call conductor_await_run")
        assert next_action.index("conductor fleet") < next_action.index("conductor_await_run")
        assert "only if the user asked you to block" in next_action

    async def test_every_observe_command_actually_exists_in_the_cli(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``observe`` block is handed to a human to paste into a
        terminal, so an invented-but-plausible command is worse than no
        entry at all -- it fails in front of the user, at the exact moment
        they were told how to watch their run.

        This resolves each command against the real Typer app rather than a
        hardcoded list, so a command that is later renamed or removed fails
        here instead of in someone's shell. (``conductor fleet list --json``
        shipped in a draft of this block and does not exist.)
        """
        import shlex

        import typer

        from conductor.cli.app import app

        registries_config, catalogue = _build_catalogue(conductor_home)
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "conductor.mcp.serve.invoke.launch_background", _make_fake_launch_background(calls)
        )

        _, structured = await invoke_workflow_tool(
            "review_pr",
            {"pr_number": 1, "_wait_seconds": 0},
            catalogue=catalogue,
            options=ServeOptions(),
            tracker=LaunchTracker(),
            registries_config=registries_config,
        )

        root = typer.main.get_command(app)
        checked = 0
        for key, value in structured["observe"].items():
            if key == "dashboard":
                continue
            tokens = shlex.split(value)
            assert tokens[0] == "conductor", f"{key!r} is not a conductor command: {value!r}"
            command: Any = root
            expect_value = False
            for token in tokens[1:]:
                if token.startswith("-"):
                    # An option: it must belong to the command resolved so far.
                    names = {opt for param in command.params for opt in param.opts}
                    assert token in names, f"{key!r}: {token!r} is not an option of {value!r}"
                    expect_value = True
                    continue
                if expect_value:
                    # The value of the option we just validated.
                    expect_value = False
                    continue
                lookup = getattr(command, "get_command", None)
                resolved = lookup(None, token) if lookup is not None else None
                assert resolved is not None, (
                    f"{key!r}: {token!r} is not a subcommand of "
                    f"{' '.join(tokens[: tokens.index(token)])!r} in {value!r}"
                )
                command = resolved
            checked += 1
        assert checked >= 3, "observe block shrank -- this guard is no longer covering it"


# ---------------------------------------------------------------------------
# FR5/E9-T4: _wait_seconds resolution (pure function, all four cases)
# ---------------------------------------------------------------------------


class TestResolveWaitSeconds:
    def test_zero_returns_immediately(self) -> None:
        assert resolve_wait_seconds(0, mcp_mode="async", max_wait_seconds=300) == 0.0

    def test_negative_treated_as_zero(self) -> None:
        assert resolve_wait_seconds(-5, mcp_mode="async", max_wait_seconds=300) == 0.0

    def test_positive_value_under_ceiling_is_honored(self) -> None:
        assert resolve_wait_seconds(30, mcp_mode="async", max_wait_seconds=300) == 30.0

    def test_ceiling_caps_an_over_large_request(self) -> None:
        assert resolve_wait_seconds(99_999, mcp_mode="async", max_wait_seconds=300) == 300.0

    def test_omitted_defers_to_async_mode_returns_immediately(self) -> None:
        assert resolve_wait_seconds(None, mcp_mode="async", max_wait_seconds=300) == 0.0

    def test_omitted_defers_to_sync_mode_resolves_to_ceiling(self) -> None:
        assert resolve_wait_seconds(None, mcp_mode="sync", max_wait_seconds=300) == 300.0

    def test_omitted_defers_to_auto_mode_returns_immediately(self) -> None:
        assert resolve_wait_seconds(None, mcp_mode="auto", max_wait_seconds=300) == 0.0

    def test_zero_ceiling_forces_every_call_non_blocking(self) -> None:
        """``--max-wait-seconds 0`` is the operator's hard guarantee that no
        tool call ever blocks, regardless of what a caller requests or what
        a workflow declares -- the ceiling applies to *every* blocking path,
        including the ``mode: sync`` branch that would otherwise resolve to
        it."""
        assert resolve_wait_seconds(300, mcp_mode="async", max_wait_seconds=0) == 0.0
        assert resolve_wait_seconds(None, mcp_mode="sync", max_wait_seconds=0) == 0.0
        assert resolve_wait_seconds(99_999, mcp_mode="sync", max_wait_seconds=0) == 0.0


# ---------------------------------------------------------------------------
# DD2/E9-T4/T5: the bounded poll loop -- gate short-circuit, deadline, progress
# ---------------------------------------------------------------------------


def _summary(run_id: str, *, status: str, gate: GateInfo | None = None) -> RunSummary:
    return RunSummary(
        run_id=run_id,
        workflow_name="review-pr",
        mode="bg",
        port=9001,
        started_at="2026-01-01T00:00:00+00:00",
        status=status,  # type: ignore[arg-type]
        current_step="worker",
        current_step_type="agent",
        current_step_started_at=time.time(),
        total_tokens=0,
        total_cost_usd=None,
        unpriced_agent_count=0,
        gate=gate,
        gate_resolvable=True,
        topology=None,
    )


class TestBoundedWait:
    async def test_reaching_a_gate_ends_the_wait_early(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_run_record(
            RunRecord(
                run_id="atgate01",
                pid=os.getpid(),
                workflow_path="wf.yaml",
                workflow_name="wf",
                started_at="2026-01-01T00:00:00+00:00",
                event_log_path="",
                port=9010,
                mode="bg",
                checkpoint_dir=None,
            )
        )
        gate = GateInfo(
            agent_name="worker", prompt="Approve?", options=["yes", "no"], option_details=[]
        )
        monkeypatch.setattr(
            "conductor.mcp.serve.invoke.derive_run_summary",
            lambda record: _summary(record.run_id, status="at-gate", gate=gate),
        )

        start = time.monotonic()
        summary, terminal = await _await_terminal_or_gate(
            "atgate01",
            deadline=time.monotonic() + 100.0,  # would hang for 100s if not short-circuited
            progress_token=None,
            send_progress=None,
        )
        elapsed = time.monotonic() - start

        assert elapsed < 2.0
        assert terminal is None
        assert summary is not None
        assert summary.status == "at-gate"
        assert summary.gate is not None
        assert summary.gate.agent_name == "worker"

    async def test_deadline_reached_while_still_running(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_run_record(
            RunRecord(
                run_id="stillrun",
                pid=os.getpid(),
                workflow_path="wf.yaml",
                workflow_name="wf",
                started_at="2026-01-01T00:00:00+00:00",
                event_log_path="",
                port=9011,
                mode="bg",
                checkpoint_dir=None,
            )
        )
        monkeypatch.setattr(
            "conductor.mcp.serve.invoke.derive_run_summary",
            lambda record: _summary(record.run_id, status="running"),
        )

        start = time.monotonic()
        summary, terminal = await _await_terminal_or_gate(
            "stillrun",
            deadline=time.monotonic() + 0.1,
            progress_token=None,
            send_progress=None,
        )
        elapsed = time.monotonic() - start

        assert elapsed < 2.0
        assert terminal is None
        assert summary is not None
        assert summary.status == "running"

    async def test_progress_emitted_only_when_token_supplied(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_run_record(
            RunRecord(
                run_id="progress1",
                pid=os.getpid(),
                workflow_path="wf.yaml",
                workflow_name="wf",
                started_at="2026-01-01T00:00:00+00:00",
                event_log_path="",
                port=9012,
                mode="bg",
                checkpoint_dir=None,
            )
        )
        monkeypatch.setattr(
            "conductor.mcp.serve.invoke.derive_run_summary",
            lambda record: _summary(record.run_id, status="running"),
        )

        progress_calls: list[tuple[Any, ...]] = []

        async def _record_progress(
            token: Any, progress: float, total: float | None, message: str | None
        ) -> None:
            progress_calls.append((token, progress, total, message))

        # With a token + sender: progress is emitted.
        await _await_terminal_or_gate(
            "progress1",
            deadline=time.monotonic() + 0.1,
            progress_token="tok-1",
            send_progress=_record_progress,
        )
        assert len(progress_calls) >= 1
        assert progress_calls[0][0] == "tok-1"

        # Without a token: silently skipped, even with a sender available.
        progress_calls.clear()
        await _await_terminal_or_gate(
            "progress1",
            deadline=time.monotonic() + 0.1,
            progress_token=None,
            send_progress=_record_progress,
        )
        assert progress_calls == []


# ---------------------------------------------------------------------------
# R3: --max-concurrent-runs
# ---------------------------------------------------------------------------


class TestConcurrencyCap:
    async def test_default_unbounded_never_rejects(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registries_config, catalogue = _build_catalogue(conductor_home)
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "conductor.mcp.serve.invoke.launch_background", _make_fake_launch_background(calls)
        )
        tracker = LaunchTracker()
        for _ in range(5):
            await invoke_workflow_tool(
                "review_pr",
                {"pr_number": 1, "_wait_seconds": 0},
                catalogue=catalogue,
                options=ServeOptions(),  # max_concurrent_runs=0 (default) = unbounded
                tracker=tracker,
                registries_config=registries_config,
            )
        assert len(calls) == 5

    async def test_rejects_at_the_cap_with_an_instructive_message_and_nothing_forked(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registries_config, catalogue = _build_catalogue(conductor_home)
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "conductor.mcp.serve.invoke.launch_background", _make_fake_launch_background(calls)
        )
        tracker = LaunchTracker()
        options = ServeOptions(max_concurrent_runs=1)

        await invoke_workflow_tool(
            "review_pr",
            {"pr_number": 1, "_wait_seconds": 0},
            catalogue=catalogue,
            options=options,
            tracker=tracker,
            registries_config=registries_config,
        )
        assert len(calls) == 1

        with pytest.raises(ConcurrentRunLimitError, match="max-concurrent-runs=1"):
            await invoke_workflow_tool(
                "review_pr",
                {"pr_number": 2, "_wait_seconds": 0},
                catalogue=catalogue,
                options=options,
                tracker=tracker,
                registries_config=registries_config,
            )
        # Nothing new was forked for the rejected call.
        assert len(calls) == 1

    async def test_a_run_that_has_since_exited_frees_a_slot(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registries_config, catalogue = _build_catalogue(conductor_home)
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "conductor.mcp.serve.invoke.launch_background", _make_fake_launch_background(calls)
        )
        tracker = LaunchTracker()
        # Tracked, but no run record was ever written for it -- simulating a
        # run that has already exited (and whose record was removed).
        tracker.register("ghost0001")
        options = ServeOptions(max_concurrent_runs=1)

        await invoke_workflow_tool(
            "review_pr",
            {"pr_number": 1, "_wait_seconds": 0},
            catalogue=catalogue,
            options=options,
            tracker=tracker,
            registries_config=registries_config,
        )
        assert len(calls) == 1

    async def test_a_live_run_this_server_did_not_launch_does_not_count(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registries_config, catalogue = _build_catalogue(conductor_home)
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "conductor.mcp.serve.invoke.launch_background", _make_fake_launch_background(calls)
        )
        # A live run record exists (as if a human ran `conductor run` by
        # hand), but this server never launched it, so it must not count
        # against this tracker's cap.
        write_run_record(
            RunRecord(
                run_id="humanrun",
                pid=os.getpid(),
                workflow_path="wf.yaml",
                workflow_name="wf",
                started_at="2026-01-01T00:00:00+00:00",
                event_log_path="",
                port=9099,
                mode="bg",
                checkpoint_dir=None,
            )
        )
        tracker = LaunchTracker()
        options = ServeOptions(max_concurrent_runs=1)

        await invoke_workflow_tool(
            "review_pr",
            {"pr_number": 1, "_wait_seconds": 0},
            catalogue=catalogue,
            options=options,
            tracker=tracker,
            registries_config=registries_config,
        )
        assert len(calls) == 1


class TestLaunchTracker:
    def test_live_count_prunes_run_ids_that_are_no_longer_alive(self, conductor_home: Path) -> None:
        write_run_record(
            RunRecord(
                run_id="alive001",
                pid=os.getpid(),
                workflow_path="wf.yaml",
                workflow_name="wf",
                started_at="2026-01-01T00:00:00+00:00",
                event_log_path="",
                port=9100,
                mode="bg",
                checkpoint_dir=None,
            )
        )
        tracker = LaunchTracker()
        tracker.register("alive001")
        tracker.register("gone0001")  # never written -- simulates "already exited"

        assert tracker.live_count() == 1
        assert tracker.launched_run_ids == {"alive001"}
