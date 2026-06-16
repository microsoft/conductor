"""Semantic output validator for the ``validator:`` agent block (issue #220).

After a provider-backed agent completes, :class:`OutputValidator` runs a
**second LLM call** that grades the primary output against a user-defined
rubric (``validator.criteria``). It returns a structured
:class:`ValidationOutcome` describing whether the output passed and, if not,
the concrete issues to fix.

This module is deliberately side-effect free: it does not emit workflow
events or record usage. It returns the raw :class:`AgentOutput` from the
validator call so the engine helper can attribute token cost to a separate
``"<agent> (validator)"`` row and emit the ``agent_validator_*`` events.

The validator runs as a synthetic agent through the provider's normal
``execute()`` path (with an ``output:`` schema of
``{"passed": bool, "issues": [str]}`` and no tools). Unlike
``execute_dialog_turn`` this yields a full ``AgentOutput`` with token
counts and works on every provider that implements ``execute`` — including
the experimental ``claude-agent-sdk`` provider.

Validation is **fail-open**: if the validator call raises or returns
unparseable output, the outcome is treated as a pass (with a logged
warning) so a flaky grader never blocks an otherwise-valid workflow.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from conductor.config.schema import AgentDef, OutputField

if TYPE_CHECKING:
    from conductor.providers.base import AgentOutput, AgentProvider

logger = logging.getLogger(__name__)

# Validator output schema injected on the synthetic agent so providers that
# build schema hints from ``agent.output`` steer the model toward the right
# JSON shape and run their JSON-recovery loop on the response.
_VALIDATOR_OUTPUT_SCHEMA: dict[str, OutputField] = {
    "passed": OutputField(type="boolean"),
    "issues": OutputField(type="array", items=OutputField(type="string")),
}

VALIDATOR_SYSTEM_PROMPT = """\
You are an output validator. Your job is to decide whether an agent's output \
satisfies a set of acceptance criteria defined by the workflow author. You are \
a strict but fair grader — do not invent requirements beyond the criteria, but \
do not pass output that fails any of them.

--- CRITERIA ---
{criteria}
--- END CRITERIA ---

Examine the agent's task and its output, then decide whether the output fully \
satisfies every point in the criteria.

You MUST respond with ONLY a JSON object (no markdown, no prose, no code fences):
{{"passed": true_or_false, "issues": ["specific actionable problem", ...]}}

- "passed" is true only if the output satisfies ALL of the criteria.
- "issues" is a list of concrete, actionable problems to fix; it must be empty \
when "passed" is true and non-empty when "passed" is false. Each issue should \
tell the agent exactly what to change.
"""

VALIDATOR_USER_PROMPT = """\
Agent name: {agent_name}

--- AGENT TASK (the prompt the agent was given) ---
{primary_prompt}
--- END AGENT TASK ---

--- AGENT OUTPUT (validate this) ---
{primary_output}
--- END AGENT OUTPUT ---
"""

# Sentinel appended when the primary prompt/output is truncated to fit the
# validator prompt. Signals to the grader that it is seeing partial data.
_TRUNCATION_MARKER = "\n…[truncated]"

# Character budgets for the embedded primary prompt and output. Generous
# enough for typical reviews while bounding validator prompt size/cost.
_PROMPT_LIMIT = 6000
_OUTPUT_LIMIT = 8000


def _truncate(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` chars, appending a marker if cut."""
    if len(text) <= limit:
        return text
    headroom = len(_TRUNCATION_MARKER)
    return text[: max(0, limit - headroom)] + _TRUNCATION_MARKER


@dataclass
class ValidationOutcome:
    """Result of a single output-validation call.

    Attributes:
        passed: Whether the primary output satisfied the criteria. Defaults
            to ``True`` on validator error/parse failure (fail-open).
        issues: Concrete, actionable problems reported by the validator
            (empty when ``passed`` is ``True``).
        output: The raw :class:`AgentOutput` from the validator call, used by
            the engine to attribute usage/cost. ``None`` when the validator
            call raised before producing output.
        errored: ``True`` when the validator failed open due to an exception
            or unparseable response (as opposed to a genuine pass).
    """

    passed: bool
    issues: list[str] = field(default_factory=list)
    output: AgentOutput | None = None
    errored: bool = False


class OutputValidator:
    """Runs a second LLM call to grade an agent's output against a rubric."""

    async def validate(
        self,
        agent: AgentDef,
        primary_prompt: str,
        primary_output: dict[str, Any],
        provider: AgentProvider,
    ) -> ValidationOutcome:
        """Validate ``primary_output`` against ``agent.validator.criteria``.

        Args:
            agent: The primary agent definition (must have ``validator`` set).
            primary_prompt: The primary agent's rendered prompt (plain text).
            primary_output: The primary agent's output content.
            provider: Provider used for the validator LLM call (the primary
                agent's provider).

        Returns:
            A :class:`ValidationOutcome`. Always fail-open on error.
        """
        if agent.validator is None:  # defensive; callers guard on this
            return ValidationOutcome(passed=True)

        try:
            output_str = json.dumps(primary_output, indent=2, default=str)
        except (TypeError, ValueError):
            output_str = str(primary_output)

        validator_agent = self._build_validator_agent(agent)
        rendered_prompt = VALIDATOR_USER_PROMPT.format(
            agent_name=agent.name,
            primary_prompt=_truncate(primary_prompt, _PROMPT_LIMIT),
            primary_output=_truncate(output_str, _OUTPUT_LIMIT),
        )

        try:
            output = await provider.execute(
                agent=validator_agent,
                context={},
                rendered_prompt=rendered_prompt,
                tools=[],
            )
        except Exception:
            logger.warning(
                "Validator call failed for agent '%s'; treating as pass",
                agent.name,
                exc_info=True,
            )
            return ValidationOutcome(passed=True, errored=True)

        passed, issues, parse_ok = self._parse(output.content)
        return ValidationOutcome(
            passed=passed,
            issues=issues,
            output=output,
            errored=not parse_ok,
        )

    def _build_validator_agent(self, agent: AgentDef) -> AgentDef:
        """Construct the synthetic agent used for the validator call.

        Inherits the primary agent's model unless ``validator.model`` is set.
        Carries the validator rubric as its system prompt, a fixed
        ``{passed, issues}`` output schema, and no tools.
        """
        assert agent.validator is not None
        model = agent.validator.model or agent.model
        return AgentDef(
            name=f"{agent.name} (validator)",
            model=model,
            prompt="",
            system_prompt=VALIDATOR_SYSTEM_PROMPT.format(criteria=agent.validator.criteria),
            tools=[],
            output=_VALIDATOR_OUTPUT_SCHEMA,
        )

    def _parse(self, content: Any) -> tuple[bool, list[str], bool]:
        """Parse validator output content into ``(passed, issues, parse_ok)``.

        Fail-open: any content that does not clearly express ``passed: false``
        is treated as a pass. ``parse_ok`` is ``False`` when the content was
        not a usable dict (so the engine can flag the fall-back to pass).
        """
        if not isinstance(content, dict):
            logger.warning("Validator returned non-dict content; treating as pass: %r", content)
            return True, [], False

        if "passed" not in content:
            logger.warning("Validator response missing 'passed'; treating as pass: %r", content)
            return True, [], False

        passed = bool(content.get("passed"))
        raw_issues = content.get("issues") or []
        if isinstance(raw_issues, str):
            issues = [raw_issues]
        elif isinstance(raw_issues, list):
            issues = [str(i) for i in raw_issues]
        else:
            issues = [str(raw_issues)]

        return passed, issues, True
