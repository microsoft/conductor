"""Session-continuity tests for the Claude Agent SDK provider.

Covers the per-agent ``session_key`` contract: executions sharing a key
resume one underlying Claude session, a key that resolves to a transcript
the CLI can no longer find degrades to a fresh session rather than
aborting the run, and the recorded map round-trips through a checkpoint.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import Mock, patch

import pytest

pytest.importorskip(
    "claude_agent_sdk",
    reason="claude-agent-sdk extra not installed (pip install conductor[claude-agent-sdk])",
)

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ResultMessage,
    TextBlock,
)

from conductor.config.schema import AgentDef  # noqa: E402
from conductor.exceptions import ProviderError  # noqa: E402
from conductor.providers.claude_agent_sdk import ClaudeAgentSdkProvider  # noqa: E402


def _assistant(text: str, session_id: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="claude-sonnet-4-5",
        session_id=session_id,
    )


def _result(text: str, session_id: str) -> ResultMessage:
    return ResultMessage(
        subtype="result",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=session_id,
        result=text,
    )


class _Recorder:
    """Stand-in for ``query`` that records the options it was handed."""

    def __init__(self, session_ids: list[str], crash_after_assistant: bool = False) -> None:
        self._session_ids = list(session_ids)
        self._next = 0
        self._crash = crash_after_assistant
        self.calls: list[Any] = []

    @property
    def resumes(self) -> list[str | None]:
        return [getattr(o, "resume", None) for o in self.calls]

    def __call__(self, *, prompt: str, options: Any) -> Any:
        del prompt
        self.calls.append(options)
        resumed = getattr(options, "resume", None)
        if resumed:
            # ``fork_session`` defaults to False, so a resumed session keeps
            # its id rather than being issued a new one.
            session_id = resumed
        else:
            session_id = self._session_ids[min(self._next, len(self._session_ids) - 1)]
            self._next += 1
        crash = self._crash

        async def gen():
            yield _assistant("working", session_id)
            if crash:
                raise RuntimeError("boom")
            yield _result("done", session_id)

        return gen()


@contextmanager
def _sdk(recorder: _Recorder, session_exists: bool = True):
    """Patch the SDK surface the provider touches.

    ``get_session_info`` must be patched in nearly every test: the real one
    consults ``~/.claude/projects`` and would find none of these synthetic
    ids, so the provider would correctly refuse to resume them.
    """
    info = Mock() if session_exists else None
    with (
        patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True),
        patch("conductor.providers.claude_agent_sdk.query", recorder),
        patch(
            "conductor.providers.claude_agent_sdk.get_session_info",
            return_value=info,
        ) as info_mock,
    ):
        yield info_mock


async def _run(provider: ClaudeAgentSdkProvider, name: str, session_key: str | None) -> Any:
    agent = AgentDef(name=name, prompt="go", session_key=session_key)
    return await provider.execute(agent=agent, context={}, rendered_prompt="go")


class TestSessionCapture:
    async def test_records_session_id_under_the_key(self) -> None:
        rec = _Recorder(["sess-1"])
        with _sdk(rec):
            provider = ClaudeAgentSdkProvider()
            await _run(provider, "analyze", "investigation")

        assert provider.get_session_ids() == {"investigation": "sess-1"}

    async def test_records_nothing_without_a_session_key(self) -> None:
        rec = _Recorder(["sess-1"])
        with _sdk(rec):
            provider = ClaudeAgentSdkProvider()
            await _run(provider, "analyze", None)

        assert provider.get_session_ids() == {}
        assert rec.resumes == [None]

    async def test_records_session_even_when_the_run_crashes(self) -> None:
        """The mid-run casualty is exactly the session worth resuming."""
        rec = _Recorder(["sess-1"], crash_after_assistant=True)
        with _sdk(rec):
            provider = ClaudeAgentSdkProvider()
            with pytest.raises(ProviderError):
                await _run(provider, "analyze", "investigation")

        assert provider.get_session_ids() == {"investigation": "sess-1"}

    async def test_get_session_ids_returns_a_copy(self) -> None:
        rec = _Recorder(["sess-1"])
        with _sdk(rec):
            provider = ClaudeAgentSdkProvider()
            await _run(provider, "analyze", "investigation")

        provider.get_session_ids()["investigation"] = "tampered"
        assert provider.get_session_ids() == {"investigation": "sess-1"}


class TestSessionReuse:
    async def test_re_execution_resumes_the_same_session(self) -> None:
        """The loop-back case: second run of one agent continues its session."""
        rec = _Recorder(["sess-1"])
        with _sdk(rec):
            provider = ClaudeAgentSdkProvider()
            await _run(provider, "analyze", "investigation")
            await _run(provider, "analyze", "investigation")

        assert rec.resumes == [None, "sess-1"]

    async def test_a_different_agent_sharing_the_key_resumes_it(self) -> None:
        rec = _Recorder(["sess-1"])
        with _sdk(rec):
            provider = ClaudeAgentSdkProvider()
            await _run(provider, "analyze", "investigation")
            await _run(provider, "report", "investigation")

        assert rec.resumes == [None, "sess-1"]

    async def test_distinct_keys_do_not_share_a_session(self) -> None:
        rec = _Recorder(["sess-1", "sess-2"])
        with _sdk(rec):
            provider = ClaudeAgentSdkProvider()
            await _run(provider, "analyze", "alpha")
            await _run(provider, "review", "beta")
            await _run(provider, "analyze", "alpha")

        assert rec.resumes == [None, None, "sess-1"]
        assert provider.get_session_ids() == {"alpha": "sess-1", "beta": "sess-2"}

    async def test_unkeyed_agent_never_resumes(self) -> None:
        rec = _Recorder(["sess-1"])
        with _sdk(rec):
            provider = ClaudeAgentSdkProvider()
            await _run(provider, "analyze", "investigation")
            await _run(provider, "other", None)

        assert rec.resumes == [None, None]

    async def test_existence_check_is_scoped_to_the_working_directory(self) -> None:
        rec = _Recorder(["sess-1"])
        with _sdk(rec) as info_mock:
            provider = ClaudeAgentSdkProvider()
            await _run(provider, "analyze", "investigation")
            await _run(provider, "analyze", "investigation")

        _, kwargs = info_mock.call_args
        assert kwargs["directory"]


class TestUnresolvableSession:
    async def test_missing_transcript_falls_back_to_a_fresh_session(self) -> None:
        """Regression guard.

        ``--resume`` on a transcript the CLI cannot find aborts it *before*
        the agent runs, so an id that no longer resolves must be dropped
        rather than passed through.
        """
        rec = _Recorder(["sess-1", "sess-2"])
        with _sdk(rec, session_exists=False):
            provider = ClaudeAgentSdkProvider()
            await _run(provider, "analyze", "investigation")
            output = await _run(provider, "analyze", "investigation")

        assert rec.resumes == [None, None]
        assert output.content == {"response": "working"}
        # The newer session replaces the one that could not be resumed.
        assert provider.get_session_ids() == {"investigation": "sess-2"}

    async def test_missing_transcript_is_logged(self, caplog: Any) -> None:
        rec = _Recorder(["sess-1"])
        with _sdk(rec, session_exists=False):
            provider = ClaudeAgentSdkProvider()
            await _run(provider, "analyze", "investigation")
            with caplog.at_level("WARNING"):
                await _run(provider, "analyze", "investigation")

        assert "no longer available" in caplog.text

    async def test_survives_an_sdk_without_session_lookup(self) -> None:
        """Older SDKs may not export ``get_session_info``; degrade, don't crash."""
        rec = _Recorder(["sess-1"])
        with (
            patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True),
            patch("conductor.providers.claude_agent_sdk.query", rec),
            patch("conductor.providers.claude_agent_sdk.get_session_info", None),
        ):
            provider = ClaudeAgentSdkProvider()
            await _run(provider, "analyze", "investigation")
            await _run(provider, "analyze", "investigation")

        assert rec.resumes == [None, "sess-1"]


class TestCheckpointRoundTrip:
    async def test_restored_ids_are_resumed(self) -> None:
        rec = _Recorder(["sess-2"])
        with _sdk(rec):
            provider = ClaudeAgentSdkProvider()
            provider.set_resume_session_ids({"investigation": "sess-1"})
            await _run(provider, "analyze", "investigation")

        assert rec.resumes == ["sess-1"]

    async def test_ids_recorded_this_run_win_over_restored_ones(self) -> None:
        rec = _Recorder(["sess-live"])
        with _sdk(rec):
            provider = ClaudeAgentSdkProvider()
            await _run(provider, "analyze", "investigation")
            provider.set_resume_session_ids({"investigation": "sess-stale"})
            await _run(provider, "analyze", "investigation")

        assert rec.resumes == [None, "sess-live"]

    async def test_restored_ids_for_other_keys_are_ignored(self) -> None:
        """A foreign key-space (e.g. a Copilot map) must simply miss."""
        rec = _Recorder(["sess-1"])
        with _sdk(rec):
            provider = ClaudeAgentSdkProvider()
            provider.set_resume_session_ids({"some_agent_name": "copilot-sid"})
            await _run(provider, "analyze", "investigation")

        assert rec.resumes == [None]

    async def test_map_round_trips_across_provider_instances(self) -> None:
        rec = _Recorder(["sess-1"])
        with _sdk(rec):
            first = ClaudeAgentSdkProvider()
            await _run(first, "analyze", "investigation")

        checkpointed = first.get_session_ids()

        rec2 = _Recorder(["sess-2"])
        with _sdk(rec2):
            second = ClaudeAgentSdkProvider()
            second.set_resume_session_ids(checkpointed)
            await _run(second, "analyze", "investigation")

        assert rec2.resumes == ["sess-1"]


class TestCapability:
    def test_declares_checkpoint_resume(self) -> None:
        assert ClaudeAgentSdkProvider.CAPABILITIES.checkpoint_resume is True

    def test_checkpoint_resume_is_not_listed_as_a_limitation(self) -> None:
        assert (
            "no checkpoint resume" not in ClaudeAgentSdkProvider.CAPABILITIES.declared_limitations()
        )
