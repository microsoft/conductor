"""Unit tests for ``model_listing.extract_model_token_limits``.

These tests pin the vendor field shapes that the Anthropic-compatible
model-listing parser must accept without raising.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import override

import pytest

from conductor.providers._pydantic_ai.model_listing import (
    ModelTokenLimits,
    extract_model_token_limits,
)


class _HostileMetadataError(Exception):
    """Test-only failure from an inaccessible metadata value."""


class _ContainsFailureMapping(Mapping[str, None]):
    """Mapping double that raises while checking key presence."""

    @override
    def __contains__(self, key: object) -> bool:
        raise _HostileMetadataError("contains failed")

    @override
    def __getitem__(self, key: str) -> None:
        return None

    @override
    def __iter__(self) -> Iterator[str]:
        return iter(())

    @override
    def __len__(self) -> int:
        return 0


class _GetitemFailureMapping(Mapping[str, None]):
    """Mapping double that raises while retrieving a present key."""

    @override
    def __contains__(self, key: object) -> bool:
        return key == "max_input_tokens"

    @override
    def __getitem__(self, key: str) -> None:
        raise _HostileMetadataError("getitem failed")

    @override
    def __iter__(self) -> Iterator[str]:
        return iter(())

    @override
    def __len__(self) -> int:
        return 0


class _RaisingInputLimit:
    """Object double with an inaccessible input-limit property."""

    @property
    def max_input_tokens(self) -> int:
        raise _HostileMetadataError("input limit failed")


class _TopProvider:
    """Attribute-shaped nested provider metadata."""

    max_completion_tokens: int = 8192


class _AttributeTopProviderModel:
    """Object double that exposes nested provider metadata as an attribute."""

    top_provider: _TopProvider = _TopProvider()


def test_omniroute_shape() -> None:
    """OmniRoute uses ``max_input_tokens`` and ``max_output_tokens``."""
    entry = {
        "id": "claude-3-5-sonnet-20241022",
        "max_input_tokens": 200_000,
        "max_output_tokens": 8192,
    }
    assert extract_model_token_limits(entry) == ModelTokenLimits(
        max_input_tokens=200_000,
        max_output_tokens=8192,
    )


def test_litellm_shape() -> None:
    """LiteLLM uses ``max_input_tokens`` and ``max_output_tokens``."""
    entry = {
        "model_name": "claude-3-opus-20240229",
        "max_input_tokens": 200_000,
        "max_output_tokens": 4096,
    }
    assert extract_model_token_limits(entry) == ModelTokenLimits(
        max_input_tokens=200_000,
        max_output_tokens=4096,
    )


def test_vllm_shape() -> None:
    """vLLM uses ``max_model_len`` as the context window."""
    entry = {
        "id": "meta-llama/Llama-3.1-8B-Instruct",
        "max_model_len": 131_072,
    }
    assert extract_model_token_limits(entry) == ModelTokenLimits(
        max_input_tokens=131_072,
        max_output_tokens=None,
    )


def test_openrouter_shape() -> None:
    """OpenRouter nests output limit under ``top_provider.max_completion_tokens``."""
    entry = {
        "id": "anthropic/claude-3.5-sonnet",
        "context_length": 200_000,
        "top_provider": {"max_completion_tokens": 8192},
    }
    assert extract_model_token_limits(entry) == ModelTokenLimits(
        max_input_tokens=200_000,
        max_output_tokens=8192,
    )


def test_lm_studio_shape() -> None:
    """LM Studio exposes ``max_context_length`` and ``max_output_tokens``."""
    entry = {
        "id": "qwen2.5-7b-instruct",
        "max_context_length": 131_072,
        "max_output_tokens": 8192,
    }
    assert extract_model_token_limits(entry) == ModelTokenLimits(
        max_input_tokens=131_072,
        max_output_tokens=8192,
    )


def test_bare_openai_shape() -> None:
    """Bare OpenAI listing uses ``max_output_tokens`` only."""
    entry = {"id": "gpt-4o", "max_output_tokens": 4096}
    assert extract_model_token_limits(entry) == ModelTokenLimits(
        max_input_tokens=None,
        max_output_tokens=4096,
    )


def test_object_with_attributes() -> None:
    """Parser also accepts objects exposing the same fields as attributes."""

    class FakeModel:
        max_input_tokens = 128_000
        max_output_tokens = 4096

    assert extract_model_token_limits(FakeModel()) == ModelTokenLimits(
        max_input_tokens=128_000,
        max_output_tokens=4096,
    )


def test_input_priority_max_input_tokens_wins() -> None:
    """``max_input_tokens`` takes priority over other input keys."""
    entry = {
        "max_input_tokens": 100,
        "context_length": 200,
        "max_model_len": 300,
        "max_context_length": 400,
    }
    assert extract_model_token_limits(entry).max_input_tokens == 100


def test_input_priority_context_length_second() -> None:
    """``context_length`` is used when ``max_input_tokens`` is absent."""
    entry = {
        "context_length": 200,
        "max_model_len": 300,
        "max_context_length": 400,
    }
    assert extract_model_token_limits(entry).max_input_tokens == 200


def test_input_priority_max_model_len_third() -> None:
    """``max_model_len`` is used when higher-priority keys are absent."""
    entry = {"max_model_len": 300, "max_context_length": 400}
    assert extract_model_token_limits(entry).max_input_tokens == 300


def test_input_priority_max_context_length_last() -> None:
    """``max_context_length`` is used when no other input key is present."""
    entry = {"max_context_length": 400}
    assert extract_model_token_limits(entry).max_input_tokens == 400


@pytest.mark.parametrize(
    ("value",),
    [
        ("not a number",),
        (-1,),
        (0,),
        (True,),
        (3.14,),
        ({"nested": 1},),
        ([1, 2, 3],),
    ],
)
def test_garbage_values_are_ignored(value: object) -> None:
    """Malformed or non-positive values are treated as absent, never raised."""
    entry = {"max_input_tokens": value, "max_output_tokens": value}
    assert extract_model_token_limits(entry) == ModelTokenLimits(
        max_input_tokens=None,
        max_output_tokens=None,
    )


def test_integral_float_accepted() -> None:
    """Floats representing an integral positive value are accepted."""
    entry = {"max_input_tokens": 128000.0, "max_output_tokens": 4096.0}
    assert extract_model_token_limits(entry) == ModelTokenLimits(
        max_input_tokens=128000,
        max_output_tokens=4096,
    )


def test_non_integral_float_rejected() -> None:
    """Floats that are not whole numbers are rejected."""
    entry = {"max_input_tokens": 128000.5}
    assert extract_model_token_limits(entry).max_input_tokens is None


def test_none_and_missing_fields_return_none() -> None:
    """Absent or ``None`` fields produce ``None`` limits without errors."""
    entry = {"id": "unknown"}
    assert extract_model_token_limits(entry) == ModelTokenLimits()


def test_non_mapping_non_object_input() -> None:
    """The parser never raises, even for completely unexpected types."""
    assert extract_model_token_limits(None) == ModelTokenLimits()
    assert extract_model_token_limits("string") == ModelTokenLimits()
    assert extract_model_token_limits(12345) == ModelTokenLimits()


def test_contains_failure_mapping_is_treated_as_missing() -> None:
    """A mapping that rejects membership checks yields unknown limits."""
    # Requirement: hostile Mapping membership checks must not escape the parser.
    assert extract_model_token_limits(_ContainsFailureMapping()) == ModelTokenLimits()


def test_getitem_failure_mapping_is_treated_as_missing() -> None:
    """A mapping that rejects value reads yields unknown limits."""
    # Requirement: hostile Mapping value reads must not escape the parser.
    assert extract_model_token_limits(_GetitemFailureMapping()) == ModelTokenLimits()


def test_raising_attribute_is_treated_as_missing() -> None:
    """An object property that raises yields unknown limits."""
    # Requirement: hostile model properties must not escape the parser.
    assert extract_model_token_limits(_RaisingInputLimit()) == ModelTokenLimits()


def test_top_provider_attribute_object_supplies_output_limit() -> None:
    """Attribute-shaped nested provider metadata exposes the output limit."""
    # Requirement: top_provider attribute objects provide max_completion_tokens.
    assert extract_model_token_limits(_AttributeTopProviderModel()) == ModelTokenLimits(
        max_output_tokens=8192
    )
