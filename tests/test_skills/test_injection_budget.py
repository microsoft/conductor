"""Tests for the eager skill-injection budget (issue #350).

Providers without a native skill surface have no progressive disclosure:
``AgentExecutor`` prepends every enabled skill's ``SKILL.md`` *plus its
whole ``references/`` tree* to the prompt on every call and every retry. The
bundled ``conductor`` skill alone is ~117KB (~29K tokens), and before this
there was no ceiling of any kind. (A ``validator:`` block's
own grading call bypasses prompt rendering, so it does not re-pay this.)

Defaults are deliberately chosen so that the existing ~117KB case *warns*
rather than breaking — ``test_bundled_skill_warns_but_does_not_error``
pins that, since a stricter default would be a silent breaking change.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from conductor.config.schema import AgentDef, RuntimeConfig, SkillInjectionConfig
from conductor.exceptions import ExecutionError
from conductor.executor.agent import AgentExecutor
from conductor.providers.base import AgentOutput, AgentProvider, EventCallback
from conductor.skills import get_skill_directory, load_skill_content


class _EagerProvider(AgentProvider, abstract=True):
    """Stub with ``supports_native_skills = False`` (the injecting path)."""

    @property
    def supports_native_skills(self) -> bool:
        return False

    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None = None,
        interrupt_signal: asyncio.Event | None = None,
        event_callback: EventCallback | None = None,
        skill_directories: list[str] | None = None,
        custom_agents: list[dict[str, Any]] | None = None,
        extra_mcp_servers: dict[str, Any] | None = None,
    ) -> AgentOutput:
        return AgentOutput(content={"ok": True}, raw_response="")

    async def validate_connection(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class _NativeProvider(_EagerProvider, abstract=True):
    @property
    def supports_native_skills(self) -> bool:
        return True


def _make_skill(directory: Path, filler_bytes: int = 0) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {directory.name}\ndescription: A test skill.\n---\nBody\n"
    )
    if filler_bytes:
        references = directory / "references"
        references.mkdir(exist_ok=True)
        (references / "big.md").write_text("x" * filler_bytes)
    return directory


def _executor(provider: AgentProvider, **limits: int | None) -> AgentExecutor:
    return AgentExecutor(provider, skill_injection=SkillInjectionConfig(**limits))


class TestBudgetEnforcement:
    def test_over_max_bytes_raises(self, tmp_path: Path) -> None:
        skill = _make_skill(tmp_path / "big", filler_bytes=5000)
        agent = AgentDef(name="a", prompt="hi", skills=[str(skill)])
        executor = _executor(_EagerProvider(), warn_bytes=100, max_bytes=1000)
        with pytest.raises(ExecutionError) as exc_info:
            executor._build_prompt_prefix(agent)
        message = str(exc_info.value)
        assert "max_bytes" in message
        assert "big" in message, "the per-skill breakdown names the offender"
        assert exc_info.value.agent_name == "a"
        assert exc_info.value.suggestion is not None

    def test_under_warn_bytes_is_silent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        skill = _make_skill(tmp_path / "small")
        agent = AgentDef(name="a", prompt="hi", skills=[str(skill)])
        with caplog.at_level(logging.WARNING):
            prefix = _executor(_EagerProvider())._build_prompt_prefix(agent)
        assert prefix, "content is still injected"
        assert not caplog.records

    def test_between_thresholds_warns_without_raising(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        skill = _make_skill(tmp_path / "mid", filler_bytes=5000)
        agent = AgentDef(name="a", prompt="hi", skills=[str(skill)])
        executor = _executor(_EagerProvider(), warn_bytes=1000, max_bytes=100_000)
        with caplog.at_level(logging.WARNING):
            prefix = executor._build_prompt_prefix(agent)
        assert prefix
        assert any("skill content" in record.message for record in caplog.records)

    @pytest.mark.parametrize(
        ("limits", "label"),
        [
            ({"warn_bytes": None, "max_bytes": None}, "both disabled"),
            ({"warn_bytes": None, "max_bytes": 100_000}, "warning disabled"),
        ],
    )
    def test_null_limits_disable_checks(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        limits: dict[str, int | None],
        label: str,
    ) -> None:
        skill = _make_skill(tmp_path / "mid", filler_bytes=5000)
        agent = AgentDef(name="a", prompt="hi", skills=[str(skill)])
        with caplog.at_level(logging.WARNING):
            assert _executor(_EagerProvider(), **limits)._build_prompt_prefix(agent)
        assert not caplog.records, label

    def test_bundled_skill_warns_but_does_not_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """``skills: [conductor]`` on an eager provider already ships today.

        The defaults must surface its ~117KB payload without breaking it —
        a lower ``max_bytes`` default would be a silent breaking change.
        """
        agent = AgentDef(name="a", prompt="hi", skills=["conductor"])
        with caplog.at_level(logging.WARNING):
            prefix = _executor(_EagerProvider())._build_prompt_prefix(agent)
        assert prefix
        assert any("skill content" in record.message for record in caplog.records)

    def test_bundled_skill_size_sits_between_the_defaults(self) -> None:
        """Pins the assumption the defaults were chosen against, so a skill
        that grows past 128KB fails here rather than in a user's workflow.

        Measures the *rendered* string, which is what both enforcement paths
        compare against — summing raw file sizes would understate it by the
        ``<skills>``/``<skill>`` envelope and drift further as references grow.
        """
        directory = get_skill_directory("conductor")
        size = len(load_skill_content([("conductor", directory)]).encode("utf-8"))
        defaults = SkillInjectionConfig()
        assert defaults.warn_bytes is not None and defaults.max_bytes is not None
        assert defaults.warn_bytes < size < defaults.max_bytes


class TestBudgetScope:
    def test_native_providers_are_unaffected(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Progressive disclosure means nothing is prepended, so no limit applies
        even at a threshold the same skill would blow past when injected."""
        skill = _make_skill(tmp_path / "big", filler_bytes=5000)
        agent = AgentDef(name="a", prompt="hi", skills=[str(skill)])
        executor = _executor(_NativeProvider(), warn_bytes=10, max_bytes=100)
        with caplog.at_level(logging.WARNING):
            assert executor._build_prompt_prefix(agent) == ""
        assert not caplog.records

    def test_agent_opting_out_is_unaffected(self, caplog: pytest.LogCaptureFixture) -> None:
        agent = AgentDef(name="a", prompt="hi", skills=[])
        executor = _executor(_EagerProvider(), warn_bytes=10, max_bytes=100)
        with caplog.at_level(logging.WARNING):
            assert executor._build_prompt_prefix(agent) == ""
        assert not caplog.records

    def test_workflow_default_skills_are_budgeted(self, tmp_path: Path) -> None:
        """An inherited ``runtime.skills`` list costs the same as a per-agent one."""
        skill = _make_skill(tmp_path / "big", filler_bytes=5000)
        agent = AgentDef(name="a", prompt="hi")
        executor = AgentExecutor(
            _EagerProvider(),
            workflow_skills=[str(skill)],
            skill_injection=SkillInjectionConfig(warn_bytes=100, max_bytes=1000),
        )
        with pytest.raises(ExecutionError, match="max_bytes"):
            executor._build_prompt_prefix(agent)

    def test_combined_skills_are_measured_together(self, tmp_path: Path) -> None:
        """Two skills that each fit can still exceed the limit together — the
        accumulation case the budget exists for."""
        first = _make_skill(tmp_path / "one", filler_bytes=3000)
        second = _make_skill(tmp_path / "two", filler_bytes=3000)
        executor = _executor(_EagerProvider(), warn_bytes=100, max_bytes=5000)
        for skill in (first, second):
            alone = executor._build_prompt_prefix(
                AgentDef(name="a", prompt="hi", skills=[str(skill)])
            )
            assert len(alone.encode("utf-8")) <= 5000, "each skill must fit on its own"
        with pytest.raises(ExecutionError, match="max_bytes"):
            executor._build_prompt_prefix(
                AgentDef(name="a", prompt="hi", skills=[str(first), str(second)])
            )


class TestSkillInjectionConfigSchema:
    def test_defaults(self) -> None:
        config = SkillInjectionConfig()
        assert config.warn_bytes == 64 * 1024
        assert config.max_bytes == 160 * 1024

    def test_warn_above_max_is_rejected(self) -> None:
        """Such a config can never warn — the error fires first."""
        with pytest.raises(ValueError, match="must not exceed"):
            SkillInjectionConfig(warn_bytes=200_000, max_bytes=1_000)

    def test_equal_thresholds_are_allowed(self) -> None:
        """Equality makes the warning unreachable for the same reason
        ``warn > max`` does, but it is a coherent "hard limit only" request
        and ``warn_bytes: null`` is not the only way to spell it. Allowed
        deliberately rather than by oversight.
        """
        assert SkillInjectionConfig(warn_bytes=1000, max_bytes=1000).max_bytes == 1000

    def test_frozen_after_construction(self) -> None:
        """``validate_assignment`` on the enclosing ``RuntimeConfig`` does not
        re-fire this model's cross-field validator on attribute assignment, so
        without ``frozen=True`` both invariants are bypassable post-construction.

        Same reasoning ``ProviderSettings`` records for the same Pydantic gotcha.
        """
        config = SkillInjectionConfig(warn_bytes=100, max_bytes=1000)
        with pytest.raises(ValidationError):
            config.warn_bytes = 999_999
        assert RuntimeConfig().skill_injection is not None
        with pytest.raises(ValidationError):
            RuntimeConfig().skill_injection.max_bytes = -5

    def test_negative_values_rejected(self) -> None:
        with pytest.raises(ValueError):
            SkillInjectionConfig(max_bytes=-1)

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValueError):
            SkillInjectionConfig(max_byte=1)  # ty: ignore[unknown-argument]


class TestUnsupportedProviderRejection:
    """``capabilities.skills=False`` must hold at run time, not only at
    ``conductor validate`` time.

    ``conductor run`` never calls the static validator, so without this the
    declaration was enforced in one place and quietly contradicted in the
    other: the eager-injection path keys off ``supports_native_skills``, so a
    provider declaring ``skills=False`` still had the full skill body
    prepended to its prompt.
    """

    def test_provider_declaring_no_skill_support_is_refused(self) -> None:
        from conductor.providers.aca import AcaRuntimeProvider

        assert AcaRuntimeProvider.CAPABILITIES.skills is False

        class _Unsupported(_EagerProvider, abstract=True):
            CAPABILITIES = AcaRuntimeProvider.CAPABILITIES

        agent = AgentDef(name="a", prompt="hi", skills=["conductor"])
        with pytest.raises(ExecutionError) as exc_info:
            AgentExecutor(_Unsupported())._build_prompt_prefix(agent)
        assert "does not support skills" in str(exc_info.value)
        assert exc_info.value.agent_name == "a"

    def test_opting_out_on_such_a_provider_is_fine(self) -> None:
        from conductor.providers.aca import AcaRuntimeProvider

        class _Unsupported(_EagerProvider, abstract=True):
            CAPABILITIES = AcaRuntimeProvider.CAPABILITIES

        agent = AgentDef(name="a", prompt="hi", skills=[])
        assert AgentExecutor(_Unsupported())._build_prompt_prefix(agent) == ""

    def test_provider_without_capabilities_is_left_alone(self) -> None:
        """Test fakes declare ``abstract=True`` and have no CAPABILITIES;
        they must not be swept up by the check."""
        agent = AgentDef(name="a", prompt="hi", skills=["conductor"])
        assert AgentExecutor(_EagerProvider())._build_prompt_prefix(agent)


class TestWarningReachesTheUser:
    """`logger.warning` alone does not reach a user running `conductor run`:
    Conductor installs no logging handlers, so it surfaces through
    `logging.lastResort` as an unattributed stderr line, absent from the JSONL
    log and the dashboard. Since the defaults trip this for the bundled skill
    on every eager-provider call, it has to travel the event channel too —
    the same both-halves pattern as `checkpoint_save_failed`.
    """

    @staticmethod
    def _capture(tmp_path: Path, **limits: int | None) -> list[tuple[str, dict[str, object]]]:
        skill = _make_skill(tmp_path / "mid", filler_bytes=5000)
        events: list[tuple[str, dict[str, object]]] = []
        executor = _executor(_EagerProvider(), **limits)
        executor._build_prompt_prefix(
            AgentDef(name="a", prompt="hi", skills=[str(skill)]),
            lambda name, data: events.append((name, data)),
        )
        return events

    def test_breach_emits_an_event(self, tmp_path: Path) -> None:
        events = self._capture(tmp_path, warn_bytes=1000, max_bytes=100_000)
        assert [name for name, _ in events] == ["skill_injection_warning"]
        payload = events[0][1]
        assert payload["agent_name"] == "a"
        assert isinstance(payload["bytes"], int) and payload["bytes"] > 1000
        assert payload["warn_bytes"] == 1000
        assert "mid" in str(payload["breakdown"])

    def test_no_event_below_the_threshold(self, tmp_path: Path) -> None:
        assert self._capture(tmp_path, warn_bytes=100_000, max_bytes=200_000) == []

    def test_no_event_when_warning_disabled(self, tmp_path: Path) -> None:
        assert self._capture(tmp_path, warn_bytes=None, max_bytes=200_000) == []

    def test_omitting_the_callback_still_works(self, tmp_path: Path) -> None:
        """The callback is optional — `render_prompt` is called without one."""
        skill = _make_skill(tmp_path / "mid", filler_bytes=5000)
        executor = _executor(_EagerProvider(), warn_bytes=1000, max_bytes=100_000)
        assert executor._build_prompt_prefix(AgentDef(name="a", prompt="hi", skills=[str(skill)]))
