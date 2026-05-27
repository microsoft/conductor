/**
 * Shared helpers for CLI commands.
 */
import readline from "node:readline/promises";
import chalk from "chalk";
import type { UserInputRequest, UserInputResponse } from "@conductor/core";

/** Interactive input handler — reads from stdin (for CLI mode). */
export async function stdinInputHandler(
  req: UserInputRequest,
): Promise<UserInputResponse> {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });

  let prompt = `\n${chalk.cyan("[skill asks]")} ${req.question}`;
  if (req.choices?.length) {
    prompt += "\n" + req.choices.map((c, i) => `  ${i + 1}. ${c}`).join("\n");
    if (req.allowFreeform !== false) {
      prompt += "\n  (or type a free-form answer)";
    }
  }
  prompt += `\n${chalk.green("> ")}`;

  const answer = await rl.question(prompt);
  rl.close();

  const trimmed = answer.trim();

  // Expand numeric choice to label
  if (req.choices?.length && /^\d+$/.test(trimmed)) {
    const idx = parseInt(trimmed, 10) - 1;
    if (idx >= 0 && idx < req.choices.length) {
      return { answer: req.choices[idx]!, wasFreeform: false };
    }
  }
  const wasFreeform = !req.choices?.includes(trimmed);
  return { answer: trimmed, wasFreeform };
}

/** Parse --input key=value pairs into an object. */
export function parseInputPairs(pairs: string[]): Record<string, string> {
  const result: Record<string, string> = {};
  for (const pair of pairs) {
    const eq = pair.indexOf("=");
    if (eq === -1) {
      console.warn(`Warning: ignoring input '${pair}' — expected key=value format`);
      continue;
    }
    const key = pair.slice(0, eq);
    const value = pair.slice(eq + 1);
    result[key] = value;
  }
  return result;
}
