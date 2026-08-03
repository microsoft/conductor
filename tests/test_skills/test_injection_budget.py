"""Tests for the eager skill-injection budget (issue #350).

Providers without a native skill surface have no progressive disclosure:
``AgentExecutor`` prepends every enabled skill's ``SKILL.md`` *plus its
whole ``references/`` tree* to the prompt on every call, every retry, and
every ``validator:`` call. The bundled ``conductor`` skill alone is ~117KB
(~29K tokens), and before this there was no ceiling of any kind.

Defaults are deliberately chosen so that existing ~117KB case *warns*
rather than breaking — ``test_bundled_skill_warns_but_does_not_error``
pins that, since a stricter default would be a silent breaking change.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from conductor.config.schema import AgentDef, SkillInjectionConfig
from conductor.exceptions import ExecutionError
from conductor.executor.agent import AgentExecutor
from conductor.providers.base import AgentOutput, AgentProvider, EventCallback
from conductor.skills import get_skill_directory


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
        that grows past 128KB fails here rather than in a user's workflow."""
        directory = get_skill_directory("conductor")
        size = (directory / "SKILL.md").stat().st_size + sum(
            path.stat().st_size for path in (directory / "references").glob("*.md")
        )
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
            executor._build_prompt_prefix(AgentDef(name="a", prompt="hi", skills=[str(skill)]))
        with pytest.raises(ExecutionError, match="max_bytes"):
            executor._build_prompt_prefix(
                AgentDef(name="a", prompt="hi", skills=[str(first), str(second)])
            )


class TestSkillInjectionConfigSchema:
    def test_defaults(self) -> None:
        config = SkillInjectionConfig()
        assert config.warn_bytes == 64 * 1024
        assert config.max_bytes == 128 * 1024

    def test_warn_above_max_is_rejected(self) -> None:
        """Such a config can never warn — the error fires first."""
        with pytest.raises(ValueError, match="must not exceed"):
            SkillInjectionConfig(warn_bytes=200_000, max_bytes=1_000)

    def test_equal_thresholds_are_allowed(self) -> None:
        assert SkillInjectionConfig(warn_bytes=1000, max_bytes=1000).max_bytes == 1000

    def test_negative_values_rejected(self) -> None:
        with pytest.raises(ValueError):
            SkillInjectionConfig(max_bytes=-1)

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValueError):
            SkillInjectionConfig(max_byte=1)  # ty: ignore[unknown-argument]
