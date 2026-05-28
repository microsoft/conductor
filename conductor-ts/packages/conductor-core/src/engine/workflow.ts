/**
 * WorkflowEngine — the main orchestrator.
 * Mirrors src/conductor/engine/workflow.py
 *
 * Supports: sequential agents, parallel groups, for-each groups,
 * script steps, human gates, sub-workflows, routing, checkpoints.
 */
import path from "node:path";
import { WorkflowContext } from "./context.js";
import { Router } from "./router.js";
import { LimitEnforcer } from "./limits.js";
import { CheckpointManager } from "./checkpoint.js";
import { TemplateRenderer } from "../executor/template.js";
import { executeScript } from "../executor/script.js";
import { parseOutput } from "../executor/output.js";
import { WorkflowEventEmitter } from "../events.js";
import type { AgentProvider, ExecuteOptions, UserInputRequest, UserInputResponse } from "../providers/base.js";
import type { WorkflowConfig, AgentDef, ParallelGroup, ForEachDef } from "../config/schema.js";
import { ExecutionError, MaxIterationsError, ValidationError } from "../exceptions.js";

export interface WorkflowEngineOptions {
  /** Provider for agent execution. */
  provider: AgentProvider;
  /** Per-agent provider overrides (name → provider). */
  agentProviders?: Map<string, AgentProvider>;
  /** Event emitter. */
  emitter?: WorkflowEventEmitter;
  /** Skill directories to pass to providers. */
  skillDirectories?: string[];
  /** Callback when an agent needs interactive input. */
  onUserInputRequest?: (req: UserInputRequest) => Promise<UserInputResponse>;
  /** Resume from checkpoint nextAgent. */
  resumeFrom?: string;
  /** Pre-built context for resume. */
  resumeContext?: WorkflowContext;
  /** Sub-workflow depth (prevents infinite recursion). */
  subworkflowDepth?: number;
  /** AbortSignal for external cancellation. */
  signal?: AbortSignal;
}

export interface WorkflowResult {
  output: Record<string, unknown>;
  context: WorkflowContext;
  durationMs: number;
}

const MAX_SUBWORKFLOW_DEPTH = 10;
const renderer = new TemplateRenderer();
const router = new Router();

export class WorkflowEngine {
  private readonly config: WorkflowConfig;
  private readonly options: WorkflowEngineOptions;
  private readonly emitter: WorkflowEventEmitter;
  private context: WorkflowContext;

  constructor(config: WorkflowConfig, options: WorkflowEngineOptions) {
    this.config = config;
    this.options = options;
    this.emitter = options.emitter ?? new WorkflowEventEmitter();
    this.context = options.resumeContext ?? new WorkflowContext();
  }

  async run(inputs: Record<string, unknown> = {}): Promise<WorkflowResult> {
    const startMs = Date.now();

    if (!this.options.resumeContext) {
      this.context.setWorkflowInputs(inputs);
    }

    const limits = new LimitEnforcer(this.config.workflow.limits ?? { max_iterations: 10 });

    await this.emitter.emit("workflow_started", {
      workflowName: this.config.workflow.name,
      inputs,
    });

    // Find start agent
    const startAgent =
      this.options.resumeFrom ??
      this.config.workflow.entry_point ??
      this.config.agents[0]?.name ??
      this.config.parallel[0]?.name ??
      this.config.for_each[0]?.name;

    if (!startAgent) {
      throw new ExecutionError("Workflow has no agents defined.");
    }

    let current: string | null = startAgent;

    try {
      while (current && current !== "$end") {
        limits.check(this.context.currentIteration);

        if (this.options.signal?.aborted) {
          throw new ExecutionError("Workflow aborted via signal.");
        }

        const nextTarget = await this.step(current);
        current = nextTarget === "$end" ? null : nextTarget;
      }
    } catch (err) {
      await this.emitter.emit("workflow_failed", {
        workflowName: this.config.workflow.name,
        error: err instanceof Error ? err.message : String(err),
      });
      throw err;
    }

    const output = this.buildOutput();
    await this.emitter.emit("workflow_completed", {
      workflowName: this.config.workflow.name,
      output,
      durationMs: Date.now() - startMs,
    });

    return {
      output,
      context: this.context,
      durationMs: Date.now() - startMs,
    };
  }

  // -------------------------------------------------------------------------
  // Step dispatch
  // -------------------------------------------------------------------------

  private async step(name: string): Promise<string> {
    // Check parallel groups
    const parallel = this.config.parallel.find((p) => p.name === name);
    if (parallel) return this.runParallel(parallel);

    // Check for-each groups
    const forEach = this.config.for_each.find((f) => f.name === name);
    if (forEach) return this.runForEach(forEach);

    // Regular agent
    const agent = this.config.agents.find((a) => a.name === name);
    if (!agent) {
      throw new ExecutionError(`Unknown step '${name}' — not found in agents, parallel, or for_each`);
    }

    switch (agent.type ?? "agent") {
      case "script":
        return this.runScript(agent);
      case "human_gate":
        return this.runHumanGate(agent);
      case "workflow":
        return this.runSubworkflow(agent);
      default:
        return this.runAgent(agent);
    }
  }

  // -------------------------------------------------------------------------
  // Regular agent
  // -------------------------------------------------------------------------

  private async runAgent(agent: AgentDef): Promise<string> {
    await this.emitter.emit("agent_started", { agentName: agent.name });

    const mode = this.config.workflow.context?.mode ?? "accumulate";
    const ctx = this.context.buildForAgent(agent.name, agent.input, mode, agent.type ?? "agent");

    // Render prompt
    const prompt = renderer.render(agent.prompt, ctx);

    // Resolve provider
    const provider =
      this.options.agentProviders?.get(agent.name) ??
      this.resolveProviderForAgent(agent);

    const execOpts: ExecuteOptions = {
      skillDirectories: [
        ...(this.options.skillDirectories ?? []),
        ...(this.config.workflow.runtime?.skill_directories ?? []),
        ...(agent.skill_directories ?? []),
      ],
      onUserInputRequest: this.options.onUserInputRequest,
      maxIterations: agent.max_agent_iterations ?? this.config.workflow.runtime?.max_agent_iterations,
      emitter: this.emitter,
      signal: this.options.signal,
    };

    let agentOutput;
    try {
      agentOutput = await provider.execute(agent, prompt, execOpts);
    } catch (err) {
      await this.emitter.emit("agent_failed", {
        agentName: agent.name,
        error: err instanceof Error ? err.message : String(err),
      });
      throw err;
    }

    await this.emitter.emit("agent_message", {
      agentName: agent.name,
      content: agentOutput.content,
      model: agentOutput.model,
    });

    if (agentOutput.reasoningContent) {
      await this.emitter.emit("agent_reasoning", {
        agentName: agent.name,
        content: agentOutput.reasoningContent,
      });
    }

    // Parse output
    const parsed = parseOutput(agentOutput.content, agent.output, agent.name);
    this.context.store(agent.name, parsed);

    // Evaluate route before emitting so the destination is included in the event
    const nextAgent = this.route(agent.routes, parsed, agent.name);

    await this.emitter.emit("agent_completed", {
      agentName: agent.name,
      output: parsed,
      nextAgent,
      model: agentOutput.model,
      inputTokens: agentOutput.inputTokens ?? 0,
      outputTokens: agentOutput.outputTokens ?? 0,
    });

    return nextAgent;
  }

  // -------------------------------------------------------------------------
  // Script agent
  // -------------------------------------------------------------------------

  private async runScript(agent: AgentDef): Promise<string> {
    await this.emitter.emit("agent_started", { agentName: agent.name, type: "script" });

    const mode = this.config.workflow.context?.mode ?? "accumulate";
    const ctx = this.context.buildForAgent(agent.name, agent.input, mode, "script");

    const result = await executeScript(agent, ctx);

    // Build output: declared output fields + script metadata
    const parsed = agent.output
      ? parseOutput(JSON.stringify(result.output), agent.output, agent.name)
      : result.output;

    const stored: Record<string, unknown> = {
      ...parsed,
      stdout: result.stdout,
      stderr: result.stderr,
      exit_code: result.exit_code,
      success: result.success,
    };

    this.context.store(agent.name, stored);
    await this.emitter.emit("agent_completed", { agentName: agent.name, output: stored });

    return this.route(agent.routes, stored, agent.name);
  }

  // -------------------------------------------------------------------------
  // Human gate
  // -------------------------------------------------------------------------

  private async runHumanGate(agent: AgentDef): Promise<string> {
    if (!agent.options?.length) {
      throw new ExecutionError(`Human gate '${agent.name}' has no options defined`);
    }
    if (!this.options.onUserInputRequest) {
      // Auto-select first option if no input handler
      const first = agent.options[0]!;
      this.context.store(agent.name, { selected: first.value });
      return first.route;
    }

    const response = await this.options.onUserInputRequest({
      question: agent.prompt || `Choose an option for '${agent.name}':`,
      choices: agent.options.map((o) => o.label),
      allowFreeform: false,
    });

    const chosen = agent.options.find(
      (o) => o.label === response.answer || o.value === response.answer,
    ) ?? agent.options[0]!;

    this.context.store(agent.name, { selected: chosen.value });
    return chosen.route;
  }

  // -------------------------------------------------------------------------
  // Sub-workflow
  // -------------------------------------------------------------------------

  private async runSubworkflow(agent: AgentDef): Promise<string> {
    const depth = this.options.subworkflowDepth ?? 0;
    if (depth >= MAX_SUBWORKFLOW_DEPTH) {
      throw new ExecutionError(
        `Sub-workflow depth limit (${MAX_SUBWORKFLOW_DEPTH}) exceeded`,
      );
    }

    if (!agent.workflow) {
      throw new ExecutionError(`Workflow agent '${agent.name}' has no 'workflow' path`);
    }

    const { loadConfig } = await import("../config/loader.js");
    const { validateConfig } = await import("../config/validator.js");

    const workflowPath = path.resolve(this.context.workflowDir || ".", agent.workflow);
    const subConfig = loadConfig(workflowPath);
    validateConfig(subConfig, workflowPath);

    const mode = this.config.workflow.context?.mode ?? "accumulate";
    const parentCtx = this.context.buildForAgent(agent.name, agent.input, mode, "workflow");

    // Build sub-workflow inputs
    let subInputs: Record<string, unknown> = this.context.workflowInputs;
    if (agent.input_mapping) {
      subInputs = {};
      for (const [key, expr] of Object.entries(agent.input_mapping)) {
        subInputs[key] = renderer.render(expr, parentCtx);
      }
    }

    const subEngine = new WorkflowEngine(subConfig, {
      ...this.options,
      subworkflowDepth: depth + 1,
      resumeFrom: undefined,
      resumeContext: undefined,
    });
    const result = await subEngine.run(subInputs);

    this.context.store(agent.name, result.output);
    await this.emitter.emit("agent_completed", { agentName: agent.name, output: result.output });

    return this.route(agent.routes, result.output, agent.name);
  }

  // -------------------------------------------------------------------------
  // Parallel group
  // -------------------------------------------------------------------------

  private async runParallel(group: ParallelGroup): Promise<string> {
    await this.emitter.emit("parallel_started", { groupName: group.name });

    const snapshot = this.context.snapshot();
    const tasks = group.agents.map(async (agentName) => {
      const agent = this.config.agents.find((a) => a.name === agentName);
      if (!agent) throw new ExecutionError(`Unknown agent '${agentName}' in parallel group`);

      const isolated = snapshot.snapshot();
      const subEngine = new WorkflowEngine(this.config, {
        ...this.options,
        resumeContext: isolated,
        resumeFrom: agentName,
      });
      // Run just the one agent
      const mode = this.config.workflow.context?.mode ?? "accumulate";
      const ctx = isolated.buildForAgent(agentName, agent.input, mode, agent.type ?? "agent");
      const prompt = renderer.render(agent.prompt, ctx);
      const provider = this.resolveProviderForAgent(agent);
      const execOpts: ExecuteOptions = {
        skillDirectories: this.options.skillDirectories,
        onUserInputRequest: this.options.onUserInputRequest,
        emitter: this.emitter,
        signal: this.options.signal,
      };
      const agentOutput = await provider.execute(agent, prompt, execOpts);
      const parsed = parseOutput(agentOutput.content, agent.output, agentName);
      return { agentName, output: parsed };
    });

    const results = await this.settleParallel(tasks, group.failure_mode);

    // Merge outputs into main context
    for (const [name, output] of Object.entries(results)) {
      this.context.store(name, output as Record<string, unknown>);
    }

    await this.emitter.emit("parallel_completed", { groupName: group.name, outputs: results });

    const groupOutput = { outputs: results };
    this.context.store(group.name, groupOutput);
    return this.route(group.routes, groupOutput, group.name);
  }

  // -------------------------------------------------------------------------
  // For-each group
  // -------------------------------------------------------------------------

  private async runForEach(fe: ForEachDef): Promise<string> {
    await this.emitter.emit("foreach_started", { groupName: fe.name });

    const mode = this.config.workflow.context?.mode ?? "accumulate";
    const ctx = this.context.buildForAgent(fe.name, [], mode);

    // Resolve source array
    const parts = fe.source.split(".");
    let source: unknown = ctx;
    for (const part of parts) {
      source = (source as Record<string, unknown>)[part];
    }
    if (!Array.isArray(source)) {
      throw new ExecutionError(
        `for_each source '${fe.source}' did not resolve to an array`,
      );
    }

    const items: unknown[] = source;
    const results: unknown[] = [];
    const errors: Array<{ index: number; error: string }> = [];

    // Process in batches
    for (let batchStart = 0; batchStart < items.length; batchStart += fe.max_concurrent) {
      const batch = items.slice(batchStart, batchStart + fe.max_concurrent);
      const batchTasks = batch.map(async (item, batchIdx) => {
        const globalIdx = batchStart + batchIdx;
        const loopCtx: Record<string, unknown> = {
          ...ctx,
          [fe.as]: item,
          _index: globalIdx,
          _key: fe.key_by ? (item as Record<string, unknown>)[fe.key_by] : globalIdx,
        };
        const prompt = renderer.render(fe.agent.prompt, loopCtx);
        const provider = this.resolveProviderForAgent(fe.agent);
        const execOpts: ExecuteOptions = {
          skillDirectories: this.options.skillDirectories,
          onUserInputRequest: this.options.onUserInputRequest,
          emitter: this.emitter,
          signal: this.options.signal,
        };
        const agentOutput = await provider.execute(fe.agent, prompt, execOpts);
        return parseOutput(agentOutput.content, fe.agent.output, fe.agent.name);
      });

      const settled = await this.settleForEachBatch(batchTasks, fe.failure_mode, batchStart);
      for (const r of settled) {
        if (r.success) {
          results.push(r.value);
        } else {
          errors.push({ index: r.index, error: r.error ?? "unknown" });
        }
      }
    }

    const outputs = fe.key_by
      ? Object.fromEntries(
          results.map((r, i) => [
            (items[i] as Record<string, unknown>)[fe.key_by!],
            r,
          ]),
        )
      : results;

    const groupOutput = { outputs, errors };
    this.context.store(fe.name, groupOutput);
    await this.emitter.emit("foreach_completed", { groupName: fe.name, outputs });

    return this.route(fe.routes, groupOutput, fe.name);
  }

  // -------------------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------------------

  private route(
    routes: WorkflowConfig["agents"][0]["routes"],
    output: Record<string, unknown>,
    stepName: string,
  ): string {
    if (routes.length === 0) return "$end";
    const ctx = this.context.buildForAgent(stepName, [], this.config.workflow.context?.mode ?? "accumulate");
    const result = router.evaluate(routes, output, ctx);
    return result.target;
  }

  private resolveProviderForAgent(agent: AgentDef): AgentProvider {
    const providerName =
      agent.provider ?? this.config.workflow.runtime?.provider ?? "copilot";
    if (this.options.agentProviders?.has(providerName)) {
      return this.options.agentProviders.get(providerName)!;
    }
    return this.options.provider;
  }

  private async settleParallel(
    tasks: Promise<{ agentName: string; output: Record<string, unknown> }>[],
    failureMode: "fail_fast" | "continue_on_error" | "all_or_nothing",
  ): Promise<Record<string, unknown>> {
    const settled = await Promise.allSettled(tasks);
    const outputs: Record<string, unknown> = {};
    const errors: string[] = [];

    for (const result of settled) {
      if (result.status === "fulfilled") {
        outputs[result.value.agentName] = result.value.output;
      } else {
        errors.push(String(result.reason));
        if (failureMode === "fail_fast") throw result.reason;
      }
    }

    if (failureMode === "all_or_nothing" && errors.length > 0) {
      throw new ExecutionError(`Parallel group failed: ${errors.join("; ")}`);
    }
    if (failureMode === "continue_on_error" && Object.keys(outputs).length === 0) {
      throw new ExecutionError("Parallel group: all agents failed");
    }
    return outputs;
  }

  private async settleForEachBatch(
    tasks: Promise<Record<string, unknown>>[],
    failureMode: "fail_fast" | "continue_on_error" | "all_or_nothing",
    startIndex: number,
  ): Promise<Array<{ success: boolean; value?: unknown; error?: string; index: number }>> {
    const settled = await Promise.allSettled(tasks);
    const results = [];
    for (let i = 0; i < settled.length; i++) {
      const r = settled[i]!;
      const idx = startIndex + i;
      if (r.status === "fulfilled") {
        results.push({ success: true, value: r.value, index: idx });
      } else {
        const err = String(r.reason);
        if (failureMode === "fail_fast") throw r.reason;
        results.push({ success: false, error: err, index: idx });
      }
    }
    if (failureMode === "all_or_nothing" && results.some((r) => !r.success)) {
      throw new ExecutionError("for_each group: one or more items failed (all_or_nothing)");
    }
    return results;
  }

  private buildOutput(): Record<string, unknown> {
    if (!this.config.output) return {};
    const ctx = this.context.buildForAgent("$output", [], "accumulate");
    return Object.fromEntries(
      Object.entries(this.config.output).map(([k, expr]) => [
        k,
        renderer.render(expr, ctx),
      ]),
    );
  }
}
