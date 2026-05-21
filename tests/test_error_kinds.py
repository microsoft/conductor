"""Tests for the kind constants and helpers in ``conductor.error_kinds``."""

from __future__ import annotations

import pytest

from conductor.error_kinds import (
    KIND_PATTERN,
    RESERVED_KIND_PREFIXES,
    RESERVED_ON_ERROR_ALLOWLIST,
    is_reserved_prefix,
)


class TestKindPattern:
    """Tests for the KIND_PATTERN regex."""

    @pytest.mark.parametrize(
        "kind",
        [
            "external.git.fetch_failed",
            "policy.budget",
            "a.b",
            "_private.x",
            "x.y.z.aa",
            "x.y_1.z2",
        ],
    )
    def test_valid_kinds(self, kind: str) -> None:
        assert KIND_PATTERN.match(kind) is not None

    @pytest.mark.parametrize(
        "kind",
        [
            "",
            "oops",  # no dot
            "External.Git",  # uppercase
            ".leading",
            "trailing.",
            "double..dot",
            "1starts_with_digit.x",
            "x.1starts_with_digit",
            "x-y.z",  # hyphen not allowed
            "x y.z",  # space not allowed
        ],
    )
    def test_invalid_kinds(self, kind: str) -> None:
        assert KIND_PATTERN.match(kind) is None


class TestReservedPrefix:
    """Tests for ``is_reserved_prefix`` and the prefix tuple."""

    @pytest.mark.parametrize(
        "kind",
        [
            "internal.script_error",
            "internal.schema_violation",
            "internal.undeclared_kind",
            "provider.exhausted",
            "subworkflow.failed",
            "retry.exhausted",
        ],
    )
    def test_reserved_kinds_detected(self, kind: str) -> None:
        assert is_reserved_prefix(kind)

    @pytest.mark.parametrize(
        "kind",
        [
            "external.git",
            "internal_x.y",  # underscore not a dot — not reserved
            "providers.x",  # plural is fine
            "subworkflows.x",
        ],
    )
    def test_non_reserved_kinds(self, kind: str) -> None:
        assert not is_reserved_prefix(kind)

    def test_all_reserved_prefixes_end_with_dot(self) -> None:
        """A prefix without the trailing dot would false-positive
        on flat identifiers like ``internalstuff``."""
        for prefix in RESERVED_KIND_PREFIXES:
            assert prefix.endswith(".")


class TestReservedOnErrorAllowlist:
    """Tests for the allowlist of runtime kinds matchable in ``on_error``."""

    def test_allowlist_entries_are_reserved(self) -> None:
        """Every allowlisted kind must itself live under a reserved
        prefix — otherwise the matrix is inconsistent."""
        for kind in RESERVED_ON_ERROR_ALLOWLIST:
            assert is_reserved_prefix(kind), (
                f"allowlist entry {kind!r} is not under a reserved prefix"
            )

    def test_allowlist_is_frozenset(self) -> None:
        """Allowlist is immutable — callers shouldn't mutate it."""
        assert isinstance(RESERVED_ON_ERROR_ALLOWLIST, frozenset)
