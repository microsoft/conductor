/**
 * Language Model Tool — conductor_runWorkflow
 *
 * Exposes Conductor workflow execution as a vscode.LanguageModelTool so it
 * can be called by Copilot's agentic loop (e.g. "Open in Agents" window)
 * without going through the @conductor chat participant.
 */
import * as vscode from "vscode";
import path from "node:path";
import {
  loadConfig,
  validateConfig,
  WorkflowEngine,
  WorkflowEventEmitter,
  ConductorError,
} from "@conductor/core";
import { VscodeLmProvider } from "../providers/vscode-lm.js";
import { log } from "../logger.js";

export interface RunWorkflowInput {
  workflowPath: string;
  inputs?: Record<string, string>;
}

export class RunWorkflowTool implements vscode.LanguageModelTool<RunWorkflowInput> {
  async prepareInvocation(
    options: vscode.LanguageModelToolInvocationPrepareOptions<RunWorkflowInput>,
    _token: vscode.CancellationToken,
  ): Promise<vscode.PreparedToolInvocation> {
    const { workflowPath, inputs } = options.input;
    const inputsStr =
      inputs && Object.keys(inputs).length
        ? `\n\nInputs: ${Object.entries(inputs)
            .map(([k, v]) => `${k}=${v}`)
            .join(", ")}`
        : "";
    return {
      invocationMessage: `Running workflow: ${workflowPath}${inputsStr}`,
    };
  }

  async invoke(
    options: vscode.LanguageModelToolInvocationOptions<RunWorkflowInput>,
    token: vscode.CancellationToken,
  ): Promise<vscode.LanguageModelToolResult> {
    const { workflowPath, inputs = {} } = options.input;
    const absPath = resolveWorkflowPath(workflowPath);
    log(`[run-workflow-tool] invoke path='${absPath}'`);

    try {
      const config = loadConfig(absPath);
      validateConfig(config, absPath);

      const workflowDir = path.dirname(absPath);
      const resolvedSkillDirs = (config.workflow.runtime?.skill_directories ?? []).map(
        (d) => path.resolve(workflowDir, d),
      );

      const emitter = new WorkflowEventEmitter();
      const provider = new VscodeLmProvider({ token });

      // Collect agent messages as a fallback when there is no structured output.
      const agentOutputs: string[] = [];
      emitter.subscribe((event) => {
        if (event.type === "agent_message") {
          const name = event.data["agentName"] as string;
          const content = event.data["content"] as string;
          if (content) agentOutputs.push(`**${name}:** ${content}`);
        }
      });

      const engine = new WorkflowEngine(config, {
        provider,
        emitter,
        workflowFile: absPath,
        skillDirectories: resolvedSkillDirs,
      });

      const result = await engine.run(inputs);
      await provider.close();

      const outputText =
        Object.keys(result.output).length > 0
          ? JSON.stringify(result.output, null, 2)
          : agentOutputs.join("\n\n") || "(no output)";

      return new vscode.LanguageModelToolResult([
        new vscode.LanguageModelTextPart(outputText),
      ]);
    } catch (err) {
      const msg = err instanceof ConductorError ? err.message : String(err);
      return new vscode.LanguageModelToolResult([
        new vscode.LanguageModelTextPart(`Workflow failed: ${msg}`),
      ]);
    }
  }
}

function resolveWorkflowPath(file: string): string {
  if (path.isAbsolute(file)) return file;
  const folders = vscode.workspace.workspaceFolders;
  const root = folders?.[0]?.uri.fsPath ?? process.cwd();
  return path.resolve(root, file);
}
