/**
 * Checkpoint save/resume.
 * Mirrors src/conductor/engine/checkpoint.py
 *
 * Checkpoint format is compatible with the Python implementation —
 * both read/write the same JSON structure so runs can cross between
 * the Python CLI and the TypeScript CLI.
 */
import fs from "node:fs";
import path from "node:path";
import { WorkflowContext } from "./context.js";
import { CheckpointError } from "../exceptions.js";

export interface CheckpointData {
  version: "1";
  timestamp: number;
  workflowFile: string;
  nextAgent: string;
  context: Record<string, unknown>;
  metadata: Record<string, unknown>;
}

export class CheckpointManager {
  private readonly dir: string;

  constructor(workflowFile: string) {
    // Store checkpoints next to the workflow file
    this.dir = path.dirname(path.resolve(workflowFile));
  }

  private checkpointPath(workflowFile: string): string {
    const base = path.basename(workflowFile, path.extname(workflowFile));
    return path.join(this.dir, `${base}.checkpoint.json`);
  }

  save(
    workflowFile: string,
    nextAgent: string,
    context: WorkflowContext,
    metadata: Record<string, unknown> = {},
  ): void {
    const data: CheckpointData = {
      version: "1",
      timestamp: Date.now(),
      workflowFile: path.resolve(workflowFile),
      nextAgent,
      context: context.serialize(),
      metadata,
    };
    const filePath = this.checkpointPath(workflowFile);
    try {
      fs.writeFileSync(filePath, JSON.stringify(data, null, 2), "utf-8");
    } catch (err) {
      throw new CheckpointError(`Failed to save checkpoint: ${String(err)}`);
    }
  }

  load(workflowFile: string): CheckpointData {
    const filePath = this.checkpointPath(workflowFile);
    let raw: string;
    try {
      raw = fs.readFileSync(filePath, "utf-8");
    } catch {
      throw new CheckpointError(
        `No checkpoint found for '${workflowFile}'`,
        { suggestion: "Run the workflow from the beginning first." },
      );
    }
    try {
      return JSON.parse(raw) as CheckpointData;
    } catch (err) {
      throw new CheckpointError(`Checkpoint file is corrupt: ${String(err)}`);
    }
  }

  restoreContext(data: CheckpointData): WorkflowContext {
    return WorkflowContext.deserialize(data.context);
  }

  exists(workflowFile: string): boolean {
    return fs.existsSync(this.checkpointPath(workflowFile));
  }

  delete(workflowFile: string): void {
    const filePath = this.checkpointPath(workflowFile);
    if (fs.existsSync(filePath)) {
      fs.unlinkSync(filePath);
    }
  }

  list(): CheckpointData[] {
    const results: CheckpointData[] = [];
    for (const file of fs.readdirSync(this.dir)) {
      if (file.endsWith(".checkpoint.json")) {
        try {
          const raw = fs.readFileSync(path.join(this.dir, file), "utf-8");
          results.push(JSON.parse(raw) as CheckpointData);
        } catch {
          // Skip corrupt checkpoints
        }
      }
    }
    return results.sort((a, b) => b.timestamp - a.timestamp);
  }
}
