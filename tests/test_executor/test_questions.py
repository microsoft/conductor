"""Tests for the pure helpers backing ``type: questions`` steps."""

from __future__ import annotations

import pytest

from conductor.config.schema import AgentDef, QuestionDef
from conductor.exceptions import ExecutionError
from conductor.executor.questions import (
    FREE_TEXT,
    NAV_ABORT,
    NAV_BACK,
    NAV_SKIP,
    NAV_SKIP_ALL,
    AnswerRecord,
    NavFlags,
    build_output,
    build_prompt,
    coerce_questions,
    resolve_questions,
)


def _node(**kwargs) -> AgentDef:
    """Build a minimal questions node."""
    kwargs.setdefault("questions", [QuestionDef(text="Why?")])
    return AgentDef(name="ask", type="questions", **kwargs)


class TestCoerceQuestions:
    """Normalizing a resolved ``source`` array."""

    def test_plain_strings_become_questions(self) -> None:
        """The existing ``array of string`` shape migrates without changes."""
        result = coerce_questions(["First?", "Second?"], agent_name="ask")

        assert [q.text for q in result] == ["First?", "Second?"]
        assert all(q.choices is None for q in result)

    def test_objects_parse_with_choices(self) -> None:
        """Objects unlock choices as a backward-compatible upgrade."""
        result = coerce_questions(
            [{"text": "Server or client?", "choices": ["Server", "Client"]}],
            agent_name="ask",
        )

        assert result[0].choices == ["Server", "Client"]

    def test_question_key_aliases_text(self) -> None:
        """``question:`` is accepted — agents reach for the domain word."""
        result = coerce_questions([{"question": "Why?", "choices": ["a"]}], agent_name="ask")

        assert result[0].text == "Why?"
        assert result[0].choices == ["a"]

    def test_non_string_non_object_is_rejected(self) -> None:
        """A malformed entry names its position rather than failing silently."""
        with pytest.raises(ExecutionError, match="Question 2 in 'ask' has type int"):
            coerce_questions(["ok", 42], agent_name="ask")

    def test_invalid_object_is_rejected(self) -> None:
        """An object with no question text is an error, not a skipped entry."""
        with pytest.raises(ExecutionError, match="not a valid question"):
            coerce_questions([{"choices": ["a"]}], agent_name="ask")


class TestResolveQuestions:
    """Rendering and answer-key assignment."""

    def test_default_ids_are_positional(self) -> None:
        """Ids default to q1..qN."""
        result = resolve_questions(
            [QuestionDef(text="a"), QuestionDef(text="b")],
            render=lambda s: s,
            agent_name="ask",
        )

        assert [q.id for q in result] == ["q1", "q2"]

    def test_explicit_id_is_kept(self) -> None:
        """An explicit id survives so downstream templates stay stable."""
        result = resolve_questions(
            [QuestionDef(id="auth", text="a")], render=lambda s: s, agent_name="ask"
        )

        assert result[0].id == "auth"

    def test_text_hint_and_choices_are_rendered(self) -> None:
        """Every human-visible string goes through the renderer."""
        result = resolve_questions(
            [QuestionDef(text="{{ x }}", hint="{{ y }}", choices=["{{ z }}"])],
            render=lambda s: s.replace("{{ x }}", "X")
            .replace("{{ y }}", "Y")
            .replace("{{ z }}", "Z"),
            agent_name="ask",
        )

        assert (result[0].text, result[0].hint, result[0].choices) == ("X", "Y", ["Z"])

    def test_duplicate_ids_are_rejected(self) -> None:
        """Ids become answer keys, so a clash would silently drop an answer."""
        with pytest.raises(ExecutionError, match="Duplicate question id 'dup'"):
            resolve_questions(
                [QuestionDef(id="dup", text="a"), QuestionDef(id="dup", text="b")],
                render=lambda s: s,
                agent_name="ask",
            )


class TestNavFlags:
    """Tri-state navigation flags."""

    def test_defaults_when_unset(self) -> None:
        """Unset flags take the documented defaults."""
        flags = NavFlags.resolve(_node())

        assert (flags.back, flags.skip, flags.skip_all, flags.abort) == (True, True, True, False)

    def test_explicit_false_is_honored(self) -> None:
        """An explicit False is distinguishable from an unset flag."""
        flags = NavFlags.resolve(_node(allow_back=False, allow_abort=True))

        assert flags.back is False
        assert flags.abort is True


class TestBuildPrompt:
    """Choice construction for one question."""

    def _question(self, **kwargs):
        defaults = {"text": "Why?"}
        defaults.update(kwargs)
        return resolve_questions([QuestionDef(**defaults)], render=lambda s: s, agent_name="ask")[0]

    def _build(self, question, *, nav=None, cursor=0, total=3, intro=None):
        return build_prompt(
            _node(),
            question,
            nav=nav or NavFlags(),
            cursor=cursor,
            total=total,
            answered=0,
            skipped=0,
            prompt_id="ask:run:1",
            intro=intro,
        )

    def test_choices_precede_free_text_and_nav(self) -> None:
        """Suggested answers come first, then write-your-own, then controls."""
        prompt = self._build(self._question(choices=["Server", "Client"]), cursor=1)

        values = [c.value for c in prompt.choices]
        assert values[:3] == ["Server", "Client", FREE_TEXT]
        assert set(values[3:]) == {NAV_BACK, NAV_SKIP, NAV_SKIP_ALL}

    def test_back_is_hidden_on_the_first_question(self) -> None:
        """There is nowhere to go back to from question one."""
        prompt = self._build(self._question(), cursor=0)

        assert NAV_BACK not in [c.value for c in prompt.choices]

    def test_free_text_can_be_disabled(self) -> None:
        """``allow_free_text: false`` restricts the answer to the choices."""
        prompt = self._build(self._question(choices=["a"], allow_free_text=False))

        assert FREE_TEXT not in [c.value for c in prompt.choices]

    def test_free_text_carries_multiline(self) -> None:
        """Free text defaults to multi-line, where long answers are expected."""
        prompt = self._build(self._question())

        free_text = next(c for c in prompt.choices if c.value == FREE_TEXT)
        assert free_text.multiline is True
        assert free_text.prompt_for == "answer"

    def test_disabled_nav_controls_are_absent(self) -> None:
        """Flags remove the control entirely rather than rejecting it later."""
        prompt = self._build(
            self._question(), nav=NavFlags(back=False, skip=False, skip_all=False, abort=False)
        )

        values = [c.value for c in prompt.choices]
        assert NAV_SKIP not in values
        assert NAV_SKIP_ALL not in values
        assert NAV_ABORT not in values

    def test_abort_is_opt_in(self) -> None:
        """Abort appears only when enabled."""
        prompt = self._build(self._question(), nav=NavFlags(abort=True))

        assert NAV_ABORT in [c.value for c in prompt.choices]

    def test_progress_header_is_in_the_prompt(self) -> None:
        """The dashboard renders progress from the prompt markdown."""
        prompt = self._build(self._question(), cursor=2, total=7)

        assert "Question 3 of 7" in prompt.prompt

    def test_intro_shows_only_on_the_first_question(self) -> None:
        """Repeating the intro on every question would be noise."""
        first = self._build(self._question(), cursor=0, intro="Read me")
        later = self._build(self._question(), cursor=1, intro="Read me")

        assert "Read me" in first.prompt
        assert "Read me" not in later.prompt

    def test_auto_select_is_never_set(self) -> None:
        """--skip-gates must not fabricate an answer from a suggested choice."""
        prompt = self._build(self._question(choices=["Server"]))

        assert prompt.auto_select is None

    def test_prompt_id_is_carried(self) -> None:
        """The staleness token reaches the wire."""
        prompt = self._build(self._question())

        assert prompt.prompt_id == "ask:run:1"


class TestBuildOutput:
    """The node's output contract."""

    def _order(self):
        return resolve_questions(
            [QuestionDef(text="First?"), QuestionDef(text="Second?")],
            render=lambda s: s,
            agent_name="ask",
        )

    def test_answers_are_keyed_not_concatenated(self) -> None:
        """A keyed dict is what makes an answer addressable, therefore revisable."""
        order = self._order()
        records = {
            "q1": AnswerRecord("q1", "First?", "A", "choice"),
            "q2": AnswerRecord("q2", "Second?", "B", "free_text"),
        }

        output = build_output(records, order, "completed")

        assert output["answers"] == {"q1": "A", "q2": "B"}

    def test_skipped_answers_are_excluded_from_answers(self) -> None:
        """A skipped question must not look answered downstream."""
        order = self._order()
        records = {
            "q1": AnswerRecord("q1", "First?", "A", "choice"),
            "q2": AnswerRecord("q2", "Second?", None, "skipped"),
        }

        output = build_output(records, order, "completed")

        assert output["answers"] == {"q1": "A"}
        assert output["answered_count"] == 1
        assert output["skipped_count"] == 1
        assert output["items"][1]["skipped"] is True

    def test_default_answer_counts_as_answered(self) -> None:
        """A declared default is a real answer, not a skip."""
        order = self._order()
        records = {"q1": AnswerRecord("q1", "First?", "fallback", "default")}

        output = build_output(records, order, "completed")

        assert output["answers"] == {"q1": "fallback"}
        assert output["skipped_count"] == 0

    def test_transcript_follows_presentation_order(self) -> None:
        """The transcript numbers questions as presented, not as answered."""
        order = self._order()
        records = {
            "q2": AnswerRecord("q2", "Second?", "B", "choice"),
            "q1": AnswerRecord("q1", "First?", "A", "choice"),
        }

        output = build_output(records, order, "completed")

        assert output["transcript"] == "Q1. First?\nA: A\n\nQ2. Second?\nA: B"

    def test_unanswered_questions_are_absent(self) -> None:
        """A partially-answered node reports only what it has."""
        order = self._order()
        records = {"q1": AnswerRecord("q1", "First?", "A", "choice")}

        output = build_output(records, order, "in_progress")

        assert len(output["items"]) == 1
        assert output["answered_any"] is True
        assert output["outcome"] == "in_progress"

    def test_empty_records_report_nothing_answered(self) -> None:
        """``answered_any`` is the cheap check for "did the human engage"."""
        output = build_output({}, self._order(), "skipped_remaining")

        assert output["answered_any"] is False
        assert output["answers"] == {}
