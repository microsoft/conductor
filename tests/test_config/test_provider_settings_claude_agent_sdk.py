"""Tests for ProviderSettings.auth_mode for claude-agent-sdk."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from conductor.config.schema import ProviderSettings


class TestAuthModeField:
    def test_defaults_to_auto_for_claude_agent_sdk(self) -> None:
        s = ProviderSettings(name="claude-agent-sdk")
        assert s.auth_mode == "auto"

    def test_auto_explicit(self) -> None:
        s = ProviderSettings(name="claude-agent-sdk", auth_mode="auto")
        assert s.auth_mode == "auto"

    def test_subscription_accepted(self) -> None:
        s = ProviderSettings(name="claude-agent-sdk", auth_mode="subscription")
        assert s.auth_mode == "subscription"

    def test_api_key_accepted(self) -> None:
        s = ProviderSettings(name="claude-agent-sdk", auth_mode="api_key")
        assert s.auth_mode == "api_key"

    def test_invalid_value_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ProviderSettings(name="claude-agent-sdk", auth_mode="invalid")  # type: ignore[arg-type]

    def test_auth_mode_rejected_for_copilot(self) -> None:
        with pytest.raises(ValidationError, match="auth_mode.*only supported"):
            ProviderSettings(name="copilot", auth_mode="auto")  # type: ignore[arg-type]

    def test_auth_mode_rejected_for_claude(self) -> None:
        with pytest.raises(ValidationError, match="auth_mode.*only supported"):
            ProviderSettings(name="claude", auth_mode="subscription")  # type: ignore[arg-type]

    def test_auth_mode_rejected_for_hermes(self) -> None:
        with pytest.raises(ValidationError, match="auth_mode.*only supported"):
            ProviderSettings(name="hermes", auth_mode="api_key")  # type: ignore[arg-type]

    def test_does_not_affect_has_custom_routing(self) -> None:
        for mode in ("auto", "subscription", "api_key"):
            s = ProviderSettings(name="claude-agent-sdk", auth_mode=mode)
            assert s.has_custom_routing() is False, (
                f"has_custom_routing() should be False for auth_mode={mode}"
            )

    def test_auto_mode_does_not_affect_has_structured_config(self) -> None:
        s = ProviderSettings(name="claude-agent-sdk", auth_mode="auto")
        assert s.has_structured_config() is False

    def test_subscription_mode_marks_structured_config(self) -> None:
        s = ProviderSettings(name="claude-agent-sdk", auth_mode="subscription")
        assert s.has_structured_config() is True

    def test_api_key_mode_marks_structured_config(self) -> None:
        s = ProviderSettings(name="claude-agent-sdk", auth_mode="api_key")
        assert s.has_structured_config() is True

    def test_api_key_still_rejected_in_yaml_for_claude_agent_sdk(self) -> None:
        with pytest.raises(ValidationError):
            ProviderSettings(name="claude-agent-sdk", api_key=SecretStr("mykey"))

    def test_base_url_still_rejected_for_claude_agent_sdk(self) -> None:
        with pytest.raises(ValidationError):
            ProviderSettings(name="claude-agent-sdk", base_url="http://localhost")

    def test_auth_token_still_rejected_for_claude_agent_sdk(self) -> None:
        with pytest.raises(ValidationError):
            ProviderSettings(name="claude-agent-sdk", auth_token=SecretStr("token"))


class TestNativeToolsField:
    """``runtime.provider.native_tools``: which built-ins an omitted ``tools:`` gets."""

    def test_defaults_to_none_for_claude_agent_sdk(self) -> None:
        assert ProviderSettings(name="claude-agent-sdk").native_tools == "none"

    @pytest.mark.parametrize("value", ["none", "claude_code"])
    def test_valid_values_accepted(self, value: str) -> None:
        s = ProviderSettings(name="claude-agent-sdk", native_tools=value)  # type: ignore[arg-type]
        assert s.native_tools == value

    @pytest.mark.parametrize(
        "value", ["", "None", "NONE", "claude-code", "all", "preset", "bypass", 0, True]
    )
    def test_invalid_values_rejected(self, value: object) -> None:
        with pytest.raises(ValidationError):
            ProviderSettings(name="claude-agent-sdk", native_tools=value)  # type: ignore[arg-type]

    def test_explicit_null_resolves_to_none(self) -> None:
        """``None`` is "absent", not a third state: it lands on the secure default."""
        s = ProviderSettings(name="claude-agent-sdk", native_tools=None)
        assert s.native_tools == "none"

    @pytest.mark.parametrize("name", ["copilot", "claude", "hermes", "openai"])
    @pytest.mark.parametrize("value", ["none", "claude_code"])
    def test_rejected_for_every_other_provider(self, name: str, value: str) -> None:
        with pytest.raises(ValidationError, match="native_tools.*only supported"):
            ProviderSettings(name=name, native_tools=value)  # type: ignore[arg-type]

    def test_rejected_for_aca(self) -> None:
        with pytest.raises(ValidationError, match="native_tools.*only supported"):
            ProviderSettings(
                name="aca",
                pool_endpoint="https://pool.example.com",
                native_tools="none",  # type: ignore[arg-type]
            )

    def test_other_providers_leave_it_unset(self) -> None:
        """The default is claude-agent-sdk's alone; nothing else acquires it."""
        assert ProviderSettings(name="copilot").native_tools is None

    def test_does_not_affect_custom_routing(self) -> None:
        for value in ("none", "claude_code"):
            s = ProviderSettings(name="claude-agent-sdk", native_tools=value)  # type: ignore[arg-type]
            assert s.has_custom_routing() is False

    def test_does_not_change_auth_mode(self) -> None:
        """Orthogonal to PR #522's credential selection."""
        s = ProviderSettings(name="claude-agent-sdk", native_tools="claude_code")
        assert s.auth_mode == "auto"


class TestNativeToolsSerialization:
    """The shorthand must stay a shorthand, and the opt-in must never collapse."""

    def test_default_keeps_the_string_shorthand(self) -> None:
        # The validator stamps "none" onto every claude-agent-sdk settings
        # object; counting that as structure would break the shorthand.
        assert ProviderSettings(name="claude-agent-sdk").model_dump() == "claude-agent-sdk"

    def test_explicit_none_keeps_the_string_shorthand(self) -> None:
        s = ProviderSettings(name="claude-agent-sdk", native_tools="none")
        assert s.has_structured_config() is False
        assert s.model_dump() == "claude-agent-sdk"

    def test_claude_code_forces_structured_serialization(self) -> None:
        s = ProviderSettings(name="claude-agent-sdk", native_tools="claude_code")
        assert s.has_structured_config() is True
        dumped = s.model_dump()
        assert isinstance(dumped, dict)
        assert dumped["native_tools"] == "claude_code"

    def test_claude_code_round_trips(self) -> None:
        """Collapsing the opt-in to a bare string would drop it on reload,
        silently downgrading the run to `none` — or, read the other way,
        the reason a resumed run would behave differently."""
        s = ProviderSettings(name="claude-agent-sdk", native_tools="claude_code")
        assert ProviderSettings.model_validate(s.model_dump()).native_tools == "claude_code"

    def test_old_serialized_config_without_the_field_resolves_to_none(self) -> None:
        """A checkpoint or config written before this field existed carries
        no ``native_tools``; it must land on the secure default, not the old
        implicit preset."""
        legacy = {"name": "claude-agent-sdk", "auth_mode": "subscription"}
        assert ProviderSettings.model_validate(legacy).native_tools == "none"


class TestNativeToolsFromYaml:
    """Through the real loader — the path a user's workflow file takes."""

    @staticmethod
    def _load(provider_block: str):
        from conductor.config.loader import load_config_string

        return load_config_string(
            f"""
workflow:
  name: native-tools
  entry_point: a
  runtime:
{provider_block}
agents:
  - name: a
    prompt: "hi"
    routes:
      - to: $end
"""
        )

    def test_string_shorthand_resolves_to_none(self) -> None:
        config = self._load("    provider: claude-agent-sdk")
        assert config.workflow.runtime.provider.native_tools == "none"

    @pytest.mark.parametrize("literal", ["null", "~"])
    def test_yaml_null_means_omitted(self, literal: str) -> None:
        config = self._load(
            f"    provider:\n      name: claude-agent-sdk\n      native_tools: {literal}"
        )
        assert config.workflow.runtime.provider.native_tools == "none"

    def test_claude_code_opt_in(self) -> None:
        config = self._load(
            "    provider:\n      name: claude-agent-sdk\n      native_tools: claude_code"
        )
        assert config.workflow.runtime.provider.native_tools == "claude_code"

    def test_rejected_on_another_provider_at_load(self) -> None:
        """Enforced by the schema, which ``conductor run`` does go through —
        unlike ``config/validator.py``."""
        with pytest.raises(Exception, match="native_tools"):
            self._load("    provider:\n      name: copilot\n      native_tools: none")
