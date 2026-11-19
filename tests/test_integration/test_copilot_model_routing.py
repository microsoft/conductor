"""Copilot Fusion / Auto routing from workflow YAML to the SDK session.

The hermetic tests drive ``load_config`` -> ``ProviderRegistry`` ->
``WorkflowEngine`` -> ``AgentExecutor`` -> ``CopilotProvider`` against a fake
SDK client, asserting the exact ``capi`` session arguments each agent produces
and how the engine accounts for composite Fusion usage.

The ``real_api`` cases are opt-in smoke tests against a live, eligible
Copilot account: ``uv run pytest tests/test_integration/test_copilot_model_routing.py
-m real_api -v``. They consume Copilot quota and stay out of default runs.
"""

from __future__ import annotations

import os
import shutil
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conductor.config.loader import load_config
from conductor.engine.workflow import WorkflowEngine
from conductor.exceptions import ValidationError
from conductor.providers.copilot import CopilotProvider
from conductor.providers.registry import ProviderRegistry


def _event(event_type: str, **data: Any) -> Any:
    return SimpleNamespace(type=SimpleNamespace(value=event_type), data=SimpleNamespace(**data))


def _usage(model: str, input_tokens: int, output_tokens: int, call_id: str) -> Any:
    return _event(
        "assistant.usage",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=None,
        cache_write_tokens=None,
        api_call_id=call_id,
    )


class _Session:
    def __init__(self, session_id: str, events: list[Any]) -> None:
        self.session_id = session_id
        self._events = events
        self._handler: Any = None

    def on(self, handler: Any) -> None:
        self._handler = handler

    async def send(self, prompt: str) -> None:
        for event in self._events:
            self._handler(event)

    async def disconnect(self) -> None:
        return None


class _Client:
    """Fake SDK client scripting one turn per model selector."""

    def __init__(self) -> None:
        self.creates: list[dict[str, Any]] = []

    async def create_session(self, **kwargs: Any) -> _Session:
        self.creates.append(kwargs)
        model = kwargs["model"]
        if model == "fusion":
            events = [
                _event("session.fusion_route_started", fusion_id="f1"),
                _usage("claude-opus-4.8", 1000, 100, "c1"),
                _usage("gpt-5", 2000, 200, "c2"),
                _event("assistant.message", content='{"answer": "fused"}'),
            ]
        else:
            events = [
                _usage("gpt-4.1", 300, 30, "c3"),
                _event("assistant.message", content='{"verdict": "fine"}'),
            ]
        return _Session(f"s{len(self.creates)}", [*events, _event("session.idle")])

    async def list_models(self) -> list[Any]:
        return []


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> _Client:
    client = _Client()

    async def started(self: CopilotProvider) -> None:
        self._client = client
        self._started = True

    async def connected(self: CopilotProvider) -> bool:
        return True

    monkeypatch.setattr(CopilotProvider, "_ensure_client_started", started)
    monkeypatch.setattr(CopilotProvider, "validate_connection", connected)
    return client


def _write(tmp_path: Path, runtime: str, extra_agent: str = "", cost: str = "") -> Path:
    path = tmp_path / "workflow.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            workflow:
              name: routing
              entry_point: answer
              runtime:
            {runtime}
            {cost}
            agents:
              - name: answer
                tools: []
                prompt: "Answer {{{{ workflow.input.tier }}}}"
                output:
                  answer: {{type: string}}
                routes: [{{to: review}}]
              - name: review
                model: auto
                tools: []
            {extra_agent}
                prompt: Review
                output:
                  verdict: {{type: string}}
                routes: [{{to: $end}}]
            output:
              answer: "{{{{ answer.output.answer }}}}"
            """
        ).format(
            runtime=textwrap.indent(textwrap.dedent(runtime), "    "),
            cost=textwrap.indent(textwrap.dedent(cost), "  "),
            extra_agent=textwrap.indent(textwrap.dedent(extra_agent), "    "),
        ),
        encoding="utf-8",
    )
    return path


async def _run(path: Path, inputs: dict[str, Any]) -> WorkflowEngine:
    config = load_config(path)
    async with ProviderRegistry(config) as registry:
        engine = WorkflowEngine(config, registry=registry, workflow_path=path)
        await engine.run(inputs)
    return engine


@pytest.mark.asyncio
async def test_yaml_preferences_reach_sdk_sessions(fake_client: _Client, tmp_path: Path) -> None:
    # Requirement: runtime defaults and agent overrides reach create_session exactly.
    path = _write(
        tmp_path,
        "provider: copilot\ndefault_model: fusion\ndefault_fusion_tier: high\n",
        extra_agent="auto_tier: efficiency\n",
    )

    await _run(path, {"tier": "x"})

    answer, review = fake_client.creates
    assert (answer["model"], answer["github"]["capi"]) == ("fusion", {"fusion_tier": "high"})
    assert (review["model"], review["github"]["capi"]) == ("auto", {"auto_tier": "efficiency"})


@pytest.mark.asyncio
async def test_unset_preferences_are_omitted(fake_client: _Client, tmp_path: Path) -> None:
    path = _write(tmp_path, "provider: copilot\ndefault_model: fusion\n")

    await _run(path, {"tier": "x"})

    assert all("capi" not in create.get("github", {}) for create in fake_client.creates)


@pytest.mark.asyncio
async def test_templated_agent_tier_is_rendered(fake_client: _Client, tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "provider: copilot\ndefault_model: fusion\n",
        extra_agent='fusion_tier: "{{ workflow.input.tier }}"\n',
    )

    await _run(path, {"tier": "max"})

    assert fake_client.creates[1]["github"]["capi"] == {"fusion_tier": "max"}


@pytest.mark.asyncio
async def test_incompatible_run_is_refused_before_any_session(
    fake_client: _Client, tmp_path: Path
) -> None:
    # Requirement: conductor run refuses what conductor validate reports.
    path = tmp_path / "workflow.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            workflow:
              name: routing
              entry_point: answer
              runtime: {provider: copilot}
            agents:
              - name: answer
                model: gpt-5
                fusion_tier: high
                prompt: hi
                routes: [{to: $end}]
            output: {}
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="fusion_tier requires model"):
        await _run(path, {})
    assert fake_client.creates == []


@pytest.mark.asyncio
async def test_engine_accounts_fusion_conservatively(fake_client: _Client, tmp_path: Path) -> None:
    # Requirement: composite Fusion tokens are recorded under the selector label with
    # unknown cost, while single-model Auto keeps concrete attribution.
    path = _write(tmp_path, "provider: copilot\ndefault_model: fusion\ndefault_fusion_tier: max\n")

    engine = await _run(path, {"tier": "x"})

    answer, review = engine.usage_tracker.get_summary().agents
    assert (answer.model, answer.input_tokens, answer.output_tokens) == ("fusion", 3000, 300)
    assert answer.cost_usd is None
    assert review.model == "gpt-4.1"


@pytest.mark.asyncio
async def test_explicit_pricing_override_prices_the_selector(
    fake_client: _Client, tmp_path: Path
) -> None:
    path = _write(
        tmp_path,
        "provider: copilot\ndefault_model: fusion\ndefault_fusion_tier: standard\n",
        cost="cost:\n  pricing:\n    fusion: {input_per_mtok: 1.0, output_per_mtok: 10.0}\n",
    )

    engine = await _run(path, {"tier": "x"})

    answer = engine.usage_tracker.get_summary().agents[0]
    assert answer.model == "fusion"
    assert answer.cost_usd == pytest.approx(3000 / 1e6 * 1.0 + 300 / 1e6 * 10.0)


# --------------------------------------------------------------------------
# Live smoke tests (opt in with -m real_api; they consume Copilot quota)
# --------------------------------------------------------------------------


def _live_provider(**kwargs: Any) -> CopilotProvider:
    if shutil.which("copilot") is None and not os.environ.get("COPILOT_PROVIDER_RUNTIME_URL"):
        try:
            import copilot as _sdk

            if not (Path(_sdk.__file__).parent / "bin" / "copilot").exists():
                pytest.skip("Copilot CLI not available")
        except ImportError:
            pytest.skip("Copilot SDK not available")
    return CopilotProvider(**kwargs)


def _live_agent(**kwargs: Any) -> Any:
    from conductor.config.schema import AgentDef

    return AgentDef(name=kwargs.pop("name", "live"), prompt="p", tools=[], **kwargs)


@pytest.mark.real_api
@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["standard", "high", "max"])
async def test_live_fusion_tiers_answer(tier: str) -> None:
    provider = _live_provider()
    try:
        output = await provider.execute(
            _live_agent(model="fusion", fusion_tier=tier, output={"answer": {"type": "string"}}),
            {},
            'Reply with the JSON object {"answer": "ok"}.',
        )
    finally:
        await provider.close()
    assert output.content.get("answer")
    assert output.model == "fusion"


@pytest.mark.real_api
@pytest.mark.asyncio
async def test_live_plain_auto_answers() -> None:
    provider = _live_provider()
    try:
        output = await provider.execute(
            _live_agent(model="auto", auto_tier="efficiency"), {}, "Say hello in one word."
        )
    finally:
        await provider.close()
    assert output.content.get("result")


@pytest.mark.real_api
@pytest.mark.asyncio
async def test_live_fusion_resume_keeps_preference() -> None:
    provider = _live_provider()
    agent = _live_agent(model="fusion", fusion_tier="high")
    try:
        await provider.execute(agent, {}, "Remember the word 'quartz'. Reply 'ok'.")
        provider.set_resume_session_ids(provider.get_session_ids())
        provider.set_resume_session_cwds(provider.get_session_cwds())
        output = await provider.execute(agent, {}, "Which word did I ask you to remember?")
    finally:
        await provider.close()
    assert "quartz" in str(output.content).lower()


@pytest.mark.real_api
@pytest.mark.asyncio
async def test_live_fusion_through_external_runtime_url() -> None:
    # An existing runtime is a transport, not BYOK routing, so Fusion still applies.
    if not os.environ.get("COPILOT_PROVIDER_RUNTIME_URL"):
        pytest.skip("Set COPILOT_PROVIDER_RUNTIME_URL to an authenticated headless runtime")
    provider = CopilotProvider()
    try:
        output = await provider.execute(
            _live_agent(model="fusion", fusion_tier="standard"), {}, "Say hello in one word."
        )
    finally:
        await provider.close()
    assert output.content.get("result")
    assert output.model == "fusion"
