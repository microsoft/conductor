"""Cross-engine integration test for the ``CONDUCTOR_ERROR_OUT`` contract.

Phase 1 acceptance #1: a script-type node that writes the typed error
envelope to ``$CONDUCTOR_ERROR_OUT`` and exits 0 causes the node to be
marked errored, regardless of *which* script engine produced the
envelope. The brief calls for at least pwsh-on-Windows,
bash-on-Linux, and python on both.

This module exercises the contract through the real ``WorkflowEngine``
(no mocking the script executor) with three small writer scripts in
different languages, sharing one workflow YAML shape. Each test runs
only if the corresponding interpreter is on ``PATH``; CI matrices that
provide pwsh and bash on every OS will execute all three.
"""

from __future__ import annotations

import shutil
import sys
import textwrap

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
from conductor.providers.copilot import CopilotProvider


def _build_workflow(probe: AgentDef) -> WorkflowConfig:
    """Wrap a probe script node in a minimal workflow with one rescue agent.

    Every engine variant shares this shape so the only thing under test
    is the script's envelope writing — not the surrounding workflow.
    """
    rescue = AgentDef(
        name="rescue",
        model="gpt-4",
        prompt="rescue from {{ probe.error.kind }}",
        routes=[RouteDef(to="$end")],
        output={"recovered_kind": OutputField(type="string")},
    )
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="xeng",
            entry_point="probe",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
        ),
        agents=[probe, rescue],
        output={"recovered_kind": "{{ rescue.output.recovered_kind }}"},
    )


def _make_handler():
    def handler(agent, prompt, context):
        if agent.name == "rescue":
            err = context["probe"]["error"]
            return {"recovered_kind": err["kind"]}
        return {}

    return handler


class TestCrossEngineEnvelope:
    """One test per script engine; same expected behaviour from the workflow."""

    @pytest.mark.asyncio
    async def test_python_writes_envelope_and_routes(self) -> None:
        """Python writer: the contract works without any helper at all."""
        probe = AgentDef(
            name="probe",
            type="script",
            command=sys.executable,
            args=[
                "-c",
                "import json, os, sys; "
                "open(os.environ['CONDUCTOR_ERROR_OUT'], 'w').write("
                "json.dumps({'conductor_error': True, "
                "'kind': 'external.git.drift', "
                "'message': 'sha mismatch'})); "
                "sys.exit(0)",
            ],
            raises=["external.git.drift"],
            routes=[
                RouteDef(to="rescue", on_error="external.git.drift"),
                RouteDef(to="$end"),
            ],
        )
        engine = WorkflowEngine(
            config=_build_workflow(probe),
            provider=CopilotProvider(mock_handler=_make_handler()),
        )
        result = await engine.run({})
        assert result == {"recovered_kind": "external.git.drift"}

    @pytest.mark.asyncio
    @pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
    async def test_pwsh_writes_envelope_and_routes(self, tmp_path) -> None:
        """PowerShell writer using Set-Content with utf8 (no BOM)."""
        script = tmp_path / "raise.ps1"
        # PowerShell here-string assembles the envelope inline; we avoid
        # the shipped helper to confirm the bare contract works.
        script.write_text(
            textwrap.dedent(
                """\
                $envelope = @{
                    conductor_error = $true
                    kind            = 'external.git.drift'
                    message         = 'sha mismatch'
                } | ConvertTo-Json -Compress
                Set-Content -Path $env:CONDUCTOR_ERROR_OUT `
                    -Value $envelope -Encoding utf8 -NoNewline
                exit 0
                """
            ),
            encoding="utf-8",
        )
        probe = AgentDef(
            name="probe",
            type="script",
            command="pwsh",
            args=["-NoProfile", "-File", str(script)],
            raises=["external.git.drift"],
            routes=[
                RouteDef(to="rescue", on_error="external.git.drift"),
                RouteDef(to="$end"),
            ],
        )
        engine = WorkflowEngine(
            config=_build_workflow(probe),
            provider=CopilotProvider(mock_handler=_make_handler()),
        )
        result = await engine.run({})
        assert result == {"recovered_kind": "external.git.drift"}

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        shutil.which("bash") is None or sys.platform == "win32",
        reason="bash on Windows is typically a broken WSL shim; brief requires bash-on-Linux only",
    )
    async def test_bash_writes_envelope_and_routes(self, tmp_path) -> None:
        """Bash writer using a heredoc."""
        script = tmp_path / "raise.sh"
        script.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                cat > "$CONDUCTOR_ERROR_OUT" <<'JSON'
                {"conductor_error":true,"kind":"external.git.drift","message":"sha mismatch"}
                JSON
                exit 0
                """
            ),
            encoding="utf-8",
        )
        probe = AgentDef(
            name="probe",
            type="script",
            command="bash",
            args=[str(script)],
            raises=["external.git.drift"],
            routes=[
                RouteDef(to="rescue", on_error="external.git.drift"),
                RouteDef(to="$end"),
            ],
        )
        engine = WorkflowEngine(
            config=_build_workflow(probe),
            provider=CopilotProvider(mock_handler=_make_handler()),
        )
        result = await engine.run({})
        assert result == {"recovered_kind": "external.git.drift"}
