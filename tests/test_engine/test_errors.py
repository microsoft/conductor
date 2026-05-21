"""Tests for ``conductor.engine.errors`` and the new exception classes."""

from __future__ import annotations

import pytest

from conductor.engine.errors import (
    EnvelopeValidationError,
    ErrorEnvelope,
    coerce_envelope,
    make_schema_violation,
    make_script_error,
    wrap_undeclared_kind,
)
from conductor.exceptions import (
    ConductorError,
    UnhandledNodeError,
    UnhandledWorkflowError,
)


class TestCoerceEnvelope:
    """Tests for :func:`coerce_envelope`."""

    def test_minimal_envelope(self) -> None:
        env = coerce_envelope({"kind": "external.git.fetch_failed", "message": "boom"})
        assert env["kind"] == "external.git.fetch_failed"
        assert env["message"] == "boom"
        assert env["details"] == {}

    def test_full_envelope(self) -> None:
        env = coerce_envelope(
            {
                "kind": "external.git.fetch_failed",
                "message": "boom",
                "details": {"exit_code": 128},
            }
        )
        assert env["details"] == {"exit_code": 128}

    def test_strips_conductor_error_discriminator(self) -> None:
        """The on-the-wire ``conductor_error: true`` discriminator does
        not survive into the internal envelope shape."""
        env = coerce_envelope(
            {
                "conductor_error": True,
                "kind": "external.git.fetch_failed",
                "message": "boom",
            }
        )
        assert "conductor_error" not in env

    def test_details_none_becomes_empty_dict(self) -> None:
        env = coerce_envelope({"kind": "x.y", "message": "m", "details": None})
        assert env["details"] == {}

    def test_non_dict_raw_rejected(self) -> None:
        with pytest.raises(EnvelopeValidationError) as exc:
            coerce_envelope("not a dict")
        assert "JSON object" in str(exc.value)

    def test_missing_kind_rejected(self) -> None:
        with pytest.raises(EnvelopeValidationError):
            coerce_envelope({"message": "m"})

    def test_malformed_kind_rejected(self) -> None:
        with pytest.raises(EnvelopeValidationError):
            coerce_envelope({"kind": "Oops", "message": "m"})

    def test_missing_message_rejected(self) -> None:
        with pytest.raises(EnvelopeValidationError):
            coerce_envelope({"kind": "x.y"})

    def test_empty_message_rejected(self) -> None:
        with pytest.raises(EnvelopeValidationError):
            coerce_envelope({"kind": "x.y", "message": ""})

    def test_non_dict_details_rejected(self) -> None:
        with pytest.raises(EnvelopeValidationError):
            coerce_envelope({"kind": "x.y", "message": "m", "details": "not a dict"})


class TestMakeScriptError:
    """Tests for :func:`make_script_error`."""

    def test_basic(self) -> None:
        env = make_script_error(
            exit_code=128,
            stderr_tail="fatal: could not resolve host",
            command="git",
        )
        assert env["kind"] == "internal.script_error"
        assert "fatal: could not resolve host" in env["message"]
        assert env["details"]["exit_code"] == 128
        assert env["details"]["command"] == "git"

    def test_empty_stderr_uses_exit_code(self) -> None:
        env = make_script_error(exit_code=42, stderr_tail="", command="x")
        assert "42" in env["message"]


class TestMakeSchemaViolation:
    def test_basic(self) -> None:
        env = make_schema_violation(
            node_name="extractor",
            source="agent",
            original_message="missing field 'answer'",
        )
        assert env["kind"] == "internal.schema_violation"
        assert env["details"]["node"] == "extractor"
        assert env["details"]["source"] == "agent"

    def test_with_failed_field(self) -> None:
        env = make_schema_violation(
            node_name="extractor",
            source="agent",
            original_message="bad",
            failed_field="answer",
        )
        assert env["details"]["failed_field"] == "answer"


class TestWrapUndeclaredKind:
    def test_preserves_original(self) -> None:
        original = ErrorEnvelope(
            kind="external.git.fetch_failed",
            message="boom",
            details={"exit_code": 128},
        )
        wrapped = wrap_undeclared_kind(original, declared=["external.git.push_failed"])
        assert wrapped["kind"] == "internal.undeclared_kind"
        assert wrapped["details"]["original_kind"] == "external.git.fetch_failed"
        assert wrapped["details"]["original_message"] == "boom"
        assert wrapped["details"]["original_details"] == {"exit_code": 128}
        assert wrapped["details"]["declared"] == ["external.git.push_failed"]


class TestExceptions:
    """Tests for the new exception classes."""

    def test_unhandled_node_error_carries_envelope(self) -> None:
        env: ErrorEnvelope = {"kind": "x.y", "message": "m", "details": {}}
        exc = UnhandledNodeError(env, node_name="step1")
        assert exc.envelope is env
        assert exc.node_name == "step1"
        assert isinstance(exc, ConductorError)
        assert "step1" in str(exc)
        assert "x.y" in str(exc)

    def test_unhandled_workflow_error_carries_envelope_and_frames(self) -> None:
        env: ErrorEnvelope = {"kind": "x.y", "message": "m", "details": {}}
        frames = [{"node": "step1", "workflow": "root", "type": "agent"}]
        exc = UnhandledWorkflowError(env, frames=frames)
        assert exc.envelope is env
        assert exc.frames == frames
        assert isinstance(exc, ConductorError)
        assert "step1" in str(exc)
        assert "x.y" in str(exc)
        assert "m" in str(exc)

    def test_unhandled_workflow_error_empty_frames(self) -> None:
        """Empty frames shouldn't crash; should produce a defensible message."""
        env: ErrorEnvelope = {"kind": "x.y", "message": "m", "details": {}}
        exc = UnhandledWorkflowError(env, frames=[])
        assert "<unknown>" in str(exc)
