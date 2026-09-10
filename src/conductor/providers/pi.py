"""Pi SDK provider via a persistent-session Node bridge."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from conductor.config.schema import AgentDef
from conductor.exceptions import ProviderError, ValidationError
from conductor.providers._event_format import emit_parse_recovery_event
from conductor.providers.base import AgentOutput, AgentProvider, EventCallback
from conductor.providers.capabilities import ProviderCapabilities


class PiProvider(AgentProvider):
    """Run Pi SDK sessions through a JSONL Node bridge."""

    CAPABILITIES = ProviderCapabilities(
        tier="experimental",
        mcp_tools=False,
        workflow_tools_passthrough=False,
        streaming_events=True,
        agent_reasoning_events=True,
        reasoning_effort=("low", "medium", "high", "xhigh", "max"),
        structured_output="prompt_injection",
        interrupt=True,
        max_session_seconds=True,
        checkpoint_resume=True,
        usage_tracking=True,
        concurrent_safe=True,
        working_dir=True,
        skills=True,
        upstream_pin="@earendil-works/pi-coding-agent",
        maintainer="local project",
    )

    def __init__(
        self,
        model: str | None = None,
        max_session_seconds: float | None = None,
        max_agent_iterations: int | None = None,
    ) -> None:
        self._default_model = model
        self._max_session_seconds = max_session_seconds
        self._max_agent_iterations = max_agent_iterations
        self._bridge = Path(__file__).parent / "_pi" / "runner.mjs"
        self._session_ids: dict[str, str] = {}
        self._resume_session_ids: dict[str, str] = {}

    async def validate_connection(self) -> bool:
        """Verify bridge can load Pi and see one authenticated model."""
        return bool(await self.list_models())

    async def close(self) -> None:
        """Bridge processes are scoped to individual executions."""

    async def list_models(self) -> list[str] | None:
        """Return authenticated Pi model ids as ``provider/model`` strings."""
        if not self._bridge.is_file() or shutil.which("node") is None:
            return None
        process = await asyncio.create_subprocess_exec(
            "node",
            str(self._bridge),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(b'{"action":"list_models"}\n')
        await process.stdin.drain()
        process.stdin.close()
        line = await process.stdout.readline()
        await process.wait()
        if process.returncode != 0 or not line:
            return None
        message = json.loads(line)
        return message.get("models") if message.get("type") == "models" else None

    def get_session_ids(self) -> dict[str, str]:
        """Return durable Pi session-file paths for Conductor checkpoints."""
        return self._session_ids.copy()

    def set_resume_session_ids(self, ids: dict[str, str]) -> None:
        """Accept checkpointed Pi session-file paths for next execution."""
        self._resume_session_ids = dict(ids)

    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None = None,
        interrupt_signal: asyncio.Event | None = None,
        event_callback: EventCallback | None = None,
        skill_directories: list[str] | None = None,
    ) -> AgentOutput:
        if tools:
            raise ProviderError(
                "Pi provider cannot map Conductor `tools:` allowlists to Pi tools.",
                suggestion="Omit `tools:`. Pi's tool policy comes from Pi settings and extensions.",
                provider_name="pi",
                is_retryable=False,
            )
        if skill_directories:
            raise ProviderError(
                "Pi provider received native skill directories unexpectedly.",
                provider_name="pi",
                is_retryable=False,
            )

        max_seconds = (
            agent.max_session_seconds
            if agent.max_session_seconds is not None
            else self._max_session_seconds
        )
        max_iterations = (
            agent.max_agent_iterations
            if agent.max_agent_iterations is not None
            else self._max_agent_iterations
        )
        retry = agent.retry
        max_recovery = retry.max_parse_recovery_attempts if retry else None
        payload = {
            "cwd": agent.working_dir or os.getcwd(),
            "prompt": rendered_prompt,
            "model": agent.model or self._default_model,
            "thinking": agent.reasoning.effort if agent.reasoning else None,
            "output_schema": {
                name: field.model_dump() for name, field in (agent.output or {}).items()
            },
            "max_parse_recovery_attempts": max_recovery,
            "max_agent_iterations": max_iterations,
            "session_path": self._resume_session_ids.get(agent.name)
            or self._session_ids.get(agent.name),
        }
        if event_callback is not None:
            event_callback("agent_turn_start", {"turn": "awaiting_model"})

        process = await asyncio.create_subprocess_exec(
            "node",
            str(self._bridge),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write((json.dumps(payload) + "\n").encode())
        await process.stdin.drain()
        process.stdin.close()

        result: dict[str, Any] | None = None
        bridge_error: str | None = None
        interrupted = False
        timed_out = False
        terminating_at: float | None = None
        started = time.monotonic()
        while True:
            now = time.monotonic()
            should_abort = (interrupt_signal is not None and interrupt_signal.is_set()) or (
                max_seconds is not None and now - started >= max_seconds
            )
            if should_abort and terminating_at is None:
                interrupted = interrupt_signal is not None and interrupt_signal.is_set()
                timed_out = not interrupted
                process.terminate()  # Bridge SIGTERM handler calls AgentSession.abort().
                terminating_at = now
            if terminating_at is not None and now - terminating_at > 5:
                process.kill()
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=0.1)
            except TimeoutError:
                continue
            if not line:
                break
            message = json.loads(line)
            kind = message["type"]
            if kind == "session" and message.get("path"):
                self._session_ids[agent.name] = message["path"]
            elif kind == "result":
                result = message
                if message.get("session_path"):
                    self._session_ids[agent.name] = message["session_path"]
            elif kind == "error":
                bridge_error = message["error"]
            elif kind == "parse_recovery":
                emit_parse_recovery_event(
                    event_callback,
                    attempt=message["attempt"],
                    max_attempts=message["max_attempts"],
                    is_schema_failure=True,
                    error=message["error"],
                )
            elif event_callback is not None:
                self._emit_event(event_callback, message)

        stderr = (await process.stderr.read()).decode() if process.stderr is not None else ""
        await process.wait()
        if timed_out:
            raise ProviderError(
                f"Pi agent exceeded max_session_seconds ({max_seconds:.0f}s).",
                provider_name="pi",
                is_retryable=False,
            )
        if interrupted and result is None:
            return AgentOutput(content={"response": ""}, raw_response="", partial=True)
        if bridge_error is not None:
            if bridge_error.startswith("SCHEMA:"):
                raise ValidationError(bridge_error.removeprefix("SCHEMA:"))
            raise ProviderError(bridge_error, provider_name="pi")
        if result is None:
            raise ProviderError(
                f"Pi bridge exited {process.returncode}: {stderr.strip() or 'without result'}",
                provider_name="pi",
            )

        text = result.get("text", "")
        content = result.get("content") if agent.output else {"response": text}
        if agent.output and not isinstance(content, dict) and not result.get("partial"):
            raise ValidationError(f"Pi agent '{agent.name}' returned no valid structured output.")
        return AgentOutput(
            content=content if isinstance(content, dict) else {"response": text},
            raw_response=text,
            tokens_used=result.get("tokens_used"),
            input_tokens=result.get("input_tokens"),
            output_tokens=result.get("output_tokens"),
            cache_read_tokens=result.get("cache_read_tokens"),
            cache_write_tokens=result.get("cache_write_tokens"),
            model=result.get("model"),
            partial=bool(result.get("partial") or interrupted),
        )

    @staticmethod
    def _emit_event(callback: EventCallback, message: dict[str, Any]) -> None:
        event_type = message["type"]
        if event_type == "text":
            callback("agent_message", {"content": message["delta"]})
        elif event_type == "reasoning":
            callback("agent_reasoning", {"content": message["delta"]})
        elif event_type == "tool_start":
            callback(
                "agent_tool_start",
                {"tool_name": message["name"], "arguments": message.get("args")},
            )
        elif event_type == "tool_complete":
            callback(
                "agent_tool_complete",
                {"tool_name": message["name"], "is_error": message.get("is_error", False)},
            )
