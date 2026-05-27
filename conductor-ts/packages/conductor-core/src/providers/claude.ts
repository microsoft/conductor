/**
 * ClaudeProvider — uses @anthropic-ai/sdk.
 * Mirrors src/conductor/providers/claude.py
 *
 * Reasoning effort maps to extended thinking budgets:
 *   low=2048, medium=8192, high=16384, xhigh=32768 tokens
 */
import type { AgentDef } from "../config/schema.js";
import type { AgentOutput, AgentProvider, ExecuteOptions } from "./base.js";
import { ProviderError } from "../exceptions.js";

type AnthropicType = import("@anthropic-ai/sdk").default;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type MessageStreamType = any;

let Anthropic: (new (opts: { apiKey?: string }) => AnthropicType) | undefined;

async function loadSdk(): Promise<void> {
  if (Anthropic) return;
  try {
    const sdk = await import("@anthropic-ai/sdk");
    Anthropic = sdk.default as unknown as new (opts: { apiKey?: string }) => AnthropicType;
  } catch {
    throw new ProviderError(
      "Anthropic SDK not found. Install it: npm install @anthropic-ai/sdk",
    );
  }
}

const THINKING_BUDGET: Record<string, number> = {
  low: 2048,
  medium: 8192,
  high: 16384,
  xhigh: 32768,
};

const MIN_MAX_TOKENS_WITH_THINKING = 16000;

export interface ClaudeProviderOptions {
  apiKey?: string;
  defaultModel?: string;
  defaultReasoningEffort?: string;
}

export class ClaudeProvider implements AgentProvider {
  private client: AnthropicType | undefined;
  private readonly options: ClaudeProviderOptions;

  constructor(options: ClaudeProviderOptions = {}) {
    this.options = {
      defaultModel: "claude-sonnet-4-5-20250929",
      ...options,
    };
  }

  async validateConnection(): Promise<void> {
    await loadSdk();
    this.getClient();
    // Lightweight check — list models
    await (this.getClient() as unknown as { models: { list: () => Promise<unknown> } }).models
      .list()
      .catch(() => {
        // Some keys don't have model listing; ignore
      });
  }

  async execute(agent: AgentDef, prompt: string, opts: ExecuteOptions = {}): Promise<AgentOutput> {
    await loadSdk();
    const client = this.getClient();

    const model = agent.model ?? this.options.defaultModel ?? "claude-sonnet-4-5-20250929";
    const effortStr = agent.reasoning?.effort ?? this.options.defaultReasoningEffort;
    const thinkingBudget = effortStr ? THINKING_BUDGET[effortStr] : undefined;

    const messages: Array<{ role: "user" | "assistant"; content: string }> = [
      { role: "user", content: prompt },
    ];

    const maxTokens = thinkingBudget
      ? Math.max(MIN_MAX_TOKENS_WITH_THINKING, thinkingBudget + 4096)
      : 8192;

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const requestParams: Record<string, any> = {
      model,
      max_tokens: maxTokens,
      messages,
      ...(agent.system_prompt ? { system: agent.system_prompt } : {}),
      ...(thinkingBudget
        ? { thinking: { type: "enabled", budget_tokens: thinkingBudget } }
        : {}),
      stream: true,
    };

    // When thinking is enabled, temperature must be 1.0 per Anthropic API
    if (thinkingBudget) {
      requestParams["temperature"] = 1.0;
    }

    let content = "";
    let reasoningContent = "";
    let inputTokens = 0;
    let outputTokens = 0;

    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const stream = (client as any).messages.stream(requestParams) as MessageStreamType;

      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      stream.on("text", (text: any) => {
        content += String(text);
      });

      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      stream.on("message", (msg: any) => {
        inputTokens = msg.usage?.input_tokens ?? 0;
        outputTokens = msg.usage?.output_tokens ?? 0;
        // Extract thinking blocks
        if (Array.isArray(msg.content)) {
          for (const block of msg.content) {
            if (block.type === "thinking") {
              reasoningContent += block.thinking ?? "";
            } else if (block.type === "text") {
              content = block.text ?? content;
            }
          }
        }
      });

      // Fire tool events (Claude tool use via SDK)
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      stream.on("streamEvent", (event: any) => {
        if (event.type === "content_block_start" && event.content_block?.type === "tool_use") {
          opts.emitter
            ?.emit("agent_tool_start", {
              agentName: agent.name,
              toolName: event.content_block.name,
            })
            .catch(() => undefined);
        }
      });

      await stream.finalMessage();
    } catch (err) {
      throw new ProviderError(`Claude API error: ${String(err)}`);
    }

    return {
      content,
      model,
      inputTokens,
      outputTokens,
      reasoningContent: reasoningContent || undefined,
    };
  }

  async close(): Promise<void> {
    this.client = undefined;
  }

  private getClient(): AnthropicType {
    if (!this.client) {
      if (!Anthropic) throw new ProviderError("Anthropic SDK not loaded.");
      this.client = new Anthropic({
        apiKey: this.options.apiKey ?? process.env["ANTHROPIC_API_KEY"],
      });
    }
    return this.client;
  }
}
