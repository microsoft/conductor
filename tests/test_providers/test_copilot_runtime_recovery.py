"""Tests for Copilot spawned-runtime death detection and recovery (issue #483).

Covers three cooperating pieces, all inside ``src/conductor/providers/copilot.py``:

- Detecting a dead spawned runtime via ``subprocess.Popen.poll()`` on the SDK's
  own child handle, plus explicit recognition of ``BrokenPipeError`` /
  ``ConnectionResetError`` at the SDK boundary.
- Recovering by rebuilding the client inside ``_ensure_client_started()``'s
  existing ``_start_lock``, so the existing retry loop lands the next attempt
  on a fresh runtime.
- Classifying the failure truthfully: an externally-owned runtime
  (``runtime_url`` / ``COPILOT_PROVIDER_RUNTIME_URL``) is never respawned and
  is non-retryable; a spawned runtime is restarted and is retryable.
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import conductor.providers.copilot as copilot_mod
from conductor.config.schema import AgentDef, ProviderSettings
from conductor.exceptions import ProviderError
from conductor.providers.copilot import (
    _MAX_CONSECUTIVE_RUNTIME_RESTARTS,
    CopilotProvider,
    IdleRecoveryConfig,
    RetryConfig,
    SDKResponse,
    _is_broken_pipe_error,
)


def _fake_popen(exit_code: int | None) -> MagicMock:
    """Build a ``subprocess.Popen``-shaped double with a scriptable ``poll()``.

    Uses ``spec=subprocess.Popen`` so ``isinstance(process, subprocess.Popen)``
    (the check ``_spawned_runtime_process`` uses to reject test doubles that
    don't represent a real spawned child) passes, matching the real SDK's
    ``_cli_process`` attribute, which is always a genuine ``subprocess.Popen``.
    """
    process = MagicMock(spec=subprocess.Popen)
    process.poll.return_value = exit_code
    return process


class _FakeClient:
    """Minimal stand-in for the Copilot SDK client.

    Deliberately a plain class (not a bare ``MagicMock``/``AsyncMock``) so
    attribute access on it does not auto-vivify a truthy ``_cli_process`` --
    the pitfall that made unconfigured mocks in other test files look like a
    live spawned runtime before the ``isinstance(..., subprocess.Popen)``
    guard was added.
    """

    def __init__(self, cli_process: MagicMock | None = None) -> None:
        self._cli_process = cli_process
        self.start = AsyncMock()
        self.stop = AsyncMock()
        self.create_session = AsyncMock()


class _FakeSession:
    session_id = "fake-session"

    def __init__(self) -> None:
        self.disconnect = AsyncMock()


def _agent(name: str = "a") -> AgentDef:
    return AgentDef(name=name, model="gpt-4o", prompt="p")


class TestIsBrokenPipeError:
    """Unit tests for the module-level ``_is_broken_pipe_error`` helper."""

    def test_bare_broken_pipe_error(self) -> None:
        assert _is_broken_pipe_error(BrokenPipeError("broken")) is True

    def test_broken_pipe_nested_via_cause(self) -> None:
        wrapper = RuntimeError("write failed")
        wrapper.__cause__ = BrokenPipeError("broken")
        assert _is_broken_pipe_error(wrapper) is True

    def test_broken_pipe_nested_via_context(self) -> None:
        wrapper = RuntimeError("write failed")
        wrapper.__context__ = BrokenPipeError("broken")
        assert _is_broken_pipe_error(wrapper) is True

    def test_connection_reset_error(self) -> None:
        assert _is_broken_pipe_error(ConnectionResetError("reset")) is True

    def test_unrelated_error_returns_false(self) -> None:
        assert _is_broken_pipe_error(ValueError("nope")) is False

    def test_unrelated_error_with_unrelated_cause_returns_false(self) -> None:
        wrapper = RuntimeError("failed")
        wrapper.__cause__ = ValueError("nope")
        assert _is_broken_pipe_error(wrapper) is False


class TestRuntimeIsDead:
    """Unit tests for ``_runtime_is_dead`` / ``_spawned_runtime_process``."""

    def test_false_when_poll_returns_none(self) -> None:
        provider = CopilotProvider()
        provider._client = _FakeClient(cli_process=_fake_popen(None))
        assert provider._runtime_is_dead() is False

    def test_true_when_poll_returns_exit_code(self) -> None:
        provider = CopilotProvider()
        provider._client = _FakeClient(cli_process=_fake_popen(1))
        assert provider._runtime_is_dead() is True

    def test_false_when_external_runtime_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The Q1 carve-out: an external runtime is never treated as dead
        here, even if a ``_cli_process``-shaped attribute happens to exist,
        because this provider does not own its lifecycle."""
        provider = CopilotProvider(
            provider_settings=ProviderSettings(name="copilot", runtime_url="localhost:3000")
        )
        provider._client = _FakeClient(cli_process=_fake_popen(1))
        assert provider._runtime_is_dead() is False

    def test_false_when_cli_process_absent(self) -> None:
        """FFI in-process mode: no OS child process at all."""
        provider = CopilotProvider()
        provider._client = _FakeClient(cli_process=None)
        assert provider._runtime_is_dead() is False

    def test_false_when_client_not_yet_built(self) -> None:
        provider = CopilotProvider()
        assert provider._client is None
        assert provider._runtime_is_dead() is False

    def test_false_when_cli_process_is_not_a_real_popen(self) -> None:
        """A generic Mock (as used by many pre-existing test doubles) must
        not be mistaken for a live spawned runtime."""
        provider = CopilotProvider()
        provider._client = _FakeClient(cli_process=MagicMock())
        assert provider._runtime_is_dead() is False


class TestEnsureClientStartedRebuildsOnDeath:
    """``_ensure_client_started`` rebuilds the client when the child died."""

    @pytest.mark.asyncio
    async def test_rebuilds_via_build_client_and_starts_new_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old_client = _FakeClient(cli_process=_fake_popen(1))
        new_client = _FakeClient(cli_process=_fake_popen(None))

        provider = CopilotProvider()
        provider._client = old_client
        provider._started = True

        build_client = MagicMock(return_value=new_client)
        monkeypatch.setattr(provider, "_build_client", build_client)

        await provider._ensure_client_started()

        # The stale client must never be re-started (the SDK's start() is a
        # silent no-op once _state == "connected"); recovery must construct
        # and start a genuinely new client object.
        assert provider._client is new_client
        build_client.assert_called_once_with()
        old_client.stop.assert_awaited_once()
        new_client.start.assert_awaited_once()
        assert provider._started is True

    @pytest.mark.asyncio
    async def test_does_not_rebuild_when_child_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        alive_client = _FakeClient(cli_process=_fake_popen(None))

        provider = CopilotProvider()
        provider._client = alive_client
        provider._started = True

        build_client = MagicMock(side_effect=AssertionError("must not rebuild a live runtime"))
        monkeypatch.setattr(provider, "_build_client", build_client)

        await provider._ensure_client_started()

        assert provider._client is alive_client
        build_client.assert_not_called()
        alive_client.stop.assert_not_called()

    @pytest.mark.asyncio
    async def test_concurrent_callers_produce_exactly_one_rebuild(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old_client = _FakeClient(cli_process=_fake_popen(1))
        new_client = _FakeClient(cli_process=_fake_popen(None))

        provider = CopilotProvider()
        provider._client = old_client
        provider._started = True

        build_client = MagicMock(return_value=new_client)
        monkeypatch.setattr(provider, "_build_client", build_client)

        await asyncio.gather(
            provider._ensure_client_started(),
            provider._ensure_client_started(),
        )

        build_client.assert_called_once_with()
        new_client.start.assert_awaited_once()
        assert provider._consecutive_runtime_restarts == 1


class TestRuntimeUnavailableErrorClassification:
    """``_runtime_unavailable_error`` forks on external vs spawned (Q1)."""

    def test_external_runtime_is_not_retryable_and_names_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = CopilotProvider(
            provider_settings=ProviderSettings(name="copilot", runtime_url="localhost:3000")
        )

        error = provider._runtime_unavailable_error(BrokenPipeError("broken"))

        assert error.is_retryable is False
        assert "localhost:3000" in str(error)
        assert "orchestrator" in (error.suggestion or "")

    def test_spawned_runtime_is_retryable_and_names_death_not_install(self) -> None:
        provider = CopilotProvider()
        provider._client = _FakeClient(cli_process=_fake_popen(137))

        error = provider._runtime_unavailable_error(BrokenPipeError("broken"))

        assert error.is_retryable is True
        assert "died" in str(error)
        assert "installed and authenticated" not in str(error)
        assert "installed and authenticated" not in (error.suggestion or "")

    @pytest.mark.asyncio
    async def test_execute_sdk_call_classifies_broken_pipe_as_external(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A BrokenPipeError raised while talking to an external runtime must
        surface as non-retryable and must never trigger a client rebuild."""
        provider = CopilotProvider(
            provider_settings=ProviderSettings(name="copilot", runtime_url="localhost:3000")
        )
        fake_client = _FakeClient()
        fake_client.create_session = AsyncMock(side_effect=BrokenPipeError("broken"))
        provider._client = fake_client
        provider._started = True

        build_client = MagicMock(side_effect=AssertionError("must never respawn"))
        monkeypatch.setattr(provider, "_build_client", build_client)

        agent = _agent()
        with pytest.raises(ProviderError) as exc_info:
            await provider.execute(agent=agent, context={}, rendered_prompt="p")

        assert exc_info.value.is_retryable is False
        assert "localhost:3000" in str(exc_info.value)
        build_client.assert_not_called()
        # Non-retryable — the retry loop must not have attempted a second call.
        fake_client.create_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_execute_sdk_call_classifies_broken_pipe_as_spawned(self) -> None:
        # The child is still alive as far as poll() is concerned (the pipe
        # broke mid-call, before the OS has reaped the process) — this is
        # the realistic race the exception-classification path exists for,
        # distinct from the poll-detects-death path exercised elsewhere.
        #
        # max_attempts=1 means the retry loop's own "all retries exhausted"
        # wrapper (which is always is_retryable=False, unrelated to this
        # change) is what the caller ultimately sees, so the classification
        # itself is asserted against the recorded retry-history entry rather
        # than the final wrapped exception.
        provider = CopilotProvider(retry_config=RetryConfig(max_attempts=1))
        fake_client = _FakeClient(cli_process=_fake_popen(None))
        fake_client.create_session = AsyncMock(side_effect=BrokenPipeError("broken"))
        provider._client = fake_client
        provider._started = True

        agent = _agent()
        with pytest.raises(ProviderError) as exc_info:
            await provider.execute(agent=agent, context={}, rendered_prompt="p")

        assert "died" in str(exc_info.value)
        assert "installed and authenticated" not in str(exc_info.value)
        assert len(provider._retry_history) == 1
        assert provider._retry_history[0]["is_retryable"] is True
        assert "died" in provider._retry_history[0]["error"]


class TestEndToEndRecovery:
    """A dead spawned runtime recovers on the next retry attempt."""

    @pytest.mark.asyncio
    async def test_recovers_on_retry_with_freshly_built_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("COPILOT_PROVIDER_RUNTIME_URL", raising=False)
        monkeypatch.delenv("COPILOT_PROVIDER_RUNTIME_TOKEN", raising=False)

        # The runtime died exactly when the broken pipe occurred: the child
        # is already reporting an exit code by the time the next attempt's
        # _ensure_client_started() checks liveness.
        dying_client = _FakeClient(cli_process=_fake_popen(1))
        dying_client.create_session = AsyncMock(side_effect=BrokenPipeError("broken"))

        healthy_client = _FakeClient(cli_process=_fake_popen(None))
        healthy_client.create_session = AsyncMock(return_value=_FakeSession())

        client_factory = MagicMock(side_effect=[dying_client, healthy_client])
        monkeypatch.setattr(copilot_mod, "CopilotClient", client_factory)

        provider = CopilotProvider(
            retry_config=RetryConfig(max_attempts=3, base_delay=0.0, jitter=0.0)
        )
        monkeypatch.setattr(
            provider,
            "_send_and_wait",
            AsyncMock(return_value=SDKResponse(content="ok")),
        )

        agent = _agent()
        result = await provider.execute(agent=agent, context={}, rendered_prompt="p")

        assert result.content == {"result": "ok"}
        assert client_factory.call_count == 2
        assert provider._client is healthy_client
        dying_client.create_session.assert_awaited_once()
        healthy_client.create_session.assert_awaited_once()
        dying_client.stop.assert_awaited_once()
        healthy_client.start.assert_awaited_once()
        # The successful call reset the restart budget.
        assert provider._consecutive_runtime_restarts == 0


class TestConsecutiveRestartCap:
    """Q2: the restart budget is a consecutive-failure cap reset by success."""

    @pytest.mark.asyncio
    async def test_cap_exceeded_after_max_consecutive_restarts_without_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = CopilotProvider()
        provider._client = _FakeClient(cli_process=_fake_popen(1))
        provider._started = True

        # Every rebuilt client is itself already dead — a crash loop.
        dead_clients = [_FakeClient(cli_process=_fake_popen(1)) for _ in range(5)]
        monkeypatch.setattr(provider, "_build_client", MagicMock(side_effect=dead_clients))

        for _ in range(_MAX_CONSECUTIVE_RUNTIME_RESTARTS):
            await provider._restart_spawned_runtime()

        with pytest.raises(ProviderError) as exc_info:
            await provider._restart_spawned_runtime()

        assert exc_info.value.is_retryable is False
        assert str(_MAX_CONSECUTIVE_RUNTIME_RESTARTS) in str(
            exc_info.value
        ) or f"{_MAX_CONSECUTIVE_RUNTIME_RESTARTS}" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_successful_call_resets_counter_so_later_death_still_restarts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = CopilotProvider(retry_config=RetryConfig(max_attempts=1))
        # Simulate restarts having already happened up to the cap.
        provider._consecutive_runtime_restarts = _MAX_CONSECUTIVE_RUNTIME_RESTARTS

        healthy_client = _FakeClient(cli_process=_fake_popen(None))
        healthy_client.create_session = AsyncMock(return_value=_FakeSession())
        provider._client = healthy_client
        provider._started = True

        monkeypatch.setattr(
            provider,
            "_send_and_wait",
            AsyncMock(return_value=SDKResponse(content="ok")),
        )

        agent = _agent()
        result = await provider.execute(agent=agent, context={}, rendered_prompt="p")
        assert result.content == {"result": "ok"}
        assert provider._consecutive_runtime_restarts == 0

        # A later death must be able to restart again — a fixed lifetime
        # budget (rather than Q2's consecutive-failure semantics) would
        # incorrectly still be exhausted here.
        next_dead_client = _FakeClient(cli_process=_fake_popen(1))
        monkeypatch.setattr(provider, "_build_client", MagicMock(return_value=next_dead_client))
        await provider._restart_spawned_runtime()
        assert provider._consecutive_runtime_restarts == 1


class TestIdleRecoveryDeadRuntime:
    """Root cause #3: a dead runtime is reported as such, not as a stuck agent."""

    @pytest.mark.asyncio
    async def test_dead_runtime_raises_runtime_unavailable_not_stuck(self) -> None:
        config = IdleRecoveryConfig(idle_timeout_seconds=0.05, max_recovery_attempts=2)
        provider = CopilotProvider(idle_recovery_config=config)
        provider._client = _FakeClient(cli_process=_fake_popen(1))
        provider._started = True

        done = asyncio.Event()  # never set
        mock_session = MagicMock()
        mock_session.send = AsyncMock()

        last_activity_ref: list[Any] = ["tool.execution_start", "slow_tool", 0.0]

        with pytest.raises(ProviderError) as exc_info:
            await provider._wait_with_idle_detection(
                done=done,
                session=mock_session,
                verbose_enabled=False,
                full_enabled=False,
                last_activity_ref=last_activity_ref,
            )

        assert "died" in str(exc_info.value) or "not running" in str(exc_info.value)
        assert "stuck" not in str(exc_info.value)
        assert exc_info.value.is_retryable is True
        mock_session.send.assert_not_awaited()
