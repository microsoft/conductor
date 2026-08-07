"""Execution of ``type: questions`` steps (issue #376).

A ``questions`` node presents N prompts to a human inside **one** engine step,
holding the cursor and answers internally. That "one step" property is the
whole point: a workflow step cannot be un-executed, so a loop of N gate steps
can never support going back, and it burns 2N iterations against
``limits.max_iterations``.

Answers are keyed rather than concatenated, so revisiting question 3
overwrites ``answers.q3`` instead of appending a second entry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from conductor.exceptions import ExecutionError
from conductor.gates.human import GateChoice, GatePrompt

if TYPE_CHECKING:
    from collections.abc import Callable

    from conductor.config.schema import AgentDef, QuestionDef

logger = logging.getLogger(__name__)

# Control values are dunder-prefixed to keep them clear of suggested answers,
# which become choice values verbatim. `resolve_questions` rejects a suggested
# answer that lands on one anyway, so the separation is enforced, not assumed.
NAV_BACK = "__back__"
NAV_SKIP = "__skip__"
NAV_SKIP_ALL = "__skip_all__"
NAV_ABORT = "__abort__"
NAV_FINISH = "__finish__"
FREE_TEXT = "__free_text__"

FREE_TEXT_FIELD = "answer"
"""``prompt_for`` field name used for the write-your-own path."""

_RESERVED_VALUES = frozenset({NAV_BACK, NAV_SKIP, NAV_SKIP_ALL, NAV_ABORT, NAV_FINISH, FREE_TEXT})
"""Choice values reserved for controls; a suggested answer may not use one."""

AnswerSource = Literal["choice", "free_text", "default", "skipped"]
"""Where an answer came from. Mirrors ``CheckpointTrigger``'s Literal style."""

QuestionsOutcome = Literal["completed", "skipped_remaining", "aborted", "in_progress"]
"""Terminal state of a questions node. ``in_progress`` appears only in a
mid-node checkpoint, never in a completed node's output."""

# Named so callers compare against a symbol rather than a bare string: the
# output crosses a Jinja/JSON boundary as a plain dict, so a mistyped literal
# would silently stop matching (disabling the abort route) where a mistyped
# name is an immediate NameError.
OUTCOME_COMPLETED: QuestionsOutcome = "completed"
OUTCOME_SKIPPED_REMAINING: QuestionsOutcome = "skipped_remaining"
OUTCOME_ABORTED: QuestionsOutcome = "aborted"
OUTCOME_IN_PROGRESS: QuestionsOutcome = "in_progress"


@dataclass
class ResolvedQuestion:
    """A question with its rendered text and stable answer key."""

    id: str
    text: str
    hint: str | None = None
    choices: list[str] = field(default_factory=list)
    allow_free_text: bool = True
    default: str | None = None
    required: bool = False
    multiline: bool = True


@dataclass
class AnswerRecord:
    """One question's outcome."""

    id: str
    question: str
    answer: str | None
    source: AnswerSource

    @property
    def skipped(self) -> bool:
        """Whether this question was skipped without an answer.

        Derived rather than stored so ``skipped=True`` can never contradict a
        non-null ``answer`` — a state reachable through checkpoint restore,
        where it produced an answer visible in ``transcript`` but absent from
        ``answers``.
        """
        return self.source == "skipped"

    @classmethod
    def for_skip(cls, question: ResolvedQuestion) -> AnswerRecord:
        """Build the record for a skipped question, applying its default.

        A declared default is a real answer, so it is recorded as one. Three
        call sites derived this pairing independently before.

        Args:
            question: The question being skipped.

        Returns:
            The answer record.
        """
        return cls(
            id=question.id,
            question=question.text,
            answer=question.default,
            source="default" if question.default is not None else "skipped",
        )


@dataclass(frozen=True)
class NavFlags:
    """Resolved navigation controls for a questions node.

    The schema stores these as tri-state ``bool | None`` so it can reject them
    on other step types; this is the single place those defaults are applied.
    """

    back: bool = True
    skip: bool = True
    skip_all: bool = True
    abort: bool = False

    @classmethod
    def resolve(cls, agent: AgentDef) -> NavFlags:
        """Apply defaults to a node's declared navigation flags.

        Args:
            agent: The questions node definition.

        Returns:
            The resolved flags.
        """
        defaults = cls()
        return cls(
            back=defaults.back if agent.allow_back is None else agent.allow_back,
            skip=defaults.skip if agent.allow_skip is None else agent.allow_skip,
            skip_all=(defaults.skip_all if agent.allow_skip_all is None else agent.allow_skip_all),
            abort=defaults.abort if agent.allow_abort is None else agent.allow_abort,
        )


def coerce_questions(
    raw: list[Any],
    *,
    agent_name: str,
) -> list[QuestionDef]:
    """Normalize a resolved ``source`` array into question definitions.

    Plain strings become a question with only ``text`` set. This is exactly the
    shape agents already emit for ``open_questions: array of string``, so
    existing workflows migrate without touching the producing agent, and
    gaining choices later is a backward-compatible upgrade.

    Args:
        raw: The resolved array from workflow context.
        agent_name: Node name, for error messages.

    Returns:
        A list of QuestionDef.

    Raises:
        ExecutionError: If an entry is neither a string nor an object, or an
            object fails QuestionDef validation.
    """
    from pydantic import ValidationError as PydanticValidationError

    from conductor.config.schema import QuestionDef

    questions: list[QuestionDef] = []
    for index, entry in enumerate(raw):
        if isinstance(entry, str):
            questions.append(QuestionDef(text=entry))
            continue
        if isinstance(entry, dict):
            payload = dict(entry)
            # Accept ``question:`` as an alias for ``text:`` — agents asked to
            # emit ``{question, choices}`` objects reach for the domain word,
            # and silently dropping such entries would be worse than aliasing.
            if "text" not in payload and "question" in payload:
                payload["text"] = payload.pop("question")
            try:
                questions.append(QuestionDef.model_validate(payload))
            except PydanticValidationError as exc:
                raise ExecutionError(
                    f"Question {index + 1} in '{agent_name}' is not a valid question: {exc}",
                    suggestion=(
                        "Each entry must be a string, or an object with a 'text' "
                        "(or 'question') field."
                    ),
                ) from exc
            continue
        raise ExecutionError(
            f"Question {index + 1} in '{agent_name}' has type "
            f"{type(entry).__name__}, expected a string or an object",
            suggestion="Source arrays must contain strings or question objects.",
        )
    return questions


def resolve_questions(
    definitions: list[QuestionDef],
    *,
    render: Callable[[str], str],
    agent_name: str,
    trusted: bool = True,
) -> list[ResolvedQuestion]:
    """Render question text and assign stable answer keys.

    Args:
        definitions: The question definitions.
        render: Jinja2 renderer bound to the current workflow context.
        agent_name: Node name, for error messages.
        trusted: Whether the text was authored in the workflow file. Inline
            ``questions:`` are, and are validated at ``conductor validate``
            time, so they render normally. Text resolved from ``source:``
            came out of a model, where ``{{ user.id }}`` or ``{% if %}`` is
            an ordinary thing to ask about — rendering it would abort the
            step with a TemplateError just as the human was about to be
            asked. Untrusted text falls back to itself verbatim, matching
            ``for_each``, which injects source items as values and never
            renders them.

    Returns:
        Resolved questions in presentation order.

    Raises:
        ExecutionError: If two questions claim the same id, or a suggested
            answer collides with a reserved control value.
    """

    def _render(text: str) -> str:
        if trusted:
            return render(text)
        try:
            return render(text)
        except Exception:  # noqa: BLE001 — any template failure falls back
            logger.debug(
                "Question text from a source: array is not a valid template; using it verbatim: %r",
                text,
            )
            return text

    resolved: list[ResolvedQuestion] = []
    seen: set[str] = set()
    for index, definition in enumerate(definitions):
        question_id = definition.id or f"q{index + 1}"
        if question_id in seen:
            raise ExecutionError(
                f"Duplicate question id '{question_id}' in '{agent_name}'",
                suggestion=(
                    "Question ids become answer keys, so they must be unique. "
                    "Remove the explicit 'id' to fall back to q1..qN."
                ),
            )
        seen.add(question_id)
        rendered_choices = [_render(c) for c in (definition.choices or [])]
        reserved_hit = _RESERVED_VALUES.intersection(rendered_choices)
        if reserved_hit:
            raise ExecutionError(
                f"Question '{question_id}' in '{agent_name}' offers reserved choice "
                f"value(s) {sorted(reserved_hit)}",
                suggestion=(
                    "These values are used for navigation controls. Reword the suggested answer."
                ),
            )
        resolved.append(
            ResolvedQuestion(
                id=question_id,
                text=_render(definition.text),
                hint=_render(definition.hint) if definition.hint else None,
                choices=rendered_choices,
                allow_free_text=definition.allow_free_text,
                default=definition.default,
                required=definition.required,
                multiline=definition.multiline,
            )
        )
    return resolved


def build_prompt(
    agent: AgentDef,
    question: ResolvedQuestion,
    *,
    nav: NavFlags,
    cursor: int,
    total: int,
    answered: int,
    skipped: int,
    prompt_id: str,
    intro: str | None = None,
    rejection: str | None = None,
) -> GatePrompt:
    """Build the prompt for a single question.

    Args:
        agent: The questions node (read for its name).
        question: The question to present.
        nav: Resolved navigation controls.
        cursor: Zero-based index of this question.
        total: Total number of questions.
        answered: How many are answered so far.
        skipped: How many are skipped so far.
        prompt_id: Staleness token for this specific presentation.
        intro: Optional rendered node-level intro, shown on the first question.
        rejection: Why the previous attempt at this question was refused.
            Carried in the prompt body because a re-presentation otherwise
            looks identical to the first, on both the terminal and the
            dashboard.

    Returns:
        A GatePrompt with suggested answers, a write-your-own option, and the
        enabled navigation controls.

    Note:
        ``auto_select`` is deliberately left unset. Auto-selecting a suggested
        answer under ``--skip-gates`` would fabricate human input and feed it
        back to the upstream agent as real; the engine short-circuits to
        skip-all instead.
    """
    header = f"**Question {cursor + 1} of {total}** · {answered} answered, {skipped} skipped"
    body = [header, "", question.text]
    if question.hint:
        body += ["", f"_{question.hint}_"]
    if question.required:
        body += ["", "_An answer is required._"]
    if rejection:
        body += ["", f"**{rejection}**"]
    if intro and cursor == 0:
        body = [intro, "", *body]

    choices = [GateChoice(label=choice, value=choice) for choice in question.choices]
    if question.allow_free_text:
        choices.append(
            GateChoice(
                label="✎ Write your own",
                value=FREE_TEXT,
                prompt_for=FREE_TEXT_FIELD,
                multiline=question.multiline,
            )
        )
    if nav.back and cursor > 0:
        choices.append(GateChoice(label="← Back", value=NAV_BACK))
    # Skipping a required question is refused at resolution time rather than
    # hidden here, so the reason is stated instead of the control vanishing.
    if nav.skip:
        choices.append(GateChoice(label="⤼ Skip this question", value=NAV_SKIP))
    if nav.skip_all:
        choices.append(GateChoice(label="⏭ Skip all remaining", value=NAV_SKIP_ALL))
    if nav.abort:
        choices.append(GateChoice(label="✕ Abort", value=NAV_ABORT))

    return GatePrompt(
        name=agent.name,
        prompt="\n".join(body),
        choices=choices,
        prompt_id=prompt_id,
    )


def build_review_prompt(
    agent: AgentDef,
    records: dict[str, AnswerRecord],
    order: list[ResolvedQuestion],
    *,
    nav: NavFlags,
    prompt_id: str,
) -> GatePrompt:
    """Build the closing confirmation shown after the last question.

    Exists so Back is reachable from the final question. Without it the node
    would end the instant the last answer landed, making Back unusable exactly
    where a user is most likely to want it — having just seen the whole set.

    Args:
        agent: The questions node.
        records: Answers collected so far.
        order: All questions, in presentation order.
        nav: Resolved navigation controls.
        prompt_id: Staleness token for this presentation.

    Returns:
        A GatePrompt offering Finish, and Back when enabled.
    """
    answered = sum(1 for r in records.values() if not r.skipped)
    skipped = sum(1 for r in records.values() if r.skipped)
    lines = [
        f"**All {len(order)} questions reviewed** · {answered} answered, {skipped} skipped",
        "",
    ]
    for index, question in enumerate(order, 1):
        record = records.get(question.id)
        if record is None or record.answer is None:
            lines.append(f"{index}. {question.text}\n   _(skipped)_")
        else:
            lines.append(f"{index}. {question.text}\n   **{record.answer}**")

    choices = [GateChoice(label="✓ Finish", value=NAV_FINISH)]
    if nav.back:
        choices.append(GateChoice(label="← Back to last question", value=NAV_BACK))
    if nav.abort:
        choices.append(GateChoice(label="✕ Abort", value=NAV_ABORT))

    return GatePrompt(
        name=agent.name,
        prompt="\n".join(lines),
        choices=choices,
        prompt_id=prompt_id,
    )


def build_output(
    records: dict[str, AnswerRecord],
    order: list[ResolvedQuestion],
    outcome: QuestionsOutcome,
) -> dict[str, Any]:
    """Assemble the node's output from the answers collected so far.

    Args:
        records: Answer records keyed by question id.
        order: All questions, in presentation order.
        outcome: Terminal state; ``in_progress`` only in a mid-node
            checkpoint.

    Returns:
        The node output dict.
    """
    items: list[dict[str, Any]] = []
    answers: dict[str, str] = {}
    transcript_parts: list[str] = []

    for index, question in enumerate(order, 1):
        record = records.get(question.id)
        if record is None:
            continue
        items.append(
            {
                "id": record.id,
                "question": record.question,
                "answer": record.answer,
                "source": record.source,
                "skipped": record.skipped,
            }
        )
        if not record.skipped and record.answer is not None:
            answers[record.id] = record.answer
        if record.answer is not None:
            transcript_parts.append(f"Q{index}. {record.question}\nA: {record.answer}")

    answered_count = len(answers)
    skipped_count = sum(1 for r in records.values() if r.skipped)
    return {
        "answers": answers,
        "items": items,
        "transcript": "\n\n".join(transcript_parts),
        "answered_count": answered_count,
        "skipped_count": skipped_count,
        "answered_any": answered_count > 0,
        "outcome": outcome,
    }


__all__ = [
    "FREE_TEXT",
    "FREE_TEXT_FIELD",
    "NAV_ABORT",
    "NAV_BACK",
    "NAV_FINISH",
    "NAV_SKIP",
    "NAV_SKIP_ALL",
    "AnswerRecord",
    "AnswerSource",
    "OUTCOME_ABORTED",
    "OUTCOME_COMPLETED",
    "OUTCOME_IN_PROGRESS",
    "OUTCOME_SKIPPED_REMAINING",
    "NavFlags",
    "QuestionsOutcome",
    "ResolvedQuestion",
    "build_output",
    "build_review_prompt",
    "build_prompt",
    "coerce_questions",
    "resolve_questions",
]
