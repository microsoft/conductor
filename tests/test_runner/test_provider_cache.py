"""Reservation and configuration identity of the remote provider cache."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from conductor.aca_runner.server import _InnerProviderCache
from tests.test_runner.conftest import DeliveryProvider


@pytest.mark.parametrize("failure", [RuntimeError("eviction failed"), asyncio.CancelledError()])
async def test_failed_eviction_leaves_new_provider_unreserved(
    failure: BaseException,
    delivery_provider: type[DeliveryProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: failed or cancelled eviction cannot strand an active reservation.
    cache = _InnerProviderCache()
    monkeypatch.setattr(_InnerProviderCache, "_MAX_IDLE_ENTRIES", 1)
    first = await cache.get(mcp_servers=None, inner_provider_settings=None, tool_output=None)
    original_close = first.close
    monkeypatch.setattr(first, "close", AsyncMock(side_effect=failure))

    with pytest.raises(type(failure)):
        await cache.get(
            mcp_servers=None,
            inner_provider_settings={"base_url": "https://second.example.test"},
            tool_output=None,
            reserve=True,
        )

    assert len(cache._entries) == 2
    assert any(entry.provider is first and entry.active == 0 for entry in cache._entries.values())
    assert all(entry.active == 0 for entry in cache._entries.values())
    monkeypatch.setattr(first, "close", original_close)
    await asyncio.wait_for(cache.close(), timeout=1)
    assert delivery_provider.instances[0].closed


@pytest.mark.parametrize("cancel", [False, True])
async def test_eviction_keeps_provider_owned_while_close_fails_or_is_cancelled(
    cancel: bool, delivery_provider: type[DeliveryProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: an interrupted close leaves the provider owned for shutdown retry.
    monkeypatch.setattr(_InnerProviderCache, "_MAX_IDLE_ENTRIES", 1)
    cache = _InnerProviderCache()
    first = await cache.get(mcp_servers=None, inner_provider_settings=None, tool_output=None)
    original_close = first.close
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def interrupted_close() -> None:
        entered.set()
        await finish.wait()
        raise RuntimeError("close failed")

    monkeypatch.setattr(first, "close", interrupted_close)
    pending = asyncio.create_task(
        cache.get(
            mcp_servers=None,
            inner_provider_settings={"base_url": "https://second.example.test"},
            tool_output=None,
            reserve=True,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert any(entry.provider is first for entry in cache._entries.values())
    if cancel:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        finish.set()
        with pytest.raises(RuntimeError, match="close failed"):
            await pending
    assert len(cache._entries) == 2
    assert all(entry.active == 0 for entry in cache._entries.values())
    assert not delivery_provider.instances[0].closed
    monkeypatch.setattr(first, "close", original_close)
    await asyncio.wait_for(cache.close(), timeout=1)
    assert all(provider.closed for provider in delivery_provider.instances)


async def test_sampling_values_partition_cached_providers(
    delivery_provider: type[DeliveryProvider],
) -> None:
    # Requirement: different effective sampling settings cannot share a provider instance.
    cache = _InnerProviderCache()
    first = await cache.get(
        mcp_servers=None,
        inner_provider_settings={"api_key": "first"},
        tool_output=None,
        inner_provider="openai",
        temperature=0.2,
        max_tokens=512,
    )
    second = await cache.get(
        mcp_servers=None,
        inner_provider_settings={"api_key": "first"},
        tool_output=None,
        inner_provider="openai",
        temperature=0.3,
        max_tokens=512,
    )
    third = await cache.get(
        mcp_servers=None,
        inner_provider_settings={"api_key": "first"},
        tool_output=None,
        inner_provider="openai",
        temperature=0.2,
        max_tokens=1024,
    )
    assert len({id(first), id(second), id(third)}) == 3
    assert first is await cache.get(
        mcp_servers=None,
        inner_provider_settings={"api_key": "first"},
        tool_output=None,
        inner_provider="openai",
        temperature=0.2,
        max_tokens=512,
    )
    await cache.close()


async def test_release_notifies_shutdown_even_if_idle_close_fails(
    delivery_provider: type[DeliveryProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: release drops its active count before eviction can raise.
    monkeypatch.setattr(_InnerProviderCache, "_MAX_IDLE_ENTRIES", 1)
    cache = _InnerProviderCache()
    first = await cache.get(
        mcp_servers=None, inner_provider_settings=None, tool_output=None, reserve=True
    )
    second = await cache.get(
        mcp_servers=None,
        inner_provider_settings={"base_url": "https://second.example.test"},
        tool_output=None,
        reserve=True,
    )
    original_close = first.close
    monkeypatch.setattr(first, "close", AsyncMock(side_effect=RuntimeError("close failed")))
    with pytest.raises(RuntimeError, match="close failed"):
        await cache.release(first)
    assert any(entry.provider is first and entry.active == 0 for entry in cache._entries.values())
    monkeypatch.setattr(first, "close", original_close)
    await cache.release(second)
    await asyncio.wait_for(cache.close(), timeout=1)


async def test_shutdown_keeps_failed_entry_and_closes_others(
    delivery_provider: type[DeliveryProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: shutdown attempts all closes and retains failures for a later retry.
    cache = _InnerProviderCache()
    first = await cache.get(mcp_servers=None, inner_provider_settings=None, tool_output=None)
    await cache.get(
        mcp_servers=None,
        inner_provider_settings={"base_url": "https://second.example.test"},
        tool_output=None,
    )
    original_close = first.close
    monkeypatch.setattr(first, "close", AsyncMock(side_effect=RuntimeError("close failed")))
    with pytest.raises(ExceptionGroup, match="close failed"):
        await cache.close()
    assert not delivery_provider.instances[0].closed
    assert delivery_provider.instances[1].closed
    assert [entry.provider for entry in cache._entries.values()] == [first]
    monkeypatch.setattr(first, "close", original_close)
    await cache.close()
    assert delivery_provider.instances[0].closed
    assert not cache._entries
