/**
 * `conductor validate <workflow.yaml>` command.
 */
import type { Command } from "commander";
import path from "node:path";
import chalk from "chalk";
import { loadConfig, validateConfig, ConductorError } from "@conductor/core";

export function registerValidateCommand(program: Command): void {
  program
    .command("validate <workflow>")
    .description("Validate a workflow YAML file")
    .action(async (workflowFile: string) => {
      const absPath = path.resolve(workflowFile);
      try {
        const config = loadConfig(absPath);
        validateConfig(config, absPath);
        console.log(chalk.green(`✓ ${path.basename(absPath)} is valid`));
        console.log(`  Name: ${config.workflow.name}`);
        console.log(`  Agents: ${config.agents.length}`);
        if (config.parallel.length) console.log(`  Parallel groups: ${config.parallel.length}`);
        if (config.for_each.length) console.log(`  For-each groups: ${config.for_each.length}`);
      } catch (err) {
        if (err instanceof ConductorError) {
          console.error(chalk.red(`✗ Validation failed: ${err.message}`));
          if (err.suggestion) console.error(chalk.yellow(`  💡 ${err.suggestion}`));
        } else {
          console.error(err);
        }
        process.exit(1);
      }
    });
}
