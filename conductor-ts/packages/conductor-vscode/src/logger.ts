/**
 * Shared OutputChannel logger for the Conductor extension.
 * Write to "Conductor" in the Output panel (View → Output → Conductor).
 */
import * as vscode from "vscode";

let channel: vscode.OutputChannel | undefined;

export function initLogger(context: vscode.ExtensionContext): void {
  channel = vscode.window.createOutputChannel("Conductor");
  context.subscriptions.push(channel);
  log("Conductor extension activated");
}

export function log(...args: unknown[]): void {
  const line = `[${new Date().toISOString()}] ${args.map(String).join(" ")}`;
  channel?.appendLine(line);
  // Also mirror to console so exthost DevTools console works too
  console.log(line);
}

export function logError(...args: unknown[]): void {
  const line = `[${new Date().toISOString()}] ERROR ${args.map(String).join(" ")}`;
  channel?.appendLine(line);
  console.error(line);
}
