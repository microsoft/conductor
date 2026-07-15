"""Tests for the ``conductor doctor`` CLI command (issue #274).

Exercises rendering, JSON output, exit-code semantics, and error handling.
Data gathering is patched at ``conductor.cli.doctor.gather`` for the
flag/exit-code cases; one test runs the real offline path end-to-end.
"""

from __future__ import annotations

import importlib
import json
from unittest.mock import AsyncMock

import pytest
import typer.main
from rich.console import Console
from typer.testing import CliRunner

from conductor.cli.app import app
from conductor.providers.diagnostics import (
    CredentialEnvVar,
    DoctorReport,
    EnvDiagnostic,
    ModelDiagnostic,
    ProviderDiagnostic,
    RegistryDiagnostic,
    RegistryInfo,
)

runner = CliRunner()

# The submodule ``conductor.cli.app`` is shadowed by the ``app`` Typer object
# it exports (``conductor/cli/__init__.py`` does ``from conductor.cli.app
# import app``), so the string path / plain import resolves to the Typer, not
# the module. Grab the real module object explicitly for console patching.
_app_module = importlib.import_module("conductor.cli.app")


@pytest.fixture(autouse=True)
def _no_update_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the CLI offline and render at a fixed wide width.

    The doctor command renders through the module-level ``output_console`` /
    ``console`` in ``conductor.cli.app``, whose width tracks the ambient
    terminal. CI runs with a narrow non-TTY width that wraps and truncates
    Rich table cells, which would break substring assertions on the rendered
    output. Pinning both consoles to a fixed width makes rendering
    deterministic regardless of the environment.
    """
    monkeypatch.setenv("CONDUCTOR_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(_app_module, "output_console", Console(width=200))
    monkeypatch.setattr(_app_module, "console", Console(stderr=True, width=200))


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
    models: list[str] | list[ModelDiagnostic] | None = None,
    models_error: str | None = None,
    note: str | None = None,
) -> ProviderDiagnostic:
    """Build a ``ProviderDiagnostic`` for tests.

    ``models`` accepts either plain model-id strings (wrapped into id-only
    ``ModelDiagnostic`` entries — the common case for tests that don't care
    about per-model capability fields) or fully-populated ``ModelDiagnostic``
    instances (for tests exercising reasoning-effort / token-limit
    rendering).
    """
    model_diagnostics = None
    if models is not None:
        model_diagnostics = [
            m if isinstance(m, ModelDiagnostic) else ModelDiagnostic(id=m) for m in models
        ]
    return ProviderDiagnostic(
        name=name,
        installed=installed,
        implemented=implemented,
        tier=tier,
        credential_env_vars=creds or [],
        checked=checked,
        connection_ok=connection_ok,
        connection_error=connection_error,
        models=model_diagnostics,
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
    def test_help_runs(self) -> None:
        result = runner.invoke(app, ["doctor", "--help"])
        assert result.exit_code == 0

    def test_options_are_registered(self) -> None:
        # Inspect the command's registered parameters rather than parsing the
        # rendered help text: Rich wraps/truncates the options panel at narrow
        # (CI non-TTY) widths, so a substring check on the help output is
        # fragile. Param inspection verifies the flags actually exist.
        doctor_cmd = typer.main.get_command(app).commands["doctor"]
        opts = {opt for param in doctor_cmd.params for opt in (*param.opts, *param.secondary_opts)}
        for token in ("--check", "--models", "--provider", "--json"):
            assert token in opts


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

    def test_json_with_check_failure_exits_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The primary CI use case: emit machine-readable JSON AND signal a
        # non-zero exit when the scoped provider fails to connect.
        report = DoctorReport(providers=[_prov("copilot", checked=True, connection_ok=False)])
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "providers", "--check", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.stdout)  # JSON still valid despite exit 1
        assert data["providers"][0]["connection_ok"] is False

    def test_json_includes_registries_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = DoctorReport(registries=RegistryDiagnostic(default=None, error="malformed TOML"))
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "registries", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["registries"]["error"] == "malformed TOML"


# ---------------------------------------------------------------------------
# Secret-leak safety (end-to-end, real environment)
# ---------------------------------------------------------------------------


class TestDoctorSecretLeakEndToEnd:
    """A real secret in the environment must never reach stdout (presence only)."""

    _CANARY = "sk-ant-LEAK-CANARY-DO-NOT-PRINT"

    def test_offline_json_does_not_leak_env_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Real gather (NOT patched) with a real secret env var set.
        monkeypatch.setenv("ANTHROPIC_API_KEY", self._CANARY)
        result = runner.invoke(app, ["doctor", "providers", "--json"])
        assert result.exit_code == 0
        assert self._CANARY not in result.output
        data = json.loads(result.stdout)
        claude = next(p for p in data["providers"] if p["name"] == "claude")
        present = {c["name"]: c["present"] for c in claude["credential_env_vars"]}
        assert present["ANTHROPIC_API_KEY"] is True  # detected by presence
        assert all("value" not in c for c in claude["credential_env_vars"])

    def test_check_json_does_not_leak_env_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # --check must not echo the secret even while probing; patch provider
        # construction so no real network I/O happens.
        monkeypatch.setenv("ANTHROPIC_API_KEY", self._CANARY)
        fake = AsyncMock()
        fake.validate_connection.return_value = False
        fake.list_models.return_value = None
        fake.close.return_value = None
        monkeypatch.setattr(
            "conductor.providers.factory.create_provider",
            AsyncMock(return_value=fake),
        )
        result = runner.invoke(
            app, ["doctor", "providers", "--provider", "claude", "--check", "--json"]
        )
        assert result.exit_code == 1  # scoped claude fails to connect
        assert self._CANARY not in result.output
        data = json.loads(result.stdout)
        assert data["providers"][0]["connection_ok"] is False


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

    def test_all_models_shown_no_truncation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Every model id must appear — no "(+N more)" cap. Use a wide render so
        # the assertion isn't defeated by cell wrapping mid-identifier.
        models = [f"model-{i:02d}" for i in range(20)]
        report = DoctorReport(
            providers=[_prov("copilot", checked=True, connection_ok=True, models=models)]
        )
        _patch_gather(monkeypatch, report)
        monkeypatch.setattr(_app_module, "output_console", Console(width=1000))
        result = runner.invoke(app, ["doctor", "--models"])
        assert result.exit_code == 0
        assert "more)" not in result.output
        for model in models:
            assert model in result.output

    def test_models_summary_cell_shows_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The Providers table's Models column shows a count, not raw ids."""
        report = DoctorReport(
            providers=[
                _prov("copilot", checked=True, connection_ok=True, models=["gpt-5", "gpt-4"])
            ]
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "--models"])
        assert result.exit_code == 0
        assert "2 models" in result.output

    def test_model_capabilities_rendered_in_detail_table(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per-model reasoning-effort and token-limit fields render in the
        separate Models detail table (#301)."""
        report = DoctorReport(
            providers=[
                _prov(
                    "copilot",
                    checked=True,
                    connection_ok=True,
                    models=[
                        ModelDiagnostic(
                            id="gpt-5.5",
                            supported_reasoning_efforts=["low", "medium", "high", "xhigh"],
                            default_reasoning_effort="medium",
                            max_prompt_tokens=128_000,
                            max_output_tokens=64_000,
                            max_context_window_tokens=192_000,
                        )
                    ],
                )
            ]
        )
        _patch_gather(monkeypatch, report)
        monkeypatch.setattr(_app_module, "output_console", Console(width=200))
        result = runner.invoke(app, ["doctor", "--models"])
        assert result.exit_code == 0
        assert "Models — copilot" in result.output
        assert "low, medium, high, xhigh" in result.output
        assert "medium" in result.output
        assert "128,000" in result.output
        assert "64,000" in result.output
        assert "192,000" in result.output

    def test_unknown_model_capabilities_render_as_dash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unknown capability fields degrade to n/a / — rather than crashing."""
        report = DoctorReport(
            providers=[
                _prov(
                    "claude",
                    checked=True,
                    connection_ok=True,
                    models=[ModelDiagnostic(id="claude-3-opus-20240229")],
                )
            ]
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "--models"])
        assert result.exit_code == 0
        assert "claude-3-opus-20240229" in result.output
        assert "n/a" in result.output  # unknown reasoning-effort support

    def test_no_detail_table_when_models_empty_or_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Providers with no enumerated models don't get an empty detail table."""
        report = DoctorReport(
            providers=[
                _prov("copilot", checked=True, connection_ok=True, models=[]),
                _prov("claude", checked=True, connection_ok=True, models=None),
            ]
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "--models"])
        assert result.exit_code == 0
        assert "Models — copilot" not in result.output
        assert "Models — claude" not in result.output

    def test_json_includes_model_capability_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The JSON ``models`` field is a list of capability objects, not ids."""
        report = DoctorReport(
            providers=[
                _prov(
                    "copilot",
                    checked=True,
                    connection_ok=True,
                    models=[
                        ModelDiagnostic(
                            id="gpt-5.5",
                            supported_reasoning_efforts=["low", "medium"],
                            default_reasoning_effort="low",
                            max_prompt_tokens=128_000,
                            max_output_tokens=64_000,
                            max_context_window_tokens=192_000,
                        )
                    ],
                )
            ]
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "--models", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        model = data["providers"][0]["models"][0]
        assert model == {
            "id": "gpt-5.5",
            "supported_reasoning_efforts": ["low", "medium"],
            "default_reasoning_effort": "low",
            "max_prompt_tokens": 128_000,
            "max_output_tokens": 64_000,
            "max_context_window_tokens": 192_000,
        }


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

    def test_registries_load_error_renders(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A corrupt registries config is surfaced (not shown as "no registries")
        # and bracketed error text does not crash Rich rendering.
        report = DoctorReport(
            registries=RegistryDiagnostic(default=None, error="bad TOML at [line 3]")
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "registries"])
        assert result.exception is None
        assert result.exit_code == 0
        assert "failed to load registries" in result.output
        assert "line 3" in result.output
        assert "No registries configured" not in result.output
