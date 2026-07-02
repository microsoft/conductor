"""Tests for the ``conductor doctor`` CLI command (issue #274).

Exercises rendering, JSON output, exit-code semantics, and error handling.
Data gathering is patched at ``conductor.cli.doctor.gather`` for the
flag/exit-code cases; one test runs the real offline path end-to-end.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app
from conductor.providers.diagnostics import (
    CredentialEnvVar,
    DoctorReport,
    EnvDiagnostic,
    ProviderDiagnostic,
    RegistryDiagnostic,
    RegistryInfo,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_update_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the CLI offline and quiet across all doctor tests."""
    monkeypatch.setenv("CONDUCTOR_NO_UPDATE_CHECK", "1")


def _prov(
    name: str,
    *,
    installed: bool = True,
    implemented: bool = True,
    tier: str | None = "stable",
    creds: list[CredentialEnvVar] | None = None,
    checked: bool = False,
    connection_ok: bool | None = None,
    connection_error: str | None = None,
    models: list[str] | None = None,
    models_error: str | None = None,
    note: str | None = None,
) -> ProviderDiagnostic:
    return ProviderDiagnostic(
        name=name,
        installed=installed,
        implemented=implemented,
        tier=tier,
        credential_env_vars=creds or [],
        checked=checked,
        connection_ok=connection_ok,
        connection_error=connection_error,
        models=models,
        models_error=models_error,
        note=note,
    )


def _patch_gather(
    monkeypatch: pytest.MonkeyPatch,
    report: DoctorReport,
    captured: dict[str, object] | None = None,
) -> None:
    """Patch ``conductor.cli.doctor.gather`` to return *report*."""

    async def _fake_gather(**kwargs: object) -> DoctorReport:
        if captured is not None:
            captured.update(kwargs)
        return report

    monkeypatch.setattr("conductor.cli.doctor.gather", _fake_gather)


# ---------------------------------------------------------------------------
# Help / basic wiring
# ---------------------------------------------------------------------------


class TestDoctorHelp:
    def test_help_lists_options(self) -> None:
        result = runner.invoke(app, ["doctor", "--help"])
        assert result.exit_code == 0
        for token in ("--check", "--models", "--provider", "--json"):
            assert token in result.output


# ---------------------------------------------------------------------------
# Offline rendering (real end-to-end)
# ---------------------------------------------------------------------------


class TestDoctorOffline:
    def test_default_all_sections(self, monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
        monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "Environment" in result.output
        assert "copilot" in result.output
        assert "claude" in result.output

    def test_section_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}
        _patch_gather(
            monkeypatch, DoctorReport(registries=RegistryDiagnostic(default=None)), captured
        )
        result = runner.invoke(app, ["doctor", "registries"])
        assert result.exit_code == 0
        assert captured["sections"] == ("registries",)


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


class TestDoctorJson:
    def test_json_is_parseable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = DoctorReport(
            env=EnvDiagnostic(
                conductor_version="1.2.3",
                python_version="3.12.0",
                platform="test",
                update_checked=False,
                update_available=None,
                latest_version=None,
            ),
            providers=[_prov("copilot")],
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["env"]["conductor_version"] == "1.2.3"
        assert data["providers"][0]["name"] == "copilot"

    def test_json_never_leaks_secret_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = DoctorReport(
            providers=[
                _prov(
                    "claude",
                    creds=[
                        CredentialEnvVar("ANTHROPIC_API_KEY", True),
                        CredentialEnvVar("ANTHROPIC_AUTH_TOKEN", False),
                    ],
                )
            ]
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        creds = data["providers"][0]["credential_env_vars"]
        # Only name + present are ever serialized (no value field).
        assert creds[0] == {"name": "ANTHROPIC_API_KEY", "present": True}
        assert all(set(c) == {"name", "present"} for c in creds)


# ---------------------------------------------------------------------------
# Exit-code semantics
# ---------------------------------------------------------------------------


class TestDoctorExitCodes:
    def test_offline_exit_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_gather(monkeypatch, DoctorReport(providers=[_prov("copilot")]))
        result = runner.invoke(app, ["doctor", "providers"])
        assert result.exit_code == 0

    def test_scoped_default_failure_exits_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = DoctorReport(providers=[_prov("copilot", checked=True, connection_ok=False)])
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "providers", "--check"])
        assert result.exit_code == 1

    def test_optional_provider_failure_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = DoctorReport(
            providers=[
                _prov("copilot", checked=True, connection_ok=True),
                _prov("claude", checked=True, connection_ok=False),
            ]
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "providers", "--check"])
        assert result.exit_code == 0

    def test_scoped_provider_failure_exits_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = DoctorReport(providers=[_prov("claude", checked=True, connection_ok=False)])
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "providers", "--provider", "claude", "--check"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------


class TestDoctorFlags:
    def test_models_implies_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}
        _patch_gather(monkeypatch, DoctorReport(providers=[_prov("copilot")]), captured)
        result = runner.invoke(app, ["doctor", "--models"])
        assert result.exit_code == 0
        assert captured["check"] is True
        assert captured["list_models"] is True

    def test_models_rendered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = DoctorReport(
            providers=[
                _prov("copilot", checked=True, connection_ok=True, models=["gpt-5", "gpt-4"])
            ]
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "--models"])
        assert result.exit_code == 0
        assert "gpt-5" in result.output


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestDoctorErrors:
    def test_unknown_provider(self) -> None:
        result = runner.invoke(app, ["doctor", "--provider", "bogus"])
        assert result.exit_code == 1
        assert "Unknown provider" in (result.stderr or result.output)

    def test_unknown_section(self) -> None:
        result = runner.invoke(app, ["doctor", "bogus"])
        assert result.exit_code == 1
        assert "Unknown section" in (result.stderr or result.output)


class TestDoctorMarkupSafety:
    """Free-form strings with Rich markup metacharacters must not crash rendering."""

    def test_bracketed_connection_error_renders(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = DoctorReport(
            providers=[
                _prov(
                    "claude",
                    checked=True,
                    connection_ok=False,
                    connection_error="[Errno 2] No such file [/Users/x]",
                )
            ]
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "providers", "--provider", "claude", "--check"])
        # Rendering the whole table happens in one console.print; if the
        # bracketed error weren't escaped, Rich would raise MarkupError and the
        # error text would never reach stdout. Its presence proves no crash.
        assert result.exit_code == 1
        assert "Errno 2" in result.output

    def test_bracketed_registry_source_renders(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = DoctorReport(
            registries=RegistryDiagnostic(
                default="local",
                registries=[
                    RegistryInfo(name="local", type="path", source="[/weird/path]", is_default=True)
                ],
            )
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "registries"])
        assert result.exception is None
        assert result.exit_code == 0
        assert "weird/path" in result.output
