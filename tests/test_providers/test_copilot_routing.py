"""Copilot native routing preferences (Fusion / Auto tiers) through the provider.

The fake client records the exact keyword arguments the provider hands the
SDK's ``create_session`` / ``resume_session``, so these tests pin the wire
arguments rather than accepting any call. The fake session drives the real
``_send_and_wait`` event loop with scripted SDK events.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conductor.config.schema import AgentDef, ProviderSettings
from conductor.exceptions import ProviderError, ValidationError
from conductor.providers.copilot import CopilotProvider, RetryConfig, SDKResponse


def _event(event_type: str, **data: Any) -> Any:
    return SimpleNamespace(type=SimpleNamespace(value=event_type), data=SimpleNamespace(**data))


def _usage(model: str | None, input_tokens: int, output_tokens: int, call_id: str) -> Any:
    return _event(
        "assistant.usage",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=None,
        cache_write_tokens=None,
        api_call_id=call_id,
    )


def _message(content: str) -> Any:
    return _event("assistant.message", content=content)


_IDLE = _event("session.idle")


class _FakeSession:
    """A session whose turns replay scripted SDK events, then go idle."""

    def __init__(self, session_id: str, turns: list[list[Any]]) -> None:
        self.session_id = session_id
        self._turns = turns
        self._handlers: list[Any] = []
        self.prompts: list[str] = []
        self.disconnected = False

    def on(self, handler: Any) -> None:
        self._handlers.append(handler)

    async def send(self, prompt: str) -> None:
        self.prompts.append(prompt)
        events = self._turns.pop(0) if self._turns else [_message("{}"), _IDLE]
        handler = self._handlers[-1]
        for event in events:
            handler(event)

    async def disconnect(self) -> None:
        self.disconnected = True

    async def abort(self) -> None:
        self._handlers[-1](_IDLE)


class _FakeClient:
    def __init__(self, turns: list[list[Any]] | None = None) -> None:
        self.creates: list[dict[str, Any]] = []
        self.resumes: list[tuple[str, dict[str, Any]]] = []
        self.turns = turns
        self.fail_create: Exception | None = None
        self.sessions: list[_FakeSession] = []

    def _session(self, session_id: str) -> _FakeSession:
        turns = list(self.turns) if self.turns is not None else [[_message("{}"), _IDLE]]
        session = _FakeSession(session_id, turns)
        self.sessions.append(session)
        return session

    async def create_session(self, **kwargs: Any) -> _FakeSession:
        self.creates.append(kwargs)
        if self.fail_create is not None:
            raise self.fail_create
        return self._session(f"session-{len(self.creates)}")

    async def resume_session(self, session_id: str, **kwargs: Any) -> _FakeSession:
        self.resumes.append((session_id, kwargs))
        return self._session(session_id)

    async def list_models(self) -> list[Any]:
        return []


def _provider(
    client: _FakeClient,
    monkeypatch: pytest.MonkeyPatch,
    **kwargs: Any,
) -> CopilotProvider:
    provider = CopilotProvider(retry_config=RetryConfig(max_attempts=1), **kwargs)
    provider._client = client
    provider._started = True

    async def _noop() -> None:
        return None

    monkeypatch.setattr(provider, "_ensure_client_started", _noop)
    return provider


def _agent(**kwargs: Any) -> AgentDef:
    return AgentDef(name=kwargs.pop("name", "writer"), prompt="p", **kwargs)


class TestSessionArguments:
    async def test_agent_fusion_tier_reaches_create_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: an agent Fusion tier is sent as the SDK's github.capi option.
        client = _FakeClient()
        provider = _provider(client, monkeypatch)

        await provider.execute(_agent(model="fusion", fusion_tier="high"), {}, "p")

        (create,) = client.creates
        assert create["model"] == "fusion"
        assert create["github"]["capi"] == {"fusion_tier": "high"}

    async def test_agent_auto_tier_reaches_create_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _FakeClient()
        provider = _provider(client, monkeypatch)

        await provider.execute(_agent(model="auto", auto_tier="premium-v2"), {}, "p")

        assert client.creates[0]["github"]["capi"] == {"auto_tier": "premium-v2"}

    async def test_unset_preferences_send_no_capi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Requirement: omitted preferences leave runtime defaults in place.
        client = _FakeClient()
        provider = _provider(client, monkeypatch)

        await provider.execute(_agent(model="fusion"), {}, "p")
        await provider.execute(_agent(model="auto"), {}, "p")

        assert all("capi" not in create.get("github", {}) for create in client.creates)

    @pytest.mark.parametrize(
        ("model", "defaults", "expected"),
        [
            ("fusion", {"default_fusion_tier": "max"}, {"fusion_tier": "max"}),
            ("FUSION", {"default_fusion_tier": "standard"}, {"fusion_tier": "standard"}),
            ("auto", {"default_auto_tier": "efficiency"}, {"auto_tier": "efficiency"}),
            ("auto", {"default_fusion_tier": "max"}, None),
            ("fusion", {"default_auto_tier": "efficiency"}, None),
            ("gpt-5", {"default_fusion_tier": "max"}, None),
            ("hydrafusion", {"default_fusion_tier": "max"}, None),
        ],
    )
    async def test_defaults_apply_only_to_matching_routed_models(
        self,
        monkeypatch: pytest.MonkeyPatch,
        model: str,
        defaults: dict[str, str],
        expected: dict[str, str] | None,
    ) -> None:
        client = _FakeClient()
        provider = _provider(client, monkeypatch, **defaults)

        await provider.execute(_agent(model=model), {}, "p")

        assert client.creates[0].get("github", {}).get("capi") == expected

    async def test_agent_preference_replaces_default_as_a_unit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: fusion_tier on model auto replaces default_auto_tier entirely.
        client = _FakeClient()
        provider = _provider(client, monkeypatch, default_auto_tier="efficiency")

        await provider.execute(_agent(model="auto", fusion_tier="standard"), {}, "p")

        assert client.creates[0]["github"]["capi"] == {"fusion_tier": "standard"}

    async def test_default_model_selects_fusion_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _FakeClient()
        provider = _provider(client, monkeypatch, model="fusion", default_fusion_tier="high")

        await provider.execute(_agent(), {}, "p")

        assert client.creates[0]["model"] == "fusion"
        assert client.creates[0]["github"]["capi"] == {"fusion_tier": "high"}

    @pytest.mark.parametrize("model", ["hydrafusion", "hydrafusion-max"])
    async def test_deprecated_hydrafusion_models_pass_through(
        self, monkeypatch: pytest.MonkeyPatch, model: str
    ) -> None:
        client = _FakeClient()
        provider = _provider(client, monkeypatch)

        await provider.execute(_agent(model=model), {}, "p")

        assert client.creates[0]["model"] == model
        assert "capi" not in client.creates[0].get("github", {})

    async def test_deprecated_auto_tier_identifier_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _FakeClient()
        provider = _provider(client, monkeypatch)

        await provider.execute(_agent(model="auto", auto_tier="hydrafusion"), {}, "p")

        assert client.creates[0]["github"]["capi"] == {"auto_tier": "hydrafusion"}

    async def test_resume_carries_preference(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Requirement: a resumed session receives the same explicit preference.
        client = _FakeClient()
        provider = _provider(client, monkeypatch)
        provider.set_resume_session_ids({"writer": "persisted"})

        await provider.execute(_agent(model="fusion", fusion_tier="max"), {}, "p")

        (resume,) = client.resumes
        assert resume[0] == "persisted"
        assert resume[1]["github"]["capi"] == {"fusion_tier": "max"}
        assert client.creates == []

    async def test_resume_without_preference_preserves_persisted_tier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: omission on resume lets the runtime restore its persisted tier.
        client = _FakeClient()
        provider = _provider(client, monkeypatch)
        provider.set_resume_session_ids({"writer": "persisted"})

        await provider.execute(_agent(model="fusion"), {}, "p")

        assert "capi" not in client.resumes[0][1].get("github", {})

    async def test_dialog_turn_uses_only_matching_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _FakeClient(turns=[[_message("reply"), _IDLE]])
        provider = _provider(client, monkeypatch, model="fusion", default_fusion_tier="high")

        assert await provider.execute_dialog_turn("sys", "hi") == "reply"
        await provider.execute_dialog_turn("sys", "hi", model="gpt-5")

        assert client.creates[0]["github"]["capi"] == {"fusion_tier": "high"}
        assert "capi" not in client.creates[1].get("github", {})

    async def test_concurrent_calls_keep_their_own_preferences(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: one shared provider never leaks a preference across calls.
        client = _FakeClient()
        provider = _provider(client, monkeypatch, default_auto_tier="efficiency")

        await asyncio.gather(
            provider.execute(_agent(name="a", model="fusion", fusion_tier="max"), {}, "p"),
            provider.execute(_agent(name="b", model="auto"), {}, "p"),
            provider.execute(_agent(name="c", model="gpt-5"), {}, "p"),
        )

        by_model = {
            create["model"]: create.get("github", {}).get("capi") for create in client.creates
        }
        assert by_model == {
            "fusion": {"fusion_tier": "max"},
            "auto": {"auto_tier": "efficiency"},
            "gpt-5": None,
        }


class TestRefusals:
    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"model": "gpt-5", "fusion_tier": "high"}, "fusion_tier requires model"),
            ({"model": "fusion", "auto_tier": "efficiency"}, "auto_tier requires model"),
            ({"model": "{{ x }}", "fusion_tier": "high"}, "fusion_tier requires model"),
            ({"model": "fusion", "fusion_tier": "{{ tier }}"}, "unresolved"),
        ],
    )
    async def test_incompatible_preference_fails_before_session(
        self, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, str], message: str
    ) -> None:
        client = _FakeClient()
        provider = _provider(client, monkeypatch)

        with pytest.raises(ValidationError, match=message):
            await provider.execute(_agent(**kwargs), {}, "p")

        assert client.creates == []

    async def test_byok_routing_refuses_preferences(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient()
        settings = ProviderSettings(name="copilot", base_url="http://localhost:11434/v1")
        provider = _provider(client, monkeypatch, provider_settings=settings, model="m")

        with pytest.raises(ValidationError, match="BYOK"):
            await provider.execute(_agent(model="fusion", fusion_tier="high"), {}, "p")
        assert client.creates == []

    async def test_runtime_url_is_not_byok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Requirement: an external runtime transport still reaches the Copilot router.
        client = _FakeClient()
        settings = ProviderSettings(name="copilot", runtime_url="localhost:4321")
        provider = _provider(client, monkeypatch, provider_settings=settings)

        await provider.execute(_agent(model="fusion", fusion_tier="high"), {}, "p")

        assert client.creates[0]["github"]["capi"] == {"fusion_tier": "high"}

    async def test_both_defaults_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            CopilotProvider(default_fusion_tier="high", default_auto_tier="efficiency")

    async def test_runtime_rejection_is_surfaced_not_downgraded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: a runtime that refuses the tier fails the call; it is never
        # retried without the preference.
        client = _FakeClient()
        client.fail_create = RuntimeError("fusion is not enabled for this account")
        provider = _provider(client, monkeypatch)

        with pytest.raises(ProviderError, match="fusion is not enabled"):
            await provider.execute(_agent(model="fusion", fusion_tier="high"), {}, "p")

        assert client.creates and all(
            create["github"]["capi"] == {"fusion_tier": "high"} for create in client.creates
        )

    async def test_failed_resume_falls_back_with_the_same_preference(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _FakeClient()

        async def failing_resume(session_id: str, **kwargs: Any) -> Any:
            client.resumes.append((session_id, kwargs))
            raise RuntimeError("session gone")

        monkeypatch.setattr(client, "resume_session", failing_resume)
        provider = _provider(client, monkeypatch)
        provider.set_resume_session_ids({"writer": "persisted"})

        await provider.execute(_agent(model="auto", auto_tier="efficiency"), {}, "p")

        assert client.resumes[0][1]["github"]["capi"] == {"auto_tier": "efficiency"}
        assert client.creates[0]["github"]["capi"] == {"auto_tier": "efficiency"}


class TestRoutingRejection:
    @pytest.mark.parametrize("capi", [None, {"auto_tier": "efficiency"}, {"fusion_tier": "high"}])
    async def test_managed_followup_honors_retained_preference_rejection(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capi: dict[str, str] | None
    ) -> None:
        from unittest.mock import Mock

        from conductor.execution.local_process_scope import LocalProcessScopeController
        from conductor.execution.types import ManagedWorkspaceContext
        from conductor.providers.copilot import _ManagedCopilotSession

        rejected = _event(
            "session.auto_tier_switch_failed",
            reason="policy_rejected",
            requested_auto_tier="intelligence",
            effective_auto_tier="efficiency",
        )
        client = _FakeClient(turns=[[rejected, _message("answer"), _IDLE]])
        provider = _provider(client, monkeypatch)
        controller = Mock(spec=LocalProcessScopeController)
        workspace = ManagedWorkspaceContext(str(tmp_path), "managed-followup", controller)
        kwargs: dict[str, Any] = {"working_directory": str(tmp_path)}
        if capi is not None:
            kwargs["capi"] = capi
        handle = _ManagedCopilotSession(
            workspace.root, workspace.incarnation, "retained", str(tmp_path), kwargs
        )

        async def in_owned_context(*args: Any) -> Any:
            return await args[-1]()

        monkeypatch.setattr(provider, "_managed_call", in_owned_context)
        if capi is not None:
            with pytest.raises(ProviderError, match="policy_rejected") as error:
                await provider.send_followup(handle, "more", managed_workspace=workspace)
            assert error.value.is_retryable is False
            assert "requested_auto_tier='intelligence'" in str(error.value)
            assert "effective_auto_tier='efficiency'" in str(error.value)
        else:
            output = await provider.send_followup(handle, "more", managed_workspace=workspace)
            assert output.content == {"result": "answer"}

        expected: dict[str, Any] = {"working_directory": str(tmp_path)}
        if capi is not None:
            expected["github"] = {"capi": capi}
        assert client.resumes == [("retained", expected)]
        assert client.creates == []
        assert client.sessions[0].disconnected
        assert client.sessions[0].prompts == ["more"]

    @pytest.mark.parametrize("dialog", [False, True])
    @pytest.mark.parametrize("fusion", [False, True])
    async def test_answer_and_idle_cannot_hide_tier_rejection(
        self, monkeypatch: pytest.MonkeyPatch, dialog: bool, fusion: bool
    ) -> None:
        from copilot.session_events import (
            AutoTier,
            AutoTierSwitchFailureReason,
            SessionAutoTierSwitchFailedData,
        )

        rejected = _event("session.auto_tier_switch_failed")
        rejected.data = SessionAutoTierSwitchFailedData(
            reason=AutoTierSwitchFailureReason.POLICY_REJECTED,
            requested_auto_tier=AutoTier.INTELLIGENCE,
            effective_auto_tier=AutoTier.BALANCE,
        )
        client = _FakeClient(turns=[[rejected, _message("answer"), _IDLE]])
        defaults = (
            {"default_fusion_tier": "high"} if fusion else {"default_auto_tier": "intelligence"}
        )
        model = "fusion" if fusion else "auto"
        provider = _provider(client, monkeypatch, model=model, **defaults)

        with pytest.raises(ProviderError, match="routing preference rejected") as error:
            if dialog:
                await provider.execute_dialog_turn("sys", "hi")
            else:
                await provider.execute(_agent(model=model), {}, "p")

        assert error.value.is_retryable is False
        assert "policy_rejected" in str(error.value)
        assert "requested_auto_tier='intelligence'" in str(error.value)
        assert "effective_auto_tier='balance'" in str(error.value)
        assert len(client.creates) == 1
        assert len(client.sessions[0].prompts) == 1

    @pytest.mark.parametrize("dialog", [False, True])
    async def test_omitted_preference_does_not_refuse_runtime_tier_failure(
        self, monkeypatch: pytest.MonkeyPatch, dialog: bool
    ) -> None:
        rejected = _event(
            "session.auto_tier_switch_failed",
            reason="request_failed",
            requested_auto_tier="intelligence",
            effective_auto_tier="balance",
        )
        client = _FakeClient(turns=[[rejected, _message("answer"), _IDLE]])
        provider = _provider(client, monkeypatch, model="auto")

        if dialog:
            assert await provider.execute_dialog_turn("sys", "hi") == "answer"
        else:
            output = await provider.execute(_agent(model="auto"), {}, "p")
            assert output.content == {"result": "answer"}
        assert "capi" not in client.creates[0].get("github", {})

    async def test_rejected_fusion_replacement_does_not_mark_plain_auto_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _FakeClient()
        provider = _provider(client, monkeypatch)
        await provider.execute(_agent(model="auto", auto_tier="efficiency"), {}, "p")
        sid = client.sessions[0].session_id
        provider.set_resume_session_ids({"writer": sid})
        client.turns = [
            [
                _event("session.model_change", new_model="fusion"),
                _event(
                    "session.auto_tier_switch_failed",
                    reason="unsupported",
                    requested_auto_tier="intelligence",
                    effective_auto_tier="efficiency",
                ),
                _message("answer"),
                _IDLE,
            ]
        ]

        with pytest.raises(ProviderError, match="routing preference rejected"):
            await provider.execute(_agent(model="auto", fusion_tier="high"), {}, "p")

        assert sid not in provider._fusion_session_ids


class TestRoutingWarnings:
    @pytest.mark.parametrize("dialog", [False, True])
    @pytest.mark.parametrize(
        ("event_type", "details", "reason"),
        [
            (
                "session.fusion_completed",
                {"degraded_reason": "synthesis_timeout"},
                "synthesis_timeout",
            ),
            (
                "session.fusion_route_failed",
                {"reason": "policy_rejected", "fallback_model": "gpt-5"},
                "policy_rejected",
            ),
        ],
    )
    async def test_fusion_degradation_logs_and_emits_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        dialog: bool,
        event_type: str,
        details: dict[str, Any],
        reason: str,
    ) -> None:
        client = _FakeClient(turns=[[_event(event_type, **details), _message("reply"), _IDLE]])
        provider = _provider(client, monkeypatch)
        events: list[tuple[str, dict[str, Any]]] = []

        def capture(name: str, data: dict[str, Any]) -> None:
            events.append((name, data))

        with caplog.at_level(logging.WARNING, logger="conductor.providers.copilot"):
            if dialog:
                assert (
                    await provider.execute_dialog_turn(
                        "sys", "hi", model="fusion", event_callback=capture
                    )
                    == "reply"
                )
            else:
                await provider.execute(_agent(model="fusion"), {}, "p", event_callback=capture)

        warnings = [data for name, data in events if name == "agent_routing_warning"]
        assert len(warnings) == 1
        assert warnings[0]["reason"] == reason
        assert warnings[0]["source_event"] == event_type
        assert warnings[0]["message"] in caplog.text
        if event_type == "session.fusion_route_failed":
            assert warnings[0]["fallback_model"] == "gpt-5"


class TestAttribution:
    async def test_fusion_usage_keeps_selector_label(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Requirement: aggregate Fusion tokens are never attributed to the last
        # constituent model, so they cannot be priced at its rates.
        client = _FakeClient(
            turns=[
                [
                    _event("assistant.fusion_phase_started", phase_id="p1"),
                    _usage("claude-opus-4.8", 100, 10, "c1"),
                    _usage("gpt-5", 200, 20, "c2"),
                    _message("done"),
                    _IDLE,
                ]
            ]
        )
        provider = _provider(client, monkeypatch)

        output = await provider.execute(_agent(model="fusion", fusion_tier="high"), {}, "p")

        assert output.model == "fusion"
        assert (output.input_tokens, output.output_tokens) == (300, 30)

    async def test_auto_with_fusion_tier_keeps_auto_label_without_events(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: a requested Fusion tier is composite even if the runtime
        # reports a single constituent model.
        client = _FakeClient(turns=[[_usage("gpt-5", 10, 1, "c1"), _message("x"), _IDLE]])
        provider = _provider(client, monkeypatch)

        output = await provider.execute(_agent(model="auto", fusion_tier="max"), {}, "p")

        assert output.model == "auto"

    async def test_plain_auto_reports_single_concrete_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _FakeClient(
            turns=[[_usage("gpt-5", 10, 1, "c1"), _usage("gpt-5", 5, 1, "c2"), _IDLE]]
        )
        provider = _provider(client, monkeypatch)

        output = await provider.execute(_agent(model="auto", auto_tier="efficiency"), {}, "p")

        assert output.model == "gpt-5"
        assert output.input_tokens == 15

    async def test_mixed_model_auto_keeps_selector_label(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _FakeClient(
            turns=[[_usage("gpt-5", 10, 1, "c1"), _usage("claude-opus-4.8", 5, 1, "c2"), _IDLE]]
        )
        provider = _provider(client, monkeypatch)

        output = await provider.execute(_agent(model="auto"), {}, "p")

        assert output.model == "auto"

    async def test_missing_usage_model_falls_back_to_configured_label(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _FakeClient(turns=[[_usage(None, 10, 1, "c1"), _IDLE]])
        provider = _provider(client, monkeypatch)

        output = await provider.execute(_agent(model="auto"), {}, "p")

        assert output.model == "auto"
        assert output.input_tokens == 10

    async def test_duplicate_usage_is_counted_once_and_aggregate_never_added(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: dedup still applies, and fusion_completed totals (which
        # cover the same calls) are not added on top of assistant.usage.
        completed = _event(
            "session.fusion_completed",
            input_tokens=999,
            output_tokens=99,
            cached_tokens=9,
            cache_write_tokens=None,
            degraded_reason=None,
        )
        client = _FakeClient(
            turns=[
                [
                    _usage("a", 100, 10, "c1"),
                    _usage("a", 100, 10, "c1"),
                    _usage("b", 50, 5, "c2"),
                    completed,
                    _message("x"),
                    _IDLE,
                ]
            ]
        )
        provider = _provider(client, monkeypatch)

        output = await provider.execute(_agent(model="fusion", fusion_tier="high"), {}, "p")

        assert (output.input_tokens, output.output_tokens) == (150, 15)
        assert output.cache_read_tokens is None

    async def test_aggregate_is_fallback_evidence_only(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        completed = _event(
            "session.fusion_completed",
            input_tokens=400,
            output_tokens=40,
            cached_tokens=100,
            cache_write_tokens=None,
            degraded_reason="synthesis_timeout",
        )
        client = _FakeClient(turns=[[completed, _message("x"), _IDLE]])
        provider = _provider(client, monkeypatch)

        with caplog.at_level(logging.WARNING, logger="conductor.providers.copilot"):
            output = await provider.execute(_agent(model="fusion", fusion_tier="high"), {}, "p")

        assert (output.input_tokens, output.output_tokens, output.cache_read_tokens) == (
            400,
            40,
            100,
        )
        assert output.model == "fusion"
        assert "completed degraded: synthesis_timeout" in caplog.text

    async def test_route_failure_is_warned(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        failed = _event(
            "session.fusion_route_failed", reason="policy_rejected", fallback_model="gpt-5"
        )
        client = _FakeClient(turns=[[failed, _usage("gpt-5", 1, 1, "c1"), _IDLE]])
        provider = _provider(client, monkeypatch)

        with caplog.at_level(logging.WARNING, logger="conductor.providers.copilot"):
            output = await provider.execute(_agent(model="fusion", fusion_tier="high"), {}, "p")

        assert "policy_rejected" in caplog.text and "gpt-5" in caplog.text
        assert output.model == "fusion"

    async def test_parse_recovery_merges_constituent_models(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: a recovery call to a different model makes the turn composite.
        client = _FakeClient(
            turns=[
                [_usage("gpt-5", 10, 1, "c1"), _message("not json"), _IDLE],
                [_usage("claude-opus-4.8", 20, 2, "c2"), _message('{"answer": "a"}'), _IDLE],
            ]
        )
        provider = _provider(client, monkeypatch)
        agent = _agent(model="auto", output={"answer": {"type": "string"}})

        output = await provider.execute(agent, {}, "p")

        assert output.content == {"answer": "a"}
        assert output.model == "auto"
        assert output.input_tokens == 30

    async def test_partial_output_keeps_fusion_attribution(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _FakeClient()
        provider = _provider(client, monkeypatch)

        async def interrupted(*_args: Any, **_kwargs: Any) -> SDKResponse:
            return SDKResponse(
                content="part", input_tokens=5, output_tokens=1, partial=True, models=("gpt-5",)
            )

        monkeypatch.setattr(provider, "_send_and_wait", interrupted)

        output = await provider.execute(_agent(model="auto", fusion_tier="high"), {}, "p")

        assert output.partial
        assert output.model == "auto"

    async def test_followup_on_fusion_session_keeps_selector(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _FakeClient(turns=[[_usage("gpt-5", 1, 1, "c1"), _message("x"), _IDLE]])
        provider = _provider(client, monkeypatch)
        await provider.execute(_agent(model="auto", fusion_tier="high"), {}, "p")
        session = _FakeSession("session-1", [[_usage("gpt-5", 2, 1, "c9"), _IDLE]])

        output = await provider.send_followup(session, "more", agent_model="auto")

        assert output.model == "auto"

    async def test_followup_on_plain_auto_session_reports_concrete_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _provider(_FakeClient(), monkeypatch)
        session = _FakeSession("other", [[_usage("gpt-5", 2, 1, "c9"), _IDLE]])

        output = await provider.send_followup(session, "more", agent_model="auto")

        assert output.model == "gpt-5"

    @pytest.mark.parametrize(
        "event_type", ["session.start", "session.resume", "session.model_change"]
    )
    @pytest.mark.parametrize(
        "replacement", ["confirmed", "unconfirmed", "mismatched", "omitted", "rejected"]
    )
    async def test_resumed_routing_survives_interrupt_and_actual_handle_followup(
        self, monkeypatch: pytest.MonkeyPatch, event_type: str, replacement: str
    ) -> None:
        from copilot.session_events import AutoTier

        model_field = "new_model" if event_type == "session.model_change" else "selected_model"
        client = _FakeClient(
            turns=[
                [
                    _event(event_type, auto_tier=AutoTier.INTELLIGENCE, **{model_field: "auto"}),
                    _usage("gpt-5", 1, 1, "initial"),
                    _message("x"),
                    _IDLE,
                ]
            ]
        )
        provider = _provider(client, monkeypatch)
        await provider.execute(_agent(model="auto", fusion_tier="high"), {}, "p")
        sid = client.sessions[0].session_id
        assert sid in provider._fusion_session_ids
        provider.set_resume_session_ids({"writer": sid})
        confirmation = _event(event_type, auto_tier=AutoTier.EFFICIENCY, **{model_field: "auto"})
        if replacement == "mismatched":
            confirmation.data.auto_tier = AutoTier.INTELLIGENCE

        if replacement == "rejected":
            client.turns = [
                [
                    confirmation,
                    _event(
                        "session.auto_tier_switch_failed",
                        reason="policy_rejected",
                        requested_auto_tier="efficiency",
                        effective_auto_tier="intelligence",
                    ),
                    _message("answer"),
                    _IDLE,
                ]
            ]
            with pytest.raises(ProviderError, match="routing preference rejected"):
                await provider.execute(_agent(model="auto", auto_tier="efficiency"), {}, "p")
            assert sid in provider._fusion_session_ids

        events = [] if replacement in ("unconfirmed", "rejected") else [confirmation]
        client.turns = [
            [*events, _usage("gpt-5", 2, 1, "partial"), _message("partial")],
            [_usage("gpt-5", 3, 1, "followup"), _message("answer"), _IDLE],
        ]
        signal = asyncio.Event()
        signal.set()
        tier = (
            {"auto_tier": "efficiency"}
            if replacement in ("confirmed", "unconfirmed", "mismatched")
            else {}
        )
        partial = await provider.execute(
            _agent(model="auto", **tier), {}, "p", interrupt_signal=signal
        )
        handle = provider.get_interrupted_session()
        assert partial.partial
        assert handle is client.sessions[-1]
        assert not handle.disconnected
        expected = "gpt-5" if replacement == "confirmed" else "auto"
        assert partial.model == expected

        followup = await provider.send_followup(handle, "more", agent_model="auto")

        assert followup.model == expected
        assert handle.disconnected
        assert (sid in provider._fusion_session_ids) == (replacement != "confirmed")

    async def test_fusion_turn_evidence_does_not_persist_a_preference(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _FakeClient(
            turns=[[_event("session.fusion_started"), _usage("gpt-5", 1, 1, "c1"), _IDLE]]
        )
        provider = _provider(client, monkeypatch)

        output = await provider.execute(_agent(model="auto"), {}, "p")

        assert output.model == "auto"
        assert client.sessions[0].session_id not in provider._fusion_session_ids

    @pytest.mark.parametrize("model", ["auto", "fusion", "hydrafusion", "hydrafusion-max"])
    async def test_selector_labels_have_no_provider_price_or_window(
        self, monkeypatch: pytest.MonkeyPatch, model: str
    ) -> None:
        billing = SimpleNamespace(
            token_prices=SimpleNamespace(batch_size=1000, input_price=2.0, output_price=4.0)
        )
        capabilities = SimpleNamespace(limits=SimpleNamespace(max_prompt_tokens=128000))
        client = _FakeClient()

        async def list_models() -> list[Any]:
            return [
                SimpleNamespace(id=model, billing=billing, capabilities=capabilities),
                SimpleNamespace(id="gpt-4.1", billing=billing, capabilities=capabilities),
            ]

        monkeypatch.setattr(client, "list_models", list_models)
        provider = _provider(client, monkeypatch)

        assert await provider.get_model_pricing(model) is None
        assert await provider.get_max_prompt_tokens(model) is None
        pricing = await provider.get_model_pricing("gpt-4.1")
        assert pricing is not None
        assert pricing.input_per_mtok == 20.0
        assert pricing.output_per_mtok == 40.0
        assert await provider.get_max_prompt_tokens("gpt-4.1") == 128000
