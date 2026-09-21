"""Tests for the claude-agent-sdk effective authentication context.

One ``EffectiveAuthContext`` is captured per provider operation and is the only
source of environment, cwd, settings tiers, and CLI path for both the
``claude auth status --json`` readiness probe and ``ClaudeAgentOptions``. Every
test here is hermetic: process creation, CLI lookup, and the environment are
mocked or supplied explicitly, and no real CLI or credential is consulted.
"""

from __future__ import annotations

import dataclasses
import json
import os
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip(
    "claude_agent_sdk",
    reason="claude-agent-sdk extra not installed",
)

from conductor.config.schema import AgentDef, ProviderSettings  # noqa: E402
from conductor.exceptions import ProviderError  # noqa: E402
from conductor.providers.claude_agent_sdk import (  # noqa: E402
    ClaudeAgentSdkProvider,
    EffectiveAuthContext,
)

FAKE_CLI = Path("/fake/claude")

EXPLICIT_NEUTRALIZED = (
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)

COMPETING = {
    "ANTHROPIC_API_KEY": "sk-ant-fake",
    "ANTHROPIC_AUTH_TOKEN": "tok-fake",
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth-fake",
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "CLAUDE_CODE_USE_VERTEX": "1",
    "CLAUDE_CODE_USE_FOUNDRY": "1",
    "UNRELATED": "kept",
}

# Competing credentials only. Readiness refuses inherited cloud selectors in
# explicit modes, so tests about env/cwd parity or status parsing use this.
COMPETING_CREDENTIALS = {k: v for k, v in COMPETING.items() if not k.startswith("CLAUDE_CODE_USE_")}


def _ctx(
    env: dict[str, str],
    auth_mode: str = "auto",
    *,
    setting_sources: tuple[str, ...] = (),
    cli_path: Path | None = FAKE_CLI,
) -> EffectiveAuthContext:
    return EffectiveAuthContext(
        env_snapshot=env,
        resolved_cwd="/work",
        setting_sources=setting_sources,  # type: ignore[arg-type]
        cli_path=cli_path,
        auth_mode=auth_mode,  # type: ignore[arg-type]
    )


def _status_process(payload: bytes, returncode: int) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(payload, b""))
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=returncode)
    return proc


async def _empty_query(**kwargs: Any):
    return
    yield  # pragma: no cover - makes this an async generator


class TestContextIsImmutableAndIsolated:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("env_snapshot", {}),
            ("finalized_child_env", {}),
            ("resolved_cwd", "/elsewhere"),
            ("setting_sources", ("user",)),
            ("cli_path", None),
            ("auth_mode", "api_key"),
        ],
    )
    def test_fields_cannot_be_reassigned(self, field: str, value: object) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(_ctx({}), field, value)

    def test_caller_dict_is_copied_not_referenced(self) -> None:
        """Immutability does not depend on the caller wrapping its input."""
        env = {"KEY": "original"}
        ctx = _ctx(env)
        env["KEY"] = "mutated"
        env["ADDED"] = "x"
        assert dict(ctx.env_snapshot) == {"KEY": "original"}
        assert dict(ctx.finalized_child_env) == {"KEY": "original"}

    @pytest.mark.parametrize("field", ["env_snapshot", "finalized_child_env"])
    def test_environment_mappings_are_read_only(self, field: str) -> None:
        mapping = getattr(_ctx({"KEY": "v"}), field)
        assert isinstance(mapping, types.MappingProxyType)
        with pytest.raises(TypeError):
            mapping["KEY"] = "new"  # type: ignore[index]
        with pytest.raises(TypeError):
            del mapping["KEY"]  # type: ignore[attr-defined]

    def test_setting_sources_sequence_becomes_a_tuple(self) -> None:
        sources = ["project"]
        ctx = EffectiveAuthContext(
            env_snapshot={},
            resolved_cwd="/work",
            setting_sources=sources,  # type: ignore[arg-type]
            cli_path=None,
            auth_mode="auto",
        )
        sources.append("user")
        assert ctx.setting_sources == ("project",)

    def test_repr_never_renders_the_environment(self) -> None:
        rendered = repr(_ctx({"ANTHROPIC_API_KEY": "sk-ant-secret"}, "api_key"))
        assert "sk-ant-secret" not in rendered
        assert "ANTHROPIC_API_KEY" not in rendered


class TestNeutralizationMatrix:
    def test_subscription_blanks_api_key_tokens_and_backend_selectors(self) -> None:
        env = _ctx(COMPETING, "subscription").finalized_child_env
        assert dict(env) == {
            "ANTHROPIC_API_KEY": "",
            **dict.fromkeys(EXPLICIT_NEUTRALIZED, ""),
            "UNRELATED": "kept",
        }

    def test_api_key_keeps_only_the_api_key(self) -> None:
        env = _ctx(COMPETING, "api_key").finalized_child_env
        assert dict(env) == {
            "ANTHROPIC_API_KEY": "sk-ant-fake",
            **dict.fromkeys(EXPLICIT_NEUTRALIZED, ""),
            "UNRELATED": "kept",
        }

    def test_auto_passes_the_snapshot_through_unchanged(self) -> None:
        ctx = _ctx(COMPETING, "auto")
        assert dict(ctx.finalized_child_env) == COMPETING

    @pytest.mark.parametrize("mode", ["subscription", "api_key"])
    def test_explicit_modes_pin_absent_variables_blank(self, mode: str) -> None:
        """Pinned even when absent: the SDK merges ``env`` over the *live*
        ``os.environ``, so an omitted key would let a later parent value in."""
        env = _ctx({}, mode).finalized_child_env
        for name in EXPLICIT_NEUTRALIZED:
            assert env[name] == ""
        assert ("ANTHROPIC_API_KEY" in env) is (mode == "subscription")

    def test_snapshot_keeps_inherited_values(self) -> None:
        """Neutralization applies to the child env only, not to the snapshot."""
        ctx = _ctx(COMPETING, "subscription")
        assert dict(ctx.env_snapshot) == COMPETING

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            ("subscription", ["ANTHROPIC_API_KEY", *EXPLICIT_NEUTRALIZED]),
            ("api_key", list(EXPLICIT_NEUTRALIZED)),
            ("auto", []),
        ],
    )
    def test_overridden_credentials_lists_only_nonblank_inherited_names(
        self, mode: str, expected: list[str]
    ) -> None:
        assert _ctx(COMPETING, mode).overridden_credentials() == expected
        assert _ctx({"CLAUDE_CODE_USE_VERTEX": "  "}, mode).overridden_credentials() == []


class TestCapture:
    def test_capture_snapshots_provider_config_and_process_state(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="api_key")
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-fake"}, clear=True),
            patch("conductor.providers.claude_agent_sdk._find_claude_cli", return_value=FAKE_CLI),
        ):
            ctx = provider._capture_auth_context("/work")
        assert ctx.auth_mode == "api_key"
        assert ctx.resolved_cwd == "/work"
        assert ctx.cli_path == FAKE_CLI
        assert ctx.setting_sources == ()
        assert dict(ctx.env_snapshot) == {"ANTHROPIC_API_KEY": "sk-ant-fake"}

    def test_later_os_environ_changes_do_not_reach_the_context(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        with (
            patch.dict(os.environ, {"PATH": "/bin"}, clear=True),
            patch("conductor.providers.claude_agent_sdk._find_claude_cli", return_value=FAKE_CLI),
        ):
            ctx = provider._capture_auth_context("/work")
            os.environ["ANTHROPIC_API_KEY"] = "sk-ant-late"
            os.environ["PATH"] = "/changed"
            assert ctx.env_snapshot == {"PATH": "/bin"}
            assert ctx.finalized_child_env["PATH"] == "/bin"
            assert ctx.finalized_child_env["ANTHROPIC_API_KEY"] == ""


@pytest.mark.claude_auth_readiness_mocked
class TestReadinessAndExecutionShareOneContext:
    """The probe and the SDK session see identical values, even when the
    parent environment changes between the two."""

    @pytest.mark.asyncio
    async def test_probe_and_options_receive_identical_env_cwd_and_cli(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        agent = AgentDef(name="a", type="agent", prompt="hi")
        probe: dict[str, Any] = {}

        async def probe_then_mutate_parent(*args: object, **kwargs: Any) -> MagicMock:
            probe["cli"] = args[0]
            probe["env"] = kwargs["env"]
            probe["cwd"] = kwargs["cwd"]
            # The parent changes after readiness but before option construction.
            os.environ["ANTHROPIC_API_KEY"] = "sk-ant-late"
            os.environ["PATH"] = "/changed"
            return _status_process(json.dumps({"loggedIn": True}).encode(), 0)

        options_ctor = MagicMock()
        with (
            patch.dict(os.environ, {**COMPETING_CREDENTIALS, "PATH": "/bin"}, clear=True),
            patch("conductor.providers.claude_agent_sdk._find_claude_cli", return_value=FAKE_CLI),
            patch("asyncio.create_subprocess_exec", probe_then_mutate_parent),
            patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", options_ctor),
            patch("conductor.providers.claude_agent_sdk.query", _empty_query),
        ):
            await provider.execute(agent=agent, context={}, rendered_prompt="hi")

        options = options_ctor.call_args.kwargs
        assert options["env"] == probe["env"]
        assert options["cwd"] == probe["cwd"]
        assert options["cli_path"] == FAKE_CLI
        assert probe["cli"] == str(FAKE_CLI)
        assert options["env"]["PATH"] == "/bin"
        assert options["env"]["ANTHROPIC_API_KEY"] == ""
        assert "sk-ant-late" not in options["env"].values()

    @pytest.mark.asyncio
    async def test_each_operation_captures_a_fresh_context(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="api_key")
        seen: list[EffectiveAuthContext] = []
        real_capture = provider._capture_auth_context

        def recording_capture(cwd: str) -> EffectiveAuthContext:
            seen.append(real_capture(cwd))
            return seen[-1]

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "first"}, clear=True),
            patch("conductor.providers.claude_agent_sdk._find_claude_cli", return_value=FAKE_CLI),
            patch.object(provider, "_capture_auth_context", recording_capture),
        ):
            await provider.validate_connection()
            os.environ["ANTHROPIC_API_KEY"] = "second"
            await provider.validate_connection()

        assert [c.env_snapshot["ANTHROPIC_API_KEY"] for c in seen] == ["first", "second"]


class TestSettingSourcesRefusedByExplicitModes:
    @pytest.mark.parametrize("mode", ["subscription", "api_key"])
    def test_static_validation_refuses_with_both_remedies(self, mode: str) -> None:
        with pytest.raises(ValueError) as exc:
            ProviderSettings(name="claude-agent-sdk", auth_mode=mode, setting_sources=["project"])
        message = str(exc.value)
        assert "Remove setting_sources" in message
        assert "auth_mode 'auto'" in message
        assert "after Conductor configures the child environment" in message

    @pytest.mark.parametrize("mode", ["subscription", "api_key"])
    def test_static_validation_allows_explicit_mode_without_sources(self, mode: str) -> None:
        ProviderSettings(name="claude-agent-sdk", auth_mode=mode, setting_sources=[])

    def test_static_validation_leaves_auto_sources_unchanged(self) -> None:
        settings = ProviderSettings(
            name="claude-agent-sdk", auth_mode="auto", setting_sources=["project", "user"]
        )
        assert settings.setting_sources == ["project", "user"]

    @pytest.mark.claude_auth_readiness_mocked
    @pytest.mark.parametrize("mode", ["subscription", "api_key"])
    @pytest.mark.asyncio
    async def test_execution_boundary_refuses_before_any_probe_or_session(self, mode: str) -> None:
        """``conductor run`` never calls the static validator, so a provider
        built directly must refuse too — before spawning or querying."""
        provider = ClaudeAgentSdkProvider(auth_mode=mode, setting_sources=["project"])
        agent = AgentDef(name="a", type="agent", prompt="hi")
        spawn = AsyncMock()
        query = MagicMock()
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-fake"}, clear=True),
            patch("conductor.providers.claude_agent_sdk._find_claude_cli", return_value=FAKE_CLI),
            patch("asyncio.create_subprocess_exec", spawn),
            patch("conductor.providers.claude_agent_sdk.query", query),
            pytest.raises(ProviderError) as exc,
        ):
            await provider.execute(agent=agent, context={}, rendered_prompt="hi")

        assert "Remove setting_sources" in str(exc.value)
        assert "auth_mode 'auto'" in str(exc.value)
        assert exc.value.is_retryable is False
        spawn.assert_not_called()
        query.assert_not_called()

    @pytest.mark.claude_auth_readiness_mocked
    @pytest.mark.asyncio
    async def test_auto_with_sources_proceeds_to_readiness(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="auto", setting_sources=["project"])
        status = await provider._check_auth_readiness(
            _ctx({"ANTHROPIC_API_KEY": "sk-ant-fake"}, "auto", setting_sources=("project",))
        )
        assert status.ready is True


@pytest.mark.claude_auth_readiness_mocked
class TestApiKeyMode:
    """Readiness is key + non-spawning CLI availability; status is never run."""

    @pytest.fixture(autouse=True)
    def _no_spawn(self):
        with patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn:
            yield
        spawn.assert_not_called()

    @pytest.mark.parametrize("key", [None, "", "   "])
    @pytest.mark.asyncio
    async def test_blank_or_missing_key_is_not_ready(self, key: str | None) -> None:
        env = {} if key is None else {"ANTHROPIC_API_KEY": key}
        provider = ClaudeAgentSdkProvider(auth_mode="api_key")
        status = await provider._check_auth_readiness(_ctx(env, "api_key"))
        assert status.ready is False
        assert "ANTHROPIC_API_KEY" in (status.error or "")

    @pytest.mark.asyncio
    async def test_missing_cli_is_not_ready_even_with_a_key(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="api_key")
        status = await provider._check_auth_readiness(
            _ctx({"ANTHROPIC_API_KEY": "sk-ant-fake"}, "api_key", cli_path=None)
        )
        assert status.ready is False
        assert "Claude CLI not found" in (status.error or "")

    @pytest.mark.asyncio
    async def test_key_and_cli_are_ready(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="api_key")
        status = await provider._check_auth_readiness(
            _ctx({"ANTHROPIC_API_KEY": "sk-ant-fake"}, "api_key")
        )
        assert status.ready is True
        assert status.inferred_mode == "api_key"


@pytest.mark.claude_auth_readiness_mocked
class TestSubscriptionStatusParsing:
    async def _readiness(self, stdout: bytes, returncode: int) -> tuple[Any, dict[str, Any]]:
        captured: dict[str, Any] = {}

        async def create(*args: object, **kwargs: Any) -> MagicMock:
            captured.update(kwargs)
            return _status_process(stdout, returncode)

        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        with patch("asyncio.create_subprocess_exec", create):
            status = await provider._check_auth_readiness(
                _ctx(COMPETING_CREDENTIALS, "subscription")
            )
        return status, captured

    @pytest.mark.asyncio
    async def test_logged_out_json_on_nonzero_exit_is_a_specific_failure(self) -> None:
        status, _ = await self._readiness(json.dumps({"loggedIn": False}).encode(), 1)
        assert status.ready is False
        assert status.error == "Not logged in to Claude Code. Run: claude auth login"

    @pytest.mark.asyncio
    async def test_logged_in_true_on_zero_exit_is_ready(self) -> None:
        status, _ = await self._readiness(json.dumps({"loggedIn": True}).encode(), 0)
        assert status.ready is True

    @pytest.mark.asyncio
    async def test_logged_in_true_on_nonzero_exit_is_not_ready(self) -> None:
        """A success payload contradicted by a failing exit code is not a
        success. Reported generically, without echoing the CLI's output."""
        stdout = json.dumps(
            {"loggedIn": True, "authMethod": "claude.ai", "apiKeySource": "env"}
        ).encode()
        status, _ = await self._readiness(stdout, 1)
        assert status.ready is False
        assert status.error is not None
        assert "Not logged in" not in status.error
        rendered = str(dataclasses.asdict(status))
        for leaked in ("claude.ai", "loggedIn", "sk-ant-fake", "tok-fake", "oauth-fake"):
            assert leaked not in rendered

    @pytest.mark.parametrize("returncode", [1, 2, 127])
    @pytest.mark.asyncio
    async def test_logged_out_keeps_its_guidance_on_any_nonzero_exit(self, returncode: int) -> None:
        status, _ = await self._readiness(json.dumps({"loggedIn": False}).encode(), returncode)
        assert status.ready is False
        assert status.error == "Not logged in to Claude Code. Run: claude auth login"

    @pytest.mark.asyncio
    async def test_errors_never_carry_stdout_stderr_or_credentials(self) -> None:
        """Neither the CLI's own output nor an inherited credential may appear
        in a status error."""
        captured: dict[str, Any] = {}

        async def create(*args: object, **kwargs: Any) -> MagicMock:
            captured.update(kwargs)
            proc = MagicMock()
            proc.returncode = 3
            proc.communicate = AsyncMock(
                return_value=(b"stdout-marker-a41f", b"stderr-marker-b52e")
            )
            proc.kill = MagicMock()
            proc.wait = AsyncMock(return_value=3)
            return proc

        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        with patch("asyncio.create_subprocess_exec", create):
            status = await provider._check_auth_readiness(
                _ctx(COMPETING_CREDENTIALS, "subscription")
            )

        assert status.ready is False
        rendered = str(dataclasses.asdict(status))
        for leaked in ("stdout-marker-a41f", "stderr-marker-b52e", "sk-ant-fake", "tok-fake"):
            assert leaked not in rendered

    @pytest.mark.asyncio
    async def test_non_json_on_nonzero_exit_is_not_ready(self) -> None:
        status, _ = await self._readiness(b"command failed", 127)
        assert status.ready is False
        assert status.error is not None
        assert "Not logged in" not in status.error

    @pytest.mark.asyncio
    async def test_probe_runs_with_the_finalized_env_and_cwd(self) -> None:
        _, captured = await self._readiness(json.dumps({"loggedIn": True}).encode(), 0)
        assert captured["env"] == dict(
            _ctx(COMPETING_CREDENTIALS, "subscription").finalized_child_env
        )
        assert captured["cwd"] == "/work"


@pytest.mark.claude_auth_readiness_mocked
class TestApiProviderIsNotAuthMethod:
    @pytest.mark.asyncio
    async def test_api_provider_is_kept_separate_through_to_the_diagnostic(self) -> None:
        payload = json.dumps({"loggedIn": True, "apiProvider": "firstParty"}).encode()

        async def create(*args: object, **kwargs: object) -> MagicMock:
            return _status_process(payload, 0)

        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("conductor.providers.claude_agent_sdk._find_claude_cli", return_value=FAKE_CLI),
            patch("asyncio.create_subprocess_exec", create),
        ):
            assert await provider.validate_connection() is True

        status = provider._last_auth_status
        assert status is not None
        assert status.api_provider == "firstParty"
        assert status.auth_method is None
        observed = (provider.auth_status_diagnostic or {})["sdk_observed"]
        assert observed == {"apiProvider": "firstParty"}


def _sdk_setting_sources_flag(sources: list[str]) -> str:
    """The flag the SDK derives from ``ClaudeAgentOptions.setting_sources``
    (0.2.87, ``_internal/transport/subprocess_cli.py``)."""
    return f"--setting-sources={','.join(sources)}"


def _cli_honouring_setting_sources(recorded: list[tuple[str, ...]]):
    """A fake ``claude`` that is logged in only through an ambient settings tier.

    Mirrors the bundled CLI, whose ``eagerLoadSettings`` reads
    ``--setting-sources`` from argv before command dispatch: without the flag
    it loads every ambient tier; ``--setting-sources=`` loads none.
    """

    async def spawn(*args: object, **kwargs: object) -> MagicMock:
        argv = tuple(str(a) for a in args)
        recorded.append(argv)
        flags = [a for a in argv if a.startswith("--setting-sources")]
        ambient_tiers_loaded = not flags or flags[0] != "--setting-sources="
        if ambient_tiers_loaded:
            return _status_process(json.dumps({"loggedIn": True}).encode(), 0)
        return _status_process(json.dumps({"loggedIn": False}).encode(), 1)

    return spawn


@pytest.mark.claude_auth_readiness_mocked
class TestSettingSourcesParity:
    """The probe loads exactly the settings tiers the SDK session will load."""

    async def _execute(
        self,
        provider: ClaudeAgentSdkProvider,
        agent: AgentDef,
        env: dict[str, str],
    ) -> tuple[list[tuple[str, ...]], dict[str, Any]]:
        probes: list[tuple[str, ...]] = []

        async def spawn(*args: object, **kwargs: object) -> MagicMock:
            probes.append(tuple(str(a) for a in args))
            return _status_process(json.dumps({"loggedIn": True}).encode(), 0)

        options_ctor = MagicMock()
        with (
            patch.dict(os.environ, env, clear=True),
            patch("conductor.providers.claude_agent_sdk._find_claude_cli", return_value=FAKE_CLI),
            patch("asyncio.create_subprocess_exec", spawn),
            patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", options_ctor),
            patch("conductor.providers.claude_agent_sdk.query", _empty_query),
        ):
            await provider.execute(agent=agent, context={}, rendered_prompt="hi")
        return probes, options_ctor.call_args.kwargs

    @pytest.mark.parametrize(
        ("mode", "configured", "agent_skills", "expected"),
        [
            pytest.param("subscription", [], None, [], id="explicit-empty"),
            pytest.param("auto", [], None, [], id="auto-empty"),
            pytest.param("auto", ["project", "user"], None, ["project", "user"], id="auto-tiers"),
            pytest.param("auto", ["project"], [], [], id="skills-opt-out"),
        ],
    )
    @pytest.mark.asyncio
    async def test_probe_and_options_receive_equivalent_setting_sources(
        self,
        mode: str,
        configured: list[str],
        agent_skills: list[str] | None,
        expected: list[str],
    ) -> None:
        provider = ClaudeAgentSdkProvider(
            auth_mode=mode,  # type: ignore[arg-type]
            setting_sources=configured,  # type: ignore[arg-type]
        )
        agent = AgentDef(name="a", type="agent", prompt="hi", skills=agent_skills)
        # No API key, so ``auto`` takes the subscription path and probes too.
        probes, options = await self._execute(provider, agent, {"PATH": "/bin"})

        assert options["setting_sources"] == expected
        assert probes == [
            (str(FAKE_CLI), _sdk_setting_sources_flag(expected), "auth", "status", "--json")
        ]
        assert probes[0][1] == _sdk_setting_sources_flag(options["setting_sources"])

    @pytest.mark.asyncio
    async def test_skills_opt_out_is_recorded_in_the_context(self) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="auto", setting_sources=["project"])
        opted_out = AgentDef(name="a", type="agent", prompt="hi", skills=[])
        inherits = AgentDef(name="b", type="agent", prompt="hi")
        with patch("conductor.providers.claude_agent_sdk._find_claude_cli", return_value=FAKE_CLI):
            assert provider._capture_auth_context("/work", opted_out).setting_sources == ()
            assert provider._capture_auth_context("/work", inherits).setting_sources == ("project",)
            assert provider._capture_auth_context("/work").setting_sources == ("project",)

    @pytest.mark.parametrize(
        ("configured", "expected_flag"),
        [([], "--setting-sources="), (["user", "local"], "--setting-sources=user,local")],
    )
    @pytest.mark.asyncio
    async def test_validate_connection_probes_with_provider_default_sources(
        self, configured: list[str], expected_flag: str
    ) -> None:
        provider = ClaudeAgentSdkProvider(
            auth_mode="auto",
            setting_sources=configured,  # type: ignore[arg-type]
        )
        probes: list[tuple[str, ...]] = []
        with (
            patch.dict(os.environ, {"PATH": "/bin"}, clear=True),
            patch("conductor.providers.claude_agent_sdk._find_claude_cli", return_value=FAKE_CLI),
            patch("asyncio.create_subprocess_exec", _cli_honouring_setting_sources(probes)),
        ):
            await provider.validate_connection()
        assert probes == [(str(FAKE_CLI), expected_flag, "auth", "status", "--json")]

    @pytest.mark.asyncio
    async def test_subscription_cannot_pass_on_an_ambient_tier_the_session_disables(
        self,
    ) -> None:
        """Regression: the only credential lives in an ambient settings tier.

        The SDK session runs with ``--setting-sources=`` and never sees it, so
        the probe must not see it either — readiness fails and no session
        starts, instead of vouching for a credential execution will not use.
        """
        provider = ClaudeAgentSdkProvider(auth_mode="subscription")
        agent = AgentDef(name="a", type="agent", prompt="hi")
        probes: list[tuple[str, ...]] = []
        query = MagicMock()
        with (
            patch.dict(os.environ, {"PATH": "/bin"}, clear=True),
            patch("conductor.providers.claude_agent_sdk._find_claude_cli", return_value=FAKE_CLI),
            patch("asyncio.create_subprocess_exec", _cli_honouring_setting_sources(probes)),
            patch("conductor.providers.claude_agent_sdk.query", query),
            pytest.raises(ProviderError) as exc,
        ):
            await provider.execute(agent=agent, context={}, rendered_prompt="hi")

        assert "Not logged in" in str(exc.value)
        assert probes and all("--setting-sources=" in p for p in probes)
        query.assert_not_called()

    @pytest.mark.parametrize("mode", ["subscription", "api_key"])
    @pytest.mark.parametrize("agent_skills", [None, []], ids=["inherits", "skills-opt-out"])
    @pytest.mark.asyncio
    async def test_explicit_modes_refuse_a_credential_bearing_tier_before_probing(
        self, mode: str, agent_skills: list[str] | None
    ) -> None:
        """A configured tier that could supply a credential is refused in both
        explicit modes — even for an agent whose ``skills: []`` would not load
        it — before any probe runs or any session is queried."""
        provider = ClaudeAgentSdkProvider(
            auth_mode=mode,  # type: ignore[arg-type]
            setting_sources=["user"],
        )
        agent = AgentDef(name="a", type="agent", prompt="hi", skills=agent_skills)
        probes: list[tuple[str, ...]] = []
        query = MagicMock()
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-fake"}, clear=True),
            patch("conductor.providers.claude_agent_sdk._find_claude_cli", return_value=FAKE_CLI),
            patch("asyncio.create_subprocess_exec", _cli_honouring_setting_sources(probes)),
            patch("conductor.providers.claude_agent_sdk.query", query),
        ):
            with pytest.raises(ProviderError) as exc:
                await provider.execute(agent=agent, context={}, rendered_prompt="hi")
            assert await provider.validate_connection() is False

        assert "Remove setting_sources" in str(exc.value)
        assert "(user)" in str(exc.value)
        assert probes == []
        query.assert_not_called()


CLOUD_SELECTORS = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
# Distinctive so a leak of the value into an error is detectable.
SELECTOR_VALUE = "selector-value-7f3a"


@pytest.mark.claude_auth_readiness_mocked
class TestExplicitModesRejectInheritedCloudSelectors:
    """An inherited cloud-backend selector would route an explicit mode to a
    different backend, so both explicit modes refuse it rather than blanking it
    silently; ``auto`` keeps the inherited selection."""

    @pytest.mark.parametrize("selector", CLOUD_SELECTORS)
    @pytest.mark.parametrize("mode", ["subscription", "api_key"])
    @pytest.mark.asyncio
    async def test_explicit_mode_refuses_before_probe_or_session(
        self, mode: str, selector: str
    ) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode=mode)  # type: ignore[arg-type]
        agent = AgentDef(name="a", type="agent", prompt="hi")
        spawn = AsyncMock()
        query = MagicMock()
        env = {"ANTHROPIC_API_KEY": "sk-ant-fake", selector: SELECTOR_VALUE}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("conductor.providers.claude_agent_sdk._find_claude_cli", return_value=FAKE_CLI),
            patch("asyncio.create_subprocess_exec", spawn),
            patch("conductor.providers.claude_agent_sdk.query", query),
        ):
            with pytest.raises(ProviderError) as exc:
                await provider.execute(agent=agent, context={}, rendered_prompt="hi")
            connected = await provider.validate_connection()

        message = str(exc.value)
        assert selector in message
        assert "auth_mode 'auto'" in message
        assert SELECTOR_VALUE not in message
        assert exc.value.is_retryable is False
        assert connected is False
        assert provider._last_validation_error is not None
        assert selector in provider._last_validation_error
        assert SELECTOR_VALUE not in provider._last_validation_error
        spawn.assert_not_called()
        query.assert_not_called()

    @pytest.mark.parametrize("mode", ["subscription", "api_key"])
    @pytest.mark.asyncio
    async def test_every_conflicting_selector_is_named(self, mode: str) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode=mode)  # type: ignore[arg-type]
        env = {"ANTHROPIC_API_KEY": "sk-ant-fake", **dict.fromkeys(CLOUD_SELECTORS, "1")}
        status = await provider._check_auth_readiness(_ctx(env, mode))
        assert status.ready is False
        assert status.error is not None
        for selector in CLOUD_SELECTORS:
            assert selector in status.error

    @pytest.mark.parametrize("mode", ["subscription", "api_key"])
    @pytest.mark.asyncio
    async def test_blank_selector_is_not_a_conflict(self, mode: str) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode=mode)  # type: ignore[arg-type]
        env = {"ANTHROPIC_API_KEY": "sk-ant-fake", "CLAUDE_CODE_USE_BEDROCK": "  "}
        with patch(
            "asyncio.create_subprocess_exec",
            AsyncMock(return_value=_status_process(json.dumps({"loggedIn": True}).encode(), 0)),
        ):
            status = await provider._check_auth_readiness(_ctx(env, mode))
        assert status.ready is True

    @pytest.mark.parametrize("selector", CLOUD_SELECTORS)
    @pytest.mark.asyncio
    async def test_auto_preserves_inherited_selectors(self, selector: str) -> None:
        provider = ClaudeAgentSdkProvider(auth_mode="auto")
        agent = AgentDef(name="a", type="agent", prompt="hi")
        options_ctor = MagicMock()
        spawn = AsyncMock()
        env = {"ANTHROPIC_API_KEY": "sk-ant-fake", selector: SELECTOR_VALUE}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("conductor.providers.claude_agent_sdk._find_claude_cli", return_value=FAKE_CLI),
            patch("asyncio.create_subprocess_exec", spawn),
            patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", options_ctor),
            patch("conductor.providers.claude_agent_sdk.query", _empty_query),
        ):
            await provider.execute(agent=agent, context={}, rendered_prompt="hi")

        assert options_ctor.call_args.kwargs["env"][selector] == SELECTOR_VALUE
        spawn.assert_not_called()
