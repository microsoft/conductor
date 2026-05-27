/**
 * WorkflowContext — manages workflow execution state.
 * Mirrors src/conductor/engine/context.py
 *
 * Three accumulation modes:
 *   accumulate  — all prior agent outputs available
 *   last_only   — only previous agent's output
 *   explicit    — only inputs declared in agent.input
 */

/** Script/workflow agents always see workflow.input even in explicit mode. */
const LOCAL_RENDER_AGENT_TYPES = new Set(["script", "workflow"]);

export class WorkflowContext {
  workflowInputs: Record<string, unknown> = {};
  workflowDir = "";
  workflowFile = "";
  workflowName = "";
  agentOutputs: Record<string, Record<string, unknown>> = {};
  currentIteration = 0;
  executionHistory: string[] = [];
  userGuidance: string[] = [];

  setWorkflowInputs(inputs: Record<string, unknown>): void {
    this.workflowInputs = { ...inputs };
  }

  getLatestOutput(): Record<string, unknown> | undefined {
    const lastAgent = this.executionHistory[this.executionHistory.length - 1];
    return lastAgent ? this.agentOutputs[lastAgent] : undefined;
  }

  store(agentName: string, output: Record<string, unknown>): void {
    this.agentOutputs[agentName] = output;
    this.executionHistory.push(agentName);
    this.currentIteration++;
  }

  addGuidance(text: string): void {
    this.userGuidance.push(text);
  }

  getGuidancePromptSection(): string | undefined {
    if (this.userGuidance.length === 0) return undefined;
    const entries = this.userGuidance.map((g) => `- ${g}`).join("\n");
    return (
      "\n\n[User Guidance]\n" +
      "The following guidance was provided by the user during workflow execution. " +
      "Incorporate this guidance into your response:\n" +
      entries
    );
  }

  buildForAgent(
    agentName: string,
    inputs: string[],
    mode: "accumulate" | "last_only" | "explicit" = "accumulate",
    agentType?: string,
  ): Record<string, unknown> {
    const workflowMeta: Record<string, unknown> = {};
    if (this.workflowDir) workflowMeta["dir"] = this.workflowDir;
    if (this.workflowFile) workflowMeta["file"] = this.workflowFile;
    if (this.workflowName) workflowMeta["name"] = this.workflowName;

    const isLocalRender = agentType ? LOCAL_RENDER_AGENT_TYPES.has(agentType) : false;

    // In explicit mode, workflow.input is still available for local-render agents
    const workflowInputsForCtx =
      mode !== "explicit" || isLocalRender ? this.workflowInputs : {};

    const ctx: Record<string, unknown> = {
      workflow: { ...workflowMeta, input: workflowInputsForCtx },
      context: {
        iteration: this.currentIteration,
        history: [...this.executionHistory],
        agent_name: agentName,
      },
    };

    if (mode === "accumulate") {
      for (const [name, output] of Object.entries(this.agentOutputs)) {
        ctx[name] = { output };
      }
    } else if (mode === "last_only") {
      const lastAgent = this.executionHistory[this.executionHistory.length - 1];
      if (lastAgent && this.agentOutputs[lastAgent]) {
        ctx[lastAgent] = { output: this.agentOutputs[lastAgent] };
      }
    } else {
      // explicit
      for (const ref of inputs) {
        const optional = ref.endsWith("?");
        const cleanRef = optional ? ref.slice(0, -1) : ref;
        this.resolveExplicitInput(cleanRef, ctx, optional);
      }
    }

    return ctx;
  }

  private resolveExplicitInput(
    ref: string,
    ctx: Record<string, unknown>,
    optional: boolean,
  ): void {
    // workflow.input.param or workflow.input
    if (ref.startsWith("workflow.input")) {
      const parts = ref.split(".");
      if (parts.length === 2) {
        ctx["workflow"] = { ...(ctx["workflow"] as object), input: this.workflowInputs };
      } else {
        const paramName = parts[2]!;
        const existing = (ctx["workflow"] as Record<string, unknown>) ?? {};
        const existingInput = (existing["input"] as Record<string, unknown>) ?? {};
        if (paramName in this.workflowInputs) {
          ctx["workflow"] = {
            ...existing,
            input: { ...existingInput, [paramName]: this.workflowInputs[paramName] },
          };
        } else if (!optional) {
          throw new Error(`Required input '${ref}' not found in workflow inputs`);
        }
      }
      return;
    }

    // agent_name.output or agent_name.output.field
    const dotIdx = ref.indexOf(".");
    if (dotIdx === -1) {
      // Just agent name
      if (this.agentOutputs[ref]) {
        ctx[ref] = { output: this.agentOutputs[ref] };
      } else if (!optional) {
        throw new Error(`Required input agent '${ref}' has no output in context`);
      }
      return;
    }

    const agentName = ref.slice(0, dotIdx);
    const rest = ref.slice(dotIdx + 1);
    const agentOutput = this.agentOutputs[agentName];
    if (!agentOutput && !optional) {
      throw new Error(`Required input '${ref}' — agent '${agentName}' has no output`);
    }
    if (agentOutput) {
      ctx[agentName] = ctx[agentName] ?? { output: agentOutput };
      // If requesting a specific field, verify it exists
      if (rest !== "output") {
        const field = rest.replace(/^output\./, "");
        if (!(field in agentOutput) && !optional) {
          throw new Error(`Required input '${ref}' — field '${field}' not in '${agentName}' output`);
        }
      }
    }
  }

  /** Snapshot for parallel group isolation. */
  snapshot(): WorkflowContext {
    const copy = new WorkflowContext();
    copy.workflowInputs = { ...this.workflowInputs };
    copy.workflowDir = this.workflowDir;
    copy.workflowFile = this.workflowFile;
    copy.workflowName = this.workflowName;
    copy.agentOutputs = Object.fromEntries(
      Object.entries(this.agentOutputs).map(([k, v]) => [k, { ...v }]),
    );
    copy.currentIteration = this.currentIteration;
    copy.executionHistory = [...this.executionHistory];
    copy.userGuidance = [...this.userGuidance];
    return copy;
  }

  serialize(): Record<string, unknown> {
    return {
      workflowInputs: this.workflowInputs,
      workflowDir: this.workflowDir,
      workflowFile: this.workflowFile,
      workflowName: this.workflowName,
      agentOutputs: this.agentOutputs,
      currentIteration: this.currentIteration,
      executionHistory: this.executionHistory,
      userGuidance: this.userGuidance,
    };
  }

  static deserialize(data: Record<string, unknown>): WorkflowContext {
    const ctx = new WorkflowContext();
    ctx.workflowInputs = (data["workflowInputs"] as Record<string, unknown>) ?? {};
    ctx.workflowDir = (data["workflowDir"] as string) ?? "";
    ctx.workflowFile = (data["workflowFile"] as string) ?? "";
    ctx.workflowName = (data["workflowName"] as string) ?? "";
    ctx.agentOutputs =
      (data["agentOutputs"] as Record<string, Record<string, unknown>>) ?? {};
    ctx.currentIteration = (data["currentIteration"] as number) ?? 0;
    ctx.executionHistory = (data["executionHistory"] as string[]) ?? [];
    ctx.userGuidance = (data["userGuidance"] as string[]) ?? [];
    return ctx;
  }
}
