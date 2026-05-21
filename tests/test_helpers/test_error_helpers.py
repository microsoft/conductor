"""Tests for the Python flavour of the engine-agnostic error helpers.

These confirm that ``conductor.helpers.error.conductor_error.raise_kind``:

* writes the expected envelope shape to ``$CONDUCTOR_ERROR_OUT``;
* refuses to run when the env var is unset (so a script accidentally
  executed outside of a Conductor script-node fails loudly instead of
  silently dropping the envelope);
* omits the ``details`` key when no details are passed (so the
  envelope round-trips cleanly through
  :func:`conductor.engine.errors.coerce_envelope`).

The other-language helpers (psm1, sh, mjs, cs) are exercised by the
cross-engine integration test in Step 13. They don't get unit tests
here because they're not Python.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conductor.engine.errors import coerce_envelope
from conductor.helpers.error import conductor_error


class TestRaiseKind:
    def test_writes_envelope_with_required_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = tmp_path / "envelope.json"
        monkeypatch.setenv("CONDUCTOR_ERROR_OUT", str(out))

        returned = conductor_error.raise_kind("external.git.fetch_failed", "remote rejected")

        assert returned == out
        loaded = json.loads(out.read_text(encoding="utf-8"))
        assert loaded == {
            "conductor_error": True,
            "kind": "external.git.fetch_failed",
            "message": "remote rejected",
        }
        envelope = coerce_envelope(loaded)
        assert envelope["kind"] == "external.git.fetch_failed"
        assert envelope["message"] == "remote rejected"

    def test_includes_details_when_provided(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = tmp_path / "envelope.json"
        monkeypatch.setenv("CONDUCTOR_ERROR_OUT", str(out))

        conductor_error.raise_kind(
            "external.git.drift",
            "SHA mismatch",
            details={"expected": "abc", "actual": "def"},
        )

        loaded = json.loads(out.read_text(encoding="utf-8"))
        assert loaded["details"] == {"expected": "abc", "actual": "def"}

    def test_raises_when_env_var_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CONDUCTOR_ERROR_OUT", raising=False)

        with pytest.raises(RuntimeError, match="CONDUCTOR_ERROR_OUT is not set"):
            conductor_error.raise_kind("x.y", "msg")

    def test_does_not_call_sys_exit(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Helper must not exit the process itself — callers stay in charge."""
        out = tmp_path / "envelope.json"
        monkeypatch.setenv("CONDUCTOR_ERROR_OUT", str(out))

        sentinel = 0
        conductor_error.raise_kind("x.y", "msg")
        sentinel = 1
        assert sentinel == 1

    def test_helper_files_are_packaged(self) -> None:
        """All five language helpers + README ship under the wheel package dir."""
        pkg = Path(conductor_error.__file__).parent
        for name in (
            "Conductor.Error.psm1",
            "conductor-error.sh",
            "conductor_error.py",
            "conductor-error.mjs",
            "ConductorError.cs",
            "README.md",
        ):
            assert (pkg / name).is_file(), f"missing helper file: {name}"

    def test_env_var_is_read_per_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression: the helper reads the env var each call, not at import time."""
        monkeypatch.setenv("CONDUCTOR_ERROR_OUT", os.path.join(os.path.sep, "tmp", "first"))
        monkeypatch.delenv("CONDUCTOR_ERROR_OUT", raising=False)
        with pytest.raises(RuntimeError):
            conductor_error.raise_kind("x.y", "msg")
