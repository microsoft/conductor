"""Copilot native model-routing preferences: Copilot Fusion and Auto tiers.

The Copilot runtime owns model routing. ``model: fusion`` selects Copilot
Fusion and ``fusion_tier`` (``standard`` / ``high`` / ``max``) picks its tier;
``model: auto`` selects Auto routing and ``auto_tier`` picks a routing
preference. The SDK forwards both as ``capi`` session options on create and
resume. How a Fusion tier maps to Auto routing preferences is runtime-owned
and deliberately not duplicated here.

This is a leaf module (it depends only on :mod:`conductor.templating`), so the
schema, validator, executor, providers and the remote runner can share one
definition of the vocabulary, precedence and compatibility rules without an
import cycle or an SDK import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, get_args

from conductor.templating import is_jinja_template

FusionTier = Literal["standard", "high", "max"]
"""Copilot Fusion tiers, matching the CLI's ``--fusion-tier``."""

FUSION_TIERS: Final[tuple[str, ...]] = get_args(FusionTier)

FUSION_MODEL: Final = "fusion"
AUTO_MODEL: Final = "auto"

DEPRECATED_FUSION_MODELS: Final[frozenset[str]] = frozenset({"hydrafusion", "hydrafusion-max"})
"""Model identifiers that Fusion replaces; still forwarded unchanged as models."""

ROUTING_SELECTOR_MODELS: Final[frozenset[str]] = frozenset(
    {FUSION_MODEL, AUTO_MODEL, *DEPRECATED_FUSION_MODELS}
)
"""Model names that ask the runtime to route rather than naming one model."""

COMPOSITE_ROUTING_MODELS: Final[frozenset[str]] = frozenset(
    {FUSION_MODEL, *DEPRECATED_FUSION_MODELS}
)
"""Selectors whose turns may combine several constituent models."""


class RoutingPreferenceError(ValueError):
    """A configured routing preference cannot be honored as declared."""


def normalize_model(model: str | None) -> str | None:
    """Return the case-insensitive comparison form of a model name."""
    return model.strip().lower() if model is not None else None


def is_fusion_model(model: str | None) -> bool:
    """Whether ``model`` selects Copilot Fusion (``fusion``, case-insensitive)."""
    return normalize_model(model) == FUSION_MODEL


def is_auto_model(model: str | None) -> bool:
    """Whether ``model`` selects Auto routing (``auto``, case-insensitive)."""
    return normalize_model(model) == AUTO_MODEL


def is_routing_selector(model: str | None) -> bool:
    """Whether ``model`` names a runtime routing selector rather than one model."""
    return normalize_model(model) in ROUTING_SELECTOR_MODELS


def is_composite_selector(model: str | None) -> bool:
    """Whether ``model`` selects a router that can mix constituent models in one turn."""
    return normalize_model(model) in COMPOSITE_ROUTING_MODELS


def validate_fusion_tier(value: str, *, field_name: str = "fusion_tier") -> str:
    """Return ``value`` when it is a Fusion tier literal, else raise."""
    if value not in FUSION_TIERS:
        raise RoutingPreferenceError(
            f"{field_name} must be one of {list(FUSION_TIERS)} (got {value!r})"
        )
    return value


def validate_auto_tier(value: str, *, field_name: str = "auto_tier") -> str:
    """Return the stripped Auto tier identifier, rejecting blank values.

    Auto tiers are extensible runtime identifiers, not a closed enumeration,
    so only blankness is rejected here; the runtime validates availability.
    """
    stripped = value.strip()
    if not stripped:
        raise RoutingPreferenceError(f"{field_name} must be a nonblank identifier")
    return stripped


@dataclass(frozen=True)
class RoutingPreference:
    """The effective routing preference for one Copilot session.

    At most one of ``fusion_tier`` and ``auto_tier`` is set. ``source`` records
    whether it came from the agent or from a runtime default, for messages.
    """

    fusion_tier: str | None = None
    auto_tier: str | None = None
    source: Literal["agent", "default"] | None = None

    def __bool__(self) -> bool:
        return self.fusion_tier is not None or self.auto_tier is not None

    @property
    def is_fusion(self) -> bool:
        """Whether this preference requests a Copilot Fusion tier."""
        return self.fusion_tier is not None

    def to_capi(self) -> dict[str, str] | None:
        """Build the SDK's ``capi`` session options, or ``None`` to send nothing."""
        if self.fusion_tier is not None:
            return {"fusion_tier": self.fusion_tier}
        if self.auto_tier is not None:
            return {"auto_tier": self.auto_tier}
        return None


def check_preference_compatibility(
    *,
    model: str | None,
    fusion_tier: str | None,
    auto_tier: str | None,
    subject: str,
) -> None:
    """Raise when an explicitly configured preference cannot apply to ``model``.

    ``fusion_tier`` requires ``model: fusion`` or ``model: auto``; ``auto_tier``
    requires ``model: auto``. The two are mutually exclusive.

    Args:
        model: Effective model name (``None`` when unknown).
        fusion_tier: Configured Fusion tier, if any.
        auto_tier: Configured Auto tier, if any.
        subject: Who configured the preference, for the error message.

    Raises:
        RoutingPreferenceError: On any incompatible combination.
    """
    if fusion_tier is not None and auto_tier is not None:
        raise RoutingPreferenceError(
            f"{subject} sets both fusion_tier and auto_tier; they are mutually exclusive"
        )
    described = f"its model is {model!r}" if model is not None else "it has no model set"
    if fusion_tier is not None and not (is_fusion_model(model) or is_auto_model(model)):
        raise RoutingPreferenceError(
            f"{subject} sets fusion_tier={fusion_tier!r} but {described}; "
            f"fusion_tier requires model '{FUSION_MODEL}' (or '{AUTO_MODEL}')"
        )
    if auto_tier is not None and not is_auto_model(model):
        raise RoutingPreferenceError(
            f"{subject} sets auto_tier={auto_tier!r} but {described}; "
            f"auto_tier requires model '{AUTO_MODEL}'"
        )


def ensure_rendered(value: str | None, field_name: str) -> None:
    """Refuse a ``{{ ... }}`` template that reached a provider unrendered."""
    if value is not None and is_jinja_template(value):
        raise RoutingPreferenceError(f"{field_name} reached the provider unresolved: {value!r}")


def resolve_routing_preference(
    *,
    model: str | None,
    agent_fusion_tier: str | None,
    agent_auto_tier: str | None,
    default_fusion_tier: str | None,
    default_auto_tier: str | None,
    subject: str,
) -> RoutingPreference:
    """Resolve the effective routing preference for one session.

    An agent preference replaces the runtime defaults as a unit and must be
    compatible with ``model``. A runtime default applies only to a compatible
    routed model: ``default_fusion_tier`` to ``model: fusion`` and
    ``default_auto_tier`` to ``model: auto``. Everything else resolves to an
    empty preference, which sends no ``capi`` value and so preserves runtime
    defaults and persisted resume preferences.

    Raises:
        RoutingPreferenceError: When an agent preference is incompatible, both
            agent preferences are set, or a template reached this point.
    """
    for value, name in (
        (agent_fusion_tier, "fusion_tier"),
        (agent_auto_tier, "auto_tier"),
        (default_fusion_tier, "default_fusion_tier"),
        (default_auto_tier, "default_auto_tier"),
    ):
        ensure_rendered(value, name)
    if agent_fusion_tier is not None or agent_auto_tier is not None:
        check_preference_compatibility(
            model=model, fusion_tier=agent_fusion_tier, auto_tier=agent_auto_tier, subject=subject
        )
        if agent_fusion_tier is not None:
            return RoutingPreference(
                fusion_tier=validate_fusion_tier(agent_fusion_tier), source="agent"
            )
        assert agent_auto_tier is not None
        return RoutingPreference(auto_tier=validate_auto_tier(agent_auto_tier), source="agent")
    if default_fusion_tier is not None and is_fusion_model(model):
        return RoutingPreference(
            fusion_tier=validate_fusion_tier(default_fusion_tier, field_name="default_fusion_tier"),
            source="default",
        )
    if default_auto_tier is not None and is_auto_model(model):
        return RoutingPreference(
            auto_tier=validate_auto_tier(default_auto_tier, field_name="default_auto_tier"),
            source="default",
        )
    return RoutingPreference()
