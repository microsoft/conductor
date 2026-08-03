"""Session-continuity tests for the Claude Agent SDK provider.

Covers the per-agent ``session_key`` contract: executions sharing a key
resume one underlying Claude session, a key that resolves to a transcript
the CLI can no longer find degrades to a fresh session rather than
aborting the run, and the recorded map round-trips through a checkpoint.
"""

from __future__ import annotations

import json
import os
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
from conductor.providers.claude_agent_sdk import (  # noqa: E402
    _SESSION_KEY_NAMESPACE,
    ClaudeAgentSdkProvider,
)


def _ck(session_key: str, cwd: str | None = None) -> str:
    """Build the namespaced checkpoint key the provider exports."""
    return f"{_SESSION_KEY_NAMESPACE}{json.dumps([session_key, cwd or os.getcwd()])}"


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

    def __init__(
        self,
        session_ids: list[str],
        crash_after_assistant: bool = False,
        stop_after_prefix: bool = False,
    ) -> None:
        self._session_ids = list(session_ids)
        self._next = 0
        self._crash = crash_after_assistant
        self._stop_after_prefix = stop_after_prefix
        self.prefix_messages: list[Any] = []
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
        prefix = list(self.prefix_messages)
        stop_after_prefix = self._stop_after_prefix

        async def gen():
            for msg in prefix:
                yield msg
            if stop_after_prefix:
                raise RuntimeError("died during startup")
            yield _assistant("working", session_id)
            if crash:
                raise RuntimeError("boom")
            yield _result("done", session_id)

        return gen()


@contextmanager
def _sdk(recorder: _Recorder, session_exists: bool = True):
    """Patch the SDK surface the provider touches.

    The transcript probe is stubbed here rather than the SDK lookups it wraps:
    these tests use synthetic session ids with no files on disk, so the real
    probe would (correctly) refuse every one of them. The probe's own
    behaviour against real transcripts is covered by
    :class:`TestTranscriptProbe`.
    """
    probe = Mock(return_value=session_exists)
    with (
        patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True),
        patch("conductor.providers.claude_agent_sdk.query", recorder),
        patch.object(ClaudeAgentSdkProvider, "_session_transcript_exists", staticmethod(probe)),
    ):
        yield probe


async def _run(provider: ClaudeAgentSdkProvider, name: str, session_key: str | None) -> Any:
    agent = AgentDef(name=name, prompt="go", session_key=session_key)
    return await provider.execute(agent=agent, context={}, rendered_prompt="go")


class TestSessionCapture:
    async def test_records_session_id_under_the_key(self) -> None:
        rec = _Recorder(["sess-1"])
        with _sdk(rec):
            provider = ClaudeAgentSdkProvider()
            await _run(provider, "analyze", "investigation")

        assert provider.get_session_ids() == {_ck("investigation"): "sess-1"}

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

        assert provider.get_session_ids() == {_ck("investigation"): "sess-1"}

    async def test_get_session_ids_returns_a_copy(self) -> None:
        rec = _Recorder(["sess-1"])
        with _sdk(rec):
            provider = ClaudeAgentSdkProvider()
            await _run(provider, "analyze", "investigation")

        provider.get_session_ids()["investigation"] = "tampered"
        assert provider.get_session_ids() == {_ck("investigation"): "sess-1"}


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
        assert provider.get_session_ids() == {_ck("alpha"): "sess-1", _ck("beta"): "sess-2"}

    async def test_unkeyed_agent_never_resumes(self) -> None:
        rec = _Recorder(["sess-1"])
        with _sdk(rec):
            provider = ClaudeAgentSdkProvider()
            await _run(provider, "analyze", "investigation")
            await _run(provider, "other", None)

        assert rec.resumes == [None, None]

    async def test_existence_check_is_scoped_to_the_working_directory(self) -> None:
        rec = _Recorder(["sess-1"])
        with _sdk(rec) as probe:
            provider = ClaudeAgentSdkProvider()
            await _run(provider, "analyze", "investigation")
            await _run(provider, "analyze", "investigation")

        probe.assert_called_once_with("sess-1", os.getcwd())


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
        assert provider.get_session_ids() == {_ck("investigation"): "sess-2"}

    async def test_missing_transcript_is_logged(self, caplog: Any) -> None:
        rec = _Recorder(["sess-1"])
        with _sdk(rec, session_exists=False):
            provider = ClaudeAgentSdkProvider()
            await _run(provider, "analyze", "investigation")
            with caplog.at_level("WARNING"):
                await _run(provider, "analyze", "investigation")

        assert "could not be found" in caplog.text

    async def test_survives_an_sdk_without_session_lookup(self) -> None:
        """A sub-floor SDK lacking the lookup helpers must not disable resume."""
        rec = _Recorder(["sess-1"])
        with (
            patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True),
            patch("conductor.providers.claude_agent_sdk.query", rec),
            patch("conductor.providers.claude_agent_sdk.get_session_info", None),
            patch("conductor.providers.claude_agent_sdk.project_key_for_directory", None),
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
            provider.set_resume_session_ids({_ck("investigation"): "sess-1"})
            await _run(provider, "analyze", "investigation")

        assert rec.resumes == ["sess-1"]

    async def test_ids_recorded_this_run_win_over_restored_ones(self) -> None:
        rec = _Recorder(["sess-live"])
        with _sdk(rec):
            provider = ClaudeAgentSdkProvider()
            await _run(provider, "analyze", "investigation")
            provider.set_resume_session_ids({_ck("investigation"): "sess-stale"})
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
    def test_declares_session_continuity(self) -> None:
        assert ClaudeAgentSdkProvider.CAPABILITIES.session_continuity is True

    def test_session_continuity_is_not_listed_as_a_limitation(self) -> None:
        assert (
            "no session_key continuity"
            not in ClaudeAgentSdkProvider.CAPABILITIES.declared_limitations()
        )

    def test_under_claims_blanket_checkpoint_resume(self) -> None:
        """Sessions do survive resume, but only for agents that opted in.

        The flag is a blanket promise the startup banner reads out, so it
        stays False; ``session_continuity`` carries the granular claim.
        """
        assert ClaudeAgentSdkProvider.CAPABILITIES.checkpoint_resume is False
