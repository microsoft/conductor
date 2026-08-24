"""Tests for the ``conductor doctor`` CLI command (issue #274).

Exercises rendering, JSON output, exit-code semantics, and error handling.
Data gathering is patched at ``conductor.cli.doctor.gather`` for the
flag/exit-code cases; one test runs the real offline path end-to-end.
"""

from __future__ import annotations

import importlib
import io
import json
from unittest.mock import AsyncMock

import pytest
import typer.main
from rich.console import Console
from typer.testing import CliRunner

from conductor.cli.app import app
from conductor.console import make_console
from conductor.providers.diagnostics import (
    CredentialEnvVar,
    DoctorReport,
    EnvDiagnostic,
    McpServeCollision,
    McpServeDiagnostic,
    McpServeFailedRegistry,
    McpServeRejectedWorkflow,
    McpServeToolInfo,
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

    ``make_console`` rather than a bare ``Console``: the production consoles
    are markup-free (#406), and substituting a markup-parsing one here would
    make the markup-safety tests below assert against a console the CLI never
    uses — which is exactly the bug they exist to catch.
    """
    monkeypatch.setenv("CONDUCTOR_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(_app_module, "output_console", make_console(width=200))
    monkeypatch.setattr(_app_module, "console", make_console(stderr=True, width=200))


def _prov(
    name: str,
    *,
    installed: bool = True,
    implemented: bool = True,
    tier: str | None = "stable",
    creds: list[CredentialEnvVar] | None = None,
    credentials_optional: bool = False,
    checked: bool = False,
    connection_ok: bool | None = None,
    connection_error: str | None = None,
    connection_note: str | None = None,
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
        credentials_optional=credentials_optional,
        checked=checked,
        connection_ok=connection_ok,
        connection_error=connection_error,
        connection_note=connection_note,
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

    def test_copilot_absent_creds_render_neutral_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Real gather (NOT patched) reproducing issue #319 exactly as filed:
        # with every copilot credential env var cleared, the offline view
        # must render the neutral ○ (never the alarming ✗) plus its note.
        for var in (
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "COPILOT_PROVIDER_API_KEY",
            "COPILOT_PROVIDER_BEARER_TOKEN",
            "COPILOT_PROVIDER_RUNTIME_TOKEN",
        ):
            monkeypatch.delenv(var, raising=False)
        result = runner.invoke(app, ["doctor", "providers", "--provider", "copilot"])
        assert result.exit_code == 0
        assert "✗" not in result.output
        assert "○ GITHUB_TOKEN" in result.output
        assert "optional" in result.output


# ---------------------------------------------------------------------------
# Credential rendering — optional vs. required (issue #319)
# ---------------------------------------------------------------------------


class TestDoctorCredentialRendering:
    """Absent *optional* credentials render neutrally, not as an alarming ✗."""

    def test_optional_absent_credentials_render_neutral_with_note(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # copilot authenticates via the CLI login on disk, so an all-absent
        # credentials cell must NOT read as a misconfiguration.
        report = DoctorReport(
            providers=[
                _prov(
                    "copilot",
                    creds=[
                        CredentialEnvVar("GITHUB_TOKEN", True),
                        CredentialEnvVar("GH_TOKEN", False),
                        CredentialEnvVar("COPILOT_PROVIDER_API_KEY", False),
                    ],
                    credentials_optional=True,
                    note="CLI login; env vars optional",
                )
            ]
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "providers"])
        assert result.exit_code == 0
        # A present credential is still a ✓ regardless of optionality.
        assert "✓ GITHUB_TOKEN" in result.output
        # Absent optional credentials use the neutral ○, never the red ✗.
        assert "○ GH_TOKEN" in result.output
        assert "○ COPILOT_PROVIDER_API_KEY" in result.output
        assert "✗ GH_TOKEN" not in result.output
        assert "✗ COPILOT_PROVIDER_API_KEY" not in result.output
        # The auth-path note explains why an all-absent cell is expected.
        assert "CLI login; env vars optional" in result.output

    def test_required_absent_credentials_still_render_cross(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # claude (direct API) genuinely requires a key — an absent required
        # credential must keep the ✗ so a real misconfiguration stays visible.
        report = DoctorReport(
            providers=[
                _prov(
                    "claude",
                    creds=[
                        CredentialEnvVar("ANTHROPIC_API_KEY", False),
                        CredentialEnvVar("ANTHROPIC_AUTH_TOKEN", False),
                    ],
                    credentials_optional=False,
                )
            ]
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "providers"])
        assert result.exit_code == 0
        assert "✗ ANTHROPIC_API_KEY" in result.output
        assert "○ ANTHROPIC_API_KEY" not in result.output


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

    def test_json_includes_credentials_optional(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # credentials_optional must survive the full report -> to_dict ->
        # print_json round trip, not just direct-construct unit tests.
        report = DoctorReport(
            providers=[
                _prov("copilot", credentials_optional=True),
                _prov("claude", credentials_optional=False),
            ]
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        by_name = {p["name"]: p["credentials_optional"] for p in data["providers"]}
        assert by_name == {"copilot": True, "claude": False}

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
        # Target the model's own row, not just "n/a" anywhere in the output.
        model_lines = [line for line in result.output.splitlines() if "claude-3-opus" in line]
        assert model_lines, "model row not found in output"
        assert "n/a" in model_lines[0]

    def test_empty_reasoning_efforts_renders_as_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty list (definitively 'supports none') renders as 'none',
        distinct from the 'n/a' shown for unknown (None) support."""
        report = DoctorReport(
            providers=[
                _prov(
                    "claude",
                    checked=True,
                    connection_ok=True,
                    models=[
                        ModelDiagnostic(
                            id="claude-3-5-sonnet-20241022",
                            supported_reasoning_efforts=[],
                            max_prompt_tokens=200_000,
                        )
                    ],
                )
            ]
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "--models"])
        assert result.exit_code == 0
        model_lines = [line for line in result.output.splitlines() if "claude-3-5-sonnet" in line]
        assert model_lines, "model row not found in output"
        assert "none" in model_lines[0]
        assert "n/a" not in model_lines[0]

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
            "input_per_mtok": None,
            "output_per_mtok": None,
            "pricing_source": None,
        }


class TestDoctorModelsPricing:
    """Tests for the pricing columns added to the Models detail table (#386)."""

    def test_pricing_columns_render_in_detail_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = DoctorReport(
            providers=[
                _prov(
                    "copilot",
                    checked=True,
                    connection_ok=True,
                    models=[
                        ModelDiagnostic(
                            id="gpt-4o",
                            input_per_mtok=2.50,
                            output_per_mtok=10.00,
                            pricing_source="table",
                        )
                    ],
                )
            ]
        )
        _patch_gather(monkeypatch, report)
        monkeypatch.setattr(_app_module, "output_console", Console(width=200))
        result = runner.invoke(app, ["doctor", "--models"])
        assert result.exit_code == 0
        model_lines = [line for line in result.output.splitlines() if "gpt-4o" in line]
        assert model_lines, "model row not found in output"
        assert "2.50" in model_lines[0]
        assert "10.00" in model_lines[0]
        assert "table" in model_lines[0]

    def test_provider_source_renders(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = DoctorReport(
            providers=[
                _prov(
                    "copilot",
                    checked=True,
                    connection_ok=True,
                    models=[
                        ModelDiagnostic(
                            id="gpt-5.6-sol",
                            input_per_mtok=2.00,
                            output_per_mtok=8.00,
                            pricing_source="provider",
                        )
                    ],
                )
            ]
        )
        _patch_gather(monkeypatch, report)
        monkeypatch.setattr(_app_module, "output_console", Console(width=200))
        result = runner.invoke(app, ["doctor", "--models"])
        assert result.exit_code == 0
        model_lines = [line for line in result.output.splitlines() if "gpt-5.6-sol" in line]
        assert model_lines, "model row not found in output"
        assert "provider" in model_lines[0]

    def test_unpriced_model_renders_dash_not_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unpriced model must render `—`, never `0.00` — a zero would
        read as "free", the exact silent-wrong-number bug #386 is about."""
        report = DoctorReport(
            providers=[
                _prov(
                    "copilot",
                    checked=True,
                    connection_ok=True,
                    models=[ModelDiagnostic(id="grok-4.5", pricing_source="none")],
                )
            ]
        )
        _patch_gather(monkeypatch, report)
        monkeypatch.setattr(_app_module, "output_console", Console(width=200))
        result = runner.invoke(app, ["doctor", "--models"])
        assert result.exit_code == 0
        model_lines = [line for line in result.output.splitlines() if "grok-4.5" in line]
        assert model_lines, "model row not found in output"
        assert "0.00" not in model_lines[0]
        assert "none" in model_lines[0]

    def test_genuine_zero_rate_renders_as_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A genuinely free model (rate 0.0, distinct from unpriced ``None``)
        must render `0.00`, not `—` — ``CopilotProvider.get_model_pricing``
        returns exactly this for free models (see test_copilot.py)."""
        report = DoctorReport(
            providers=[
                _prov(
                    "copilot",
                    checked=True,
                    connection_ok=True,
                    models=[
                        ModelDiagnostic(
                            id="free-model",
                            input_per_mtok=0.0,
                            output_per_mtok=0.0,
                            pricing_source="provider",
                        )
                    ],
                )
            ]
        )
        _patch_gather(monkeypatch, report)
        monkeypatch.setattr(_app_module, "output_console", Console(width=200))
        result = runner.invoke(app, ["doctor", "--models"])
        assert result.exit_code == 0
        model_lines = [line for line in result.output.splitlines() if "free-model" in line]
        assert model_lines, "model row not found in output"
        assert "0.00" in model_lines[0]

    def test_pricing_resolution_failure_renders_distinct_from_unpriced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``pricing_source=None`` (resolution itself failed) must render
        distinctly from the determined ``"none"`` — both used to collapse
        to the same `—` glyph the table uses for "provider doesn't expose
        this", hiding a systemic pricing-hook break behind a boring-looking
        row."""
        report = DoctorReport(
            providers=[
                _prov(
                    "copilot",
                    checked=True,
                    connection_ok=True,
                    models=[ModelDiagnostic(id="broken-model", pricing_source=None)],
                )
            ]
        )
        _patch_gather(monkeypatch, report)
        monkeypatch.setattr(_app_module, "output_console", Console(width=200))
        result = runner.invoke(app, ["doctor", "--models"])
        assert result.exit_code == 0
        model_lines = [line for line in result.output.splitlines() if "broken-model" in line]
        assert model_lines, "model row not found in output"
        assert "error" in model_lines[0]

    def test_degraded_capabilities_still_gets_pricing_columns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A model whose capabilities call failed (all capability fields
        ``None``) must still render its independently-resolved pricing."""
        report = DoctorReport(
            providers=[
                _prov(
                    "copilot",
                    checked=True,
                    connection_ok=True,
                    models=[
                        ModelDiagnostic(
                            id="gpt-4o",
                            supported_reasoning_efforts=None,
                            input_per_mtok=2.50,
                            output_per_mtok=10.00,
                            pricing_source="table",
                        )
                    ],
                )
            ]
        )
        _patch_gather(monkeypatch, report)
        monkeypatch.setattr(_app_module, "output_console", Console(width=200))
        result = runner.invoke(app, ["doctor", "--models"])
        assert result.exit_code == 0
        model_lines = [line for line in result.output.splitlines() if "gpt-4o" in line]
        assert model_lines, "model row not found in output"
        assert "n/a" in model_lines[0]
        assert "2.50" in model_lines[0]
        assert "10.00" in model_lines[0]
        assert "table" in model_lines[0]

    def test_json_includes_pricing_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = DoctorReport(
            providers=[
                _prov(
                    "copilot",
                    checked=True,
                    connection_ok=True,
                    models=[
                        ModelDiagnostic(
                            id="gpt-4o",
                            input_per_mtok=2.50,
                            output_per_mtok=10.00,
                            pricing_source="table",
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
        assert model["input_per_mtok"] == 2.50
        assert model["output_per_mtok"] == 10.00
        assert model["pricing_source"] == "table"


# ---------------------------------------------------------------------------
# MCP serve section (issue #432, E13)
# ---------------------------------------------------------------------------


class TestDoctorMcpServeRendering:
    """Mocked-gather rendering tests for the ``mcp`` section."""

    def test_mcp_is_a_valid_section(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}
        _patch_gather(
            monkeypatch,
            DoctorReport(mcp_serve=McpServeDiagnostic()),
            captured,
        )
        result = runner.invoke(app, ["doctor", "mcp"])
        assert result.exit_code == 0
        assert captured["sections"] == ("mcp",)

    def test_mcp_included_in_default_sections(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}
        _patch_gather(monkeypatch, DoctorReport(mcp_serve=McpServeDiagnostic()), captured)
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "mcp" in captured["sections"]

    def test_no_registries_renders(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = DoctorReport(mcp_serve=McpServeDiagnostic(registries=[], tools=[], mode="direct"))
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "mcp"])
        assert result.exit_code == 0
        assert "no tools" in result.output

    def test_registries_configured_but_no_tools_exposed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = DoctorReport(
            mcp_serve=McpServeDiagnostic(registries=["official"], tools=[], mode="direct")
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "mcp"])
        assert result.exit_code == 0
        assert "No workflows would be exposed" in result.output

    def test_one_registry_with_a_tool_renders(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = DoctorReport(
            mcp_serve=McpServeDiagnostic(
                registries=["official"],
                tools=[
                    McpServeToolInfo(
                        tool_name="review_pr",
                        registry="official",
                        workflow="review-pr",
                        resolution_tier="parsed",
                        pin="hash:" + "a" * 64,
                    )
                ],
                mode="direct",
            )
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "mcp"])
        assert result.exit_code == 0
        assert "review_pr" in result.output
        assert "official" in result.output
        assert "review-pr" in result.output
        assert "direct" in result.output

    def test_degraded_schema_is_flagged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = DoctorReport(
            mcp_serve=McpServeDiagnostic(
                registries=["official"],
                tools=[
                    McpServeToolInfo(
                        tool_name="broken_wf",
                        registry="official",
                        workflow="broken-wf",
                        resolution_tier="degraded",
                        pin="hash:" + "b" * 64,
                    )
                ],
                mode="direct",
                degraded=["broken_wf"],
            )
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "mcp"])
        assert result.exit_code == 0
        assert "degraded" in result.output

    def test_collision_renders_qualified_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = DoctorReport(
            mcp_serve=McpServeDiagnostic(
                registries=["official", "team"],
                tools=[
                    McpServeToolInfo(
                        tool_name="official_review_pr",
                        registry="official",
                        workflow="review-pr",
                        resolution_tier="parsed",
                        pin="hash:" + "a" * 64,
                    ),
                    McpServeToolInfo(
                        tool_name="team_review_pr",
                        registry="team",
                        workflow="review-pr",
                        resolution_tier="parsed",
                        pin="hash:" + "b" * 64,
                    ),
                ],
                mode="direct",
                collisions=[
                    McpServeCollision(
                        base_slug="review_pr",
                        identities=["official/review-pr", "team/review-pr"],
                        qualified_names=["official_review_pr", "team_review_pr"],
                    )
                ],
            )
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "mcp"])
        assert result.exit_code == 0
        assert "official_review_pr" in result.output
        assert "team_review_pr" in result.output
        assert "collision" in result.output

    def test_rejected_workflow_renders(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = DoctorReport(
            mcp_serve=McpServeDiagnostic(
                registries=["official"],
                tools=[],
                mode="direct",
                rejected=[
                    McpServeRejectedWorkflow(
                        registry="official",
                        workflow="bad-wf",
                        reason="input '_wait_seconds' collides with the reserved parameter",
                    )
                ],
            )
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "mcp"])
        assert result.exit_code == 0
        assert "bad-wf" in result.output
        assert "reserved parameter" in result.output

    def test_failed_registry_renders(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = DoctorReport(
            mcp_serve=McpServeDiagnostic(
                registries=["broken"],
                tools=[],
                mode="direct",
                failed_registries=[
                    McpServeFailedRegistry(
                        registry="broken",
                        reason="could not resolve its index",
                    )
                ],
            )
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "mcp"])
        assert result.exit_code == 0
        assert "broken" in result.output
        assert "could not resolve its index" in result.output

    def test_broken_registry_degrades_to_reported_problem(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A total catastrophic failure (e.g. malformed registries.toml) is
        # surfaced via `error`, never a crash -- mirrors the registries
        # section's own error handling.
        report = DoctorReport(mcp_serve=McpServeDiagnostic(error="malformed registries.toml"))
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "mcp"])
        assert result.exception is None
        assert result.exit_code == 0
        assert "failed to build the MCP catalogue" in result.output
        assert "malformed registries.toml" in result.output

    def test_json_includes_mcp_serve(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = DoctorReport(
            mcp_serve=McpServeDiagnostic(
                registries=["official"],
                tools=[
                    McpServeToolInfo(
                        tool_name="review_pr",
                        registry="official",
                        workflow="review-pr",
                        resolution_tier="parsed",
                        pin="hash:" + "a" * 64,
                    )
                ],
                mode="direct",
                failed_registries=[McpServeFailedRegistry(registry="broken", reason="unreachable")],
            )
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "mcp", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["mcp_serve"]["mode"] == "direct"
        assert data["mcp_serve"]["registries"] == ["official"]
        assert data["mcp_serve"]["tools"][0]["tool_name"] == "review_pr"
        assert data["mcp_serve"]["tools"][0]["pin"] == "hash:" + "a" * 64
        assert data["mcp_serve"]["failed_registries"] == [
            {"registry": "broken", "reason": "unreachable"}
        ]


class TestGatherMcpServe:
    """Real (unpatched) ``gather_mcp_serve`` / ``conductor doctor mcp`` tests,
    exercising the actual catalogue-building pipeline against on-disk
    registry fixtures (issue #432, E13-T3)."""

    def test_no_registries_configured_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))
        from conductor.providers.diagnostics import gather_mcp_serve

        diag = gather_mcp_serve()
        assert diag.error is None
        assert diag.registries == []
        assert diag.tools == []
        assert diag.mode == "direct"

    def test_one_registry_with_a_workflow_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        from tests.test_mcp.conftest import write_path_registry

        home = tmp_path / "home"  # type: ignore[operator]
        home.mkdir()
        monkeypatch.setenv("CONDUCTOR_HOME", str(home))

        entry = write_path_registry(
            tmp_path,  # type: ignore[arg-type]
            name="official",
            workflows={"review-pr": _SIMPLE_WORKFLOW_YAML.format(name="review-pr")},
        )
        (home / "registries.toml").write_text(
            f'[registries.official]\ntype = "path"\nsource = "{entry.source}"\n',
            encoding="utf-8",
        )

        from conductor.providers.diagnostics import gather_mcp_serve

        diag = gather_mcp_serve()
        assert diag.error is None
        assert diag.registries == ["official"]
        assert len(diag.tools) == 1
        assert diag.tools[0].registry == "official"
        assert diag.tools[0].workflow == "review-pr"
        assert diag.mode == "direct"

        result = runner.invoke(app, ["doctor", "mcp"])
        assert result.exit_code == 0
        assert "official" in result.output

    def test_collision_across_two_registries_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        from tests.test_mcp.conftest import write_path_registry

        home = tmp_path / "home"  # type: ignore[operator]
        home.mkdir()
        monkeypatch.setenv("CONDUCTOR_HOME", str(home))

        official = write_path_registry(
            tmp_path,  # type: ignore[arg-type]
            name="official",
            workflows={"review-pr": _SIMPLE_WORKFLOW_YAML.format(name="review-pr")},
        )
        team = write_path_registry(
            tmp_path,  # type: ignore[arg-type]
            name="team",
            workflows={"review-pr": _SIMPLE_WORKFLOW_YAML.format(name="review-pr")},
        )
        (home / "registries.toml").write_text(
            "[registries.official]\n"
            'type = "path"\n'
            f'source = "{official.source}"\n'
            "[registries.team]\n"
            'type = "path"\n'
            f'source = "{team.source}"\n',
            encoding="utf-8",
        )

        from conductor.providers.diagnostics import gather_mcp_serve

        diag = gather_mcp_serve()
        assert diag.error is None
        assert {t.tool_name for t in diag.tools} == {"official_review_pr", "team_review_pr"}
        assert len(diag.collisions) == 1
        assert diag.collisions[0].base_slug == "review_pr"

        result = runner.invoke(app, ["doctor", "mcp"])
        assert result.exit_code == 0
        assert "official_review_pr" in result.output
        assert "team_review_pr" in result.output

    def test_unreachable_github_registry_degrades_without_raising(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        # A github registry with no warm cache (no ref pointer ever
        # recorded) cannot be resolved offline -- the catalogue builder
        # skips it (logged, not raised); the diagnostic reports the empty
        # result rather than surfacing an exception.
        home = tmp_path / "home"  # type: ignore[operator]
        home.mkdir()
        monkeypatch.setenv("CONDUCTOR_HOME", str(home))
        (home / "registries.toml").write_text(
            '[registries.broken]\ntype = "github"\nsource = "someorg/does-not-exist"\n',
            encoding="utf-8",
        )

        from conductor.providers.diagnostics import gather_mcp_serve

        diag = gather_mcp_serve()
        assert diag.error is None
        assert diag.registries == ["broken"]
        assert diag.tools == []
        assert diag.mode == "direct"
        assert len(diag.failed_registries) == 1
        assert diag.failed_registries[0].registry == "broken"
        assert diag.failed_registries[0].reason

        result = runner.invoke(app, ["doctor", "mcp"])
        assert result.exception is None
        assert result.exit_code == 0
        assert "broken" in result.output

    def test_malformed_registries_toml_degrades_to_reported_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        home = tmp_path / "home"  # type: ignore[operator]
        home.mkdir()
        monkeypatch.setenv("CONDUCTOR_HOME", str(home))
        (home / "registries.toml").write_text("this is not valid toml [[[", encoding="utf-8")

        from conductor.providers.diagnostics import gather_mcp_serve

        diag = gather_mcp_serve()
        assert diag.error is not None
        assert diag.tools == []

        result = runner.invoke(app, ["doctor", "mcp"])
        assert result.exception is None
        assert result.exit_code == 0
        assert "failed to build the MCP catalogue" in result.output


_SIMPLE_WORKFLOW_YAML = """\
workflow:
  name: {name}
  description: A simple workflow named {name}.
  entry_point: worker
agents:
  - name: worker
    prompt: "Do the thing."
    output:
      result:
        type: string
output:
  result: "{{{{ worker.output.result }}}}"
"""


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


# ---------------------------------------------------------------------------
# Encoding fallback (issue #401)
# ---------------------------------------------------------------------------


class TestDoctorEncodingFallback:
    """The table output degrades to ASCII glyphs on a stream that cannot
    encode the Unicode ones, instead of dying part-written."""

    def _bind_console(
        self, monkeypatch: pytest.MonkeyPatch, encoding: str
    ) -> tuple[io.BytesIO, io.TextIOWrapper]:
        """Point the CLI's output console at a fresh buffer with *encoding*.

        Returns the raw byte buffer rather than relying on ``result.output``:
        Click's ``CliRunner`` captures through a UTF-8 stream that would
        accept a glyph a real cp1252 console rejects, so a test could pass
        on output the user never gets. Binding the CLI's console to a
        ``TextIOWrapper`` in the target *encoding* makes a leaked glyph
        raise ``UnicodeEncodeError`` at write time, surfacing via
        ``result.exception``.
        """
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding=encoding, newline="")
        monkeypatch.setattr(_app_module, "output_console", make_console(file=stream, width=200))
        monkeypatch.setattr(
            _app_module, "console", make_console(file=stream, stderr=True, width=200)
        )
        return buffer, stream

    @pytest.mark.parametrize(
        "shape_kwargs",
        [
            pytest.param({}, id="no-connection-data"),
            pytest.param(
                {
                    "checked": True,
                    "connection_ok": True,
                    "connection_note": "probe was inconclusive; endpoint may lack /v1/models",
                },
                id="connection-note",
            ),
        ],
    )
    @pytest.mark.parametrize("cli_args", [[], ["--check"], ["--models"]])
    def test_cp1252_console_renders_ascii_glyphs_without_crashing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        shape_kwargs: dict[str, object],
        cli_args: list[str],
    ) -> None:
        # The "connection-note" shape crossed with "--check"/"--models" is
        # what exercises _connection_cell's warning branch: the two flags
        # that add the Connection column are the only paths that ever touch
        # the cp1252 stream with this data (#401 follow-up review).
        buffer, stream = self._bind_console(monkeypatch, "cp1252")
        report = DoctorReport(
            providers=[_prov("copilot", installed=True, **shape_kwargs)],
            registries=RegistryDiagnostic(
                default="local",
                registries=[
                    RegistryInfo(name="local", type="path", source="~/.conductor", is_default=True)
                ],
            ),
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", *cli_args])
        stream.flush()
        assert result.exception is None
        assert result.exit_code == 0
        # Decode is exact, not lossy; a leaked glyph is caught by the
        # result.exception assertion above, which fails at write time.
        output = buffer.getvalue().decode("cp1252")
        assert "OK" in output

    def test_utf8_console_keeps_unicode_glyphs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        buffer, stream = self._bind_console(monkeypatch, "utf-8")
        report = DoctorReport(providers=[_prov("copilot", installed=True)])
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor"])
        stream.flush()
        assert result.exception is None
        assert result.exit_code == 0
        output = buffer.getvalue().decode("utf-8")
        assert "✓" in output
        assert "OK" not in output

    def test_stream_with_no_encoding_keeps_unicode_glyphs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A console file with no encoding (``.encoding is None``, e.g. an
        in-memory ``StringIO``) is treated as capable of anything, not
        downgraded to ASCII (#401)."""
        stream = io.StringIO()
        assert stream.encoding is None
        monkeypatch.setattr(_app_module, "output_console", make_console(file=stream, width=200))
        monkeypatch.setattr(
            _app_module, "console", make_console(file=stream, stderr=True, width=200)
        )
        report = DoctorReport(providers=[_prov("copilot", installed=True)])
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor"])
        assert result.exception is None
        assert result.exit_code == 0
        assert "✓" in stream.getvalue()
        assert "OK" not in stream.getvalue()


class TestDoctorTierAndModelsErrorCells:
    """Two more per-cell branches driven by the same glyph set as the
    encoding-fallback tests above, exercised on the default UTF-8 path."""

    def test_missing_tier_renders_dash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Credentials and note are filled so the tier cell is the only one
        # that can render a dash; with _prov's defaults both of those cells
        # dash too, and the assertion holds even without the tier=None
        # branch under test (caught by review on #469).
        report = DoctorReport(
            providers=[
                _prov(
                    "copilot",
                    tier=None,
                    creds=[CredentialEnvVar(name="COPILOT_TOKEN", present=True)],
                    note="see docs",
                )
            ]
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "—" in result.output

    def test_models_error_renders_cross_with_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = DoctorReport(
            providers=[
                _prov("copilot", checked=True, connection_ok=True, models_error="rate limited")
            ]
        )
        _patch_gather(monkeypatch, report)
        result = runner.invoke(app, ["doctor", "--models"])
        assert result.exit_code == 0
        assert "rate limited" in result.output
        assert "✗" in result.output
