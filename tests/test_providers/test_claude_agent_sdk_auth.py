"""Tests for ClaudeAgentSdkProvider authentication readiness."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

pytest.importorskip(
    "claude_agent_sdk",
    reason="claude-agent-sdk extra not installed",
)

from conductor.config.schema import AgentDef, OutputField, ProviderSettings
from conductor.exceptions import ProviderError
from conductor.providers.claude_agent_sdk import (
    ClaudeAgentSdkProvider,
    ClaudeAuthStatus,
    _find_claude_cli,
    _run_auth_status_subprocess,
)
from conductor.providers.factory import create_provider

FAKE_CLI = Path("/fake/claude")


# Every credential and backend selector the provider's auth modes read. The
# developer's (or a hostile CI's) values must never reach readiness.
_AUTH_VARIABLES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _AUTH_VARIABLES}


class TestFindClaudeCli:
    def test_returns_none_when_not_found(self) -> None:
        with (
            patch("shutil.which", return_value=None),
            patch("pathlib.Path.exists", return_value=False),
            patch("pathlib.Path.is_file", return_value=False),
        ):
            result = _find_claude_cli()
            assert result is None

    def test_returns_path_when_on_path(self) -> None:
        with patch("shutil.which", return_value="/usr/local/bin/claude"):
            result = _find_claude_cli()
            assert result is not None

    @pytest.mark.parametrize(
        "error",
        [RuntimeError("no home"), OSError("home unavailable")],
    )
    def test_unresolvable_home_does_not_raise(self, error: Exception) -> None:
        """Platform-independent regression for the Windows CI failures.

        With the environment cleared, Windows has no ``USERPROFILE`` /
        ``HOMEDRIVE`` to derive a home from, so ``Path.home()`` raises
        ``RuntimeError``; the POSIX password-database lookup raises
        ``OSError``. Both must skip the home-anchored fallbacks rather than
        propagate, so readiness still reports "Claude CLI not found".
        """
        with (
            patch("shutil.which", return_value=None),
            patch("pathlib.Path.home", side_effect=error),
            patch("pathlib.Path.exists", return_value=False),
            patch("pathlib.Path.is_file", return_value=False),
        ):
            assert _find_claude_cli() is None


@pytest.mark.claude_auth_readiness_mocked
class TestClaudeAuthStatusAutoMode:
    @pytest.mark.asyncio
    async def test_auto_with_api_key_resolves_to_api_key_without_subprocess(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="auto")
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-fake"}, clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch("conductor.providers.claude_agent_sdk._run_auth_status_subprocess") as mock_run,
        ):
            status = await provider._check_auth_readiness()
            assert status.inferred_mode == "api_key"
            assert status.ready is True
            mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_with_api_key_but_no_cli_binary_is_not_ready(self) -> None:
        """auto + ANTHROPIC_API_KEY still needs a discoverable CLI: the SDK
        session is the ``claude`` binary, so a key alone cannot run anything.

        The availability check is non-spawning — asserted at the process
        creation seam rather than a private helper — and leaves
        ``_find_claude_cli`` with nothing to find, so it holds even in CI
        where the extra ships a bundled binary.
        """
        provider = ClaudeAgentSdkProvider(auth_mode="auto")
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-fake"}, clear=True),
            patch("shutil.which", return_value=None),
            patch("pathlib.Path.exists", return_value=False),
            patch("pathlib.Path.is_file", return_value=False),
            patch("asyncio.create_subprocess_exec") as mock_exec,
        ):
            status = await provider._check_auth_readiness()
            connected = await provider.validate_connection()

        assert status.requested_mode == "auto"
        assert status.inferred_mode == "api_key"
        assert status.ready is False
        assert status.error is not None
        assert "Claude CLI not found" in status.error
        assert "npm install -g @anthropic-ai/claude-code" in status.error
        assert connected is False
        assert provider._last_validation_error == status.error
        assert "sk-ant-fake" not in str(dataclasses.asdict(status))
        mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_with_api_key_and_cli_is_ready_without_spawning(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="auto")
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-fake"}, clear=True),
            patch("conductor.providers.claude_agent_sdk._find_claude_cli", return_value=FAKE_CLI),
            patch("asyncio.create_subprocess_exec") as mock_exec,
        ):
            status = await provider._check_auth_readiness()

        assert status.inferred_mode == "api_key"
        assert status.ready is True
        mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_without_api_key_resolves_to_subscription(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="auto")
        auth_json = json.dumps(
            {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "pro"}
        ).encode()
        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch(
                "conductor.providers.claude_agent_sdk._run_auth_status_subprocess",
                AsyncMock(return_value=(auth_json, b"", 0)),
            ),
        ):
            status = await provider._check_auth_readiness()
            assert status.inferred_mode == "subscription"
            assert status.ready is True
            assert status.subscription_type == "pro"


@pytest.mark.claude_auth_readiness_mocked
class TestClaudeAuthStatusSubscriptionMode:
    @pytest.mark.asyncio
    async def test_subscription_with_conflicting_api_key_overrides_and_proceeds(self) -> None:
        """A conflicting ANTHROPIC_API_KEY no longer fails readiness closed.

        subscription mode selects the child process's authentication path via a
        finalized child environment (see ``EffectiveAuthContext``),
        not by refusing to run — readiness proceeds to the CLI check as usual.
        """
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        auth_json = json.dumps({"loggedIn": True, "authMethod": "claude.ai"}).encode()
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-fake"}, clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch(
                "conductor.providers.claude_agent_sdk._run_auth_status_subprocess",
                AsyncMock(return_value=(auth_json, b"", 0)),
            ) as mock_run,
        ):
            status = await provider._check_auth_readiness()
            assert status.ready is True
            assert status.inferred_mode == "subscription"
            mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_subscription_with_conflicting_auth_token_overrides_and_proceeds(self) -> None:
        """A conflicting ANTHROPIC_AUTH_TOKEN no longer fails readiness closed."""
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        env = _clean_env() | {"ANTHROPIC_AUTH_TOKEN": "tok-fake"}
        auth_json = json.dumps({"loggedIn": True, "authMethod": "claude.ai"}).encode()
        with (
            patch.dict(os.environ, env, clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch(
                "conductor.providers.claude_agent_sdk._run_auth_status_subprocess",
                AsyncMock(return_value=(auth_json, b"", 0)),
            ) as mock_run,
        ):
            status = await provider._check_auth_readiness()
            assert status.ready is True
            assert status.inferred_mode == "subscription"
            mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_subscription_logged_in_passes(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        auth_json = json.dumps({"loggedIn": True, "authMethod": "claude.ai"}).encode()
        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch(
                "conductor.providers.claude_agent_sdk._run_auth_status_subprocess",
                AsyncMock(return_value=(auth_json, b"", 0)),
            ),
        ):
            status = await provider._check_auth_readiness()
            assert status.ready is True
            assert status.inferred_mode == "subscription"

    @pytest.mark.asyncio
    async def test_subscription_logged_out_fails_with_guidance(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        auth_json = json.dumps({"loggedIn": False}).encode()
        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch(
                "conductor.providers.claude_agent_sdk._run_auth_status_subprocess",
                AsyncMock(return_value=(auth_json, b"", 0)),
            ),
        ):
            status = await provider._check_auth_readiness()
            assert status.ready is False
            assert "claude auth login" in (status.error or "").lower()

    @pytest.mark.asyncio
    async def test_subscription_malformed_json_fails_sanitized(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch(
                "conductor.providers.claude_agent_sdk._run_auth_status_subprocess",
                AsyncMock(return_value=(b"not valid json", b"", 0)),
            ),
        ):
            status = await provider._check_auth_readiness()
            assert status.ready is False
            assert "not valid json" not in (status.error or "")

    @pytest.mark.asyncio
    async def test_subscription_nonzero_exit_fails_sanitized(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch(
                "conductor.providers.claude_agent_sdk._run_auth_status_subprocess",
                AsyncMock(return_value=(b"", b"", 1)),
            ),
        ):
            status = await provider._check_auth_readiness()
            assert status.ready is False

    @pytest.mark.asyncio
    async def test_status_json_email_org_fields_are_filtered(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        auth_json = json.dumps(
            {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "email": "user@example.com",
                "orgId": "org-secret",
                "accessToken": "tok-secret",
                "refreshToken": "refresh-secret",
            }
        ).encode()
        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch(
                "conductor.providers.claude_agent_sdk._run_auth_status_subprocess",
                AsyncMock(return_value=(auth_json, b"", 0)),
            ),
        ):
            status = await provider._check_auth_readiness()
            assert status.ready is True
            status_dict = dataclasses.asdict(status)
            assert "user@example.com" not in str(status_dict)
            assert "org-secret" not in str(status_dict)
            assert "tok-secret" not in str(status_dict)


@pytest.mark.claude_auth_readiness_mocked
class TestClaudeAuthStatusApiKeyMode:
    @pytest.mark.asyncio
    async def test_api_key_present_passes_without_key_value(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="api_key")
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-real"}, clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
        ):
            status = await provider._check_auth_readiness()
            assert status.ready is True
            assert status.inferred_mode == "api_key"
            assert "sk-ant-real" not in str(dataclasses.asdict(status))

    @pytest.mark.asyncio
    async def test_api_key_missing_fails(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="api_key")
        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
        ):
            status = await provider._check_auth_readiness()
            assert status.ready is False

    @pytest.mark.asyncio
    async def test_api_key_blank_treated_as_absent(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="api_key")
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "   "}, clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
        ):
            status = await provider._check_auth_readiness()
            assert status.ready is False


@pytest.mark.claude_auth_readiness_mocked
class TestAuthPreflightSubprocessTimeout:
    @pytest.mark.asyncio
    async def test_validate_connection_does_not_hang_on_timeout(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch(
                "conductor.providers.claude_agent_sdk._run_auth_status_subprocess",
                side_effect=TimeoutError,
            ),
        ):
            ok = await provider.validate_connection()
            assert ok is False
            assert provider.connection_error_hint is not None

    @pytest.mark.asyncio
    async def test_execute_auth_preflight_ignores_interrupt_signal_set_before_call(self) -> None:
        """An interrupt_signal already set before execute() no longer short-circuits.

        The interrupt-race against auth preflight was removed (finding 4): preflight
        now runs to completion via a plain bounded ``await auth_task``, and only the
        later message-streaming loop honors ``interrupt_signal``. So a signal that is
        set before the call does not by itself produce a partial result during the
        preflight — the auth check runs and, since it is not ready, execute() raises.
        """
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        interrupt = asyncio.Event()
        agent = AgentDef(
            name="test",
            type="agent",
            prompt="hello",
            output={"result": OutputField(type="string")},
        )

        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=None,
            ),
            patch("conductor.providers.claude_agent_sdk.query") as mock_query,
        ):
            interrupt.set()
            with pytest.raises(ProviderError):
                await provider.execute(
                    agent=agent,
                    context={},
                    rendered_prompt="hello",
                    interrupt_signal=interrupt,
                )
            mock_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_auth_preflight_cancellation_propagates(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        agent = AgentDef(
            name="test",
            type="agent",
            prompt="hello",
            output={"result": OutputField(type="string")},
        )

        async def slow_check(**kwargs: object) -> ClaudeAuthStatus:
            await asyncio.sleep(100)
            return ClaudeAuthStatus(
                requested_mode="subscription",
                inferred_mode="subscription",
                ready=True,
            )

        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch.object(provider, "_check_auth_readiness", slow_check),
        ):
            task = asyncio.create_task(
                provider.execute(
                    agent=agent,
                    context={},
                    rendered_prompt="hello",
                )
            )
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.claude_auth_readiness_mocked
class TestAuthCliPathNotShell:
    @pytest.mark.asyncio
    async def test_auth_status_uses_exec_not_shell(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        captured_calls: list[dict[str, object]] = []

        async def recording_create(*args: object, **kwargs: object) -> MagicMock:
            captured_calls.append({"args": args, "kwargs": kwargs})
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(
                return_value=(json.dumps({"loggedIn": True}).encode(), b"")
            )
            proc.kill = MagicMock()
            proc.wait = AsyncMock(return_value=0)
            return proc

        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch("asyncio.create_subprocess_exec", recording_create),
        ):
            await provider._check_auth_readiness()

        assert captured_calls
        for call in captured_calls:
            assert call["kwargs"].get("shell") is not True  # type: ignore[union-attr]


@pytest.mark.claude_auth_readiness_mocked
class TestAuthSubprocessSpawnRobustness:
    """Exercises the real ``_run_auth_status_subprocess`` body (not stubbed).

    Every other test that touches ``_run_auth_status_subprocess`` patches it
    out entirely, which proves nothing about cancellation or timeout
    handling *inside* the helper itself — only that callers clean up their
    own ``auth_task``. These two tests patch one level lower
    (``asyncio.create_subprocess_exec``) so the helper's own spawn/communicate
    cleanup arms actually run.
    """

    @pytest.mark.asyncio
    async def test_cancellation_during_spawn_reaps_process(self) -> None:
        """Cancelling while inside the spawn await must still reap the process.

        Regression test for cancellation landing during
        ``await asyncio.create_subprocess_exec(...)`` itself — before any
        ``process`` handle is bound in ``_run_auth_status_subprocess`` — as
        opposed to during the later ``communicate()`` wait, which the
        pre-existing tests already cover via a different path.
        """
        spawn_started = asyncio.Event()
        release_spawn = asyncio.Event()
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"{}", b""))
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)

        async def slow_create(*args: object, **kwargs: object) -> MagicMock:
            spawn_started.set()
            await release_spawn.wait()
            return proc

        with patch("asyncio.create_subprocess_exec", slow_create):
            task = asyncio.create_task(_run_auth_status_subprocess(FAKE_CLI, {}, "/work", ()))
            await spawn_started.wait()
            # Still inside the spawn await — no process handle exists yet in
            # the caller's frame. Cancel here, then let the shielded spawn
            # actually finish so the cancellation handler can recover and
            # reap the process instead of leaking it.
            task.cancel()
            release_spawn.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_timeout_during_communicate_kills_and_reaps_process(self) -> None:
        """A hung ``communicate()`` must actually be killed and reaped."""

        async def hang_forever() -> tuple[bytes, bytes]:
            await asyncio.sleep(100)
            return b"", b""

        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = MagicMock(side_effect=hang_forever)
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)

        async def fast_create(*args: object, **kwargs: object) -> MagicMock:
            return proc

        with (
            patch("asyncio.create_subprocess_exec", fast_create),
            patch("conductor.providers.claude_agent_sdk._CLAUDE_AUTH_TIMEOUT", 0.01),
            pytest.raises(TimeoutError),
        ):
            await _run_auth_status_subprocess(FAKE_CLI, {}, "/work", ())

        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_env_and_cwd_are_exactly_the_ones_given(self) -> None:
        """The helper passes its arguments through; it never reads live state."""
        captured: dict[str, object] = {}
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"{}", b""))

        async def recording_create(*args: object, **kwargs: object) -> MagicMock:
            captured["argv"] = args
            captured.update(kwargs)
            return proc

        with (
            patch.dict(os.environ, {"LIVE_ONLY": "1"}, clear=True),
            patch("asyncio.create_subprocess_exec", recording_create),
        ):
            await _run_auth_status_subprocess(
                FAKE_CLI, {"GIVEN": "1"}, "/given/cwd", ("project", "local")
            )

        assert captured["env"] == {"GIVEN": "1"}
        assert captured["cwd"] == "/given/cwd"
        # Root option before the subcommand: the CLI parses positionally.
        assert captured["argv"] == (
            str(FAKE_CLI),
            "--setting-sources=project,local",
            "auth",
            "status",
            "--json",
        )

    @pytest.mark.asyncio
    async def test_empty_selection_is_sent_explicitly(self) -> None:
        """No tiers is ``--setting-sources=``, never an omitted flag — omitting
        it makes the CLI load every ambient tier."""
        captured: dict[str, object] = {}
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"{}", b""))

        async def recording_create(*args: object, **kwargs: object) -> MagicMock:
            captured["argv"] = args
            return proc

        with patch("asyncio.create_subprocess_exec", recording_create):
            await _run_auth_status_subprocess(FAKE_CLI, {}, "/work", ())

        assert captured["argv"] == (str(FAKE_CLI), "--setting-sources=", "auth", "status", "--json")


@pytest.mark.claude_auth_readiness_mocked
class TestFactoryAuthModeWiring:
    @pytest.mark.asyncio
    async def test_factory_passes_auth_mode_subscription(self) -> None:
        settings = ProviderSettings(name="claude-agent-sdk", auth_mode="subscription")
        with (
            patch("conductor.providers.factory.CLAUDE_AGENT_SDK_AVAILABLE", True),
            patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True),
        ):
            provider = await create_provider(
                "claude-agent-sdk",
                validate=False,
                provider_settings=settings,
            )
        assert provider._auth_mode == "subscription"
        await provider.close()

    @pytest.mark.asyncio
    async def test_factory_passes_auth_mode_api_key(self) -> None:
        settings = ProviderSettings(name="claude-agent-sdk", auth_mode="api_key")
        with (
            patch("conductor.providers.factory.CLAUDE_AGENT_SDK_AVAILABLE", True),
            patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True),
        ):
            provider = await create_provider(
                "claude-agent-sdk",
                validate=False,
                provider_settings=settings,
            )
        assert provider._auth_mode == "api_key"
        await provider.close()

    @pytest.mark.asyncio
    async def test_factory_defaults_to_auto_without_settings(self) -> None:
        with (
            patch("conductor.providers.factory.CLAUDE_AGENT_SDK_AVAILABLE", True),
            patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True),
        ):
            provider = await create_provider("claude-agent-sdk", validate=False)
        assert provider._auth_mode == "auto"
        await provider.close()

    @pytest.mark.asyncio
    async def test_factory_preserves_temperature_rejection(self) -> None:
        settings = ProviderSettings(name="claude-agent-sdk", auth_mode="subscription")
        with (
            patch("conductor.providers.factory.CLAUDE_AGENT_SDK_AVAILABLE", True),
            patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True),
            pytest.raises(ProviderError, match="temperature"),
        ):
            await create_provider(
                "claude-agent-sdk",
                validate=False,
                provider_settings=settings,
                temperature=0.5,
            )

    @pytest.mark.asyncio
    async def test_factory_error_includes_hint_when_validate_fails(self) -> None:
        with (
            patch("conductor.providers.factory.CLAUDE_AGENT_SDK_AVAILABLE", True),
            patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True),
            patch(
                "conductor.providers.claude_agent_sdk.ClaudeAgentSdkProvider.validate_connection",
                AsyncMock(return_value=False),
            ),
            patch(
                "conductor.providers.claude_agent_sdk.ClaudeAgentSdkProvider.connection_error_hint",
                new_callable=PropertyMock,
                return_value="Authentication failed: Not logged in",
            ),
            pytest.raises(ProviderError, match="Not logged in"),
        ):
            await create_provider("claude-agent-sdk", validate=True)


@pytest.mark.claude_auth_readiness_mocked
class TestSecretHygiene:
    @pytest.mark.asyncio
    async def test_api_key_value_not_in_auth_status(self) -> None:
        canary = "sk-ant-CANARY-VALUE-12345"
        provider = ClaudeAgentSdkProvider(auth_mode="api_key")
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": canary}, clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
        ):
            status = await provider._check_auth_readiness()
        assert canary not in str(dataclasses.asdict(status))

    @pytest.mark.asyncio
    async def test_credential_env_vars_not_in_auth_status_on_subscription_conflict(self) -> None:
        """Subscription blanks an inherited key rather than failing closed, so the
        canary must reach neither the probe's environment nor the status."""
        canary_key = "sk-ant-CANARY-98765"
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        probe_env: dict[str, str] = {}

        async def fake_create(*args: object, **kwargs: object) -> MagicMock:
            probe_env.update(kwargs["env"])  # type: ignore[arg-type]
            proc = MagicMock()
            proc.returncode = 1
            proc.communicate = AsyncMock(return_value=(b'{"loggedIn": false}', b""))
            return proc

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": canary_key}, clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch("asyncio.create_subprocess_exec", fake_create),
        ):
            status = await provider._check_auth_readiness()
        assert probe_env["ANTHROPIC_API_KEY"] == ""
        assert canary_key not in str(dataclasses.asdict(status))


# ---------------------------------------------------------------------------
# Finding 1 — auto + ANTHROPIC_AUTH_TOKEN must NOT fail closed
# ---------------------------------------------------------------------------


@pytest.mark.claude_auth_readiness_mocked
class TestAutoModeAuthToken:
    @pytest.mark.asyncio
    async def test_auto_with_auth_token_only_resolves_to_subscription(self) -> None:
        """auto + ANTHROPIC_AUTH_TOKEN (no API key) must resolve to subscription.

        The env-var conflict guard is subscription-mode only.  With auth_mode="auto"
        and no ANTHROPIC_API_KEY, the mode should resolve subscription and call the
        CLI — not fail before reaching the subprocess.
        """
        provider = ClaudeAgentSdkProvider(auth_mode="auto")
        auth_json = json.dumps({"loggedIn": True, "authMethod": "claude.ai"}).encode()
        env = _clean_env() | {"ANTHROPIC_AUTH_TOKEN": "tok-fake"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch(
                "conductor.providers.claude_agent_sdk._run_auth_status_subprocess",
                AsyncMock(return_value=(auth_json, b"", 0)),
            ) as mock_run,
        ):
            status = await provider._check_auth_readiness()

        assert status.inferred_mode == "subscription"
        assert status.ready is True
        mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# Finding 3 — OSError during subprocess spawn returns sanitized failure
# ---------------------------------------------------------------------------


@pytest.mark.claude_auth_readiness_mocked
class TestSubprocessOsError:
    @pytest.mark.asyncio
    async def test_oserror_during_spawn_returns_sanitized_failure(self) -> None:
        """FileNotFoundError/PermissionError from create_subprocess_exec must not escape.

        The resolved CLI path exists according to _find_claude_cli but is not
        executable (e.g. a broken symlink, a filesystem race).  The OSError must
        be caught and returned as a sanitized non-ready ClaudeAuthStatus.
        """
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch(
                "conductor.providers.claude_agent_sdk._run_auth_status_subprocess",
                side_effect=OSError("No such file or directory"),
            ),
        ):
            status = await provider._check_auth_readiness()

        assert status.ready is False
        assert status.error is not None
        assert "No such file or directory" not in status.error  # raw OS message suppressed


# ---------------------------------------------------------------------------
# Finding 4 — interrupt_waiter is cancelled on outer CancelledError
# ---------------------------------------------------------------------------


@pytest.mark.claude_auth_readiness_mocked
class TestCancelWithInterruptSignal:
    @pytest.mark.asyncio
    async def test_execute_cancellation_during_auth_preflight_leaves_no_pending_task(
        self,
    ) -> None:
        """Outer CancelledError during auth preflight must not leave ``auth_task``
        dangling as a pending asyncio task.

        The interrupt-racing pattern (a separate ``interrupt_waiter`` task raced
        against ``auth_task`` via ``asyncio.wait(FIRST_COMPLETED)``) was removed
        (finding 4); ``_execute_session`` now does a plain bounded ``await auth_task``
        wrapped in try/except ``CancelledError`` that explicitly cancels and awaits
        ``auth_task`` before re-raising. This regression-tests that cleanup still
        happens with an ``interrupt_signal`` supplied and never set.
        """
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        interrupt = asyncio.Event()  # never set
        agent = AgentDef(
            name="test",
            type="agent",
            prompt="hello",
            output={"result": OutputField(type="string")},
        )

        pending_before: set[asyncio.Task] = set()

        async def slow_check(**kwargs: object) -> ClaudeAuthStatus:
            nonlocal pending_before
            pending_before = {t for t in asyncio.all_tasks() if not t.done()}
            await asyncio.sleep(100)
            return ClaudeAuthStatus(
                requested_mode="subscription",
                inferred_mode="subscription",
                ready=True,
            )

        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch.object(provider, "_check_auth_readiness", slow_check),
        ):
            task = asyncio.create_task(
                provider.execute(
                    agent=agent,
                    context={},
                    rendered_prompt="hello",
                    interrupt_signal=interrupt,
                )
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        pending_after = {t for t in asyncio.all_tasks() if not t.done()}
        leaked = pending_after & pending_before - {asyncio.current_task()}
        assert not leaked, f"Tasks leaked after cancellation: {leaked}"


@pytest.mark.claude_auth_readiness_mocked
class TestAuthEnvWiredIntoOptions:
    """``ClaudeAgentOptions(env=...)`` receives the complete finalized child
    environment, not a sparse override: the SDK spreads ``env`` over a live
    copy of ``os.environ``, so only a complete mapping pins what the child sees.

    The per-mode neutralization matrix itself is unit-tested against
    ``EffectiveAuthContext`` in ``test_claude_agent_sdk_effective_auth_context.py``;
    these tests prove that result is what reaches the real construction seam.
    """

    async def _run_execute_and_capture_options(
        self, auth_mode: str, environ: dict[str, str]
    ) -> dict[str, object]:
        provider = ClaudeAgentSdkProvider(auth_mode=auth_mode)
        agent = AgentDef(name="test", type="agent", prompt="hello")
        ready_status = ClaudeAuthStatus(
            requested_mode=auth_mode,  # type: ignore[arg-type]
            inferred_mode="subscription" if auth_mode != "api_key" else "api_key",
            ready=True,
        )
        options_ctor = MagicMock()

        async def fake_query(**kwargs):
            return
            yield  # pragma: no cover - never reached; makes this an async generator

        with (
            patch.dict(os.environ, environ, clear=True),
            patch("conductor.providers.claude_agent_sdk._find_claude_cli", return_value=FAKE_CLI),
            patch.object(provider, "_check_auth_readiness", AsyncMock(return_value=ready_status)),
            patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", options_ctor),
            patch("conductor.providers.claude_agent_sdk.query", fake_query),
        ):
            await provider.execute(agent=agent, context={}, rendered_prompt="hello")

        return options_ctor.call_args.kwargs

    @pytest.mark.asyncio
    async def test_subscription_mode_env_kwarg(self) -> None:
        kwargs = await self._run_execute_and_capture_options(
            "subscription", {"ANTHROPIC_API_KEY": "sk-ant-fake", "PATH": "/bin"}
        )
        assert kwargs["env"] == {
            "PATH": "/bin",
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_AUTH_TOKEN": "",
            "CLAUDE_CODE_OAUTH_TOKEN": "",
            "CLAUDE_CODE_USE_BEDROCK": "",
            "CLAUDE_CODE_USE_VERTEX": "",
            "CLAUDE_CODE_USE_FOUNDRY": "",
        }

    @pytest.mark.asyncio
    async def test_api_key_mode_env_kwarg(self) -> None:
        kwargs = await self._run_execute_and_capture_options(
            "api_key",
            {"ANTHROPIC_API_KEY": "sk-ant-fake", "ANTHROPIC_AUTH_TOKEN": "tok", "PATH": "/bin"},
        )
        assert kwargs["env"] == {
            "PATH": "/bin",
            "ANTHROPIC_API_KEY": "sk-ant-fake",
            "ANTHROPIC_AUTH_TOKEN": "",
            "CLAUDE_CODE_OAUTH_TOKEN": "",
            "CLAUDE_CODE_USE_BEDROCK": "",
            "CLAUDE_CODE_USE_VERTEX": "",
            "CLAUDE_CODE_USE_FOUNDRY": "",
        }

    @pytest.mark.asyncio
    async def test_auto_mode_env_kwarg_is_the_unmodified_snapshot(self) -> None:
        environ = {"ANTHROPIC_API_KEY": "sk-ant-fake", "CLAUDE_CODE_USE_BEDROCK": "1"}
        kwargs = await self._run_execute_and_capture_options("auto", environ)
        assert kwargs["env"] == environ

    @pytest.mark.asyncio
    async def test_cli_path_kwarg_is_the_resolved_cli(self) -> None:
        kwargs = await self._run_execute_and_capture_options("subscription", {})
        assert kwargs["cli_path"] == FAKE_CLI


@pytest.mark.claude_auth_readiness_mocked
class TestNeutralizedCredentialWarning:
    """``execute`` names, and never reveals, the inherited credentials it blanks."""

    async def _execute(self, auth_mode: str, environ: dict[str, str]) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode=auth_mode)
        agent = AgentDef(name="test", type="agent", prompt="hello")
        ready = ClaudeAuthStatus(
            requested_mode=auth_mode,  # type: ignore[arg-type]
            inferred_mode="api_key" if auth_mode == "api_key" else "subscription",
            ready=True,
        )

        async def fake_query(**kwargs):
            return
            yield  # pragma: no cover

        with (
            patch.dict(os.environ, environ, clear=True),
            patch.object(provider, "_check_auth_readiness", AsyncMock(return_value=ready)),
            patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", MagicMock()),
            patch("conductor.providers.claude_agent_sdk.query", fake_query),
        ):
            await provider.execute(agent=agent, context={}, rendered_prompt="hello")

    @pytest.mark.asyncio
    async def test_warning_names_var_never_value(self, caplog) -> None:
        with caplog.at_level("WARNING"):
            await self._execute(
                "subscription",
                {"ANTHROPIC_API_KEY": "sk-ant-super-secret", "CLAUDE_CODE_OAUTH_TOKEN": "oat"},
            )
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "ANTHROPIC_API_KEY" in joined
        assert "CLAUDE_CODE_OAUTH_TOKEN" in joined
        assert "sk-ant-super-secret" not in joined
        assert "oat" not in joined.split()

    @pytest.mark.asyncio
    async def test_api_key_mode_does_not_report_its_own_key(self, caplog) -> None:
        with caplog.at_level("WARNING"):
            await self._execute("api_key", {"ANTHROPIC_API_KEY": "sk-ant-fake"})
        assert not caplog.records

    @pytest.mark.asyncio
    async def test_no_warning_when_nothing_conflicts(self, caplog) -> None:
        with caplog.at_level("WARNING"):
            await self._execute("subscription", {"CLAUDE_CODE_USE_VERTEX": "   "})
        assert not caplog.records


@pytest.mark.claude_auth_readiness_mocked
class TestParentEnvironNeverMutated:
    @pytest.mark.asyncio
    async def test_execute_leaves_os_environ_byte_identical(self) -> None:
        """The override must reach the child process exclusively via
        ``ClaudeAgentOptions(env=...)`` — the parent's own ``os.environ`` must
        never be read for this purpose and must never be mutated."""
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        agent = AgentDef(
            name="test",
            type="agent",
            prompt="hello",
        )
        ready_status = ClaudeAuthStatus(
            requested_mode="subscription",
            inferred_mode="subscription",
            ready=True,
        )

        async def fake_query(**kwargs):
            return
            yield  # pragma: no cover

        environ = {"ANTHROPIC_API_KEY": "sk-ant-fake", "PATH": os.environ.get("PATH", "")}
        with (
            patch.dict(os.environ, environ, clear=True),
            patch.object(provider, "_check_auth_readiness", AsyncMock(return_value=ready_status)),
            patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", MagicMock()),
            patch("conductor.providers.claude_agent_sdk.query", fake_query),
        ):
            before = dict(os.environ)
            await provider.execute(agent=agent, context={}, rendered_prompt="hello")
            after = dict(os.environ)

        assert before == after


@pytest.mark.claude_auth_readiness_mocked
class TestApiKeySourcePropagation:
    """``api_key_source`` is populated from the whitelisted ``apiKeySource``
    JSON field, on both the logged-in and logged-out branches, and is absent
    (``None``) when the CLI does not report it."""

    @pytest.mark.asyncio
    async def test_api_key_source_populated_when_logged_in(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        auth_json = json.dumps(
            {"loggedIn": True, "authMethod": "claude.ai", "apiKeySource": "env"}
        ).encode()
        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch(
                "conductor.providers.claude_agent_sdk._run_auth_status_subprocess",
                AsyncMock(return_value=(auth_json, b"", 0)),
            ),
        ):
            status = await provider._check_auth_readiness()
        assert status.api_key_source == "env"

    @pytest.mark.asyncio
    async def test_api_key_source_populated_when_logged_out(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        auth_json = json.dumps({"loggedIn": False, "apiKeySource": "config"}).encode()
        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch(
                "conductor.providers.claude_agent_sdk._run_auth_status_subprocess",
                AsyncMock(return_value=(auth_json, b"", 0)),
            ),
        ):
            status = await provider._check_auth_readiness()
        assert status.api_key_source == "config"
        assert status.ready is False

    @pytest.mark.asyncio
    async def test_api_key_source_absent_when_not_reported(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        auth_json = json.dumps({"loggedIn": True, "authMethod": "claude.ai"}).encode()
        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch(
                "conductor.providers.claude_agent_sdk._run_auth_status_subprocess",
                AsyncMock(return_value=(auth_json, b"", 0)),
            ),
        ):
            status = await provider._check_auth_readiness()
        assert status.api_key_source is None

    @pytest.mark.asyncio
    async def test_api_key_source_non_string_value_ignored(self) -> None:
        """Whitelist filtering, not the JSON parser, is the trust boundary —
        a non-string ``apiKeySource`` (malformed/hostile CLI output) must not
        be surfaced verbatim."""
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        auth_json = json.dumps({"loggedIn": True, "apiKeySource": 12345}).encode()
        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch(
                "conductor.providers.claude_agent_sdk._run_auth_status_subprocess",
                AsyncMock(return_value=(auth_json, b"", 0)),
            ),
        ):
            status = await provider._check_auth_readiness()
        assert status.api_key_source is None


@pytest.mark.claude_auth_readiness_mocked
class TestAuthStatusDiagnostic:
    """``auth_status_diagnostic`` surfaces two separate groups for
    ``conductor doctor --check`` (TICKET-20260816-0002, Finding 3): the
    Conductor-inferred mode never merges with the SDK/CLI's sanitized
    as-observed fields, and ``authMethod`` alone must not be relied on to
    distinguish a subscription session from an API-key-present one."""

    @pytest.mark.asyncio
    async def test_none_before_any_validate_connection_call(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        assert provider.auth_status_diagnostic is None

    @pytest.mark.asyncio
    async def test_populated_after_validate_connection_ready(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        auth_json = json.dumps(
            {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}
        ).encode()
        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch(
                "conductor.providers.claude_agent_sdk._run_auth_status_subprocess",
                AsyncMock(return_value=(auth_json, b"", 0)),
            ),
        ):
            await provider.validate_connection()

        diagnostic = provider.auth_status_diagnostic
        assert diagnostic is not None
        assert diagnostic["conductor_inferred"] == {
            "requested_mode": "subscription",
            "inferred_mode": "subscription",
        }
        assert diagnostic["sdk_observed"] == {
            "authMethod": "claude.ai",
            "subscriptionType": "max",
        }
        assert "auto_note" not in diagnostic

    @pytest.mark.asyncio
    async def test_populated_after_validate_connection_not_ready(self) -> None:
        """The failure path must populate the diagnostic too — this is not
        only a success-path summary."""
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        auth_json = json.dumps({"loggedIn": False}).encode()
        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch(
                "conductor.providers.claude_agent_sdk._run_auth_status_subprocess",
                AsyncMock(return_value=(auth_json, b"", 0)),
            ),
        ):
            ready = await provider.validate_connection()

        assert ready is False
        assert provider.auth_status_diagnostic is not None

    @pytest.mark.asyncio
    async def test_auth_method_alone_does_not_distinguish_subscription_from_api_key(
        self,
    ) -> None:
        """Measured against the bundled CLI: ``authMethod`` reports
        ``claude.ai`` identically whether the session is a real subscription
        or is backed by an ambient ``ANTHROPIC_API_KEY`` — only
        ``apiKeySource``/``subscriptionType`` in ``sdk_observed`` distinguish
        the two rows a doctor reader needs told apart."""
        subscription_provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        subscription_json = json.dumps(
            {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}
        ).encode()
        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch(
                "conductor.providers.claude_agent_sdk._run_auth_status_subprocess",
                AsyncMock(return_value=(subscription_json, b"", 0)),
            ),
        ):
            await subscription_provider.validate_connection()

        api_key_provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        api_key_json = json.dumps(
            {"loggedIn": True, "authMethod": "claude.ai", "apiKeySource": "env"}
        ).encode()
        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch(
                "conductor.providers.claude_agent_sdk._run_auth_status_subprocess",
                AsyncMock(return_value=(api_key_json, b"", 0)),
            ),
        ):
            await api_key_provider.validate_connection()

        subscription_observed = subscription_provider.auth_status_diagnostic["sdk_observed"]
        api_key_observed = api_key_provider.auth_status_diagnostic["sdk_observed"]

        # Identical authMethod on both...
        assert subscription_observed["authMethod"] == api_key_observed["authMethod"] == "claude.ai"
        # ...but the two groups differ, so a doctor reader can still tell them apart.
        assert subscription_observed != api_key_observed
        assert "apiKeySource" not in subscription_observed
        assert "subscriptionType" not in api_key_observed

    @pytest.mark.asyncio
    async def test_auto_note_present_only_for_auto_mode(self) -> None:
        auto_provider = ClaudeAgentSdkProvider(auth_mode="auto")
        auth_json = json.dumps({"loggedIn": True, "authMethod": "claude.ai"}).encode()
        with (
            patch.dict(os.environ, {**_clean_env(), "ANTHROPIC_API_KEY": ""}, clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch(
                "conductor.providers.claude_agent_sdk._run_auth_status_subprocess",
                AsyncMock(return_value=(auth_json, b"", 0)),
            ),
        ):
            await auto_provider.validate_connection()
        auto_diagnostic = auto_provider.auth_status_diagnostic
        assert auto_diagnostic is not None
        assert "auto_note" in auto_diagnostic
        assert "inherited" in auto_diagnostic["auto_note"].lower()

        subscription_provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch(
                "conductor.providers.claude_agent_sdk._run_auth_status_subprocess",
                AsyncMock(return_value=(auth_json, b"", 0)),
            ),
        ):
            await subscription_provider.validate_connection()
        assert "auto_note" not in subscription_provider.auth_status_diagnostic
