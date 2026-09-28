"""Billing-provenance tests for the ``claude-agent-sdk`` provider.

``_derive_billing`` is exercised as a pure function over explicitly built
``EffectiveAuthContext`` / ``ClaudeAuthStatus`` values, so no test reads the live
``os.environ`` or a real login. Execute-level tests drive the real SDK through
``FakeTransport`` (no process is spawned) and control provenance explicitly: the
autouse ``_stub_claude_auth_readiness`` fixture yields a status without a
``subscription_type``, so every *un*controlled execution derives ``unknown``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip(
    "claude_agent_sdk",
    reason="claude-agent-sdk extra not installed",
)

from conductor.config.schema import AgentDef  # noqa: E402
from conductor.exceptions import ProviderError  # noqa: E402
from conductor.providers import claude_agent_sdk as sdk_module  # noqa: E402
from conductor.providers.claude_agent_sdk import (  # noqa: E402
    ClaudeAgentSdkProvider,
    ClaudeAuthStatus,
    EffectiveAuthContext,
    _derive_billing,
)
from tests.test_providers.claude_sdk_harness import (  # noqa: E402
    HANG_GUARD_SECONDS,
    FakeTransport,
    assistant_frame,
    patch_sdk_entry,
    result_frame,
)

_AGENT = AgentDef(name="t", prompt="hi")

_LOGIN: dict[str, Any] = {
    "inferred_mode": "subscription",
    "subscription_type": "max",
    "api_provider": "firstParty",
}


def _context(
    env: dict[str, str],
    mode: str = "auto",
    *,
    setting_sources: tuple[str, ...] = (),
) -> EffectiveAuthContext:
    return EffectiveAuthContext(
        env_snapshot=env,
        resolved_cwd="/work",
        setting_sources=setting_sources,  # type: ignore[arg-type]
        cli_path=None,
        auth_mode=mode,  # type: ignore[arg-type]
    )


def _status(mode: str = "auto", *, ready: bool = True, **fields: Any) -> ClaudeAuthStatus:
    fields.setdefault("inferred_mode", "subscription")
    return ClaudeAuthStatus(requested_mode=mode, ready=ready, **fields)  # type: ignore[arg-type]


_KEY = {"ANTHROPIC_API_KEY": "sk-ant-key"}

# (id, requested mode, env, setting_sources, status kwargs, ready, expected mode, reason)
_TRUTH_TABLE: list[Any] = [
    (1, "api_key", _KEY, (), {"inferred_mode": "api_key"}, True, "metered_api", "api_key"),
    (
        2,
        "api_key",
        {**_KEY, "ANTHROPIC_BASE_URL": "https://gw.invalid"},
        (),
        {"inferred_mode": "api_key"},
        True,
        "unknown",
        "custom_endpoint",
    ),
    (3, "subscription", {}, (), _LOGIN, True, "subscription", "first_party_login"),
    (
        4,
        "subscription",
        {},
        (),
        {**_LOGIN, "api_provider": None},
        True,
        "subscription",
        "first_party_login",
    ),
    (
        5,
        "subscription",
        {},
        (),
        {**_LOGIN, "subscription_type": None},
        True,
        "unknown",
        "no_subscription_evidence",
    ),
    (
        "5b",
        "subscription",
        {},
        (),
        {**_LOGIN, "subscription_type": "   "},
        True,
        "unknown",
        "no_subscription_evidence",
    ),
    (
        6,
        "subscription",
        {},
        (),
        {**_LOGIN, "api_key_source": "env"},
        True,
        "unknown",
        "api_key_source_reported",
    ),
    (
        7,
        "subscription",
        {},
        (),
        {**_LOGIN, "api_provider": "bedrock"},
        True,
        "unknown",
        "non_first_party_backend",
    ),
    (
        "7b",
        "subscription",
        {},
        (),
        {**_LOGIN, "api_provider": "somethingElse"},
        True,
        "unknown",
        "non_first_party_backend",
    ),
    (8, "auto", _KEY, (), {"inferred_mode": "api_key"}, True, "metered_api", "api_key"),
    (
        9,
        "auto",
        {**_KEY, "CLAUDE_CODE_OAUTH_TOKEN": "oauth"},
        (),
        {"inferred_mode": "api_key"},
        True,
        "unknown",
        "competing_credentials",
    ),
    (
        10,
        "auto",
        {**_KEY, "ANTHROPIC_AUTH_TOKEN": "tok"},
        (),
        {"inferred_mode": "api_key"},
        True,
        "unknown",
        "gateway_token",
    ),
    (
        11,
        "auto",
        {"CLAUDE_CODE_USE_BEDROCK": "1"},
        (),
        _LOGIN,
        True,
        "unknown",
        "cloud_backend_selector",
    ),
    (
        12,
        "auto",
        {**_KEY, "CLAUDE_CODE_USE_VERTEX": "1"},
        (),
        {"inferred_mode": "api_key"},
        True,
        "unknown",
        "cloud_backend_selector",
    ),
    (13, "auto", {}, (), _LOGIN, True, "subscription", "first_party_login"),
    (14, "auto", {}, ("project",), _LOGIN, True, "unknown", "settings_tier"),
    (
        15,
        "auto",
        {"ANTHROPIC_BASE_URL": "https://gw.invalid"},
        (),
        _LOGIN,
        True,
        "unknown",
        "custom_endpoint",
    ),
    (
        16,
        "auto",
        _KEY,
        (),
        {"inferred_mode": "subscription"},
        True,
        "unknown",
        "inferred_mode_mismatch",
    ),
    (17, "auto", _KEY, (), {"inferred_mode": "api_key"}, False, "unknown", "not_ready"),
    (
        18,
        "subscription",
        {
            "ANTHROPIC_API_KEY": "sk-ant-key",
            "ANTHROPIC_AUTH_TOKEN": "tok",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth",
        },
        (),
        _LOGIN,
        True,
        "subscription",
        "first_party_login",
    ),
    # Blank values are "absent": presence is `.strip()` truthiness.
    (
        "19",
        "auto",
        {"ANTHROPIC_BASE_URL": "  ", "CLAUDE_CODE_USE_BEDROCK": ""},
        (),
        _LOGIN,
        True,
        "subscription",
        "first_party_login",
    ),
    # No key, but the drift cross-check says the readiness path saw an API key.
    (
        "20",
        "auto",
        {},
        (),
        {"inferred_mode": "api_key"},
        True,
        "unknown",
        "inferred_mode_mismatch",
    ),
]


class TestDeriveBillingTruthTable:
    @pytest.mark.parametrize(
        ("row", "mode", "env", "sources", "status", "ready", "expected", "reason"),
        _TRUTH_TABLE,
        ids=[f"row{r[0]}" for r in _TRUTH_TABLE],
    )
    def test_derive_billing_truth_table(
        self,
        row: object,
        mode: str,
        env: dict[str, str],
        sources: tuple[str, ...],
        status: dict[str, Any],
        ready: bool,
        expected: str,
        reason: str,
    ) -> None:
        context = _context(env, mode, setting_sources=sources)
        # The reason code is asserted so no row can pass through a different rule.
        assert _derive_billing(context, _status(mode, ready=ready, **status)) == (
            expected,
            reason,
        )

    def test_apiprovider_is_an_exclusion_never_an_identification(self) -> None:
        # ``firstParty`` with no subscription evidence must NOT read as a subscription.
        status = _status(inferred_mode="subscription", api_provider="firstParty")
        assert _derive_billing(_context({}), status) == ("unknown", "no_subscription_evidence")

    def test_row_18_reads_the_finalized_child_env_not_the_snapshot(self) -> None:
        context = _context({"ANTHROPIC_API_KEY": "sk-ant-key"}, "subscription")
        assert context.env_snapshot["ANTHROPIC_API_KEY"] == "sk-ant-key"
        assert context.finalized_child_env["ANTHROPIC_API_KEY"] == ""
        assert _derive_billing(context, _status("subscription", **_LOGIN))[0] == "subscription"


class TestCloudBackendSelectorsAreEachIndependentlyPinned:
    """Every supported cloud-backend selector gets its own dedicated row, naming the
    variable literally rather than iterating ``_CLOUD_BACKEND_SELECTORS`` itself.

    Rows 11/12 above only ever exercised Bedrock and Vertex, so a scratch mutation that
    dropped ``CLAUDE_CODE_USE_FOUNDRY`` from that tuple left the rest of this module's
    truth table green. These rows exist so the same class of omission -- for Foundry, or
    for a selector added later without a matching row here -- fails instead of passing
    silently.
    """

    @pytest.mark.parametrize(
        "selector",
        ["CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"],
    )
    def test_selector_forces_unknown_with_no_key_present(self, selector: str) -> None:
        # Otherwise-clean first-party login evidence: the selector alone must still win.
        context = _context({selector: "1"}, "auto")
        assert _derive_billing(context, _status("auto", **_LOGIN)) == (
            "unknown",
            "cloud_backend_selector",
        )

    @pytest.mark.parametrize(
        "selector",
        ["CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"],
    )
    def test_selector_forces_unknown_with_a_key_present(self, selector: str) -> None:
        # Otherwise-clean api_key evidence: the selector must still win before the key is read.
        context = _context({**_KEY, selector: "1"}, "auto")
        assert _derive_billing(context, _status("auto", inferred_mode="api_key")) == (
            "unknown",
            "cloud_backend_selector",
        )


class TestBuildOutputSignature:
    def test_build_output_billing_mode_is_required_keyword(self) -> None:
        param = inspect.signature(ClaudeAgentSdkProvider._build_output).parameters["billing_mode"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is inspect.Parameter.empty


def _reply_ok(transport: FakeTransport) -> None:
    transport.reply_with(assistant_frame("hello"), result_frame(result="hello"))


def _start(provider: ClaudeAgentSdkProvider, **kwargs: Any) -> asyncio.Task[Any]:
    return asyncio.create_task(
        provider.execute(agent=_AGENT, context={}, rendered_prompt="hi", **kwargs)
    )


class TestOutputCarriesBillingMode:
    async def test_normal_output_carries_billing_mode(self) -> None:
        transport = FakeTransport()
        _reply_ok(transport)
        with (
            patch_sdk_entry(transport),
            patch.object(sdk_module, "_derive_billing", return_value=("subscription", "t")),
        ):
            output = await asyncio.wait_for(_start(ClaudeAgentSdkProvider()), HANG_GUARD_SECONDS)
        assert output.partial is False
        assert output.billing_mode == "subscription"

    async def test_partial_interrupt_output_carries_billing_mode(self) -> None:
        transport = FakeTransport()  # never answers: the interrupt is the only exit
        interrupt = asyncio.Event()
        with (
            patch_sdk_entry(transport),
            patch.object(sdk_module, "_derive_billing", return_value=("metered_api", "t")),
        ):
            execution = _start(ClaudeAgentSdkProvider(), interrupt_signal=interrupt)
            while not transport.user_messages:
                await asyncio.sleep(0)
            interrupt.set()
            output = await asyncio.wait_for(execution, HANG_GUARD_SECONDS)
        assert output.partial is True
        assert output.billing_mode == "metered_api"

    async def test_uncontrolled_execution_derives_unknown(self) -> None:
        """The autouse readiness stub has no ``subscription_type``: never a guess."""
        transport = FakeTransport()
        _reply_ok(transport)
        with patch_sdk_entry(transport):
            output = await asyncio.wait_for(_start(ClaudeAgentSdkProvider()), HANG_GUARD_SECONDS)
        assert output.billing_mode == "unknown"

    async def test_billing_mode_is_per_execution_not_instance_state(self) -> None:
        async def _ready(self: ClaudeAgentSdkProvider, **kwargs: object) -> ClaudeAuthStatus:
            return ClaudeAuthStatus(
                requested_mode="auto",
                inferred_mode="subscription",
                ready=True,
                subscription_type="max",
            )

        provider = ClaudeAgentSdkProvider()
        base_env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
        outputs = []
        for extra in ({}, {"ANTHROPIC_BASE_URL": "https://gw.invalid"}, {}):
            transport = FakeTransport()
            _reply_ok(transport)
            with (
                patch.dict(os.environ, {**base_env, **extra}, clear=True),
                patch.object(ClaudeAgentSdkProvider, "_check_auth_readiness", _ready),
                patch_sdk_entry(transport),
            ):
                outputs.append(await asyncio.wait_for(_start(provider), HANG_GUARD_SECONDS))
        assert [o.billing_mode for o in outputs] == ["subscription", "unknown", "subscription"]

    async def test_timeout_raises_and_returns_no_output(self) -> None:
        transport = FakeTransport()  # never answers
        with patch_sdk_entry(transport):
            provider = ClaudeAgentSdkProvider(max_session_seconds=0.05)
            with pytest.raises(ProviderError, match="exceeded maximum session duration"):
                await asyncio.wait_for(_start(provider), HANG_GUARD_SECONDS)


_CANARY_KEY = "sk-ant-CANARY-api-key-7f3a"
_CANARY_TOKEN = "CANARY-gateway-token-91bc"
_CANARY_OAUTH = "CANARY-oauth-token-52de"
_CANARY_HOST = "canary-gateway-4471.example.invalid"
_CANARIES = (_CANARY_KEY, _CANARY_TOKEN, _CANARY_OAUTH, _CANARY_HOST)


class TestCredentialCanary:
    async def test_credential_canary(self, caplog: pytest.LogCaptureFixture) -> None:
        """No credential, token, or endpoint host reaches output, events, or logs.

        The base URL and tokens force ``unknown``, proving this run exercised the
        derivation rather than skipping it.
        """
        events: list[tuple[str, dict[str, Any]]] = []

        def _callback(event_type: str, data: dict[str, Any]) -> None:
            events.append((event_type, data))

        transport = FakeTransport()
        _reply_ok(transport)
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "ANTHROPIC_API_KEY": _CANARY_KEY,
            "ANTHROPIC_AUTH_TOKEN": _CANARY_TOKEN,
            "CLAUDE_CODE_OAUTH_TOKEN": _CANARY_OAUTH,
            "ANTHROPIC_BASE_URL": f"https://{_CANARY_HOST}/v1",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch_sdk_entry(transport),
            caplog.at_level(logging.DEBUG),
        ):
            output = await asyncio.wait_for(
                _start(ClaudeAgentSdkProvider(), event_callback=_callback), HANG_GUARD_SECONDS
            )

        assert output.billing_mode == "unknown"
        surfaces = {
            "output": json.dumps(dataclasses.asdict(output), default=str),
            "events": json.dumps(events, default=str),
            "logs": caplog.text,
        }
        for name, text in surfaces.items():
            for canary in _CANARIES:
                assert canary not in text, f"{canary!r} leaked into {name}"
        assert "billing_mode=unknown" in caplog.text  # the derivation did run and log
        assert "reason=" in caplog.text


class TestCredentialCanaryFleetSurfaces:
    """The canary's ``unknown`` result, carried through every downstream surface."""

    async def test_no_canary_reaches_usage_records_or_fleet_surfaces(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import tempfile

        from conductor.billing import AggregateBilling, cell_legend
        from conductor.engine.usage import UsageTracker
        from conductor.fleet.history import build_history_entries
        from conductor.fleet.records import RunRecord, TerminalRunRecord
        from conductor.fleet.retention import event_log_root
        from conductor.fleet.summary import derive_run_detail, derive_run_summary
        from conductor.fleet.tui.screens.history import _cost_basis_lines, _format_cost
        from conductor.fleet.tui.screens.run_detail import _format_agent_cost
        from conductor.fleet.tui.screens.runs import _format_cost as _runs_format_cost
        from conductor.fleet.tui.screens.runs import _preview_text, _summary_bar_text

        transport = FakeTransport()
        _reply_ok(transport)
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "ANTHROPIC_API_KEY": _CANARY_KEY,
            "ANTHROPIC_AUTH_TOKEN": _CANARY_TOKEN,
            "CLAUDE_CODE_OAUTH_TOKEN": _CANARY_OAUTH,
            "ANTHROPIC_BASE_URL": f"https://{_CANARY_HOST}/v1",
        }
        with patch.dict(os.environ, env, clear=True), patch_sdk_entry(transport):
            output = await asyncio.wait_for(_start(ClaudeAgentSdkProvider()), HANG_GUARD_SECONDS)
        assert output.billing_mode == "unknown"  # the base URL and tokens force it

        # Usage rows and the summary dict the engine builds from them.
        tracker = UsageTracker()
        row = tracker.record("t", output, elapsed=1.0)
        usage = tracker.get_summary()
        summary_dict = {
            "billing": usage.billing.to_wire(),
            "agents": [dataclasses.asdict(a) for a in usage.agents],
        }
        assert row.billing_mode == "unknown"

        # The terminal record as written to disk.
        record = TerminalRunRecord(
            run_id="canary01",
            workflow_path="/tmp/wf.yaml",
            workflow_name="wf",
            started_at="2026-01-01T00:00:00+00:00",
            ended_at="2026-01-01T00:05:00+00:00",
            status="success",
            output={},
            error_type=None,
            error_message=None,
            total_tokens=150,
            total_cost_usd=0.01,
            unpriced_agent_count=0,
            event_log_path="",
            bg_stderr_log=None,
            bg_stdout_log=None,
            billing=usage.billing,
        )

        # An event log carrying the mode the derivation produced, in the engine's shape.
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        completion = {
            "type": "agent_completed",
            "timestamp": 2.0,
            "data": {
                "agent_name": "t",
                "tokens": 150,
                "cost_usd": 0.01,
                "billing_mode": output.billing_mode,
            },
        }
        started = {
            "type": "workflow_started",
            "timestamp": 1.0,
            "data": {
                "name": "wf",
                "entry_point": "t",
                "agents": [{"name": "t", "type": "agent"}],
            },
        }
        log = event_log_root() / "conductor-wf-20260101-120000-cafe0001.events.jsonl"
        log.write_text(
            "\n".join(json.dumps(e) for e in (started, completion)) + "\n", encoding="utf-8"
        )
        run_record = RunRecord(
            run_id="cafe0001",
            pid=os.getpid(),
            workflow_path="/tmp/wf.yaml",
            workflow_name="wf",
            started_at="2026-01-01T00:00:00+00:00",
            event_log_path=str(log),
            port=8080,
            mode="bg",
            checkpoint_dir=None,
        )
        run_summary = derive_run_summary(run_record)
        detail = derive_run_detail(run_record)
        (entry,) = build_history_entries(keep_last=5)

        # The derivation reached every Fleet product (so the scan below is not vacuous).
        assert run_summary.billing == AggregateBilling({"unknown": 1})
        assert entry.billing == AggregateBilling({"unknown": 1})
        assert detail.agents[0].billing == AggregateBilling({"unknown": 1})
        assert _runs_format_cost(run_summary) == "~$0.01 src?"

        legend = cell_legend(a.billing for a in detail.agents)
        rendered = {
            "record json": json.dumps(record.to_dict(), default=str),
            "usage summary": json.dumps(summary_dict, default=str),
            "event log": log.read_text(encoding="utf-8"),
            "RunSummary repr": repr(run_summary),
            "AgentDetail repr": repr(detail.agents),
            "HistoryEntry repr": repr(entry),
            "runs cell": _runs_format_cost(run_summary),
            "history cell": _format_cost(entry),
            "detail cell": _format_agent_cost(detail.agents[0].cost_usd, detail.agents[0].billing),
            "summary bar": _summary_bar_text([run_summary]).plain,
            "preview": _preview_text(run_summary).main.plain,
            "legend": legend or "",
            "history notification": "\n".join(_cost_basis_lines(entry)),
        }
        for name, text in rendered.items():
            for canary in _CANARIES:
                assert canary not in text, f"{canary!r} leaked into {name}"
        assert rendered["legend"] == "src? = billing source unknown"
        assert rendered["history notification"] == "Cost basis: billing source unknown"
