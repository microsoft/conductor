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

from conductor.engine.pricing import ModelPricing


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
