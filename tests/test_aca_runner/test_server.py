"""Tests for the in-container `conductor-agent-runner` server (#284, epic E4).

Exercises `POST /execute` (NDJSON streaming, terminal `result` frame) and
`GET /health` against a mocked `CopilotProvider` — the real SDK spawns a
nested `copilot` process and is out of scope for these unit tests. See
docs/projects/aca/aca-provider.plan.md, epic E4.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from starlette.testclient import TestClient

from conductor.providers.base import AgentOutput


class _FakeCopilotProvider:
    """Stand-in for `conductor.providers.copilot.CopilotProvider`.

    Records every constructor call and every `execute()` call so tests can
    assert the runner forwards `mcp_servers`/`provider_settings`/`tool_output`
    at construction time and `agent`/`context`/`tools` at execute time.
    """

    instances: list[_FakeCopilotProvider] = []

    def __init__(
        self,
        mcp_servers: dict[str, Any] | None = None,
        provider_settings: Any | None = None,
        tool_output: Any | None = None,
        github_token: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.mcp_servers = mcp_servers
        self.provider_settings = provider_settings
        self.tool_output = tool_output
        self.github_token = github_token
        self.close = AsyncMock(return_value=None)
        self.execute_calls: list[dict[str, Any]] = []
        self.execute_error: Exception | None = None
        self._result = AgentOutput(
            content={"summary": "done"},
            raw_response=None,
            input_tokens=10,
            output_tokens=5,
            model="gpt-4.1",
        )
        _FakeCopilotProvider.instances.append(self)

    async def execute(
        self,
        agent: Any,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None = None,
        interrupt_signal: Any | None = None,
        event_callback: Any | None = None,
    ) -> AgentOutput:
        self.execute_calls.append(
            {
                "agent": agent,
                "context": context,
                "rendered_prompt": rendered_prompt,
                "tools": tools,
            }
        )
        if self.execute_error is not None:
            raise self.execute_error
        if event_callback is not None:
            event_callback("agent_turn_start", {"turn": "awaiting_model"})
            event_callback("agent_message", {"content": "hi"})
        return self._result


class _DelayedCloseFakeCopilotProvider(_FakeCopilotProvider):
    """`_FakeCopilotProvider` whose `close()` yields to the event loop.

    Used to widen the race window in `_InnerProviderCache` concurrency
    tests: without a real `await` suspension inside `close()`, two
    concurrent `get()` calls racing on the same stale cache entry might not
    reliably interleave under cooperative scheduling.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        async def _delayed_close() -> None:
            await asyncio.sleep(0.01)

        self.close = AsyncMock(side_effect=_delayed_close)


@pytest.fixture(autouse=True)
def _reset_fake_provider_instances() -> Any:
    _FakeCopilotProvider.instances = []
    yield
    _FakeCopilotProvider.instances = []


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr("conductor.aca_runner.server.CopilotProvider", _FakeCopilotProvider)
    from conductor.aca_runner.server import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def _execute_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "agent": {"name": "implement", "model": "gpt-4.1"},
        "rendered_prompt": "do the thing",
        "tools": ["git"],
        "mcp_servers": None,
        "context": {},
        "inner_provider": "copilot",
    }
    body.update(overrides)
    return body


def _parse_ndjson(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class TestHealth:
    """`GET /health` (E4-T1) — readiness + Conductor/runner version."""

    def test_health_reports_readiness_and_version(self, client: TestClient) -> None:
        from conductor import __version__ as conductor_version

        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is True
        assert body["conductor_version"] == conductor_version
        assert "runner_version" in body


class TestExecuteStreaming:
    """`POST /execute` (E4-T2) — NDJSON event frames + terminal `result`."""

    def test_execute_streams_events_and_terminal_result(self, client: TestClient) -> None:
        response = client.post("/execute", json=_execute_body())
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")

        frames = _parse_ndjson(response.text)
        assert frames[0] == {"type": "agent_turn_start", "data": {"turn": "awaiting_model"}}
        assert frames[1] == {"type": "agent_message", "data": {"content": "hi"}}
        result = frames[-1]
        assert result["type"] == "result"
        assert result["data"]["content"] == {"summary": "done"}
        assert result["data"]["model"] == "gpt-4.1"
        assert result["data"]["input_tokens"] == 10
        assert result["data"]["output_tokens"] == 5
        assert result["data"]["partial"] is False
        assert isinstance(result["data"]["session_seconds"], (int, float))
        assert result["data"]["session_seconds"] >= 0

    def test_execute_forwards_tools_and_mcp_servers_to_inner_provider(
        self, client: TestClient
    ) -> None:
        mcp_servers = {"git": {"type": "stdio", "command": "echo", "args": ["hi"], "tools": ["*"]}}
        response = client.post(
            "/execute",
            json=_execute_body(tools=["git", "read_file"], mcp_servers=mcp_servers),
        )
        assert response.status_code == 200
        _parse_ndjson(response.text)  # drain the stream

        assert len(_FakeCopilotProvider.instances) == 1
        instance = _FakeCopilotProvider.instances[0]
        assert instance.mcp_servers == mcp_servers
        assert len(instance.execute_calls) == 1
        assert instance.execute_calls[0]["tools"] == ["git", "read_file"]
        assert instance.execute_calls[0]["agent"].name == "implement"
        assert instance.execute_calls[0]["rendered_prompt"] == "do the thing"

    def test_execute_reuses_provider_across_calls_with_same_settings(
        self, client: TestClient
    ) -> None:
        client.post("/execute", json=_execute_body())
        client.post("/execute", json=_execute_body())
        assert len(_FakeCopilotProvider.instances) == 1

    def test_execute_reconstructs_provider_when_mcp_servers_change(
        self, client: TestClient
    ) -> None:
        client.post("/execute", json=_execute_body())
        client.post(
            "/execute",
            json=_execute_body(mcp_servers={"git": {"type": "stdio", "command": "echo"}}),
        )
        assert len(_FakeCopilotProvider.instances) == 2
        assert _FakeCopilotProvider.instances[0].close.await_count == 1


class TestExecuteMissingStdioBinary:
    """Runner-image contract (E4-T3): a missing stdio binary fails loudly."""

    def test_missing_stdio_binary_surfaces_as_runner_error(self, client: TestClient) -> None:
        mcp_servers = {
            "ghost": {
                "type": "stdio",
                "command": "definitely-not-a-real-binary-xyz",
                "args": [],
                "tools": ["*"],
            }
        }
        response = client.post("/execute", json=_execute_body(mcp_servers=mcp_servers))

        assert response.status_code >= 400
        body = response.json()
        message = body.get("error", body).get("message", "")
        assert "definitely-not-a-real-binary-xyz" in message
        # The provider must never be constructed/executed for a rejected
        # request — this is a hard failure, not a silent drop of the tool.
        assert _FakeCopilotProvider.instances == []

    def test_remote_mcp_server_does_not_require_a_binary(self, client: TestClient) -> None:
        mcp_servers = {"remote": {"type": "http", "url": "https://example.com/mcp", "tools": ["*"]}}
        response = client.post("/execute", json=_execute_body(mcp_servers=mcp_servers))
        assert response.status_code == 200


class TestExecuteInnerProviderCredentials:
    """OQ#6 stopgap (E4-T4): `inner_provider_settings` builds `ProviderSettings`."""

    def test_inner_provider_settings_build_copilot_provider_settings(
        self, client: TestClient
    ) -> None:
        response = client.post(
            "/execute",
            json=_execute_body(inner_provider_settings={"bearer_token": "stopgap-token-value"}),
        )
        assert response.status_code == 200
        _parse_ndjson(response.text)

        instance = _FakeCopilotProvider.instances[0]
        assert instance.provider_settings is not None
        assert instance.provider_settings.name == "copilot"
        assert instance.provider_settings.bearer_token.get_secret_value() == ("stopgap-token-value")

    def test_unsupported_inner_provider_is_rejected(self, client: TestClient) -> None:
        response = client.post("/execute", json=_execute_body(inner_provider="claude-agent-sdk"))
        assert response.status_code >= 400
        assert _FakeCopilotProvider.instances == []

    def test_github_token_forwarded_to_copilot_provider_with_no_provider_settings(
        self, client: TestClient
    ) -> None:
        """Epic E9: a `github_token`-only credential (Copilot-capacity auth,
        DD4) is forwarded to `CopilotProvider(github_token=...)` and does NOT
        build a BYOK `ProviderSettings` (no `base_url` routing)."""
        response = client.post(
            "/execute",
            json=_execute_body(inner_provider_settings={"github_token": "gh-forwarded-token"}),
        )
        assert response.status_code == 200
        _parse_ndjson(response.text)

        instance = _FakeCopilotProvider.instances[0]
        assert instance.github_token == "gh-forwarded-token"
        assert instance.provider_settings is None

    def test_byok_settings_still_route_via_provider_settings_with_no_github_token(
        self, client: TestClient
    ) -> None:
        """BYOK path unchanged (E9 acceptance criteria): `base_url`/`bearer_token`
        still build `ProviderSettings` and `github_token` stays unset."""
        response = client.post(
            "/execute",
            json=_execute_body(
                inner_provider_settings={
                    "base_url": "http://localhost:11434/v1",
                    "bearer_token": "byok-token-value",
                }
            ),
        )
        assert response.status_code == 200
        _parse_ndjson(response.text)

        instance = _FakeCopilotProvider.instances[0]
        assert instance.github_token is None
        assert instance.provider_settings is not None
        assert instance.provider_settings.base_url == "http://localhost:11434/v1"
        assert instance.provider_settings.bearer_token.get_secret_value() == "byok-token-value"

    def test_no_inner_provider_settings_forwards_no_github_token(self, client: TestClient) -> None:
        response = client.post("/execute", json=_execute_body(inner_provider_settings=None))
        assert response.status_code == 200
        _parse_ndjson(response.text)

        instance = _FakeCopilotProvider.instances[0]
        assert instance.github_token is None
        assert instance.provider_settings is None


class TestHostRunnerCredentialContract:
    """Cross-component contract test (review follow-up, issue #284): pins the
    host's default credential body to the runner's actual acceptance of it.

    Prior tests confirmed each side of the seam in isolation — host tests
    assert `AcaRuntimeProvider._resolve_inner_provider_settings()` returns
    `{"github_token": ...}` by default (`COPILOT_GITHUB_TOKEN` set, no
    `COPILOT_PROVIDER_BASE_URL`); runner tests assert `_InnerProviderCache`
    pops a `github_token` key correctly. Neither test constructs the host's
    *actual* output and feeds it into the runner's *actual* cache, so a
    version skew between the two sides (e.g. a runner image pinned to a
    commit predating the `github_token` pop, epic E9) would build and pass
    every existing test while failing at runtime with a Pydantic
    `extra="forbid"` `ValidationError` on `ProviderSettings`. This test
    would have caught that: it builds the exact dict the host produces by
    default and asserts the runner's cache accepts it without raising.
    """

    async def test_runner_cache_accepts_hosts_default_github_token_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import patch

        from conductor.aca_runner.server import _InnerProviderCache
        from conductor.config.schema import ProviderSettings
        from conductor.providers.aca import AcaRuntimeProvider

        monkeypatch.setattr("conductor.aca_runner.server.CopilotProvider", _FakeCopilotProvider)
        # Default credential path (DD4/E8/E10): no COPILOT_PROVIDER_BASE_URL,
        # a GitHub token available via the top env-var precedence.
        monkeypatch.delenv("COPILOT_PROVIDER_BASE_URL", raising=False)
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "hosts-default-github-token")

        settings = ProviderSettings(
            name="aca",
            pool_endpoint="https://pool.example.com",
            api_version="2025-07-01",
        )
        with patch("conductor.providers.aca.AZURE_IDENTITY_AVAILABLE", True):
            host_provider = AcaRuntimeProvider(provider_settings=settings)

        # The exact dict the host forwards in the `/execute` request body.
        inner_provider_settings = host_provider._resolve_inner_provider_settings()
        assert inner_provider_settings.keys() == {"github_token"}

        cache = _InnerProviderCache()
        # Must not raise (e.g. Pydantic `extra="forbid"` on `ProviderSettings`
        # from a stale runner that hasn't popped `github_token` yet).
        provider = await cache.get(
            mcp_servers=None,
            inner_provider_settings=inner_provider_settings,
            tool_output=None,
        )

        assert provider.github_token == "hosts-default-github-token"
        assert provider.provider_settings is None


class TestExecuteValidatesBeforeStreaming:
    """Review fix: agent reconstruction runs before `StreamingResponse` opens.

    An invalid agent payload (e.g. a bad `context_tier` literal) must
    surface as a clean, non-streaming 4xx JSON error — not as a broken
    mid-stream frame after a 200 has already been sent.
    """

    def test_invalid_context_tier_returns_400_without_opening_stream(
        self, client: TestClient
    ) -> None:
        response = client.post(
            "/execute",
            json=_execute_body(agent={"name": "implement", "context_tier": "not-a-real-tier"}),
        )
        assert response.status_code == 400
        assert not response.headers["content-type"].startswith("application/x-ndjson")
        body = response.json()
        message = body.get("error", body).get("message", "")
        assert "context_tier" in message
        # The provider must never be constructed for a rejected payload —
        # confirms validation ran before the cache lookup / stream, too.
        assert _FakeCopilotProvider.instances == []


class TestExecuteTerminalError:
    """A failing inner `execute()` call still terminates the stream cleanly."""

    def test_execute_failure_after_stream_open_yields_error_frame(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original_execute = _FakeCopilotProvider.execute

        async def _failing_execute(self: _FakeCopilotProvider, *args: Any, **kwargs: Any) -> Any:
            self.execute_error = RuntimeError("inner SDK exploded")
            return await original_execute(self, *args, **kwargs)

        monkeypatch.setattr(_FakeCopilotProvider, "execute", _failing_execute)

        response = client.post("/execute", json=_execute_body())
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")

        frames = _parse_ndjson(response.text)
        terminal = frames[-1]
        assert terminal["type"] == "error"
        assert "inner SDK exploded" in terminal["data"]["message"]
        # Exactly one terminal frame — never a result *and* an error.
        assert sum(1 for f in frames if f["type"] in ("result", "error")) == 1


class TestExecuteForwardsRetryAndContextTier:
    """Review fix: `retry` and `context_tier` reach the inner `AgentDef`."""

    def test_retry_and_context_tier_are_forwarded_to_inner_agent(self, client: TestClient) -> None:
        response = client.post(
            "/execute",
            json=_execute_body(
                agent={
                    "name": "implement",
                    "model": "gpt-4.1",
                    "context_tier": "long_context",
                    "retry": {"max_attempts": 5, "backoff": "fixed", "delay_seconds": 1.0},
                }
            ),
        )
        assert response.status_code == 200
        _parse_ndjson(response.text)

        instance = _FakeCopilotProvider.instances[0]
        agent = instance.execute_calls[0]["agent"]
        assert agent.context_tier == "long_context"
        assert agent.retry is not None
        assert agent.retry.max_attempts == 5
        assert agent.retry.backoff == "fixed"

    def test_no_retry_or_context_tier_forwards_none(self, client: TestClient) -> None:
        response = client.post("/execute", json=_execute_body())
        assert response.status_code == 200
        _parse_ndjson(response.text)

        instance = _FakeCopilotProvider.instances[0]
        agent = instance.execute_calls[0]["agent"]
        assert agent.retry is None
        assert agent.context_tier is None


class TestInnerProviderCacheConcurrency:
    """Review fix: concurrent `get()` calls don't corrupt cache state."""

    async def test_concurrent_rebuilds_close_the_stale_provider_exactly_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "conductor.aca_runner.server.CopilotProvider", _DelayedCloseFakeCopilotProvider
        )
        from conductor.aca_runner.server import _InnerProviderCache

        cache = _InnerProviderCache()
        # Prime the cache with an initial provider (key1).
        await cache.get(mcp_servers=None, inner_provider_settings=None, tool_output=None)
        assert len(_FakeCopilotProvider.instances) == 1
        first = _FakeCopilotProvider.instances[0]

        # Two concurrent requests, each with a *different* key from the
        # cached one (and from each other), racing to rebuild.
        results = await asyncio.gather(
            cache.get(
                mcp_servers={"git": {"type": "stdio", "command": "echo"}},
                inner_provider_settings=None,
                tool_output=None,
            ),
            cache.get(
                mcp_servers={"grep": {"type": "stdio", "command": "echo"}},
                inner_provider_settings=None,
                tool_output=None,
            ),
        )

        # The initial provider must be closed exactly once — a race in the
        # unlocked check-close-rebuild sequence would double-close it.
        assert first.close.await_count == 1
        # No provider is ever silently orphaned: every constructed instance
        # is either the live cache entry or has been closed.
        live = cache._provider
        for instance in _FakeCopilotProvider.instances:
            if instance is not live:
                assert instance.close.await_count == 1
        # Both concurrent callers got a real, live provider back.
        assert all(r is not None for r in results)
        assert cache._key is not None

    async def test_concurrent_get_with_same_key_returns_single_instance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "conductor.aca_runner.server.CopilotProvider", _DelayedCloseFakeCopilotProvider
        )
        from conductor.aca_runner.server import _InnerProviderCache

        cache = _InnerProviderCache()
        results = await asyncio.gather(
            *[
                cache.get(mcp_servers=None, inner_provider_settings=None, tool_output=None)
                for _ in range(5)
            ]
        )
        assert len(_FakeCopilotProvider.instances) == 1
        assert all(r is results[0] for r in results)


class TestInnerProviderCacheKeyHashing:
    """Review fix: `_key_for` hashes the cache key rather than retaining the
    canonical JSON (which would otherwise hold plaintext credentials in a
    long-lived instance attribute for the cache's whole lifetime).
    """

    def test_distinct_secretstr_credentials_yield_distinct_keys(self) -> None:
        from conductor.aca_runner.server import _InnerProviderCache

        key_a = _InnerProviderCache._key_for(
            mcp_servers=None,
            inner_provider_settings={"bearer_token": SecretStr("token-a-value")},
            tool_output=None,
        )
        key_b = _InnerProviderCache._key_for(
            mcp_servers=None,
            inner_provider_settings={"bearer_token": SecretStr("token-b-value")},
            tool_output=None,
        )
        assert key_a != key_b

    def test_same_secretstr_credential_yields_the_same_key(self) -> None:
        from conductor.aca_runner.server import _InnerProviderCache

        settings = {"bearer_token": SecretStr("token-value")}
        key_1 = _InnerProviderCache._key_for(
            mcp_servers=None, inner_provider_settings=settings, tool_output=None
        )
        key_2 = _InnerProviderCache._key_for(
            mcp_servers=None, inner_provider_settings=dict(settings), tool_output=None
        )
        assert key_1 == key_2

    def test_key_never_discloses_the_plaintext_credential(self) -> None:
        import hashlib

        from conductor.aca_runner.server import _InnerProviderCache

        secret_value = "super-secret-token-value"
        key = _InnerProviderCache._key_for(
            mcp_servers=None,
            inner_provider_settings={"github_token": SecretStr(secret_value)},
            tool_output=None,
        )
        assert secret_value not in key
        # The key is a one-way sha256 hexdigest, not the masked `SecretStr`
        # repr — guards against a regression back to
        # `json.dumps(..., default=str)`, whose masked "**********" output
        # would collapse every distinct credential onto the same key.
        assert "*" not in key
        assert len(key) == len(hashlib.sha256(b"").hexdigest())
        assert all(c in "0123456789abcdef" for c in key)

    async def test_cache_rebuilds_when_only_the_credential_value_changes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: `get()` must still tell two distinct credentials
        apart post-hashing, even though they otherwise share the same
        `mcp_servers`/`tool_output` and the same `inner_provider_settings`
        *key* (`bearer_token`)."""
        monkeypatch.setattr("conductor.aca_runner.server.CopilotProvider", _FakeCopilotProvider)
        from conductor.aca_runner.server import _InnerProviderCache

        cache = _InnerProviderCache()
        await cache.get(
            mcp_servers=None,
            inner_provider_settings={"bearer_token": SecretStr("token-one")},
            tool_output=None,
        )
        await cache.get(
            mcp_servers=None,
            inner_provider_settings={"bearer_token": SecretStr("token-two")},
            tool_output=None,
        )
        assert len(_FakeCopilotProvider.instances) == 2
        assert _FakeCopilotProvider.instances[0].close.await_count == 1
