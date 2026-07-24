"""Tests for the `aca` provider (#284).

E2 tests (factory arm, capability registration) live in the classes above the
E3 marker below. E3 tests (identifier derivation, streaming transport,
interrupt, validate_connection, close) exercise the transport shim itself
against a mocked runner (`httpx.MockTransport`) and a mocked
`DefaultAzureCredential`.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pydantic import SecretStr

from conductor.config.schema import AgentDef, ProviderSettings, SandboxConfig, ToolOutputConfig
from conductor.exceptions import ProviderError
from conductor.providers.aca_protocol import AcaExecuteRequest
from conductor.providers.capabilities import ProviderCapabilities, get_capabilities
from conductor.providers.factory import create_provider


class TestAcaCapabilities:
    """`get_capabilities("aca")` resolves the declared descriptor without instantiation."""

    def test_aca_registered_in_known_provider_names(self) -> None:
        from conductor.providers.capabilities import known_provider_names

        assert "aca" in known_provider_names()

    def test_get_capabilities_resolves_without_azure_identity_installed(self) -> None:
        """Resolving capabilities must not require azure-identity or any network access.

        `azure-identity` is gated behind the `aca` extra and may or may not be
        installed in a given test environment. A successful resolution here
        (regardless of that) proves the resolver only imports the provider
        module and reads the class-level CAPABILITIES attribute — it never
        instantiates the provider (which is the thing that actually requires
        `azure-identity`).
        """
        caps = get_capabilities("aca")
        assert isinstance(caps, ProviderCapabilities)

    def test_aca_capabilities_match_declared_table(self) -> None:
        """Declared capabilities match the design's capability table exactly."""
        caps = get_capabilities("aca")
        assert caps.tier == "experimental"
        assert caps.is_experimental is True
        assert caps.mcp_tools is True
        assert caps.workflow_tools_passthrough is False
        assert caps.streaming_events is True
        assert caps.agent_reasoning_events is True
        assert caps.reasoning_effort == ("low", "medium", "high", "xhigh", "max")
        assert caps.structured_output == "prompt_injection"
        assert caps.interrupt is True
        assert caps.max_session_seconds is True
        assert caps.checkpoint_resume is False
        assert caps.usage_tracking is True
        assert caps.concurrent_safe is True
        assert caps.working_dir is False

    def test_aca_workflow_tools_passthrough_capability_false(self) -> None:
        """The in-container `CopilotProvider` never applies the `tools:`
        allowlist to the SDK session (no filtering of which MCP
        servers/tools the model can call) — `aca` declares this honestly
        rather than claiming enforcement that doesn't happen."""
        caps = get_capabilities("aca")
        assert caps.workflow_tools_passthrough is False
        assert "no per-agent tools allowlist" in caps.declared_limitations()

    def test_aca_working_dir_capability_false(self) -> None:
        """`aca` does not apply the generic, host-resolved `working_dir` —
        only the separate, container-relative `sandbox.working_dir` field."""
        caps = get_capabilities("aca")
        assert caps.working_dir is False
        assert "working_dir ignored" in caps.declared_limitations()

    def test_aca_declared_limitations_lists_no_checkpoint_resume(self) -> None:
        caps = get_capabilities("aca")
        assert "no checkpoint resume" in caps.declared_limitations()


class TestAcaExtraCleanInstall:
    """The `aca` extra must be sufficient on its own — no other test in this
    file exercises the real, unmocked azure-identity async credential path
    (they all patch `AZURE_IDENTITY_AVAILABLE` and swap in a fake credential),
    so a missing transitive dependency could regress silently. `aca.py` uses
    `azure.identity.aio.DefaultAzureCredential`, which builds an async HTTP
    pipeline requiring `aiohttp` — `azure-identity` alone does not pull it in
    (`azure-core`'s own `aio` extra does). See pyproject.toml's `aca` extra."""

    @pytest.mark.asyncio
    async def test_async_default_azure_credential_constructs_without_import_error(self) -> None:
        """Skipped when the `aca` extra isn't installed in this environment;
        when it *is* installed, construction must not raise the `aiohttp
        package is not installed` ImportError that azure-core's async
        transport raises when only `azure-identity` (and not `aiohttp` /
        `azure-core[aio]`) is present."""
        pytest.importorskip(
            "azure.identity.aio",
            reason="aca extra not installed (pip install 'conductor-cli[aca]')",
        )
        from azure.identity.aio import DefaultAzureCredential

        credential = DefaultAzureCredential()
        try:
            pass  # construction alone is the regression surface; no token fetch needed
        finally:
            await credential.close()


class TestAcaFactory:
    """`create_provider("aca", ...)` wiring, mirroring the claude/hermes availability guards."""

    @patch("conductor.providers.factory.AZURE_IDENTITY_AVAILABLE", False)
    @pytest.mark.asyncio
    async def test_factory_raises_when_azure_identity_not_available(self) -> None:
        """The `aca` extra may or may not be installed in this test env, so the
        unavailable-SDK branch is exercised explicitly via a patched flag
        rather than relying on the ambient environment (mirrors how the
        claude/hermes availability guards are tested elsewhere)."""
        settings = ProviderSettings(name="aca", pool_endpoint="https://pool.example.com")
        with pytest.raises(ProviderError, match="azure-identity"):
            await create_provider("aca", validate=False, provider_settings=settings)

    @patch("conductor.providers.factory.AZURE_IDENTITY_AVAILABLE", False)
    @pytest.mark.asyncio
    async def test_factory_error_includes_install_suggestion(self) -> None:
        settings = ProviderSettings(name="aca", pool_endpoint="https://pool.example.com")
        with pytest.raises(ProviderError) as exc_info:
            await create_provider("aca", validate=False, provider_settings=settings)
        assert exc_info.value.suggestion is not None
        assert "aca" in exc_info.value.suggestion

    @patch("conductor.providers.factory.AZURE_IDENTITY_AVAILABLE", True)
    @pytest.mark.asyncio
    async def test_factory_raises_when_provider_settings_missing(self) -> None:
        """aca requires structured provider_settings (pool_endpoint lives there)."""
        with pytest.raises(ProviderError, match="requires structured"):
            await create_provider("aca", validate=False)

    @patch("conductor.providers.factory.AZURE_IDENTITY_AVAILABLE", True)
    @pytest.mark.asyncio
    async def test_factory_raises_when_provider_settings_wrong_name(self) -> None:
        settings = ProviderSettings(name="copilot")
        with pytest.raises(ProviderError, match="requires structured"):
            await create_provider("aca", validate=False, provider_settings=settings)

    @patch("conductor.providers.factory.AZURE_IDENTITY_AVAILABLE", True)
    @patch("conductor.providers.aca.AZURE_IDENTITY_AVAILABLE", True)
    @pytest.mark.asyncio
    async def test_factory_creates_aca_provider_when_available(self) -> None:
        """With azure-identity mocked as available, the factory constructs the provider."""
        from conductor.providers.aca import AcaRuntimeProvider

        settings = ProviderSettings(name="aca", pool_endpoint="https://pool.example.com")
        provider = await create_provider("aca", validate=False, provider_settings=settings)
        assert isinstance(provider, AcaRuntimeProvider)

    @patch("conductor.providers.factory.AZURE_IDENTITY_AVAILABLE", True)
    @patch("conductor.providers.aca.AZURE_IDENTITY_AVAILABLE", True)
    @pytest.mark.asyncio
    async def test_factory_forwards_provider_settings_and_config(self) -> None:
        from conductor.providers.aca import AcaRuntimeProvider

        settings = ProviderSettings(
            name="aca",
            pool_endpoint="https://pool.example.com",
            api_version="2025-07-01",
        )
        mcp_servers = {"my-server": {"command": "npx", "args": ["some-mcp-server"]}}
        tool_output = ToolOutputConfig(enabled=False, max_chars=12345, spill_to_file=False)
        provider = await create_provider(
            "aca",
            validate=False,
            provider_settings=settings,
            mcp_servers=mcp_servers,
            default_model="gpt-4o",
            max_agent_iterations=25,
            max_session_seconds=120.0,
            default_reasoning_effort="high",
            tool_output=tool_output,
        )
        assert isinstance(provider, AcaRuntimeProvider)
        assert provider._provider_settings is settings
        assert provider._mcp_servers is mcp_servers
        assert provider._default_model == "gpt-4o"
        assert provider._default_max_agent_iterations == 25
        assert provider._default_max_session_seconds == 120.0
        assert provider._default_reasoning_effort == "high"
        assert provider._tool_output_config is tool_output

    @patch("conductor.providers.factory.AZURE_IDENTITY_AVAILABLE", False)
    @pytest.mark.asyncio
    async def test_factory_does_not_construct_provider_when_unavailable(self) -> None:
        """The provider class must never be instantiated when the SDK is missing."""
        with patch(
            "conductor.providers.factory.AcaRuntimeProvider", new_callable=MagicMock
        ) as mock_cls:
            settings = ProviderSettings(name="aca", pool_endpoint="https://pool.example.com")
            with pytest.raises(ProviderError):
                await create_provider("aca", validate=False, provider_settings=settings)
            mock_cls.assert_not_called()


class TestAcaRuntimeProviderInit:
    """Direct construction guards, independent of the factory."""

    def test_init_raises_when_azure_identity_unavailable(self) -> None:
        from conductor.providers.aca import AcaRuntimeProvider

        with patch("conductor.providers.aca.AZURE_IDENTITY_AVAILABLE", False):
            settings = ProviderSettings(name="aca", pool_endpoint="https://pool.example.com")
            with pytest.raises(ProviderError, match="azure-identity"):
                AcaRuntimeProvider(provider_settings=settings)

    def test_init_succeeds_when_azure_identity_available(self) -> None:
        from conductor.providers.aca import AcaRuntimeProvider

        with patch("conductor.providers.aca.AZURE_IDENTITY_AVAILABLE", True):
            settings = ProviderSettings(name="aca", pool_endpoint="https://pool.example.com")
            provider = AcaRuntimeProvider(provider_settings=settings)
            assert provider._provider_settings is settings

    def test_class_declares_capabilities(self) -> None:
        from conductor.providers.aca import AcaRuntimeProvider

        assert isinstance(AcaRuntimeProvider.CAPABILITIES, ProviderCapabilities)


# ---------------------------------------------------------------------------
# E3 — host transport shim (#284): identifier derivation, streaming
# transport, interrupt, validate_connection, close. See
# docs/projects/aca/aca-provider.plan.md, epic E3.
# ---------------------------------------------------------------------------


class _FakeAccessToken:
    """Stand-in for `azure.core.credentials.AccessToken`."""

    def __init__(self, token: str = "fake-aad-token") -> None:
        self.token = token
        self.expires_on = 9999999999  # far future — never treated as expired


class _FakeAsyncCredential:
    """Stand-in for `azure.identity.aio.DefaultAzureCredential`."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.get_token = AsyncMock(return_value=_FakeAccessToken())
        self.close = AsyncMock(return_value=None)


class _FakeStreamContext:
    """Async context manager wrapping a pre-built fake streaming response.

    Used to substitute `AcaRuntimeProvider._stream_execute` for the
    interrupt-race tests below, which need deterministic control over frame
    arrival timing that a real (even mocked-transport) httpx stream cannot
    reliably guarantee.
    """

    def __init__(self, response: object) -> None:
        self._response = response

    async def __aenter__(self) -> object:
        return self._response

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _FakeInterruptResponse:
    """Fully-controlled fake streaming response for interrupt-race tests.

    Each line is yielded only after a short real delay, so a concurrently
    racing `interrupt_signal.wait()` (which resolves near-instantly when the
    event is already set) deterministically wins the race — removing any
    dependence on real transport/event-loop scheduling nuances.
    """

    def __init__(self, lines: list[str]) -> None:
        self.status_code = 200
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            await asyncio.sleep(0.02)
            yield line


@pytest.fixture(autouse=True)
def _default_aca_credential_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests in this file exercise transport/streaming/interrupt
    behavior, not credential resolution — supply a default
    ``COPILOT_GITHUB_TOKEN`` so `_resolve_inner_provider_settings` doesn't
    fail loudly (DD4, epic E8) for those unrelated tests. Tests in
    `TestAcaCredentialPrecedence` explicitly clear/override the credential
    env vars (via the same per-test `monkeypatch` fixture) to exercise the
    real precedence logic.
    """
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "test-default-github-token")


def _make_provider(**settings_kwargs: object):
    from conductor.providers.aca import AcaRuntimeProvider

    settings = ProviderSettings(
        name="aca",
        pool_endpoint="https://pool.example.com",
        api_version="2025-07-01",
        **settings_kwargs,
    )
    with patch("conductor.providers.aca.AZURE_IDENTITY_AVAILABLE", True):
        return AcaRuntimeProvider(provider_settings=settings)


def _agent(name: str = "implement", **kwargs: object) -> AgentDef:
    return AgentDef(name=name, prompt="do the thing", **kwargs)


def _ndjson_body(frames: list[dict]) -> bytes:
    return ("\n".join(json.dumps(f) for f in frames) + "\n").encode("utf-8")


class TestIdentifierDerivation:
    """`identifier_for` — DD5 *Data Flow*, OQ#1 concurrency-discriminator decision (E3-T3)."""

    def test_default_agent_scope_reuses_identifier_across_sequential_calls(self) -> None:
        provider = _make_provider()
        agent = _agent()
        assert provider.identifier_for(agent, {}) == provider.identifier_for(agent, {})

    def test_plain_self_loop_reuses_identifier(self) -> None:
        """A loop-back retry (no for-each context keys) keeps the same session
        so partial edits / cloned state survive between attempts (DD5)."""
        provider = _make_provider()
        agent = _agent()
        first = provider.identifier_for(agent, {"iteration": 1})
        second = provider.identifier_for(agent, {"iteration": 2})
        assert first == second

    def test_agent_scope_diverges_by_agent_name(self) -> None:
        provider = _make_provider()
        id_a = provider.identifier_for(_agent(name="alpha"), {})
        id_b = provider.identifier_for(_agent(name="beta"), {})
        assert id_a != id_b

    def test_for_each_loop_key_diverges_identifier_even_under_agent_scope(self) -> None:
        """OQ#1 decision (b): a for-each loop signal always diverges the
        identifier — even for a *serial* for_each — because the provider
        cannot distinguish serial from concurrent without an `execute()`
        signature change, which the E3 acceptance criteria rule out."""
        provider = _make_provider()
        agent = _agent()
        assert provider.identifier_for(agent, {"_key": "K1"}) != provider.identifier_for(
            agent, {"_key": "K2"}
        )

    def test_for_each_index_diverges_when_no_key_by(self) -> None:
        provider = _make_provider()
        agent = _agent()
        assert provider.identifier_for(agent, {"_index": 0}) != provider.identifier_for(
            agent, {"_index": 1}
        )

    def test_workflow_scope_is_constant_across_agents(self) -> None:
        provider = _make_provider(identifier_scope="workflow")
        id_a = provider.identifier_for(_agent(name="alpha"), {})
        id_b = provider.identifier_for(_agent(name="beta"), {})
        assert id_a == id_b

    def test_item_scope_uses_loop_key_and_reuses_across_calls(self) -> None:
        provider = _make_provider(identifier_scope="item")
        agent = _agent()
        id_k1 = provider.identifier_for(agent, {"_key": "K1"})
        id_k1_again = provider.identifier_for(agent, {"_key": "K1"})
        id_k2 = provider.identifier_for(agent, {"_key": "K2"})
        assert id_k1 == id_k1_again
        assert id_k1 != id_k2

    def test_item_scope_falls_back_to_agent_name_outside_a_loop(self) -> None:
        provider = _make_provider(identifier_scope="item")
        agent = _agent()
        assert provider.identifier_for(agent, {}) == provider.identifier_for(agent, {})

    def test_none_scope_never_reuses(self) -> None:
        provider = _make_provider(identifier_scope="none")
        agent = _agent()
        assert provider.identifier_for(agent, {}) != provider.identifier_for(agent, {})

    def test_per_agent_sandbox_override_wins_over_workflow_scope(self) -> None:
        provider = _make_provider(identifier_scope="agent")
        agent = _agent(sandbox=SandboxConfig(identifier_scope="none"))
        assert provider.identifier_for(agent, {}) != provider.identifier_for(agent, {})

    def test_identifier_is_charset_normalized_and_bounded(self) -> None:
        provider = _make_provider()
        agent = _agent(name="Weird Agent Name!! With Spaces_and_Punct...")
        identifier = provider.identifier_for(agent, {"_key": "K" * 300})
        assert len(identifier) <= 128
        assert all(c.islower() or c.isdigit() or c == "-" for c in identifier)

    def test_long_identifier_gets_hash_suffix_and_stays_distinct(self) -> None:
        provider = _make_provider()
        agent = _agent(name="a" * 200)
        id_1 = provider.identifier_for(agent, {"_key": "1"})
        id_2 = provider.identifier_for(agent, {"_key": "2"})
        assert len(id_1) <= 128
        assert len(id_2) <= 128
        assert id_1 != id_2

    def test_two_provider_instances_get_different_run_salts(self) -> None:
        """Different workflow runs (distinct provider instances) never collide
        on identifier, even for identical agent/context (`run_salt`, E3-T1)."""
        provider_a = _make_provider()
        provider_b = _make_provider()
        agent = _agent()
        assert provider_a.identifier_for(agent, {}) != provider_b.identifier_for(agent, {})

    def test_normalization_does_not_collapse_distinct_names(self) -> None:
        """Review fix: charset-normalization must not collide distinct raw
        identifiers. Agent names that differ only in the characters the
        charset-normalization regex collapses to a hyphen (``_``, ``.``,
        space) must still resolve to distinct identifiers."""
        provider = _make_provider()
        id_underscore = provider.identifier_for(_agent(name="foo_bar"), {})
        id_dot = provider.identifier_for(_agent(name="foo.bar"), {})
        id_space = provider.identifier_for(_agent(name="foo bar"), {})
        id_hyphen = provider.identifier_for(_agent(name="foo-bar"), {})
        ids = {id_underscore, id_dot, id_space, id_hyphen}
        assert len(ids) == 4, f"normalization collapsed distinct names: {ids}"


class TestAcaConcurrencyIsolation:
    """`execute()`'s in-flight identifier registry (OQ#1 review fix).

    `identifier_for` alone is insufficient to keep genuinely concurrent
    siblings (e.g. a `parallel` group under `identifier_scope: workflow`,
    which carries no for-each loop-key signal at all) from colliding on the
    *same* logical identifier. `execute()` layers `_acquire_wire_identifier`/
    `_release_wire_identifier` on top so concurrent calls diverge on the wire
    while sequential calls still reuse the identifier.
    """

    @pytest.mark.asyncio
    async def test_concurrent_calls_sharing_scope_key_diverge_on_the_wire(self) -> None:
        """Two `parallel`-group-like concurrent calls under `identifier_scope:
        workflow` (constant `scope_key`, no loop-key context) must not be
        routed to the same ACA session."""
        provider = _make_provider(identifier_scope="workflow")
        identifiers_seen: list[str] = []
        release_gate = asyncio.Event()
        entered = asyncio.Event()
        entered_count = 0

        class _GatedStreamContext:
            """Records the identifier immediately, then blocks entry (mimicking
            a slow in-flight request) until `release_gate` is set — this keeps
            both calls' `execute()` genuinely overlapping in time so the
            in-flight registry sees them as concurrent."""

            def __init__(self, identifier: str) -> None:
                self._identifier = identifier

            async def __aenter__(self):
                nonlocal entered_count
                identifiers_seen.append(self._identifier)
                entered_count += 1
                if entered_count == 2:
                    entered.set()
                await release_gate.wait()
                return _FakeInterruptResponse(
                    [json.dumps({"type": "result", "data": {"content": {}}})]
                )

            async def __aexit__(self, *exc_info: object) -> None:
                return None

        def slow_stream_execute(url: str, params: dict, headers: dict, body: dict):
            return _GatedStreamContext(params["identifier"])

        provider._stream_execute = slow_stream_execute  # type: ignore[method-assign]

        async def run(agent_name: str) -> None:
            with patch(
                "conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential
            ):
                await provider.execute(
                    agent=_agent(name=agent_name), context={}, rendered_prompt="x"
                )

        task_a = asyncio.create_task(run("alpha"))
        task_b = asyncio.create_task(run("beta"))
        await asyncio.wait_for(entered.wait(), timeout=5)
        release_gate.set()
        await asyncio.gather(task_a, task_b)

        assert len(identifiers_seen) == 2
        assert identifiers_seen[0] != identifiers_seen[1]

    @pytest.mark.asyncio
    async def test_sequential_calls_sharing_scope_key_reuse_the_wire_identifier(self) -> None:
        """The same base identifier is reused across sequential (non-
        overlapping) calls, preserving the `identifier_scope: workflow`
        cross-agent workspace-sharing guarantee."""
        provider = _make_provider(identifier_scope="workflow")
        identifiers_seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            identifiers_seen.append(request.url.params["identifier"])
            return httpx.Response(
                200, content=_ndjson_body([{"type": "result", "data": {"content": {}}}])
            )

        provider._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential):
            await provider.execute(agent=_agent(name="alpha"), context={}, rendered_prompt="x")
            await provider.execute(agent=_agent(name="beta"), context={}, rendered_prompt="x")

        assert identifiers_seen[0] == identifiers_seen[1]

    @pytest.mark.asyncio
    async def test_active_identifier_registry_is_cleared_after_execute(self) -> None:
        provider = _make_provider()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=_ndjson_body([{"type": "result", "data": {"content": {}}}])
            )

        provider._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential):
            await provider.execute(agent=_agent(), context={}, rendered_prompt="x")

        assert provider._active_identifiers == {}

    def test_out_of_order_release_does_not_collide_with_still_active_slot(self) -> None:
        """Regression (review fix): three overlapping calls sharing one logical
        identifier, released out of order, must never hand two *simultaneously
        active* calls the same wire identifier.

        Call A acquires the base identifier (slot 0); call B acquires the
        first concurrency slot (slot 1) while A is still active; call C then
        starts a third, overlapping reservation before either releases (slot
        2). A finishes first — out of order relative to B and C, which are
        both still in flight — and releases slot 0. A subsequent call D must
        reuse the just-freed slot 0, never B's slot 1 or C's slot 2. A naive
        in-flight *count* (rather than tracking the specific reserved slot
        numbers) would instead have derived D's suffix from the current
        count (2, since B and C are still active), colliding with C's
        `-conc2` identifier even though C has not released it.
        """
        provider = _make_provider()
        logical_id = "cond-abcd1234-implement"

        id_a, slot_a = provider._acquire_wire_identifier(logical_id)
        id_b, slot_b = provider._acquire_wire_identifier(logical_id)
        id_c, slot_c = provider._acquire_wire_identifier(logical_id)
        assert len({id_a, id_b, id_c}) == 3
        assert (slot_a, slot_b, slot_c) == (0, 1, 2)

        # A finishes first, out of order — B and C are still active.
        provider._release_wire_identifier(logical_id, slot_a)

        id_d, slot_d = provider._acquire_wire_identifier(logical_id)

        assert id_d == id_a  # reuses the freed base slot...
        assert id_d not in (id_b, id_c)  # ...and never collides with B or C.
        assert slot_d == slot_a

        provider._release_wire_identifier(logical_id, slot_b)
        provider._release_wire_identifier(logical_id, slot_c)
        provider._release_wire_identifier(logical_id, slot_d)
        assert provider._active_identifiers == {}


class TestAcaExecuteStreaming:
    """`execute()` — Branch S streaming transport (E3-T5)."""

    @pytest.mark.asyncio
    async def test_execute_relays_events_and_parses_result(self) -> None:
        frames = [
            {"type": "agent_turn_start", "data": {"turn": "awaiting_model"}},
            {"type": "agent_message", "data": {"content": "hi"}},
            {
                "type": "result",
                "data": {
                    "content": {"summary": "done"},
                    "model": "gpt-4.1",
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "partial": False,
                },
            },
        ]
        captured_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_requests.append(request)
            assert request.url.path == "/execute"
            assert request.url.params["identifier"]
            assert request.headers["Authorization"] == "Bearer fake-aad-token"
            return httpx.Response(200, content=_ndjson_body(frames))

        provider = _make_provider()
        provider._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        events: list[tuple[str, dict]] = []
        with patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential):
            output = await provider.execute(
                agent=_agent(),
                context={},
                rendered_prompt="do the thing",
                tools=["git"],
                event_callback=lambda t, d: events.append((t, d)),
            )

        assert events == [
            ("agent_turn_start", {"turn": "awaiting_model"}),
            ("agent_message", {"content": "hi"}),
        ]
        assert output.content == {"summary": "done"}
        assert output.model == "gpt-4.1"
        assert output.input_tokens == 10
        assert output.output_tokens == 5
        assert output.partial is False
        assert len(captured_requests) == 1
        body = json.loads(captured_requests[0].content)
        assert body["rendered_prompt"] == "do the thing"
        assert body["tools"] == ["git"]
        assert body["agent"]["name"] == "implement"

    @pytest.mark.asyncio
    async def test_execute_raises_provider_error_on_error_frame(self) -> None:
        frames = [
            {
                "type": "error",
                "data": {"code": "ToolFailed", "message": "git clone failed", "traceId": "abc123"},
            },
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_ndjson_body(frames))

        provider = _make_provider()
        provider._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        with (
            patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential),
            pytest.raises(ProviderError, match="git clone failed"),
        ):
            await provider.execute(agent=_agent(), context={}, rendered_prompt="x")

    @pytest.mark.asyncio
    async def test_execute_raises_provider_error_on_non_2xx_response(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                json={"error": {"code": "Forbidden", "message": "no access", "traceId": "t-1"}},
            )

        provider = _make_provider()
        provider._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        with (
            patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential),
            pytest.raises(ProviderError) as exc_info,
        ):
            await provider.execute(agent=_agent(), context={}, rendered_prompt="x")
        assert exc_info.value.status_code == 403
        assert "no access" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_execute_raises_provider_error_when_stream_ends_without_result(self) -> None:
        frames = [{"type": "agent_message", "data": {"content": "partial only"}}]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_ndjson_body(frames))

        provider = _make_provider()
        provider._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        with (
            patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential),
            pytest.raises(ProviderError, match="terminal"),
        ):
            await provider.execute(agent=_agent(), context={}, rendered_prompt="x")

    @pytest.mark.asyncio
    async def test_execute_parses_cache_tokens_from_result(self) -> None:
        """Review fix: `AcaResultData.cache_read_tokens`/`cache_write_tokens`
        must reach `AgentOutput`, not be silently dropped."""
        frames = [
            {
                "type": "result",
                "data": {
                    "content": {},
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_tokens": 7,
                    "cache_write_tokens": 3,
                },
            },
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_ndjson_body(frames))

        provider = _make_provider()
        provider._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential):
            output = await provider.execute(agent=_agent(), context={}, rendered_prompt="x")

        assert output.cache_read_tokens == 7
        assert output.cache_write_tokens == 3

    @pytest.mark.asyncio
    async def test_execute_parses_session_seconds_from_result(self) -> None:
        """E6-T1: `AcaResultData.session_seconds` reaches `AgentOutput`, distinct
        from token cost (FR7)."""
        frames = [
            {
                "type": "result",
                "data": {
                    "content": {"summary": "done"},
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "session_seconds": 12.5,
                },
            },
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_ndjson_body(frames))

        provider = _make_provider()
        provider._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential):
            output = await provider.execute(agent=_agent(), context={}, rendered_prompt="x")

        assert output.session_seconds == 12.5
        # Token fields are unaffected by the presence of session_seconds.
        assert output.input_tokens == 10
        assert output.output_tokens == 5

    @pytest.mark.asyncio
    async def test_execute_session_seconds_none_when_absent(self) -> None:
        """When the runner omits `session_seconds`, `AgentOutput.session_seconds`
        stays `None` (matches every non-`aca` provider's default)."""
        frames = [{"type": "result", "data": {"content": {"summary": "done"}}}]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_ndjson_body(frames))

        provider = _make_provider()
        provider._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential):
            output = await provider.execute(agent=_agent(), context={}, rendered_prompt="x")

        assert output.session_seconds is None

    @pytest.mark.asyncio
    async def test_execute_forwards_tool_output_config(self) -> None:
        """Review fix: `runtime.tool_output` must be forwarded so the runner's
        inner provider applies the same per-result MCP tool-output limit."""
        from conductor.providers.aca import AcaRuntimeProvider

        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200, content=_ndjson_body([{"type": "result", "data": {"content": {}}}])
            )

        settings = ProviderSettings(
            name="aca", pool_endpoint="https://pool.example.com", api_version="2025-07-01"
        )
        with patch("conductor.providers.aca.AZURE_IDENTITY_AVAILABLE", True):
            provider = AcaRuntimeProvider(
                provider_settings=settings,
                tool_output=ToolOutputConfig(max_chars=1234, spill_to_file=False),
            )
        provider._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential):
            await provider.execute(agent=_agent(), context={}, rendered_prompt="x")

        assert captured["body"]["tool_output"]["max_chars"] == 1234
        assert captured["body"]["tool_output"]["spill_to_file"] is False

    @pytest.mark.asyncio
    async def test_execute_omits_tool_output_when_unset(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200, content=_ndjson_body([{"type": "result", "data": {"content": {}}}])
            )

        provider = _make_provider()
        provider._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential):
            await provider.execute(agent=_agent(), context={}, rendered_prompt="x")

        assert captured["body"]["tool_output"] is None

    @pytest.mark.asyncio
    async def test_execute_forwards_sandbox_working_dir_only(self) -> None:
        """`_build_request` reads only `agent.sandbox.working_dir` — the
        generic, host-resolved `agent.working_dir` (a different field, with a
        different meaning: a host path resolved against the workflow file's
        directory) must never leak into the request body, since a host path
        has no meaning inside the sandbox filesystem. Capability declaration:
        `CAPABILITIES.working_dir=False` (only `sandbox.working_dir` is
        honored)."""
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200, content=_ndjson_body([{"type": "result", "data": {"content": {}}}])
            )

        provider = _make_provider()
        provider._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        # The engine only ever resolves generic `working_dir` for providers
        # that declare `capabilities.working_dir=True`; constructing it here
        # directly on the AgentDef exercises `_build_request` in isolation
        # even though `conductor validate` would reject this combination
        # against the real aca capability descriptor.
        agent = _agent(
            working_dir="/host/should/be/ignored",
            sandbox=SandboxConfig(working_dir="/workspace"),
        )
        with patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential):
            await provider.execute(agent=agent, context={}, rendered_prompt="x")

        assert captured["body"]["agent"]["working_dir"] == "/workspace"

    @pytest.mark.asyncio
    async def test_execute_omits_working_dir_when_sandbox_unset(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200, content=_ndjson_body([{"type": "result", "data": {"content": {}}}])
            )

        provider = _make_provider()
        provider._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential):
            await provider.execute(agent=_agent(), context={}, rendered_prompt="x")

        assert captured["body"]["agent"]["working_dir"] is None


class _FakeUnreadStreamedResponse:
    """Mimics a real (not `MockTransport`-materialized) streamed httpx
    response whose body has not been read yet — `.json()` must fail until
    `.aread()` is awaited, exactly like the real `httpx.Response` returned by
    `client.stream()` against a genuine network transport (reproduced via
    `httpx._content.AsyncIteratorByteStream` — see review fix notes on
    `_error_from_response`)."""

    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self._body = body
        self._read = False

    async def aread(self) -> bytes:
        self._read = True
        return json.dumps(self._body).encode("utf-8")

    def json(self) -> dict:
        if not self._read:
            raise httpx.ResponseNotRead()
        return self._body


class TestAcaStreamingErrorDiagnostics:
    """Review fix: a non-2xx streamed `/execute` response must not lose its
    ACA `code`/`message`/`traceId` to an unread-body error."""

    @pytest.mark.asyncio
    async def test_error_response_diagnostics_survive_unread_body(self) -> None:
        provider = _make_provider()
        fake_response = _FakeUnreadStreamedResponse(
            403,
            {"error": {"code": "Forbidden", "message": "no access", "traceId": "t-1"}},
        )
        provider._stream_execute = lambda *a, **kw: _FakeStreamContext(fake_response)  # type: ignore[method-assign]

        with (
            patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential),
            pytest.raises(ProviderError) as exc_info,
        ):
            await provider.execute(agent=_agent(), context={}, rendered_prompt="x")

        assert exc_info.value.status_code == 403
        assert "no access" in str(exc_info.value)
        assert "Forbidden" in str(exc_info.value)
        assert "t-1" in str(exc_info.value)


class TestAcaCredentialPrecedence:
    """DD4 / E8: `base_url` → BYOK; else GitHub token → Copilot capacity; else
    fail loudly. Supersedes the OQ#6 Phase 1 stopgap (which silently returned
    ``None`` when nothing was configured)."""

    _CREDENTIAL_ENV_VARS = (
        "COPILOT_PROVIDER_BASE_URL",
        "COPILOT_PROVIDER_API_KEY",
        "COPILOT_PROVIDER_BEARER_TOKEN",
        "COPILOT_GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
    )

    @classmethod
    def _clear_credential_env(cls, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in cls._CREDENTIAL_ENV_VARS:
            monkeypatch.delenv(var, raising=False)

    @staticmethod
    def _make_streaming_provider(monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]):
        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200, content=_ndjson_body([{"type": "result", "data": {"content": {}}}])
            )

        provider = _make_provider()
        provider._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return provider

    @pytest.mark.asyncio
    async def test_execute_forwards_byok_settings_when_base_url_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_credential_env(monkeypatch)
        monkeypatch.setenv("COPILOT_PROVIDER_BASE_URL", "https://byok.example.com")
        monkeypatch.setenv("COPILOT_PROVIDER_BEARER_TOKEN", "secret-token")
        captured: dict[str, object] = {}
        provider = self._make_streaming_provider(monkeypatch, captured)

        with patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential):
            await provider.execute(agent=_agent(), context={}, rendered_prompt="x")

        assert captured["body"]["inner_provider_settings"] == {
            "base_url": "https://byok.example.com",
            "bearer_token": "secret-token",
        }

    @pytest.mark.asyncio
    async def test_execute_base_url_wins_over_github_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A GitHub token present alongside `base_url` is ignored — `base_url`
        wins, mirroring the Copilot CLI's own precedence (DD4)."""
        self._clear_credential_env(monkeypatch)
        monkeypatch.setenv("COPILOT_PROVIDER_BASE_URL", "https://byok.example.com")
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "gh-token-should-be-ignored")
        captured: dict[str, object] = {}
        provider = self._make_streaming_provider(monkeypatch, captured)

        with patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential):
            await provider.execute(agent=_agent(), context={}, rendered_prompt="x")

        assert captured["body"]["inner_provider_settings"] == {
            "base_url": "https://byok.example.com"
        }

    @pytest.mark.asyncio
    async def test_execute_forwards_github_token_when_no_base_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_credential_env(monkeypatch)
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "gh-secret-token")
        captured: dict[str, object] = {}
        provider = self._make_streaming_provider(monkeypatch, captured)

        with patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential):
            await provider.execute(agent=_agent(), context={}, rendered_prompt="x")

        assert captured["body"]["inner_provider_settings"] == {"github_token": "gh-secret-token"}

    @pytest.mark.parametrize("priority_var", ["COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"])
    def test_github_token_env_var_precedence_order(
        self, monkeypatch: pytest.MonkeyPatch, priority_var: str
    ) -> None:
        """``COPILOT_GITHUB_TOKEN`` → ``GH_TOKEN`` → ``GITHUB_TOKEN``: the
        highest-priority var set always wins, even with lower-priority vars
        also present."""
        self._clear_credential_env(monkeypatch)
        order = ["COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"]
        for var in order[order.index(priority_var) :]:
            monkeypatch.setenv(var, f"token-from-{var}")

        provider = _make_provider()
        settings = provider._resolve_inner_provider_settings()

        assert set(settings) == {"github_token"}
        assert settings["github_token"].get_secret_value() == f"token-from-{priority_var}"

    def test_raises_provider_error_when_neither_credential_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_credential_env(monkeypatch)
        provider = _make_provider()

        with pytest.raises(ProviderError) as exc_info:
            provider._resolve_inner_provider_settings()

        message = str(exc_info.value)
        assert "COPILOT_GITHUB_TOKEN" in message
        assert "COPILOT_PROVIDER_BASE_URL" in message
        assert "secret-token" not in message
        assert "gh-secret-token" not in message

    @pytest.mark.asyncio
    async def test_execute_raises_provider_error_when_neither_credential_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_credential_env(monkeypatch)
        provider = _make_provider()

        with (
            patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential),
            pytest.raises(ProviderError),
        ):
            await provider.execute(agent=_agent(), context={}, rendered_prompt="x")

    # ------------------------------------------------------------------
    # Review fix: whitespace-only env vars must not be treated as "configured"
    # ------------------------------------------------------------------

    def test_whitespace_only_base_url_falls_back_to_github_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A blank/whitespace-only `COPILOT_PROVIDER_BASE_URL` (e.g. an unset
        CI secret expanding to an empty string) must not "win" the BYOK
        branch and suppress a perfectly valid GitHub token — it should be
        treated as unset."""
        self._clear_credential_env(monkeypatch)
        monkeypatch.setenv("COPILOT_PROVIDER_BASE_URL", "   ")
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "gh-secret-token")

        provider = _make_provider()
        settings = provider._resolve_inner_provider_settings()

        assert set(settings) == {"github_token"}
        assert settings["github_token"].get_secret_value() == "gh-secret-token"

    def test_whitespace_only_github_token_vars_treated_as_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All three GitHub-token env vars set to whitespace-only values, with
        no BYOK `base_url`, must raise — not silently resolve to a blank
        credential."""
        self._clear_credential_env(monkeypatch)
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", " ")
        monkeypatch.setenv("GH_TOKEN", "\t")
        monkeypatch.setenv("GITHUB_TOKEN", "")

        provider = _make_provider()

        with pytest.raises(ProviderError):
            provider._resolve_inner_provider_settings()

    def test_whitespace_only_github_token_falls_through_to_next_priority_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A whitespace-only `COPILOT_GITHUB_TOKEN` must not suppress a valid
        lower-priority `GH_TOKEN`."""
        self._clear_credential_env(monkeypatch)
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "   ")
        monkeypatch.setenv("GH_TOKEN", "real-token-from-gh-token")

        provider = _make_provider()
        settings = provider._resolve_inner_provider_settings()

        assert settings["github_token"].get_secret_value() == "real-token-from-gh-token"

    def test_whitespace_only_api_key_and_bearer_token_excluded_from_byok_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`base_url` set to a real value with whitespace-only
        `COPILOT_PROVIDER_API_KEY`/`COPILOT_PROVIDER_BEARER_TOKEN` should
        forward only `base_url` — not an empty-looking credential."""
        self._clear_credential_env(monkeypatch)
        monkeypatch.setenv("COPILOT_PROVIDER_BASE_URL", "https://byok.example.com")
        monkeypatch.setenv("COPILOT_PROVIDER_API_KEY", "   ")
        monkeypatch.setenv("COPILOT_PROVIDER_BEARER_TOKEN", "\t\n")

        provider = _make_provider()
        settings = provider._resolve_inner_provider_settings()

        assert settings == {"base_url": "https://byok.example.com"}

    def test_whitespace_only_base_url_with_no_other_credentials_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._clear_credential_env(monkeypatch)
        monkeypatch.setenv("COPILOT_PROVIDER_BASE_URL", "   ")

        provider = _make_provider()

        with pytest.raises(ProviderError):
            provider._resolve_inner_provider_settings()

    def test_base_url_value_is_stripped_of_surrounding_whitespace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real `base_url` with incidental leading/trailing whitespace
        (e.g. a trailing newline from ``$(cat file)``) is forwarded trimmed,
        not verbatim."""
        self._clear_credential_env(monkeypatch)
        monkeypatch.setenv("COPILOT_PROVIDER_BASE_URL", "  https://byok.example.com  \n")

        provider = _make_provider()
        settings = provider._resolve_inner_provider_settings()

        assert settings == {"base_url": "https://byok.example.com"}

    def test_github_token_is_redacted_as_secretstr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The resolved token is held as a `SecretStr` until the instant it is
        placed in the outgoing settings dict, so any incidental repr/str of
        the intermediate value never leaks the plaintext — the same
        redaction discipline as every other secret in the codebase (e.g.
        `ProviderSettings.api_key`)."""
        self._clear_credential_env(monkeypatch)
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "super-secret-value")

        provider = _make_provider()
        secret = provider._resolve_github_token()

        assert secret is not None
        assert "super-secret-value" not in repr(secret)
        assert "super-secret-value" not in str(secret)
        assert secret.get_secret_value() == "super-secret-value"

    def test_github_token_redacted_in_request_dump_and_repr(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review fix: `_resolve_inner_provider_settings` keeps the token as a
        `SecretStr` all the way into `AcaExecuteRequest`, so `model_dump()` /
        `model_dump_json()` / `repr()` on the *request object itself* never
        expose the plaintext — only the dedicated wire-serialization step
        (`_wire_body`, exercised by `test_execute_forwards_github_token_when_no_base_url`
        via `MockTransport`) unwraps it."""
        self._clear_credential_env(monkeypatch)
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "super-secret-value")

        provider = _make_provider()
        request = provider._build_request(_agent(), {}, "x", None)

        assert "super-secret-value" not in repr(request)
        dumped = request.model_dump(mode="json")
        assert dumped["inner_provider_settings"] == {"github_token": "**********"}
        assert "super-secret-value" not in json.dumps(dumped)
        assert "super-secret-value" not in request.model_dump_json()

    def test_byok_bearer_token_redacted_in_request_dump_and_repr(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same redaction guarantee for the BYOK branch's `bearer_token` /
        `api_key`, not just the GitHub-token branch."""
        self._clear_credential_env(monkeypatch)
        monkeypatch.setenv("COPILOT_PROVIDER_BASE_URL", "https://byok.example.com")
        monkeypatch.setenv("COPILOT_PROVIDER_BEARER_TOKEN", "byok-secret-token")

        provider = _make_provider()
        request = provider._build_request(_agent(), {}, "x", None)

        assert "byok-secret-token" not in repr(request)
        dumped = request.model_dump(mode="json")
        assert dumped["inner_provider_settings"] == {
            "base_url": "https://byok.example.com",
            "bearer_token": "**********",
        }
        assert "byok-secret-token" not in json.dumps(dumped)

    @pytest.mark.asyncio
    async def test_wire_body_unwraps_secret_for_mock_transport_but_dump_stays_redacted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end proof: the object `execute()` builds stays redacted under
        `model_dump`/`repr`, while the actual bytes received by the runner
        (`MockTransport`) carry the real plaintext token — i.e. the unwrap
        happens exactly once, at the wire boundary, and nowhere else."""
        self._clear_credential_env(monkeypatch)
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "super-secret-value")
        captured: dict[str, object] = {}
        provider = self._make_streaming_provider(monkeypatch, captured)

        request = provider._build_request(_agent(), {}, "x", None)
        assert "super-secret-value" not in repr(request)
        assert request.model_dump(mode="json")["inner_provider_settings"] == {
            "github_token": "**********"
        }

        with patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential):
            await provider.execute(agent=_agent(), context={}, rendered_prompt="x")

        assert captured["body"]["inner_provider_settings"] == {"github_token": "super-secret-value"}


class TestAcaExecuteRequestSecretRedactionOnValidate:
    """Review fix: `AcaExecuteRequest.inner_provider_settings` is a loosely
    typed ``dict[str, Any]`` — plain construction via
    `AcaRuntimeProvider._resolve_inner_provider_settings` already wraps
    known credential keys in `SecretStr`, but that coercion previously did
    *not* happen for the model's other construction paths
    (`model_validate` / `model_validate_json` on a raw dict/JSON payload,
    e.g. the runner's own FastAPI request parsing), so a plaintext
    credential string surviving through one of those paths would leak via
    `repr()`/`model_dump()`. `AcaExecuteRequest._redact_inner_provider_secrets`
    (a `field_validator`) closes that gap."""

    @staticmethod
    def _minimal_request_dict(**inner_provider_settings: object) -> dict[str, object]:
        return {
            "agent": {"name": "implement"},
            "rendered_prompt": "hi",
            "inner_provider_settings": inner_provider_settings,
        }

    def test_model_validate_dict_coerces_plain_github_token_to_secretstr(self) -> None:
        request = AcaExecuteRequest.model_validate(
            self._minimal_request_dict(github_token="plaintext-value")
        )

        token = request.inner_provider_settings["github_token"]
        assert isinstance(token, SecretStr)
        assert token.get_secret_value() == "plaintext-value"
        assert "plaintext-value" not in repr(request)
        assert request.model_dump(mode="json")["inner_provider_settings"] == {
            "github_token": "**********"
        }

    def test_model_validate_dict_coerces_all_three_credential_keys(self) -> None:
        request = AcaExecuteRequest.model_validate(
            self._minimal_request_dict(
                base_url="https://byok.example.com",
                api_key="plain-api-key",
                bearer_token="plain-bearer-token",
            )
        )

        settings = request.inner_provider_settings
        assert isinstance(settings["api_key"], SecretStr)
        assert isinstance(settings["bearer_token"], SecretStr)
        assert settings["api_key"].get_secret_value() == "plain-api-key"
        assert settings["bearer_token"].get_secret_value() == "plain-bearer-token"
        # `base_url` is not a credential — stays a plain string, unwrapped.
        assert settings["base_url"] == "https://byok.example.com"

        dumped = request.model_dump(mode="json")["inner_provider_settings"]
        assert dumped == {
            "base_url": "https://byok.example.com",
            "api_key": "**********",
            "bearer_token": "**********",
        }
        assert "plain-api-key" not in json.dumps(dumped)
        assert "plain-bearer-token" not in repr(request)

    def test_model_validate_json_coerces_plain_secret_to_secretstr(self) -> None:
        """Same guarantee for `model_validate_json` (raw wire bytes), not
        just a Python dict passed to `model_validate`."""
        body = json.dumps(self._minimal_request_dict(github_token="from-the-wire"))

        request = AcaExecuteRequest.model_validate_json(body)

        assert isinstance(request.inner_provider_settings["github_token"], SecretStr)
        assert request.inner_provider_settings["github_token"].get_secret_value() == "from-the-wire"
        assert "from-the-wire" not in repr(request)
        assert "from-the-wire" not in request.model_dump_json()

    def test_model_validate_is_idempotent_when_value_already_secretstr(self) -> None:
        """Constructing directly with an already-`SecretStr` value (the
        `AcaRuntimeProvider._resolve_inner_provider_settings` call site)
        must not be double-wrapped or otherwise mangled by the validator."""
        request = AcaExecuteRequest.model_validate(
            self._minimal_request_dict(github_token=SecretStr("already-wrapped"))
        )

        token = request.inner_provider_settings["github_token"]
        assert isinstance(token, SecretStr)
        assert token.get_secret_value() == "already-wrapped"

    def test_model_validate_none_inner_provider_settings_stays_none(self) -> None:
        request = AcaExecuteRequest.model_validate(
            {"agent": {"name": "implement"}, "rendered_prompt": "hi"}
        )

        assert request.inner_provider_settings is None


class TestAcaInterrupt:
    """Interrupt handling (E3-T6): in-stream interrupt frame + hard-abort fallback."""

    @pytest.mark.asyncio
    async def test_interrupt_signal_sends_interrupt_and_returns_partial(self) -> None:
        interrupt_signal = asyncio.Event()
        interrupt_signal.set()
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/interrupt":
                assert request.url.params["identifier"]
                return httpx.Response(200, json={"status": "ok"})
            raise AssertionError(f"unexpected path {request.url.path}")

        provider = _make_provider()
        provider._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        fake_response = _FakeInterruptResponse(
            [
                json.dumps({"type": "agent_message", "data": {"content": "working"}}),
                json.dumps(
                    {
                        "type": "result",
                        "data": {
                            "content": {"partial_summary": "stopped early"},
                            "partial": True,
                        },
                    }
                ),
            ]
        )
        provider._stream_execute = lambda *a, **kw: _FakeStreamContext(fake_response)  # type: ignore[method-assign]

        with patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential):
            output = await provider.execute(
                agent=_agent(),
                context={},
                rendered_prompt="x",
                interrupt_signal=interrupt_signal,
            )

        assert calls == ["/interrupt"]
        assert output.partial is True
        assert output.content == {"partial_summary": "stopped early"}
        assert interrupt_signal.is_set() is False

    @pytest.mark.asyncio
    async def test_interrupt_failure_falls_back_to_session_delete(self) -> None:
        """The hard-abort fallback uses ACA's real `DELETE /session` contract.

        Real ACA data-plane docs mark this operation "not supported for
        custom container session pools" — this fallback is still attempted
        best-effort (and may itself fail against a real pool), but `execute`
        must always return the partial result regardless of its outcome.
        """
        interrupt_signal = asyncio.Event()
        interrupt_signal.set()
        calls: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            if request.url.path == "/interrupt":
                return httpx.Response(500, json={"error": {"message": "boom"}})
            if request.url.path == "/session":
                assert request.method == "DELETE"
                assert request.url.params["identifier"]
                return httpx.Response(200, json={"status": "stopped"})
            raise AssertionError(f"unexpected path {request.url.path}")

        provider = _make_provider()
        provider._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        fake_response = _FakeInterruptResponse(
            [json.dumps({"type": "agent_message", "data": {}}) for _ in range(5)]
        )
        provider._stream_execute = lambda *a, **kw: _FakeStreamContext(fake_response)  # type: ignore[method-assign]

        with patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential):
            output = await provider.execute(
                agent=_agent(),
                context={},
                rendered_prompt="x",
                interrupt_signal=interrupt_signal,
            )

        assert calls == [("POST", "/interrupt"), ("DELETE", "/session")]
        assert output.partial is True
        assert output.content == {}


class TestAcaValidateConnection:
    """`validate_connection()` — management-plane + `/health` probe (E3-T6)."""

    @pytest.mark.asyncio
    async def test_validate_connection_success(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/health"
            assert request.headers["Authorization"] == "Bearer fake-aad-token"
            return httpx.Response(200, json={"status": "ok"})

        provider = _make_provider()
        provider._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential):
            assert await provider.validate_connection() is True

    @pytest.mark.asyncio
    async def test_validate_connection_sends_identifier(self) -> None:
        """Every container-path-forwarded request — `/health` included —
        requires an `identifier` query parameter (review fix)."""
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["identifier"] = request.url.params.get("identifier")
            return httpx.Response(200, json={"status": "ok"})

        provider = _make_provider()
        provider._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential):
            await provider.validate_connection()

        assert captured["identifier"]
        assert captured["identifier"] == provider._health_identifier

    @pytest.mark.asyncio
    async def test_validate_connection_raises_on_error_response(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": {"message": "pool not ready"}})

        provider = _make_provider()
        provider._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with (
            patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential),
            pytest.raises(ProviderError, match="pool not ready"),
        ):
            await provider.validate_connection()

    @pytest.mark.asyncio
    async def test_validate_connection_raises_when_token_acquisition_fails(self) -> None:
        provider = _make_provider()

        class _FailingCredential:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def get_token(self, *args: object, **kwargs: object) -> _FakeAccessToken:
                raise RuntimeError("no az login")

            async def close(self) -> None:
                pass

        with (
            patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FailingCredential),
            pytest.raises(ProviderError, match="access token"),
        ):
            await provider.validate_connection()


class TestAcaClose:
    """`close()` releases the httpx client and AAD credential (E3-T1)."""

    @pytest.mark.asyncio
    async def test_close_releases_http_client_and_credential(self) -> None:
        provider = _make_provider()
        with patch("conductor.providers.aca._AsyncDefaultAzureCredential", _FakeAsyncCredential):
            await provider._get_access_token()
        credential = provider._credential
        client = provider._ensure_client()
        assert provider._http_client is client

        await provider.close()

        credential.close.assert_awaited_once()
        assert provider._http_client is None
        assert provider._credential is None

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self) -> None:
        provider = _make_provider()
        await provider.close()
        await provider.close()


# ---------------------------------------------------------------------------
# E4-T5 — dialog turns (#284, OQ#5): the MVP runner exposes no dialog
# endpoint, so `AcaRuntimeProvider` overrides `execute_dialog_turn` to raise a
# clear, documented error rather than silently falling back to an on-host
# call (which would bypass the sandbox boundary). See
# docs/projects/aca/aca-provider.plan.md, epic E4.
# ---------------------------------------------------------------------------


class TestAcaDialogTurns:
    """`execute_dialog_turn()` is disabled with a clear error (OQ#5 fallback)."""

    @pytest.mark.asyncio
    async def test_execute_dialog_turn_raises_clear_provider_error(self) -> None:
        provider = _make_provider()
        with pytest.raises(ProviderError, match="dialog turn"):
            await provider.execute_dialog_turn(
                system_prompt="You are a helpful assistant.",
                user_message="hello",
            )

    @pytest.mark.asyncio
    async def test_execute_dialog_turn_error_is_not_retryable(self) -> None:
        """A missing runner dialog endpoint is a config-time fact, not transient."""
        provider = _make_provider()
        try:
            await provider.execute_dialog_turn(
                system_prompt="You are a helpful assistant.",
                user_message="hello",
            )
        except ProviderError as exc:
            assert exc.is_retryable is False
        else:
            pytest.fail("expected ProviderError")
