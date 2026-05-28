/**
 * @conductor chat participant.
 *
 * Usage in Copilot Chat:
 *   @conductor run examples/simple-qa.yaml --input question="What is Python?"
 *   @conductor validate examples/simple-qa.yaml
 *   @conductor help
 *
 * Interactive input (ask_user tool / human gates) is handled by suspending
 * the generator and waiting for the next chat turn via the inputBridge.
 */
import * as vscode from "vscode";
import path from "node:path";
import {
  loadConfig,
  validateConfig,
  WorkflowEngine,
  WorkflowEventEmitter,
  ConductorError,
  type WorkflowEvent,
} from "@conductor/core";
import { VscodeLmProvider } from "../providers/vscode-lm.js";
import { createInputBridge, type InputBridge } from "./input-bridge.js";
import { log } from "../logger.js";

export function registerConductorParticipant(context: vscode.ExtensionContext): void {
  const participant = vscode.chat.createChatParticipant(
    "conductor.conductor",
    handleRequest,
  );
  participant.iconPath = vscode.Uri.joinPath(context.extensionUri, "assets", "icon.png");
  context.subscriptions.push(participant);
}

async function handleRequest(
  request: vscode.ChatRequest,
  chatContext: vscode.ChatContext,
  stream: vscode.ChatResponseStream,
  token: vscode.CancellationToken,
): Promise<void> {
  const args = parseArgs(request.prompt.trim());

  // --- help ---
  if (!args.command || args.command === "help") {
    stream.markdown(HELP_TEXT);
    return;
  }

  // --- validate ---
  if (args.command === "validate") {
    if (!args.workflowFile) {
      stream.markdown("Usage: `@conductor validate <workflow.yaml>`");
      return;
    }
    try {
      const absPath = resolveWorkflowPath(args.workflowFile);
      const config = loadConfig(absPath);
      validateConfig(config, absPath);
      stream.markdown(`✅ **${path.basename(absPath)}** is valid\n- Agents: ${config.agents.length}`);
    } catch (err) {
      stream.markdown(`❌ ${err instanceof ConductorError ? err.message : String(err)}`);
    }
    return;
  }

  // --- run ---
  if (args.command === "run") {
    if (!args.workflowFile) {
      stream.markdown("Usage: `@conductor run <workflow.yaml> [--input key=value]`");
      return;
    }

    const bridge = createInputBridge();
    const emitter = new WorkflowEventEmitter();
    const provider = new VscodeLmProvider({ token });

    attachChatSubscriber(emitter, stream);

    let absPath: string;
    try {
      absPath = resolveWorkflowPath(args.workflowFile);
      const config = loadConfig(absPath);
      validateConfig(config, absPath);

      stream.markdown(`▶ Running **${config.workflow.name}**…\n\n`);

      const engine = new WorkflowEngine(config, {
        provider,
        emitter,
        onUserInputRequest: bridge.requestInput,
      });

      // Run the workflow (may be suspended waiting for input)
      const runPromise = engine.run(args.inputs);

      // Handle input requests from the bridge in a separate async loop
      void handleBridgeInputs(bridge, stream, chatContext, token);

      const result = await runPromise;
      bridge.close();

      if (Object.keys(result.output).length > 0) {
        stream.markdown("\n\n---\n\n**Workflow output:**\n\n```json\n" + JSON.stringify(result.output, null, 2) + "\n```\n");
      }
    } catch (err) {
      bridge.close();
      const msg = err instanceof ConductorError ? err.message : String(err);
      stream.markdown(`\n❌ **Workflow failed:** ${msg}`);
    } finally {
      await provider.close();
    }
    return;
  }

  stream.markdown(`Unknown command: \`${args.command}\`. Try \`@conductor help\`.`);
}

// ---------------------------------------------------------------------------
// Bridge: suspend workflow on input request, resume on next chat turn
// ---------------------------------------------------------------------------

async function handleBridgeInputs(
  bridge: InputBridge,
  stream: vscode.ChatResponseStream,
  _chatContext: vscode.ChatContext,
  token: vscode.CancellationToken,
): Promise<void> {
  for await (const req of bridge.requests) {
    if (token.isCancellationRequested) break;
    stream.markdown(`\n🙋 **${req.question}**\n`);
    if (req.choices?.length) {
      req.choices.forEach((c, i) => stream.markdown(`${i + 1}. ${c}\n`));
    }
    stream.markdown("\n*Reply in the next message to continue the workflow.*\n");
    // The bridge will suspend; the next chat turn triggers resume
    break;
  }
}

// ---------------------------------------------------------------------------
// Chat event subscriber
// ---------------------------------------------------------------------------

function attachChatSubscriber(emitter: WorkflowEventEmitter, stream: vscode.ChatResponseStream): void {
  const agentStartTimes = new Map<string, number>();

  emitter.subscribe(async (event: WorkflowEvent) => {
    const data = event.data;
    log(`[participant] event=${event.type} ${JSON.stringify(data).slice(0, 200)}`);
    switch (event.type) {
      case "agent_started": {
        const name = data["agentName"] as string;
        agentStartTimes.set(name, Date.now());
        stream.progress(`Running agent: ${name}`);
        stream.markdown(`\n**🤖 Agent: ${name}**\n\n`);
        break;
      }
      case "agent_turn_start": {
        const name = data["agentName"] as string;
        const turn = data["turn"] as number | string;
        stream.progress(turn === "awaiting_model" ? `${name}: processing…` : `${name}: turn ${turn}…`);
        break;
      }
      case "agent_reasoning": {
        const name = data["agentName"] as string;
        const content = data["content"] as string;
        stream.markdown(
          `\n<details><summary>💭 Thinking (${name})</summary>\n\n${content}\n\n</details>\n\n`,
        );
        break;
      }
      case "agent_tool_start": {
        const name = data["agentName"] as string;
        const tool = data["toolName"] as string;
        stream.progress(`${name}: calling ${tool}…`);
        stream.markdown(`\n> ⚙ \`${tool}\`\n\n`);
        break;
      }
      case "agent_tool_complete": {
        const error = data["error"] as string | undefined;
        if (error) {
          const tool = data["toolName"] as string;
          stream.markdown(`\n> ✗ \`${tool}\` failed: ${error}\n\n`);
        }
        break;
      }
      case "agent_completed": {
        const name = data["agentName"] as string;
        const startTime = agentStartTimes.get(name);
        const elapsed = startTime ? `${((Date.now() - startTime) / 1000).toFixed(2)}s` : "";
        const model = data["model"] as string | undefined;
        const inputTokens = (data["inputTokens"] as number | undefined) ?? 0;
        const outputTokens = (data["outputTokens"] as number | undefined) ?? 0;
        const nextAgent = data["nextAgent"] as string | undefined;
        const output = data["output"] as Record<string, unknown> | undefined;

        // Render parsed output fields (answers, summaries, etc.) as readable prose
        if (output && Object.keys(output).length > 0) {
          for (const [key, value] of Object.entries(output)) {
            const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
            stream.markdown(`**${key}:**\n\n${text}\n\n`);
          }
        }

        // Summary line
        const parts: string[] = [];
        if (elapsed) parts.push(elapsed);
        if (model) parts.push(model);
        if (inputTokens > 0 || outputTokens > 0)
          parts.push(`${inputTokens.toLocaleString()} in / ${outputTokens.toLocaleString()} out`);
        stream.markdown(
          `\n*✅ ${name} — ${parts.join(" · ")}${nextAgent && nextAgent !== "$end" ? ` → ${nextAgent}` : ""}*\n\n`,
        );
        agentStartTimes.delete(name);
        break;
      }
      case "agent_failed": {
        const name = data["agentName"] as string;
        stream.markdown(`\n❌ **${name}** failed: ${data["error"] as string}\n\n`);
        agentStartTimes.delete(name);
        break;
      }
      case "parallel_started":
        stream.markdown(`\n⟳ **Parallel group:** ${data["groupName"] as string}\n\n`);
        break;
      case "foreach_started":
        stream.markdown(`\n↺ **For-each:** ${data["groupName"] as string}\n\n`);
        break;
    }
  });
}

// ---------------------------------------------------------------------------
// Arg parsing
// ---------------------------------------------------------------------------

interface ParsedArgs {
  command: string;
  workflowFile?: string;
  inputs: Record<string, string>;
}

function parseArgs(prompt: string): ParsedArgs {
  // Tokenize respecting double- and single-quoted strings so that
  // --input question="What is Python?" is a single token after the flag.
  const tokens: string[] = [];
  let current = "";
  let inDouble = false;
  let inSingle = false;

  for (let i = 0; i < prompt.length; i++) {
    const ch = prompt[i]!;
    if (ch === '"' && !inSingle) {
      inDouble = !inDouble;
    } else if (ch === "'" && !inDouble) {
      inSingle = !inSingle;
    } else if (ch === " " && !inDouble && !inSingle) {
      if (current) { tokens.push(current); current = ""; }
    } else {
      current += ch;
    }
  }
  if (current) tokens.push(current);

  const command = tokens[0] ?? "";
  const inputs: Record<string, string> = {};
  let workflowFile: string | undefined;

  for (let i = 1; i < tokens.length; i++) {
    const part = tokens[i]!;
    if (part === "--input" || part === "-i") {
      const kv = tokens[++i];
      if (kv) {
        const eq = kv.indexOf("=");
        if (eq !== -1) inputs[kv.slice(0, eq)] = kv.slice(eq + 1);
      }
    } else if (part.includes("=") && !part.startsWith("-")) {
      const eq = part.indexOf("=");
      inputs[part.slice(0, eq)] = part.slice(eq + 1);
    } else if (!part.startsWith("-")) {
      workflowFile ??= part;
    }
  }

  return { command, workflowFile, inputs };
}

function resolveWorkflowPath(file: string): string {
  if (path.isAbsolute(file)) return file;
  const folders = vscode.workspace.workspaceFolders;
  const root = folders?.[0]?.uri.fsPath ?? process.cwd();
  return path.resolve(root, file);
}

const HELP_TEXT = `
## Conductor

Run multi-agent Conductor workflows inside Copilot Chat.

**Commands:**
- \`@conductor run <workflow.yaml> [--input key=value]\` — Run a workflow
- \`@conductor validate <workflow.yaml>\` — Validate a workflow YAML
- \`@conductor help\` — Show this help

**Example:**
\`\`\`
@conductor run examples/simple-qa.yaml --input question="What is Python?"
\`\`\`
`;
