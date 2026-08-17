"""Tests for the workflow progress renderer (``tui/dag.py``).

The renderer replaced a single line of ``a → b → c  +11 more``. Its whole
claim is that the *live* step is findable at a glance, so these tests assert
the properties that claim rests on: the current step is styled apart from
every other, completed work recedes, the flow stays inside the width it was
given, and a bounded render says how much it left out.
"""

from __future__ import annotations

from conductor.fleet.summary import RunTopology, TopologyAgent
from conductor.fleet.tui.dag import render_score, step_statuses


def _topology(*names: str) -> RunTopology:
    return RunTopology(
        entry_point=names[0] if names else None,
        agents=[
            TopologyAgent(name=n, type="agent", model="gpt-5", provider_name="copilot")
            for n in names
        ],
    )


class TestStepStatuses:
    def test_marks_everything_before_the_cursor_complete(self) -> None:
        statuses = step_statuses(["a", "b", "c"], "b")
        assert statuses == {"a": "completed", "b": "running"}

    def test_pending_steps_are_simply_absent(self) -> None:
        """Absence is the caller's ``pending`` default -- inventing a status
        for a step that has not run would be a claim the log cannot support."""
        assert "c" not in step_statuses(["a", "b", "c"], "b")

    def test_unknown_current_step_invents_no_history(self) -> None:
        assert step_statuses(["a", "b"], "somewhere-else") == {}

    def test_no_current_step_invents_no_history(self) -> None:
        assert step_statuses(["a", "b"], None) == {}

    def test_entry_point_has_nothing_before_it(self) -> None:
        assert step_statuses(["a", "b"], "a") == {"a": "running"}


class TestRenderScore:
    def test_empty_topology_renders_nothing(self) -> None:
        assert render_score(_topology(), {}, width=80).plain == ""

    def test_zero_width_renders_nothing(self) -> None:
        assert render_score(_topology("a"), {}, width=0).plain == ""

    def test_every_step_name_appears(self) -> None:
        rendered = render_score(_topology("alpha", "beta", "gamma"), {}, width=120)
        for name in ("alpha", "beta", "gamma"):
            assert name in rendered.plain

    def test_lines_stay_within_the_given_width(self) -> None:
        """A line that overflows is re-wrapped by the terminal, which reads as
        a rendering fault rather than as a flowed layout."""
        topology = _topology(*[f"step_number_{i}" for i in range(12)])
        rendered = render_score(topology, {}, width=48)
        for line in rendered.plain.splitlines():
            assert len(line) <= 48

    def test_more_marker_also_stays_within_the_width(self) -> None:
        topology = _topology(*[f"step_number_{i}" for i in range(40)])
        rendered = render_score(topology, {}, width=48, max_lines=2)
        for line in rendered.plain.splitlines():
            assert len(line) <= 48

    def test_bounded_render_says_what_it_left_out(self) -> None:
        topology = _topology(*[f"step_{i}" for i in range(40)])
        rendered = render_score(topology, {}, width=40, max_lines=2)
        assert "more" in rendered.plain

    def test_unbounded_render_has_no_marker(self) -> None:
        topology = _topology(*[f"step_{i}" for i in range(12)])
        rendered = render_score(topology, {}, width=40)
        assert "more" not in rendered.plain

    def test_running_step_is_styled_apart_from_the_rest(self) -> None:
        topology = _topology("a", "b", "c")
        rendered = render_score(
            topology, {"a": "completed", "b": "running"}, width=80, animate=False
        )
        styles = {span.style for span in rendered.spans}
        # The live step carries a style no other chip does; without that the
        # only way to find it is to read every name.
        assert any("bright_cyan" in str(s) for s in styles)

    def test_completed_steps_recede(self) -> None:
        rendered = render_score(_topology("a", "b"), {"a": "completed"}, width=80)
        assert any("dim" in str(span.style) for span in rendered.spans)

    def test_animation_moves_only_the_live_step(self) -> None:
        topology = _topology("a", "b")
        frames = {
            render_score(topology, {"b": "running"}, width=80, frame=f).plain for f in range(10)
        }
        assert len(frames) > 1

    def test_static_render_is_stable_across_frames(self) -> None:
        """With animation off the same input must always render identically,
        or a screen with animation disabled would still flicker."""
        topology = _topology("a", "b")
        frames = {
            render_score(topology, {"b": "running"}, width=80, frame=f, animate=False).plain
            for f in range(10)
        }
        assert len(frames) == 1

    def test_a_step_missing_from_the_status_map_still_renders(self) -> None:
        rendered = render_score(_topology("a"), {}, width=80)
        assert "a" in rendered.plain

    def test_an_unrecognised_status_does_not_raise(self) -> None:
        """A future status reaching an un-updated renderer must not take down
        the poll loop that drew it."""
        rendered = render_score(_topology("a"), {"a": "teleported"}, width=80)
        assert "a" in rendered.plain
