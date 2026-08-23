"""Naming: slugify workflow identifiers into legal, unique MCP tool names
(FR3, DD10, E7-T2).

MCP ``2026-07-28`` recommends tool names be 1-128 characters drawn from
``A-Za-z0-9_-.`` and unique within a server; this module enforces all
three, applies the operator's optional ``--tool-prefix``, and — on
collision — qualifies **every** colliding tool with its source registry,
never only the "losing" one (DD10): a name that silently changes meaning
when an unrelated registry gains a same-named workflow is worse than one
that is consistently qualified from the start.

Naming source: ``WorkflowDef.name`` when known.

FR3 requires the published tool name be derived from the workflow's own
declared ``name:`` field — the same string ``config/validator.py
::slugify_workflow_name`` slugifies for ``conductor validate``'s per-file
report (E6) — so the two never disagree about what a workflow's tool name
is. ``ToolIdentity.display`` carries that declared name when the caller
actually parsed the workflow (tiers 2/3, and any ``--workflow-dir`` entry,
which always parses); ``ToolIdentity.workflow`` — the registry index key,
or a filename stem — is used only as a fallback, for a tier-1 (index-only)
entry that never parses the workflow at all, or a ``degraded`` entry with
no schema to read a name from.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from conductor.config.validator import slugify_workflow_name

# MCP `2026-07-28` recommends tool names 1-128 characters drawn from
# `A-Za-z0-9_-.`. Mirrors the bound `config/validator.py` enforces on a bare
# `workflow.name` (E6-T2); kept as its own constant here because this
# module bounds a different string — a *tool name*, possibly already
# qualified with a registry — not a bare workflow name.
TOOL_NAME_MIN_LENGTH = 1
TOOL_NAME_MAX_LENGTH = 128


@dataclass(frozen=True)
class ToolIdentity:
    """Identifies one workflow a tool name is generated for.

    ``registry``/``workflow`` are the *display* identifier — used for
    logging, the returned reverse map, and (on collision) the qualifier —
    while ``source`` is a unique-per-candidate discriminator that never
    appears in a name: two candidates can legitimately share the same
    display ``registry``/``workflow`` (e.g. two ``--workflow-dir``
    directories with the same basename and a same-named file in each) and
    must still be distinct identities, or one candidate silently
    overwrites the other. ``display``, when set, is the workflow's own
    declared ``WorkflowDef.name`` and is what naming actually slugifies
    (see the module docstring); ``workflow`` is the fallback used only
    when no declared name is known.
    """

    registry: str
    workflow: str
    source: str = ""
    display: str | None = None


@dataclass(frozen=True)
class NameCollision:
    """One base slug that more than one candidate produced, and how it was
    resolved. Logged at warning level by the caller, naming every involved
    registry (FR10)."""

    base_slug: str
    identities: tuple[ToolIdentity, ...]
    qualified_names: tuple[str, ...]


@dataclass(frozen=True)
class NamingResult:
    """The output of :func:`build_tool_names`."""

    names: dict[ToolIdentity, str]
    """Final tool name for every candidate that produced a legal name."""

    reverse: dict[str, ToolIdentity]
    """The ``tool name -> (registry, workflow)`` map the invocation layer
    needs to know what to launch (DD10)."""

    collisions: tuple[NameCollision, ...]
    """Every base slug shared by more than one candidate."""

    rejected: dict[ToolIdentity, str]
    """Candidates excluded because no legal tool name could be produced
    (their slug fell outside the 1-128 character bound), keyed by
    identity, valued by the human-readable reason. The catalogue builder
    logs these per FR10 rather than dropping them silently."""


def slugify(name: str) -> str:
    """Slugify a workflow identifier into the MCP tool-name charset.

    Delegates to :func:`conductor.config.validator.slugify_workflow_name`
    — the single definition of this rule, so a workflow's tool name here
    always matches what ``conductor validate`` reports for the same string
    (E6-T3's ``_report_mcp``).
    """
    return slugify_workflow_name(name)


def build_tool_names(
    identities: Sequence[ToolIdentity],
    *,
    tool_prefix: str | None = None,
) -> NamingResult:
    """Slugify, prefix, and qualify a set of candidate workflows into legal,
    unique tool names (FR3, DD10).

    Three passes:

    1. Compute every identity's base slug (its declared name when known,
       else its ``workflow`` fallback — see the module docstring —
       slugified, with no prefix or registry qualifier yet) and group
       identities that share one. A shared base slug is a collision.
    2. For a collision, qualify **every** colliding identity with its
       *slugified* registry name (``official_review_pr``,
       ``team_review_pr``), never only one of them — DD10 is explicit that
       qualifying only the "losing" entry would let an unrelated registry
       silently change what an existing name means. The registry name is
       slugified before use, since it is operator-authored, not
       necessarily already legal in the MCP tool-name charset (a
       ``--workflow-dir`` registry label is ``dir:<directory name>``,
       whose colon is illegal on its own). If two identities in the
       *same* registry still collide after that (registry-qualification
       cannot disambiguate them, since they would both qualify to the
       identical name), a numeric suffix is appended in a stable, sorted
       order.
    3. Apply ``tool_prefix`` (itself sanitized into the same charset)
       uniformly to every resulting name, then perform one final,
       deterministic, global allocation pass: any final name still shared
       by more than one identity — a qualified name coinciding with an
       unrelated candidate's own base slug — gets a numeric suffix, in a
       stable sorted order, so no name is ever silently reused across
       candidates (which would otherwise overwrite one candidate's slot
       in the reverse map with another's). Each final name is then
       re-validated against the legal 1-128-character MCP charset; a name
       that still falls outside it after qualification/prefixing is
       rejected rather than published.
    """
    base_slugs: dict[ToolIdentity, str] = {}
    rejected: dict[ToolIdentity, str] = {}
    for identity in identities:
        source = identity.display if identity.display is not None else identity.workflow
        slug = slugify(source)
        if not TOOL_NAME_MIN_LENGTH <= len(slug) <= TOOL_NAME_MAX_LENGTH:
            rejected[identity] = (
                f"workflow identifier {source!r} slugifies to {slug!r} "
                f"({len(slug)} characters), outside the legal "
                f"{TOOL_NAME_MIN_LENGTH}-{TOOL_NAME_MAX_LENGTH}-character MCP tool-name range"
            )
            continue
        base_slugs[identity] = slug

    groups: dict[str, list[ToolIdentity]] = {}
    for identity, slug in base_slugs.items():
        groups.setdefault(slug, []).append(identity)

    collisions: list[NameCollision] = []
    unprefixed: dict[ToolIdentity, str] = {}
    for slug, group in groups.items():
        if len(group) == 1:
            unprefixed[group[0]] = slug
            continue

        # DD10: qualify ALL colliding identities with their registry,
        # never only the loser. Sorted for deterministic output. The
        # registry qualifier is itself slugified (FR3/DD10) so an illegal
        # character in the registry label (e.g. the colon in a
        # `--workflow-dir` label's `dir:<name>`) never leaks into the
        # final tool name.
        ordered = sorted(group, key=lambda i: (i.registry, i.workflow))
        qualified: dict[ToolIdentity, str] = {
            identity: f"{slugify(identity.registry)}_{slug}" for identity in ordered
        }

        # Registry-qualification cannot disambiguate two workflows in the
        # SAME registry that slugify identically -- both would qualify to
        # the same name. Append a numeric suffix in the same stable order
        # so the server still never publishes a duplicate.
        seen: dict[str, int] = {}
        for identity in ordered:
            candidate = qualified[identity]
            seen[candidate] = seen.get(candidate, 0) + 1
            if seen[candidate] > 1:
                qualified[identity] = f"{candidate}_{seen[candidate]}"

        unprefixed.update(qualified)
        collisions.append(
            NameCollision(
                base_slug=slug,
                identities=tuple(ordered),
                qualified_names=tuple(qualified[identity] for identity in ordered),
            )
        )

    sanitized_prefix = slugify(tool_prefix) if tool_prefix else ""
    prefixed: dict[ToolIdentity, str] = {
        identity: f"{sanitized_prefix}_{name}" if sanitized_prefix else name
        for identity, name in unprefixed.items()
    }

    # FR3/DD10: one final, deterministic, global allocation pass. A
    # qualified/prefixed name computed above can still collide with an
    # unrelated candidate's own name (e.g. its base slug, or another
    # group's qualified name) -- this is the only point that has visibility
    # across every candidate at once, so it is where any remaining
    # duplicate must be caught and resolved, rather than left to silently
    # overwrite an earlier entry in the reverse map.
    names: dict[ToolIdentity, str] = {}
    reverse: dict[str, ToolIdentity] = {}
    for identity in sorted(prefixed, key=lambda i: (i.registry, i.workflow, i.source)):
        candidate_name = prefixed[identity]
        final_name = candidate_name
        suffix = 1
        while final_name in reverse:
            suffix += 1
            final_name = f"{candidate_name}_{suffix}"

        if not TOOL_NAME_MIN_LENGTH <= len(final_name) <= TOOL_NAME_MAX_LENGTH:
            rejected[identity] = (
                f"final tool name {final_name!r} ({len(final_name)} characters) falls "
                f"outside the legal {TOOL_NAME_MIN_LENGTH}-{TOOL_NAME_MAX_LENGTH}-character "
                "MCP tool-name range after prefixing/qualification"
            )
            continue

        names[identity] = final_name
        reverse[final_name] = identity

    return NamingResult(
        names=names,
        reverse=reverse,
        collisions=tuple(collisions),
        rejected=rejected,
    )
