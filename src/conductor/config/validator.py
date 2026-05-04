"""Cross-field validators for workflow configuration.

This module provides additional validation beyond Pydantic schema validation,
including semantic checks for agent references, input dependencies, and
tool references.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import jinja2
from jinja2 import Environment, meta, nodes

from conductor.exceptions import ConfigurationError

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
    r"(?P<parallel>[a-zA-Z_][a-zA-Z0-9_]*)\.(?:outputs|errors)(?:\.(?P<pg_agent>[a-zA-Z_][a-zA-Z0-9_]*)(?:\.(?P<pg_field>[a-zA-Z_][a-zA-Z0-9_]*))?)?|"
    r"workflow\.input\.(?P<input>[a-zA-Z_][a-zA-Z0-9_]*)"
    r")(?P<optional>\?)?$"
)


def validate_workflow_config(
    config: WorkflowConfig,
    workflow_path: Path | None = None,
) -> list[str]:
    """Perform comprehensive validation of a workflow configuration.

    This function performs semantic validation beyond what Pydantic can check,
    including cross-field references, consistency checks, and Jinja2 template
    reference validation.

    Args:
        config: The WorkflowConfig to validate.
        workflow_path: Optional path to the workflow file (for !file resolution).

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

        # Validate tool references (skip for script-type agents, they don't use tools)
        if agent.tools is not None and agent.tools and agent.type != "script":
            tool_errors = _validate_tool_references(agent.name, agent.tools, set(config.tools))
            errors.extend(tool_errors)

    # Validate parallel groups
    if config.parallel:
        parallel_errors = _validate_parallel_groups(config)
        errors.extend(parallel_errors)

    # Validate for_each groups: reject script and workflow steps as inline agents
    for for_each_group in config.for_each:
        if for_each_group.agent.type == "script":
            errors.append(
                f"For-each group '{for_each_group.name}' uses a script step as its "
                "inline agent. Script steps cannot be used in for_each groups."
            )
        if for_each_group.agent.type == "workflow":
            errors.append(
                f"For-each group '{for_each_group.name}' uses a workflow step as its "
                "inline agent. Workflow steps cannot be used in for_each groups."
            )

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

            # Validate no workflow steps in parallel groups
            if agent.type == "workflow":
                errors.append(
                    f"Agent '{agent_name}' in parallel group '{pg.name}' is a workflow step. "
                    "Workflow steps cannot be used in parallel groups."
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


def _build_routing_graph(config: WorkflowConfig) -> dict[str, list[tuple[str, bool]]]:
    """Build adjacency list from workflow config for path analysis.

    Args:
        config: The WorkflowConfig to analyze.

    Returns:
        Dict mapping node names to list of (target, is_conditional) tuples.
    """
    graph: dict[str, list[tuple[str, bool]]] = {}
    for agent in config.agents:
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
) -> list[list[str]]:
    """Enumerate paths from start to $end via DFS, up to _MAX_ENUMERATED_PATHS.

    Args:
        start: Entry point node name.
        graph: Adjacency list from _build_routing_graph.
        max_depth: Maximum path depth (prevents infinite exploration).

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


def _extract_template_refs(template: str) -> tuple[set[str], set[str]]:
    """Extract agent/group and workflow-input references from a Jinja2 template.

    Uses Jinja2's own parser, so:
      - Loop variables are excluded: ``{% for x in y %}{{ x.output }}{% endfor %}``
        does not produce a spurious reference to ``x``.
      - ``{% set x = ... %}`` bindings and macro parameters are excluded.
      - String literals are excluded: ``{{ x | replace("foo.output", "y") }}``
        does not produce a reference to ``foo``.

    A name is reported as an output reference when it appears as the root of a
    Getattr chain whose first attribute is one of ``output``/``outputs``/``errors``
    (e.g. ``agent.output.field``, ``group.outputs.member``, ``group.errors``).

    A name is reported as a workflow-input reference when the chain matches
    ``workflow.input.<name>``.

    Built-in namespaces (``workflow``, ``context``, ``item``, ``_index``, ``_key``,
    ``loop``) and any name bound by a Jinja2 scope are filtered out.

    Args:
        template: A Jinja2 template string (may contain no template tags).

    Returns:
        Tuple of ``(agent_refs, workflow_input_refs)``. Both sets are empty when
        the template has no recognizable references or contains a syntax error
        we cannot parse — semantic validation should not fail on malformed
        templates; render-time will raise the precise error.
    """
    if not template or ("{{" not in template and "{%" not in template):
        return set(), set()

    try:
        ast = _JINJA_ENV.parse(template)
    except jinja2.TemplateSyntaxError:
        return set(), set()

    undeclared = meta.find_undeclared_variables(ast)
    agent_refs: set[str] = set()
    input_refs: set[str] = set()

    for node in ast.find_all(nodes.Getattr):
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

        root = cur.name
        if root == "workflow" and len(attrs) >= 2 and attrs[0] == "input":
            input_refs.add(attrs[1])
        elif attrs and attrs[0] in _OUTPUT_ATTRS and root not in _BUILTIN_NAMES:
            agent_refs.add(root)

    return agent_refs, input_refs


def _extract_output_template_refs(output: dict[str, str]) -> set[str]:
    """Extract agent/group names referenced across all workflow output templates.

    Args:
        output: Dict of output field names to template expressions.

    Returns:
        Set of referenced agent/group names.
    """
    refs: set[str] = set()
    for template in output.values():
        agents, _ = _extract_template_refs(template)
        refs.update(agents)
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
    that don't appear on every possible execution path from entry_point to $end.

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
    paths = _enumerate_paths_to_end(config.workflow.entry_point, graph, max_depth)

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
            path_str = " \u2192 ".join(example + ["$end"])
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

    # input_mapping is on AgentDef in main (added by #109 closing #101) but may not
    # exist on the schema in branches that haven't merged that yet. getattr keeps
    # this forward-compatible without coupling validate semantics to schema timing.
    input_mapping: dict[str, str] | None = getattr(agent, "input_mapping", None)
    if input_mapping:
        for key, expr in input_mapping.items():
            templates.append((f"agent '{agent.name}' input_mapping.{key}", expr))

    return templates


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

        # Extract declared input references for explicit-mode advisory checks.
        declared_agents: set[str] = set()
        declared_workflow_inputs: set[str] = set()
        for ref in agent.input:
            match = INPUT_REF_PATTERN.match(ref.rstrip("?"))
            if not match:
                continue
            ref_agent = match.group("agent")
            if ref_agent:
                declared_agents.add(ref_agent)
            ref_parallel = match.group("parallel")
            if ref_parallel:
                declared_agents.add(ref_parallel)
            ref_input = match.group("input")
            if ref_input:
                declared_workflow_inputs.add(ref_input)

        for source, template in templates:
            agent_refs, input_refs = _extract_template_refs(template)

            for ref_name in agent_refs:
                if ref_name not in valid_names:
                    errors.append(
                        f"{source} references unknown agent '{ref_name}'. "
                        f"Available: {', '.join(sorted(valid_names))}"
                    )
                elif (
                    is_explicit
                    and agent.type not in ("script", "workflow")
                    and ref_name not in declared_agents
                ):
                    warnings.append(
                        f"{source} references '{ref_name}.output' but "
                        f"agent '{agent.name}' does not declare '{ref_name}.output' "
                        f"in its input: list (explicit context mode)"
                    )

            for input_name in input_refs:
                if workflow_input_names and input_name not in workflow_input_names:
                    # Only error when inputs ARE declared — workflows without
                    # input: blocks may use workflow.input conditionally.
                    errors.append(
                        f"{source} references unknown workflow input '{input_name}'. "
                        f"Declared inputs: {', '.join(sorted(workflow_input_names))}"
                    )
                elif (
                    is_explicit
                    and agent.type not in ("script", "workflow")
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
            agent_refs, input_refs = _extract_template_refs(template)
            for ref_name in agent_refs:
                if ref_name not in all_names:
                    errors.append(
                        f"Workflow output '{field}' references unknown agent '{ref_name}'"
                    )
            for input_name in input_refs:
                if workflow_input_names and input_name not in workflow_input_names:
                    errors.append(
                        f"Workflow output '{field}' references unknown "
                        f"workflow input '{input_name}'"
                    )

    return errors, warnings
