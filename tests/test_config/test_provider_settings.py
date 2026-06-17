"""Tests for ``ProviderSettings`` and structured ``runtime.provider`` config.

Covers issue #136: structured provider configuration that lets the Copilot
SDK be pointed at OpenAI-compatible / Azure / Anthropic endpoints (Ollama,
vLLM, LM Studio, Azure OpenAI, etc.).
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from conductor.config.schema import (
    AzureProviderOptions,
    ProviderSettings,
    RuntimeConfig,
)


class TestProviderSettingsCoercion:
    """``runtime.provider`` accepts both string shorthand and object form."""

    def test_string_shorthand_coerces_to_provider_settings(self) -> None:
        rc = RuntimeConfig.model_validate({"provider": "copilot"})
        assert isinstance(rc.provider, ProviderSettings)
        assert rc.provider.name == "copilot"
        assert not rc.provider.has_custom_routing()

    def test_object_form(self) -> None:
        rc = RuntimeConfig.model_validate(
            {
                "provider": {
                    "name": "copilot",
                    "type": "openai",
                    "base_url": "http://localhost:11434/v1",
                    "api_key": "sk-xxx",
                    "wire_api": "completions",
                }
            }
        )
        assert rc.provider.name == "copilot"
        assert rc.provider.type == "openai"
        assert rc.provider.wire_api == "completions"
        assert rc.provider.base_url == "http://localhost:11434/v1"
        assert isinstance(rc.provider.api_key, SecretStr)
        assert rc.provider.api_key.get_secret_value() == "sk-xxx"
        assert rc.provider.has_custom_routing()

    def test_default_runtime_has_default_provider(self) -> None:
        rc = RuntimeConfig()
        assert rc.provider.name == "copilot"
        assert not rc.provider.has_custom_routing()

    def test_reassignment_with_string_is_validated(self) -> None:
        """``validate_assignment=True`` makes string reassignment work."""
        rc = RuntimeConfig.model_validate(
            {"provider": {"name": "copilot", "base_url": "http://x/v1"}}
        )
        assert rc.provider.has_custom_routing()
        rc.provider = "copilot"  # type: ignore[assignment]
        assert isinstance(rc.provider, ProviderSettings)
        assert rc.provider.name == "copilot"
        assert not rc.provider.has_custom_routing(), (
            "string reassignment must reset structured fields"
        )


class TestProviderSettingsValidation:
    """Cross-field validators reject incompatible combinations."""

    def test_non_copilot_with_copilot_only_field_rejected(self) -> None:
        with pytest.raises(ValidationError, match="only supported when name='copilot'"):
            ProviderSettings(name="claude", type="anthropic")

    def test_non_copilot_with_base_url_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not yet implemented"):
            ProviderSettings(name="openai-agents", base_url="http://some-proxy/v1")

    def test_claude_with_base_url_accepted(self) -> None:
        s = ProviderSettings(name="claude", base_url="https://my-gateway.example.com/api/v1")
        assert s.base_url == "https://my-gateway.example.com/api/v1"

    def test_claude_with_auth_token_accepted(self) -> None:
        s = ProviderSettings(name="claude", auth_token="dapi-abc123")
        assert s.auth_token is not None
        assert s.auth_token.get_secret_value() == "dapi-abc123"

    def test_claude_with_base_url_and_auth_token_accepted(self) -> None:
        s = ProviderSettings(
            name="claude",
            base_url="https://my-gateway.example.com/api/v1",
            auth_token="dapi-abc123",
        )
        assert s.base_url == "https://my-gateway.example.com/api/v1"
        assert s.auth_token.get_secret_value() == "dapi-abc123"

    def test_auth_token_on_non_claude_rejected(self) -> None:
        with pytest.raises(ValidationError, match="only supported when name='claude'"):
            ProviderSettings(name="copilot", auth_token="some-token", base_url="http://x/v1")

    def test_azure_options_require_azure_type(self) -> None:
        with pytest.raises(ValidationError, match="require type='azure'"):
            ProviderSettings(
                name="copilot",
                type="openai",
                azure=AzureProviderOptions(api_version="2024-10-21"),
            )

    def test_azure_with_azure_type_accepted(self) -> None:
        s = ProviderSettings(
            name="copilot",
            type="azure",
            base_url="https://x.openai.azure.com",
            azure=AzureProviderOptions(api_version="2024-10-21"),
        )
        assert s.azure is not None
        assert s.azure.api_version == "2024-10-21"

    def test_invalid_type_literal_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ProviderSettings(name="copilot", type="ollama")  # type: ignore[arg-type]

    def test_invalid_wire_api_literal_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ProviderSettings(name="copilot", wire_api="grpc")  # type: ignore[arg-type]

    def test_invalid_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ProviderSettings(name="ollama")  # type: ignore[arg-type]

    def test_anchorless_type_rejected(self) -> None:
        """``type`` alone (no endpoint anchor) is rejected — it cannot
        produce a usable SDK provider config."""
        with pytest.raises(ValidationError, match="cannot stand alone"):
            ProviderSettings(name="copilot", type="openai")

    def test_anchorless_wire_api_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cannot stand alone"):
            ProviderSettings(name="copilot", wire_api="completions")

    def test_anchorless_headers_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cannot stand alone"):
            ProviderSettings(name="copilot", headers={"X-Foo": "1"})

    def test_empty_headers_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least one entry"):
            ProviderSettings(name="copilot", base_url="http://x/v1", headers={})

    def test_empty_api_key_rejected(self) -> None:
        """Empty SecretStr would activate custom routing but resolve to
        falsy in the resolver — fail loudly at config time instead."""
        with pytest.raises(ValidationError, match="'api_key' is empty"):
            ProviderSettings(name="copilot", base_url="http://x/v1", api_key="")

    def test_empty_bearer_token_rejected(self) -> None:
        with pytest.raises(ValidationError, match="'bearer_token' is empty"):
            ProviderSettings(name="copilot", base_url="http://x/v1", bearer_token="")

    def test_empty_azure_block_rejected(self) -> None:
        """``azure: {}`` (or ``azure.api_version: null``) activates custom
        routing but would be silently dropped at the SDK boundary."""
        with pytest.raises(ValidationError, match="'azure' block is empty"):
            ProviderSettings(
                name="copilot",
                type="azure",
                base_url="https://x.openai.azure.com",
                azure=AzureProviderOptions(),
            )


class TestProviderSettingsSerialization:
    """Round-trip serialization preserves backward compatibility."""

    def test_default_serializes_as_bare_string(self) -> None:
        """``provider: copilot`` must round-trip as a bare string, not
        ``{name: copilot}``, so existing tooling that reads serialized
        workflow configs keeps working."""
        rc = RuntimeConfig()
        dumped = rc.model_dump(mode="json", exclude_none=True)
        assert dumped["provider"] == "copilot"

    def test_custom_routing_serializes_as_object(self) -> None:
        rc = RuntimeConfig.model_validate(
            {"provider": {"name": "copilot", "base_url": "http://x/v1", "type": "openai"}}
        )
        dumped = rc.model_dump(mode="json", exclude_none=True)
        assert dumped["provider"] == {
            "name": "copilot",
            "type": "openai",
            "base_url": "http://x/v1",
        }

    def test_secrets_redacted_in_dump(self) -> None:
        """``SecretStr`` fields must redact in ``model_dump`` (no secrets
        in event logs / checkpoints / dashboard payloads)."""
        rc = RuntimeConfig.model_validate(
            {"provider": {"name": "copilot", "base_url": "http://x/v1", "api_key": "sk-shh"}}
        )
        dumped = rc.model_dump(mode="json", exclude_none=True)
        # Pydantic SecretStr renders as "**********" in model_dump
        assert dumped["provider"]["api_key"] == "**********"


class TestHermesProviderSettings:
    """Hermes provider accepts ``base_url`` and ``api_key`` in structured config."""

    def test_hermes_with_base_url_accepted(self) -> None:
        s = ProviderSettings(name="hermes", base_url="https://openrouter.ai/api/v1")
        assert s.base_url == "https://openrouter.ai/api/v1"
        assert s.has_custom_routing()

    def test_hermes_with_api_key_accepted(self) -> None:
        s = ProviderSettings(
            name="hermes", base_url="https://openrouter.ai/api/v1", api_key="sk-or-test"
        )
        assert isinstance(s.api_key, SecretStr)
        assert s.api_key.get_secret_value() == "sk-or-test"

    def test_hermes_with_base_url_and_api_key_accepted(self) -> None:
        s = ProviderSettings(
            name="hermes",
            base_url="https://openrouter.ai/api/v1",
            api_key="sk-or-test",
        )
        assert s.has_custom_routing()

    def test_hermes_skip_memory_accepted(self) -> None:
        s = ProviderSettings(name="hermes", hermes_skip_memory=True)
        assert s.hermes_skip_memory is True

    def test_hermes_skip_context_files_accepted(self) -> None:
        s = ProviderSettings(name="hermes", hermes_skip_context_files=False)
        assert s.hermes_skip_context_files is False

    def test_hermes_skip_memory_rejected_for_non_hermes(self) -> None:
        with pytest.raises(ValidationError, match="hermes_skip_memory"):
            ProviderSettings(name="copilot", hermes_skip_memory=True)

    def test_hermes_skip_context_files_rejected_for_non_hermes(self) -> None:
        with pytest.raises(ValidationError, match="hermes_skip_context_files"):
            ProviderSettings(name="copilot", hermes_skip_context_files=True)

    def test_unsupported_provider_with_base_url_still_rejected(self) -> None:
        # claude/copilot/hermes support base_url; other providers must still reject it.
        with pytest.raises(ValidationError, match="not yet implemented"):
            ProviderSettings(name="claude-agent-sdk", base_url="http://proxy/v1")


class TestHasCustomRouting:
    """``has_custom_routing()`` gates env-var fallback activation."""

    def test_name_only_is_not_custom(self) -> None:
        assert not ProviderSettings(name="copilot").has_custom_routing()

    @pytest.mark.parametrize(
        "field,value",
        [
            # Anchor fields (any one activates custom routing on its own).
            ("base_url", "http://x"),
            ("api_key", "k"),
            ("bearer_token", "t"),
        ],
    )
    def test_anchor_field_activates_custom_routing(self, field: str, value: object) -> None:
        s = ProviderSettings(name="copilot", **{field: value})  # type: ignore[arg-type]
        assert s.has_custom_routing()

    @pytest.mark.parametrize(
        "field,value",
        [
            # Non-anchor fields activate custom routing only alongside an anchor;
            # the schema validator rejects them on their own (see TestProviderSettingsValidation).
            ("type", "openai"),
            ("wire_api", "completions"),
            ("headers", {"X-Foo": "1"}),
        ],
    )
    def test_non_anchor_field_activates_with_anchor(self, field: str, value: object) -> None:
        s = ProviderSettings(name="copilot", base_url="http://x/v1", **{field: value})  # type: ignore[arg-type]
        assert s.has_custom_routing()

    def test_azure_activates_custom_routing(self) -> None:
        s = ProviderSettings(
            name="copilot",
            type="azure",
            base_url="https://x.openai.azure.com",
            azure=AzureProviderOptions(api_version="2024-10-21"),
        )
        assert s.has_custom_routing()
