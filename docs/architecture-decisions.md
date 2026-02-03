# Architecture Decision Records

This document tracks key architectural decisions made during the development of Conductor.

## ADR 001: Tool-Based Structured Output for Claude Provider

**Status**: Accepted  
**Date**: 2026-02  
**Deciders**: Development Team

### Context

The Claude provider needs to extract structured JSON output from agent responses to match workflow output schemas. There are two primary approaches:

1. **Prompt Engineering**: Instruct the model via prompt to return JSON, then parse the text response
2. **Tool-Based Extraction**: Define an `emit_output` tool that accepts the output schema, forcing the model to use the tool

### Decision

We will use **tool-based structured output** via a dynamically generated `emit_output` tool for the Claude provider.

### Rationale

1. **Higher Reliability**: Tool-based extraction leverages Claude's native tool-calling mechanism, which has stronger guarantees than text parsing. The model is explicitly instructed to return structured data via the tool schema.

2. **Type Safety**: Tool schemas are validated by the SDK before sending to the API, catching schema errors early.

3. **Clear Fallback Path**: If the model returns text instead of using the tool, we can:
   - Attempt JSON extraction from the text (fallback)
   - Send a recovery message asking the model to use the tool (parse recovery)
   
4. **Consistent with SDK Best Practices**: The Anthropic SDK documentation recommends tool-based extraction for structured output scenarios.

5. **Future-Proof**: Tool-based extraction aligns with potential future SDK features for structured output.

### Implementation Details

- The `emit_output` tool is dynamically generated from the workflow's output schema
- Tool schema includes field names, types, and descriptions
- The model receives an instruction to use the tool in the prompt
- Parse recovery (up to 2 attempts) handles cases where the model ignores the tool

### Trade-offs Accepted

- **Extra API Call for Recovery**: Parse recovery adds 1-2 extra API calls in failure cases (mitigated by making this rare through clear instructions)
- **Tool Overhead**: Tool definitions add ~500-1000 tokens to the request (acceptable for improved reliability)
- **Non-Streaming Constraint**: Tool-based extraction requires non-streaming API calls in Phase 1 (Phase 2 will add streaming support)

### Alternatives Considered

#### Prompt Engineering Only

**Rejected** because:
- Lower reliability: Models sometimes ignore JSON formatting instructions
- Harder to debug: Text parsing failures require inspecting raw text
- More brittle: Prone to edge cases (markdown code blocks, incomplete JSON, etc.)
- Complex recovery: Would need multiple heuristics for different failure modes

#### Prompt + Validation + Retry

**Rejected** because:
- Still relies on text parsing as primary path
- Retry logic would be expensive (full re-execution vs recovery message)
- Doesn't leverage SDK's structured output capabilities

### Consequences

**Positive:**
- Structured output extraction is reliable and consistent (< 5% parse recovery rate)
- Clear error messages when extraction fails
- Easy to extend with additional validation
- Aligns with Anthropic SDK best practices

**Negative:**
- Parse recovery adds latency in failure cases (~2-3 seconds per recovery attempt)
- Tool definitions consume input tokens (minor impact on cost, < 10% in typical workflows)
- Requires Phase 2 work for streaming support

### References

- Anthropic SDK Documentation: https://github.com/anthropics/anthropic-sdk-python
- Claude API Reference: https://docs.anthropic.com/en/api/messages
- Implementation: `src/conductor/providers/claude.py`
- Related Tests: `tests/test_providers/test_claude_parse_recovery.py`

---

## ADR 002: Phase 2 Deferral of MCP Tool Support for Claude

**Status**: Accepted  
**Date**: 2026-02  
**Deciders**: Development Team

### Context

The GitHub Copilot provider supports MCP (Model Context Protocol) tool integration, allowing workflows to expose external tools to agents. The question is whether to include MCP support in the initial Claude provider release (Phase 1) or defer it to Phase 2.

### Decision

We will **defer MCP tool support to Phase 2** for the Claude provider.

### Rationale

1. **Production Readiness**: Phase 1 focuses on core functionality needed for production workflows:
   - Non-streaming execution ✅
   - Structured output ✅
   - Error handling and retry logic ✅
   - Parameter configuration ✅

2. **Scope Management**: MCP integration is a cross-cutting concern that requires:
   - Tool discovery and registration
   - Tool invocation and response handling
   - State management across tool calls
   - This adds significant complexity beyond basic agent execution

3. **Testing Requirements**: MCP support requires extensive integration testing with real MCP servers, which is outside the scope of Phase 1 verification.

4. **Clear Upgrade Path**: The architecture is designed to accommodate MCP support in Phase 2 without breaking changes to existing workflows.

### Implementation Strategy for Phase 2

When MCP support is added in Phase 2:

1. **Tool Translation**: MCP tool schemas will be translated to Claude tool format (similar to Copilot provider)
2. **Tool Execution**: Tool calls will be executed via MCP server connections and results returned to Claude
3. **Multi-Turn Support**: Claude's messages API naturally supports multi-turn conversations for tool execution
4. **Backward Compatibility**: Existing workflows without MCP tools will continue to work unchanged

### Consequences

**Positive:**
- Phase 1 ships faster with reduced scope
- Core functionality is production-ready without MCP complexity
- Architecture allows clean Phase 2 integration

**Negative:**
- Claude provider cannot use MCP tools until Phase 2
- Workflows requiring external tools must use Copilot provider in Phase 1

### Migration Path

When Phase 2 is released:

1. Update `conductor` to latest version
2. Add `mcp_servers` configuration to workflow YAML (same as Copilot provider)
3. No code changes needed for existing workflows
4. New workflows can leverage MCP tools immediately

### References

- MCP Protocol Specification: https://modelcontextprotocol.io/
- Copilot Provider MCP Implementation: `src/conductor/providers/copilot.py`
- Phase 2 Planning: `add-claude-sdk-support.plan.md` (Section 5)
