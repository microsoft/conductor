"""Unit tests for :mod:`conductor.executor.mcp_step`.

Covers:
- Recursive argument rendering with set-auto coercion (whole-string templates
  coerce to typed scalars, embedded templates stay strings)
- Non-string YAML-native leaves passing through unchanged
- FileString (``!file`` tag) rendering as a normal template
- Envelope merge (structured keys merged on top; envelope keys never overridden)
- Per-call timeout raising :class:`ExecutionError`
- Manager errors (unknown tool ValueError) propagating unchanged
- :func:`mcp_result_bytes` byte-exactness, including multibyte UTF-8 text
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from conductor.config.schema import MCPStepDef
from conductor.engine.context import WorkflowContext
from conductor.exceptions import ExecutionError
from conductor.executor.mcp_step import McpStepExecutor, mcp_result_bytes
from conductor.file_string import FileString


class FakeMCPManager:
    """Minimal stand-in for MCPManager recording the last structured call."""

    def __init__(
        self,
        envelope: dict[str, Any] | None = None,
        *,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.envelope = envelope or {"content": [], "structured": None, "is_error": False}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.error = error
        self.delay = delay

    async def call_tool_structured(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Record the call and return the canned envelope (or raise / sleep)."""
        self.calls.append((server_name, tool_name, arguments))
        if self.error is not None:
            raise self.error
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.envelope


@pytest.fixture
def executor() -> McpStepExecutor:
    return McpStepExecutor()


def make_agent(**overrides: Any) -> MCPStepDef:
    """Build an mcp AgentDef with sensible defaults, overridden per test."""
    kwargs: dict[str, Any] = {"name": "lookup", "type": "mcp", "server": "srv", "tool": "ping"}
    kwargs.update(overrides)
    return MCPStepDef(**kwargs)


class TestArgumentRendering:
    """Recursive render + auto-coercion of ``arguments``."""

    async def test_whole_string_template_coerces_to_int(self, executor: McpStepExecutor) -> None:
        # Requirement: a whole-string numeric template renders to a typed int argument.
        manager = FakeMCPManager()
        agent = make_agent(arguments={"limit": "{{ limit }}"})
        await executor.execute(agent, {"limit": 105}, manager)  # type: ignore[arg-type]
        assert manager.calls[0][2] == {"limit": 105}
        assert isinstance(manager.calls[0][2]["limit"], int)

    async def test_embedded_template_stays_string(self, executor: McpStepExecutor) -> None:
        # Requirement: a template embedded in surrounding text must remain a string.
        manager = FakeMCPManager()
        agent = make_agent(arguments={"query": "pre-{{ x }}"})
        await executor.execute(agent, {"x": "fix"}, manager)  # type: ignore[arg-type]
        assert manager.calls[0][2] == {"query": "pre-fix"}

    async def test_boolean_template_coerces_to_bool(self, executor: McpStepExecutor) -> None:
        # Requirement: a whole-string boolean template renders to a typed bool argument.
        manager = FakeMCPManager()
        agent = make_agent(arguments={"strict": "{{ flag }}"})
        await executor.execute(agent, {"flag": True}, manager)  # type: ignore[arg-type]
        assert manager.calls[0][2] == {"strict": True}
        assert isinstance(manager.calls[0][2]["strict"], bool)

    async def test_nested_dict_and_list_arguments_recurse(self, executor: McpStepExecutor) -> None:
        # Requirement: rendering recurses through nested dicts and lists at any depth.
        manager = FakeMCPManager()
        agent = make_agent(
            arguments={
                "filter": {"name": "{{ name }}", "tags": ["a", "{{ tag }}"]},
            }
        )
        await executor.execute(agent, {"name": "n1", "tag": "b"}, manager)  # type: ignore[arg-type]
        assert manager.calls[0][2] == {"filter": {"name": "n1", "tags": ["a", "b"]}}

    async def test_non_string_leaves_pass_through(self, executor: McpStepExecutor) -> None:
        # Requirement: YAML-native scalars (int/float/bool/None) pass through unchanged.
        manager = FakeMCPManager()
        agent = make_agent(arguments={"n": 7, "f": 1.5, "b": False, "nothing": None})
        await executor.execute(agent, {}, manager)  # type: ignore[arg-type]
        args = manager.calls[0][2]
        assert args == {"n": 7, "f": 1.5, "b": False, "nothing": None}
        assert isinstance(args["n"], int)

    async def test_file_string_renders_as_template(
        self, executor: McpStepExecutor, tmp_path: Path
    ) -> None:
        # Requirement: a FileString (!file tag) is a str subclass and renders like a
        # normal template, yielding a plain string argument.
        source = tmp_path / "prompt.txt"
        source.write_text("Hello {{ who }}", encoding="utf-8")
        manager = FakeMCPManager()
        file_value = FileString("Hello {{ who }}", source_path=source)
        agent = make_agent(arguments={"greeting": file_value})
        await executor.execute(agent, {"who": "world"}, manager)  # type: ignore[arg-type]
        assert manager.calls[0][2] == {"greeting": "Hello world"}
        assert type(manager.calls[0][2]["greeting"]) is str

    async def test_empty_render_binds_empty_string_not_none(
        self, executor: McpStepExecutor
    ) -> None:
        # Requirement: an empty/whitespace-only template render binds "" (not None),
        # matching the set-step auto rule.
        manager = FakeMCPManager()
        agent = make_agent(arguments={"q": "{{ missing | default('') }}"})
        await executor.execute(agent, {}, manager)  # type: ignore[arg-type]
        assert manager.calls[0][2] == {"q": ""}

    async def test_fromjson_parses_prior_step_text_into_native_argument(
        self, executor: McpStepExecutor
    ) -> None:
        # Requirement: the issue #579 read-modify-write pattern — a prior MCP
        # step's content[0].text JSON string is parsed with ``fromjson``,
        # merged, serialized with ``tojson``, and auto-coercion delivers a
        # native list argument to the tool. The context is built through the
        # real WorkflowContext API so the test pins the access path the
        # engine actually exposes (<step>.output..., no steps. prefix).
        manager = FakeMCPManager()
        agent = make_agent(
            arguments={
                "labels": (
                    "{{ (((get_mr.output.content[0].text | fromjson).labels"
                    " | default([])) + ['Conductor::Need human']) | list | tojson }}"
                )
            }
        )
        workflow_context = WorkflowContext()
        workflow_context.store(
            "get_mr",
            {
                "content": [{"type": "text", "text": '{"labels": ["renovate"]}'}],
                "structured": None,
                "is_error": False,
            },
        )
        context = workflow_context.build_for_agent("update_mr", [])
        await executor.execute(agent, context, manager)  # type: ignore[arg-type]
        labels = manager.calls[0][2]["labels"]
        assert labels == ["renovate", "Conductor::Need human"]
        assert isinstance(labels, list)

    async def test_no_arguments_sends_empty_dict(self, executor: McpStepExecutor) -> None:
        # Requirement: steps without arguments call the tool with an empty dict.
        manager = FakeMCPManager()
        agent = make_agent(arguments=None)
        await executor.execute(agent, {}, manager)  # type: ignore[arg-type]
        assert manager.calls[0][2] == {}


class TestEnvelopeMerge:
    """Structured keys merged on top; envelope keys never overridden."""

    async def test_structured_keys_merge_on_top(self, executor: McpStepExecutor) -> None:
        # Requirement: dict structured content lands on top of the envelope so routes
        # can address individual result fields.
        manager = FakeMCPManager(
            envelope={
                "content": [{"type": "text", "text": "ok"}],
                "structured": {"count": 3, "items": ["a"]},
                "is_error": False,
            }
        )
        agent = make_agent()
        result = await executor.execute(agent, {}, manager)  # type: ignore[arg-type]
        assert result["count"] == 3
        assert result["items"] == ["a"]
        assert result["content"] == [{"type": "text", "text": "ok"}]
        assert result["is_error"] is False

    async def test_envelope_key_collisions_are_dropped(self, executor: McpStepExecutor) -> None:
        # Requirement: structured keys named content/structured/is_error can never
        # override the envelope fields — collisions are dropped.
        manager = FakeMCPManager(
            envelope={
                "content": [{"type": "text", "text": "real"}],
                "structured": {"content": "fake", "is_error": True, "structured": {}, "ok": 1},
                "is_error": False,
            }
        )
        agent = make_agent()
        result = await executor.execute(agent, {}, manager)  # type: ignore[arg-type]
        assert result["content"] == [{"type": "text", "text": "real"}]
        assert result["is_error"] is False
        assert result["ok"] == 1
        assert "structured" in result  # the envelope's own structured mapping survives

    async def test_shadow_collision_logged_at_debug(
        self, executor: McpStepExecutor, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Requirement: dropped envelope-key collisions are logged at debug level,
        # listing the offending keys (mirrors the script-step shadow precedent).
        manager = FakeMCPManager(
            envelope={
                "content": [],
                "structured": {"content": "fake", "is_error": True},
                "is_error": False,
            }
        )
        agent = make_agent()
        with caplog.at_level(logging.DEBUG, logger="conductor.executor.mcp_step"):
            await executor.execute(agent, {}, manager)  # type: ignore[arg-type]
        assert any(
            "content" in record.message and "is_error" in record.message
            for record in caplog.records
            if record.levelno == logging.DEBUG
        )

    async def test_non_dict_structured_is_not_merged(self, executor: McpStepExecutor) -> None:
        # Requirement: a None (or otherwise non-dict) structured payload leaves the
        # envelope untouched.
        manager = FakeMCPManager(envelope={"content": [], "structured": None, "is_error": False})
        agent = make_agent()
        result = await executor.execute(agent, {}, manager)  # type: ignore[arg-type]
        assert result == {"content": [], "structured": None, "is_error": False}

    async def test_outputs_errors_keys_are_reserved_from_flattening(
        self, executor: McpStepExecutor
    ) -> None:
        # Requirement: structured keys named ``outputs``/``errors`` are never
        # flattened onto the envelope — WorkflowContext duck-types
        # parallel/for-each group outputs by exactly those two top-level
        # keys, so flattening them would misclassify this step's output as a
        # group output in every context mode (and confuse for-each source
        # resolution). They stay reachable under ``structured``.
        manager = FakeMCPManager(
            envelope={
                "content": [{"type": "text", "text": "ok"}],
                "structured": {"outputs": [1, 2], "errors": [], "answer": 7},
                "is_error": False,
            }
        )
        agent = make_agent()
        result = await executor.execute(agent, {}, manager)  # type: ignore[arg-type]
        assert "outputs" not in result
        assert "errors" not in result
        assert result["answer"] == 7
        assert result["structured"] == {"outputs": [1, 2], "errors": [], "answer": 7}
        assert result["is_error"] is False


class TestGroupOutputMisclassification:
    """A merged envelope must never read as a parallel/for-each group output."""

    async def test_envelope_with_structured_outputs_keys_keeps_output_wrapper(
        self, executor: McpStepExecutor
    ) -> None:
        # Requirement: stored in the workflow context, the envelope keeps its
        # normal ``.output`` wrapper — ``{{ step.output.is_error }}`` works
        # even when the tool's structured payload carries ``outputs`` /
        # ``errors`` keys of its own.
        from conductor.engine.context import WorkflowContext

        manager = FakeMCPManager(
            envelope={
                "content": [{"type": "text", "text": "ok"}],
                "structured": {"outputs": [1, 2], "errors": [], "answer": 7},
                "is_error": False,
            }
        )
        agent = make_agent()
        envelope = await executor.execute(agent, {}, manager)  # type: ignore[arg-type]

        context = WorkflowContext()
        context.store("call", envelope)
        built = context.build_for_agent("downstream", [])
        assert built["call"]["output"]["is_error"] is False
        assert built["call"]["output"]["answer"] == 7


class TestTimeoutAndErrors:
    """Timeout and error propagation contracts."""

    async def test_timeout_raises_execution_error(self, executor: McpStepExecutor) -> None:
        # Requirement: a call exceeding agent.timeout raises ExecutionError naming the
        # step and the timeout.
        manager = FakeMCPManager(delay=5.0)
        agent = make_agent(timeout=1)
        with pytest.raises(ExecutionError, match="lookup.*timed out after 1s"):
            await asyncio.wait_for(executor.execute(agent, {}, manager), timeout=10)  # type: ignore[arg-type]

    async def test_no_timeout_awaits_without_wait_for(self, executor: McpStepExecutor) -> None:
        # Requirement: without agent.timeout the call is awaited with no wait_for wrapper.
        manager = FakeMCPManager(delay=0.01)
        agent = make_agent()
        result = await executor.execute(agent, {}, manager)  # type: ignore[arg-type]
        assert result["is_error"] is False
        assert manager.calls  # the call went through

    async def test_manager_value_error_propagates(self, executor: McpStepExecutor) -> None:
        # Requirement: the manager's ValueError (unknown server/tool) propagates
        # unchanged so the engine can classify it.
        manager = FakeMCPManager(error=ValueError("Unknown server: srv"))
        agent = make_agent()
        with pytest.raises(ValueError, match="Unknown server: srv"):
            await executor.execute(agent, {}, manager)  # type: ignore[arg-type]


class TestMcpResultBytes:
    """Byte-exactness of the shared result-size contract."""

    def test_byte_exactness_against_manual_computation(self) -> None:
        # Requirement: mcp_result_bytes equals the length of the compact UTF-8 JSON
        # encoding of {"content": ..., "structured": ...}.
        content = [{"type": "text", "text": "hello"}]
        structured = {"a": 1}
        expected = len(
            json.dumps(
                {"content": content, "structured": structured},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        assert mcp_result_bytes(content, structured) == expected

    def test_multibyte_text_counts_utf8_bytes(self) -> None:
        # Requirement: multibyte characters are measured in UTF-8 bytes, not characters
        # (ensure_ascii=False keeps them as-is).
        content = [{"type": "text", "text": "こんにちは"}]
        measured = mcp_result_bytes(content, None)
        payload = '{"content":[{"type":"text","text":"こんにちは"}],"structured":null}'
        assert measured == len(payload.encode("utf-8"))
        assert measured > len(payload)  # 5 Japanese chars are 3 bytes each

    def test_none_structured_serializes_as_null(self) -> None:
        # Requirement: a None structured payload serializes as JSON null in the
        # measured payload.
        assert mcp_result_bytes([], None) == len(b'{"content":[],"structured":null}')

    def test_separators_are_compact(self) -> None:
        # Requirement: the measurement uses compact separators so it never depends on
        # default ", " / ": " formatting.
        measured = mcp_result_bytes([{"a": 1}], {"b": 2})
        assert measured == len(b'{"content":[{"a":1}],"structured":{"b":2}}')
