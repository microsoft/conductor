"""Tests for ProviderCapabilities schema + lazy resolver (#241)."""

from __future__ import annotations

import pytest

from conductor.providers.capabilities import (
    ProviderCapabilities,
    get_capabilities,
    known_provider_names,
)


def _stable_capabilities(**overrides: object) -> ProviderCapabilities:
    """Build a fully-stable capability descriptor; tests override specific fields."""
    base: dict[str, object] = {
        "tier": "stable",
        "mcp_tools": True,
        "workflow_tools_passthrough": True,
        "streaming_events": True,
        "agent_reasoning_events": True,
        "reasoning_effort": ("low", "medium", "high", "xhigh"),
        "structured_output": "native",
        "interrupt": True,
        "max_session_seconds": True,
        "checkpoint_resume": True,
        "usage_tracking": True,
        "concurrent_safe": True,
        "upstream_pin": None,
        "maintainer": None,
    }
    base.update(overrides)
    return ProviderCapabilities(**base)  # type: ignore[arg-type]


class TestSchemaValidation:
    def test_construct_stable_descriptor(self) -> None:
        caps = _stable_capabilities()
        assert caps.tier == "stable"
        assert caps.is_experimental is False
        assert caps.declared_limitations() == []

    def test_construct_experimental_descriptor(self) -> None:
        caps = _stable_capabilities(
            tier="experimental",
            mcp_tools=False,
            reasoning_effort=None,
            structured_output="prompt_injection",
            checkpoint_resume=False,
            upstream_pin="claude-agent-sdk>=0.1.0",
            maintainer="@external (best-effort)",
        )
        assert caps.is_experimental is True
        assert caps.upstream_pin == "claude-agent-sdk>=0.1.0"

    def test_descriptor_is_frozen(self) -> None:
        """ProviderCapabilities is immutable to prevent runtime tampering."""
        caps = _stable_capabilities()
        with pytest.raises((TypeError, AttributeError, ValueError)):
            caps.tier = "experimental"  # type: ignore[misc]

    def test_extra_fields_rejected(self) -> None:
        """extra='forbid' catches typos in capability declarations."""
        with pytest.raises(ValueError, match="extra"):
            ProviderCapabilities(
                tier="stable",
                mcp_tools=True,
                workflow_tools_passthrough=True,
                streaming_events=True,
                agent_reasoning_events=True,
                reasoning_effort=("low",),
                structured_output="native",
                interrupt=True,
                max_session_seconds=True,
                checkpoint_resume=True,
                usage_tracking=True,
                concurrent_safe=True,
                unknown_capability=True,  # type: ignore[call-arg]
            )

    def test_invalid_tier_rejected(self) -> None:
        with pytest.raises(ValueError):
            _stable_capabilities(tier="alpha")

    def test_invalid_reasoning_level_rejected(self) -> None:
        with pytest.raises(ValueError):
            _stable_capabilities(reasoning_effort=("ultra",))

    def test_empty_reasoning_effort_tuple_rejected(self) -> None:
        """Empty tuple is meaningless — None says 'no support', tuple says 'these levels'.

        Without this validator, an empty tuple silently passed every per-level
        membership check (``"high" not in ()`` always True), making the
        validator fire spurious errors for every workflow.
        """
        with pytest.raises(ValueError, match="meaningless"):
            _stable_capabilities(reasoning_effort=())

    def test_invalid_structured_output_mode_rejected(self) -> None:
        with pytest.raises(ValueError):
            _stable_capabilities(structured_output="json_mode")


class TestDeclaredLimitations:
    """Auto-generated limitations line for the experimental banner."""

    def test_fully_stable_has_no_limitations(self) -> None:
        assert _stable_capabilities().declared_limitations() == []

    def test_each_false_flag_produces_a_limitation(self) -> None:
        caps = _stable_capabilities(
            tier="experimental",
            mcp_tools=False,
            workflow_tools_passthrough=False,
            streaming_events=False,
            agent_reasoning_events=False,
            reasoning_effort=None,
            structured_output="none",
            interrupt=False,
            max_session_seconds=False,
            checkpoint_resume=False,
            usage_tracking=False,
            concurrent_safe=False,
        )
        lims = caps.declared_limitations()
        # Every "off" capability shows up in the human-readable list.
        assert "no MCP servers" in lims
        assert "no per-agent tools allowlist" in lims
        assert "no streaming events" in lims
        assert "no reasoning events" in lims
        assert "reasoning_effort ignored" in lims
        assert "no structured output" in lims
        assert "no mid-stream interrupt" in lims
        assert "max_session_seconds ignored" in lims
        assert "no checkpoint resume" in lims
        assert "no usage tracking" in lims
        assert "not safe to run in parallel" in lims

    def test_prompt_injection_structured_output_listed_as_limitation(self) -> None:
        caps = _stable_capabilities(
            tier="experimental",
            structured_output="prompt_injection",
        )
        assert "structured output via prompt injection" in caps.declared_limitations()


class TestResolver:
    """get_capabilities reads CAPABILITIES from each provider class without instantiating."""

    def test_known_provider_names_listed(self) -> None:
        names = known_provider_names()
        assert "copilot" in names
        assert "claude" in names
        assert "claude-agent-sdk" in names
        assert "codex" in names

    def test_unknown_provider_raises_keyerror(self) -> None:
        with pytest.raises(KeyError, match="Unknown provider"):
            get_capabilities("nonexistent-provider")

    @pytest.mark.parametrize("provider_name", ["copilot", "claude", "claude-agent-sdk", "codex"])
    def test_every_production_provider_has_capabilities(self, provider_name: str) -> None:
        """Hard requirement: every provider in the registry declares CAPABILITIES.

        If this test fails, a provider is missing its class-level
        ``CAPABILITIES: ProviderCapabilities`` attribute — see #241.
        """
        # Skip when the optional extra is not installed (e.g. CI install-scripts job).
        if provider_name == "claude-agent-sdk":
            pytest.importorskip("claude_agent_sdk")

        caps = get_capabilities(provider_name)
        assert isinstance(caps, ProviderCapabilities)

    def test_resolver_does_not_instantiate_provider(self) -> None:
        """The validator runs without API keys, so resolution MUST be class-only.

        Verified by ensuring ``get_capabilities`` does not invoke any
        provider's ``__init__`` — if it did, providers that raise on
        missing credentials (e.g. ClaudeProvider) would break ``validate``.
        """
        from unittest.mock import patch

        with patch(
            "conductor.providers.copilot.CopilotProvider.__init__",
            side_effect=AssertionError("__init__ called by resolver"),
        ):
            caps = get_capabilities("copilot")
            assert isinstance(caps, ProviderCapabilities)


class TestSubclassEnforcement:
    """`__init_subclass__` enforces CAPABILITIES at import time (#241 type hardening)."""

    def test_subclass_without_capabilities_raises_at_definition(self) -> None:
        """A non-abstract subclass that forgets CAPABILITIES fails at class creation."""
        from conductor.providers.base import AgentProvider

        with pytest.raises(TypeError, match="must declare a class-level CAPABILITIES"):

            class _Broken(AgentProvider):  # type: ignore[misc]
                async def execute(self, *a, **kw):
                    raise NotImplementedError

                async def validate_connection(self) -> bool:
                    return False

                async def close(self) -> None:
                    pass

    def test_subclass_with_wrong_type_raises(self) -> None:
        """CAPABILITIES set to something other than ProviderCapabilities is rejected."""
        from conductor.providers.base import AgentProvider

        with pytest.raises(TypeError, match="must declare a class-level CAPABILITIES"):

            class _WrongType(AgentProvider):  # type: ignore[misc]
                CAPABILITIES = "not a capability descriptor"  # type: ignore[assignment]

                async def execute(self, *a, **kw):
                    raise NotImplementedError

                async def validate_connection(self) -> bool:
                    return False

                async def close(self) -> None:
                    pass

    def test_abstract_subclass_opt_out(self) -> None:
        """Test fakes can opt out of the CAPABILITIES requirement with abstract=True."""
        from conductor.providers.base import AgentProvider

        class _Fake(AgentProvider, abstract=True):
            async def execute(self, *a, **kw):
                raise NotImplementedError

            async def validate_connection(self) -> bool:
                return False

            async def close(self) -> None:
                pass

        # No exception — abstract=True bypasses the check.
        assert _Fake.CAPABILITIES is None
