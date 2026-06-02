"""Cross-field validators for workflow configuration.

This module provides additional validation beyond Pydantic schema validation,
including semantic checks for agent references, input dependencies, and
tool references.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import jinja2
from jinja2 import Environment, meta, nodes

from conductor.exceptions import ConfigurationError
from conductor.providers.capabilities import ProviderCapabilities, get_capabilities

if TYPE_CHECKING:
    from conductor.config.schema import AgentDef, WorkflowConfig


# Shared Jinja2 environment used purely for AST parsing of template strings.
# We never render with this env; we only ask it to produce an AST so we can
# walk Getattr chains and find undeclared variables. Using Jinja2's own parser
# (rather than regex) gives us scope-aware tracking — `{% for x in y %}`,
# `{% set x = ... %}`, macro params — and string-literal awareness for free.
#
# `meta.find_undeclared_variables` runs Jinja2's compiler over the AST, which
# fails on unknown filters/tests (e.g. conductor's `| json` filter is registered
# at render time on a different env). We don't want validation to choke on that,
# so we install tolerant `filters`/`tests` mappings that pretend every name is
# defined and return identity. Render-time validation will surface real errors.


def _identity_filter(value: object, *_args: object, **_kwargs: object) -> object:
    return value


class _TolerantNameMap(dict):
    """A dict that pretends every key exists, returning an identity function.

    Used for Jinja2 ``Environment.filters`` / ``Environment.tests`` during
    validation so that workflow-specific filters (registered only at render
    time) don't cause the AST walk to raise ``TemplateAssertionError``.
    """

    def __contains__(self, key: object) -> bool:
        return True

    def __getitem__(self, key: str) -> object:
        try:
            return dict.__getitem__(self, key)
        except KeyError:
            return _identity_filter

    def get(self, key: str, default: object = None) -> object:
        return dict.get(self, key, _identity_filter)


_JINJA_ENV = Environment(autoescape=False)
_JINJA_ENV.filters = _TolerantNameMap(_JINJA_ENV.filters)
_JINJA_ENV.tests = _TolerantNameMap(_JINJA_ENV.tests)

_BUILTIN_NAMES = frozenset({"workflow", "context", "item", "_index", "_key", "loop"})

# Attribute names that mark a Getattr chain as an "output reference":
#   agent.output.field, group.outputs.member, group.errors.member
_OUTPUT_ATTRS = frozenset({"output", "outputs", "errors"})

# Attribute names that look like fields on an output but are actually built-in
# dict methods. We avoid emitting field-precision warnings for these because
# templates like ``{% for k, v in a.output.items() %}`` are valid uses of the
# whole output object — even though ``items`` lexically resembles a field.
# Note: the Call-vs-Getattr filter handles the common method-call case more
# precisely; this set is a belt-and-suspenders fallback for code paths that
# reference these names without calling them (e.g., assigning the method to a
# variable, which is rare in practice).
_DICT_METHOD_NAMES = frozenset({"items", "keys", "values", "get"})

# DFS path cap: larger workflows may get partial coverage analysis
_MAX_ENUMERATED_PATHS = 100

# Pattern for input references:
# - agent.output(.field)?
# - parallel_group.outputs.agent(.field)?
# - workflow.input.param
# All with optional ? suffix
INPUT_REF_PATTERN = re.compile(
    r"^(?:"
    r"(?P<agent>[a-zA-Z_][a-zA-Z0-9_]*)\.output(?:\.(?P<field>[a-zA-Z_][a-zA-Z0-9_]*))?|"
    r"(?P<parallel>[a-zA-Z_][a-zA-Z0-9_]*)\.(?P<pg_kind>outputs|errors)(?:\.(?P<pg_agent>[a-zA-Z_][a-zA-Z0-9_]*)(?:\.(?P<pg_field>[a-zA-Z_][a-zA-Z0-9_]*))?)?|"
    r"workflow\.input\.(?P<input>[a-zA-Z_][a-zA-Z0-9_]*)"
    r")(?P<optional>\?)?$"
)


def validate_workflow_config(
    config: WorkflowConfig,
    workflow_path: Path | None = None,
    *,
    _visited_subworkflows: frozenset[tuple[int, int]] | None = None,
    _subworkflow_depth: int = 0,
) -> list[str]:
    """Perform comprehensive validation of a workflow configuration.

    This function performs semantic validation beyond what Pydantic can check,
    including cross-field references, consistency checks, and Jinja2 template
    reference validation.

    Args:
        config: The WorkflowConfig to validate.
        workflow_path: Optional path to the workflow file (for !file resolution).
        _visited_subworkflows: Internal — set of canonical (st_dev, st_ino)
            tuples for sub-workflow files already on the validation stack,
            used for cycle detection in recursive sub-workflow validation.
            External callers should leave this as ``None``.
        _subworkflow_depth: Internal — current recursion depth for
            sub-workflow validation. External callers should leave this as 0.

    Returns:
        A list of warning messages (non-fatal issues).

    Raises:
        ConfigurationError: If any validation errors are found.
    """
    errors: list[str] = []
    warnings: list[str] = []

    # Build index of all addressable node names
    agent_names = {agent.name for agent in config.agents}
    parallel_names = {pg.name for pg in config.parallel}
    for_each_names = {fe.name for fe in config.for_each}
    all_names = agent_names | parallel_names | for_each_names

    # Validate entry_point exists (already done by Pydantic, but good to have explicit)
    if config.workflow.entry_point not in all_names:
        errors.append(
            f"entry_point '{config.workflow.entry_point}' not found in agents or parallel groups. "
            f"Available: {', '.join(sorted(all_names))}"
        )

    # Validate each agent
    for agent in config.agents:
        # Validate route targets - allow routing to agents and parallel groups
        agent_errors = _validate_agent_routes(agent.name, agent.routes, all_names)
        errors.extend(agent_errors)

        # Validate human_gate has options
        if agent.type == "human_gate":
            if not agent.options:
                errors.append(f"Agent '{agent.name}' is a human_gate but has no options defined")
            else:
                # Validate gate option routes - allow routing to agents and parallel groups
                for i, option in enumerate(agent.options):
                    if option.route != "$end" and option.route not in all_names:
                        errors.append(
                            f"Agent '{agent.name}' gate option {i} ('{option.label}') "
                            f"routes to unknown agent or parallel group '{option.route}'"
                        )

        # Validate input references
        input_errors, input_warnings = _validate_input_references(
            agent.name,
            agent.input,
            agent_names,
            parallel_names,
            set(config.workflow.input.keys()),
            for_each_names,
        )
        errors.extend(input_errors)
        warnings.extend(input_warnings)

        # Validate tool references (skip for script, set, and wait agents — they don't use tools)
        if agent.tools is not None and agent.tools and agent.type not in ("script", "set", "wait"):
            tool_errors = _validate_tool_references(agent.name, agent.tools, set(config.tools))
            errors.extend(tool_errors)

        # Warn when an LLM agent has system_prompt but no (non-empty) prompt.
        # The Copilot provider concatenates `agent.system_prompt` into the prompt,
        # but providers that ignore system_prompt entirely (e.g., Claude) would
        # leave such agents with an empty user message. Even with the executor
        # rendering system_prompt, omitting `prompt:` is a portability hazard
        # and almost always indicates the author meant to include a `prompt:`
        # block alongside the persona/methodology in `system_prompt:`.
        if (
            agent.type in (None, "agent")
            and agent.system_prompt
            and not (agent.prompt and agent.prompt.strip())
        ):
            warnings.append(
                f"Agent '{agent.name}' defines `system_prompt` but no `prompt` "
                "(or only whitespace). "
                "Some providers (e.g., Claude) ignore `system_prompt` entirely, "
                "which would send an empty user message to the model. "
                "Even with the Copilot provider, the model often responds poorly "
                "to a missing user prompt. Move the dynamic, must-execute content "
                "(input references, instructions) into a `prompt:` block; keep the "
                "persona and static methodology in `system_prompt:`."
            )

    # Validate parallel groups
    if config.parallel:
        parallel_errors = _validate_parallel_groups(config)
        errors.extend(parallel_errors)

    # Validate for_each groups: reject step types that can't be used inline
    for for_each_group in config.for_each:
        if for_each_group.agent.type == "script":
            errors.append(
                f"For-each group '{for_each_group.name}' uses a script step as its "
                "inline agent. Script steps cannot be used in for_each groups."
            )
        if for_each_group.agent.type == "wait":
            errors.append(
                f"For-each group '{for_each_group.name}' uses a wait step as its "
                "inline agent. Wait steps cannot be used in for_each groups."
            )
        if for_each_group.agent.type == "terminate":
            errors.append(
                f"For-each group '{for_each_group.name}' uses a terminate step as its "
                "inline agent. Terminate steps cannot run inside a for_each iteration; "
                "route to a terminate step from the for_each group's routes instead."
            )

    # Validate sub-workflow references (local paths and registry refs).
    # Skipped when workflow_path is not provided — relative paths cannot be
    # resolved without knowing the file's location.
    if workflow_path is not None:
        sub_errors, sub_warnings = _validate_subworkflow_refs(
            config,
            workflow_path,
            _visited=_visited_subworkflows,
            _depth=_subworkflow_depth,
        )
        errors.extend(sub_errors)
        warnings.extend(sub_warnings)

    # Validate workflow output references
    output_errors = _validate_output_references(
        config.output,
        agent_names | parallel_names | for_each_names,
        set(config.workflow.input.keys()),
    )
    errors.extend(output_errors)

    # Check output templates against conditional execution paths (warnings only)
    warnings.extend(_validate_output_path_coverage(config))

    # Validate Jinja2 template references across all agents
    tmpl_errors, tmpl_warnings = _validate_template_references(config, workflow_path)
    errors.extend(tmpl_errors)
    warnings.extend(tmpl_warnings)

    # Cross-check workflow features against each provider's declared
    # ProviderCapabilities (issue #241). Surfaces silent capability
    # mismatches at validate time rather than at runtime.
    cap_errors, cap_warnings = _validate_provider_capabilities(config)
    errors.extend(cap_errors)
    warnings.extend(cap_warnings)

    if errors:
        raise ConfigurationError(
            "Workflow configuration validation failed:\n  - " + "\n  - ".join(errors),
            suggestion="Fix the validation errors listed above and try again.",
        )

    return warnings


def _validate_agent_routes(
    agent_name: str,
    routes: list,
    valid_targets: set[str],
) -> list[str]:
    """Validate that all route targets exist.

    Args:
        agent_name: Name of the agent whose routes are being validated.
        routes: List of RouteDef objects.
        valid_targets: Set of valid target names (agents, parallel groups, and for-each groups).

    Returns:
        List of error messages.
    """
    errors: list[str] = []

    for i, route in enumerate(routes):
        if route.to != "$end" and route.to not in valid_targets:
            errors.append(
                f"Agent '{agent_name}' route {i} targets unknown agent, "
                f"parallel group, or for-each group '{route.to}'. "
                f"Use '$end' to terminate or one of: "
                f"{', '.join(sorted(valid_targets))}"
            )

    return errors


def _validate_input_references(
    agent_name: str,
    inputs: list[str],
    agent_names: set[str],
    parallel_names: set[str],
    workflow_inputs: set[str],
    for_each_names: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Validate input reference formats and targets.

    Args:
        agent_name: Name of the agent whose inputs are being validated.
        inputs: List of input reference strings.
        agent_names: Set of valid agent names.
        parallel_names: Set of valid parallel group names.
        workflow_inputs: Set of valid workflow input parameter names.
        for_each_names: Set of valid for-each group names.

    Returns:
        Tuple of (error messages, warning messages).
    """
    errors: list[str] = []
    warnings: list[str] = []
    group_names = parallel_names | (for_each_names or set())

    for input_ref in inputs:
        match = INPUT_REF_PATTERN.match(input_ref)

        if not match:
            errors.append(
                f"Agent '{agent_name}' has invalid input reference '{input_ref}'. "
                "Expected format: 'agent_name.output', 'agent_name.output.field', "
                "'parallel_group.outputs.agent_name', 'parallel_group.outputs.agent_name.field', "
                "or 'workflow.input.param_name' (append '?' for optional)"
            )
            continue

        # Check if referencing another agent's output
        ref_agent = match.group("agent")
        if ref_agent and ref_agent not in agent_names:
            is_optional = match.group("optional") == "?"
            if is_optional:
                warnings.append(
                    f"Agent '{agent_name}' has optional reference to unknown agent '{ref_agent}'"
                )
            else:
                errors.append(
                    f"Agent '{agent_name}' references unknown agent '{ref_agent}' in input"
                )

        # Check if referencing parallel/for-each group output
        ref_parallel = match.group("parallel")
        if ref_parallel and ref_parallel not in group_names:
            is_optional = match.group("optional") == "?"
            if is_optional:
                warnings.append(
                    f"Agent '{agent_name}' has optional reference to "
                    f"unknown parallel group '{ref_parallel}'"
                )
            else:
                errors.append(
                    f"Agent '{agent_name}' references unknown parallel group "
                    f"'{ref_parallel}' in input"
                )
        # Note: We cannot validate the specific agent within the parallel group here
        # as that would require knowing which agents are in which parallel groups
        # That validation happens in _validate_parallel_groups

        # Check if referencing workflow input
        workflow_input = match.group("input")
        if workflow_input and workflow_input not in workflow_inputs:
            is_optional = match.group("optional") == "?"
            if is_optional:
                warnings.append(
                    f"Agent '{agent_name}' has optional reference to unknown "
                    f"workflow input '{workflow_input}'"
                )
            else:
                errors.append(
                    f"Agent '{agent_name}' references unknown workflow input "
                    f"'{workflow_input}'. Available: {', '.join(sorted(workflow_inputs))}"
                )

    return errors, warnings


def _validate_tool_references(
    agent_name: str,
    agent_tools: list[str],
    workflow_tools: set[str],
) -> list[str]:
    """Validate that agent tools are defined at workflow level.

    Args:
        agent_name: Name of the agent whose tools are being validated.
        agent_tools: List of tool names the agent wants to use.
        workflow_tools: Set of tools defined at workflow level.

    Returns:
        List of error messages.
    """
    errors: list[str] = []

    for tool in agent_tools:
        if tool not in workflow_tools:
            errors.append(
                f"Agent '{agent_name}' references unknown tool '{tool}'. "
                f"Available tools: {', '.join(sorted(workflow_tools))}"
            )

    return errors


def _validate_output_references(
    output: dict[str, str],
    valid_names: set[str],
    workflow_inputs: set[str],
) -> list[str]:
    """Validate that output template references are valid.

    This performs a basic check for obvious references in Jinja2 templates.
    Full validation happens at render time.

    Args:
        output: Dict of output field names to template expressions.
        valid_names: Set of valid agent, parallel group, and for-each group names.
        workflow_inputs: Set of valid workflow input parameter names.

    Returns:
        List of error messages.
    """
    # This is a basic check - full validation happens at render time
    # We just check for obvious issues in the template patterns
    errors: list[str] = []

    # Pattern to find potential agent references in templates
    agent_ref_pattern = re.compile(r"\{\{\s*(\w+)\.output")

    for field, template in output.items():
        matches = agent_ref_pattern.findall(template)
        for ref in matches:
            if ref not in valid_names and ref not in ("workflow", "context"):
                errors.append(f"Workflow output '{field}' references unknown agent '{ref}'")

    return errors


def _validate_parallel_groups(config: WorkflowConfig) -> list[str]:
    """Validate parallel group configurations.

    This function validates:
    - Parallel agent references exist
    - Parallel agents have no routes
    - No cross-agent dependencies within parallel group
    - Unique names between parallel groups and agents
    - No nested parallel groups
    - No human gates in parallel groups

    Args:
        config: The WorkflowConfig to validate.

    Returns:
        List of error messages.
    """
    errors: list[str] = []

    # Build indices
    agent_names = {agent.name for agent in config.agents}
    parallel_names = {pg.name for pg in config.parallel}
    agents_by_name = {agent.name: agent for agent in config.agents}

    # PE-2.5: Validate unique names (parallel groups vs agents)
    name_conflicts = agent_names & parallel_names
    if name_conflicts:
        conflicts_str = ", ".join(sorted(name_conflicts))
        errors.append(
            f"Duplicate names found between agents and parallel groups: {conflicts_str}. "
            "Parallel group names must be unique from agent names."
        )

    # Validate each parallel group
    for pg in config.parallel:
        # PE-2.2: Validate parallel agent references exist
        for agent_name in pg.agents:
            if agent_name not in agent_names:
                errors.append(
                    f"Parallel group '{pg.name}' references unknown agent '{agent_name}'. "
                    f"Available agents: {', '.join(sorted(agent_names))}"
                )
                continue  # Skip further validation for this agent

            agent = agents_by_name[agent_name]

            # PE-2.3: Validate parallel agents have no routes
            if agent.routes:
                errors.append(
                    f"Agent '{agent_name}' in parallel group '{pg.name}' cannot have routes. "
                    "Agents within parallel groups must not define their own routing logic."
                )

            # PE-2.7: Validate no human gates in parallel groups
            if agent.type == "human_gate":
                errors.append(
                    f"Agent '{agent_name}' in parallel group '{pg.name}' is a human gate. "
                    "Human gates cannot be used in parallel groups."
                )

            # Validate no script steps in parallel groups
            if agent.type == "script":
                errors.append(
                    f"Agent '{agent_name}' in parallel group '{pg.name}' is a script step. "
                    "Script steps cannot be used in parallel groups."
                )

            # Validate no wait steps in parallel groups
            if agent.type == "wait":
                errors.append(
                    f"Agent '{agent_name}' in parallel group '{pg.name}' is a wait step. "
                    "Wait steps cannot be used in parallel groups."
                )

            # Validate no workflow steps in parallel groups
            if agent.type == "workflow":
                errors.append(
                    f"Agent '{agent_name}' in parallel group '{pg.name}' is a workflow step. "
                    "Workflow steps cannot be used in parallel groups."
                )

            # Validate no terminate steps in parallel groups
            if agent.type == "terminate":
                errors.append(
                    f"Agent '{agent_name}' in parallel group '{pg.name}' is a terminate step. "
                    "Terminate steps cannot run inside a parallel branch; route to a "
                    "terminate step from the parallel group's routes instead."
                )

        # PE-6.2: Validate parallel group route targets
        for_each_names = {fe.name for fe in config.for_each}
        all_names = agent_names | parallel_names | for_each_names
        route_errors = _validate_agent_routes(pg.name, pg.routes, all_names)
        errors.extend(route_errors)

        # PE-2.4: Validate no cross-agent dependencies within parallel group
        # Check if any agent in the parallel group references another agent in the same group
        pg_agents_set = set(pg.agents)
        for agent_name in pg.agents:
            if agent_name not in agents_by_name:
                continue  # Already reported as unknown

            agent = agents_by_name[agent_name]
            for input_ref in agent.input:
                # Parse input reference to extract agent name
                match = INPUT_REF_PATTERN.match(input_ref)
                if match:
                    ref_agent = match.group("agent")
                    if ref_agent and ref_agent in pg_agents_set and ref_agent != agent_name:
                        errors.append(
                            f"Agent '{agent_name}' in parallel group '{pg.name}' references "
                            f"another agent '{ref_agent}' in the same parallel group. "
                            "Agents within the same parallel group cannot have dependencies "
                            "on each other."
                        )

            # For 'set' steps, also walk value/values.* templates — they can
            # reference siblings directly without declaring them in input:.
            # Parallel execution uses a pre-group snapshot, so any reference
            # to a same-group member would silently miss its output.
            if agent.type == "set":
                for source_label, template_str in _collect_template_strings(agent):
                    refs = _extract_template_refs(template_str)
                    cross_refs = refs.agent_refs & pg_agents_set
                    cross_refs.discard(agent_name)
                    for ref_agent in sorted(cross_refs):
                        errors.append(
                            f"{source_label} references "
                            f"another agent '{ref_agent}' in the same parallel group "
                            f"'{pg.name}'. Agents within the same parallel group cannot "
                            "have dependencies on each other."
                        )

        # PE-2.6: Validate no nested parallel groups
        # This means checking if any agent name in pg.agents is actually a parallel group name
        nested_groups = pg_agents_set & parallel_names
        if nested_groups:
            nested_str = ", ".join(sorted(nested_groups))
            errors.append(
                f"Parallel group '{pg.name}' contains nested parallel groups: {nested_str}. "
                "Nested parallel groups are not supported."
            )

    return errors


def _terminate_agent_names(config: WorkflowConfig) -> set[str]:
    """Names of agents whose ``type`` is ``terminate``.

    Terminate steps end the workflow when reached and behave like ``$end`` for
    path-enumeration purposes (no outbound edges, sink in the routing graph).
    """
    return {agent.name for agent in config.agents if agent.type == "terminate"}


def _build_routing_graph(config: WorkflowConfig) -> dict[str, list[tuple[str, bool]]]:
    """Build adjacency list from workflow config for path analysis.

    Args:
        config: The WorkflowConfig to analyze.

    Returns:
        Dict mapping node names to list of (target, is_conditional) tuples.
    """
    graph: dict[str, list[tuple[str, bool]]] = {}
    for agent in config.agents:
        # Terminate steps end the workflow; treat them as sinks with no edges.
        if agent.type == "terminate":
            graph[agent.name] = []
            continue
        edges: list[tuple[str, bool]] = []
        if agent.routes:
            for route in agent.routes:
                edges.append((route.to, route.when is not None))
        elif agent.type == "human_gate" and agent.options:
            for option in agent.options:
                edges.append((option.route, True))
        graph[agent.name] = edges
    for pg in config.parallel:
        graph[pg.name] = [(r.to, r.when is not None) for r in pg.routes]
    for fe in config.for_each:
        graph[fe.name] = [(r.to, r.when is not None) for r in fe.routes]
    return graph


def _enumerate_paths_to_end(
    start: str,
    graph: dict[str, list[tuple[str, bool]]],
    max_depth: int = 50,
    terminal_nodes: frozenset[str] = frozenset(),
) -> list[list[str]]:
    """Enumerate paths from start to a terminal node via DFS.

    Args:
        start: Entry point node name.
        graph: Adjacency list from _build_routing_graph.
        max_depth: Maximum path depth (prevents infinite exploration).
        terminal_nodes: Set of node names that terminate the workflow in
            addition to the implicit ``$end`` sentinel (e.g., ``type: terminate``
            steps).

    Returns:
        List of paths (up to _MAX_ENUMERATED_PATHS), where each path is a list
        of node names. If the graph has more paths than the cap, returns the
        first ones found. Callers should treat results as best-effort for
        highly branchy workflows.
    """
    paths: list[list[str]] = []

    def dfs(current: str, path: list[str], visited: set[str]) -> None:
        if len(paths) >= _MAX_ENUMERATED_PATHS or len(path) > max_depth:
            return
        if current == "$end":
            paths.append(list(path))
            return
        if current in terminal_nodes:
            # Terminal node (e.g., terminate step) — record it as part of the
            # path so callers can inspect the terminating step.
            paths.append(list(path) + [current])
            return
        if current not in graph or current in visited:
            return
        visited.add(current)
        path.append(current)
        for target, _ in graph[current]:
            dfs(target, path, visited)
        path.pop()
        visited.discard(current)

    dfs(start, [], set())
    return paths


class TemplateRefs(NamedTuple):
    """Structured references extracted from a Jinja2 template.

    Provides both flat root-name sets (preserves the original API contract for
    "unknown agent/workflow input" checks) and per-reference field detail
    (enables explicit-mode field-precision warnings).

    Attributes:
        agent_refs: Root names referenced via ``<name>.output``,
            ``<name>.outputs``, or ``<name>.errors`` (deduped). Used for
            unknown-agent checks and undeclared-agent warnings.
        workflow_inputs: Names referenced via ``workflow.input.<name>``.
        agent_output_fields: Maps each agent name to the set of fields that
            were referenced via ``<name>.output.<field>``. The sentinel value
            ``None`` in the set means "bare ``<name>.output`` was referenced"
            (i.e., the whole-output object) — this distinguishes
            ``{{ a.output }}`` from ``{{ a.output.foo }}`` for field-precision
            analysis. Absence from this dict means no ``<name>.output*`` ref
            was seen (only ``.outputs`` / ``.errors`` perhaps).
        group_member_fields: Maps each ``(group, member)`` pair to the set of
            fields referenced via ``<group>.outputs.<member>.<field>``.
            ``None`` in the set indicates a bare
            ``<group>.outputs.<member>`` reference (whole member). The
            sentinel key ``(group, None)`` means the template referenced
            ``<group>.outputs`` with no member — all members are referenced
            implicitly.
        group_error_refs: Group names referenced via ``<group>.errors``. Kept
            separate from output refs because the engine's runtime semantics
            for ``.errors`` always copy the whole errors dict and never field-
            slice, so field-precision checks must not be applied to them.
    """

    agent_refs: set[str]
    workflow_inputs: set[str]
    agent_output_fields: dict[str, set[str | None]]
    group_member_fields: dict[tuple[str, str | None], set[str | None]]
    group_error_refs: set[str]


def _extract_template_refs(template: str) -> TemplateRefs:
    """Extract agent/group and workflow-input references from a Jinja2 template.

    Uses Jinja2's own parser, so:
      - Loop variables are excluded: ``{% for x in y %}{{ x.output }}{% endfor %}``
        does not produce a spurious reference to ``x``.
      - ``{% set x = ... %}`` bindings and macro parameters are excluded.
      - String literals are excluded: ``{{ x | replace("foo.output", "y") }}``
        does not produce a reference to ``foo``.
      - Method calls on outputs are detected: ``{{ a.output.items() }}`` does
        not emit a field ref to ``items`` (the ``items`` Getattr is the callee
        of a Call node and is treated as a method invocation, not a field
        access).

    A name is reported as an output reference when it appears as the root of a
    Getattr chain whose first attribute is one of ``output``/``outputs``/``errors``
    (e.g. ``agent.output.field``, ``group.outputs.member``, ``group.errors``).

    A name is reported as a workflow-input reference when the chain matches
    ``workflow.input.<name>``.

    Built-in namespaces (``workflow``, ``context``, ``item``, ``_index``, ``_key``,
    ``loop``) and any name bound by a Jinja2 scope are filtered out.

    Limitations (documented intentionally):
      - Bracket access (``a.output["bar"]``) is not detected. Detecting it
        would require walking ``Getitem`` nodes with constant string keys.
      - Dynamic field access (``a.output[var]``) is not detected.
      - Method-call detection is local to each chain — if a method like
        ``items`` is referenced without being called, it is still treated as
        a field for the unknown-agent check, but is filtered from field-
        precision checks via ``_DICT_METHOD_NAMES`` as a safety net.

    Args:
        template: A Jinja2 template string (may contain no template tags).

    Returns:
        A :class:`TemplateRefs` instance with flat and structured reference
        information. All fields are empty when the template has no
        recognizable references or contains a syntax error we cannot parse —
        semantic validation should not fail on malformed templates; render-
        time will raise the precise error.
    """
    empty = TemplateRefs(
        agent_refs=set(),
        workflow_inputs=set(),
        agent_output_fields={},
        group_member_fields={},
        group_error_refs=set(),
    )

    if not template or ("{{" not in template and "{%" not in template):
        return empty

    try:
        ast = _JINJA_ENV.parse(template)
    except jinja2.TemplateSyntaxError:
        return empty

    # ``meta.find_undeclared_variables`` runs Jinja2's compiler over the AST
    # and can raise ``TemplateAssertionError`` for semantic issues that
    # ``parse()`` accepts (e.g. duplicate ``{% block %}`` names). Validation
    # should not hard-fail on such templates — render-time will produce the
    # precise error if the workflow actually runs.
    try:
        undeclared = meta.find_undeclared_variables(ast)
    except jinja2.TemplateAssertionError:
        return empty

    # Pre-pass: identify Getattr nodes that are the callee of a Call so we can
    # treat ``a.output.items()`` as a method invocation rather than a field
    # access. Also identify Getattr nodes that are the ``.node`` of another
    # Getattr — those are inner links in a chain (e.g. ``a.output`` from
    # within ``a.output.bar``) and would otherwise emit spurious
    # whole-output references. Using ``id()`` for identity comparison is safe
    # within a single AST; we never store these IDs beyond this function.
    callee_ids: set[int] = set()
    for call in ast.find_all(nodes.Call):
        if isinstance(call.node, nodes.Getattr):
            callee_ids.add(id(call.node))
    inner_link_ids: set[int] = set()
    for ga in ast.find_all(nodes.Getattr):
        if isinstance(ga.node, nodes.Getattr):
            inner_link_ids.add(id(ga.node))

    # workflow.input.<name> chains; collected directly into the result.
    workflow_inputs: set[str] = set()
    # group.errors chains; collected directly into the result.
    group_error_refs: set[str] = set()
    # Output / outputs chains, accumulated as the structured maps directly.
    agent_output_fields: dict[str, set[str | None]] = {}
    group_member_fields: dict[tuple[str, str | None], set[str | None]] = {}
    agent_refs: set[str] = set()

    for node in ast.find_all(nodes.Getattr):
        is_callee = id(node) in callee_ids
        is_inner_link = id(node) in inner_link_ids
        # Only top-level Getattrs are the entry point for a chain. Inner-link
        # Getattrs are walked transitively when we process their enclosing
        # outer Getattr (or, if the outer is the callee of a Call, when we
        # process the callee itself).
        if is_inner_link and not is_callee:
            continue

        # Walk down the Getattr chain to its root Name, collecting attributes.
        attrs: list[str] = []
        cur: nodes.Node = node
        while isinstance(cur, nodes.Getattr):
            attrs.insert(0, cur.attr)
            cur = cur.node
        if not isinstance(cur, nodes.Name):
            continue
        # Skip names bound by an enclosing scope (loop var, macro param, set).
        if cur.name not in undeclared:
            continue

        # If this is a method call (e.g. ``a.output.items()``), the trailing
        # attribute is the method name, not a field. Trim it so the chain
        # reduces to the receiver — yielding a whole-output ref rather than
        # a spurious field ref to the method name.
        if is_callee and attrs:
            attrs = attrs[:-1]
            if not attrs:
                continue

        root = cur.name

        # workflow.input.<name>
        if root == "workflow" and len(attrs) >= 2 and attrs[0] == "input":
            workflow_inputs.add(attrs[1])
            continue

        # Other built-in namespaces and bare names are ignored.
        if root in _BUILTIN_NAMES or not attrs:
            continue

        kind = attrs[0]
        if kind not in _OUTPUT_ATTRS:
            continue

        # Errors are handled separately and never get field-precision treatment.
        if kind == "errors":
            group_error_refs.add(root)
            agent_refs.add(root)
            continue

        agent_refs.add(root)
        if kind == "output":
            # attrs is ["output"] or ["output", "<field>", ...]
            field: str | None = attrs[1] if len(attrs) >= 2 else None
            agent_output_fields.setdefault(root, set()).add(field)
        else:  # kind == "outputs"
            # attrs is ["outputs"] or ["outputs", "<member>", ...]
            if len(attrs) == 1:
                # Bare group.outputs — record under sentinel member=None.
                group_member_fields.setdefault((root, None), set()).add(None)
            else:
                member = attrs[1]
                field = attrs[2] if len(attrs) >= 3 else None
                group_member_fields.setdefault((root, member), set()).add(field)

    return TemplateRefs(
        agent_refs=agent_refs,
        workflow_inputs=workflow_inputs,
        agent_output_fields=agent_output_fields,
        group_member_fields=group_member_fields,
        group_error_refs=group_error_refs,
    )


def _extract_output_template_refs(output: dict[str, str]) -> set[str]:
    """Extract agent/group names referenced across all workflow output templates.

    Args:
        output: Dict of output field names to template expressions.

    Returns:
        Set of referenced agent/group names.
    """
    refs: set[str] = set()
    for template in output.values():
        refs.update(_extract_template_refs(template).agent_refs)
    return refs


def _name_on_path(name: str, path: list[str], config: WorkflowConfig) -> bool:
    """Check if an agent/group name appears on a given execution path.

    Checks both direct presence and membership in a parallel group on the path.
    Note: for-each inline agents are not checked here because users reference
    the group name (e.g., analyzers.outputs), not the inner agent name directly.

    Args:
        name: Agent or group name to check.
        path: List of node names representing an execution path.
        config: The WorkflowConfig for parallel group membership lookup.

    Returns:
        True if the name is on the path (directly or via parallel group).
    """
    if name in path:
        return True
    return any(pg.name in path and name in pg.agents for pg in config.parallel)


def _validate_output_path_coverage(config: WorkflowConfig) -> list[str]:
    """Validate that output template references are reachable on all paths.

    Emits warnings (not errors) for output template references to agents/groups
    that don't appear on every possible execution path from entry_point to a
    terminal node (``$end`` or a ``type: terminate`` step). Paths that end on a
    terminate step whose ``output_template`` is set are excluded from coverage
    because that step supplies its own final output dict and bypasses the
    workflow-level ``output:``.

    Args:
        config: The WorkflowConfig to validate.

    Returns:
        List of warning messages.
    """
    if not config.output:
        return []

    graph = _build_routing_graph(config)
    node_count = len(config.agents) + len(config.parallel) + len(config.for_each)
    max_depth = max(config.workflow.limits.max_iterations, node_count)
    terminate_names = _terminate_agent_names(config)
    paths = _enumerate_paths_to_end(
        config.workflow.entry_point,
        graph,
        max_depth,
        terminal_nodes=frozenset(terminate_names),
    )

    if not paths:
        return []

    # Drop paths that terminate via a `type: terminate` step whose
    # `output_template` overrides the workflow-level `output:`. Those paths do
    # not consume the workflow `output:` mapping and would produce spurious
    # "not reached" warnings.
    overriding_terminators = {
        a.name for a in config.agents if a.type == "terminate" and a.output_template is not None
    }
    paths = [p for p in paths if not p or p[-1] not in overriding_terminators]

    if not paths:
        return []

    refs = _extract_output_template_refs(config.output)
    if not refs:
        return []

    warnings: list[str] = []
    for ref in sorted(refs):
        missing_paths = [p for p in paths if not _name_on_path(ref, p, config)]
        if missing_paths:
            # Pick the shortest example path for the warning message
            missing_paths.sort(key=len)
            example = missing_paths[0]
            # If the path ended on a terminate step, show that explicitly;
            # otherwise append the implicit "$end" marker.
            tail = "$end" if not example or example[-1] not in terminate_names else ""
            display = example + ([tail] if tail else [])
            path_str = " \u2192 ".join(display)
            warnings.append(
                f"Output template references '{ref}' which may not run on all paths. "
                f"Example path where it is skipped: {path_str}. "
                f"Consider wrapping with {{% if {ref} is defined %}} to handle "
                f"cases where this agent/group does not execute."
            )

    return warnings


def _collect_template_strings(
    agent: AgentDef,
) -> list[tuple[str, str]]:
    """Collect all Jinja2 template strings from an agent definition.

    Returns:
        List of (source_label, template_string) tuples for error reporting.
    """
    templates: list[tuple[str, str]] = []

    if agent.prompt:
        templates.append((f"agent '{agent.name}' prompt", agent.prompt))
    if agent.system_prompt:
        templates.append((f"agent '{agent.name}' system_prompt", agent.system_prompt))
    if agent.command:
        templates.append((f"agent '{agent.name}' command", agent.command))
    for i, arg in enumerate(agent.args):
        templates.append((f"agent '{agent.name}' args[{i}]", arg))
    if agent.working_dir:
        templates.append((f"agent '{agent.name}' working_dir", agent.working_dir))

    # 'set' step bindings — value: single expression, values: named expressions.
    # Use getattr so duck-typed test fixtures without these attributes still
    # work (matches the input_mapping pattern below).
    value: str | None = getattr(agent, "value", None)
    if value is not None:
        templates.append((f"agent '{agent.name}' value", value))
    values: dict[str, str] | None = getattr(agent, "values", None)
    if values:
        for key, expr in values.items():
            templates.append((f"agent '{agent.name}' values.{key}", expr))

    # input_mapping is on AgentDef in main (added by #109 closing #101) but may not
    # exist on the schema in branches that haven't merged that yet. getattr keeps
    # this forward-compatible without coupling validate semantics to schema timing.
    input_mapping: dict[str, str] | None = getattr(agent, "input_mapping", None)
    if input_mapping:
        for key, expr in input_mapping.items():
            templates.append((f"agent '{agent.name}' input_mapping.{key}", expr))

    # Terminate steps: validate `reason` and `output_template` like other
    # Jinja2-rendered fields so bad refs fail at validate-time, not runtime.
    # Use a runtime-imported `AgentDef` isinstance check (NOT `getattr`) so a
    # future rename of `AgentDef.type` / `.reason` / `.output_template`
    # surfaces as an `AttributeError` here instead of silently skipping
    # template validation. Duck-typed agent objects (the only callers using
    # `SimpleNamespace`-style stubs are forward-compat tests like
    # `TestInputMappingTemplateCollection`) never set `type="terminate"`, so
    # they don't enter this branch and don't need the `getattr` fallback.
    from conductor.config.schema import AgentDef as _AgentDef

    if isinstance(agent, _AgentDef) and agent.type == "terminate":
        if agent.reason is not None:
            templates.append((f"agent '{agent.name}' reason", agent.reason))
        if agent.output_template:
            for key, expr in agent.output_template.items():
                templates.append((f"agent '{agent.name}' output_template.{key}", expr))

    return templates


# Maximum depth for recursive sub-workflow validation to prevent infinite loops.
_MAX_SUBWORKFLOW_VALIDATION_DEPTH = 10


def _validate_subworkflow_refs(
    config: WorkflowConfig,
    workflow_path: Path | None,
    _visited: frozenset[tuple[int, int]] | None = None,
    _depth: int = 0,
) -> tuple[list[str], list[str]]:
    """Validate all ``type: workflow`` agent references in *config*.

    For local paths, checks that the file exists. For registry references,
    fetches the workflow to the local cache and recursively validates the
    full composition tree. Cycle detection uses inode identity so that the
    same file referenced via different cases (on case-insensitive
    filesystems like macOS/Windows) or via symlinks resolves to the same
    canonical key.

    Args:
        config: The workflow configuration to validate.
        workflow_path: Path of the workflow file being validated (used as the
            base directory for relative sub-workflow paths).
        _visited: Set of already-visited canonical (st_dev, st_ino) tuples
            for cycle detection. Callers should leave this as ``None``; it is
            threaded through recursive calls.
        _depth: Current recursion depth (internal). When the depth reaches
            :data:`_MAX_SUBWORKFLOW_VALIDATION_DEPTH`, recursion stops and a
            warning is emitted so callers know the validation tree was
            truncated.

    Returns:
        Tuple of (error messages, warning messages).
    """
    if _visited is None:
        _visited = frozenset()

    errors: list[str] = []
    warnings: list[str] = []

    if _depth >= _MAX_SUBWORKFLOW_VALIDATION_DEPTH:
        warnings.append(
            f"Sub-workflow validation depth limit "
            f"({_MAX_SUBWORKFLOW_VALIDATION_DEPTH}) reached; "
            "deeper sub-workflows were not validated. "
            "Reduce nesting or check for unintended cycles."
        )
        return errors, warnings

    base_dir = workflow_path.resolve().parent if workflow_path is not None else Path.cwd()

    # Collect all (agent_name, workflow_ref, context_label) tuples to validate.
    candidates: list[tuple[str, str, str]] = []
    for agent in config.agents:
        if agent.type == "workflow" and agent.workflow:
            candidates.append((agent.name, agent.workflow, f"agent '{agent.name}'"))
    for fe in config.for_each:
        agent = fe.agent
        if agent.type == "workflow" and agent.workflow:
            candidates.append(
                (agent.name, agent.workflow, f"for_each group '{fe.name}' agent '{agent.name}'")
            )

    for _agent_name, workflow_ref, label in candidates:
        sub_path, ref_errors = _resolve_subworkflow_ref_for_validation(
            workflow_ref, label, base_dir
        )
        errors.extend(ref_errors)
        if sub_path is None:
            continue

        # Use inode identity (st_dev, st_ino) for cycle detection so that the
        # same file referenced via different cases (case-insensitive
        # filesystems) or different relative paths resolves to one key.
        try:
            stat = sub_path.stat()
            canonical: tuple[int, int] = (stat.st_dev, stat.st_ino)
        except OSError as exc:
            # Should be rare since _resolve_subworkflow_ref_for_validation
            # already returned a path it considered valid, but stat() can
            # still fail on some platforms (e.g. permission errors).
            errors.append(f"{label}: cannot stat sub-workflow file '{sub_path}': {exc}")
            continue

        if canonical in _visited:
            errors.append(
                f"{label}: circular sub-workflow reference detected "
                f"('{workflow_ref}' → '{sub_path}' is already in the validation chain)"
            )
            continue

        # Recursively validate the sub-workflow.
        try:
            from conductor.config.loader import load_config

            sub_config = load_config(sub_path)
        except Exception as exc:
            errors.append(f"{label}: failed to load sub-workflow '{sub_path}': {exc}")
            continue

        try:
            # Thread _visited and _depth through validate_workflow_config so
            # nested sub-workflow validation also gets cycle detection.
            sub_warnings = validate_workflow_config(
                sub_config,
                workflow_path=sub_path,
                _visited_subworkflows=_visited | {canonical},
                _subworkflow_depth=_depth + 1,
            )
            warnings.extend(f"{label} → sub-workflow '{sub_path.name}': {w}" for w in sub_warnings)
        except ConfigurationError as exc:
            errors.append(f"{label}: sub-workflow '{sub_path.name}' failed validation: {exc}")

    return errors, warnings


def _resolve_subworkflow_ref_for_validation(
    workflow_ref: str,
    label: str,
    base_dir: Path,
) -> tuple[Path | None, list[str]]:
    """Resolve a ``workflow:`` field value to a local path for validation.

    Mirrors the engine's ``_resolve_subworkflow_path`` but is synchronous and
    returns errors as a list rather than raising.

    Args:
        workflow_ref: The raw ``workflow:`` field value.
        label: Human-readable context for error messages.
        base_dir: Base directory for relative path resolution.

    Returns:
        Tuple of (resolved path or None on error, list of error strings).
    """
    from conductor.registry.cache import auto_fetch_relative_workflow, resolve_and_fetch
    from conductor.registry.errors import RegistryError
    from conductor.registry.resolver import resolve_ref

    errors: list[str] = []

    # Step 1: check for an existing file beside the parent workflow first.
    candidate = (base_dir / workflow_ref).resolve()
    if candidate.is_file():
        return candidate, errors

    # Step 1b: when the parent workflow lives inside a registry SHA cache,
    # try to auto-fetch a sibling workflow from the same registry. Mirrors
    # the engine's ``_resolve_subworkflow_path`` step 1b so that
    # ``conductor validate`` succeeds for the same cross-workflow refs
    # (e.g. ``../document-review/workflow.yaml``) that succeed at runtime.
    # Only attempts when the candidate looks like a file path (has
    # separators or a YAML extension) AND is not a registry ref
    # ('@' indicates named or ad-hoc registry syntax handled below).
    looks_like_file = "@" not in workflow_ref and (
        "/" in workflow_ref or "\\" in workflow_ref or candidate.suffix.lower() in {".yaml", ".yml"}
    )
    if looks_like_file:
        try:
            auto_fetched = auto_fetch_relative_workflow(candidate)
        except RegistryError as exc:
            errors.append(f"{label}: failed to auto-fetch sub-workflow '{workflow_ref}': {exc}")
            return None, errors
        if auto_fetched is not None and auto_fetched.is_file():
            return auto_fetched, errors

    try:
        resolved = resolve_ref(workflow_ref)
    except RegistryError as exc:
        errors.append(f"{label}: invalid sub-workflow reference '{workflow_ref}': {exc}")
        return None, errors

    if resolved.kind == "file":
        # File-path syntax but file does not exist.
        errors.append(f"{label}: sub-workflow file not found: '{candidate}'")
        return None, errors

    # Named registry or ad-hoc reference: fetch (uses cache; makes network
    # request on first access).
    try:
        sub_path = resolve_and_fetch(resolved)
    except RegistryError as exc:
        errors.append(f"{label}: failed to fetch sub-workflow '{workflow_ref}': {exc}")
        return None, errors

    return sub_path, errors


def _validate_template_references(
    config: WorkflowConfig,
    workflow_path: Path | None = None,
) -> tuple[list[str], list[str]]:
    """Validate Jinja2 template references across all agents and workflow output.

    Checks that:
    - ``{{ X.output.Y }}`` (and ``X.outputs``/``X.errors``) references resolve to a
      known agent, parallel group, or for-each group.
    - ``{{ workflow.input.X }}`` references resolve to a declared workflow input.
    - In explicit context mode, agents only reference inputs they have declared
      in their ``input:`` list (warning, not error).

    Uses Jinja2's AST so loop variables, ``{% set %}`` bindings, macro params, and
    string literals do not produce false positives.

    Args:
        config: The WorkflowConfig to validate.
        workflow_path: Optional path to the workflow file (currently unused;
            reserved for future ``!file`` cross-file scanning).

    Returns:
        Tuple of (error messages, warning messages).
    """
    del workflow_path  # reserved for future cross-file resolution

    errors: list[str] = []
    warnings: list[str] = []

    agent_names = {a.name for a in config.agents}
    parallel_names = {pg.name for pg in config.parallel}
    for_each_names = {fe.name for fe in config.for_each}
    all_names = agent_names | parallel_names | for_each_names
    workflow_input_names = set(config.workflow.input.keys())
    is_explicit = config.workflow.context.mode == "explicit"

    # Collect all agents including for-each inline agents.
    all_agents: list[tuple[AgentDef, set[str]]] = []
    for agent in config.agents:
        all_agents.append((agent, all_names))
    for fe in config.for_each:
        all_agents.append((fe.agent, all_names))

    for agent, valid_names in all_agents:
        templates = _collect_template_strings(agent)

        # Extract declared input references for explicit-mode advisory checks,
        # tracking the namespace (agent ``.output``, group ``.outputs``, group
        # ``.errors``) separately. The same declaration set cannot suppress
        # warnings for a different namespace — declaring ``pg.errors`` must
        # not silence warnings about ``pg.outputs.*`` references and
        # vice-versa, because the engine only populates the declared
        # namespace into the agent's ctx (see ``_add_parallel_group_input``).
        #
        # Field-precision tracking (Gap A): ``set[str | None]`` values mean:
        #   - ``None`` in the set => the whole namespace was declared
        #     (e.g. ``a.output`` or ``g.outputs`` or ``g.outputs.m``). Any
        #     field/member reference on that root is allowed at runtime.
        #   - One or more strings => only those specific fields were declared
        #     (e.g. ``a.output.foo``); referencing a different field will
        #     fail at runtime.
        declared_workflow_inputs: set[str] = set()
        declared_agent_output_fields: dict[str, set[str | None]] = {}
        # Per (group, member) — only populated for ``.outputs`` declarations.
        # Member is ``None`` for the bare-group form ``g.outputs``.
        declared_group_output_member_fields: dict[tuple[str, str | None], set[str | None]] = {}
        # Group names that have ANY ``.outputs`` declaration (whole-group,
        # whole-member, or specific-field). Used for the "undeclared outputs"
        # warning so we don't recompute the set per template iteration.
        declared_groups_with_outputs: set[str] = set()
        # Set of group names with errors declared. The engine copies the
        # whole errors dict regardless of ``.member`` or ``.field`` suffixes
        # (see ``_add_parallel_group_input`` errors branch), so no field-
        # precision tracking is needed for errors.
        declared_group_errors: set[str] = set()
        for ref in agent.input:
            match = INPUT_REF_PATTERN.match(ref.rstrip("?"))
            if not match:
                continue
            ref_agent = match.group("agent")
            if ref_agent:
                field = match.group("field")
                # field is None for bare ``a.output`` (whole output declared).
                declared_agent_output_fields.setdefault(ref_agent, set()).add(field)
            ref_parallel = match.group("parallel")
            if ref_parallel:
                pg_kind = match.group("pg_kind")
                if pg_kind == "outputs":
                    pg_agent = match.group("pg_agent")
                    pg_field = match.group("pg_field")
                    # pg_agent is None for bare ``g.outputs``;
                    # pg_field is None for ``g.outputs.member`` (whole member).
                    declared_group_output_member_fields.setdefault(
                        (ref_parallel, pg_agent), set()
                    ).add(pg_field)
                    declared_groups_with_outputs.add(ref_parallel)
                else:  # pg_kind == "errors"
                    declared_group_errors.add(ref_parallel)
            ref_input = match.group("input")
            if ref_input:
                declared_workflow_inputs.add(ref_input)

        for source, template in templates:
            refs = _extract_template_refs(template)

            # Explicit-mode exclusions:
            # - human_gate prompts render with the full accumulated context
            #   (engine uses ``WorkflowContext.get_for_template()`` which forces
            #   ``mode="accumulate"``), so they're never subject to
            #   explicit-mode warnings.
            # - script and workflow (sub-workflow) agents are excluded only for
            #   ``workflow.input`` references because the engine's
            #   ``_LOCAL_RENDER_AGENT_TYPES`` carve-out populates
            #   ``workflow.input`` for them regardless of context mode.
            #   Their ``agent.output`` references still require declaration —
            #   the engine raises ``KeyError`` via ``_add_explicit_input`` if
            #   an undeclared agent output is accessed.
            agent_output_warning_allowed = is_explicit and agent.type != "human_gate"

            # --- Agent-output references (``a.output[.field]``) ---
            for ref_root, ref_fields in refs.agent_output_fields.items():
                if ref_root not in valid_names:
                    errors.append(
                        f"{source} references unknown agent '{ref_root}'. "
                        f"Available: {', '.join(sorted(valid_names))}"
                    )
                    continue
                if agent_output_warning_allowed and ref_root not in declared_agent_output_fields:
                    warnings.append(
                        f"{source} references '{ref_root}.output' but "
                        f"agent '{agent.name}' does not declare '{ref_root}.output' "
                        f"in its input: list (explicit context mode)"
                    )
                    continue
                # Field-precision (Gap A): warn when the template references a
                # field that wasn't declared. Skip the check entirely when the
                # declaration was for the whole output (``None`` in set).
                if not agent_output_warning_allowed:
                    continue
                declared_fields = declared_agent_output_fields[ref_root]
                if None in declared_fields:
                    continue
                declared_field_names = sorted(f for f in declared_fields if f)
                declared_list = ", ".join(f"{ref_root}.output.{f}" for f in declared_field_names)
                for ref_field in ref_fields:
                    if ref_field is None:
                        # Bare ``ref_root.output`` reference but only specific
                        # fields were declared — at runtime the engine only
                        # copies the declared fields into ctx, so the
                        # whole-output access will only see a partial dict.
                        warnings.append(
                            f"{source} references the whole '{ref_root}.output' "
                            f"object but agent '{agent.name}' only declares "
                            f"specific fields ({', '.join(declared_field_names)}) "
                            f"in its input: list. Declare '{ref_root}.output' (without "
                            f"a field) to access the whole output (explicit context mode)"
                        )
                        continue
                    if ref_field in _DICT_METHOD_NAMES:
                        continue
                    if ref_field not in declared_fields:
                        warnings.append(
                            f"{source} references '{ref_root}.output.{ref_field}' but "
                            f"agent '{agent.name}' only declares "
                            f"{declared_list} "
                            f"in its input: list (explicit context mode)"
                        )

            # --- Group-output references (``g.outputs[.member[.field]]``) ---
            # Skip the field-precision check for for-each groups because the
            # engine's ``_add_parallel_group_input`` copies the whole member
            # dict for dict-keyed for-each groups regardless of the declared
            # ``.field`` suffix (see context.py:
            # ``elif is_for_each_dict or len(remaining_parts) == 2``), so
            # field-precision warnings would be false positives.
            for (group, member), ref_fields in refs.group_member_fields.items():
                if group not in valid_names:
                    errors.append(
                        f"{source} references unknown agent '{group}'. "
                        f"Available: {', '.join(sorted(valid_names))}"
                    )
                    continue
                if agent_output_warning_allowed and group not in declared_groups_with_outputs:
                    warnings.append(
                        f"{source} references '{group}.outputs' but "
                        f"agent '{agent.name}' does not declare '{group}.outputs' "
                        f"in its input: list (explicit context mode)"
                    )
                    continue
                if not agent_output_warning_allowed:
                    continue
                if member is None or group in for_each_names:
                    continue
                # Skip if the whole group's outputs are declared (bare
                # ``g.outputs`` covers all members).
                if declared_group_output_member_fields.get((group, None)) is not None:
                    continue
                declared_fields = declared_group_output_member_fields.get((group, member))
                if declared_fields is None or None in declared_fields:
                    # Either the member isn't declared at all (will be
                    # surfaced by the undeclared warning) or the whole
                    # member is declared (any field is OK).
                    continue
                declared_field_names = sorted(f for f in declared_fields if f)
                declared_list = ", ".join(
                    f"{group}.outputs.{member}.{f}" for f in declared_field_names
                )
                for ref_field in ref_fields:
                    if ref_field is None or ref_field in _DICT_METHOD_NAMES:
                        continue
                    if ref_field not in declared_fields:
                        warnings.append(
                            f"{source} references "
                            f"'{group}.outputs.{member}.{ref_field}' but "
                            f"agent '{agent.name}' only declares "
                            f"{declared_list} "
                            f"in its input: list (explicit context mode)"
                        )

            # --- Group-error references (``g.errors``) ---
            for group in refs.group_error_refs:
                if group not in valid_names:
                    errors.append(
                        f"{source} references unknown agent '{group}'. "
                        f"Available: {', '.join(sorted(valid_names))}"
                    )
                    continue
                if agent_output_warning_allowed and group not in declared_group_errors:
                    warnings.append(
                        f"{source} references '{group}.errors' but "
                        f"agent '{agent.name}' does not declare '{group}.errors' "
                        f"in its input: list (explicit context mode)"
                    )

            for input_name in refs.workflow_inputs:
                if workflow_input_names and input_name not in workflow_input_names:
                    # Only error when inputs ARE declared — workflows without
                    # input: blocks may use workflow.input conditionally.
                    errors.append(
                        f"{source} references unknown workflow input '{input_name}'. "
                        f"Declared inputs: {', '.join(sorted(workflow_input_names))}"
                    )
                elif (
                    is_explicit
                    and agent.type not in ("script", "set", "workflow", "human_gate", "wait")
                    and input_name not in declared_workflow_inputs
                ):
                    warnings.append(
                        f"{source} references 'workflow.input.{input_name}' but "
                        f"agent '{agent.name}' does not declare "
                        f"'workflow.input.{input_name}' in its input: list "
                        f"(explicit context mode)"
                    )

    # Check workflow output templates.
    if config.output:
        for field, template in config.output.items():
            refs = _extract_template_refs(template)
            for ref_name in refs.agent_refs:
                if ref_name not in all_names:
                    errors.append(
                        f"Workflow output '{field}' references unknown agent '{ref_name}'"
                    )
            for input_name in refs.workflow_inputs:
                if workflow_input_names and input_name not in workflow_input_names:
                    errors.append(
                        f"Workflow output '{field}' references unknown "
                        f"workflow input '{input_name}'"
                    )

    return errors, warnings


# ---------------------------------------------------------------------------
# Provider capability cross-checks (issue #241)
# ---------------------------------------------------------------------------

# Agent types that drive a provider. All other types (human_gate, script,
# set, terminate, wait, workflow) do not invoke a provider directly and are
# skipped by every capability check.
_LLM_AGENT_TYPES = frozenset({None, "agent"})


def _is_llm_agent(agent: AgentDef) -> bool:
    """True iff this agent invokes a provider (vs. human_gate, script, etc.)."""
    return agent.type in _LLM_AGENT_TYPES


def _resolved_provider_name(agent: AgentDef, default: str) -> str:
    """The provider name an agent will actually use at runtime.

    Honors the per-agent ``provider:`` override and falls back to the
    workflow-level default.
    """
    return agent.provider or default


def _validate_provider_capabilities(
    config: WorkflowConfig,
) -> tuple[list[str], list[str]]:
    """Cross-check workflow features against each provider's declared capabilities.

    Returns a ``(errors, warnings)`` tuple. Errors block ``conductor validate``;
    warnings print but don't fail. The matrix is documented in #241 and in
    the plan; the policy follows the rubber-duck design review:

    * Silently-dropped features (mcp_servers, tools allowlist,
      reasoning effort, structured output, max_session_seconds) → **error**.
    * Concurrency unsafety in a parallel group → **error**. Same in a
      ``for_each`` group ONLY when its ``max_concurrent > 1`` (a serial
      ``for_each`` is effectively sequential).
    * Experimental + ``structured_output: "prompt_injection"`` + declared
      ``output:`` schema → **warning** (works, may be flaky).
    * Stable providers with ``prompt_injection`` do NOT trigger the warning;
      they are assumed to have earned that behavior through tests and docs.

    Capabilities are resolved lazily without instantiating providers so this
    runs cleanly in environments without API keys / network.
    """
    errors: list[str] = []
    warnings: list[str] = []

    default_provider = config.workflow.runtime.provider.name
    workflow_mcp_servers = config.workflow.runtime.mcp_servers

    # Cache per provider name so we don't re-resolve for every agent.
    cache: dict[str, ProviderCapabilities] = {}

    def _caps_for(name: str) -> ProviderCapabilities | None:
        if name not in cache:
            try:
                cache[name] = get_capabilities(name)
            except (KeyError, AttributeError) as exc:
                # The provider is unknown to the resolver OR declares no
                # CAPABILITIES. Surface as an error so the user knows
                # capability cross-checks were skipped for this provider.
                errors.append(
                    f"Provider '{name}' has no declared ProviderCapabilities "
                    f"(see issue #241): {exc}"
                )
                cache[name] = None  # type: ignore[assignment]
        return cache.get(name)

    # ----- Workflow-level: MCP servers -----
    # An mcp_servers block applies only to provider-backed agents that
    # actually resolve to a provider lacking MCP support. If every LLM
    # agent overrides to an MCP-capable provider, the workflow-level
    # mcp_servers block is fine even when the default provider lacks MCP.
    if workflow_mcp_servers:
        agents_using_default = [
            a
            for a in config.agents
            if _is_llm_agent(a) and _resolved_provider_name(a, default_provider) == default_provider
        ]
        if agents_using_default:
            default_caps = _caps_for(default_provider)
            if default_caps is not None and not default_caps.mcp_tools:
                errors.append(
                    f"Workflow declares 'runtime.mcp_servers' "
                    f"({sorted(workflow_mcp_servers)!r}) but the default provider "
                    f"'{default_provider}' does not support MCP servers "
                    f"(capabilities.mcp_tools=False) and is used by agent(s): "
                    f"{sorted(a.name for a in agents_using_default)!r}. "
                    f"Remove mcp_servers, override these agents to a provider with "
                    f"MCP support, or use an MCP-capable default provider."
                )

    # Per-agent default reasoning effort (workflow-wide). Pulled outside
    # the per-agent loop because it applies to every LLM agent that does
    # NOT override ``reasoning.effort`` explicitly.
    runtime_default_effort = config.workflow.runtime.default_reasoning_effort

    # ----- Per-agent checks -----
    for agent in config.agents:
        if not _is_llm_agent(agent):
            continue

        provider_name = _resolved_provider_name(agent, default_provider)
        caps = _caps_for(provider_name)
        if caps is None:
            continue  # error already recorded by _caps_for

        # Per-agent override against workflow-level mcp_servers: if the
        # workflow declared mcp_servers but this agent's resolved provider
        # is different from the default, the override skips MCP entirely.
        if workflow_mcp_servers and provider_name != default_provider and not caps.mcp_tools:
            errors.append(
                f"Agent '{agent.name}' overrides provider to '{provider_name}', which "
                f"does not support the workflow's declared MCP servers "
                f"(capabilities.mcp_tools=False)."
            )

        # tools allowlist: any explicit non-empty list against a provider
        # that doesn't pass through is a security regression risk. An empty
        # list ("no tools") is fine — the provider may honor it as
        # "no tools" semantics. Only non-empty triggers the security concern.
        if agent.tools and not caps.workflow_tools_passthrough:
            errors.append(
                f"Agent '{agent.name}' declares tools={agent.tools!r} but provider "
                f"'{provider_name}' does not honor per-agent tool allowlists "
                f"(capabilities.workflow_tools_passthrough=False). Silently "
                f"granting different tools than declared is a security regression."
            )

        # reasoning.effort: validate per-agent override OR workflow-wide
        # default against the supported levels tuple. Per-agent override
        # takes precedence — if it's set, the default doesn't apply.
        effective_effort = (
            agent.reasoning.effort
            if (agent.reasoning is not None and agent.reasoning.effort is not None)
            else runtime_default_effort
        )
        if effective_effort is not None:
            requested = effective_effort
            supported = caps.reasoning_effort
            source = (
                "reasoning.effort"
                if (agent.reasoning is not None and agent.reasoning.effort is not None)
                else "runtime.default_reasoning_effort"
            )
            if supported is None:
                errors.append(
                    f"Agent '{agent.name}' resolves to {source}={requested!r} "
                    f"but provider '{provider_name}' does not support reasoning "
                    f"effort (capabilities.reasoning_effort=None)."
                )
            elif requested not in supported:
                errors.append(
                    f"Agent '{agent.name}' resolves to {source}={requested!r} "
                    f"but provider '{provider_name}' supports only {list(supported)!r}."
                )

        # Structured output: hard error when no support, warning when
        # experimental + prompt injection (stable prompt-injection providers
        # like Copilot are silent — they've earned the behavior).
        if agent.output:
            if caps.structured_output == "none":
                errors.append(
                    f"Agent '{agent.name}' declares an output schema but provider "
                    f"'{provider_name}' does not support structured output "
                    f"(capabilities.structured_output='none')."
                )
            elif caps.structured_output == "prompt_injection" and caps.is_experimental:
                warnings.append(
                    f"Agent '{agent.name}' declares an output schema; provider "
                    f"'{provider_name}' enforces it via prompt injection (may be "
                    f"flaky on edge cases)."
                )

        # max_session_seconds: silently ignoring an explicit timeout is a
        # safety/operational regression — caller is asking for a bound.
        if agent.max_session_seconds is not None and not caps.max_session_seconds:
            errors.append(
                f"Agent '{agent.name}' sets max_session_seconds={agent.max_session_seconds!r} "
                f"but provider '{provider_name}' does not enforce session timeouts "
                f"(capabilities.max_session_seconds=False)."
            )

    # ----- Concurrency safety in parallel / for_each groups -----
    agent_by_name = {a.name: a for a in config.agents}
    for pg in config.parallel:
        for member_name in pg.agents:
            member = agent_by_name.get(member_name)
            if member is None or not _is_llm_agent(member):
                continue
            member_provider = _resolved_provider_name(member, default_provider)
            member_caps = _caps_for(member_provider)
            if member_caps is None or member_caps.concurrent_safe:
                continue
            errors.append(
                f"Parallel group '{pg.name}' includes agent '{member_name}' which "
                f"uses provider '{member_provider}' (capabilities.concurrent_safe=False). "
                f"This provider is not safe to run in parallel."
            )

    for fe in config.for_each:
        # A serial for_each (max_concurrent == 1) does not actually run
        # concurrent provider instances, so concurrent_safe=False providers
        # are allowed there.
        if fe.max_concurrent <= 1:
            continue
        inline_agent = fe.agent
        if not _is_llm_agent(inline_agent):
            continue
        provider_name = _resolved_provider_name(inline_agent, default_provider)
        caps = _caps_for(provider_name)
        if caps is None or caps.concurrent_safe:
            continue
        errors.append(
            f"For-each group '{fe.name}' has max_concurrent={fe.max_concurrent} "
            f"and uses provider '{provider_name}' (capabilities.concurrent_safe=False). "
            f"Set max_concurrent: 1 to run serially, or choose a concurrent-safe provider."
        )

    return errors, warnings
