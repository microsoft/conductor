/**
 * VscodeLmProvider — uses vscode.lm.selectChatModels() + sendRequest().
 * This provider only works inside a VS Code extension context.
 */
import * as vscode from "vscode";
import type { AgentDef } from "@conductor/core";
import type { AgentOutput, AgentProvider, ExecuteOptions } from "@conductor/core";
import { ProviderError } from "@conductor/core";

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
    const models = await vscode.lm.selectChatModels(
      modelId ? { id: modelId } : {},
    );

    const model = models[0];
    if (!model) {
      throw new ProviderError(
        `No VS Code language model found${modelId ? ` for '${modelId}'` : ""}`,
      );
    }

    const token = this.options.token ?? opts.signal
      ? new vscode.CancellationTokenSource().token
      : new vscode.CancellationTokenSource().token;

    const messages: vscode.LanguageModelChatMessage[] = [];
    if (agent.system_prompt) {
      messages.push(vscode.LanguageModelChatMessage.User(agent.system_prompt));
    }
    messages.push(vscode.LanguageModelChatMessage.User(prompt));

    let content = "";
    let inputTokens = 0;
    let outputTokens = 0;

    try {
      const response = await model.sendRequest(messages, {}, token);
      for await (const part of response.stream) {
        if (part instanceof vscode.LanguageModelTextPart) {
          content += part.value;
        }
      }

      // Token usage (not always available in all VS Code versions)
      try {
        const usage = (response as unknown as { usage?: { inputTokens?: number; outputTokens?: number } }).usage;
        inputTokens = usage?.inputTokens ?? 0;
        outputTokens = usage?.outputTokens ?? 0;
      } catch {
        // ignore
      }
    } catch (err) {
      if (err instanceof vscode.LanguageModelError) {
        throw new ProviderError(`VS Code LM error: ${err.message} (${err.code})`);
      }
      throw new ProviderError(`VS Code LM error: ${String(err)}`);
    }

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
