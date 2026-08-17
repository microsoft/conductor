"""End-to-end coverage for ``session_key`` continuity.

``tests/test_providers/test_claude_agent_sdk_session_key.py`` exercises the map
and the resume guard directly. These tests pin the two properties the feature
depends on that are invisible from the provider: ``ProviderRegistry`` hands back
the *same provider instance* on a loop-back (per-execution creation would leave
every provider-level test passing while the feature no-ops), and the duck-typed
checkpoint hop (``_write_checkpoint`` → ``copilot_session_ids`` →
``set_resume_session_ids``) connects — it is held together by ``hasattr`` calls
no type checker can verify.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

pytest.importorskip(
    "claude_agent_sdk",
    reason="claude-agent-sdk extra not installed (pip install conductor[claude-agent-sdk])",
)

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock  # noqa: E402

from conductor.config.loader import load_workflow  # noqa: E402
from conductor.engine.workflow import WorkflowEngine  # noqa: E402
from conductor.providers.claude_agent_sdk import (  # noqa: E402
    _SESSION_KEY_NAMESPACE,
    ClaudeAgentSdkProvider,
)
from conductor.providers.registry import ProviderRegistry  # noqa: E402

_WORKFLOW = """
workflow:
  name: session-key-loopback
  entry_point: investigate
  runtime:
    provider: claude-agent-sdk
    default_model: claude-sonnet-4-5
  limits:
    max_iterations: 12
agents:
  - name: investigate
    session_key: investigation
    prompt: investigate
    output:
      finding:
        type: string
    routes:
      - to: verify
  - name: verify
    type: script
    command: sh
    args: ["-c", "n=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); \
echo $n > {counter}; [ $n -ge 3 ] && exit 0 || exit 1"]
    routes:
      - to: investigate
        when: "{{{{ verify.output.exit_code != 0 }}}}"
      - to: summarize
  - name: summarize
    session_key: investigation
    prompt: summarize
    output:
      summary:
        type: string
    routes:
      - to: $end
output:
  summary: "{{{{ summarize.output.summary }}}}"
"""


class _FakeQuery:
    """Records the ``resume`` argument handed to each SDK invocation."""

    def __init__(self) -> None:
        self.resumes: list[str | None] = []
        self._issued = 0

    def __call__(self, *, prompt: str, options: Any) -> Any:
        del prompt
        resumed = getattr(options, "resume", None)
        self.resumes.append(resumed)
        if resumed:
            session_id = resumed
        else:
            self._issued += 1
            session_id = f"sess-{self._issued}"

        async def gen():
            yield AssistantMessage(
                content=[TextBlock(text="working")],
                model="claude-sonnet-4-5",
                session_id=session_id,
            )
            yield ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=session_id,
                structured_output={"finding": "f", "summary": "s"},
            )

        return gen()


@pytest.fixture
def workflow_file(tmp_path: Path) -> Path:
    # as_posix(): the path is interpolated into a double-quoted YAML scalar,
    # where a Windows backslash reads as an escape ("C:\Users" -> invalid \U)
    # and fails to parse. Forward slashes are also what the `sh` in the script
    # step expects, so one change covers both.
    counter = tmp_path.joinpath("counter").as_posix()
    path = tmp_path / "loopback.yaml"
    path.write_text(_WORKFLOW.format(counter=counter))
    return path


class TestLoopBackContinuity:
    async def test_loop_back_and_hand_off_share_one_session(self, workflow_file: Path) -> None:
        """``investigate`` runs three times via a script-gated loop-back and
        ``summarize`` inherits the same session — four SDK calls, one session.
        """
        fake = _FakeQuery()
        config = load_workflow(workflow_file)

        async with ProviderRegistry(config, mcp_servers=None) as registry:
            engine = WorkflowEngine(config, registry=registry, workflow_path=workflow_file)
            with (
                patch("conductor.providers.claude_agent_sdk.query", fake),
                patch.object(
                    ClaudeAgentSdkProvider,
                    "_session_transcript_exists",
                    staticmethod(Mock(return_value=True)),
                ),
            ):
                await engine.run({})
            provider = await registry._get_or_create_provider(registry.default_provider_type)

        assert fake.resumes == [None, "sess-1", "sess-1", "sess-1"]
        assert list(provider.get_session_ids().values()) == ["sess-1"]

    async def test_registry_reuses_one_provider_instance(self, workflow_file: Path) -> None:
        """Pins the property the whole feature rests on."""
        config = load_workflow(workflow_file)
        async with ProviderRegistry(config, mcp_servers=None) as registry:
            first = await registry.get_provider(config.agents[0])
            second = await registry.get_provider(config.agents[-1])
        assert first is second


class TestCheckpointWiring:
    async def test_checkpoint_captures_the_session_map(
        self, workflow_file: Path, tmp_path: Path, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
        config = load_workflow(workflow_file)
        provider = ClaudeAgentSdkProvider()
        provider._session_ids[("investigation", "/repo")] = "sess-1"

        engine = WorkflowEngine(config, provider=provider, workflow_path=workflow_file)
        path = engine._write_checkpoint(error=None, trigger="periodic")

        assert path is not None
        saved = json.loads(Path(path).read_text())["copilot_session_ids"]
        assert saved == {f'{_SESSION_KEY_NAMESPACE}["investigation", "/repo"]': "sess-1"}

    async def test_registry_restores_the_map_into_a_new_provider(self, workflow_file: Path) -> None:
        config = load_workflow(workflow_file)
        stored = {f'{_SESSION_KEY_NAMESPACE}["investigation", "/repo"]': "sess-1"}

        async with ProviderRegistry(config, mcp_servers=None) as registry:
            registry.set_resume_session_ids(stored)
            provider = await registry._get_or_create_provider(registry.default_provider_type)

        assert provider._resume_session_ids == {("investigation", "/repo"): "sess-1"}


class TestMixedProviderCheckpoint:
    """A second provider exposing ``get_session_ids`` must not evict the first."""

    async def test_both_providers_maps_survive(self, workflow_file: Path) -> None:
        config = load_workflow(workflow_file)

        claude = ClaudeAgentSdkProvider()
        claude._session_ids[("investigate", "/repo")] = "claude-sid"

        # A Copilot-shaped provider: agent-name keys, plus the cwd hook.
        copilot = Mock()
        copilot.get_session_ids.return_value = {"investigate": "copilot-sid"}
        copilot.get_session_cwds.return_value = {"investigate": "/repo"}

        registry = Mock()
        registry.get_active_providers.return_value = {
            "claude-agent-sdk": claude,
            "copilot": copilot,
        }

        engine = WorkflowEngine(config, registry=registry, workflow_path=workflow_file)
        with patch("conductor.engine.checkpoint.CheckpointManager.save_checkpoint") as save:
            save.return_value = Path("/tmp/cp.json")
            engine._write_checkpoint(error=None, trigger="periodic")

        ids = save.call_args.kwargs["copilot_session_ids"]
        cwds = save.call_args.kwargs["copilot_session_cwds"]

        # Copilot's agent-name key and ours coexist: namespacing keeps the
        # identical label "investigate" from colliding.
        assert ids == {
            "investigate": "copilot-sid",
            f'{_SESSION_KEY_NAMESPACE}["investigate", "/repo"]': "claude-sid",
        }
        assert cwds == {"investigate": "/repo"}

    async def test_each_provider_ignores_the_others_entries(self) -> None:
        claude = ClaudeAgentSdkProvider()
        claude.set_resume_session_ids({"investigate": "copilot-sid"})
        assert claude._resume_session_ids == {}

    async def test_a_raising_provider_costs_only_its_own_sessions(
        self, workflow_file: Path
    ) -> None:
        """One bad provider must not discard every provider's map.

        A single ``try`` around the merge loop sent the first raise to the
        outer handler, which sets ``copilot_session_ids = None`` — so a Claude
        failure silently took Copilot's resumable sessions with it, and the
        partially merged local was thrown away.
        """
        config = load_workflow(workflow_file)

        broken = Mock()
        broken.get_session_ids.side_effect = RuntimeError("provider exploded")

        copilot = Mock()
        copilot.get_session_ids.return_value = {"investigate": "copilot-sid"}
        copilot.get_session_cwds.return_value = {"investigate": "/repo"}

        registry = Mock()
        registry.get_active_providers.return_value = {"broken": broken, "copilot": copilot}

        engine = WorkflowEngine(config, registry=registry, workflow_path=workflow_file)
        with patch("conductor.engine.checkpoint.CheckpointManager.save_checkpoint") as save:
            save.return_value = Path("/tmp/cp.json")
            engine._write_checkpoint(error=None, trigger="periodic")

        assert save.call_args.kwargs["copilot_session_ids"] == {"investigate": "copilot-sid"}
        assert save.call_args.kwargs["copilot_session_cwds"] == {"investigate": "/repo"}

    async def test_the_failure_names_the_provider_that_raised(
        self, workflow_file: Path, caplog: Any
    ) -> None:
        config = load_workflow(workflow_file)
        broken = Mock()
        broken.get_session_ids.side_effect = RuntimeError("provider exploded")
        registry = Mock()
        registry.get_active_providers.return_value = {"broken": broken}

        engine = WorkflowEngine(config, registry=registry, workflow_path=workflow_file)
        with (
            patch("conductor.engine.checkpoint.CheckpointManager.save_checkpoint") as save,
            caplog.at_level("WARNING"),
        ):
            save.return_value = Path("/tmp/cp.json")
            engine._write_checkpoint(error=None, trigger="periodic")

        assert "'broken'" in caplog.text
        # Still saved, just without that provider's sessions.
        assert save.call_args.kwargs["copilot_session_ids"] == {}


_PARENT_FANOUT = """
workflow:
  name: parent-fanout
  entry_point: seed
  runtime:
    provider: claude-agent-sdk
    default_model: claude-sonnet-4-5
  limits:
    max_iterations: 20
for_each:
  - name: fan
    type: for_each
    source: seed.output.items
    as: item
    max_concurrent: 2
    agent:
      name: runner
      type: workflow
      workflow: ./child.yaml
      input_mapping:
        topic: "{{ item }}"
    routes:
      - to: $end
agents:
  - name: seed
    type: set
    values:
      items: '["alpha", "beta"]'
    routes:
      - to: fan
output:
  count: "{{ fan.outputs | length }}"
"""

_CHILD_KEYED = """
workflow:
  name: child-keyed
  entry_point: work
  runtime:
    provider: claude-agent-sdk
    default_model: claude-sonnet-4-5
  input:
    topic:
      type: string
      required: true
agents:
  - name: work
    session_key: shared
    prompt: "work on {{ workflow.input.topic }}"
    output:
      answer:
        type: string
    routes:
      - to: $end
output:
  answer: "{{ work.output.answer }}"
"""


class _SlowQuery:
    """Holds each call open long enough for a sibling iteration to start."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *, prompt: str, options: Any) -> Any:
        del prompt
        self.calls += 1
        session_id = getattr(options, "resume", None) or f"sess-{self.calls}"

        async def gen():
            await asyncio.sleep(0.2)
            yield AssistantMessage(
                content=[TextBlock(text="working")],
                model="claude-sonnet-4-5",
                session_id=session_id,
            )
            yield ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=session_id,
                structured_output={"answer": "a"},
            )

        return gen()


class TestConcurrentSubWorkflowGuard:
    """The hole ``conductor validate`` cannot see.

    A ``type: workflow`` agent cannot itself carry a ``session_key``, so the
    static shared-key check never fires for one — yet ``_execute_subworkflow``
    passes the parent's registry down, so a concurrent ``for_each`` over a
    sub-workflow whose *inner* agent is keyed ends up resuming one session from
    two iterations at once. Only the provider's in-flight guard sees it.
    """

    @pytest.fixture
    def fanout_workflow(self, tmp_path: Path) -> Path:
        (tmp_path / "child.yaml").write_text(_CHILD_KEYED)
        parent = tmp_path / "parent.yaml"
        parent.write_text(_PARENT_FANOUT)
        return parent

    async def test_overlapping_iterations_are_refused(self, fanout_workflow: Path) -> None:
        slow = _SlowQuery()
        config = load_workflow(fanout_workflow)

        async with ProviderRegistry(config, mcp_servers=None) as registry:
            engine = WorkflowEngine(config, registry=registry, workflow_path=fanout_workflow)
            with (
                patch("conductor.providers.claude_agent_sdk.query", slow),
                patch.object(
                    ClaudeAgentSdkProvider,
                    "_session_transcript_exists",
                    staticmethod(Mock(return_value=True)),
                ),
                pytest.raises(Exception) as exc,
            ):
                await engine.run({})

        assert "still running" in str(exc.value)
        # The second iteration never reached the SDK.
        assert slow.calls == 1

    async def test_a_serial_fan_out_is_unaffected(self, fanout_workflow: Path) -> None:
        """The guard must catch overlap, not sharing: the same workflow run one
        iteration at a time is the supported way to reuse a session.
        """
        serial = fanout_workflow.read_text().replace("max_concurrent: 2", "max_concurrent: 1")
        fanout_workflow.write_text(serial)

        slow = _SlowQuery()
        config = load_workflow(fanout_workflow)

        async with ProviderRegistry(config, mcp_servers=None) as registry:
            engine = WorkflowEngine(config, registry=registry, workflow_path=fanout_workflow)
            with (
                patch("conductor.providers.claude_agent_sdk.query", slow),
                patch.object(
                    ClaudeAgentSdkProvider,
                    "_session_transcript_exists",
                    staticmethod(Mock(return_value=True)),
                ),
            ):
                await engine.run({})

        assert slow.calls == 2
