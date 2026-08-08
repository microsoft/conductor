"""Tests for surfacing a systemically silent provider pricing hook (issue #386).

#265 added ``AgentProvider.get_model_pricing`` so live pricing could replace the
static table, and warns once if that hook *raises*. It does not notice the
companion case: a hook that never raises and returns ``None`` for everything.

That is not hypothetical. ``github-copilot-sdk`` 1.0.1 removed
``ModelBilling.token_prices``, which is the field ``CopilotProvider``'s hook
reads, so it resolves ``None`` for all 21 models it can list. Nothing raises,
nothing logs, and models that happen to be in the static table still report a
plausible cost — so the fact that live pricing is dead is invisible, and the
only symptom is that newer models show up as unpriced.

The distinction these tests pin:

- ``None`` for one model, or any model priced at all → ordinary, stay quiet.
- ``None`` for several models with nothing ever priced → say so, once.
"""

from __future__ import annotations

import logging

import pytest

from conductor.engine.pricing import ModelPricing


class _Recorder:
    """Minimal stand-in exposing just the latch state the helper touches."""

    def __init__(self) -> None:
        self._pricing_hook_none_models: set[str] = set()
        self._pricing_hook_priced_any = False
        self._pricing_hook_silent_warned = False

    # Bind the real implementation so these tests exercise production code.
    from conductor.engine.workflow import WorkflowEngine

    _note_pricing_hook_result = WorkflowEngine._note_pricing_hook_result


def _priced() -> ModelPricing:
    return ModelPricing(input_per_mtok=1.0, output_per_mtok=2.0)


class TestSilentPricingHookIsSurfaced:
    def test_warns_once_when_nothing_is_ever_priced(self, caplog: pytest.LogCaptureFixture) -> None:
        r = _Recorder()
        with caplog.at_level(logging.WARNING, logger="conductor.engine.workflow"):
            r._note_pricing_hook_result("claude-opus-5", None)
            r._note_pricing_hook_result("gpt-5.6-sol", None)
            r._note_pricing_hook_result("claude-sonnet-5", None)

        warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
        assert len(warnings) == 1, "must warn exactly once, not once per model"
        assert "no pricing for any" in warnings[0].getMessage()

    def test_single_unpriced_model_stays_quiet(self, caplog: pytest.LogCaptureFixture) -> None:
        """One model the provider cannot price is ordinary, not a defect."""
        r = _Recorder()
        with caplog.at_level(logging.WARNING, logger="conductor.engine.workflow"):
            r._note_pricing_hook_result("some-exotic-model", None)

        assert [rec for rec in caplog.records if rec.levelno == logging.WARNING] == []

    def test_stays_quiet_when_any_model_was_priced(self, caplog: pytest.LogCaptureFixture) -> None:
        """A working hook that declines some models is not a systemic failure."""
        r = _Recorder()
        with caplog.at_level(logging.WARNING, logger="conductor.engine.workflow"):
            r._note_pricing_hook_result("claude-sonnet-5", _priced())
            r._note_pricing_hook_result("claude-opus-5", None)
            r._note_pricing_hook_result("gpt-5.6-sol", None)

        assert [rec for rec in caplog.records if rec.levelno == logging.WARNING] == []

    def test_warning_names_the_models(self, caplog: pytest.LogCaptureFixture) -> None:
        """The message must be actionable — which models were unpriced."""
        r = _Recorder()
        with caplog.at_level(logging.WARNING, logger="conductor.engine.workflow"):
            r._note_pricing_hook_result("claude-opus-5", None)
            r._note_pricing_hook_result("gpt-5.6-sol", None)

        message = caplog.records[0].getMessage()
        assert "claude-opus-5" in message
        assert "gpt-5.6-sol" in message

    def test_repeated_models_do_not_re_arm_the_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The set is de-duplicated, so one model probed twice is still one."""
        r = _Recorder()
        with caplog.at_level(logging.WARNING, logger="conductor.engine.workflow"):
            r._note_pricing_hook_result("claude-opus-5", None)
            r._note_pricing_hook_result("claude-opus-5", None)

        assert [rec for rec in caplog.records if rec.levelno == logging.WARNING] == []
