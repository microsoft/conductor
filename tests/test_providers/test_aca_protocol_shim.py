"""Deprecation shim gates for ``conductor.providers.aca_protocol`` (issue #527).

After the wire protocol was lifted into ``conductor.runner.protocol``,
``conductor.providers.aca_protocol`` survives only as a deprecated re-export
shim for external importers. These tests pin:

1. an external importer (a fresh interpreter, as in
   ``tests/test_integration/test_install_script_extras.py``) sees the
   ``DeprecationWarning`` on import;
2. every legacy alias is the identical canonical object (no stale copies);
3. the aliases keep their behavior — the redaction validator and the
   ACA-specific error subclass both work through the shim.

The shim is the only in-repo importer of itself: pytest shows
``DeprecationWarning`` by default, so any other test importing it would fill
the full-suite output with warnings.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from pydantic import SecretStr

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

import conductor.providers.aca_protocol as shim  # noqa: E402
from conductor.providers.aca import AcaGatewayErrorData  # noqa: E402
from conductor.runner.protocol import (  # noqa: E402
    RUNNER_TOKEN_HEADER,
    RunnerAgentPayload,
    RunnerAgentRequest,
    RunnerAgentResult,
    RunnerErrorData,
    RunnerEventFrame,
)


def test_subprocess_import_warns_external_importer() -> None:
    """Importing the shim from an external process must warn but succeed.

    Requirement: the deprecation warning names the shim and its replacement
    and is visible to importers running with ``-W always::DeprecationWarning``
    (the default-filter upgrade an external consumer opts into), and the
    import itself stays functional until the shim is removed.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-W",
            "always::DeprecationWarning",
            "-c",
            "import conductor.providers.aca_protocol",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "aca_protocol is deprecated" in result.stderr
    assert "conductor.runner.protocol" in result.stderr


def test_no_deprecation_warning_when_importing_canonical_path() -> None:
    """Canonical modules must not raise DeprecationWarning (negative control).

    Requirement: nothing inside the repository goes through the shim, so
    importing the canonical protocol module and its consumers stays silent
    even under ``-W error::DeprecationWarning``.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::DeprecationWarning",
            "-c",
            "import conductor.runner.protocol; "
            "import conductor.providers.aca; "
            "import conductor.aca_runner; "
            "print('clean')",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


class TestAliasIdentity:
    """Every legacy alias must be the identical canonical object.

    Requirement: the shim re-exports, not copies — identity guarantees that
    behavioral fixes (validators, field changes) land on both names at once
    and that wire-format compatibility tests against the canonical module
    cover the alias too.
    """

    # Requirement: AcaExecuteRequest is the identical canonical
    # RunnerAgentRequest object so behavioral fixes land on both names.
    def test_execute_request_alias(self) -> None:
        assert shim.AcaExecuteRequest is RunnerAgentRequest

    # Requirement: AcaAgentPayload is the identical canonical
    # RunnerAgentPayload object so behavioral fixes land on both names.
    def test_agent_payload_alias(self) -> None:
        assert shim.AcaAgentPayload is RunnerAgentPayload

    # Requirement: AcaEventFrame is the identical canonical
    # RunnerEventFrame object so behavioral fixes land on both names.
    def test_event_frame_alias(self) -> None:
        assert shim.AcaEventFrame is RunnerEventFrame

    # Requirement: AcaResultData is the identical canonical
    # RunnerAgentResult object so behavioral fixes land on both names.
    def test_result_data_alias(self) -> None:
        assert shim.AcaResultData is RunnerAgentResult

    # Requirement: AcaErrorData is the identical canonical
    # AcaGatewayErrorData object so behavioral fixes land on both names.
    def test_error_data_alias(self) -> None:
        assert shim.AcaErrorData is AcaGatewayErrorData

    def test_runner_error_data_is_reexported(self) -> None:
        # Requirement: the neutral error base stays importable through the
        # shim's __all__ so `from ... import *` reaches it.
        assert shim.RunnerErrorData is RunnerErrorData

    # Requirement: the transport-token header name is unchanged by the lift
    # (exact wire value, shared by host and runner).
    def test_runner_token_header_unchanged(self) -> None:
        assert shim.RUNNER_TOKEN_HEADER == "X-Conductor-Runner-Token"
        assert shim.RUNNER_TOKEN_HEADER == RUNNER_TOKEN_HEADER

    def test_all_covers_every_exported_name(self) -> None:
        # Requirement: __all__ lists exactly the legacy names plus the
        # re-exported neutral base — no more, no less.
        assert set(shim.__all__) == {
            "RUNNER_TOKEN_HEADER",
            "AcaAgentPayload",
            "AcaExecuteRequest",
            "AcaEventFrame",
            "AcaResultData",
            "AcaErrorData",
            "RunnerErrorData",
        }


class TestAliasBehavior:
    """Aliases must behave identically to the canonical objects.

    Requirement: constructing through the alias validates like the canonical
    model — a plaintext credential gets wrapped in ``SecretStr`` by the
    redaction validator, and the ACA error alias preserves the ``code`` /
    ``traceId`` subclass behavior.
    """

    def test_execute_request_alias_wraps_plaintext_credential(self) -> None:
        # Requirement: the redaction validator runs on every validated
        # construction path — a dict validated through the legacy alias must
        # not retain a plaintext ``github_token``.
        request = shim.AcaExecuteRequest.model_validate(
            {
                "agent": {"name": "reviewer"},
                "rendered_prompt": "hello",
                "inner_provider_settings": {"github_token": "ghp_secret"},
            }
        )
        settings = request.inner_provider_settings
        assert settings is not None
        token = settings["github_token"]
        assert isinstance(token, SecretStr)
        assert token.get_secret_value() == "ghp_secret"

    def test_error_data_alias_preserves_aca_fields(self) -> None:
        # Requirement: the alias points at the ACA subclass, so a legacy
        # error body carrying the platform's diagnostic identifiers parses
        # ``code`` and the ``traceId`` alias through to ``trace_id``.
        error = shim.AcaErrorData.model_validate({"message": "m", "code": "c", "traceId": "t"})
        assert error.message == "m"
        assert error.code == "c"
        assert error.trace_id == "t"
