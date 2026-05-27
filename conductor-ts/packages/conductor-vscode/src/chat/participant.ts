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

      stream.markdown(`\n✅ **Workflow complete**\n`);
      if (Object.keys(result.output).length > 0) {
        stream.markdown("**Output:**\n```json\n" + JSON.stringify(result.output, null, 2) + "\n```");
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
  emitter.subscribe(async (event: WorkflowEvent) => {
    switch (event.type) {
      case "agent_started":
        stream.progress(`Running agent: ${event.data["agentName"] as string}`);
        break;
      case "agent_completed":
        stream.markdown(`\n✓ Agent **${event.data["agentName"] as string}** completed\n`);
        break;
      case "agent_message":
        stream.markdown(
          `\n**${event.data["agentName"] as string}:**\n${event.data["content"] as string}\n`,
        );
        break;
      case "agent_reasoning":
        stream.markdown(
          `\n<details><summary>Reasoning (${event.data["agentName"] as string})</summary>\n\n${event.data["content"] as string}\n\n</details>\n`,
        );
        break;
      case "agent_tool_start":
        stream.progress(`Tool: ${event.data["toolName"] as string}`);
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
  const parts = prompt.split(/\s+/).filter(Boolean);
  const command = parts[0] ?? "";
  const inputs: Record<string, string> = {};
  let workflowFile: string | undefined;

  for (let i = 1; i < parts.length; i++) {
    const part = parts[i]!;
    if (part === "--input" || part === "-i") {
      const kv = parts[++i];
      if (kv) {
        const eq = kv.indexOf("=");
        if (eq !== -1) inputs[kv.slice(0, eq)] = kv.slice(eq + 1);
      }
    } else if (part.includes("=")) {
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
