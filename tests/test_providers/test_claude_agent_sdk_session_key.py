"""Session-continuity tests for the Claude Agent SDK provider.

Covers the per-agent ``session_key`` contract: executions sharing a key resume
one Claude session, an unfindable transcript degrades to a fresh session rather
than aborting the run, and the map round-trips through a checkpoint.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
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

    The transcript probe is stubbed rather than the SDK lookups it wraps: these
    tests use synthetic session ids with no files on disk, so the real probe
    would (correctly) refuse every one. :class:`TestTranscriptProbe` covers it.
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
        """``--resume`` on a transcript the CLI cannot find aborts it *before*
        the agent runs, so an unresolvable id must be dropped, not passed on.
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

    async def test_sdk_without_session_lookup_does_not_resume_unverified(self) -> None:
        """A sub-floor SDK degrades to a fresh session: handing an unverifiable
        id to ``--resume`` would abort the CLI before the agent runs.
        """
        rec = _Recorder(["sess-1", "sess-2"])
        with (
            patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True),
            patch("conductor.providers.claude_agent_sdk.query", rec),
            patch("conductor.providers.claude_agent_sdk.get_session_info", None),
            patch("conductor.providers.claude_agent_sdk.project_key_for_directory", None),
        ):
            provider = ClaudeAgentSdkProvider()
            await _run(provider, "analyze", "investigation")
            await _run(provider, "analyze", "investigation")

        assert rec.resumes == [None, None]


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
        """Sessions survive resume, but only for agents that opted in, so this
        blanket flag stays False; ``session_continuity`` is the granular claim.
        """
        assert ClaudeAgentSdkProvider.CAPABILITIES.checkpoint_resume is False


class _Hook:
    """A hook frame. Carries its own ``session_id``, unrelated to the conversation."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.hook_name = "SessionStart:resume"


class TestSessionIdProvenance:
    async def test_hook_frames_do_not_shadow_the_real_session(self) -> None:
        """A ``SessionStart`` hook emits frames carrying a session id of their
        own before the first assistant turn. Recording one would point the map
        at an id with no transcript, breaking continuity for every later pass.
        """
        rec = _Recorder(["sess-real"])
        rec.prefix_messages = [_Hook("hook-stray")]
        with _sdk(rec):
            provider = ClaudeAgentSdkProvider()
            await _run(provider, "analyze", "investigation")

        assert provider.get_session_ids() == {_ck("investigation"): "sess-real"}

    async def test_hook_frame_alone_records_nothing(self) -> None:
        rec = _Recorder(["sess-real"], stop_after_prefix=True)
        rec.prefix_messages = [_Hook("hook-stray")]
        with _sdk(rec):
            provider = ClaudeAgentSdkProvider()
            with pytest.raises(ProviderError):
                await _run(provider, "analyze", "investigation")

        assert provider.get_session_ids() == {}

    async def test_session_recorded_when_interrupted(self) -> None:
        """An interrupted agent is one whose context is worth resuming."""
        rec = _Recorder(["sess-1"])
        interrupt = asyncio.Event()
        interrupt.set()
        with _sdk(rec):
            provider = ClaudeAgentSdkProvider()
            agent = AgentDef(name="analyze", prompt="go", session_key="investigation")
            out = await provider.execute(
                agent=agent,
                context={},
                rendered_prompt="go",
                interrupt_signal=interrupt,
            )

        assert out.partial is True
        assert provider.get_session_ids() == {_ck("investigation"): "sess-1"}


class TestWorkingDirScoping:
    async def test_same_key_under_different_cwds_does_not_stomp(self, tmp_path: Any) -> None:
        """Transcripts are stored per directory, so these cannot share a
        session. Keying on the label alone made each overwrite the other's id.
        """
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        rec = _Recorder(["sess-a", "sess-b"])
        with _sdk(rec):
            provider = ClaudeAgentSdkProvider()
            for cwd in (str(dir_a), str(dir_b), str(dir_a), str(dir_b)):
                agent = AgentDef(name="analyze", prompt="go", session_key="shared", working_dir=cwd)
                await provider.execute(agent=agent, context={}, rendered_prompt="go")

        assert rec.resumes == [None, None, "sess-a", "sess-b"]
        assert provider.get_session_ids() == {
            _ck("shared", str(dir_a)): "sess-a",
            _ck("shared", str(dir_b)): "sess-b",
        }


class TestMapHygiene:
    async def test_unkeyed_execution_does_not_clear_the_map(self) -> None:
        rec = _Recorder(["sess-1", "sess-2"])
        with _sdk(rec):
            provider = ClaudeAgentSdkProvider()
            await _run(provider, "analyze", "investigation")
            await _run(provider, "analyze", None)

        assert provider.get_session_ids() == {_ck("investigation"): "sess-1"}

    async def test_empty_restore_map_is_harmless(self) -> None:
        rec = _Recorder(["sess-1"])
        with _sdk(rec):
            provider = ClaudeAgentSdkProvider()
            provider.set_resume_session_ids({})
            await _run(provider, "analyze", "investigation")

        assert rec.resumes == [None]

    async def test_malformed_restore_entries_are_ignored(self) -> None:
        """The docstring promises unreadable entries are skipped, not raised."""
        rec = _Recorder(["sess-1"])
        with _sdk(rec):
            provider = ClaudeAgentSdkProvider()
            provider.set_resume_session_ids({f"{_SESSION_KEY_NAMESPACE}not-json": "sess-x"})
            await _run(provider, "analyze", "investigation")

        assert rec.resumes == [None]

    async def test_probe_is_skipped_when_there_is_nothing_to_resume(self) -> None:
        rec = _Recorder(["sess-1"])
        with _sdk(rec) as probe:
            provider = ClaudeAgentSdkProvider()
            await _run(provider, "analyze", "investigation")

        probe.assert_not_called()


class TestTranscriptProbe:
    """``_session_transcript_exists`` against real transcripts on disk.

    Every other test in this file stubs the probe, so without these the guard
    would have no coverage at all.
    """

    @staticmethod
    def _write(project_dir: Any, session_id: str, prompt: str) -> None:
        rows = [
            {
                "type": "user",
                "uuid": str(uuid.uuid4()),
                "parentUuid": None,
                "sessionId": session_id,
                "timestamp": "2026-08-01T00:00:00.000Z",
                "message": {"role": "user", "content": prompt},
            }
        ]
        (project_dir / f"{session_id}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n"
        )

    @pytest.fixture
    def claude_home(self, tmp_path: Any, monkeypatch: Any):
        from claude_agent_sdk import project_key_for_directory

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
        repo = tmp_path / "repo"
        repo.mkdir()
        project_dir = tmp_path / "cfg" / "projects" / project_key_for_directory(str(repo))
        project_dir.mkdir(parents=True)
        return repo, project_dir

    def test_finds_a_real_transcript(self, claude_home: Any) -> None:
        repo, project_dir = claude_home
        sid = str(uuid.uuid4())
        self._write(project_dir, sid, "hello")
        assert ClaudeAgentSdkProvider._session_transcript_exists(sid, str(repo)) is True

    def test_missing_transcript_is_reported_absent(self, claude_home: Any) -> None:
        repo, _ = claude_home
        assert (
            ClaudeAgentSdkProvider._session_transcript_exists(str(uuid.uuid4()), str(repo)) is False
        )

    def test_large_first_prompt_is_still_found(self, claude_home: Any) -> None:
        """``get_session_info`` cannot summarise this one and returns None;
        treating that as "transcript gone" would disable continuity for good.
        """
        repo, project_dir = claude_home
        sid = str(uuid.uuid4())
        self._write(project_dir, sid, "x" * 200_000)

        from claude_agent_sdk import get_session_info

        assert get_session_info(sid, directory=str(repo)) is None
        assert ClaudeAgentSdkProvider._session_transcript_exists(sid, str(repo)) is True

    def test_transcript_from_another_directory_is_not_claimed(self, claude_home: Any) -> None:
        """The CLI refuses ``--resume`` across project dirs, so the probe must
        too. ``get_session_info`` searches sibling git worktrees, which would
        report the session present and turn the fallback into a hard abort.
        """
        repo, project_dir = claude_home
        sid = str(uuid.uuid4())
        self._write(project_dir, sid, "hello")
        other = repo.parent / "other"
        other.mkdir()
        assert ClaudeAgentSdkProvider._session_transcript_exists(sid, str(other)) is False

    def test_non_uuid_id_does_not_raise(self, claude_home: Any) -> None:
        repo, _ = claude_home
        assert ClaudeAgentSdkProvider._session_transcript_exists("not-a-uuid", str(repo)) is False


class TestMultiResume:
    async def test_restored_ids_are_re_exported(self) -> None:
        """Continuity must survive more than one resume: a checkpoint taken
        before the keyed agent runs again must not drop the restored id.
        """
        provider = ClaudeAgentSdkProvider()
        stored = {_ck("investigation", "/proj"): "sess-1"}
        provider.set_resume_session_ids(stored)

        assert provider.get_session_ids() == stored

    async def test_ids_recorded_this_run_override_restored_ones_on_export(self) -> None:
        """A superseded entry must not linger: the restored transcript is gone,
        so the next checkpoint must carry the live id, not the dead one.
        """
        rec = _Recorder(["sess-live"])
        with _sdk(rec, session_exists=False):
            provider = ClaudeAgentSdkProvider()
            provider.set_resume_session_ids({_ck("investigation"): "sess-stale"})
            await _run(provider, "analyze", "investigation")

        assert rec.resumes == [None]
        assert provider.get_session_ids() == {_ck("investigation"): "sess-live"}
