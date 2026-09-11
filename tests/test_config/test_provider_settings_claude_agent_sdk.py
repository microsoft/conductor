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
