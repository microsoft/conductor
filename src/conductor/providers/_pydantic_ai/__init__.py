"""Internal helpers for Pydantic AI-based provider implementations.

This package contains adapters shared by providers that use Pydantic AI
(currently ``conductor.providers.claude``) to build Pydantic AI agents,
bridge MCP tools, convert output schemas, map streaming events, and run the
shared interrupt-aware retry/execution pipeline. The underscore prefix signals
that these modules are not a public API of Conductor.
"""
