/**
 * VS Code extension — activate/deactivate.
 * Registers the @conductor chat participant and wires up VscodeLmProvider.
 */
import * as vscode from "vscode";
import { registerConductorParticipant } from "./chat/participant.js";

export function activate(context: vscode.ExtensionContext): void {
  registerConductorParticipant(context);

  context.subscriptions.push(
    vscode.commands.registerCommand("conductor.runWorkflow", async () => {
      const files = await vscode.workspace.findFiles("**/*.yaml", "**/node_modules/**", 10);
      if (!files.length) {
        vscode.window.showWarningMessage("No YAML workflow files found in workspace.");
        return;
      }
      const picked = await vscode.window.showQuickPick(
        files.map((f) => ({ label: vscode.workspace.asRelativePath(f), uri: f })),
        { placeHolder: "Select a workflow to run" },
      );
      if (!picked) return;

      // Open a new chat session pre-filled with a @conductor run command
      await vscode.commands.executeCommand(
        "workbench.action.chat.open",
        `@conductor run ${vscode.workspace.asRelativePath(picked.uri)}`,
      );
    }),
  );
}

export function deactivate(): void {
  // nothing
}
