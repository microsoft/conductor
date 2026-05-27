/**
 * `conductor checkpoints` command.
 */
import type { Command } from "commander";
import path from "node:path";
import chalk from "chalk";
import { CheckpointManager } from "@conductor/core";

export function registerCheckpointsCommand(program: Command): void {
  program
    .command("checkpoints [directory]")
    .description("List available checkpoints")
    .action(async (directory?: string) => {
      const dir = path.resolve(directory ?? ".");
      // Use a dummy workflow path to initialize the manager pointing at dir
      const manager = new CheckpointManager(path.join(dir, "workflow.yaml"));
      const checkpoints = manager.list();

      if (checkpoints.length === 0) {
        console.log(chalk.dim("No checkpoints found."));
        return;
      }

      console.log(chalk.bold(`Found ${checkpoints.length} checkpoint(s):\n`));
      for (const cp of checkpoints) {
        const ts = new Date(cp.timestamp).toLocaleString();
        console.log(
          `  ${chalk.cyan(path.basename(cp.workflowFile))}  ` +
          `next: ${chalk.yellow(cp.nextAgent)}  ` +
          `saved: ${chalk.dim(ts)}`,
        );
      }
    });
}
