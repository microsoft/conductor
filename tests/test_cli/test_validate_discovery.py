"""``conductor validate`` reports what discovery found (issue #362).

Discovery's one real cost is that the same YAML picks up a different set
on a different machine or in CI. That is only defensible if the author can
see the set, so this listing is part of the feature rather than a
debugging aid — these tests exist to stop it being quietly dropped.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from rich.console import Console

from conductor.cli.validate import validate_workflow
from conductor.skills import load_skill_content

_FRONTMATTER = "---\nname: {name}\ndescription: A test skill.\n---\nBody\n"

_WORKFLOW = """
workflow:
  name: discovery-report
  entry_point: worker
  runtime:
    provider: copilot
{discovery}
agents:
  - name: worker
    model: gpt-5
    prompt: "Do the thing."
{agent_skills}    output:
      result:
        type: string

output:
  result: "{{{{ worker.output.result }}}}"
"""

_DISCOVERY_BLOCK = """    skill_discovery:
      sources: [personal]
"""


def _make_skill(directory: Path, name: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(_FRONTMATTER.format(name=name or directory.name))
    return directory


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Path.home()`` at an empty directory.

    Reading the real one would make the assertions depend on what the
    machine running the suite has installed.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def _validate(tmp_path: Path, *, discovery: bool, agent_opts_out: bool = False) -> str:
    path = tmp_path / "wf.yaml"
    path.write_text(
        _WORKFLOW.format(
            discovery=_DISCOVERY_BLOCK if discovery else "",
            agent_skills="    skills: []\n" if agent_opts_out else "",
        )
    )
    console = Console(record=True, width=200)
    ok, _ = validate_workflow(path, console=console)
    assert ok is True
    return console.export_text()


def _bytes_reported(output: str) -> int:
    """Pull the injected-size figure out of the rendered report."""
    match = re.search(r"([\d,]+) bytes", output.replace("\n", ""))
    assert match is not None, output
    return int(match.group(1).replace(",", ""))


class TestSkillDiscoveryReport:
    def test_lists_each_discovered_skill_and_its_location(
        self, tmp_path: Path, fake_home: Path
    ) -> None:
        _make_skill(fake_home / ".copilot" / "skills" / "alpha")
        _make_skill(fake_home / ".claude" / "skills" / "beta")
        output = _validate(tmp_path, discovery=True)
        assert "Skill discovery (personal): 2 skill(s)" in output
        assert "alpha" in output
        assert "beta" in output
        # The originating location, so an unexpected skill can be traced.
        assert str(fake_home / ".copilot" / "skills") in output.replace("\n", "")

    def test_reports_the_injected_size(self, tmp_path: Path, fake_home: Path) -> None:
        # The number that decides whether this set is affordable on an
        # eager-injection provider, so assert the number and not the label.
        skill = _make_skill(fake_home / ".copilot" / "skills" / "alpha")
        expected = len(load_skill_content([("alpha", skill)]).encode("utf-8"))
        output = _validate(tmp_path, discovery=True)
        assert f"{expected:,} bytes" in output.replace("\n", "")

    def test_size_grows_with_the_discovered_set(self, tmp_path: Path, fake_home: Path) -> None:
        _make_skill(fake_home / ".copilot" / "skills" / "alpha")
        one = _bytes_reported(_validate(tmp_path, discovery=True))
        _make_skill(fake_home / ".copilot" / "skills" / "beta")
        assert _bytes_reported(_validate(tmp_path, discovery=True)) > one

    def test_broken_skill_is_not_listed_as_present(self, tmp_path: Path, fake_home: Path) -> None:
        """The listing is the set in effect, not the raw scan.

        A skill the run would drop must not appear here, or its bytes be
        billed — the whole point of the summary is that the author can
        trust it describes what agents actually get.
        """
        _make_skill(fake_home / ".copilot" / "skills" / "good")
        broken = fake_home / ".copilot" / "skills" / "broken"
        broken.mkdir(parents=True)
        (broken / "SKILL.md").write_text("---\nname: broken\ndescription: A. Triggers: x\n---\n")
        output = _validate(tmp_path, discovery=True)
        assert "1 skill(s)" in output
        assert "• broken" not in output.replace("\n", "")

    def test_says_so_when_nothing_is_found(self, tmp_path: Path, fake_home: Path) -> None:
        assert "no skills found" in _validate(tmp_path, discovery=True)

    def test_silent_when_discovery_is_off(self, tmp_path: Path, fake_home: Path) -> None:
        _make_skill(fake_home / ".copilot" / "skills" / "alpha")
        assert "Skill discovery" not in _validate(tmp_path, discovery=False)

    def test_a_broken_skill_does_not_fail_the_command(
        self, tmp_path: Path, fake_home: Path
    ) -> None:
        """Reporting must not become a second source of validation errors."""
        _make_skill(fake_home / ".copilot" / "skills" / "good")
        broken = fake_home / ".copilot" / "skills" / "broken"
        broken.mkdir(parents=True)
        (broken / "SKILL.md").write_text("---\nname: broken\ndescription: A. Triggers: x\n---\n")
        output = _validate(tmp_path, discovery=True)
        assert "good" in output

    def test_reports_diagnostics_when_every_agent_overrides(
        self, tmp_path: Path, fake_home: Path
    ) -> None:
        """The validator only resolves skills for agents that *inherit*.

        With every agent declaring its own ``skills:``, discovery never
        runs inside the validator — so this summary is the only place a
        broken ambient location is ever mentioned.
        """
        broken = fake_home / ".copilot" / "skills" / "broken"
        broken.mkdir(parents=True)
        (broken / "SKILL.md").write_text("---\nname: broken\ndescription: A. Triggers: x\n---\n")
        output = _validate(tmp_path, discovery=True, agent_opts_out=True)
        assert "broken" in output

    def test_diagnostics_are_not_printed_twice(self, tmp_path: Path, fake_home: Path) -> None:
        output = _validate(tmp_path, discovery=True)
        assert output.count("found no skills") <= 1
