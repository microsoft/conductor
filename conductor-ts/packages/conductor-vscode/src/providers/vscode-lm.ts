/**
 * VscodeLmProvider — uses vscode.lm.selectChatModels() + sendRequest().
 * This provider only works inside a VS Code extension context.
 *
 * When skillDirectories are configured in the workflow, SKILL.md files are
 * discovered and exposed as a `skill` tool so the model can invoke them —
 * mirroring the Copilot CLI SDK's skill tool support.
 */
import * as vscode from "vscode";
import fs from "node:fs";
import path from "node:path";
import type { AgentDef } from "@conductor/core";
import type { AgentOutput, AgentProvider, ExecuteOptions, UserInputRequest, UserInputResponse } from "@conductor/core";
import { ProviderError } from "@conductor/core";
import { log, logError } from "../logger.js";

export interface VscodeLmProviderOptions {
  modelId?: string;
  token?: vscode.CancellationToken;
  /** Called when the agent calls ask_user — defaults to vscode.window.showInputBox/showQuickPick. */
  onUserInputRequest?: (req: UserInputRequest) => Promise<UserInputResponse>;
}

// ---------------------------------------------------------------------------
// Skill discovery
// ---------------------------------------------------------------------------

/**
 * Discover all SKILL.md files under the given directories.
 * Returns a Map of skill-name → file content.
 * Each skill is indexed by both its frontmatter `name:` field and its
 * directory name so callers can look it up either way.
 */
function discoverSkills(skillDirs: string[]): Map<string, string> {
  const skills = new Map<string, string>();
  for (const dir of skillDirs) {
    try {
      const entries = fs.readdirSync(dir, { withFileTypes: true });
      for (const entry of entries) {
        if (!entry.isDirectory()) continue;
        const skillFile = path.join(dir, entry.name, "SKILL.md");
        if (!fs.existsSync(skillFile)) continue;
        const content = fs.readFileSync(skillFile, "utf-8");
        // Index by directory name (e.g. "brainstorming")
        skills.set(entry.name, content);
        // Also index by frontmatter name: field if present
        const nameMatch = content.match(/^name:\s*(.+)$/m);
        const frontmatterName = nameMatch?.[1]?.trim();
        if (frontmatterName && frontmatterName !== entry.name) {
          skills.set(frontmatterName, content);
        }
      }
    } catch {
      // Ignore unreadable directories
    }
  }
  return skills;
}

// ---------------------------------------------------------------------------
// Provider
// ---------------------------------------------------------------------------

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

    // Discover skills from configured skill directories
    const skillDirs = opts.skillDirectories ?? [];
    const skills = skillDirs.length > 0 ? discoverSkills(skillDirs) : new Map<string, string>();
    log(`[vscode-lm] skillDirs=${skillDirs.length} skills discovered=${skills.size}`);

    // Prefer the bridge (opts.onUserInputRequest) so ask_user calls suspend
    // in-chat across turns.  Fall back to the constructor-level handler, then
    // to the VS Code UI popup as a last resort.
    const userInputHandler =
      opts.onUserInputRequest ??
      this.options.onUserInputRequest ??
      defaultVscodeInputHandler;

    // Build tools: skill (when skills available) + ask_user
    const tools: vscode.LanguageModelChatTool[] = [];
    if (skills.size > 0) {
      tools.push({
        name: "skill",
        description:
          "Load and activate a Superpowers skill by name. Returns the full SKILL.md content so you can follow it.",
        inputSchema: {
          type: "object",
          properties: {
            name: {
              type: "string",
              description:
                `The skill name to activate. Available skills: ${[...new Set(skills.keys())].join(", ")}`,
            },
          },
          required: ["name"],
        },
      });
    }
    // ask_user — always registered; defaultVscodeInputHandler is the fallback
    tools.push({
      name: "ask_user",
      description:
        "Ask the user a clarifying question and wait for their answer. Use this whenever the skill or task requires human input before proceeding.",
      inputSchema: {
        type: "object",
        properties: {
          question: { type: "string", description: "The question to ask the user." },
          choices: {
            type: "array",
            items: { type: "string" },
            description: "Optional list of choices to present to the user.",
          },
        },
        required: ["question"],
      },
    });

    // File system tools
    const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? process.cwd();
    tools.push(
      {
        name: "read_file",
        description: "Read the text content of a file. Path may be absolute or workspace-relative.",
        inputSchema: {
          type: "object",
          properties: { path: { type: "string", description: "File path to read." } },
          required: ["path"],
        },
      },
      {
        name: "write_file",
        description: "Write text content to a file, creating parent directories as needed. Path may be absolute or workspace-relative.",
        inputSchema: {
          type: "object",
          properties: {
            path: { type: "string", description: "File path to write." },
            content: { type: "string", description: "Text content to write." },
          },
          required: ["path", "content"],
        },
      },
      {
        name: "list_directory",
        description: "List files and subdirectories at a path. Path may be absolute or workspace-relative.",
        inputSchema: {
          type: "object",
          properties: { path: { type: "string", description: "Directory path to list." } },
          required: ["path"],
        },
      },
    );

    // Build initial messages
    const messages: vscode.LanguageModelChatMessage[] = [];
    if (agent.system_prompt) {
      messages.push(vscode.LanguageModelChatMessage.User(agent.system_prompt));
    }
    messages.push(vscode.LanguageModelChatMessage.User(fullPrompt));

    // Signal that the model request is about to start
    log(`[vscode-lm] emitting agent_turn_start awaiting_model, hasEmitter=${!!opts.emitter}`);
    opts.emitter?.emit("agent_turn_start", {
      agentName: agent.name,
      turn: "awaiting_model",
    }).catch((e) => logError("[vscode-lm] agent_turn_start emit failed", e));

    // Token counters — updated after the loop using the full message history.
    let inputTokens = 0;
    let outputTokens = 0;

    // ---------------------------------------------------------------------------
    // Agentic loop — keeps running while the model issues skill tool calls
    // ---------------------------------------------------------------------------
    let content = "";
    let turn = 0;
    const maxIterations = opts.maxIterations ?? 20;

    try {
      while (turn < maxIterations) {
        turn++;
        if (turn > 1) {
          // Emit turn-number event for subsequent iterations (first already got awaiting_model)
          opts.emitter?.emit("agent_turn_start", {
            agentName: agent.name,
            turn,
          }).catch(() => undefined);
        }

        log(`[vscode-lm] sendRequest turn=${turn} messages=${messages.length} tools=${tools.length}`);
        const requestOpts: vscode.LanguageModelChatRequestOptions = tools.length > 0
          ? { tools }
          : {};
        const response = await model.sendRequest(messages, requestOpts, token);

        const toolCalls: vscode.LanguageModelToolCallPart[] = [];
        let turnText = "";
        let partCount = 0;

        for await (const part of response.stream) {
          if (part instanceof vscode.LanguageModelTextPart) {
            turnText += part.value;
            partCount++;
          } else if (part instanceof vscode.LanguageModelToolCallPart) {
            toolCalls.push(part);
          }
        }
        log(`[vscode-lm] turn=${turn} parts=${partCount} toolCalls=${toolCalls.length} textLen=${turnText.length}`);

        // No tool calls — we have the final answer
        if (toolCalls.length === 0) {
          content = turnText;
          break;
        }

        // Add assistant message containing the text + tool calls
        const assistantParts: (vscode.LanguageModelTextPart | vscode.LanguageModelToolCallPart)[] = [
          ...(turnText ? [new vscode.LanguageModelTextPart(turnText)] : []),
          ...toolCalls,
        ];
        messages.push(vscode.LanguageModelChatMessage.Assistant(assistantParts));

        // Process each tool call and collect results
        const toolResults: vscode.LanguageModelToolResultPart[] = [];
        for (const call of toolCalls) {
          const toolName = call.name;

          let resultContent: string;
          if (call.name === "skill") {
            const skillName = (call.input as { name?: string }).name ?? "";
            opts.emitter?.emit("agent_tool_start", {
              agentName: agent.name,
              toolName: `skill(${skillName})`,
            }).catch(() => undefined);
            const skillContent = skills.get(skillName);
            if (skillContent) {
              log(`[vscode-lm] skill tool: loaded '${skillName}' (${skillContent.length} chars)`);
              resultContent = skillContent;
            } else {
              const available = [...new Set(skills.keys())].join(", ");
              resultContent = `Skill '${skillName}' not found. Available skills: ${available}`;
              log(`[vscode-lm] skill tool: '${skillName}' not found`);
            }
            opts.emitter?.emit("agent_tool_complete", { agentName: agent.name, toolName: `skill(${skillName})` }).catch(() => undefined);
          } else if (call.name === "ask_user") {
            const input = call.input as { question?: string; choices?: string[] };
            const question = input.question ?? "";
            const choices = input.choices;
            log(`[vscode-lm] ask_user tool: question='${question}' choices=${JSON.stringify(choices)}`);
            // Emit tool_start with args so participant can render the question in chat
            opts.emitter?.emit("agent_tool_start", {
              agentName: agent.name,
              toolName: "ask_user",
              args: { question, choices },
            }).catch(() => undefined);
            try {
              const response = await userInputHandler({ question, choices });
              resultContent = response.answer;
              log(`[vscode-lm] ask_user tool: answer='${response.answer}'`);
              opts.emitter?.emit("agent_tool_complete", {
                agentName: agent.name,
                toolName: "ask_user",
                result: response.answer,
              }).catch(() => undefined);
            } catch (err) {
              resultContent = `Input cancelled: ${String(err)}`;
              log(`[vscode-lm] ask_user tool: cancelled`);
              opts.emitter?.emit("agent_tool_complete", { agentName: agent.name, toolName: "ask_user", error: String(err) }).catch(() => undefined);
            }
          } else if (call.name === "read_file") {
            const filePath = resolveWorkspacePath((call.input as { path?: string }).path ?? "", workspaceRoot);
            opts.emitter?.emit("agent_tool_start", { agentName: agent.name, toolName: `read_file` }).catch(() => undefined);
            log(`[vscode-lm] read_file: ${filePath}`);
            try {
              resultContent = fs.readFileSync(filePath, "utf-8");
              opts.emitter?.emit("agent_tool_complete", { agentName: agent.name, toolName: "read_file" }).catch(() => undefined);
            } catch (err) {
              resultContent = `Error reading file: ${String(err)}`;
              opts.emitter?.emit("agent_tool_complete", { agentName: agent.name, toolName: "read_file", error: String(err) }).catch(() => undefined);
            }
          } else if (call.name === "write_file") {
            const input = call.input as { path?: string; content?: string };
            const filePath = resolveWorkspacePath(input.path ?? "", workspaceRoot);
            const content = input.content ?? "";
            opts.emitter?.emit("agent_tool_start", { agentName: agent.name, toolName: `write_file` }).catch(() => undefined);
            log(`[vscode-lm] write_file: ${filePath} (${content.length} chars)`);
            try {
              fs.mkdirSync(path.dirname(filePath), { recursive: true });
              fs.writeFileSync(filePath, content, "utf-8");
              resultContent = `File written: ${filePath}`;
              opts.emitter?.emit("agent_tool_complete", { agentName: agent.name, toolName: "write_file" }).catch(() => undefined);
            } catch (err) {
              resultContent = `Error writing file: ${String(err)}`;
              opts.emitter?.emit("agent_tool_complete", { agentName: agent.name, toolName: "write_file", error: String(err) }).catch(() => undefined);
            }
          } else if (call.name === "list_directory") {
            const dirPath = resolveWorkspacePath((call.input as { path?: string }).path ?? ".", workspaceRoot);
            opts.emitter?.emit("agent_tool_start", { agentName: agent.name, toolName: `list_directory` }).catch(() => undefined);
            log(`[vscode-lm] list_directory: ${dirPath}`);
            try {
              const entries = fs.readdirSync(dirPath, { withFileTypes: true });
              resultContent = entries
                .map((e) => `${e.isDirectory() ? "[dir]" : "[file]"} ${e.name}`)
                .join("\n");
              opts.emitter?.emit("agent_tool_complete", { agentName: agent.name, toolName: "list_directory" }).catch(() => undefined);
            } catch (err) {
              resultContent = `Error listing directory: ${String(err)}`;
              opts.emitter?.emit("agent_tool_complete", { agentName: agent.name, toolName: "list_directory", error: String(err) }).catch(() => undefined);
            }
          } else {
            opts.emitter?.emit("agent_tool_start", { agentName: agent.name, toolName }).catch(() => undefined);
            resultContent = `Unknown tool: ${call.name}`;
            opts.emitter?.emit("agent_tool_complete", { agentName: agent.name, toolName }).catch(() => undefined);
          }

          toolResults.push(
            new vscode.LanguageModelToolResultPart(call.callId, [
              new vscode.LanguageModelTextPart(resultContent),
            ]),
          );
        }

        // Add tool results as a user message
        messages.push(vscode.LanguageModelChatMessage.User(toolResults));
      }

      if (turn >= maxIterations && !content) {
        log(`[vscode-lm] max iterations (${maxIterations}) reached without final text`);
      }

      // Count tokens after the loop: full message history → input, final content → output.
      // This is accurate because every turn's messages accumulate in the array.
      try {
        const counts = await Promise.all(messages.map((m) => model.countTokens(m, token)));
        inputTokens = counts.reduce((a, b) => a + b, 0);
        log(`[vscode-lm] inputTokens (post-loop)=${inputTokens}`);
      } catch (e) {
        log(`[vscode-lm] countTokens (input) failed: ${e}`);
      }
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

    log(`[vscode-lm] returning model='${model.id}' inputTokens=${inputTokens} outputTokens=${outputTokens} turns=${turn}`);
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

// ---------------------------------------------------------------------------
// Default input handler — uses VS Code UI (showQuickPick / showInputBox)
// ---------------------------------------------------------------------------

/** Resolve a path that may be absolute or workspace-relative. */
function resolveWorkspacePath(p: string, workspaceRoot: string): string {
  return path.isAbsolute(p) ? p : path.resolve(workspaceRoot, p);
}

async function defaultVscodeInputHandler(req: UserInputRequest): Promise<UserInputResponse> {
  if (req.choices?.length) {
    const picked = await vscode.window.showQuickPick(req.choices, {
      title: req.question,
      placeHolder: "Select an option",
    });
    if (picked === undefined) {
      return { answer: req.choices[0] ?? "", wasFreeform: false };
    }
    return { answer: picked, wasFreeform: false };
  }

  const answer = await vscode.window.showInputBox({
    title: req.question,
    prompt: req.question,
    ignoreFocusOut: true,
  });
  return { answer: answer ?? "", wasFreeform: true };
}
