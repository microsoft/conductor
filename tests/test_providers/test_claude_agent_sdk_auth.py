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
)
from conductor.providers.factory import create_provider

FAKE_CLI = Path("/fake/claude")


def _clean_env() -> dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    }


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


class TestClaudeAuthStatusAutoMode:
    @pytest.mark.asyncio
    async def test_auto_with_api_key_resolves_to_api_key_without_subprocess(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="auto")
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-fake"}, clear=False),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch("conductor.providers.claude_agent_sdk._run_auth_status_subprocess") as mock_run,
        ):
            status = await provider._check_auth_readiness()
            assert status.resolved_mode == "api_key"
            assert status.ready is True
            mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_with_api_key_no_cli_binary_never_starts_subprocess(self) -> None:
        """auto + ANTHROPIC_API_KEY: no subprocess started, succeeds with no discoverable binary.

        Asserts at the observable subprocess-execution seam (asyncio.create_subprocess_exec)
        rather than the absence of one private helper call, so a rename of the internal
        wrapper does not silently defeat this test.

        Also leaves _find_claude_cli with no binary to find — if a regression puts a
        CLI prerequisite back in front of mode resolution, this test fails even in CI
        where the claude-agent-sdk extra ships a bundled binary.
        """
        provider = ClaudeAgentSdkProvider(auth_mode="auto")
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-fake"}, clear=False),
            # No CLI binary discoverable anywhere.
            patch("shutil.which", return_value=None),
            patch("pathlib.Path.exists", return_value=False),
            patch("pathlib.Path.is_file", return_value=False),
            # Assert at the subprocess-execution seam, not an internal helper.
            patch("asyncio.create_subprocess_exec") as mock_exec,
        ):
            status = await provider._check_auth_readiness()

        assert status.resolved_mode == "api_key"
        assert status.ready is True
        assert "sk-ant-fake" not in str(dataclasses.asdict(status))
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
            assert status.resolved_mode == "subscription"
            assert status.ready is True
            assert status.subscription_type == "pro"


class TestClaudeAuthStatusSubscriptionMode:
    @pytest.mark.asyncio
    async def test_subscription_with_conflicting_api_key_fails_closed(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-fake"}, clear=False),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
        ):
            status = await provider._check_auth_readiness()
            assert status.ready is False
            assert "ANTHROPIC_API_KEY" in (status.error or "")

    @pytest.mark.asyncio
    async def test_subscription_with_conflicting_auth_token_fails_closed(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        env = _clean_env() | {"ANTHROPIC_AUTH_TOKEN": "tok-fake"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
        ):
            status = await provider._check_auth_readiness()
            assert status.ready is False
            assert "ANTHROPIC_AUTH_TOKEN" in (status.error or "") or "subscription" in (
                status.error or ""
            )

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
            assert status.resolved_mode == "subscription"

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


class TestClaudeAuthStatusApiKeyMode:
    @pytest.mark.asyncio
    async def test_api_key_present_passes_without_key_value(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="api_key")
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-real"}, clear=False),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
        ):
            status = await provider._check_auth_readiness()
            assert status.ready is True
            assert status.resolved_mode == "api_key"
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
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "   "}, clear=False),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
        ):
            status = await provider._check_auth_readiness()
            assert status.ready is False


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
    async def test_execute_auth_preflight_interrupt_returns_partial(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        interrupt = asyncio.Event()
        agent = AgentDef(
            name="test",
            type="agent",
            prompt="hello",
            output={"result": OutputField(type="string")},
        )

        async def slow_check() -> ClaudeAuthStatus:
            await asyncio.sleep(100)
            return ClaudeAuthStatus(
                requested_mode="subscription",
                resolved_mode="subscription",
                ready=True,
            )

        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
            patch.object(provider, "_check_auth_readiness", slow_check),
            patch("conductor.providers.claude_agent_sdk.query") as mock_query,
        ):
            interrupt.set()
            result = await provider.execute(
                agent=agent,
                context={},
                rendered_prompt="hello",
                interrupt_signal=interrupt,
            )
            assert result.partial is True
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

        async def slow_check() -> ClaudeAuthStatus:
            await asyncio.sleep(100)
            return ClaudeAuthStatus(
                requested_mode="subscription",
                resolved_mode="subscription",
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


class TestSecretHygiene:
    @pytest.mark.asyncio
    async def test_api_key_value_not_in_auth_status(self) -> None:
        canary = "sk-ant-CANARY-VALUE-12345"
        provider = ClaudeAgentSdkProvider(auth_mode="api_key")
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": canary}, clear=False),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
        ):
            status = await provider._check_auth_readiness()
        assert canary not in str(dataclasses.asdict(status))

    @pytest.mark.asyncio
    async def test_credential_env_vars_not_in_auth_status_on_subscription_conflict(self) -> None:
        canary_key = "sk-ant-CANARY-98765"
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": canary_key}, clear=False),
            patch(
                "conductor.providers.claude_agent_sdk._find_claude_cli",
                return_value=FAKE_CLI,
            ),
        ):
            status = await provider._check_auth_readiness()
        assert canary_key not in (status.error or "")


# ---------------------------------------------------------------------------
# Finding 1 — auto + ANTHROPIC_AUTH_TOKEN must NOT fail closed
# ---------------------------------------------------------------------------


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

        assert status.resolved_mode == "subscription"
        assert status.ready is True
        mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# Finding 3 — OSError during subprocess spawn returns sanitized failure
# ---------------------------------------------------------------------------


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


class TestCancelWithInterruptSignal:
    @pytest.mark.asyncio
    async def test_execute_cancellation_with_interrupt_signal_leaves_no_pending_task(
        self,
    ) -> None:
        """Outer CancelledError during auth preflight with an interrupt_signal must not
        leave interrupt_waiter as a pending asyncio task.

        Previously the except-CancelledError handler only cancelled auth_task; with an
        interrupt_signal present interrupt_waiter was also created but never cancelled.
        """
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        interrupt = asyncio.Event()  # not set — so asyncio.wait won't complete it
        agent = AgentDef(
            name="test",
            type="agent",
            prompt="hello",
            output={"result": OutputField(type="string")},
        )

        pending_before: set[asyncio.Task] = set()

        async def slow_check() -> ClaudeAuthStatus:
            nonlocal pending_before
            # Record tasks pending at the moment auth check is running; the
            # interrupt_waiter task should be among them.
            pending_before = {t for t in asyncio.all_tasks() if not t.done()}
            await asyncio.sleep(100)
            return ClaudeAuthStatus(
                requested_mode="subscription",
                resolved_mode="subscription",
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
            # Let the task enter asyncio.wait (past the create_task calls)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        # After cancellation, no tasks from execute() should still be pending.
        pending_after = {t for t in asyncio.all_tasks() if not t.done()}
        leaked = pending_after & pending_before - {asyncio.current_task()}
        assert not leaked, f"Tasks leaked after cancellation: {leaked}"
