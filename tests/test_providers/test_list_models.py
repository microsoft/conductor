"""Tests for the public ``list_models()`` provider method (issue #274).

Covers the base default (``None``) plus the Copilot and Claude overrides,
including their never-raise behavior at the SDK boundary.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from conductor.providers.base import AgentProvider
from conductor.providers.claude import ClaudeProvider
from conductor.providers.copilot import CopilotProvider


class _StubProvider(AgentProvider, abstract=True):
    """Minimal concrete provider to exercise the base ``list_models`` default."""

    async def execute(self, *args: Any, **kwargs: Any) -> Any:  # noqa: D102
        raise NotImplementedError

    async def validate_connection(self) -> bool:  # noqa: D102
        return True

    async def close(self) -> None:  # noqa: D102
        return None


class TestBaseListModels:
    """The base implementation returns ``None`` (no enumeration)."""

    async def test_default_returns_none(self) -> None:
        provider = _StubProvider()
        assert await provider.list_models() is None


class TestCopilotListModels:
    """Copilot enumerates model ids via ``client.list_models()``."""

    async def test_returns_model_ids(self) -> None:
        provider = CopilotProvider()
        provider._ensure_client_started = AsyncMock()  # type: ignore[method-assign]
        fake_client = SimpleNamespace(
            list_models=AsyncMock(
                return_value=[SimpleNamespace(id="gpt-5"), SimpleNamespace(id="claude-x")]
            )
        )
        provider._client = fake_client  # type: ignore[assignment]

        assert await provider.list_models() == ["gpt-5", "claude-x"]

    async def test_mock_handler_mode_returns_none(self) -> None:
        provider = CopilotProvider(mock_handler=lambda *a, **k: {})
        assert await provider.list_models() is None

    async def test_sdk_unavailable_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("conductor.providers.copilot.COPILOT_SDK_AVAILABLE", False)
        provider = CopilotProvider()
        assert await provider.list_models() is None

    async def test_sdk_error_returns_none(self) -> None:
        provider = CopilotProvider()
        provider._ensure_client_started = AsyncMock()  # type: ignore[method-assign]
        fake_client = SimpleNamespace(list_models=AsyncMock(side_effect=RuntimeError("boom")))
        provider._client = fake_client  # type: ignore[assignment]

        assert await provider.list_models() is None


class TestClaudeListModels:
    """Claude enumerates model ids via ``client.models.list()``."""

    async def test_returns_model_ids(self) -> None:
        provider = ClaudeProvider(api_key="test-key")
        page = SimpleNamespace(
            data=[SimpleNamespace(id="claude-a"), SimpleNamespace(id="claude-b")]
        )
        provider._client = SimpleNamespace(  # type: ignore[assignment]
            models=SimpleNamespace(list=AsyncMock(return_value=page))
        )

        assert await provider.list_models() == ["claude-a", "claude-b"]

    async def test_client_none_returns_none(self) -> None:
        provider = ClaudeProvider(api_key="test-key")
        provider._client = None
        assert await provider.list_models() is None

    async def test_sdk_error_returns_none(self) -> None:
        provider = ClaudeProvider(api_key="test-key")
        provider._client = SimpleNamespace(  # type: ignore[assignment]
            models=SimpleNamespace(list=AsyncMock(side_effect=RuntimeError("boom")))
        )
        assert await provider.list_models() is None

    async def test_sdk_unavailable_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = ClaudeProvider(api_key="test-key")
        monkeypatch.setattr("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", False)
        assert await provider.list_models() is None
