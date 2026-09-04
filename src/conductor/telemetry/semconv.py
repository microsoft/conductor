"""OpenTelemetry semantic-convention names used by Conductor tracing.

The GenAI names intentionally follow the experimental OpenTelemetry semantic
conventions. ``conductor.*`` attributes supplement them with workflow-specific
execution metadata.
"""

from __future__ import annotations

INVOKE_WORKFLOW = "invoke_workflow"
INVOKE_AGENT = "invoke_agent"
EXECUTE_TOOL = "execute_tool"

GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_CONVERSATION_ID = "gen_ai.conversation.id"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"
GEN_AI_TOOL_TYPE = "gen_ai.tool.type"
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
ERROR_TYPE = "error.type"

CONDUCTOR_COST_USD = "conductor.cost_usd"
CONDUCTOR_STEP_TYPE = "conductor.step.type"
CONDUCTOR_GROUP_NAME = "conductor.group.name"
CONDUCTOR_ITEM_KEY = "conductor.item.key"
CONDUCTOR_ITERATION = "conductor.iteration"
CONDUCTOR_RESUMED = "conductor.resumed"
CONDUCTOR_SUPERSEDED = "conductor.superseded"
CONDUCTOR_RUN_ID = "conductor.run_id"
