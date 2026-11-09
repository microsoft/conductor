"""Tests for environment document secret bindings and digest compatibility."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from conductor.config.environment import (
    EnvironmentDocument,
    ProfileDefinition,
    SecretBinding,
    SecretBindingSource,
    _document_digest,
    builtin_local_environment,
    load_environment_document,
)
from conductor.exceptions import ConfigurationError


class TestSecretBindingSource:
    """Requirements for SecretBindingSource schema and validation."""

    def test_valid_env_source(self) -> None:
        # Requirement: env var names matching [A-Za-z_][A-Za-z0-9_]* are accepted and stripped.
        source = SecretBindingSource(env="  MY_API_KEY  ")
        assert source.env == "MY_API_KEY"

    @pytest.mark.parametrize(
        "invalid_env", ["", "   ", "1VAR", "MY-KEY", "MY.KEY", "MY KEY", "VAR$"]
    )
    def test_env_var_name_charset_rejected(self, invalid_env: str) -> None:
        # Requirement: env var names outside the identifier charset are rejected
        # with rule in message.
        with pytest.raises(ValidationError) as exc_info:
            SecretBindingSource(env=invalid_env)
        msg = str(exc_info.value)
        assert "Invalid environment variable name" in msg or "match" in msg

    def test_source_kind_missing_rejected(self) -> None:
        # Requirement: source with no kinds set is rejected naming supported kinds
        # (supported source kinds: env).
        with pytest.raises(ValidationError) as exc_info:
            SecretBindingSource.model_validate({})
        assert "exactly one source kind must be set (supported source kinds: env)" in str(
            exc_info.value
        )

    def test_source_extra_forbidden(self) -> None:
        # Requirement: extra keys are forbidden on SecretBindingSource.
        with pytest.raises(ValidationError):
            SecretBindingSource(env="MY_KEY", vault="secret/data")  # type: ignore[call-arg]


class TestSecretBinding:
    """Requirements for SecretBinding schema and validation."""

    def test_valid_binding_defaults(self) -> None:
        # Requirement: allow defaults to None (all consumer classes allowed).
        binding = SecretBinding(source=SecretBindingSource(env="MY_SECRET"))
        assert binding.source.env == "MY_SECRET"
        assert binding.allow is None

    def test_valid_binding_allow_list(self) -> None:
        # Requirement: allow accepts allowed consumer classes or empty list for fail-closed.
        b1 = SecretBinding(source=SecretBindingSource(env="MY_SECRET"), allow=["script"])
        assert b1.allow == ["script"]

        b2 = SecretBinding(source=SecretBindingSource(env="MY_SECRET"), allow=["mcp"])
        assert b2.allow == ["mcp"]

        b3 = SecretBinding(source=SecretBindingSource(env="MY_SECRET"), allow=["script", "mcp"])
        assert b3.allow == ["script", "mcp"]

        b4 = SecretBinding(source=SecretBindingSource(env="MY_SECRET"), allow=["agent"])
        assert b4.allow == ["agent"]

        b5 = SecretBinding(source=SecretBindingSource(env="MY_SECRET"), allow=[])
        assert b5.allow == []

    def test_binding_invalid_allow_rejected(self) -> None:
        # Requirement: allow rejects names outside the script, mcp, and agent consumer classes.
        with pytest.raises(ValidationError):
            SecretBinding.model_validate({"source": {"env": "MY_SECRET"}, "allow": ["database"]})

    def test_binding_extra_forbidden(self) -> None:
        # Requirement: extra keys are forbidden on SecretBinding.
        with pytest.raises(ValidationError):
            SecretBinding(
                source=SecretBindingSource(env="MY_SECRET"),
                delivery="env",  # type: ignore[call-arg]
            )


class TestEnvironmentDocumentSecrets:
    """Requirements for EnvironmentDocument.secrets field and name validation."""

    @pytest.mark.parametrize(
        "valid_name",
        ["my_secret", "SECRET-1", "auth.token", "db_password", "a", "A", "0-secret"],
    )
    def test_secret_name_charset_valid(self, valid_name: str) -> None:
        # Requirement: secret binding names matching [A-Za-z0-9_.-]+ are accepted.
        doc = EnvironmentDocument(
            profiles={"default": ProfileDefinition(backend="local")},
            secrets={valid_name: SecretBinding(source=SecretBindingSource(env="API_KEY"))},
        )
        assert doc.secrets is not None
        assert valid_name in doc.secrets

    @pytest.mark.parametrize(
        "invalid_name",
        ["", "secret with spaces", "secret/slash", "secret:colon", "secret@at", "secret#hash"],
    )
    def test_secret_name_charset_invalid_rejected(self, invalid_name: str) -> None:
        # Requirement: secret binding names with invalid characters are rejected naming the rule.
        with pytest.raises(ValidationError) as exc_info:
            EnvironmentDocument(
                profiles={"default": ProfileDefinition(backend="local")},
                secrets={invalid_name: SecretBinding(source=SecretBindingSource(env="API_KEY"))},
            )
        assert "Invalid secret binding name" in str(exc_info.value)

    def test_secrets_optional_none_default(self) -> None:
        # Requirement: omitting secrets leaves it as None.
        doc = EnvironmentDocument(profiles={"default": ProfileDefinition(backend="local")})
        assert doc.secrets is None


class TestProfileDefinitionInheritControlEnvironment:
    """Requirements for ProfileDefinition.inherit_control_environment field."""

    def test_inherit_default_none(self) -> None:
        # Requirement: inherit_control_environment defaults to None (backend default).
        profile = ProfileDefinition(backend="local")
        assert profile.inherit_control_environment is None

    def test_inherit_explicit_bool(self) -> None:
        # Requirement: inherit_control_environment accepts explicit True or False.
        p_true = ProfileDefinition(backend="local", inherit_control_environment=True)
        assert p_true.inherit_control_environment is True

        p_false = ProfileDefinition(backend="local", inherit_control_environment=False)
        assert p_false.inherit_control_environment is False

    def test_profile_extra_forbidden(self) -> None:
        # Requirement: extra keys are forbidden on ProfileDefinition.
        with pytest.raises(ValidationError):
            ProfileDefinition(backend="local", unknown_extra="value")  # type: ignore[call-arg]


class TestDigestCompatibility:
    """Requirements for digest compatibility and regression golden values."""

    def test_secret_less_document_matches_origin_main_golden_digest(self) -> None:
        # Requirement: a secret-less document digests to EXACTLY the same sha256 as origin/main.
        resolved = builtin_local_environment()
        origin_main_builtin_hex = (
            "sha256:778052725263d9f7227b71c3ed3a8e29608250b8c01d7966e4d08928b743c224"
        )
        assert resolved.digest == origin_main_builtin_hex
        assert _document_digest(resolved.document) == origin_main_builtin_hex

        prod_doc = EnvironmentDocument(
            default="prod",
            profiles={
                "dev": ProfileDefinition(backend="local"),
                "prod": ProfileDefinition(backend="local"),
            },
        )
        origin_main_prod_hex = (
            "sha256:b339df0add3cfa83a9be10ce6e66ecd95d716155ab43d94a47418a5ab5f86003"
        )
        assert _document_digest(prod_doc) == origin_main_prod_hex

    def test_secrets_and_allow_change_digest(self) -> None:
        # Requirement: changing allow or source.env changes the document digest.
        base_doc = EnvironmentDocument(
            default="default",
            profiles={"default": ProfileDefinition(backend="local")},
        )
        base_digest = _document_digest(base_doc)

        doc_sec_env1 = EnvironmentDocument(
            default="default",
            profiles={"default": ProfileDefinition(backend="local")},
            secrets={"API_KEY": SecretBinding(source=SecretBindingSource(env="VAR1"))},
        )
        sec_env1_digest = _document_digest(doc_sec_env1)
        assert sec_env1_digest != base_digest

        doc_sec_env2 = EnvironmentDocument(
            default="default",
            profiles={"default": ProfileDefinition(backend="local")},
            secrets={"API_KEY": SecretBinding(source=SecretBindingSource(env="VAR2"))},
        )
        sec_env2_digest = _document_digest(doc_sec_env2)
        assert sec_env2_digest != sec_env1_digest

        doc_allow_script = EnvironmentDocument(
            default="default",
            profiles={"default": ProfileDefinition(backend="local")},
            secrets={
                "API_KEY": SecretBinding(
                    source=SecretBindingSource(env="VAR1"),
                    allow=["script"],
                )
            },
        )
        allow_script_digest = _document_digest(doc_allow_script)
        assert allow_script_digest != sec_env1_digest

        doc_allow_closed = EnvironmentDocument(
            default="default",
            profiles={"default": ProfileDefinition(backend="local")},
            secrets={
                "API_KEY": SecretBinding(
                    source=SecretBindingSource(env="VAR1"),
                    allow=[],
                )
            },
        )
        allow_closed_digest = _document_digest(doc_allow_closed)
        assert allow_closed_digest != allow_script_digest
        assert allow_closed_digest != sec_env1_digest

    def test_inherit_control_environment_distinct_digests(self) -> None:
        # Requirement: None vs explicit True vs explicit False produce three distinct digests.
        doc_none = EnvironmentDocument(
            default="default",
            profiles={"default": ProfileDefinition(backend="local")},
        )
        doc_true = EnvironmentDocument(
            default="default",
            profiles={
                "default": ProfileDefinition(backend="local", inherit_control_environment=True)
            },
        )
        doc_false = EnvironmentDocument(
            default="default",
            profiles={
                "default": ProfileDefinition(backend="local", inherit_control_environment=False)
            },
        )

        d_none = _document_digest(doc_none)
        d_true = _document_digest(doc_true)
        d_false = _document_digest(doc_false)

        assert len({d_none, d_true, d_false}) == 3
        # Explicit values must be present in the serialized dump.
        assert (
            "inherit_control_environment" in doc_true.model_dump(mode="json")["profiles"]["default"]
        )
        assert (
            "inherit_control_environment"
            in doc_false.model_dump(mode="json")["profiles"]["default"]
        )
        assert (
            "inherit_control_environment"
            not in doc_none.model_dump(mode="json")["profiles"]["default"]
        )


class TestLoadEnvironmentDocumentWithSecrets:
    """Requirements for loading environment documents containing secrets from YAML."""

    def test_loads_document_with_secrets_and_inherit(self, tmp_path: Path) -> None:
        # Requirement: a YAML document declaring secrets and
        # inherit_control_environment loads successfully.
        yaml_content = (
            "default: default\n"
            "profiles:\n"
            "  default:\n"
            "    backend: local\n"
            "    inherit_control_environment: false\n"
            "secrets:\n"
            "  GITHUB_TOKEN:\n"
            "    source:\n"
            "      env: GH_TOKEN\n"
            "    allow:\n"
            "      - script\n"
            "      - mcp\n"
            "  DB_PASS:\n"
            "    source:\n"
            "      env: DATABASE_PASSWORD\n"
        )
        path = tmp_path / "env.yaml"
        path.write_text(yaml_content, encoding="utf-8")

        doc = load_environment_document(path)
        assert doc.default == "default"
        assert doc.profiles["default"].inherit_control_environment is False
        assert doc.secrets is not None
        assert doc.secrets["GITHUB_TOKEN"].source.env == "GH_TOKEN"
        assert doc.secrets["GITHUB_TOKEN"].allow == ["script", "mcp"]
        assert doc.secrets["DB_PASS"].source.env == "DATABASE_PASSWORD"
        assert doc.secrets["DB_PASS"].allow is None

    def test_invalid_secret_in_yaml_raises_configuration_error(self, tmp_path: Path) -> None:
        # Requirement: schema errors in secrets section raise ConfigurationError with file_path.
        yaml_content = (
            "profiles:\n"
            "  default:\n"
            "    backend: local\n"
            "secrets:\n"
            "  invalid name!:\n"
            "    source:\n"
            "      env: TOKEN\n"
        )
        path = tmp_path / "bad_secret.yaml"
        path.write_text(yaml_content, encoding="utf-8")

        with pytest.raises(ConfigurationError) as exc_info:
            load_environment_document(path)
        assert exc_info.value.file_path == str(path)
