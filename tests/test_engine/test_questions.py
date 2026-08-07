"""Engine-level tests for ``type: questions`` steps.

The pure helpers are covered in ``tests/test_executor/test_questions.py``.
These exercise the cursor loop, the context/resume path, and the
``--skip-gates`` policy, which only exist in the engine.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from conductor.config.schema import (
    AgentDef,
    InputDef,
    OutputField,
    QuestionDef,
    RouteDef,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.exceptions import ExecutionError
from conductor.gates.human import GateResponse
from conductor.providers.copilot import CopilotProvider


def _config(questions_agent: AgentDef, **overrides) -> WorkflowConfig:
    """Build a two-step workflow whose entry point is the questions node."""
    return WorkflowConfig(
        workflow=WorkflowDef(name="q", entry_point=questions_agent.name),
        agents=[
            questions_agent,
            AgentDef(
                name="after",
                model="gpt-4",
                prompt="done",
                output={"received": OutputField(type="string")},
                routes=[RouteDef(to="$end")],
            ),
        ],
        output=overrides.get("output", {"outcome": "{{ ask.output.outcome }}"}),
    )


def _engine(config: WorkflowConfig, *, skip_gates: bool = False) -> WorkflowEngine:
    """Build an engine with a stubbed provider."""
    provider = CopilotProvider(mock_handler=lambda a, p, c: {"received": "ok"})
    return WorkflowEngine(config, provider, skip_gates=skip_gates)


def _scripted(responses: list[GateResponse]):
    """Return a ``_resolve_human_prompt`` stub that replays fixed responses.

    Also records every prompt it saw so tests can assert on what was offered.
    """
    seen: list = []
    iterator = iter(responses)

    async def _resolve(self, gate_prompt):
        seen.append(gate_prompt)
        return next(iterator)

    return _resolve, seen


class TestQuestionsCursor:
    """Navigation through the question set."""

    @pytest.mark.asyncio
    async def test_answers_are_collected_in_order(self) -> None:
        """Each answer lands under its own key."""
        agent = AgentDef(
            name="ask",
            type="questions",
            questions=[QuestionDef(text="First?"), QuestionDef(text="Second?")],
            routes=[RouteDef(to="after")],
        )
        engine = _engine(_config(agent))
        resolve, seen = _scripted(
            [
                GateResponse(value="__free_text__", label="w", additional_input={"answer": "A"}),
                GateResponse(value="__free_text__", label="w", additional_input={"answer": "B"}),
                GateResponse(value="__finish__", label="finish"),
            ]
        )

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            await engine.run({})

        output = engine.context.agent_outputs["ask"]
        assert output["answers"] == {"q1": "A", "q2": "B"}
        assert output["outcome"] == "completed"
        # Two questions plus the closing review prompt.
        assert len(seen) == 3

    @pytest.mark.asyncio
    async def test_back_clears_the_revisited_answer(self) -> None:
        """Going back must overwrite, not append a second answer.

        This is the whole point of keying answers instead of concatenating a
        transcript string — the loop this replaces could not express it.
        """
        agent = AgentDef(
            name="ask",
            type="questions",
            questions=[QuestionDef(text="First?"), QuestionDef(text="Second?")],
            routes=[RouteDef(to="after")],
        )
        engine = _engine(_config(agent))
        resolve, seen = _scripted(
            [
                GateResponse(value="__free_text__", label="w", additional_input={"answer": "A"}),
                GateResponse(
                    value="__free_text__", label="w", additional_input={"answer": "first try"}
                ),
                # Review prompt -> back to Q2, revise it, then finish.
                GateResponse(value="__back__", label="back"),
                GateResponse(
                    value="__free_text__", label="w", additional_input={"answer": "revised"}
                ),
                GateResponse(value="__finish__", label="finish"),
            ]
        )

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            await engine.run({})

        output = engine.context.agent_outputs["ask"]
        assert output["answers"] == {"q1": "A", "q2": "revised"}
        assert output["answered_count"] == 2
        # Back from the review lands on the last question, not two earlier.
        assert "Question 2 of 2" in seen[3].prompt

    @pytest.mark.asyncio
    async def test_back_from_the_last_question_is_reachable(self) -> None:
        """Without the closing review, answering the last question would end
        the node instantly and Back would be unusable exactly where a user is
        most likely to want it."""
        agent = AgentDef(
            name="ask",
            type="questions",
            questions=[QuestionDef(text="Only?")],
            routes=[RouteDef(to="after")],
        )
        engine = _engine(_config(agent))
        resolve, seen = _scripted(
            [
                GateResponse(value="__free_text__", label="w", additional_input={"answer": "one"}),
                GateResponse(value="__back__", label="back"),
                GateResponse(value="__free_text__", label="w", additional_input={"answer": "two"}),
                GateResponse(value="__finish__", label="finish"),
            ]
        )

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            await engine.run({})

        assert engine.context.agent_outputs["ask"]["answers"] == {"q1": "two"}
        assert "__finish__" in [c.value for c in seen[1].choices]

    @pytest.mark.asyncio
    async def test_review_is_skipped_when_back_is_disabled(self) -> None:
        """The review only exists to keep Back reachable."""
        agent = AgentDef(
            name="ask",
            type="questions",
            questions=[QuestionDef(text="Only?")],
            allow_back=False,
            routes=[RouteDef(to="after")],
        )
        engine = _engine(_config(agent))
        resolve, seen = _scripted(
            [GateResponse(value="__free_text__", label="w", additional_input={"answer": "one"})]
        )

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            await engine.run({})

        assert len(seen) == 1
        assert engine.context.agent_outputs["ask"]["outcome"] == "completed"

    @pytest.mark.asyncio
    async def test_multiline_free_text_is_preserved(self) -> None:
        """Newlines inside an answer survive into the context."""
        agent = AgentDef(
            name="ask",
            type="questions",
            questions=[QuestionDef(text="Why?")],
            routes=[RouteDef(to="after")],
        )
        engine = _engine(_config(agent))
        resolve, _ = _scripted(
            [
                GateResponse(
                    value="__free_text__", label="w", additional_input={"answer": "line1\nline2"}
                ),
                GateResponse(value="__finish__", label="finish"),
            ]
        )

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            await engine.run({})

        assert engine.context.agent_outputs["ask"]["answers"]["q1"] == "line1\nline2"

    @pytest.mark.asyncio
    async def test_skip_all_marks_remaining_and_stops_prompting(self) -> None:
        """Skip-all must not keep asking."""
        agent = AgentDef(
            name="ask",
            type="questions",
            questions=[QuestionDef(text=f"Q{i}?") for i in range(4)],
            routes=[RouteDef(to="after")],
        )
        engine = _engine(_config(agent))
        resolve, seen = _scripted(
            [
                GateResponse(value="__free_text__", label="w", additional_input={"answer": "A"}),
                GateResponse(value="__skip_all__", label="skip all"),
            ]
        )

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            await engine.run({})

        output = engine.context.agent_outputs["ask"]
        assert len(seen) == 2
        assert output["outcome"] == "skipped_remaining"
        assert output["answers"] == {"q1": "A"}
        assert output["skipped_count"] == 3

    @pytest.mark.asyncio
    async def test_choice_selection_records_provenance(self) -> None:
        """A selected suggestion is recorded as a choice, not free text."""
        agent = AgentDef(
            name="ask",
            type="questions",
            questions=[QuestionDef(text="Server or client?", choices=["Server", "Client"])],
            routes=[RouteDef(to="after")],
        )
        engine = _engine(_config(agent))
        resolve, _ = _scripted(
            [
                GateResponse(value="Server", label="Server"),
                GateResponse(value="__finish__", label="finish"),
            ]
        )

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            await engine.run({})

        item = engine.context.agent_outputs["ask"]["items"][0]
        assert item["answer"] == "Server"
        assert item["source"] == "choice"


class TestQuestionsRequired:
    """``required`` blocks submission, never navigation."""

    @pytest.mark.asyncio
    async def test_required_question_rejects_empty_free_text(self) -> None:
        """An empty answer re-presents the question instead of being accepted."""
        agent = AgentDef(
            name="ask",
            type="questions",
            questions=[QuestionDef(text="Why?", required=True)],
            routes=[RouteDef(to="after")],
        )
        engine = _engine(_config(agent))
        resolve, seen = _scripted(
            [
                GateResponse(value="__free_text__", label="w", additional_input={"answer": "  "}),
                GateResponse(value="__free_text__", label="w", additional_input={"answer": "real"}),
                GateResponse(value="__finish__", label="finish"),
            ]
        )

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            await engine.run({})

        # Rejected attempt, accepted answer, then the closing review.
        assert len(seen) == 3
        assert engine.context.agent_outputs["ask"]["answers"] == {"q1": "real"}

    @pytest.mark.asyncio
    async def test_required_question_cannot_be_skipped(self) -> None:
        """Skip is refused with a reason rather than silently recording a skip."""
        agent = AgentDef(
            name="ask",
            type="questions",
            questions=[QuestionDef(text="Why?", required=True)],
            routes=[RouteDef(to="after")],
        )
        engine = _engine(_config(agent))
        resolve, seen = _scripted(
            [
                GateResponse(value="__skip__", label="skip"),
                GateResponse(value="__free_text__", label="w", additional_input={"answer": "x"}),
                GateResponse(value="__finish__", label="finish"),
            ]
        )

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            await engine.run({})

        # Refused skip, accepted answer, then the closing review.
        assert len(seen) == 3
        assert engine.context.agent_outputs["ask"]["skipped_count"] == 0

    @pytest.mark.asyncio
    async def test_required_with_default_can_be_skipped(self) -> None:
        """A default means the question has an answer, so skipping is safe."""
        agent = AgentDef(
            name="ask",
            type="questions",
            questions=[QuestionDef(text="Why?", required=True, default="fallback")],
            routes=[RouteDef(to="after")],
        )
        engine = _engine(_config(agent))
        resolve, seen = _scripted(
            [
                GateResponse(value="__skip__", label="skip"),
                GateResponse(value="__finish__", label="finish"),
            ]
        )

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            await engine.run({})

        assert len(seen) == 2
        assert engine.context.agent_outputs["ask"]["answers"] == {"q1": "fallback"}


class TestQuestionsSkipGates:
    """``--skip-gates`` must never invent a human answer."""

    @pytest.mark.asyncio
    async def test_skip_gates_never_prompts(self) -> None:
        """Automation skips honestly instead of selecting a suggestion.

        ``options[0]`` for a question is the *upstream agent's* first suggested
        answer, so auto-selecting it would feed invented input back as real.
        """
        agent = AgentDef(
            name="ask",
            type="questions",
            questions=[QuestionDef(text="Server or client?", choices=["Server", "Client"])],
            routes=[RouteDef(to="after")],
        )
        engine = _engine(_config(agent), skip_gates=True)
        resolve, seen = _scripted([])

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            await engine.run({})

        output = engine.context.agent_outputs["ask"]
        assert seen == []
        assert output["outcome"] == "skipped_remaining"
        assert output["answers"] == {}
        assert output["answered_any"] is False

    @pytest.mark.asyncio
    async def test_skip_gates_uses_declared_defaults(self) -> None:
        """A default is author-declared, so it is safe to apply unattended."""
        agent = AgentDef(
            name="ask",
            type="questions",
            questions=[QuestionDef(text="Why?", default="declared")],
            routes=[RouteDef(to="after")],
        )
        engine = _engine(_config(agent), skip_gates=True)

        await engine.run({})

        assert engine.context.agent_outputs["ask"]["answers"] == {"q1": "declared"}


class TestQuestionsSource:
    """Resolving questions from workflow context."""

    @pytest.mark.asyncio
    async def test_source_of_plain_strings(self) -> None:
        """The existing ``array of string`` shape needs no producer changes."""
        agent = AgentDef(
            name="ask",
            type="questions",
            source="seed.output.open_questions",
            routes=[RouteDef(to="after")],
        )
        config = _config(agent)
        config.workflow.entry_point = "seed"
        config.agents.insert(
            0,
            AgentDef(
                name="seed",
                model="gpt-4",
                prompt="seed",
                output={"open_questions": OutputField(type="array")},
                routes=[RouteDef(to="ask")],
            ),
        )
        provider = CopilotProvider(
            mock_handler=lambda a, p, c: (
                {"open_questions": ["First?", "Second?"]}
                if a.name == "seed"
                else {"received": "ok"}
            )
        )
        engine = WorkflowEngine(config, provider, skip_gates=False)
        resolve, seen = _scripted(
            [
                GateResponse(value="__free_text__", label="w", additional_input={"answer": "A"}),
                GateResponse(value="__skip__", label="skip"),
                GateResponse(value="__finish__", label="finish"),
            ]
        )

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            await engine.run({})

        assert len(seen) == 3
        assert "First?" in seen[0].prompt
        assert engine.context.agent_outputs["ask"]["answers"] == {"q1": "A"}

    @pytest.mark.asyncio
    async def test_source_of_objects_offers_choices(self) -> None:
        """Objects let the upstream agent propose candidate answers."""
        agent = AgentDef(
            name="ask",
            type="questions",
            source="seed.output.open_questions",
            routes=[RouteDef(to="after")],
        )
        config = _config(agent)
        config.workflow.entry_point = "seed"
        config.agents.insert(
            0,
            AgentDef(
                name="seed",
                model="gpt-4",
                prompt="seed",
                output={"open_questions": OutputField(type="array")},
                routes=[RouteDef(to="ask")],
            ),
        )
        provider = CopilotProvider(
            mock_handler=lambda a, p, c: (
                {"open_questions": [{"question": "Which?", "choices": ["X", "Y"]}]}
                if a.name == "seed"
                else {"received": "ok"}
            )
        )
        engine = WorkflowEngine(config, provider, skip_gates=False)
        resolve, seen = _scripted(
            [
                GateResponse(value="X", label="X"),
                GateResponse(value="__finish__", label="finish"),
            ]
        )

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            await engine.run({})

        assert [c.value for c in seen[0].choices][:2] == ["X", "Y"]
        assert engine.context.agent_outputs["ask"]["answers"] == {"q1": "X"}

    @pytest.mark.asyncio
    async def test_malformed_source_entry_raises(self) -> None:
        """A bad entry fails loudly rather than being silently dropped."""
        agent = AgentDef(
            name="ask",
            type="questions",
            source="seed.output.open_questions",
            routes=[RouteDef(to="after")],
        )
        config = _config(agent)
        config.workflow.entry_point = "seed"
        config.agents.insert(
            0,
            AgentDef(
                name="seed",
                model="gpt-4",
                prompt="seed",
                output={"open_questions": OutputField(type="array")},
                routes=[RouteDef(to="ask")],
            ),
        )
        provider = CopilotProvider(
            mock_handler=lambda a, p, c: (
                {"open_questions": [123]} if a.name == "seed" else {"received": "ok"}
            )
        )
        engine = WorkflowEngine(config, provider, skip_gates=False)

        with pytest.raises(ExecutionError, match="expected a string or an object"):
            await engine.run({})


class TestQuestionsDurability:
    """Partial answers survive a checkpoint without a schema change."""

    @pytest.mark.asyncio
    async def test_progress_is_committed_after_each_answer(self) -> None:
        """Context carries partial answers mid-node, which is what a
        checkpoint serializes (``WorkflowContext.to_dict``)."""
        agent = AgentDef(
            name="ask",
            type="questions",
            questions=[QuestionDef(text="First?"), QuestionDef(text="Second?")],
            routes=[RouteDef(to="after")],
        )
        engine = _engine(_config(agent))
        snapshots: list[dict] = []

        async def _resolve(self, gate_prompt):
            snapshots.append(dict(self.context.agent_outputs.get("ask") or {}))
            # The closing review offers Finish only; answering it would
            # re-present forever, exactly as an invalid CLI selection does.
            if "__finish__" in [c.value for c in gate_prompt.choices]:
                return GateResponse(value="__finish__", label="finish")
            return GateResponse(value="__free_text__", label="w", additional_input={"answer": "x"})

        with patch.object(WorkflowEngine, "_resolve_human_prompt", _resolve):
            await engine.run({})

        # Nothing stored before the first answer; Q1's answer visible by Q2.
        assert snapshots[0] == {}
        assert snapshots[1]["answers"] == {"q1": "x"}
        assert snapshots[1]["outcome"] == "in_progress"

    @pytest.mark.asyncio
    async def test_resume_continues_at_the_first_unanswered_question(self) -> None:
        """A restored node must not re-ask what the human already answered."""
        agent = AgentDef(
            name="ask",
            type="questions",
            questions=[
                QuestionDef(text="First?"),
                QuestionDef(text="Second?"),
                QuestionDef(text="Third?"),
            ],
            routes=[RouteDef(to="after")],
        )
        engine = _engine(_config(agent))
        engine.context.store(
            "ask",
            {
                "answers": {"q1": "restored"},
                "items": [
                    {
                        "id": "q1",
                        "question": "First?",
                        "answer": "restored",
                        "source": "free_text",
                        "skipped": False,
                    }
                ],
                "transcript": "",
                "answered_count": 1,
                "skipped_count": 0,
                "answered_any": True,
                "outcome": "in_progress",
            },
        )
        # What resume() sets; an ordinary loop-back deliberately does not.
        engine._resuming_questions = True
        resolve, seen = _scripted(
            [
                GateResponse(value="__free_text__", label="w", additional_input={"answer": "B"}),
                GateResponse(value="__free_text__", label="w", additional_input={"answer": "C"}),
                GateResponse(value="__finish__", label="finish"),
            ]
        )

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            await engine.run({})

        assert len(seen) == 3
        assert "Question 2 of 3" in seen[0].prompt
        assert engine.context.agent_outputs["ask"]["answers"] == {
            "q1": "restored",
            "q2": "B",
            "q3": "C",
        }

    @pytest.mark.asyncio
    async def test_resume_drops_answers_to_removed_questions(self) -> None:
        """Editing the workflow must not resurrect an orphaned answer.

        The removed entry is marked skipped so it is observable: without the
        filter it would inflate ``skipped_count``.
        """
        agent = AgentDef(
            name="ask",
            type="questions",
            questions=[QuestionDef(id="kept", text="Kept?")],
            routes=[RouteDef(to="after")],
        )
        engine = _engine(_config(agent))
        engine.context.store(
            "ask",
            {
                "items": [
                    {
                        "id": "removed",
                        "question": "Gone?",
                        "answer": None,
                        "source": "skipped",
                        "skipped": True,
                    }
                ]
            },
        )
        engine._resuming_questions = True
        resolve, seen = _scripted(
            [
                GateResponse(
                    value="__free_text__", label="w", additional_input={"answer": "fresh"}
                ),
                GateResponse(value="__finish__", label="finish"),
            ]
        )

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            await engine.run({})

        output = engine.context.agent_outputs["ask"]
        assert len(seen) == 2
        assert output["answers"] == {"kept": "fresh"}
        assert output["skipped_count"] == 0
        assert [i["id"] for i in output["items"]] == ["kept"]

    @pytest.mark.asyncio
    async def test_resume_rejects_an_unknown_answer_source(self) -> None:
        """A hand-edited checkpoint must not smuggle an unknown source through."""
        agent = AgentDef(
            name="ask",
            type="questions",
            questions=[QuestionDef(id="q", text="Q?"), QuestionDef(id="q2", text="Q2?")],
            routes=[RouteDef(to="after")],
        )
        engine = _engine(_config(agent))
        engine.context.store(
            "ask",
            {"items": [{"id": "q", "question": "Q?", "answer": "leaked", "source": "banana"}]},
        )
        engine._resuming_questions = True
        resolve, _ = _scripted(
            [
                GateResponse(value="__skip__", label="skip"),
                GateResponse(value="__finish__", label="finish"),
            ]
        )

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            await engine.run({})

        output = engine.context.agent_outputs["ask"]
        assert output["items"][0]["source"] == "skipped"
        # The answer is dropped with the source, so it cannot appear in the
        # transcript while being absent from `answers`.
        assert output["items"][0]["answer"] is None
        assert "leaked" not in output["transcript"]


class TestQuestionsLoopBack:
    """Re-entering the node must ask again, not replay the previous pass."""

    @pytest.mark.asyncio
    async def test_loop_back_asks_the_new_questions(self) -> None:
        """Ids default to positional q1..qN, so a second pass over a different
        question set would otherwise inherit the first pass's answers and
        report answers the human never gave."""
        agent = AgentDef(
            name="ask",
            type="questions",
            source="architect.output.open_questions",
            routes=[RouteDef(to="architect")],
        )
        config = WorkflowConfig(
            workflow=WorkflowDef(name="q", entry_point="architect"),
            agents=[
                AgentDef(
                    name="architect",
                    model="gpt-4",
                    prompt="ask",
                    output={
                        "open_questions": OutputField(type="array"),
                        "round": OutputField(type="string"),
                    },
                    routes=[
                        RouteDef(to="ask", when="{{ architect.output.round != '3' }}"),
                        RouteDef(to="$end"),
                    ],
                ),
                agent,
            ],
            output={"answers": "{{ ask.output.answers | tojson }}"},
        )

        rounds = iter([("1", ["First?"]), ("2", ["Totally different?"]), ("3", [])])

        def _handler(a, _p, _c):
            if a.name != "architect":
                return {"received": "ok"}
            round_id, questions = next(rounds)
            return {"open_questions": questions, "round": round_id}

        provider = CopilotProvider(mock_handler=_handler)
        engine = WorkflowEngine(config, provider, skip_gates=False)
        resolve, seen = _scripted(
            [
                GateResponse(value="__free_text__", label="w", additional_input={"answer": "A"}),
                GateResponse(value="__finish__", label="finish"),
                GateResponse(value="__free_text__", label="w", additional_input={"answer": "B"}),
                GateResponse(value="__finish__", label="finish"),
            ]
        )

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            await engine.run({})

        # The second pass must present its own question, not skip straight to
        # a review pre-filled with the first pass's answer.
        assert "Totally different?" in seen[2].prompt
        assert engine.context.agent_outputs["ask"]["answers"] == {"q1": "B"}

    @pytest.mark.asyncio
    async def test_mid_node_progress_does_not_inflate_execution_history(self) -> None:
        """Committing after every answer must not add N history entries.

        ``context.store`` appends to execution_history and bumps
        current_iteration, which downstream prompts, the interrupt panel, and
        the dashboard's synthetic replay all read.
        """
        agent = AgentDef(
            name="ask",
            type="questions",
            questions=[QuestionDef(text=f"Q{i}?") for i in range(4)],
            allow_back=False,
            routes=[RouteDef(to="after")],
        )
        engine = _engine(_config(agent))
        resolve, _ = _scripted([GateResponse(value="__skip__", label="skip")] * 4)

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            await engine.run({})

        assert engine.context.execution_history.count("ask") == 1


class TestQuestionsSourceIsNotATemplate:
    """Model-authored question text must not be treated as a template."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text",
        [
            "Should the template use {{ user.id }} or the numeric id?",
            "Is the syntax {% if x %} supported?",
            "Use { { partial or unbalanced {{ ?",
        ],
    )
    async def test_jinja_in_a_model_authored_question_is_shown_verbatim(self, text: str) -> None:
        """These are ordinary questions for a developer tool; rendering them
        would abort the step just as the human was about to be asked."""
        agent = AgentDef(
            name="ask",
            type="questions",
            source="seed.output.open_questions",
            allow_back=False,
            routes=[RouteDef(to="after")],
        )
        config = _config(agent)
        config.workflow.entry_point = "seed"
        config.agents.insert(
            0,
            AgentDef(
                name="seed",
                model="gpt-4",
                prompt="seed",
                output={"open_questions": OutputField(type="array")},
                routes=[RouteDef(to="ask")],
            ),
        )
        provider = CopilotProvider(
            mock_handler=lambda a, p, c: (
                {"open_questions": [text]} if a.name == "seed" else {"received": "ok"}
            )
        )
        engine = WorkflowEngine(config, provider, skip_gates=False)
        resolve, seen = _scripted([GateResponse(value="__skip__", label="skip")])

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            await engine.run({})

        assert len(seen) == 1
        assert text in seen[0].prompt

    @pytest.mark.asyncio
    async def test_inline_question_text_is_still_rendered(self) -> None:
        """Author-written questions keep full template support."""
        agent = AgentDef(
            name="ask",
            type="questions",
            questions=[QuestionDef(text="Ship {{ workflow.input.topic }}?")],
            allow_back=False,
            routes=[RouteDef(to="after")],
        )
        config = _config(agent)
        config.workflow.input = {"topic": InputDef(type="string", required=True, description="t")}
        engine = _engine(config)
        resolve, seen = _scripted([GateResponse(value="__skip__", label="skip")])

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            await engine.run({"topic": "rate limiting"})

        assert "Ship rate limiting?" in seen[0].prompt


class TestQuestionsRoutingAndCost:
    """Routing, abort, and iteration accounting."""

    @pytest.mark.asyncio
    async def test_node_costs_one_iteration_regardless_of_count(self) -> None:
        """One step, not 2N — the reason this is a node and not a loop."""
        agent = AgentDef(
            name="ask",
            type="questions",
            questions=[QuestionDef(text=f"Q{i}?") for i in range(5)],
            routes=[RouteDef(to="after")],
        )
        engine = _engine(_config(agent))
        resolve, _ = _scripted(
            [GateResponse(value="__skip__", label="skip")] * 5
            + [GateResponse(value="__finish__", label="finish")]
        )

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            await engine.run({})

        assert engine.limits.get_agent_execution_count("ask") == 1

    @pytest.mark.asyncio
    async def test_abort_routes_to_the_declared_route(self) -> None:
        """Aborting leaves via abort_route, not the normal route.

        Uses a distinct third agent: pointing abort_route at ``$end`` would
        match the fallback and prove nothing.
        """
        agent = AgentDef(
            name="ask",
            type="questions",
            questions=[QuestionDef(text="Why?")],
            allow_abort=True,
            abort_route="rescue",
            routes=[RouteDef(to="after")],
        )
        config = _config(agent)
        config.agents.append(
            AgentDef(
                name="rescue",
                model="gpt-4",
                prompt="rescue",
                output={"received": OutputField(type="string")},
                routes=[RouteDef(to="$end")],
            )
        )
        engine = _engine(config)
        resolve, _ = _scripted([GateResponse(value="__abort__", label="abort")])

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            await engine.run({})

        assert engine.context.agent_outputs["ask"]["outcome"] == "aborted"
        assert "rescue" in engine.context.agent_outputs
        assert "after" not in engine.context.agent_outputs

    @pytest.mark.asyncio
    async def test_abort_without_a_route_ends_the_workflow(self) -> None:
        """`allow_abort` with no abort_route falls back to $end."""
        agent = AgentDef(
            name="ask",
            type="questions",
            questions=[QuestionDef(text="Why?")],
            allow_abort=True,
            routes=[RouteDef(to="after")],
        )
        engine = _engine(_config(agent))
        resolve, _ = _scripted([GateResponse(value="__abort__", label="abort")])

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            result = await engine.run({})

        assert "after" not in engine.context.agent_outputs
        assert result["outcome"] == "aborted"

    @pytest.mark.asyncio
    async def test_routes_evaluate_against_the_output(self) -> None:
        """Conditional routes can branch on whether anyone answered."""
        agent = AgentDef(
            name="ask",
            type="questions",
            questions=[QuestionDef(text="Why?")],
            routes=[
                RouteDef(to="after", when="{{ ask.output.answered_any }}"),
                RouteDef(to="$end"),
            ],
        )
        engine = _engine(_config(agent))
        resolve, _ = _scripted(
            [
                GateResponse(value="__skip__", label="skip"),
                GateResponse(value="__finish__", label="finish"),
            ]
        )

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            await engine.run({})

        assert "after" not in engine.context.agent_outputs

    @pytest.mark.asyncio
    async def test_every_prompt_carries_a_distinct_prompt_id(self) -> None:
        """All prompts share the node name, so the token is the only thing
        that stops a late click resolving a later question."""
        agent = AgentDef(
            name="ask",
            type="questions",
            questions=[QuestionDef(text="First?"), QuestionDef(text="Second?")],
            routes=[RouteDef(to="after")],
        )
        engine = _engine(_config(agent))
        resolve, seen = _scripted(
            [GateResponse(value="__skip__", label="skip")] * 2
            + [GateResponse(value="__finish__", label="finish")]
        )

        with patch.object(WorkflowEngine, "_resolve_human_prompt", resolve):
            await engine.run({})

        ids = [p.prompt_id for p in seen]
        assert all(ids)
        assert len(set(ids)) == len(ids)
        assert all(p.name == "ask" for p in seen)
