"""Integration tests for Conductor OpenTelemetry tracing under env-driven activation."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
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

    def provider_type_for(self, agent: AgentDef) -> str:
        return agent.provider or "copilot"

    def provider_settings_for(self, provider_type: str) -> Any:
        """The mock provider carries no structured runtime settings."""
        return None


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
    assert "An OTLP endpoint is set" in output_text
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
            *,
            tools: list[str] | None = None,
            interrupt_signal: asyncio.Event | None = None,
            event_callback: Callable[[str, dict[str, Any]], None] | None = None,
            skill_directories: list[str] | None = None,
            custom_agents: list[dict[str, Any]] | None = None,
            extra_mcp_servers: dict[str, Any] | None = None,
            continuation_state: object | None = None,
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
            *,
            tools: list[str] | None = None,
            interrupt_signal: asyncio.Event | None = None,
            event_callback: Callable[[str, dict[str, Any]], None] | None = None,
            skill_directories: list[str] | None = None,
            custom_agents: list[dict[str, Any]] | None = None,
            extra_mcp_servers: dict[str, Any] | None = None,
            continuation_state: object | None = None,
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
            *,
            tools: list[str] | None = None,
            interrupt_signal: asyncio.Event | None = None,
            event_callback: Callable[[str, dict[str, Any]], None] | None = None,
            skill_directories: list[str] | None = None,
            custom_agents: list[dict[str, Any]] | None = None,
            extra_mcp_servers: dict[str, Any] | None = None,
            continuation_state: object | None = None,
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

    # Record every attach/detach with its owning task so the assertions below
    # observe what production code actually did — an uninstalled spy would
    # make the cross-task-detach assertion vacuous. contextvars.Token is
    # unhashable, so recordings key on id(token). Retain every token until the
    # assertions finish so Python cannot reuse an ID and overwrite its owner.
    otel_context_module = sys.modules["opentelemetry.context"]
    original_attach = otel_context_module.attach
    original_detach = otel_context_module.detach
    main_task = asyncio.current_task()
    attached_tokens: list[object] = []
    attach_owner: dict[int, asyncio.Task[None] | None] = {}
    detach_events: list[tuple[int, asyncio.Task[None] | None]] = []

    def recording_attach(context: Any) -> object:
        token = original_attach(context)
        attached_tokens.append(token)
        attach_owner[id(token)] = asyncio.current_task()
        return token

    def recording_detach(token: object) -> None:
        detach_events.append((id(token), asyncio.current_task()))
        return original_detach(token)

    caller_context = otel_context_module.get_current()

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
        patch.object(otel_context_module, "attach", recording_attach),
        patch.object(otel_context_module, "detach", recording_detach),
        pytest.raises(ExecutionError),
    ):
        mock_stdin.isatty.return_value = False
        await run_workflow_async(wf_path, {})

    # Worker spans were really attached by worker tasks — the scenario under
    # test (fail-fast cleanup ending another task's spans) was exercised.
    worker_tokens = {
        token_id
        for token_id, owner in attach_owner.items()
        if owner is not None and owner is not main_task
    }
    assert worker_tokens, "no worker-task context attaches observed; test is vacuous"

    # Cross-task detach ownership: no worker token was detached by any task
    # other than the one that attached it during fail-fast cleanup.
    cross_task_detaches = [
        (token_id, detacher)
        for token_id, detacher in detach_events
        if token_id in worker_tokens and detacher is not attach_owner[token_id]
    ]
    assert cross_task_detaches == []

    # Cleanup restored the caller's original context: no span context leaked
    # past the run.
    assert otel_context_module.get_current() == caller_context

    spans = mock_otlp_exporter.get_finished_spans()
    roots = [s for s in spans if s.name == "invoke_workflow cross-task-wf"]
    assert len(roots) == 1
    _assert_single_tree(spans, roots[0])


@pytest.mark.asyncio
async def test_provider_override_dedup_single_tree(tmp_path, monkeypatch, mock_otlp_exporter):
    """Scenario 12: native Pydantic AI spans replace Conductor's fallback tool spans.

    Exercises the real OpenAI builder and runner with only the model swapped
    for TestModel: the run must export exactly one tool span — the native
    ``execute_tool`` span — inside the workflow's single trace with the run id
    as conversation identity. A Conductor fallback span duplicating the same
    tool call would fail the count assertion.
    """
    from pydantic_ai.models.test import TestModel

    from conductor.config.schema import (
        OutputField,
        RouteDef,
        RuntimeConfig,
        WorkflowConfig,
        WorkflowDef,
    )
    from conductor.engine.workflow import RunContext, WorkflowEngine
    from conductor.events import WorkflowEventEmitter
    from conductor.providers.openai import OpenAIProvider

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

    tool_name = "filesystem__read_file"
    provider = OpenAIProvider(api_key="test-key", model="gpt-4")
    mock_mcp = MagicMock()
    mock_mcp.get_all_tools.return_value = [
        {
            "name": tool_name,
            "description": "Read a file from the filesystem",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            "server": "filesystem",
            "original_name": "read_file",
        }
    ]
    mock_mcp.has_servers.return_value = True
    mock_mcp.call_tool = AsyncMock(return_value="file contents")
    provider._mcp_servers_config = {"filesystem": {"command": "true"}}
    provider._mcp_managers = {os.getcwd(): mock_mcp}
    provider._mcp_manager_locks = {}

    # Swap only the model: the real builder still applies Conductor's
    # instrumentation settings, and the real runner still emits the events.
    monkeypatch.setattr(
        "conductor.providers._pydantic_ai.agent_builder._resolve_openai_model",
        lambda *args, **kwargs: TestModel(
            call_tools=[tool_name], custom_output_args={"result": "ok"}
        ),
    )

    config = WorkflowConfig(
        workflow=WorkflowDef(
            name="dedup-wf",
            entry_point="worker",
            runtime=RuntimeConfig(provider="openai"),
        ),
        agents=[
            AgentDef(
                name="worker",
                model="gpt-4",
                prompt="Use the read_file tool",
                output={"result": OutputField(type="string")},
                routes=[RouteDef(to="$end")],
            ),
        ],
        output={"result": "{{ worker.output.result }}"},
    )

    tracer_provider = init_tracer_provider(run_id="run-dedup")
    assert tracer_provider is not None
    subscriber = TelemetrySubscriber(tracer_provider)
    emitter = WorkflowEventEmitter()
    emitter.subscribe(subscriber.on_event)

    engine = WorkflowEngine(
        config,
        provider,
        event_emitter=emitter,
        run_context=RunContext(run_id="run-dedup"),
    )
    result = await engine.run({})
    subscriber.close()

    assert result is not None
    spans = mock_otlp_exporter.get_finished_spans()
    roots = [s for s in spans if s.name == "invoke_workflow dedup-wf"]
    assert len(roots) == 1
    _assert_single_tree(spans, roots[0])

    # Exactly one tool span: the native execute_tool span from the
    # instrumented runner. A Conductor fallback span for the same call would
    # be a duplicate and fail this count.
    tool_spans = [s for s in spans if s.name == f"execute_tool {tool_name}"]
    assert len(tool_spans) == 1

    # The native model span reached the same run-local exporter.
    chat_spans = [s for s in spans if s.name.startswith("chat ")]
    assert chat_spans

    # Conversation identity: every span in the tree carries the workflow run id.
    conversation_ids = {
        span.attributes.get("gen_ai.conversation.id") for span in spans if span.attributes
    }
    assert conversation_ids == {"run-dedup"}

    # Ancestry: Conductor's agent span carries the conductor-specific step
    # attribute; the native Pydantic AI invocation wrapper does not. The
    # wrapper nests under the Conductor span, and the model/tool spans nest
    # under the wrapper.
    conductor_agent = next(
        s
        for s in spans
        if s.name == "invoke_agent worker" and s.attributes.get("conductor.step.type")
    )
    native_wrappers = [
        s
        for s in spans
        if s.name == "invoke_agent worker" and not s.attributes.get("conductor.step.type")
    ]
    assert len(native_wrappers) == 1
    wrapper = native_wrappers[0]
    agent_span_id = conductor_agent.get_span_context().span_id
    wrapper_span_id = wrapper.get_span_context().span_id
    assert wrapper.parent is not None
    assert wrapper.parent.span_id == agent_span_id
    for native_child in [*chat_spans, *tool_spans]:
        assert native_child.parent is not None
        assert native_child.parent.span_id == wrapper_span_id


@pytest.mark.asyncio
async def test_nested_subworkflow_agents_parent_under_child_workflow(
    tmp_path, monkeypatch, mock_otlp_exporter
):
    """Scenario 13: a child workflow's spans nest under the child workflow span.

    Real nested engine execution (not an isolated lookup): the remembered
    delegate invocation span must only parent the child workflow span itself;
    once that span exists, the child's agents and groups attach to it, so the
    exported tree reads delegate -> inner-workflow -> inner-agent.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

    from conductor.config.schema import (
        AgentDef,
        LimitsConfig,
        RouteDef,
        RuntimeConfig,
        WorkflowConfig,
        WorkflowDef,
    )
    from conductor.engine.workflow import RunContext, WorkflowEngine
    from conductor.events import WorkflowEventEmitter

    (tmp_path / "child.yaml").write_text(
        """\
workflow:
  name: child-wf
  entry_point: inner
agents:
  - name: inner
    model: gpt-4
    prompt: "Do inner work"
    output:
      result:
        type: string
    routes:
      - to: $end
output:
  result: "{{ inner.output.result }}"
""",
        encoding="utf-8",
    )
    config = WorkflowConfig(
        workflow=WorkflowDef(
            name="parent-wf",
            entry_point="delegate",
            runtime=RuntimeConfig(provider="copilot"),
            limits=LimitsConfig(max_iterations=5),
        ),
        agents=[
            AgentDef(
                name="delegate",
                type="workflow",
                workflow="child.yaml",
                routes=[RouteDef(to="$end")],
            ),
        ],
        output={"result": "{{ delegate.output.result }}"},
    )

    def handler(agent, _prompt, _context):
        return {"result": "done"}

    provider = CopilotProvider(mock_handler=handler)
    tracer_provider = init_tracer_provider(run_id="run-nested")
    assert tracer_provider is not None
    subscriber = TelemetrySubscriber(tracer_provider)
    emitter = WorkflowEventEmitter()
    emitter.subscribe(subscriber.on_event)

    engine = WorkflowEngine(
        config,
        provider,
        event_emitter=emitter,
        workflow_path=tmp_path / "parent.yaml",
        run_context=RunContext(run_id="run-nested"),
    )
    result = await engine.run({})
    subscriber.close()

    assert result is not None
    spans = mock_otlp_exporter.get_finished_spans()
    by_name = {}
    for span in spans:
        by_name.setdefault(span.name, []).append(span)

    roots = by_name["invoke_workflow parent-wf"]
    assert len(roots) == 1
    _assert_single_tree(spans, roots[0])

    delegate = by_name["invoke_agent delegate"][0]
    child_workflow = by_name["invoke_workflow child-wf"][0]
    inner_agent = by_name["invoke_agent inner"][0]

    # The child workflow span attaches to the delegate invocation span...
    assert child_workflow.parent is not None
    assert child_workflow.parent.span_id == delegate.get_span_context().span_id
    # ...and the child's agent nests under the child workflow span, not under
    # the delegate span that launched the child.
    assert inner_agent.parent is not None
    assert inner_agent.parent.span_id == child_workflow.get_span_context().span_id


@pytest.mark.asyncio
async def test_nested_subworkflow_uses_inherited_registry_provider_identity(
    tmp_path, monkeypatch, mock_otlp_exporter
):
    """Scenario 14: child events reflect the provider its inherited registry executes."""
    from conductor.config.schema import (
        AgentDef,
        LimitsConfig,
        ProviderSettings,
        RouteDef,
        RuntimeConfig,
        WorkflowConfig,
        WorkflowDef,
    )
    from conductor.engine.workflow import RunContext, WorkflowEngine
    from conductor.events import WorkflowEventEmitter

    # Given: the root registry executes Copilot through an external runtime,
    # while the child workflow declares OpenAI as its own unused default.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    (tmp_path / "child-provider.yaml").write_text(
        """\
workflow:
  name: child-provider-wf
  entry_point: inner
  runtime:
    provider: openai
agents:
  - name: inner
    model: gpt-4
    prompt: "Do inner work"
    output:
      result:
        type: string
    routes:
      - to: $end
output:
  result: "{{ inner.output.result }}"
""",
        encoding="utf-8",
    )
    config = WorkflowConfig(
        workflow=WorkflowDef(
            name="parent-provider-wf",
            entry_point="delegate",
            runtime=RuntimeConfig(
                provider=ProviderSettings(
                    name="copilot",
                    runtime_url="http://localhost:9000",
                )
            ),
            limits=LimitsConfig(max_iterations=5),
        ),
        agents=[
            AgentDef(
                name="delegate",
                type="workflow",
                workflow="child-provider.yaml",
                routes=[RouteDef(to="$end")],
            )
        ],
        output={"result": "{{ delegate.output.result }}"},
    )

    class ToolProvider(AgentProvider, abstract=True):
        async def execute(
            self,
            agent: AgentDef,
            context: dict[str, Any],
            rendered_prompt: str,
            *,
            tools: list[str] | None = None,
            interrupt_signal: asyncio.Event | None = None,
            event_callback: Callable[[str, dict[str, Any]], None] | None = None,
            skill_directories: list[str] | None = None,
            custom_agents: list[dict[str, Any]] | None = None,
            extra_mcp_servers: dict[str, Any] | None = None,
            continuation_state: object | None = None,
        ) -> AgentOutput:
            if event_callback is not None:
                event_callback("agent_tool_start", {"tool_name": "lookup"})
                event_callback("agent_tool_complete", {"tool_name": "lookup"})
            return AgentOutput(content={"result": "done"}, raw_response=None, model="test")

        async def validate_connection(self) -> bool:
            return True

        async def close(self) -> None:
            return None

    class RootRegistry(MockProviderRegistry):
        def provider_type_for(self, agent: AgentDef) -> str:
            return agent.provider or "copilot"

        def provider_settings_for(self, provider_type: str) -> ProviderSettings | None:
            if provider_type == "copilot":
                return config.workflow.runtime.provider
            return None

    tracer_provider = init_tracer_provider(run_id="run-provider-identity")
    assert tracer_provider is not None
    subscriber = TelemetrySubscriber(tracer_provider)
    emitter = WorkflowEventEmitter()
    events: list[WorkflowEvent] = []
    emitter.subscribe(subscriber.on_event)
    emitter.subscribe(events.append)

    engine = WorkflowEngine(
        config,
        registry=RootRegistry(ToolProvider()),
        event_emitter=emitter,
        workflow_path=tmp_path / "parent-provider.yaml",
        run_context=RunContext(run_id="run-provider-identity"),
    )

    # When: the child agent executes through the inherited root registry.
    result = await engine.run({})
    subscriber.close()

    # Then: its event names Copilot, native spans remain disabled for the
    # external runtime, and Conductor retains the fallback tool span.
    assert result == {"result": "done"}
    inner_started = next(
        event
        for event in events
        if event.type == "agent_started" and event.data.get("agent_name") == "inner"
    )
    assert inner_started.data["provider"] == "copilot"
    assert inner_started.data["native_otel_spans_active"] is False
    tool_spans = [
        span
        for span in mock_otlp_exporter.get_finished_spans()
        if span.name == "execute_tool lookup"
    ]
    assert len(tool_spans) == 1
