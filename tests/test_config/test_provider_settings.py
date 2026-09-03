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

    def test_openai_wire_api_rejected_with_targeted_message(self) -> None:
        with pytest.raises(
            ValidationError, match=r"Provider fields \['wire_api'\] are Copilot-only"
        ):
            ProviderSettings(name="openai", wire_api="completions")

    def test_openai_type_rejected_with_targeted_message(self) -> None:
        with pytest.raises(ValidationError, match=r"Provider fields \['type'\] are Copilot-only"):
            ProviderSettings(name="openai", type="openai")

    def test_openai_bearer_token_rejected(self) -> None:
        with pytest.raises(ValidationError, match="only supported when name='copilot'"):
            ProviderSettings(name="openai", bearer_token="tok")

    def test_openai_headers_rejected(self) -> None:
        with pytest.raises(ValidationError, match="only supported when name='copilot'"):
            ProviderSettings(name="openai", headers={"X-Foo": "1"})

    def test_openai_wire_api_rejected(self) -> None:
        with pytest.raises(
            ValidationError, match=r"Provider fields \['wire_api'\] are Copilot-only"
        ):
            ProviderSettings(name="openai", wire_api="completions")

    def test_openai_with_base_url_and_api_key_accepted(self) -> None:
        s = ProviderSettings(name="openai", base_url="https://api.openai.com/v1", api_key="sk-xxx")
        assert s.base_url == "https://api.openai.com/v1"
        assert s.api_key is not None
        assert s.api_key.get_secret_value() == "sk-xxx"

    def test_openai_with_base_url_only_accepted(self) -> None:
        s = ProviderSettings(name="openai", base_url="http://localhost:11434/v1")
        assert s.base_url == "http://localhost:11434/v1"

    def test_openai_temperature_within_range_accepted(self) -> None:
        """Requirement: ``RuntimeConfig(provider={name: openai}, temperature=1.5)`` validates."""
        rc = RuntimeConfig(provider={"name": "openai"}, temperature=1.5)
        assert rc.provider.name == "openai"
        assert rc.temperature == 1.5

    def test_openai_temperature_out_of_range_rejected(self) -> None:
        """Requirement: ``RuntimeConfig(provider={name: openai}, temperature=2.5)`` raises."""
        with pytest.raises(ValidationError) as exc_info:
            RuntimeConfig(provider={"name": "openai"}, temperature=2.5)
        errors = exc_info.value.errors()
        assert any("temperature" in str(e.get("loc", [])) for e in errors)

    def test_openai_api_key_redacted_in_json_dump(self) -> None:
        """Requirement: openai ProviderSettings api_key redacts to '**********' in
        ``model_dump(mode='json')``."""
        s = ProviderSettings(name="openai", api_key="sk-x")
        dumped = s.model_dump(mode="json")
        assert dumped["api_key"] == "**********"
        assert "sk-x" not in str(dumped)

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

    def test_claude_structured_fields_accepted(self) -> None:
        # ProviderSettings accepts base_url and api_key for name="claude".
        s = ProviderSettings(
            name="claude",
            base_url="https://example.com/api/v1",
            api_key="sk-secret",
        )
        assert s.base_url == "https://example.com/api/v1"
        assert isinstance(s.api_key, SecretStr)
        assert s.api_key.get_secret_value() == "sk-secret"
        assert s.has_custom_routing()

    def test_claude_api_key_and_auth_token_coexist(self) -> None:
        # api_key and auth_token may both be set for name="claude" without conflict.
        s = ProviderSettings(
            name="claude",
            base_url="https://example.com/api/v1",
            api_key="sk-secret",
            auth_token="bt-secret",
        )
        assert s.api_key.get_secret_value() == "sk-secret"
        assert s.auth_token.get_secret_value() == "bt-secret"

    def test_claude_empty_api_key_rejected(self) -> None:
        # Empty api_key must raise ValidationError rather than silently normalize to None.
        with pytest.raises(ValidationError, match="'api_key' is empty"):
            ProviderSettings(name="claude", api_key="")

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

    def test_claude_api_key_redacted_in_json_dump(self) -> None:
        # SecretStr redaction covers model_dump output only; literal secrets in
        # YAML still leak via yaml_source in workflow_started, so ${ENV_VAR}
        # interpolation is required for true secrecy.
        s = ProviderSettings(name="claude", api_key="sk-secret")
        dumped = s.model_dump(mode="json")
        assert dumped["api_key"] == "**********"
        assert "sk-secret" not in str(dumped)


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


class TestExternalRuntimeConnection:
    """``runtime_url`` / ``runtime_token`` connect to an existing runtime."""

    def test_runtime_url_only(self) -> None:
        s = ProviderSettings(name="copilot", runtime_url="localhost:3000")
        assert s.runtime_url == "localhost:3000"
        assert s.runtime_token is None
        assert s.has_external_runtime()
        # A runtime connection is a separate axis from endpoint routing.
        assert not s.has_custom_routing()

    def test_runtime_url_and_token(self) -> None:
        s = ProviderSettings.model_validate(
            {"name": "copilot", "runtime_url": "host:9000", "runtime_token": "sekret"}
        )
        assert s.runtime_url == "host:9000"
        assert isinstance(s.runtime_token, SecretStr)
        assert s.runtime_token.get_secret_value() == "sekret"
        assert s.has_external_runtime()

    def test_runtime_fields_prevent_bare_string_collapse(self) -> None:
        """A runtime connection must survive round-trip serialization (it would
        be lost if the object collapsed to the bare ``"copilot"`` string)."""
        s = ProviderSettings(name="copilot", runtime_url="localhost:3000")
        dumped = s.model_dump()
        assert isinstance(dumped, dict)
        assert dumped["runtime_url"] == "localhost:3000"

    def test_runtime_token_redacted_in_json_dump(self) -> None:
        s = ProviderSettings.model_validate(
            {"name": "copilot", "runtime_url": "x:1", "runtime_token": "topsecret"}
        )
        dumped = s.model_dump(mode="json")
        assert dumped["runtime_token"] == "**********"
        assert "topsecret" not in str(dumped)

    def test_runtime_url_rejected_for_non_copilot(self) -> None:
        with pytest.raises(ValidationError, match="only supported when name='copilot'"):
            ProviderSettings(name="claude", runtime_url="x:1")

    def test_runtime_token_requires_runtime_url(self) -> None:
        with pytest.raises(ValidationError, match="'runtime_token' requires 'runtime_url'"):
            ProviderSettings(name="copilot", runtime_token="tok")

    def test_empty_runtime_token_rejected(self) -> None:
        with pytest.raises(ValidationError, match="'runtime_token' is empty"):
            ProviderSettings.model_validate(
                {"name": "copilot", "runtime_url": "x:1", "runtime_token": ""}
            )

    def test_empty_runtime_url_rejected(self) -> None:
        """An empty ``runtime_url`` must be rejected: otherwise ``""`` is not
        None, so ``has_external_runtime()`` returns True and the runtime_token
        guard passes, yet the provider treats ``""`` as falsy and silently
        spawns a nested runtime while dropping the token."""
        with pytest.raises(ValidationError, match="'runtime_url' is empty"):
            ProviderSettings.model_validate({"name": "copilot", "runtime_url": ""})

    def test_empty_runtime_url_with_token_rejected(self) -> None:
        with pytest.raises(ValidationError, match="'runtime_url' is empty"):
            ProviderSettings.model_validate(
                {"name": "copilot", "runtime_url": "", "runtime_token": "tok"}
            )

    def test_whitespace_runtime_url_rejected(self) -> None:
        """A whitespace-only ``runtime_url`` is stripped to empty by the provider
        resolver, so ``conductor validate`` must reject it at schema time too."""
        with pytest.raises(ValidationError, match="'runtime_url' is empty"):
            ProviderSettings.model_validate({"name": "copilot", "runtime_url": "   "})

    def test_whitespace_runtime_token_rejected(self) -> None:
        """A whitespace-only ``runtime_token`` normalizes to None at runtime
        (silently changing auth mode); reject it at schema time."""
        with pytest.raises(ValidationError, match="'runtime_token' is empty"):
            ProviderSettings.model_validate(
                {"name": "copilot", "runtime_url": "x:1", "runtime_token": "   "}
            )

    def test_whitespace_api_key_rejected(self) -> None:
        with pytest.raises(ValidationError, match="'api_key' is empty"):
            ProviderSettings.model_validate(
                {"name": "copilot", "base_url": "http://x/v1", "api_key": "   "}
            )

    def test_runtime_url_combines_with_custom_routing(self) -> None:
        s = ProviderSettings(
            name="copilot",
            runtime_url="localhost:3000",
            type="openai",
            wire_api="completions",
            base_url="http://localhost:11434/v1",
            api_key="local",
        )
        assert s.has_external_runtime()
        assert s.has_custom_routing()
        assert s.has_structured_config()


class TestSettingSourcesScoping:
    """``setting_sources`` is claude-agent-sdk-only, and security-relevant: a
    request to load ambient hooks must never be accepted and then dropped."""

    @pytest.mark.parametrize("name", ["copilot", "openai", "claude", "hermes", "aca"])
    def test_rejected_on_other_providers(self, name: str) -> None:
        """Only the ``claude-agent-sdk`` branch of the factory reads the field,
        so on any other provider it would be a silent no-op. ``aca`` is the
        sharpest case: the runner's four-key ``inner_provider_settings``
        allowlist never forwards it to the sandbox, even with
        ``inner_provider: claude-agent-sdk``."""
        with pytest.raises(ValidationError, match="only supported when name='claude-agent-sdk'"):
            ProviderSettings.model_validate({"name": name, "setting_sources": ["project"]})

    def test_accepted_on_claude_agent_sdk(self) -> None:
        s = ProviderSettings(name="claude-agent-sdk", setting_sources=["project"])
        assert s.setting_sources == ["project"]

    @pytest.mark.parametrize("tier", ["prject", "workspace", "PROJECT", ""])
    def test_unknown_tier_rejected(self, tier: str) -> None:
        """A typo would otherwise land straight on the CLI's
        ``--setting-sources`` argument."""
        with pytest.raises(ValidationError):
            ProviderSettings.model_validate({"name": "claude-agent-sdk", "setting_sources": [tier]})

    def test_counts_as_structured_config(self) -> None:
        """Without this the ``@model_serializer`` collapses the object to the
        bare name and the opt-in disappears; ``--provider`` overrides also drop
        it with no warning."""
        s = ProviderSettings(name="claude-agent-sdk", setting_sources=["project"])
        assert s.has_structured_config()

    def test_round_trips_through_model_dump(self) -> None:
        s = ProviderSettings(name="claude-agent-sdk", setting_sources=["user", "project"])
        assert ProviderSettings.model_validate(s.model_dump()) == s

    def test_override_warns_before_discarding_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`--provider claude-agent-sdk` against a workflow that opted into a
        settings tier must not drop it silently — every other structured field
        gets the warning."""
        import importlib
        from types import SimpleNamespace

        from conductor.config.schema import RuntimeConfig

        run_mod = importlib.import_module("conductor.cli.run")
        runtime = RuntimeConfig(
            provider=ProviderSettings(name="claude-agent-sdk", setting_sources=["project"])
        )
        config = SimpleNamespace(workflow=SimpleNamespace(runtime=runtime))
        messages: list[str] = []
        monkeypatch.setattr(
            run_mod, "verbose_log", lambda message, style="dim": messages.append(message)
        )

        run_mod._apply_provider_override(config, "claude-agent-sdk")

        assert any("discards structured runtime.provider settings" in msg for msg in messages)

    def test_described_for_verbose_output(self) -> None:
        """A toggle that enables arbitrary hook execution has to be visible."""
        from conductor.cli.run import _describe_provider

        s = ProviderSettings(name="claude-agent-sdk", setting_sources=["project"])
        assert _describe_provider(s) == "claude-agent-sdk setting_sources=['project']"
        assert _describe_provider(ProviderSettings(name="claude-agent-sdk")) == "claude-agent-sdk"

    def test_unset_still_serializes_as_bare_string(self) -> None:
        s = ProviderSettings(name="claude-agent-sdk")
        assert s.has_structured_config() is False
        assert s.model_dump() == "claude-agent-sdk"
