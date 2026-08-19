"""Real API integration tests for the native OpenAI provider.

These tests require:
- OPENAI_API_KEY environment variable (or a reachable OpenAI-compatible endpoint)
- Network connectivity to the configured endpoint

Run with: pytest -m real_api
Skip with: pytest -m "not real_api" (default)

An OpenAI-compatible custom endpoint (Ollama, vLLM, a local proxy, ...) can be
exercised instead of api.openai.com by setting CONDUCTOR_TEST_OPENAI_BASE_URL
and CONDUCTOR_TEST_OPENAI_MODEL alongside OPENAI_API_KEY:

    export CONDUCTOR_TEST_OPENAI_BASE_URL=http://localhost:20128/v1/
    export CONDUCTOR_TEST_OPENAI_MODEL=deepseekai/DeepSeek-V4-Flash-0731
    export OPENAI_API_KEY=sk-...
"""

import os

import pytest

from conductor.config.schema import (
    AgentDef,
    OutputField,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.providers.openai import OpenAIProvider

# A custom endpoint requires an explicit api_key (the provider refuses to
# forward an ambient OPENAI_API_KEY to a non-OpenAI endpoint), so read both.
_TEST_BASE_URL = os.getenv("CONDUCTOR_TEST_OPENAI_BASE_URL")
_TEST_MODEL = os.getenv("CONDUCTOR_TEST_OPENAI_MODEL", "gpt-5-mini")


@pytest.mark.real_api
class TestOpenAIRealAPI:
    """Real API tests (require OPENAI_API_KEY)."""

    @pytest.fixture
    def provider_kwargs(self) -> dict[str, str | None]:
        """Resolve the endpoint and key for the provider under test.

        Skips when no credential is available. When a custom base URL is
        configured the API key is passed explicitly, satisfying the
        "custom base_url requires an explicit api_key" rule.
        """
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            pytest.skip("OPENAI_API_KEY not set - skipping real API test")
        kwargs: dict[str, str | None] = {"api_key": api_key}
        if _TEST_BASE_URL:
            kwargs["base_url"] = _TEST_BASE_URL
        return kwargs

    @pytest.mark.asyncio
    async def test_real_simple_qa(self, provider_kwargs: dict[str, str | None]) -> None:
        """Test real API call with simple Q&A workflow."""
        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="real-qa-test",
                description="Real API Q&A test",
                entry_point="qa_agent",
                runtime=RuntimeConfig(provider={"name": "openai"}),
            ),
            agents=[
                AgentDef(
                    name="qa_agent",
                    model=_TEST_MODEL,
                    prompt="Answer this question concisely: {{ workflow.input.question }}",
                    output={"answer": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                )
            ],
            output={"qa_answer": "{{ qa_agent.output.answer }}"},
        )

        provider = OpenAIProvider(**provider_kwargs)  # type: ignore[arg-type]

        # Verify connection before running workflow
        is_connected = await provider.validate_connection()
        assert is_connected, "Failed to connect to OpenAI API"

        engine = WorkflowEngine(workflow, provider)

        result = await engine.run({"question": "What is 2+2?"})

        # Verify result
        assert "qa_answer" in result
        answer = result["qa_answer"].lower()
        assert "4" in answer or "four" in answer

        # Cleanup
        await provider.close()

    @pytest.mark.asyncio
    async def test_real_structured_output(self, provider_kwargs: dict[str, str | None]) -> None:
        """Test structured output parsing with real API."""
        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="real-structured-test",
                description="Real API structured output test",
                entry_point="classifier",
                runtime=RuntimeConfig(provider={"name": "openai"}),
            ),
            agents=[
                AgentDef(
                    name="classifier",
                    model=_TEST_MODEL,
                    prompt=(
                        "Classify the sentiment of this text as positive, negative, or neutral: "
                        "{{ workflow.input.text }}"
                    ),
                    output={
                        "sentiment": OutputField(type="string"),
                        "confidence": OutputField(type="number"),
                    },
                    routes=[RouteDef(to="$end")],
                )
            ],
            output={"sentiment": "{{ classifier.output.sentiment }}"},
        )

        provider = OpenAIProvider(**provider_kwargs)  # type: ignore[arg-type]
        engine = WorkflowEngine(workflow, provider)

        result = await engine.run({"text": "I absolutely love this product!"})

        assert "sentiment" in result
        assert result["sentiment"].lower() in (
            "positive",
            "negative",
            "neutral",
        )

        await provider.close()
