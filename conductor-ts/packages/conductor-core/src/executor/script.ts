/**
 * Script step executor.
 * Mirrors src/conductor/executor/script.py
 */
import { spawn } from "cross-spawn";
import type { AgentDef } from "../config/schema.js";
import { TemplateRenderer } from "./template.js";
import { ExecutionError } from "../exceptions.js";

export interface ScriptOutput {
  stdout: string;
  stderr: string;
  exit_code: number;
  success: boolean;
  output: Record<string, unknown>;
}

const renderer = new TemplateRenderer();

export async function executeScript(
  agent: AgentDef,
  context: Record<string, unknown>,
): Promise<ScriptOutput> {
  if (!agent.command) {
    throw new ExecutionError(`Script agent '${agent.name}' has no command defined`);
  }

  const command = renderer.render(agent.command, context);
  const args = agent.args.map((a) => renderer.render(a, context));
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    ...Object.fromEntries(
      Object.entries(agent.env).map(([k, v]) => [k, renderer.render(v, context)]),
    ),
  };
  const cwd = agent.working_dir
    ? renderer.render(agent.working_dir, context)
    : process.cwd();

  return new Promise<ScriptOutput>((resolve, reject) => {
    const parts = command.split(/\s+/);
    const cmd = parts[0]!;
    const cmdArgs = [...parts.slice(1), ...args];

    const child = spawn(cmd, cmdArgs, {
      env,
      cwd,
      shell: false,
    });

    let stdout = "";
    let stderr = "";

    child.stdout?.on("data", (d: Buffer) => { stdout += d.toString(); });
    child.stderr?.on("data", (d: Buffer) => { stderr += d.toString(); });

    const timeoutMs = agent.timeout ? agent.timeout * 1000 : undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;

    if (timeoutMs) {
      timer = setTimeout(() => {
        child.kill("SIGTERM");
        reject(
          new ExecutionError(
            `Script agent '${agent.name}' timed out after ${agent.timeout}s`,
          ),
        );
      }, timeoutMs);
    }

    child.on("error", (err) => {
      if (timer) clearTimeout(timer);
      reject(new ExecutionError(`Script '${agent.name}' failed to start: ${err.message}`));
    });

    child.on("close", (code) => {
      if (timer) clearTimeout(timer);
      const exitCode = code ?? 1;

      // Try to parse stdout as JSON for structured output
      let parsedOutput: Record<string, unknown> = {};
      const trimmed = stdout.trim();
      if (trimmed.startsWith("{")) {
        try {
          parsedOutput = JSON.parse(trimmed) as Record<string, unknown>;
        } catch {
          parsedOutput = { raw: stdout };
        }
      } else {
        parsedOutput = { raw: stdout };
      }

      resolve({
        stdout,
        stderr,
        exit_code: exitCode,
        success: exitCode === 0,
        output: parsedOutput,
      });
    });
  });
}
