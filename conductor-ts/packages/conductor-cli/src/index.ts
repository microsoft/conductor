#!/usr/bin/env node
/**
 * Conductor CLI entrypoint.
 * Mirrors the Python `conductor` CLI: run, resume, validate, stop, checkpoints.
 */
import { program } from "commander";
import { registerRunCommand } from "./commands/run.js";
import { registerResumeCommand } from "./commands/resume.js";
import { registerValidateCommand } from "./commands/validate.js";
import { registerCheckpointsCommand } from "./commands/checkpoints.js";

program
  .name("conductor")
  .description("Multi-agent workflow orchestration")
  .version("0.1.0");

registerRunCommand(program);
registerResumeCommand(program);
registerValidateCommand(program);
registerCheckpointsCommand(program);

program.parseAsync(process.argv).catch((err: unknown) => {
  console.error(err instanceof Error ? err.message : String(err));
  process.exit(1);
});
