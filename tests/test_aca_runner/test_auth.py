"""Unit tests for `conductor.aca_runner.auth` (issue #396).

Exercises the leaf module in isolation: env-var resolution
(`resolve_runner_token`, `resolve_allowed_base_urls`), the
`inner_provider_settings` key/base_url allowlist
(`check_inner_provider_settings`), and the token comparison
(`token_gate`).
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from conductor.aca_runner.auth import (
    ALLOWED_INNER_PROVIDER_SETTINGS_KEYS,
    check_inner_provider_settings,
    resolve_allowed_base_urls,
    resolve_runner_token,
    token_gate,
)
from conductor.exceptions import ProviderError


class TestResolveRunnerToken:
    """`resolve_runner_token` — the `_clean_env` contract."""

    def test_returns_none_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ACA_RUNNER_AUTH_TOKEN", raising=False)
        assert resolve_runner_token() is None

    def test_returns_none_for_empty_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ACA_RUNNER_AUTH_TOKEN", "")
        assert resolve_runner_token() is None

    def test_returns_none_for_whitespace_only_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ACA_RUNNER_AUTH_TOKEN", "   ")
        assert resolve_runner_token() is None

    def test_returns_stripped_value_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ACA_RUNNER_AUTH_TOKEN", "  my-token  ")
        assert resolve_runner_token() == "my-token"


class TestResolveAllowedBaseUrls:
    """`resolve_allowed_base_urls` — comma-separated allowlist parsing."""

    def test_returns_none_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ACA_RUNNER_ALLOWED_BASE_URLS", raising=False)
        assert resolve_allowed_base_urls() is None

    def test_returns_none_when_only_whitespace_or_commas(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ACA_RUNNER_ALLOWED_BASE_URLS", " , , ")
        assert resolve_allowed_base_urls() is None

    def test_parses_comma_separated_list_and_strips_entries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "ACA_RUNNER_ALLOWED_BASE_URLS",
            " http://localhost:11434/v1 , http://example.com/v1 ",
        )
        assert resolve_allowed_base_urls() == (
            "http://localhost:11434/v1",
            "http://example.com/v1",
        )

    def test_drops_empty_entries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "ACA_RUNNER_ALLOWED_BASE_URLS", "http://a.example.com,,http://b.example.com"
        )
        assert resolve_allowed_base_urls() == ("http://a.example.com", "http://b.example.com")


class TestCheckInnerProviderSettings:
    """`check_inner_provider_settings` — key allowlist + base_url allowlist."""

    def test_none_settings_is_a_no_op(self) -> None:
        check_inner_provider_settings(None, allowed_base_urls=None)

    def test_empty_settings_is_a_no_op(self) -> None:
        check_inner_provider_settings({}, allowed_base_urls=None)

    @pytest.mark.parametrize("key", sorted(ALLOWED_INNER_PROVIDER_SETTINGS_KEYS))
    def test_each_allowed_key_individually_accepted(self, key: str) -> None:
        check_inner_provider_settings({key: "value"}, allowed_base_urls=None)

    def test_all_four_allowed_keys_together_accepted(self) -> None:
        check_inner_provider_settings(
            {
                "base_url": "http://localhost:11434/v1",
                "api_key": "k",
                "bearer_token": "t",
                "github_token": "g",
            },
            allowed_base_urls=None,
        )

    @pytest.mark.parametrize("key", ["runtime_url", "headers", "type", "wire_api", "azure"])
    def test_rejects_disallowed_keys_naming_the_key(self, key: str) -> None:
        with pytest.raises(ProviderError, match=key):
            check_inner_provider_settings({key: "value"}, allowed_base_urls=None)

    def test_rejects_base_url_outside_allowlist(self) -> None:
        with pytest.raises(ProviderError, match="not in the configured allowlist"):
            check_inner_provider_settings(
                {"base_url": "http://evil.example.com"},
                allowed_base_urls=("http://localhost:11434/v1",),
            )

    def test_accepts_base_url_inside_allowlist(self) -> None:
        check_inner_provider_settings(
            {"base_url": "http://localhost:11434/v1"},
            allowed_base_urls=("http://localhost:11434/v1",),
        )

    def test_accepts_base_url_inside_allowlist_ignoring_trailing_slash(self) -> None:
        check_inner_provider_settings(
            {"base_url": "http://localhost:11434/v1/"},
            allowed_base_urls=("http://localhost:11434/v1",),
        )

    def test_no_op_when_allowlist_is_none(self) -> None:
        check_inner_provider_settings(
            {"base_url": "http://anything.example.com"}, allowed_base_urls=None
        )

    def test_no_base_url_key_is_unaffected_by_allowlist(self) -> None:
        check_inner_provider_settings(
            {"github_token": "g"}, allowed_base_urls=("http://localhost:11434/v1",)
        )

    def test_tolerates_secretstr_wrapped_values(self) -> None:
        check_inner_provider_settings(
            {
                "api_key": SecretStr("k"),
                "bearer_token": SecretStr("t"),
                "github_token": SecretStr("g"),
                "base_url": "http://localhost:11434/v1",
            },
            allowed_base_urls=("http://localhost:11434/v1",),
        )

    def test_rejects_disallowed_key_even_when_value_is_secretstr(self) -> None:
        with pytest.raises(ProviderError, match="runtime_url"):
            check_inner_provider_settings(
                {"runtime_url": SecretStr("value")}, allowed_base_urls=None
            )


class TestTokenGate:
    """`token_gate` — the runner-side comparison wrapper."""

    def test_true_when_expected_is_none(self) -> None:
        assert token_gate("anything", None) is True
        assert token_gate(None, None) is True

    def test_false_for_missing_presented_token(self) -> None:
        assert token_gate(None, "expected-token") is False

    def test_false_for_wrong_presented_token(self) -> None:
        assert token_gate("wrong-token", "expected-token") is False

    def test_true_for_exact_match(self) -> None:
        assert token_gate("expected-token", "expected-token") is True

    def test_does_not_raise_on_non_ascii_presented_value(self) -> None:
        # constant_time_match's own trap: hmac.compare_digest raises TypeError
        # on a str containing non-ASCII characters unless bytes are compared.
        assert token_gate("caf\u00e9", "expected-token") is False
