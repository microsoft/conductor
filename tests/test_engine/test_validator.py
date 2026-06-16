"""Tests for the semantic output validator (issue #220)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from conductor.config.schema import AgentDef, ValidatorConfig
from conductor.engine.validator import OutputValidator, _truncate
from conductor.providers.base import AgentOutput


def _agent(criteria: str = "Must cite a real source.", model: str = "gpt-4", **kw) -> AgentDef:
    return AgentDef(
        name="reviewer",
        model=model,
        prompt="Review {{ x }}",
        validator=ValidatorConfig(criteria=criteria, **kw),
    )


def _agent_output(content: dict, **kw) -> AgentOutput:
    return AgentOutput(content=content, raw_response="", model="gpt-4", **kw)


class TestValidatorParsing:
    """Tests for the _parse helper."""

    def setup_method(self) -> None:
        self.validator = OutputValidator()

    def test_parse_passed_true(self) -> None:
        passed, issues, ok = self.validator._parse({"passed": True, "issues": []})
        assert passed is True
        assert issues == []
        assert ok is True

    def test_parse_passed_false_with_issues(self) -> None:
        passed, issues, ok = self.validator._parse(
            {"passed": False, "issues": ["missing null check", "fabricated fn name"]}
        )
        assert passed is False
        assert issues == ["missing null check", "fabricated fn name"]
        assert ok is True

    def test_parse_missing_passed_fails_open(self) -> None:
        passed, issues, ok = self.validator._parse({"issues": ["x"]})
        assert passed is True
        assert ok is False

    def test_parse_non_dict_fails_open(self) -> None:
        passed, issues, ok = self.validator._parse("not a dict")
        assert passed is True
        assert issues == []
        assert ok is False

    def test_parse_issues_as_string_wrapped(self) -> None:
        passed, issues, ok = self.validator._parse({"passed": False, "issues": "single issue"})
        assert passed is False
        assert issues == ["single issue"]

    def test_parse_issues_non_list_stringified(self) -> None:
        passed, issues, ok = self.validator._parse({"passed": False, "issues": 42})
        assert passed is False
        assert issues == ["42"]

    def test_parse_passed_false_missing_issues(self) -> None:
        passed, issues, ok = self.validator._parse({"passed": False})
        assert passed is False
        assert issues == []
        assert ok is True

    def test_parse_string_false_is_false(self) -> None:
        # bool("false") is True, so the parser must interpret the string.
        passed, issues, ok = self.validator._parse({"passed": "false", "issues": ["x"]})
        assert passed is False
        assert issues == ["x"]
        assert ok is True

    def test_parse_string_true_case_insensitive(self) -> None:
        passed, issues, ok = self.validator._parse({"passed": "TRUE"})
        assert passed is True
        assert ok is True

    def test_parse_non_bool_passed_fails_open(self) -> None:
        # An unrecognized type (e.g. int) routes to the errored/fail-open path.
        passed, issues, ok = self.validator._parse({"passed": 1})
        assert passed is True
        assert ok is False


class TestValidationOutcomeInvariants:
    """ValidationOutcome.__post_init__ keeps illegal states unrepresentable."""

    def test_passed_drops_issues(self) -> None:
        from conductor.engine.validator import ValidationOutcome

        outcome = ValidationOutcome(passed=True, issues=["leftover"])
        assert outcome.issues == []

    def test_errored_implies_passed_and_no_issues(self) -> None:
        from conductor.engine.validator import ValidationOutcome

        outcome = ValidationOutcome(passed=False, issues=["x"], errored=True)
        assert outcome.passed is True
        assert outcome.issues == []

    def test_failed_keeps_issues(self) -> None:
        from conductor.engine.validator import ValidationOutcome

        outcome = ValidationOutcome(passed=False, issues=["real problem"])
        assert outcome.passed is False
        assert outcome.issues == ["real problem"]


class TestValidatorAgentConstruction:
    """Tests for the synthetic validator AgentDef."""

    def test_inherits_primary_model(self) -> None:
        agent = _agent(model="claude-sonnet-4-5")
        v = OutputValidator()._build_validator_agent(agent)
        assert v.model == "claude-sonnet-4-5"

    def test_validator_model_override_wins(self) -> None:
        agent = AgentDef(
            name="reviewer",
            model="expensive",
            prompt="x",
            validator=ValidatorConfig(criteria="check", model="cheap"),
        )
        v = OutputValidator()._build_validator_agent(agent)
        assert v.model == "cheap"

    def test_system_prompt_contains_criteria(self) -> None:
        agent = _agent(criteria="VERY_SPECIFIC_RUBRIC")
        v = OutputValidator()._build_validator_agent(agent)
        assert "VERY_SPECIFIC_RUBRIC" in (v.system_prompt or "")

    def test_no_tools_and_has_output_schema(self) -> None:
        v = OutputValidator()._build_validator_agent(_agent())
        assert v.tools == []
        assert v.output is not None
        assert set(v.output.keys()) == {"passed", "issues"}

    def test_name_marks_validator(self) -> None:
        v = OutputValidator()._build_validator_agent(_agent())
        assert v.name == "reviewer (validator)"


class TestValidatorTruncation:
    def test_below_limit_unchanged(self) -> None:
        assert _truncate("abc", 10) == "abc"

    def test_above_limit_appends_marker(self) -> None:
        result = _truncate("x" * 6000, 4000)
        assert result.endswith("…[truncated]")
        assert len(result) <= 4000


class TestValidatorValidate:
    """Tests for the full validate() method with a mocked provider."""

    @pytest.mark.asyncio
    async def test_pass_returns_output_for_usage(self) -> None:
        agent = _agent()
        provider = MagicMock()
        provider.execute = AsyncMock(
            return_value=_agent_output(
                {"passed": True, "issues": []}, input_tokens=10, output_tokens=5
            )
        )

        outcome = await OutputValidator().validate(
            agent, "primary prompt", {"summary": "x"}, provider
        )

        assert outcome.passed is True
        assert outcome.issues == []
        assert outcome.errored is False
        assert outcome.output is not None
        assert outcome.output.input_tokens == 10

    @pytest.mark.asyncio
    async def test_fail_returns_issues(self) -> None:
        agent = _agent()
        provider = MagicMock()
        provider.execute = AsyncMock(
            return_value=_agent_output({"passed": False, "issues": ["missing edge case"]})
        )

        outcome = await OutputValidator().validate(agent, "p", {"summary": "x"}, provider)

        assert outcome.passed is False
        assert outcome.issues == ["missing edge case"]
        assert outcome.errored is False

    @pytest.mark.asyncio
    async def test_provider_error_fails_open(self) -> None:
        agent = _agent()
        provider = MagicMock()
        provider.execute = AsyncMock(side_effect=RuntimeError("boom"))

        outcome = await OutputValidator().validate(agent, "p", {"summary": "x"}, provider)

        assert outcome.passed is True
        assert outcome.errored is True
        assert outcome.output is None

    @pytest.mark.asyncio
    async def test_malformed_output_fails_open(self) -> None:
        agent = _agent()
        provider = MagicMock()
        provider.execute = AsyncMock(return_value=_agent_output({"unexpected": "shape"}))

        outcome = await OutputValidator().validate(agent, "p", {"summary": "x"}, provider)

        assert outcome.passed is True
        assert outcome.errored is True
        # The output is still returned so its (small) cost can be attributed.
        assert outcome.output is not None

    @pytest.mark.asyncio
    async def test_truncates_large_output(self) -> None:
        agent = _agent()
        provider = MagicMock()
        provider.execute = AsyncMock(return_value=_agent_output({"passed": True, "issues": []}))

        big_output = {"text": "x" * 20000}
        await OutputValidator().validate(agent, "p", big_output, provider)

        rendered = provider.execute.call_args.kwargs["rendered_prompt"]
        assert "…[truncated]" in rendered

    @pytest.mark.asyncio
    async def test_validator_runs_without_tools(self) -> None:
        agent = _agent()
        provider = MagicMock()
        provider.execute = AsyncMock(return_value=_agent_output({"passed": True, "issues": []}))

        await OutputValidator().validate(agent, "p", {"x": 1}, provider)

        assert provider.execute.call_args.kwargs["tools"] == []

    @pytest.mark.asyncio
    async def test_validator_forwards_interrupt_signal(self) -> None:
        import asyncio

        agent = _agent()
        provider = MagicMock()
        provider.execute = AsyncMock(return_value=_agent_output({"passed": True, "issues": []}))
        signal = asyncio.Event()

        await OutputValidator().validate(agent, "p", {"x": 1}, provider, interrupt_signal=signal)

        assert provider.execute.call_args.kwargs["interrupt_signal"] is signal

    @pytest.mark.asyncio
    async def test_passed_with_issues_is_normalized(self) -> None:
        # A grader that returns passed=true but also lists issues must not
        # surface a contradictory outcome.
        agent = _agent()
        provider = MagicMock()
        provider.execute = AsyncMock(
            return_value=_agent_output({"passed": True, "issues": ["ignored"]})
        )

        outcome = await OutputValidator().validate(agent, "p", {"x": 1}, provider)

        assert outcome.passed is True
        assert outcome.issues == []

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self) -> None:
        import asyncio

        agent = _agent()
        provider = MagicMock()
        provider.execute = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await OutputValidator().validate(agent, "p", {"x": 1}, provider)
