/**
 * CopilotCliProvider — uses @github/copilot-sdk (TypeScript SDK).
 * Mirrors src/conductor/providers/copilot.py
 *
 * The SDK spawns the bundled Copilot CLI binary and communicates via JSON-RPC.
 * All the framing is handled internally by @github/copilot-sdk.
 */
import type { AgentDef } from "../config/schema.js";
import type { AgentOutput, AgentProvider, ExecuteOptions } from "./base.js";
import { ProviderError, ValidationError } from "../exceptions.js";

// The @github/copilot-sdk package is optional — provider validation checks at runtime.
type CopilotClientType = import("@github/copilot-sdk").CopilotClient;
type ApproveAllType = typeof import("@github/copilot-sdk").approveAll;

let CopilotClient: (new () => CopilotClientType) | undefined;
let approveAll: ApproveAllType | undefined;

async function loadSdk(): Promise<void> {
  if (CopilotClient) return;
  try {
    const sdk = await import("@github/copilot-sdk");
    CopilotClient = sdk.CopilotClient as unknown as new () => CopilotClientType;
    approveAll = sdk.approveAll as ApproveAllType;
  } catch {
    throw new ProviderError(
      "GitHub Copilot SDK not found. Install it: npm install @github/copilot-sdk",
    );
  }
}

/** Reasoning effort → SDK reasoningEffort string */
const EFFORT_MAP: Record<string, "low" | "medium" | "high" | "xhigh"> = {
  low: "low",
  medium: "medium",
  high: "high",
  xhigh: "xhigh",
};

export interface CopilotCliProviderOptions {
  /** CLI binary path override (equivalent to COPILOT_CLI_PATH env var). */
  cliPath?: string;
  /** Default model to use when agent doesn't specify one. */
  defaultModel?: string;
  /** Default reasoning effort. */
  defaultReasoningEffort?: string;
}

export class CopilotCliProvider implements AgentProvider {
  private client: CopilotClientType | undefined;
  private readonly options: CopilotCliProviderOptions;

  constructor(options: CopilotCliProviderOptions = {}) {
    this.options = options;
  }

  async validateConnection(): Promise<void> {
    await loadSdk();
    const client = this.getClient();
    await client.start();
    await client.ping();
  }

  async execute(agent: AgentDef, prompt: string, opts: ExecuteOptions = {}): Promise<AgentOutput> {
    await loadSdk();
    const client = this.getClient();
    if (!this.client) await client.start();

    const model = agent.model ?? this.options.defaultModel;
    const effortStr =
      agent.reasoning?.effort ?? this.options.defaultReasoningEffort;
    const effort = effortStr ? EFFORT_MAP[effortStr] : undefined;

    const skillDirs = [
      ...(opts.skillDirectories ?? []),
      ...(agent.skill_directories ?? []),
    ];

    // Append JSON schema instructions when the agent declares an output schema —
    // mirrors Python CopilotProvider behaviour.
    let fullPrompt = prompt;
    if (agent.output && Object.keys(agent.output).length > 0) {
      const schemaDesc = JSON.stringify(agent.output, null, 2);
      fullPrompt +=
        `\n\n**IMPORTANT: You MUST respond with a JSON object matching this schema:**\n` +
        `\`\`\`json\n${schemaDesc}\n\`\`\`\n` +
        `Return ONLY the JSON object, no other text.`;
    }

    let content = "";
    let reasoningContent = "";
    let inputTokens = 0;
    let outputTokens = 0;
    let resolvedModel = model ?? "gpt-4o";

    const session = await client.createSession({
      ...(model ? { model } : {}),
      ...(effort ? { reasoningEffort: effort } : {}),
      ...(skillDirs.length > 0 ? { skillDirectories: skillDirs } : {}),
      streaming: true,
      onPermissionRequest: approveAll!,
      onUserInputRequest: opts.onUserInputRequest
        ? async (req) => {
            const response = await opts.onUserInputRequest!({
              question: req.question ?? "",
              choices: req.choices,
              allowFreeform: req.allowFreeform,
            });
            return { answer: response.answer, wasFreeform: response.wasFreeform };
          }
        : undefined,
    });

    try {
      // Subscribe to events before sending
      session.on("assistant.message_delta", (event) => {
        content += (event as { data: { deltaContent: string } }).data.deltaContent ?? "";
      });
      session.on("assistant.reasoning_delta", (event) => {
        reasoningContent +=
          (event as { data: { deltaContent: string } }).data.deltaContent ?? "";
      });
      session.on("assistant.message", (event) => {
        const e = event as {
          data: { content: string; model?: string; inputTokens?: number; outputTokens?: number };
        };
        content = e.data.content;
        if (e.data.model) resolvedModel = e.data.model;
        if (e.data.inputTokens) inputTokens = e.data.inputTokens;
        if (e.data.outputTokens) outputTokens = e.data.outputTokens;
      });
      session.on("tool.execution_start", (event) => {
        const e = event as { data: { toolName: string; args?: unknown } };
        opts.emitter?.emit("agent_tool_start", {
          agentName: agent.name,
          toolName: e.data.toolName,
          args: e.data.args,
        }).catch(() => undefined);
      });
      session.on("tool.execution_complete", (event) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const e = event as unknown as { data: { toolName: string; result?: unknown } };
        opts.emitter?.emit("agent_tool_complete", {
          agentName: agent.name,
          toolName: e.data.toolName,
          result: e.data.result,
        }).catch(() => undefined);
      });

      const systemPrompt = agent.system_prompt;
      const sendPrompt = systemPrompt
        ? `${systemPrompt}\n\n${fullPrompt}`
        : fullPrompt;

      await session.sendAndWait({ prompt: sendPrompt });
    } finally {
      await session.disconnect();
    }

    return {
      content,
      model: resolvedModel,
      inputTokens,
      outputTokens,
      reasoningContent: reasoningContent || undefined,
    };
  }

  async close(): Promise<void> {
    if (this.client) {
      await this.client.stop();
      this.client = undefined;
    }
  }

  private getClient(): CopilotClientType {
    if (!this.client) {
      if (!CopilotClient) {
        throw new ProviderError("Copilot SDK not loaded. Call validateConnection() first.");
      }
      this.client = new CopilotClient();
    }
    return this.client;
  }
}
