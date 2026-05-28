/**
 * `conductor run <workflow.yaml>` command.
 */
import type { Command } from "commander";
import path from "node:path";
import chalk from "chalk";
import ora from "ora";
import {
  loadConfig,
  validateConfig,
  WorkflowEngine,
  WorkflowEventEmitter,
  createProvider,
  ConductorError,
  type ProviderName,
  type WorkflowEvent,
} from "@conductor/core";
import { parseInputPairs, stdinInputHandler } from "../helpers.js";

const BOX_WIDTH = 73;

function printBox(title: string, content: string): void {
  const inner = BOX_WIDTH - 2;
  const titleStr = title ? ` ${title} ` : "";
  const remaining = inner - titleStr.length;
  const leftDash = Math.floor(remaining / 2);
  const rightDash = remaining - leftDash;
  console.log("╭" + "─".repeat(leftDash) + titleStr + "─".repeat(rightDash) + "╮");
  for (const line of content.split("\n")) {
    const padded = line.slice(0, inner - 2).padEnd(inner - 2);
    console.log(`│ ${padded} │`);
  }
  console.log("╰" + "─".repeat(inner) + "╯");
}

export function registerRunCommand(program: Command): void {
  program
    .command("run <workflow>")
    .description("Run a workflow")
    .option("-p, --provider <name>", "Provider override (copilot|claude)")
    .option(
      "-i, --input <key=value>",
      "Workflow input (repeatable)",
      (v: string, prev: string[]) => [...prev, v],
      [] as string[],
    )
    .option("-v, --verbose", "Verbose output")
    .option("--skip-gates", "Auto-select first option at human gates")
    .action(async (workflowFile: string, opts: {
      provider?: string;
      input: string[];
      verbose: boolean;
      skipGates: boolean;
    }) => {
      const absPath = path.resolve(workflowFile);
      const startMs = Date.now();
      const spinner = ora(`Loading ${path.basename(absPath)}`).start();

      try {
        const config = loadConfig(absPath);
        validateConfig(config, absPath);
        const loadMs = Date.now() - startMs;
        spinner.succeed(`Loaded workflow: ${chalk.bold(config.workflow.name)}`);
        console.log(chalk.dim(`⏱ Configuration loaded: ${(loadMs / 1000).toFixed(2)}s`));

        console.log(`Workflow: ${chalk.bold(config.workflow.name)}`);
        console.log(`Entry point: ${chalk.cyan(config.workflow.entry_point)}`);
        console.log(`Agents: ${config.agents.length}`);

        const inputs = parseInputPairs(opts.input);
        if (Object.keys(inputs).length > 0) {
          console.log();
          printBox("Workflow Inputs", JSON.stringify(inputs, null, 2));
        }

        const mcpServers = Object.keys(config.workflow.runtime?.mcp_servers ?? {});
        if (mcpServers.length > 0) {
          console.log(`\nMCP servers configured: [${mcpServers.join(", ")}]`);
        }

        const providerName = opts.provider ?? config.workflow.runtime?.provider ?? "copilot";
        console.log(`Provider: ${providerName}`);
        console.log("\nStarting workflow execution...");
        console.log(chalk.dim("(Ctrl+C to interrupt)\n"));

        const emitter = new WorkflowEventEmitter();
        let totalInputTokens = 0;
        let totalOutputTokens = 0;
        attachConsoleSubscriber(emitter, opts.verbose ?? false, (input, output) => {
          totalInputTokens += input;
          totalOutputTokens += output;
        });

        const provider = createProvider(providerName as ProviderName);
        const engine = new WorkflowEngine(config, {
          provider,
          emitter,
          onUserInputRequest: opts.skipGates ? undefined : stdinInputHandler,
        });

        const result = await engine.run(inputs);
        const totalMs = Date.now() - startMs;

        console.log();
        console.log(chalk.dim(`⏱ Total workflow execution: ${(totalMs / 1000).toFixed(2)}s`));
        console.log(chalk.bold.green("Workflow completed successfully"));

        if (totalInputTokens > 0 || totalOutputTokens > 0) {
          console.log();
          console.log(chalk.bold("Token Usage Summary"));
          console.log(`  Input:  ${totalInputTokens.toLocaleString()} tokens`);
          console.log(`  Output: ${totalOutputTokens.toLocaleString()} tokens`);
          console.log(`  Total:  ${(totalInputTokens + totalOutputTokens).toLocaleString()} tokens`);
        }

        if (Object.keys(result.output).length > 0) {
          console.log();
          console.log(JSON.stringify(result.output, null, 2));
        }

        await provider.close();
        process.exit(0);
      } catch (err) {
        spinner.fail("Workflow failed");
        if (err instanceof ConductorError) {
          console.error(chalk.red(err.message));
          if (err.suggestion) console.error(chalk.yellow(`  💡 ${err.suggestion}`));
        } else {
          console.error(err);
        }
        process.exit(1);
      }
    });
}

function attachConsoleSubscriber(
  emitter: WorkflowEventEmitter,
  verbose: boolean,
  onTokens: (input: number, output: number) => void,
): void {
  const agentStartTimes = new Map<string, number>();
  let iteration = 0;

  emitter.subscribe(async (event: WorkflowEvent) => {
    const data = event.data;
    switch (event.type) {
      case "agent_started": {
        const name = data["agentName"] as string;
        agentStartTimes.set(name, Date.now());
        iteration++;
        console.log(chalk.cyan(`┌─ Agent: ${name} [iter ${iteration}]`));
        break;
      }
      case "agent_turn_start": {
        const name = data["agentName"] as string;
        const turn = data["turn"] as number;
        console.log(chalk.dim(`│  [${name}] ⏳ Processing (turn ${turn})...`));
        break;
      }
      case "agent_reasoning": {
        const name = data["agentName"] as string;
        const content = data["content"] as string;
        const preview = content.length > 120 ? content.slice(0, 120) + "…" : content;
        console.log(chalk.dim(`│  [${name}] 💭 ${preview}`));
        break;
      }
      case "agent_tool_start": {
        const name = data["agentName"] as string;
        const tool = data["toolName"] as string;
        console.log(chalk.dim(`│  [${name}] ⚙ tool: ${tool}`));
        break;
      }
      case "agent_tool_complete": {
        if (verbose) {
          const name = data["agentName"] as string;
          const tool = data["toolName"] as string;
          console.log(chalk.dim(`│  [${name}] ✓ ${tool} done`));
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
        const outputKeys = Object.keys((data["output"] as Record<string, unknown>) ?? {});
        const parts: string[] = [elapsed];
        if (model) parts.push(model);
        if (inputTokens > 0 || outputTokens > 0) {
          parts.push(`${inputTokens.toLocaleString()} in/${outputTokens.toLocaleString()} out`);
          onTokens(inputTokens, outputTokens);
        }
        if (outputKeys.length > 0) parts.push(`→ [${outputKeys.join(", ")}]`);
        console.log(chalk.green(`└─ ✓ ${name}`) + chalk.dim(`  (${parts.filter(Boolean).join(", ")})`) );
        if (nextAgent) console.log(chalk.dim(`   → ${nextAgent}`));
        agentStartTimes.delete(name);
        break;
      }
      case "agent_failed": {
        const name = data["agentName"] as string;
        console.log(chalk.red(`└─ ✗ ${name} failed: ${data["error"] as string}`));
        agentStartTimes.delete(name);
        break;
      }
      case "agent_message": {
        if (verbose) {
          const name = data["agentName"] as string;
          console.log(chalk.dim(`\n--- ${name} output ---`));
          console.log(data["content"]);
        }
        break;
      }
      case "parallel_started":
        console.log(chalk.magenta(`\n⟳ Parallel group: ${data["groupName"] as string}`));
        break;
      case "foreach_started":
        console.log(chalk.magenta(`\n↺ For-each: ${data["groupName"] as string}`));
        break;
    }
  });
}
