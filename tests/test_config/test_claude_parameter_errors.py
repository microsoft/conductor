"""Tests for error handling of configuration parameters.

Tests validation of temperature and max_tokens ranges and types.
Note: top_p, top_k, stop_sequences, and metadata have been removed as they
were Claude-specific parameters not supported by both providers.
"""

import pytest
from pydantic import ValidationError

from conductor.config.schema import RuntimeConfig


def test_temperature_out_of_range_low():
    """Verify temperature < 0 raises validation error."""
    with pytest.raises(ValidationError) as exc_info:
        RuntimeConfig(
            provider="claude",
            default_model="claude-3-5-sonnet-20241022",
            temperature=-0.1
        )

    errors = exc_info.value.errors()
    assert any("temperature" in str(e.get("loc", [])) for e in errors)


def test_temperature_out_of_range_high():
    """Verify temperature > 1 raises validation error."""
    with pytest.raises(ValidationError) as exc_info:
        RuntimeConfig(
            provider="claude",
            default_model="claude-3-5-sonnet-20241022",
            temperature=1.1
        )

    errors = exc_info.value.errors()
    assert any("temperature" in str(e.get("loc", [])) for e in errors)


def test_temperature_at_boundaries():
    """Verify temperature at 0.0 and 1.0 boundaries is valid."""
    # Temperature = 0.0 should be valid
    config = RuntimeConfig(
        provider="claude",
        default_model="claude-3-5-sonnet-20241022",
        temperature=0.0
    )
    assert config.temperature == 0.0

    # Temperature = 1.0 should be valid
    config = RuntimeConfig(
        provider="claude",
        default_model="claude-3-5-sonnet-20241022",
        temperature=1.0
    )
    assert config.temperature == 1.0


def test_max_tokens_negative():
    """Verify negative max_tokens raises validation error."""
    with pytest.raises(ValidationError) as exc_info:
        RuntimeConfig(
            provider="claude",
            default_model="claude-3-5-sonnet-20241022",
            max_tokens=-1
        )

    errors = exc_info.value.errors()
    assert any("max_tokens" in str(e.get("loc", [])) for e in errors)


def test_max_tokens_zero():
    """Verify max_tokens=0 raises validation error."""
    with pytest.raises(ValidationError) as exc_info:
        RuntimeConfig(
            provider="claude",
            default_model="claude-3-5-sonnet-20241022",
            max_tokens=0
        )

    errors = exc_info.value.errors()
    assert any("max_tokens" in str(e.get("loc", [])) for e in errors)


def test_max_tokens_valid():
    """Verify positive max_tokens is accepted."""
    config = RuntimeConfig(
        provider="claude",
        default_model="claude-3-5-sonnet-20241022",
        max_tokens=4096
    )
    assert config.max_tokens == 4096


def test_all_runtime_parameters_together():
    """Verify all common runtime parameters can be set together."""
    config = RuntimeConfig(
        provider="claude",
        default_model="claude-3-5-sonnet-20241022",
        temperature=0.7,
        max_tokens=2048,
        timeout=120.0,
    )

    assert config.temperature == 0.7
    assert config.max_tokens == 2048
    assert config.timeout == 120.0


def test_parameters_default_to_none():
    """Verify optional parameters default to None when not specified."""
    config = RuntimeConfig(
        provider="copilot",
        default_model="gpt-4"
    )

    assert config.temperature is None
    assert config.max_tokens is None
    assert config.timeout is None
