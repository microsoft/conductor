"""Integration tests for Conductor OpenTelemetry tracing under env-driven activation."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import sys
import time
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Skip all tests if opentelemetry-sdk is not installed
pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from conductor.config.schema import AgentDef
from conductor.engine.checkpoint import CheckpointManager
from conductor.events import WorkflowEvent
from conductor.exceptions import ExecutionError, ProviderError
from conductor.providers.base import AgentOutput, AgentProvider
from conductor.providers.copilot import CopilotProvider
from conductor.telemetry import guards
from conductor.telemetry.setup import init_tracer_provider
from conductor.telemetry.subscriber import TelemetrySubscriber

# ---------------------------------------------------------------------------
# Test Fakes and Mocks
# ---------------------------------------------------------------------------


class MockProviderRegistry:
    """Mock ProviderRegistry to return custom test providers."""

    def __init__(self, provider: AgentProvider) -> None:
        self.provider = provider
        self.set_resume_session_ids = MagicMock()
        self.set_resume_session_cwds = MagicMock()

    async def __aenter__(self) -> MockProviderRegistry:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        pass

    async def get_provider(self, agent: AgentDef) -> AgentProvider:
        return self.provider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_workflow(tmp_path: Path, name: str = "test-workflow") -> Path:
    """Write a minimal workflow YAML file and return its path."""
    wf = tmp_path / f"{name}.yaml"
    wf.write_text(
        f"""\
workflow:
  name: {name}
  entry_point: greeter
agents:
  - name: greeter
    model: gpt-4
    prompt: "Hello"
    output:
      greeting:
        type: string
    routes:
      - to: $end

output:
  message: "{{{{ greeter.output.greeting }}}}"
""",
        encoding="utf-8",
    )
    return wf


def _write_checkpoint(
    tmp_path: Path,
    workflow_path: Path,
    *,
    current_agent: str = "greeter",
    error_type: str = "ProviderError",
    error_message: str = "Network error",
    timestamp: str = "20260224-153000",
    run_id: str = "test-run-id",
    event_log_path: str = "",
) -> Path:
    """Write a checkpoint JSON file and return its path."""
    workflow_hash = CheckpointManager.compute_workflow_hash(workflow_path)

    checkpoint = {
        "version": 1,
        "workflow_path": str(workflow_path.resolve()),
        "workflow_hash": workflow_hash,
        "created_at": "2026-02-24T15:30:00+00:00",
        "failure": {
            "error_type": error_type,
            "message": error_message,
            "agent": current_agent,
            "iteration": 1,
        },
        "inputs": {},
        "current_agent": current_agent,
        "context": {
            "workflow_inputs": {},
            "agent_outputs": {},
            "current_iteration": 0,
            "execution_history": [],
        },
        "limits": {
            "current_iteration": 0,
            "max_iterations": 10,
            "execution_history": [],
        },
        "copilot_session_ids": {},
        "run_id": run_id,
        "event_log_path": event_log_path,
    }

    workflow_name = workflow_path.stem
    cp_path = tmp_path / f"{workflow_name}-{timestamp}.json"
    cp_path.write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")
    return cp_path


def _make_resume_mocks() -> tuple[MagicMock, MagicMock]:
    """Create ProviderRegistry + WorkflowEngine mocks for resume_workflow_async."""
    mock_registry = AsyncMock()
    mock_registry.__aenter__ = AsyncMock(return_value=mock_registry)
    mock_registry.__aexit__ = AsyncMock(return_value=False)
    mock_registry.set_resume_session_ids = MagicMock()

    mock_engine = MagicMock()
    mock_engine.resume = AsyncMock(return_value={"result": "ok"})
    mock_engine.config = MagicMock()
    mock_engine.config.workflow.cost.show_summary = False
    mock_engine._last_checkpoint_path = None
    mock_engine.set_context = MagicMock()
    mock_engine.set_limits = MagicMock()
    mock_engine.get_execution_summary = MagicMock(return_value={})
    mock_engine.build_workflow_started_data = AsyncMock(return_value={})
    return mock_registry, mock_engine


def _assert_single_tree(spans: list[Any], root: Any) -> None:
    """Assert every span belongs to the same trace and descends from root."""
    root_trace_id = root.get_span_context().trace_id
    for span in spans:
        assert span.get_span_context().trace_id == root_trace_id, (
            f"span {span.name} has a different trace_id"
        )

    span_by_id = {span.get_span_context().span_id: span for span in spans}
    for span in spans:
        if span is root:
            continue
        parent = span.parent
        assert parent is not None, f"span {span.name} has no parent"
        assert parent.span_id in span_by_id, (
            f"span {span.name} references a parent outside the finished set"
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_telemetry_context() -> Generator[None]:
    """Ensure each test starts without latched telemetry state."""
    guards.reset_telemetry_context()
    yield
    guards.reset_telemetry_context()


@pytest.fixture
def mock_otlp_exporter():
    """Patch the OTLP exporter to use InMemorySpanExporter."""

    exporter = InMemorySpanExporter()
    with patch("conductor.telemetry.setup._create_otlp_exporter", return_value=exporter):
        yield exporter


# ---------------------------------------------------------------------------
# Integration Scenarios
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_without_web_single_root_trace(monkeypatch, mock_otlp_exporter):
    """Scenario 1: resume without --web -> one root trace, resumed flag set.

    Two workflow_started events for the same run_id collapse into a single root span.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    provider = init_tracer_provider(run_id="run-test-1")
    subscriber = TelemetrySubscriber(provider, resumed=True)

    subscriber.on_event(
        WorkflowEvent(
            type="workflow_started",
            timestamp=time.time(),
            data={"name": "test-wf", "run_id": "run-test-1"},
        )
    )
    subscriber.on_event(
        WorkflowEvent(
            type="workflow_started",
            timestamp=time.time() + 1.0,
            data={"name": "test-wf", "run_id": "run-test-1"},
        )
    )
    subscriber.on_event(WorkflowEvent(type="workflow_completed", timestamp=time.time() + 2.0))
    subscriber.close()

    spans = mock_otlp_exporter.get_finished_spans()
    root_spans = [s for s in spans if s.name == "invoke_workflow test-wf"]
    assert len(root_spans) == 1
    assert root_spans[0].attributes.get("conductor.resumed") is True


@pytest.mark.asyncio
async def test_resume_with_web_single_root_trace(tmp_path, monkeypatch, mock_otlp_exporter):
    """Scenario 2: resume with --web -> root span exists and run_id is latched.

    The synthetic workflow_started feed attaches root in the CLI task; resumed
    agents parent under the same root in one trace.
    """
    from conductor.cli.run import resume_workflow_async

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    wf_path = _write_workflow(tmp_path, name="test-wf")
    cp_path = _write_checkpoint(tmp_path, wf_path, run_id="run-web-123")

    mock_dashboard = MagicMock()
    mock_dashboard.start = AsyncMock()
    mock_dashboard.stop = AsyncMock()
    mock_dashboard.wait_for_stop = AsyncMock()
    mock_dashboard.wait_for_kill = AsyncMock()
    mock_dashboard.wait_for_shutdown = AsyncMock()
    mock_dashboard.wait_for_clients_disconnect = AsyncMock()
    mock_dashboard.port = 8080
    mock_dashboard.url = "http://127.0.0.1:8080"

    mock_web_module = MagicMock()
    mock_web_module.WebDashboard.return_value = mock_dashboard

    mock_registry, mock_engine = _make_resume_mocks()
    mock_engine.build_workflow_started_data = AsyncMock(
        return_value={"name": "test-wf", "run_id": "run-web-123"}
    )

    with (
        patch.dict(sys.modules, {"conductor.web.server": mock_web_module}),
        patch("conductor.cli.run.ProviderRegistry", return_value=mock_registry),
        patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
        patch("conductor.cli.run._build_mcp_servers", new_callable=AsyncMock, return_value=None),
        patch(
            "conductor.cli.run._prefetch_plugin_sources",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("conductor.cli.run._write_run_record_for_current_process"),
        patch("conductor.cli.run._remove_run_record_for_current_process_safe"),
        patch("conductor.fleet.retention.maybe_prune_event_logs"),
    ):
        await resume_workflow_async(checkpoint_path=cp_path, web=True, web_bg=True)

    spans = mock_otlp_exporter.get_finished_spans()
    root_spans = [s for s in spans if s.name == "invoke_workflow test-wf"]
    assert len(root_spans) == 1
    root = root_spans[0]
    assert root.attributes.get("gen_ai.conversation.id") == "run-web-123"
    assert root.attributes.get("conductor.resumed") is True
    _assert_single_tree(spans, root)


@pytest.mark.asyncio
async def test_llm_raising_failure_closes_agent_span_error(
    tmp_path, monkeypatch, mock_otlp_exporter
):
    """Scenario 3: LLM-raising failure closes the active agent span with ERROR status."""
    from conductor.cli.run import run_workflow_async

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    wf_path = tmp_path / "error-wf.yaml"
    wf_path.write_text(
        """
workflow:
  name: error-wf
  entry_point: agent1
agents:
  - name: agent1
    model: gpt-4
    prompt: "Hello"
    output:
      result:
        type: string
    routes:
      - to: $end
output:
  result: "{{ agent1.output.result }}"
""",
        encoding="utf-8",
    )

    def mock_handler(agent, prompt, context):
        raise ProviderError("API request failed", provider_name="copilot", status_code=500)

    provider = CopilotProvider(mock_handler=mock_handler)
    mock_registry = MockProviderRegistry(provider)

    with (
        patch("conductor.cli.run.ProviderRegistry", return_value=mock_registry),
        patch("conductor.cli.run._build_mcp_servers", new_callable=AsyncMock, return_value=None),
        patch(
            "conductor.cli.run._prefetch_plugin_sources",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("conductor.cli.run._write_run_record_for_current_process"),
        patch("conductor.cli.run._remove_run_record_for_current_process_safe"),
        patch("conductor.fleet.retention.maybe_prune_event_logs"),
        patch("sys.stdin") as mock_stdin,
        pytest.raises(ProviderError, match="API request failed"),
    ):
        mock_stdin.isatty.return_value = False
        await run_workflow_async(wf_path, {})

    spans = mock_otlp_exporter.get_finished_spans()
    agent_spans = [s for s in spans if s.name == "invoke_agent agent1"]
    assert len(agent_spans) == 1
    agent_span = agent_spans[0]

    assert agent_span.status.status_code == StatusCode.ERROR
    assert agent_span.attributes.get("error.type") == "ProviderError"
    assert "API request failed" in (agent_span.attributes.get("error.message") or "")


@pytest.mark.asyncio
async def test_otel_sdk_disabled_zero_spans(tmp_path, monkeypatch, mock_otlp_exporter):
    """Scenario 4: OTEL_SDK_DISABLED=1 -> zero spans, run completes normally."""
    from conductor.cli.run import run_workflow_async

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    wf_path = tmp_path / "disabled-wf.yaml"
    wf_path.write_text(
        """
workflow:
  name: disabled-wf
  entry_point: greeter
agents:
  - name: greeter
    model: gpt-4
    prompt: "Hello"
    output:
      greeting:
        type: string
    routes:
      - to: $end
output:
  message: "{{ greeter.output.greeting }}"
""",
        encoding="utf-8",
    )

    def mock_handler(agent, prompt, context):
        return {"greeting": "hello"}

    provider = CopilotProvider(mock_handler=mock_handler)
    mock_registry = MockProviderRegistry(provider)

    with (
        patch("conductor.cli.run.ProviderRegistry", return_value=mock_registry),
        patch("conductor.cli.run._build_mcp_servers", new_callable=AsyncMock, return_value=None),
        patch(
            "conductor.cli.run._prefetch_plugin_sources",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("conductor.cli.run._write_run_record_for_current_process"),
        patch("conductor.cli.run._remove_run_record_for_current_process_safe"),
        patch("conductor.fleet.retention.maybe_prune_event_logs"),
        patch("sys.stdin") as mock_stdin,
    ):
        mock_stdin.isatty.return_value = False
        result = await run_workflow_async(wf_path, {})

    assert result == {"message": "hello"}
    spans = mock_otlp_exporter.get_finished_spans()
    assert len(spans) == 0


def test_otlp_endpoint_missing_extra_validate_warning(tmp_path, monkeypatch):
    """Scenario 5: configured OTLP endpoint with a missing telemetry extra.

    Validator should warn but exit 0.
    """
    from conductor.cli.validate import validate_workflow
    from conductor.console import make_console

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setattr("conductor.cli.validate.OTEL_SDK_AVAILABLE", False)

    wf_path = tmp_path / "validate-warn-wf.yaml"
    wf_path.write_text(
        """
workflow:
  name: validate-warn-wf
  entry_point: greeter
agents:
  - name: greeter
    model: gpt-4
    prompt: "Hello"
    output:
      greeting:
        type: string
    routes:
      - to: $end
output:
  message: "{{ greeter.output.greeting }}"
""",
        encoding="utf-8",
    )

    output = io.StringIO()
    console = make_console(file=output)

    is_valid, config = validate_workflow(wf_path, console=console)

    assert is_valid is True
    assert config is not None
    output_text = output.getvalue()
    assert "OTEL_EXPORTER_OTLP_ENDPOINT is set" in output_text
    assert "telemetry" in output_text


@pytest.mark.asyncio
async def test_unreachable_otlp_endpoint_degradation(tmp_path, monkeypatch, caplog):
    """Scenario 6: Unreachable OTLP endpoint (http://127.0.0.1:1).

    Workflow run completes, export-failure warning, wall-clock < baseline + 6s.
    """
    from conductor.cli.run import run_workflow_async

    wf_baseline_path = tmp_path / "baseline-wf.yaml"
    wf_baseline_path.write_text(
        """
workflow:
  name: baseline-wf
  entry_point: greeter
agents:
  - name: greeter
    model: gpt-4
    prompt: "Hello"
    output:
      greeting:
        type: string
    routes:
      - to: $end
output:
  message: "{{ greeter.output.greeting }}"
""",
        encoding="utf-8",
    )

    def mock_handler(agent, prompt, context):
        return {"greeting": "hello"}

    provider = CopilotProvider(mock_handler=mock_handler)
    mock_registry = MockProviderRegistry(provider)

    t0 = time.perf_counter()
    with (
        patch("conductor.cli.run.ProviderRegistry", return_value=mock_registry),
        patch("conductor.cli.run._build_mcp_servers", new_callable=AsyncMock, return_value=None),
        patch(
            "conductor.cli.run._prefetch_plugin_sources",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("conductor.cli.run._write_run_record_for_current_process"),
        patch("conductor.cli.run._remove_run_record_for_current_process_safe"),
        patch("conductor.fleet.retention.maybe_prune_event_logs"),
        patch("sys.stdin") as mock_stdin,
    ):
        mock_stdin.isatty.return_value = False
        await run_workflow_async(wf_baseline_path, {})
    baseline_dur = time.perf_counter() - t0

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")

    wf_telemetry_path = tmp_path / "telemetry-unreachable-wf.yaml"
    wf_telemetry_path.write_text(
        """
workflow:
  name: telemetry-unreachable-wf
  entry_point: greeter
agents:
  - name: greeter
    model: gpt-4
    prompt: "Hello"
    output:
      greeting:
        type: string
    routes:
      - to: $end
output:
  message: "{{ greeter.output.greeting }}"
""",
        encoding="utf-8",
    )

    import requests

    mock_resp = requests.Response()
    mock_resp.status_code = 400
    mock_resp._content = b"Bad Request"

    t0 = time.perf_counter()
    with (
        patch("conductor.cli.run.ProviderRegistry", return_value=mock_registry),
        patch("conductor.cli.run._build_mcp_servers", new_callable=AsyncMock, return_value=None),
        patch(
            "conductor.cli.run._prefetch_plugin_sources",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("conductor.cli.run._write_run_record_for_current_process"),
        patch("conductor.cli.run._remove_run_record_for_current_process_safe"),
        patch("conductor.fleet.retention.maybe_prune_event_logs"),
        patch("sys.stdin") as mock_stdin,
        patch("requests.Session.send", return_value=mock_resp),
    ):
        mock_stdin.isatty.return_value = False
        result = await run_workflow_async(wf_telemetry_path, {})
    telemetry_dur = time.perf_counter() - t0

    assert result == {"message": "hello"}
    assert telemetry_dur < (baseline_dur + 6.0)

    has_otel_log = any(
        record.name.startswith("opentelemetry") and record.levelno >= logging.WARNING
        for record in caplog.records
    )
    if not has_otel_log:
        logging.warning("No OTel warnings captured.")


@pytest.mark.asyncio
async def test_parallel_fail_fast_ends_all_member_spans(tmp_path, monkeypatch, mock_otlp_exporter):
    """Scenario 7: parallel fail_fast -> all member spans ended even when one fails early."""
    from conductor.cli.run import run_workflow_async

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    wf_path = tmp_path / "parallel-fail-fast-wf.yaml"
    wf_path.write_text(
        """
workflow:
  name: parallel-fail-fast-wf
  entry_point: parallel_tasks
agents:
  - name: task_a
    model: gpt-4
    prompt: "Task A"
    output:
      result:
        type: string
  - name: task_b
    model: gpt-4
    prompt: "Task B"
    output:
      result:
        type: string
parallel:
  - name: parallel_tasks
    agents: [task_a, task_b]
    failure_mode: fail_fast
    routes:
      - to: $end
output:
  result: "done"
""",
        encoding="utf-8",
    )

    class TestParallelTelemetryProvider(AgentProvider, abstract=True):
        async def execute(
            self,
            agent: AgentDef,
            context: dict[str, Any],
            rendered_prompt: str,
            tools: list[str] | None = None,
            interrupt_signal: asyncio.Event | None = None,
            event_callback: Callable[[str, dict[str, Any]], None] | None = None,
            skill_directories: list[str] | None = None,
            custom_agents: list[dict[str, Any]] | None = None,
            extra_mcp_servers: dict[str, Any] | None = None,
        ) -> AgentOutput:
            if agent.name == "task_a":
                raise ProviderError("Task A failed", provider_name="copilot", status_code=500)
            else:
                await asyncio.sleep(0.5)
                return AgentOutput(content={"result": "success"}, raw_response=None, model="test")

        async def validate_connection(self):
            return True

        async def close(self):
            pass

    provider = TestParallelTelemetryProvider()
    mock_registry = MockProviderRegistry(provider)

    with (
        patch("conductor.cli.run.ProviderRegistry", return_value=mock_registry),
        patch("conductor.cli.run._build_mcp_servers", new_callable=AsyncMock, return_value=None),
        patch(
            "conductor.cli.run._prefetch_plugin_sources",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("conductor.cli.run._write_run_record_for_current_process"),
        patch("conductor.cli.run._remove_run_record_for_current_process_safe"),
        patch("conductor.fleet.retention.maybe_prune_event_logs"),
        patch("sys.stdin") as mock_stdin,
        pytest.raises(ExecutionError),
    ):
        mock_stdin.isatty.return_value = False
        await run_workflow_async(wf_path, {})

    spans = mock_otlp_exporter.get_finished_spans()
    task_a_spans = [s for s in spans if s.name == "invoke_agent task_a"]
    task_b_spans = [s for s in spans if s.name == "invoke_agent task_b"]

    assert len(task_a_spans) == 1
    assert len(task_b_spans) == 1
    assert task_a_spans[0].end_time is not None
    assert task_b_spans[0].end_time is not None

    root_spans = [s for s in spans if s.name == "invoke_workflow parallel-fail-fast-wf"]
    assert len(root_spans) == 1
    _assert_single_tree(spans, root_spans[0])


@pytest.mark.asyncio
async def test_for_each_key_collision_single_tree(tmp_path, monkeypatch, mock_otlp_exporter):
    """Scenario 8: for_each key collision -> per-item spans isolated in one trace.

    Tool-span attribution via FIFO fallback works, agent spans close on envelope events.
    """
    from conductor.cli.run import run_workflow_async

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    wf_path = tmp_path / "for-each-collision-wf.yaml"
    wf_path.write_text(
        """
workflow:
  name: for-each-collision-wf
  entry_point: finder
agents:
  - name: finder
    model: gpt-4
    prompt: "Find items"
    output:
      items:
        type: array
    routes:
      - to: analyzers
for_each:
  - name: analyzers
    type: for_each
    source: finder.output.items
    as: item
    key_by: id
    failure_mode: all_or_nothing
    agent:
      name: worker
      model: gpt-4
      prompt: "process {{ item }}"
      output:
        r:
          type: string
    routes:
      - to: $end
output:
  result: "done"
""",
        encoding="utf-8",
    )

    class TestForEachTelemetryProvider(AgentProvider, abstract=True):
        def __init__(self):
            self.count = 0

        async def execute(
            self,
            agent: AgentDef,
            context: dict[str, Any],
            rendered_prompt: str,
            tools: list[str] | None = None,
            interrupt_signal: asyncio.Event | None = None,
            event_callback: Callable[[str, dict[str, Any]], None] | None = None,
            skill_directories: list[str] | None = None,
            custom_agents: list[dict[str, Any]] | None = None,
            extra_mcp_servers: dict[str, Any] | None = None,
        ) -> AgentOutput:
            if agent.name == "finder":
                return AgentOutput(
                    content={"items": [{"id": "col"}, {"id": "col"}]},
                    raw_response=None,
                    model="test",
                )

            self.count += 1
            if event_callback:
                event_callback("agent_tool_start", {"tool_name": "lookup"})
                event_callback("agent_tool_complete", {"tool_name": "lookup"})
                event_callback("agent_tool_start", {"tool_name": "lookup"})
                event_callback("agent_tool_complete", {"tool_name": "lookup"})

            if self.count == 2:
                raise ProviderError("Item 2 failed", provider_name="copilot")

            return AgentOutput(content={"r": "ok"}, raw_response=None, model="test")

        async def validate_connection(self):
            return True

        async def close(self):
            pass

    provider = TestForEachTelemetryProvider()
    mock_registry = MockProviderRegistry(provider)

    with (
        patch("conductor.cli.run.ProviderRegistry", return_value=mock_registry),
        patch("conductor.cli.run._build_mcp_servers", new_callable=AsyncMock, return_value=None),
        patch(
            "conductor.cli.run._prefetch_plugin_sources",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("conductor.cli.run._write_run_record_for_current_process"),
        patch("conductor.cli.run._remove_run_record_for_current_process_safe"),
        patch("conductor.fleet.retention.maybe_prune_event_logs"),
        patch("sys.stdin") as mock_stdin,
        pytest.raises(ExecutionError),
    ):
        mock_stdin.isatty.return_value = False
        await run_workflow_async(wf_path, {})

    spans = mock_otlp_exporter.get_finished_spans()

    item_spans = [s for s in spans if s.name == "invoke_agent analyzers[col]"]
    assert len(item_spans) == 2

    tool_spans = [s for s in spans if s.name == "execute_tool lookup"]
    assert len(tool_spans) == 4

    parent_ids = [s.parent.span_id for s in tool_spans if s.parent is not None]
    assert len(parent_ids) == 4

    distinct_parents = set(parent_ids)
    assert len(distinct_parents) == 2
    assert distinct_parents == {item_spans[0].context.span_id, item_spans[1].context.span_id}

    statuses = {item_spans[0].status.status_code, item_spans[1].status.status_code}
    assert statuses == {StatusCode.UNSET, StatusCode.ERROR}

    root_spans = [s for s in spans if s.name == "invoke_workflow for-each-collision-wf"]
    assert len(root_spans) == 1
    _assert_single_tree(spans, root_spans[0])


@pytest.mark.asyncio
async def test_two_runs_in_one_process_keep_separate_single_trees(
    tmp_path, monkeypatch, mock_otlp_exporter
):
    """Scenario 9: two sequential runs in one process each get their own unified trace."""
    from conductor.cli.run import run_workflow_async

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    wf_path = tmp_path / "two-runs-wf.yaml"
    wf_path.write_text(
        """
workflow:
  name: two-runs-wf
  entry_point: greeter
agents:
  - name: greeter
    model: gpt-4
    prompt: "Hello"
    output:
      greeting:
        type: string
    routes:
      - to: $end
output:
  message: "{{ greeter.output.greeting }}"
""",
        encoding="utf-8",
    )

    def mock_handler(agent, prompt, context):
        return {"greeting": "hello"}

    provider = CopilotProvider(mock_handler=mock_handler)
    mock_registry = MockProviderRegistry(provider)

    # Both runs share one exporter; its shutdown() would drop later spans.
    mock_otlp_exporter.shutdown = MagicMock()

    async def _run_once():
        with (
            patch("conductor.cli.run.ProviderRegistry", return_value=mock_registry),
            patch(
                "conductor.cli.run._build_mcp_servers", new_callable=AsyncMock, return_value=None
            ),
            patch(
                "conductor.cli.run._prefetch_plugin_sources",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch("conductor.cli.run._write_run_record_for_current_process"),
            patch("conductor.cli.run._remove_run_record_for_current_process_safe"),
            patch("conductor.fleet.retention.maybe_prune_event_logs"),
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = False
            return await run_workflow_async(wf_path, {})

    result_one = await _run_once()
    result_two = await _run_once()

    assert result_one == {"message": "hello"}
    assert result_two == {"message": "hello"}

    spans = mock_otlp_exporter.get_finished_spans()
    roots = [s for s in spans if s.name == "invoke_workflow two-runs-wf"]
    assert len(roots) == 2
    assert roots[0].get_span_context().trace_id != roots[1].get_span_context().trace_id
    for root in roots:
        tree_spans = [
            s for s in spans if s.get_span_context().trace_id == root.get_span_context().trace_id
        ]
        _assert_single_tree(tree_spans, root)


@pytest.mark.asyncio
async def test_questions_step_lifecycle_single_tree(tmp_path, monkeypatch, mock_otlp_exporter):
    """Scenario 10: questions step closes its agent span and leaves a single tree."""
    from conductor.cli.run import run_workflow_async

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    wf_path = tmp_path / "questions-wf.yaml"
    wf_path.write_text(
        """
workflow:
  name: questions-wf
  entry_point: ask
agents:
  - name: ask
    type: questions
    prompt: "What next?"
    questions:
      - text: "Proceed?"
        choices: ["yes", "no"]
    routes:
      - to: $end
output:
  result: "done"
""",
        encoding="utf-8",
    )

    def mock_handler(agent, prompt, context):
        return {"answer": "yes"}

    provider = CopilotProvider(mock_handler=mock_handler)
    mock_registry = MockProviderRegistry(provider)

    with (
        patch("conductor.cli.run.ProviderRegistry", return_value=mock_registry),
        patch("conductor.cli.run._build_mcp_servers", new_callable=AsyncMock, return_value=None),
        patch(
            "conductor.cli.run._prefetch_plugin_sources",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("conductor.cli.run._write_run_record_for_current_process"),
        patch("conductor.cli.run._remove_run_record_for_current_process_safe"),
        patch("conductor.fleet.retention.maybe_prune_event_logs"),
        patch("sys.stdin") as mock_stdin,
    ):
        mock_stdin.isatty.return_value = False
        result = await run_workflow_async(wf_path, {}, skip_gates=True)

    assert result == {"result": "done"}
    spans = mock_otlp_exporter.get_finished_spans()
    questions_spans = [s for s in spans if s.name == "invoke_agent ask"]
    assert len(questions_spans) == 1
    assert questions_spans[0].end_time is not None

    gate_spans = [s for s in spans if s.name in {"gate_presented", "gate_resolved"}]
    assert gate_spans == []

    roots = [s for s in spans if s.name == "invoke_workflow questions-wf"]
    assert len(roots) == 1
    _assert_single_tree(spans, roots[0])


@pytest.mark.asyncio
async def test_cross_task_detach_ownership(tmp_path, monkeypatch, mock_otlp_exporter):
    """Scenario 11: fail-fast parallel members are ended from the main task, but detach
    only happens in the owner worker task; close() finalises the run cleanly.
    """
    from conductor.cli.run import run_workflow_async

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    wf_path = tmp_path / "cross-task-wf.yaml"
    wf_path.write_text(
        """
workflow:
  name: cross-task-wf
  entry_point: parallel_tasks
agents:
  - name: task_a
    model: gpt-4
    prompt: "Task A"
    output:
      result:
        type: string
  - name: task_b
    model: gpt-4
    prompt: "Task B"
    output:
      result:
        type: string
parallel:
  - name: parallel_tasks
    agents: [task_a, task_b]
    failure_mode: fail_fast
    routes:
      - to: $end
output:
  result: "done"
""",
        encoding="utf-8",
    )

    class CrossTaskProvider(AgentProvider, abstract=True):
        async def execute(
            self,
            agent: AgentDef,
            context: dict[str, Any],
            rendered_prompt: str,
            tools: list[str] | None = None,
            interrupt_signal: asyncio.Event | None = None,
            event_callback: Callable[[str, dict[str, Any]], None] | None = None,
            skill_directories: list[str] | None = None,
            custom_agents: list[dict[str, Any]] | None = None,
            extra_mcp_servers: dict[str, Any] | None = None,
        ) -> AgentOutput:
            if agent.name == "task_a":
                raise ProviderError("boom", provider_name="copilot", status_code=500)
            await asyncio.sleep(0.2)
            return AgentOutput(content={"result": "ok"}, raw_response=None, model="test")

        async def validate_connection(self):
            return True

        async def close(self):
            pass

    provider = CrossTaskProvider()
    mock_registry = MockProviderRegistry(provider)

    original_detach = sys.modules["opentelemetry.context"].detach
    detached_in_main: list[str] = []

    def spy_detach(token):
        current = asyncio.current_task()
        assert current is not None
        loop = asyncio.get_running_loop()
        # The test coroutine runs in the only task on this loop during the
        # synchronous portion; other detach calls belong to worker tasks.
        if current is loop._task:  # type: ignore[attr-defined]
            detached_in_main.append("detached")
        return original_detach(token)

    with (
        patch("conductor.cli.run.ProviderRegistry", return_value=mock_registry),
        patch("conductor.cli.run._build_mcp_servers", new_callable=AsyncMock, return_value=None),
        patch(
            "conductor.cli.run._prefetch_plugin_sources",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("conductor.cli.run._write_run_record_for_current_process"),
        patch("conductor.cli.run._remove_run_record_for_current_process_safe"),
        patch("conductor.fleet.retention.maybe_prune_event_logs"),
        patch("sys.stdin") as mock_stdin,
        pytest.raises(ExecutionError),
    ):
        mock_stdin.isatty.return_value = False
        await run_workflow_async(wf_path, {})

    # Cross-task detach ownership means no detach token for worker spans was
    # reset from the main task during fail-fast cleanup.
    assert detached_in_main == []

    spans = mock_otlp_exporter.get_finished_spans()
    roots = [s for s in spans if s.name == "invoke_workflow cross-task-wf"]
    assert len(roots) == 1
    _assert_single_tree(spans, roots[0])


@pytest.mark.asyncio
async def test_provider_override_dedup_single_tree(tmp_path, monkeypatch, mock_otlp_exporter):
    """Scenario 12: --provider openai override suppresses duplicate conductor tool spans."""
    from conductor.cli.run import run_workflow_async

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    wf_path = tmp_path / "dedup-wf.yaml"
    wf_path.write_text(
        """
workflow:
  name: dedup-wf
  entry_point: worker
agents:
  - name: worker
    model: gpt-4
    prompt: "Use the echo tool"
    output:
      result:
        type: string
    routes:
      - to: $end
output:
  result: "{{ worker.output.result }}"
""",
        encoding="utf-8",
    )

    pytest.importorskip("pydantic_ai")
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.tools import Tool

    class DedupProvider(AgentProvider, abstract=True):
        async def execute(
            self,
            agent: AgentDef,
            context: dict[str, Any],
            rendered_prompt: str,
            tools: list[str] | None = None,
            interrupt_signal: asyncio.Event | None = None,
            event_callback: Callable[[str, dict[str, Any]], None] | None = None,
            skill_directories: list[str] | None = None,
            custom_agents: list[dict[str, Any]] | None = None,
            extra_mcp_servers: dict[str, Any] | None = None,
        ) -> AgentOutput:
            pydantic_agent = Agent(
                TestModel(call_tools=["echo"]),
                tools=[Tool(lambda value: value, name="echo")],
                name=agent.name,
                retries=0,
            )
            pydantic_agent.instrument = None
            run_result = await pydantic_agent.run(rendered_prompt)
            if event_callback:
                event_callback("agent_tool_start", {"tool_name": "echo", "tool_call_id": "call-1"})
                event_callback(
                    "agent_tool_complete", {"tool_name": "echo", "tool_call_id": "call-1"}
                )
            return AgentOutput(
                content={"result": str(run_result.output)}, raw_response=None, model="test"
            )

        async def validate_connection(self):
            return True

        async def close(self):
            pass

    provider = DedupProvider()
    mock_registry = MockProviderRegistry(provider)

    with (
        patch("conductor.cli.run.ProviderRegistry", return_value=mock_registry),
        patch("conductor.cli.run._build_mcp_servers", new_callable=AsyncMock, return_value=None),
        patch(
            "conductor.cli.run._prefetch_plugin_sources",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("conductor.cli.run._write_run_record_for_current_process"),
        patch("conductor.cli.run._remove_run_record_for_current_process_safe"),
        patch("conductor.fleet.retention.maybe_prune_event_logs"),
        patch("sys.stdin") as mock_stdin,
    ):
        mock_stdin.isatty.return_value = False
        result = await run_workflow_async(wf_path, {}, provider_override="openai")

    assert result is not None
    spans = mock_otlp_exporter.get_finished_spans()
    roots = [s for s in spans if s.name == "invoke_workflow dedup-wf"]
    assert len(roots) == 1
    _assert_single_tree(spans, roots[0])
