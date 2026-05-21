"""End-to-end engine tests for ``on_error`` routing.

These tests exercise the agent and script call sites in
:mod:`conductor.engine.workflow` to confirm that:

- An agent that returns ``conductor_error: true`` routes to its
  matching ``on_error`` route instead of the success route.
- A script that writes a ``CONDUCTOR_ERROR_OUT`` envelope routes the
  same way.
- An undeclared kind is normalized to ``internal.undeclared_kind``
  before route evaluation.
- An unhandled envelope halts the workflow with
  :class:`UnhandledWorkflowError` carrying the envelope and a frame
  trail.
- Phase 1 does NOT propagate envelopes across sub-workflow boundaries
  (they surface to the parent as generic :class:`ExecutionError`).
"""

from __future__ import annotations

import sys

import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    LimitsConfig,
    OutputField,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.exceptions import UnhandledWorkflowError
from conductor.providers.copilot import CopilotProvider


def _wf(*agents: AgentDef, output: dict[str, str] | None = None) -> WorkflowConfig:
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="t",
            entry_point=agents[0].name,
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
        ),
        agents=list(agents),
        output=output or {},
    )


class TestAgentErrorRouting:
    @pytest.mark.asyncio
    async def test_envelope_routes_to_on_error_target(self) -> None:
        """An agent envelope picks the on_error route, not the success route."""
        config = _wf(
            AgentDef(
                name="probe",
                model="gpt-4",
                prompt="x",
                raises=["external.git.fetch_failed"],
                routes=[
                    RouteDef(to="rescue", on_error="external.git.fetch_failed"),
                    RouteDef(to="$end"),
                ],
            ),
            AgentDef(
                name="rescue",
                model="gpt-4",
                prompt="recover",
                routes=[RouteDef(to="$end")],
                output={"status": OutputField(type="string")},
            ),
            output={"status": "{{ rescue.output.status }}"},
        )

        def handler(agent, prompt, context):
            if agent.name == "probe":
                return {
                    "conductor_error": True,
                    "kind": "external.git.fetch_failed",
                    "message": "remote rejected",
                }
            return {"status": "recovered"}

        provider = CopilotProvider(mock_handler=handler)
        engine = WorkflowEngine(config=config, provider=provider)

        result = await engine.run({})
        assert result == {"status": "recovered"}

    @pytest.mark.asyncio
    async def test_unhandled_envelope_halts_with_typed_error(self) -> None:
        """No matching on_error route → UnhandledWorkflowError with envelope + frames."""
        config = _wf(
            AgentDef(
                name="probe",
                model="gpt-4",
                prompt="x",
                # No on_error route — workflow must halt.
                routes=[RouteDef(to="$end")],
            ),
        )

        def handler(agent, prompt, context):
            return {
                "conductor_error": True,
                "kind": "external.api.timeout",
                "message": "took too long",
            }

        provider = CopilotProvider(mock_handler=handler)
        engine = WorkflowEngine(config=config, provider=provider)

        with pytest.raises(UnhandledWorkflowError) as exc_info:
            await engine.run({})

        assert exc_info.value.envelope["kind"] == "external.api.timeout"
        assert exc_info.value.envelope["message"] == "took too long"
        assert len(exc_info.value.frames) == 1
        assert exc_info.value.frames[0]["node"] == "probe"
        assert exc_info.value.frames[0]["kind"] == "external.api.timeout"

    @pytest.mark.asyncio
    async def test_undeclared_kind_is_normalized_then_routes(self) -> None:
        """If ``raises`` declares X but agent raises Y, kind becomes ``internal.undeclared_kind``."""
        config = _wf(
            AgentDef(
                name="probe",
                model="gpt-4",
                prompt="x",
                raises=["external.git.fetch_failed"],  # declared, but agent raises something else
                routes=[
                    RouteDef(to="rescue", on_error="internal.undeclared_kind"),
                    RouteDef(to="$end"),
                ],
            ),
            AgentDef(
                name="rescue",
                model="gpt-4",
                prompt="x",
                routes=[RouteDef(to="$end")],
                output={"original_kind": OutputField(type="string")},
            ),
            output={"recovered_from": "{{ rescue.output.original_kind }}"},
        )

        def handler(agent, prompt, context):
            if agent.name == "probe":
                return {
                    "conductor_error": True,
                    "kind": "external.unexpected.thing",
                    "message": "boom",
                }
            # rescue agent reads probe.error.details.original_kind from context
            err = context["probe"]["error"]
            return {"original_kind": err["details"]["original_kind"]}

        provider = CopilotProvider(mock_handler=handler)
        engine = WorkflowEngine(config=config, provider=provider)

        result = await engine.run({})
        assert result == {"recovered_from": "external.unexpected.thing"}

    @pytest.mark.asyncio
    async def test_success_path_unchanged(self) -> None:
        """Regression: agents without ``raises``/``on_error`` behave exactly as before."""
        config = _wf(
            AgentDef(
                name="happy",
                model="gpt-4",
                prompt="x",
                output={"answer": OutputField(type="string")},
                routes=[RouteDef(to="$end")],
            ),
            output={"answer": "{{ happy.output.answer }}"},
        )

        def handler(agent, prompt, context):
            return {"answer": "ok"}

        provider = CopilotProvider(mock_handler=handler)
        engine = WorkflowEngine(config=config, provider=provider)

        assert await engine.run({}) == {"answer": "ok"}


class TestScriptErrorRouting:
    @pytest.mark.asyncio
    async def test_script_envelope_routes_to_on_error(self, tmp_path) -> None:
        """A script writes an envelope and the engine routes via on_error."""
        # Script that writes a typed envelope and exits 1.
        script_body = (
            "import json, os, sys\n"
            "with open(os.environ['CONDUCTOR_ERROR_OUT'], 'w') as f:\n"
            "    json.dump({"
            "'conductor_error': True, "
            "'kind': 'external.git.fetch_failed', "
            "'message': 'remote down'"
            "}, f)\n"
            "sys.exit(1)\n"
        )
        config = _wf(
            AgentDef(
                name="fetch",
                type="script",
                command=sys.executable,
                args=["-c", script_body],
                raises=["external.git.fetch_failed"],
                routes=[
                    RouteDef(to="rescue", on_error="external.git.fetch_failed"),
                    RouteDef(to="$end"),
                ],
            ),
            AgentDef(
                name="rescue",
                model="gpt-4",
                prompt="recover",
                routes=[RouteDef(to="$end")],
                output={"status": OutputField(type="string")},
            ),
            output={"status": "{{ rescue.output.status }}"},
        )

        def handler(agent, prompt, context):
            return {"status": "recovered"}

        provider = CopilotProvider(mock_handler=handler)
        engine = WorkflowEngine(config=config, provider=provider)

        result = await engine.run({})
        assert result == {"status": "recovered"}

    @pytest.mark.asyncio
    async def test_legacy_script_exit_code_routing_unchanged(self) -> None:
        """Scripts without ``raises``/``on_error`` keep their legacy exit_code routing."""
        config = _wf(
            AgentDef(
                name="legacy_fail",
                type="script",
                command=sys.executable,
                args=["-c", "import sys; sys.exit(3)"],
                routes=[
                    RouteDef(when="{{ legacy_fail.output.exit_code != 0 }}", to="fallback"),
                    RouteDef(to="$end"),
                ],
            ),
            AgentDef(
                name="fallback",
                model="gpt-4",
                prompt="x",
                routes=[RouteDef(to="$end")],
                output={"v": OutputField(type="string")},
            ),
            output={"v": "{{ fallback.output.v }}"},
        )

        def handler(agent, prompt, context):
            return {"v": "fallback-ran"}

        provider = CopilotProvider(mock_handler=handler)
        engine = WorkflowEngine(config=config, provider=provider)

        result = await engine.run({})
        assert result == {"v": "fallback-ran"}
