"""Integration coverage for telemetry wiring in CLI run and resume paths."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from pathlib import Path
from types import TracebackType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from conductor.config.schema import AgentDef
from conductor.engine.checkpoint import CheckpointManager
from conductor.exceptions import ProviderError
from conductor.providers.base import AgentProvider
from conductor.providers.copilot import CopilotProvider
from conductor.telemetry import guards
from conductor.telemetry.semconv import CONDUCTOR_RESUMED, GEN_AI_CONVERSATION_ID


class _ProviderRegistry:
    """Supply one deterministic provider to the real workflow engine."""

    def __init__(self, provider: AgentProvider) -> None:
        self._provider = provider

    async def __aenter__(self) -> _ProviderRegistry:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return None

    async def get_provider(self, _agent: AgentDef) -> AgentProvider:
        return self._provider

    def get_active_providers(self) -> dict[str, Any]:
        return {}


@pytest.fixture(autouse=True)
def reset_telemetry_context() -> Generator[None]:
    """Keep task-local telemetry state isolated between requirements tests."""
    guards.reset_telemetry_context()
    yield
    guards.reset_telemetry_context()


def _write_workflow(tmp_path: Path) -> Path:
    """Write the one-agent workflow exercised by the failure and resume runs."""
    workflow_path = tmp_path / "resume-telemetry.yaml"
    workflow_path.write_text(
        """\
workflow:
  name: resume-telemetry
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
    return workflow_path


def _tracer_provider(exporter: InMemorySpanExporter) -> TracerProvider:
    """Build a synchronous in-memory trace provider for deterministic assertions."""
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider


def _dashboard() -> MagicMock:
    """Create the web-mode surface without starting an HTTP server."""
    dashboard = MagicMock()
    dashboard.port = 8080
    dashboard.url = "http://127.0.0.1:8080"
    dashboard.start = AsyncMock()
    dashboard.stop = AsyncMock()
    dashboard.wait_for_clients_disconnect = AsyncMock()
    dashboard.wait_for_stop = AsyncMock(side_effect=asyncio.Event().wait)
    dashboard.replay_events_from_jsonl = MagicMock(return_value=1)
    return dashboard


@pytest.mark.asyncio
async def test_resume_web_keeps_resumed_agents_in_one_root_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement: ``resume --web`` uses one resumed root and clears the CLI context."""
    from conductor.cli.run import resume_workflow_async, run_workflow_async

    # Given: a real failed run whose checkpoint preserves the original run ID.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    workflow_path = _write_workflow(tmp_path)
    original_exporter = InMemorySpanExporter()
    resumed_exporter = InMemorySpanExporter()
    attempts = 0

    def mock_handler(_agent: AgentDef, _prompt: str, _context: dict[str, Any]) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ProviderError("initial failure", provider_name="copilot")
        return {"greeting": "resumed"}

    registry = _ProviderRegistry(CopilotProvider(mock_handler=mock_handler))
    dashboard = _dashboard()
    web_module = MagicMock()
    web_module.WebDashboard.return_value = dashboard
    checkpoint_run_id = ""
    result: dict[str, Any] = {}

    with (
        patch(
            "conductor.telemetry.setup._build_tracer_provider",
            side_effect=[_tracer_provider(original_exporter), _tracer_provider(resumed_exporter)],
        ),
        patch("conductor.cli.run.ProviderRegistry", return_value=registry),
        patch("conductor.cli.run._build_mcp_servers", new_callable=AsyncMock, return_value=None),
        patch(
            "conductor.cli.run._prefetch_plugin_sources",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("conductor.cli.run._write_run_record_for_current_process"),
        patch("conductor.cli.run._remove_run_record_for_current_process_safe"),
        patch("conductor.fleet.retention.maybe_prune_event_logs"),
        patch.dict("sys.modules", {"conductor.web.server": web_module}),
        patch("sys.stdin") as mock_stdin,
    ):
        mock_stdin.isatty.return_value = False
        with pytest.raises(ProviderError, match="initial failure"):
            await run_workflow_async(workflow_path, {}, no_interactive=True)

        checkpoint = CheckpointManager.list_checkpoints(workflow_path)[0]
        checkpoint_run_id = checkpoint.run_id

        result = await resume_workflow_async(
            checkpoint_path=checkpoint.file_path,
            web=True,
            web_bg=True,
            no_interactive=True,
        )

    # Then: one root owns every resumed span and close detached the CLI task context.
    assert result == {"message": "resumed"}
    spans = resumed_exporter.get_finished_spans()
    roots = [
        span
        for span in spans
        if span.name == "invoke_workflow resume-telemetry"
        and span.attributes is not None
        and span.attributes[GEN_AI_CONVERSATION_ID] == checkpoint_run_id
    ]
    assert len(roots) == 1
    root = roots[0]
    assert root.attributes is not None
    assert root.attributes[CONDUCTOR_RESUMED] is True
    agent = next(span for span in spans if span.name == "invoke_agent greeter")
    assert agent.parent is not None
    assert root.context is not None
    assert agent.parent.span_id == root.context.span_id
    assert {span.context.trace_id for span in spans if span.context is not None} == {
        root.context.trace_id
    }
    assert not trace.get_current_span().get_span_context().is_valid
