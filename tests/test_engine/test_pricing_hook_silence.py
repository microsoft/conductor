"""Tests for surfacing a systemically silent provider pricing hook (issue #386).

#265 added ``AgentProvider.get_model_pricing`` so live pricing could replace the
static table, and warns once if that hook *raises*. It does not notice the
companion case: a hook that never raises and returns ``None`` for everything.

That is not hypothetical, though the root cause is more specific than "the SDK
removed the field". ``tokenPrices`` is still on the wire. In the pinned
``github-copilot-sdk`` 1.0.1 the SDK's hand-written ``client.ModelBilling``
declares only ``multiplier``, and its ``from_dict`` drops ``tokenPrices``
entirely -- so ``CopilotProvider``'s hook resolves ``None`` for every model it
can list. (Verified against both wheels: 1.0.9 parses ``tokenPrices`` and keeps
it, so bumping the SDK may be the real fix for #386. The project floor is
``>=1.0.0`` and the lock pins 1.0.1.)

Nothing raises, nothing logs, and models that happen to be in the static table
still report a plausible cost -- so the fact that live pricing is dead is
invisible, and the only symptom is that newer models show up as unpriced.

The distinction these tests pin:

- any model priced at all → ordinary, stay quiet.
- ``None`` for everything, with nothing ever priced → say so, once, at the end.

The verdict is deliberately drawn at end of run rather than on the Nth ``None``:
a hook that declines two models and then prices a third is working fine, and
per-model resolution order is nondeterministic under parallel/``for_each``
groups.
"""

from __future__ import annotations

import logging

import pytest

from conductor.config.schema import (
    AgentDef,
    LimitsConfig,
    OutputField,
    RouteDef,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.context import WorkflowContext
from conductor.engine.limits import LimitEnforcer
from conductor.engine.pricing import ModelPricing
from conductor.engine.workflow import WorkflowEngine
from conductor.providers.base import AgentProvider as AgentProviderBase
from conductor.providers.copilot import CopilotProvider


class _Recorder:
    """Minimal stand-in exposing just the latch state the helpers touch."""

    def __init__(self) -> None:
        self._pricing_hook_none_models: set[str] = set()
        self._pricing_hook_priced_any = False
        self._pricing_hook_silent_warned = False
        self.events: list[tuple[str, dict]] = []

    def _emit(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))

    # Bind the real implementations so these tests exercise production code.
    from conductor.engine.workflow import WorkflowEngine

    _note_pricing_hook_result = WorkflowEngine._note_pricing_hook_result
    _warn_if_pricing_hook_silent = WorkflowEngine._warn_if_pricing_hook_silent


def _priced() -> ModelPricing:
    return ModelPricing(input_per_mtok=1.0, output_per_mtok=2.0)


class TestSilentPricingHookIsSurfaced:
    def test_warns_once_when_nothing_is_ever_priced(self, caplog: pytest.LogCaptureFixture) -> None:
        r = _Recorder()
        r._note_pricing_hook_result("claude-opus-5", None)
        r._note_pricing_hook_result("gpt-5.6-sol", None)

        with caplog.at_level(logging.WARNING, logger="conductor.engine.workflow"):
            r._warn_if_pricing_hook_silent()
            r._warn_if_pricing_hook_silent()

        assert len(caplog.records) == 1

    def test_warning_names_the_models(self, caplog: pytest.LogCaptureFixture) -> None:
        r = _Recorder()
        r._note_pricing_hook_result("claude-opus-5", None)
        r._note_pricing_hook_result("gpt-5.6-sol", None)

        with caplog.at_level(logging.WARNING, logger="conductor.engine.workflow"):
            r._warn_if_pricing_hook_silent()

        message = caplog.records[0].getMessage()
        assert "claude-opus-5" in message
        assert "gpt-5.6-sol" in message

    def test_a_single_unpriced_model_still_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """No minimum-model floor.

        Most shipped examples resolve exactly one model, and #386's own
        reproduction is a single-model run -- a floor of two would exempt
        precisely the runs most likely to hit this.
        """
        r = _Recorder()
        r._note_pricing_hook_result("claude-opus-5", None)

        with caplog.at_level(logging.WARNING, logger="conductor.engine.workflow"):
            r._warn_if_pricing_hook_silent()

        assert len(caplog.records) == 1

    def test_quiet_when_anything_was_priced(self, caplog: pytest.LogCaptureFixture) -> None:
        r = _Recorder()
        r._note_pricing_hook_result("a", None)
        r._note_pricing_hook_result("b", None)
        r._note_pricing_hook_result("c", _priced())

        with caplog.at_level(logging.WARNING, logger="conductor.engine.workflow"):
            r._warn_if_pricing_hook_silent()

        assert caplog.records == []

    def test_quiet_when_the_hook_was_never_asked(self, caplog: pytest.LogCaptureFixture) -> None:
        r = _Recorder()
        with caplog.at_level(logging.WARNING, logger="conductor.engine.workflow"):
            r._warn_if_pricing_hook_silent()
        assert caplog.records == []

    def test_declining_then_pricing_never_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """The bug in an early verdict: order decides the answer.

        Deciding on the second ``None`` would already have warned here, and
        nothing takes it back. Resolution order varies run to run under
        parallel and ``for_each`` groups, so the same workflow would warn
        intermittently.
        """
        r = _Recorder()
        r._note_pricing_hook_result("a", None)
        r._note_pricing_hook_result("b", None)
        r._note_pricing_hook_result("c", _priced())
        r._note_pricing_hook_result("d", None)

        with caplog.at_level(logging.WARNING, logger="conductor.engine.workflow"):
            r._warn_if_pricing_hook_silent()

        assert caplog.records == []


class TestVerdictIsAlsoAnEvent:
    """A log line alone reaches ``logging.lastResort`` and nothing else.

    Conductor installs no logging handlers, so the warning is an unattributed
    stderr line -- absent from the JSONL log and the dashboard, and under
    ``--web-bg`` written to a temp file nobody was told to read.
    """

    def test_emits_an_event_with_the_models(self) -> None:
        r = _Recorder()
        r._note_pricing_hook_result("claude-opus-5", None)
        r._note_pricing_hook_result("gpt-5.6-sol", None)
        r._warn_if_pricing_hook_silent()

        assert [e[0] for e in r.events] == ["pricing_hook_silent"]
        payload = r.events[0][1]
        assert payload["model_count"] == 2
        assert payload["models"] == ["claude-opus-5", "gpt-5.6-sol"]

    def test_no_event_when_pricing_worked(self) -> None:
        r = _Recorder()
        r._note_pricing_hook_result("a", _priced())
        r._warn_if_pricing_hook_silent()
        assert r.events == []


class TestBaseHookProvidersAreNotAccused:
    """``AgentProvider.get_model_pricing`` returns ``None`` by design.

    ``providers/base.py`` documents that as the correct behaviour for a provider
    whose SDK exposes no pricing, and only ``CopilotProvider`` overrides it. A
    tracker that counted those would tell four of the five providers their SDK
    broke for doing exactly what the base class prescribes.
    """

    def test_only_overriding_providers_are_tracked(self) -> None:
        from conductor.providers.base import AgentProvider

        class _Inherits(AgentProvider, abstract=True):  # type: ignore[call-arg,misc]
            pass

        class _Overrides(AgentProvider, abstract=True):  # type: ignore[call-arg,misc]
            async def get_model_pricing(self, model: str) -> ModelPricing | None:
                return None

        assert _Inherits.get_model_pricing is AgentProvider.get_model_pricing
        assert _Overrides.get_model_pricing is not AgentProvider.get_model_pricing


def _one_agent_workflow() -> WorkflowConfig:
    """Smallest config that resolves exactly one model and finishes."""
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="pricing-silence",
            entry_point="agent1",
            limits=LimitsConfig(max_iterations=5),
        ),
        agents=[
            AgentDef(
                name="agent1",
                model="gpt-4",
                prompt="hi",
                output={"answer": OutputField(type="string")},
                routes=[RouteDef(to="$end")],
            ),
        ],
        output={"answer": "{{ agent1.output.answer }}"},
    )


def _two_agent_workflow() -> WorkflowConfig:
    """Two agents in sequence, so the second can fail after the first priced."""
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="pricing-silence-2",
            entry_point="agent1",
            limits=LimitsConfig(max_iterations=5),
        ),
        agents=[
            AgentDef(
                name="agent1",
                model="gpt-4",
                prompt="hi",
                output={"answer": OutputField(type="string")},
                routes=[RouteDef(to="agent2")],
            ),
            AgentDef(
                name="agent2",
                model="gpt-4",
                prompt="hi",
                output={"answer": OutputField(type="string")},
                routes=[RouteDef(to="$end")],
            ),
        ],
        output={"answer": "{{ agent2.output.answer }}"},
    )


def _silent_pricing_provider(**kwargs: object) -> CopilotProvider:
    """A provider that overrides the hook and prices nothing.

    Overriding matters: the engine deliberately ignores providers using the
    base implementation, so a stand-in that inherits it would never be tracked.
    """
    return _SilentPricing(**kwargs)  # type: ignore[arg-type]


class _SilentPricing(CopilotProvider):
    """Overrides the hook and returns ``None`` for every model."""

    CAPABILITIES = CopilotProvider.CAPABILITIES

    async def get_model_pricing(self, model: str) -> ModelPricing | None:
        return None


class _WorkingPricing(CopilotProvider):
    """Overrides the hook and prices every model."""

    CAPABILITIES = CopilotProvider.CAPABILITIES

    async def get_model_pricing(self, model: str) -> ModelPricing | None:
        return _priced()


class _InheritsBaseHook(CopilotProvider):
    """Uses the base implementation, which returns ``None`` by design."""

    CAPABILITIES = CopilotProvider.CAPABILITIES
    get_model_pricing = AgentProviderBase.get_model_pricing  # type: ignore[assignment]


class TestTheVerdictIsReachedByRealRuns:
    """The helpers above are exercised in isolation by a stand-in.

    That leaves the wiring untested: whether a real run asks the hook, whether
    the provider gate lets a real provider through, and whether anything calls
    the verdict at all. Reverting any one of those left the isolated tests
    green, so they are covered here through ``WorkflowEngine.run``.
    """

    @pytest.mark.asyncio
    async def test_a_completed_run_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        provider = _silent_pricing_provider(mock_handler=lambda a, p, c: {"answer": "ok"})
        engine = WorkflowEngine(_one_agent_workflow(), provider)

        with caplog.at_level(logging.WARNING, logger="conductor.engine.workflow"):
            await engine.run({})

        assert len(caplog.records) == 1
        assert "no pricing for any" in caplog.records[0].getMessage()

    @pytest.mark.asyncio
    async def test_a_run_that_dies_half_way_still_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The case a summary-time verdict can never reach.

        ``cli/run.py`` calls ``get_execution_summary()`` after its
        ``except BaseException: raise`` block, so a run that dies part way
        never reaches it. That is also the run where "these numbers came from
        the static table" matters most, because someone is looking at a
        partial cost total trying to work out what it cost before it broke.

        The first agent has to succeed: a run that dies before anything asked
        the hook has nothing to conclude, and staying quiet there is correct.
        """

        def _explode_on_second(agent: object, prompt: object, ctx: object) -> dict[str, str]:
            if getattr(agent, "name", None) == "agent2":
                raise RuntimeError("boom")
            return {"answer": "ok"}

        provider = _silent_pricing_provider(mock_handler=_explode_on_second)
        engine = WorkflowEngine(_two_agent_workflow(), provider)

        with (
            caplog.at_level(logging.WARNING, logger="conductor.engine.workflow"),
            pytest.raises(BaseException),  # noqa: B017,PT011 - any failure will do
        ):
            await engine.run({})

        assert [r for r in caplog.records if "no pricing for any" in r.getMessage()]

    @pytest.mark.asyncio
    async def test_a_working_hook_stays_quiet(self, caplog: pytest.LogCaptureFixture) -> None:
        """Negative control: the run itself does not manufacture the warning."""
        engine = WorkflowEngine(
            _one_agent_workflow(),
            _WorkingPricing(mock_handler=lambda a, p, c: {"answer": "ok"}),
        )

        with caplog.at_level(logging.WARNING, logger="conductor.engine.workflow"):
            await engine.run({})

        assert [r for r in caplog.records if "no pricing for any" in r.getMessage()] == []

    @pytest.mark.asyncio
    async def test_a_base_hook_provider_is_not_accused(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The provider gate, through a real run rather than attribute identity.

        Asserting ``is`` on two throwaway subclasses only tests Python's
        attribute lookup, which holds whether or not the gate exists. This
        provider reaches the engine and returns ``None`` from the *base*
        implementation, which is the condition the gate must not report.
        """
        engine = WorkflowEngine(
            _one_agent_workflow(),
            _InheritsBaseHook(mock_handler=lambda a, p, c: {"answer": "ok"}),
        )

        with caplog.at_level(logging.WARNING, logger="conductor.engine.workflow"):
            await engine.run({})

        assert [r for r in caplog.records if "no pricing for any" in r.getMessage()] == []

    @pytest.mark.asyncio
    async def test_a_resumed_run_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """``resume`` draws the verdict too, and nothing else covered it.

        ``run`` and ``resume`` carry separate calls, so a test through ``run``
        leaves the ``resume`` one free to be dropped -- reverting it kept the
        suite green. A resumed run is still a run that priced nothing, and the
        repository treats the two entry points as a parity pair.
        """
        provider = _silent_pricing_provider(mock_handler=lambda a, p, c: {"answer": "ok"})
        engine = WorkflowEngine(_two_agent_workflow(), provider)

        # Resume as if agent1 had already run before the checkpoint.
        restored = WorkflowContext()
        restored.set_workflow_inputs({})
        restored.store("agent1", {"answer": "ok"})
        engine.set_context(restored)
        engine.set_limits(
            LimitEnforcer.from_dict(
                {"current_iteration": 1, "max_iterations": 5, "execution_history": ["agent1"]},
                timeout_seconds=300,
                budget_usd=None,
                budget_mode="audit",
            )
        )

        with caplog.at_level(logging.WARNING, logger="conductor.engine.workflow"):
            await engine.resume("agent2")

        assert [r for r in caplog.records if "no pricing for any" in r.getMessage()]


class TestTheVerdictReachesTheUser:
    """A warning nothing renders is a warning nobody reads."""

    @pytest.mark.asyncio
    async def test_the_summary_reports_degraded_pricing(self) -> None:
        provider = _silent_pricing_provider(mock_handler=lambda a, p, c: {"answer": "ok"})
        engine = WorkflowEngine(_one_agent_workflow(), provider)
        await engine.run({})

        summary = engine.get_execution_summary()
        assert summary["usage"]["live_pricing_degraded"] is True

    @pytest.mark.asyncio
    async def test_a_working_hook_leaves_the_flag_clear(self) -> None:
        engine = WorkflowEngine(
            _one_agent_workflow(),
            _WorkingPricing(mock_handler=lambda a, p, c: {"answer": "ok"}),
        )
        await engine.run({})

        assert engine.get_execution_summary()["usage"]["live_pricing_degraded"] is False

    def test_the_displayed_summary_carries_the_caveat(self) -> None:
        """Without this the total prints as confidently as a live-priced one."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.run import display_usage_summary

        buf = StringIO()
        display_usage_summary(
            {
                "total_input_tokens": 10,
                "total_output_tokens": 5,
                "total_tokens": 15,
                "total_cost_usd": 0.25,
                "live_pricing_degraded": True,
                "unpriced_agent_count": 0,
                "unpriced_models": [],
                "agents": [],
            },
            console=Console(file=buf, width=200, force_terminal=False),
        )

        assert "Live pricing unavailable" in buf.getvalue()

    def test_no_caveat_when_pricing_was_live(self) -> None:
        from io import StringIO

        from rich.console import Console

        from conductor.cli.run import display_usage_summary

        buf = StringIO()
        display_usage_summary(
            {
                "total_input_tokens": 10,
                "total_output_tokens": 5,
                "total_tokens": 15,
                "total_cost_usd": 0.25,
                "live_pricing_degraded": False,
                "unpriced_agent_count": 0,
                "unpriced_models": [],
                "agents": [],
            },
            console=Console(file=buf, width=200, force_terminal=False),
        )

        assert "Live pricing unavailable" not in buf.getvalue()
