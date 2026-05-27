/**
 * `conductor resume <workflow.yaml>` command.
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
  CheckpointManager,
  createProvider,
  ConductorError,
  type ProviderName,
} from "@conductor/core";
import { stdinInputHandler } from "../helpers.js";

export function registerResumeCommand(program: Command): void {
  program
    .command("resume <workflow>")
    .description("Resume a workflow from its last checkpoint")
    .option("-p, --provider <name>", "Provider override (copilot|claude)")
    .option("-v, --verbose", "Verbose output")
    .option("--skip-gates", "Auto-select first option at human gates")
    .action(async (workflowFile: string, opts: {
      provider?: string;
      verbose: boolean;
      skipGates: boolean;
    }) => {
      const absPath = path.resolve(workflowFile);
      const spinner = ora(`Loading checkpoint for ${path.basename(absPath)}`).start();

      try {
        const config = loadConfig(absPath);
        validateConfig(config, absPath);

        const checkpoints = new CheckpointManager(absPath);
        if (!checkpoints.exists(absPath)) {
          spinner.fail(`No checkpoint found for '${workflowFile}'`);
          process.exit(1);
        }

        const cp = checkpoints.load(absPath);
        const context = checkpoints.restoreContext(cp);
        spinner.succeed(
          `Resuming from '${chalk.bold(cp.nextAgent)}' (checkpoint: ${new Date(cp.timestamp).toLocaleString()})`,
        );

        const providerName = (opts.provider ?? config.workflow.runtime?.provider ?? "copilot") as ProviderName;
        const provider = createProvider(providerName);
        const emitter = new WorkflowEventEmitter();

        const engine = new WorkflowEngine(config, {
          provider,
          emitter,
          resumeFrom: cp.nextAgent,
          resumeContext: context,
          onUserInputRequest: opts.skipGates ? undefined : stdinInputHandler,
        });

        const result = await engine.run();
        checkpoints.delete(absPath);

        console.log("\n" + chalk.bold.green("✓ Workflow resumed and complete"));
        if (Object.keys(result.output).length > 0) {
          console.log(chalk.bold("\nOutput:"));
          console.log(JSON.stringify(result.output, null, 2));
        }

        await provider.close();
        process.exit(0);
      } catch (err) {
        spinner.fail("Resume failed");
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
