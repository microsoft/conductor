"""Real-API integration test that reproduces tool-call argument truncation.

This test exercises the actual failure mode reported by users: when an agent
asks the Copilot model to emit a tool call with a large argument (e.g.,
``create`` with multi-KB ``file_text``) under the SDK's default non-streaming
mode, the model's per-turn output budget is exhausted mid-JSON and the CLI
silently executes the partial tool call. The model sees the tool succeed
with no content, retries the same broken call, and loops indefinitely until
the wall clock fires.

This is the empirical regression test for the ``streaming=True`` fix in
``CopilotProvider`` (see ``src/conductor/providers/copilot.py``).

Run with:
    pytest -m real_api tests/test_integration/test_copilot_large_write.py

The test is opt-in (``real_api`` marker, deselected by default) because:
- It requires the bundled or user-installed ``copilot`` CLI to be present
  and authenticated (no separate API key — uses the user's GitHub Copilot
  entitlement).
- It makes real model calls (consumes Copilot quota).
- It can take 5-10 minutes per case (success path runs to ~5 min;
  failure path is capped by ``max_session_seconds``).

Empirically verified red→green on this repo:
- Without ``streaming=True``: 9m08s wall-clock failure, 0 bytes written
  (``ProviderError: tool 'create' was executing``).
- With ``streaming=True``: 4m57s success, 62 KB written in a single
  ``create`` call.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from conductor.config.schema import (
    AgentDef,
    OutputField,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.providers.copilot import CopilotProvider

# Lower bound on the file we expect the agent to produce. Empirically the
# bug starts to bite reliably above ~30 KB of intended content; below that,
# the per-turn output budget is usually large enough to fit the tool call
# even without streaming, so the test would not be a true regression
# guard. See module docstring for the measured red→green numbers.
_MIN_BYTES_WRITTEN = 30 * 1024

# Wall-clock cap for the agent. Long enough to allow the success case
# (~5 min in our measurements) and short enough to fail fast when the
# bug is present (vs. the 1800s default).
_MAX_SESSION_SECONDS = 480.0


def _has_copilot_cli() -> bool:
    """Return True if a ``copilot`` CLI binary appears reachable.

    The SDK ships its own bundled binary, so this is a coarse check — we
    treat the test as runnable if either a user-installed ``copilot`` is
    on PATH or the bundled SDK binary exists on disk.
    """
    if shutil.which("copilot"):
        return True
    try:
        import copilot as _copilot_pkg

        bundled = Path(_copilot_pkg.__file__).parent / "bin" / "copilot"
        return bundled.exists()
    except Exception:
        return False


def _build_large_write_workflow(target_path: Path) -> WorkflowConfig:
    """Build a workflow that asks an agent to write a ~50 KB file in one call.

    The prompt explicitly forbids splitting across multiple tool calls so
    the agent is forced into the single-large-tool-call shape that
    triggers the truncation bug under non-streaming.
    """
    prompt = (
        f"Write a comprehensive ~50 KB markdown document about "
        f"{{{{ workflow.input.topic }}}} and save it to ``{target_path}`` "
        "using the ``create`` tool in a SINGLE call. Do not split the "
        "write across multiple tool calls.\n\n"
        "The document must include:\n"
        "- A title and a multi-paragraph introduction (at least 4 paragraphs).\n"
        "- At least 20 numbered sections, each with 4-6 substantive paragraphs.\n"
        "- At least three markdown tables.\n"
        "- At least eight bulleted lists.\n"
        "- Inline code examples or pseudocode in at least 5 sections.\n"
        "- A detailed conclusion section (at least 4 paragraphs).\n\n"
        "Aim for substantive content of approximately 50,000 characters. "
        "Do not produce placeholder text or 'lorem ipsum' — write real, "
        "detailed content about the topic.\n\n"
        "After the file is written, return the absolute path and the "
        "approximate byte count as your output."
    )
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="copilot-large-write-regression",
            description=(
                "Forces a single large `create` tool call to exercise the "
                "tool-argument truncation bug guarded by streaming=True."
            ),
            entry_point="writer",
            runtime=RuntimeConfig(provider="copilot"),
        ),
        agents=[
            AgentDef(
                name="writer",
                model="claude-opus-4.7-1m-internal",
                prompt=prompt,
                output={
                    "file_path": OutputField(type="string"),
                    "bytes_written": OutputField(type="number"),
                },
                routes=[RouteDef(to="$end")],
                max_session_seconds=_MAX_SESSION_SECONDS,
            )
        ],
    )


@pytest.mark.real_api
@pytest.mark.asyncio
async def test_large_create_tool_call_does_not_truncate(tmp_path: Path) -> None:
    """An agent must be able to write a multi-tens-of-KB file in one ``create``.

    Empirical regression guard for the ``streaming=True`` fix.

    Without the fix, this test fails with one of:
    - ``ProviderError`` ("Session exceeded maximum duration ... tool 'create'
      was executing"), or
    - the produced file being absent or far smaller than ``_MIN_BYTES_WRITTEN``
      because the model's tool-call ``file_text`` argument was truncated.

    With the fix, the file exists and is at least ``_MIN_BYTES_WRITTEN``.
    """
    if not _has_copilot_cli():
        pytest.skip("Copilot CLI not available — skipping real-API test")

    target = tmp_path / "large-write-test.md"
    workflow = _build_large_write_workflow(target)

    provider = CopilotProvider()
    try:
        engine = WorkflowEngine(workflow, provider)
        await engine.run({"topic": "the architecture of multi-agent workflow systems"})
    finally:
        await provider.close()

    assert target.exists(), (
        f"Expected the writer agent to create {target} via the `create` tool, "
        "but it does not exist. This is the symptom of tool-call argument "
        "truncation: the model emitted a partial tool_use block, the CLI "
        "executed it without (or with a truncated) `file_text` arg, and "
        "nothing valid was written. Likely cause: `streaming=True` was not "
        "passed to `CopilotClient.create_session`."
    )

    bytes_written = target.stat().st_size
    assert bytes_written >= _MIN_BYTES_WRITTEN, (
        f"File was created but is only {bytes_written} bytes (expected "
        f"at least {_MIN_BYTES_WRITTEN}). This is consistent with the "
        "model's tool-call argument being truncated mid-stream, leaving "
        "only a tiny prefix of the intended `file_text` payload. Likely "
        "cause: `streaming=True` was not passed to "
        "`CopilotClient.create_session`."
    )
