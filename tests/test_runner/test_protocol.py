"""Tests for ``conductor.runner.protocol`` — the backend-neutral runner wire contract.

These tests are the golden guard for the lift out of
``conductor.providers.aca_protocol``: the wire format is FROZEN, so the
key-set assertions below pin the exact ``model_dump(mode="json")`` shape of
every model, and the cross-parse tests prove that renaming the classes did
not change the on-the-wire field names either side already speaks.
"""

from __future__ import annotations

import json

import pytest
from pydantic import SecretStr

from conductor.runner.protocol import (
    RUNNER_PROTOCOL_VERSION,
    RUNNER_TOKEN_HEADER,
    RunnerAgentPayload,
    RunnerAgentRequest,
    RunnerAgentResult,
    RunnerErrorData,
    RunnerEventFrame,
    RunnerHealthResponse,
    request_to_wire_body,
)


def _sample_payload() -> RunnerAgentPayload:
    return RunnerAgentPayload(
        name="coder",
        model="gpt-5-mini",
        system_prompt="You are a coder.",
        output={"answer": {"type": "string"}},
        max_agent_iterations=10,
        max_session_seconds=120.0,
        reasoning_effort="high",
        working_dir="/workspace",
        retry={"max_retries": 2, "backoff_seconds": 1.0},
        context_tier="large",
    )


def _sample_request() -> RunnerAgentRequest:
    return RunnerAgentRequest(
        agent=_sample_payload(),
        rendered_prompt="Do the thing.",
        tools=["read"],
        mcp_servers={
            "fs": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"]}
        },
        context={"topic": "python"},
        inner_provider="copilot",
        inner_provider_settings={
            "base_url": "https://example.test",
            "api_key": SecretStr("sk-test"),
        },
        tool_output={"max_chars": 1000, "spill_to_file": False},
    )


class TestConstants:
    # Requirement: the transport-token header keeps its exact wire name — it is
    # the one definition shared by host and runner, and any drift breaks auth.
    def test_runner_token_header_value(self) -> None:
        assert RUNNER_TOKEN_HEADER == "X-Conductor-Runner-Token"

    # Requirement: the wire protocol starts at version 1 — version
    # advertisement was introduced together with this module.
    def test_protocol_version_is_one(self) -> None:
        assert RUNNER_PROTOCOL_VERSION == 1


class TestRoundTrips:
    # Requirement: every model survives a dump/parse cycle unchanged — the
    # models are the serialization contract, so nothing may be lost in transit.
    # (RunnerAgentRequest is excluded: its dump redacts secrets by design, so
    # its lossless round-trip goes through the wire body — see the dedicated
    # test below.)
    @pytest.mark.parametrize(
        "model",
        [
            _sample_payload(),
            RunnerEventFrame(type="agent_message", data={"text": "hello"}),
            RunnerAgentResult(
                content={"answer": "42"},
                model="gpt-5-mini",
                input_tokens=10,
                output_tokens=5,
                cache_read_tokens=1,
                cache_write_tokens=2,
                last_call_input_tokens=10,
                session_seconds=3.5,
                partial=False,
            ),
            RunnerErrorData(message="boom"),
            RunnerHealthResponse(
                ready=True,
                conductor_version="1.2.3",
                runner_version="0.1.0",
                protocol_version=1,
                auth_required=False,
                auth_token_present=False,
            ),
        ],
    )
    def test_round_trip(self, model: object) -> None:
        dumped = model.model_dump(mode="json")  # type: ignore[attr-defined]
        reparsed = type(model).model_validate(dumped)  # type: ignore[attr-defined]
        assert reparsed == model

    # Requirement: the request round-trips LOSSLESSLY through the wire —
    # request_to_wire_body is the one place plaintext exists, so parsing the
    # wire body back must restore the exact model, secrets included. (A plain
    # model_dump(mode="json") round-trip cannot be lossless by design: dumps
    # redact SecretStr.)
    def test_request_round_trip_via_wire_body(self) -> None:
        request = _sample_request()
        reparsed = RunnerAgentRequest.model_validate(request_to_wire_body(request))
        assert reparsed == request


class TestWireGoldenKeySets:
    """Frozen exact key sets of ``model_dump(mode="json")`` — the wire format.

    If a field is added, renamed, or dropped, exactly one assertion here
    fails; that is deliberate: the wire is frozen, and any change must be a
    conscious, reviewed decision accompanied by an update to this golden.
    """

    # Requirement: RunnerAgentPayload serializes to exactly these keys on the
    # wire — all 10 lifted fields, no more, no less.
    def test_payload_wire_keys(self) -> None:
        assert set(_sample_payload().model_dump(mode="json").keys()) == {
            "name",
            "model",
            "system_prompt",
            "output",
            "max_agent_iterations",
            "max_session_seconds",
            "reasoning_effort",
            "working_dir",
            "retry",
            "context_tier",
        }

    # Requirement: RunnerAgentRequest serializes to exactly these keys — the
    # envelope of the /execute POST body is frozen.
    def test_request_wire_keys(self) -> None:
        assert set(_sample_request().model_dump(mode="json").keys()) == {
            "agent",
            "rendered_prompt",
            "tools",
            "mcp_servers",
            "context",
            "inner_provider",
            "inner_provider_settings",
            "tool_output",
        }

    # Requirement: RunnerAgentResult serializes to exactly these keys — the
    # terminal result frame shape is frozen.
    def test_result_wire_keys(self) -> None:
        result = RunnerAgentResult(
            content={"answer": "42"},
            model="gpt-5-mini",
            input_tokens=10,
            output_tokens=5,
            cache_read_tokens=1,
            cache_write_tokens=2,
            last_call_input_tokens=10,
            session_seconds=3.5,
            partial=False,
        )
        assert set(result.model_dump(mode="json").keys()) == {
            "content",
            "model",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "last_call_input_tokens",
            "session_seconds",
            "partial",
        }

    # Requirement: RunnerErrorData is the REDUCED neutral shape — only
    # `message` (no ACA-specific `code`/`trace_id`).
    def test_error_wire_keys(self) -> None:
        assert set(RunnerErrorData(message="boom").model_dump(mode="json").keys()) == {"message"}

    # Requirement: RunnerEventFrame documents the NDJSON frame shape
    # {"type", "data"} and nothing more.
    def test_event_frame_wire_keys(self) -> None:
        frame = RunnerEventFrame(type="agent_message", data={"text": "hi"})
        assert set(frame.model_dump(mode="json").keys()) == {"type", "data"}

    # Requirement: RunnerHealthResponse covers exactly the six health keys —
    # the version-advertisement payload shape is frozen.
    def test_health_wire_keys(self) -> None:
        health = RunnerHealthResponse(
            ready=True,
            conductor_version="1.2.3",
            runner_version="0.1.0",
            protocol_version=1,
            auth_required=False,
            auth_token_present=False,
        )
        assert set(health.model_dump(mode="json").keys()) == {
            "ready",
            "conductor_version",
            "runner_version",
            "protocol_version",
            "auth_required",
            "auth_token_present",
        }

    # Requirement: the default error message is the neutral runner wording,
    # not the ACA-specific one.
    def test_error_default_message_is_neutral(self) -> None:
        assert RunnerErrorData().message == "runner reported an error"


class TestCrossParseLegacyWireNames:
    """A JSON literal written with the LEGACY wire field names (the exact
    field names of ``conductor.providers.aca_protocol``) validates against
    the new models — proving that renaming the classes did not change the
    wire."""

    # Requirement: a legacy-shaped request body (exactly the wire keys the
    # current aca host and runner exchange) parses against RunnerAgentRequest.
    def test_legacy_request_json_parses(self) -> None:
        legacy_json = {
            "agent": {
                "name": "coder",
                "model": "gpt-5-mini",
                "system_prompt": "sys",
                "output": {"answer": {"type": "string"}},
                "max_agent_iterations": 10,
                "max_session_seconds": 120.0,
                "reasoning_effort": "high",
                "working_dir": "/workspace",
                "retry": {"max_retries": 2},
                "context_tier": "large",
            },
            "rendered_prompt": "Do the thing.",
            "tools": ["read"],
            "mcp_servers": {"fs": {"command": "npx"}},
            "context": {"topic": "python"},
            "inner_provider": "copilot",
            "inner_provider_settings": {"github_token": "ghp_legacy"},
            "tool_output": {"max_chars": 1000},
        }
        request = RunnerAgentRequest.model_validate(legacy_json)
        assert request.agent.name == "coder"
        assert request.rendered_prompt == "Do the thing."

    # Requirement: a legacy error body carrying the ACA-specific `code` and
    # `traceId` keys still parses against the reduced RunnerErrorData —
    # `extra="ignore"` keeps old wire bodies readable.
    def test_legacy_error_json_parses(self) -> None:
        legacy_error = {"message": "pool busy", "code": "SessionPoolTimeout", "traceId": "abc-123"}
        parsed = RunnerErrorData.model_validate(legacy_error)
        assert parsed.message == "pool busy"

    # Requirement: a legacy terminal result frame (the keys AcaResultData
    # speaks today) parses against RunnerAgentResult unchanged.
    def test_legacy_result_json_parses(self) -> None:
        legacy_result = {
            "content": {"answer": "42"},
            "model": "gpt-5-mini",
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_tokens": 1,
            "cache_write_tokens": 2,
            "last_call_input_tokens": 10,
            "session_seconds": 3.5,
            "partial": False,
        }
        parsed = RunnerAgentResult.model_validate(legacy_result)
        assert parsed.content == {"answer": "42"}

    # Requirement: a legacy NDJSON event line ({"type", "data"}) validates
    # against RunnerEventFrame — the documenting model matches the real frame.
    def test_legacy_event_frame_json_parses(self) -> None:
        frame = json.loads('{"type": "agent_message", "data": {"text": "hi"}}')
        parsed = RunnerEventFrame.model_validate(frame)
        assert parsed.type == "agent_message"
        assert parsed.data == {"text": "hi"}


class TestSecretRedaction:
    """`inner_provider_settings` secrets must be SecretStr on every validated
    construction path; plaintext exists only in `request_to_wire_body`."""

    # Requirement: constructing via __init__ with a PLAINTEXT credential string
    # still yields a SecretStr — the redaction validator runs on validated
    # construction regardless of call site.
    def test_init_wraps_plaintext_secrets(self) -> None:
        request = RunnerAgentRequest(
            agent=_sample_payload(),
            rendered_prompt="p",
            inner_provider_settings={"github_token": "ghp_plaintext"},
        )
        assert request.inner_provider_settings is not None
        token = request.inner_provider_settings["github_token"]
        assert isinstance(token, SecretStr)
        assert token.get_secret_value() == "ghp_plaintext"

    # Requirement: model_validate on a plain dict wraps plaintext credentials
    # (the runner's own FastAPI request-parsing path must not retain plaintext).
    def test_model_validate_wraps_plaintext_secrets(self) -> None:
        request = RunnerAgentRequest.model_validate(
            {
                "agent": {"name": "a"},
                "rendered_prompt": "p",
                "inner_provider_settings": {
                    "api_key": "sk_plaintext",
                    "base_url": "https://x.test",
                },
            }
        )
        settings = request.inner_provider_settings
        assert settings is not None
        assert isinstance(settings["api_key"], SecretStr)
        # base_url is not a credential key and stays a plain str.
        assert settings["base_url"] == "https://x.test"

    # Requirement: model_validate_json wraps plaintext credentials — a JSON
    # wire body validated into the model is redacted immediately.
    def test_model_validate_json_wraps_plaintext_secrets(self) -> None:
        request = RunnerAgentRequest.model_validate_json(
            json.dumps(
                {
                    "agent": {"name": "a"},
                    "rendered_prompt": "p",
                    "inner_provider_settings": {"bearer_token": "bk_plaintext"},
                }
            )
        )
        assert request.inner_provider_settings is not None
        token = request.inner_provider_settings["bearer_token"]
        assert isinstance(token, SecretStr)
        assert token.get_secret_value() == "bk_plaintext"

    # Requirement: the redaction validator is idempotent — values already
    # SecretStr pass through unchanged across repeated validation (python-mode
    # dumps keep the SecretStr objects, so the secret survives re-validation).
    def test_redaction_is_idempotent(self) -> None:
        request = _sample_request()
        revalidated = RunnerAgentRequest.model_validate(request.model_dump())
        assert revalidated.inner_provider_settings is not None
        token = revalidated.inner_provider_settings["api_key"]
        assert isinstance(token, SecretStr)
        assert token.get_secret_value() == "sk-test"

    # Requirement: SecretStr values are REDACTED in model_dump and repr —
    # nothing that logs or stringifies the request leaks the credential.
    def test_dump_and_repr_redact_secrets(self) -> None:
        request = _sample_request()
        dumped = json.dumps(request.model_dump(mode="json"))
        assert "sk-test" not in dumped
        assert "**********" in dumped
        assert "sk-test" not in repr(request)

    # Requirement: request_to_wire_body is the ONLY place plaintext appears —
    # it unwraps the SecretStr values for the bytes that actually leave the
    # process, reading them off the model (not off the redacted dump).
    def test_request_to_wire_body_unwraps_secrets(self) -> None:
        request = _sample_request()
        body = request_to_wire_body(request)
        assert body["inner_provider_settings"]["api_key"] == "sk-test"
        assert body["inner_provider_settings"]["base_url"] == "https://example.test"

    # Requirement: request_to_wire_body leaves the request itself redacted —
    # after serialization the model still holds SecretStr, so later dumps
    # cannot leak what the wire step briefly exposed.
    def test_request_to_wire_body_does_not_mutate_request(self) -> None:
        request = _sample_request()
        request_to_wire_body(request)
        assert request.inner_provider_settings is not None
        assert isinstance(request.inner_provider_settings["api_key"], SecretStr)

    # Requirement: request_to_wire_body handles a request WITHOUT
    # inner_provider_settings (None) — no KeyError on the optional field.
    def test_request_to_wire_body_without_secrets(self) -> None:
        request = RunnerAgentRequest(agent=_sample_payload(), rendered_prompt="p")
        body = request_to_wire_body(request)
        assert body["inner_provider_settings"] is None

    # Requirement: every other field of the wire body matches
    # model_dump(mode="json") exactly — the wire helper only special-cases
    # the secret-bearing dict.
    def test_request_to_wire_body_matches_dump_elsewhere(self) -> None:
        request = _sample_request()
        body = request_to_wire_body(request)
        dump = request.model_dump(mode="json")
        for key in dump:
            if key != "inner_provider_settings":
                assert body[key] == dump[key]


class TestStrictnessModes:
    # Requirement: request models reject unknown keys — a sender cannot
    # smuggle undeclared fields past receiver validation.
    def test_request_forbids_extra_keys(self) -> None:
        with pytest.raises(ValueError, match="[Ee]xtra"):
            RunnerAgentRequest.model_validate(
                {
                    "agent": {"name": "a"},
                    "rendered_prompt": "p",
                    "surprise": True,
                }
            )

    # Requirement: response models ignore unknown keys — an old host against a
    # new runner must not choke on keys it does not know.
    def test_result_ignores_extra_keys(self) -> None:
        parsed = RunnerAgentResult.model_validate({"content": {}, "future_field": 123})
        assert parsed.content == {}

    # Requirement: the payload model is also strict — the inner agent
    # descriptor cannot carry undeclared fields either.
    def test_payload_forbids_extra_keys(self) -> None:
        with pytest.raises(ValueError, match="[Ee]xtra"):
            RunnerAgentPayload.model_validate({"name": "a", "surprise": True})


class TestHealthResponse:
    # Requirement: a health payload from an OLD runner (no protocol_version)
    # parses with protocol_version=None — the host skips the compatibility
    # check instead of failing against runners that predate version
    # advertisement.
    def test_parses_payload_without_protocol_version(self) -> None:
        old_runner_payload = {
            "ready": True,
            "conductor_version": "1.0.0",
            "runner_version": "0.1.0",
            "auth_required": True,
            "auth_token_present": False,
        }
        health = RunnerHealthResponse.model_validate(old_runner_payload)
        assert health.ready is True
        assert health.protocol_version is None

    # Requirement: a health payload with EXTRA unknown keys (a newer runner)
    # parses cleanly — forward compatibility is the point of extra="ignore".
    def test_parses_payload_with_unknown_extra_keys(self) -> None:
        new_runner_payload = {
            "ready": True,
            "conductor_version": "1.0.0",
            "runner_version": "0.2.0",
            "protocol_version": 1,
            "auth_required": False,
            "auth_token_present": False,
            "future_diagnostic": "something",
        }
        health = RunnerHealthResponse.model_validate(new_runner_payload)
        assert health.protocol_version == 1

    # Requirement: the minimal health payload (just `ready`) parses — `ready`
    # is the only mandatory key.
    def test_parses_minimal_payload(self) -> None:
        health = RunnerHealthResponse.model_validate({"ready": False})
        assert health.ready is False
        assert health.conductor_version is None
        assert health.runner_version is None
        assert health.protocol_version is None
        assert health.auth_required is None
        assert health.auth_token_present is None
