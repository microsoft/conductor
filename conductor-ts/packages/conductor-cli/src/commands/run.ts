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

export function registerRunCommand(program: Command): void {
  program
    .command("run <workflow>")
    .description("Run a workflow")
    .option("-p, --provider <name>", "Provider override (copilot|claude)", "copilot")
    .option(
      "-i, --input <key=value>",
      "Workflow input (repeatable)",
      (v: string, prev: string[]) => [...prev, v],
      [] as string[],
    )
    .option("-v, --verbose", "Verbose output")
    .option("--skip-gates", "Auto-select first option at human gates")
    .action(async (workflowFile: string, opts: {
      provider: string;
      input: string[];
      verbose: boolean;
      skipGates: boolean;
    }) => {
      const absPath = path.resolve(workflowFile);
      const spinner = ora(`Loading ${path.basename(absPath)}`).start();

      try {
        const config = loadConfig(absPath);
        validateConfig(config, absPath);
        spinner.succeed(`Loaded workflow: ${chalk.bold(config.workflow.name)}`);

        const inputs = parseInputPairs(opts.input);
        const emitter = new WorkflowEventEmitter();

        // Console subscriber
        attachConsoleSubscriber(emitter, opts.verbose);

        const provider = createProvider((opts.provider ?? config.workflow.runtime?.provider ?? "copilot") as ProviderName);

        const engine = new WorkflowEngine(config, {
          provider,
          emitter,
          onUserInputRequest: opts.skipGates ? undefined : stdinInputHandler,
        });

        const result = await engine.run(inputs);

        console.log("\n" + chalk.bold.green("✓ Workflow complete"));
        if (Object.keys(result.output).length > 0) {
          console.log(chalk.bold("\nOutput:"));
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

function attachConsoleSubscriber(emitter: WorkflowEventEmitter, verbose: boolean): void {
  const agentSpinners = new Map<string, ReturnType<typeof ora>>();

  emitter.subscribe(async (event: WorkflowEvent) => {
    switch (event.type) {
      case "agent_started": {
        const name = event.data["agentName"] as string;
        const spinner = ora({ text: chalk.cyan(`[${name}] running…`), prefixText: "" }).start();
        agentSpinners.set(name, spinner);
        break;
      }
      case "agent_completed": {
        const name = event.data["agentName"] as string;
        agentSpinners.get(name)?.succeed(chalk.green(`[${name}] done`));
        agentSpinners.delete(name);
        break;
      }
      case "agent_failed": {
        const name = event.data["agentName"] as string;
        agentSpinners.get(name)?.fail(chalk.red(`[${name}] failed`));
        agentSpinners.delete(name);
        break;
      }
      case "agent_message": {
        if (verbose) {
          const name = event.data["agentName"] as string;
          console.log(chalk.dim(`\n--- ${name} output ---`));
          console.log(event.data["content"]);
        }
        break;
      }
      case "agent_reasoning": {
        if (verbose) {
          const name = event.data["agentName"] as string;
          console.log(chalk.dim(`\n--- ${name} reasoning ---`));
          console.log(chalk.dim(event.data["content"] as string));
        }
        break;
      }
      case "agent_tool_start": {
        if (verbose) {
          console.log(
            chalk.dim(`  ⚙ tool: ${event.data["toolName"] as string}`),
          );
        }
        break;
      }
    }
  });
}
