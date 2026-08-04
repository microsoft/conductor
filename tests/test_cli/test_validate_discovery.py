"""``conductor validate`` reports what discovery found (issue #362).

Discovery's one real cost is that the same YAML picks up a different set
on a different machine or in CI. That is only defensible if the author can
see the set, so this listing is part of the feature rather than a
debugging aid — these tests exist to stop it being quietly dropped.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from conductor.cli.validate import validate_workflow

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
    output:
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


def _validate(tmp_path: Path, *, discovery: bool) -> str:
    path = tmp_path / "wf.yaml"
    path.write_text(_WORKFLOW.format(discovery=_DISCOVERY_BLOCK if discovery else ""))
    console = Console(record=True, width=200)
    ok, _ = validate_workflow(path, console=console)
    assert ok is True
    return console.export_text()


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
        # eager-injection provider.
        _make_skill(fake_home / ".copilot" / "skills" / "alpha")
        assert "Total if eagerly injected" in _validate(tmp_path, discovery=True)

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
