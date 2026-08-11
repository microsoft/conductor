"""Smoke tests for the dependency surface required by Pydantic AI providers.

These tests guard the module-level import contract that the rest of the
provider stack relies on.  The extras on ``pydantic-ai-slim[anthropic,openai]``
determine whether the model/provider classes below are importable at all;
``openai`` must be pinned to a 2.x API compatible with ``AsyncOpenAI(...,
max_retries=0)`` construction.  A regression here would surface as an
ImportError or TypeError long before any provider unit test runs, so this
module acts as an early warning.
"""

from __future__ import annotations

from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

from conductor.providers.claude import ClaudeProvider


def test_import_openai_chat_model_settings() -> None:
    """OpenAIChatModelSettings must exist in the installed pydantic-ai-slim."""
    assert OpenAIChatModelSettings is not None


def test_import_openai_chat_model() -> None:
    """OpenAIChatModel must be importable from the openai extra."""
    assert OpenAIChatModel is not None


def test_import_openai_provider() -> None:
    """The OpenAIProvider pydantic-ai shim must be importable."""
    assert OpenAIProvider is not None


def test_import_claude_provider() -> None:
    """ClaudeProvider must still import cleanly after the dependency swap."""
    assert ClaudeProvider is not None


def test_openai_async_client_construction() -> None:
    """AsyncOpenAI accepts explicit max_retries=0 on the pinned 2.x API."""
    client = AsyncOpenAI(api_key="dummy", max_retries=0)
    assert client.max_retries == 0


def test_openai_chat_model_settings_has_reasoning_field() -> None:
    """OpenAIChatModelSettings exposes the reasoning-effort field we use."""
    settings = OpenAIChatModelSettings(openai_reasoning_effort="low")
    assert settings.get("openai_reasoning_effort") == "low"
