/**
 * VscodeLmProvider — uses vscode.lm.selectChatModels() + sendRequest().
 * This provider only works inside a VS Code extension context.
 */
import * as vscode from "vscode";
import type { AgentDef } from "@conductor/core";
import type { AgentOutput, AgentProvider, ExecuteOptions } from "@conductor/core";
import { ProviderError } from "@conductor/core";
import { log, logError } from "../logger.js";

export interface VscodeLmProviderOptions {
  modelId?: string;
  token?: vscode.CancellationToken;
}

export class VscodeLmProvider implements AgentProvider {
  private readonly options: VscodeLmProviderOptions;

  constructor(options: VscodeLmProviderOptions = {}) {
    this.options = options;
  }

  async validateConnection(): Promise<void> {
    const models = await vscode.lm.selectChatModels({});
    if (!models.length) {
      throw new ProviderError(
        "No VS Code language models available. Ensure GitHub Copilot is installed and signed in.",
      );
    }
  }

  async execute(agent: AgentDef, prompt: string, opts: ExecuteOptions = {}): Promise<AgentOutput> {
    const modelId = agent.model ?? this.options.modelId;
    log(`[vscode-lm] execute agent='${agent.name}' modelId='${modelId ?? "(any)"}'`);
    const models = await vscode.lm.selectChatModels(
      modelId ? { id: modelId } : {},
    );
    log(`[vscode-lm] selectChatModels returned ${models.length} model(s): ${models.map((m) => m.id).join(", ")}`);

    const model = models[0];
    if (!model) {
      logError(`[vscode-lm] no model found for '${modelId}'`);
      throw new ProviderError(
        `No VS Code language model found${modelId ? ` for '${modelId}'` : ""}`,
      );
    }
    log(`[vscode-lm] using model id='${model.id}'`);

    const token = this.options.token ?? new vscode.CancellationTokenSource().token;
    log(`[vscode-lm] cancellation token from chat: ${!!this.options.token}`);

    // Append JSON schema instructions when the agent declares an output schema,
    // mirroring CopilotCliProvider behaviour.
    let fullPrompt = prompt;
    if (agent.output && Object.keys(agent.output).length > 0) {
      const schemaDesc = JSON.stringify(agent.output, null, 2);
      fullPrompt +=
        `\n\n**IMPORTANT: You MUST respond with a JSON object matching this schema:**\n` +
        `\`\`\`json\n${schemaDesc}\n\`\`\`\n` +
        `Return ONLY the JSON object, no other text.`;
    }

    const messages: vscode.LanguageModelChatMessage[] = [];
    if (agent.system_prompt) {
      messages.push(vscode.LanguageModelChatMessage.User(agent.system_prompt));
    }
    messages.push(vscode.LanguageModelChatMessage.User(fullPrompt));

    // Signal that the model request is about to start.
    log(`[vscode-lm] emitting agent_turn_start, hasEmitter=${!!opts.emitter}`);
    opts.emitter?.emit("agent_turn_start", {
      agentName: agent.name,
      turn: "awaiting_model",
    }).catch((e) => logError("[vscode-lm] agent_turn_start emit failed", e));

    // Count input tokens upfront via the VS Code LM API.
    let inputTokens = 0;
    let outputTokens = 0;
    try {
      const counts = await Promise.all(messages.map((m) => model.countTokens(m, token)));
      inputTokens = counts.reduce((a, b) => a + b, 0);
      log(`[vscode-lm] inputTokens=${inputTokens}`);
    } catch (e) {
      log(`[vscode-lm] countTokens (input) failed: ${e}`);
    }

    let content = "";

    try {
      log(`[vscode-lm] calling model.sendRequest with ${messages.length} message(s)`);
      const response = await model.sendRequest(messages, {}, token);
      log(`[vscode-lm] sendRequest returned, streaming...`);
      let partCount = 0;
      for await (const part of response.stream) {
        if (part instanceof vscode.LanguageModelTextPart) {
          content += part.value;
          partCount++;
        }
      }
      log(`[vscode-lm] stream done, parts=${partCount} contentLen=${content.length}`);

      // Count output tokens after streaming completes.
      try {
        outputTokens = await model.countTokens(content, token);
        log(`[vscode-lm] outputTokens=${outputTokens}`);
      } catch (e) {
        log(`[vscode-lm] countTokens (output) failed: ${e}`);
      }
    } catch (err) {
      logError("[vscode-lm] sendRequest/stream error:", err);
      if (err instanceof vscode.LanguageModelError) {
        throw new ProviderError(`VS Code LM error: ${err.message} (${err.code})`);
      }
      throw new ProviderError(`VS Code LM error: ${String(err)}`);
    }

    log(`[vscode-lm] returning output model='${model.id}' inputTokens=${inputTokens} outputTokens=${outputTokens}`);
    return {
      content,
      model: model.id,
      inputTokens,
      outputTokens,
    };
  }

  async close(): Promise<void> {
    // nothing to close
  }
}
