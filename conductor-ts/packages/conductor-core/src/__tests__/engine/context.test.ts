/**
 * Ports tests/test_engine/test_context.py to TypeScript / vitest.
 *
 * Key TypeScript differences:
 *  - WorkflowContext constructor accepts optional named fields, not kwargs.
 *  - buildForAgent(name, inputs: string[], mode) — inputs is array of agent names (explicit mode).
 *  - Context structure: { workflow: { input, dir?, file?, name? }, context: { iteration, history, agent_name }, [agentName]: { output: {...} } }
 */
import { describe, expect, it } from "vitest";
import { WorkflowContext } from "../../engine/context.js";

describe("WorkflowContext initialisation", () => {
  it("starts with correct defaults", () => {
    const ctx = new WorkflowContext();
    expect(ctx.workflowInputs).toEqual({});
    expect(ctx.agentOutputs).toEqual({});
    expect(ctx.currentIteration).toBe(0);
    expect(ctx.executionHistory).toEqual([]);
    expect(ctx.workflowDir).toBe("");
    expect(ctx.workflowFile).toBe("");
    expect(ctx.workflowName).toBe("");
  });

  it("accepts initial metadata fields", () => {
    const ctx = new WorkflowContext();
    ctx.workflowDir = "/home/user/workflows";
    ctx.workflowFile = "/home/user/workflows/main.yaml";
    ctx.workflowName = "my-workflow";
    expect(ctx.workflowDir).toBe("/home/user/workflows");
    expect(ctx.workflowFile).toBe("/home/user/workflows/main.yaml");
    expect(ctx.workflowName).toBe("my-workflow");
  });
});

describe("WorkflowContext.setWorkflowInputs", () => {
  it("stores workflow inputs", () => {
    const ctx = new WorkflowContext();
    const inputs = { question: "What is Python?", max_length: 100 };
    ctx.setWorkflowInputs(inputs);
    expect(ctx.workflowInputs).toEqual(inputs);
  });

  it("stores a copy, not the original reference", () => {
    const ctx = new WorkflowContext();
    const inputs: Record<string, unknown> = { key: "value" };
    ctx.setWorkflowInputs(inputs);
    inputs["new_key"] = "extra";
    expect(ctx.workflowInputs).not.toHaveProperty("new_key");
  });
});

describe("WorkflowContext.store", () => {
  it("stores agent output and increments iteration", () => {
    const ctx = new WorkflowContext();
    ctx.store("answerer", { answer: "Python is a programming language" });
    expect(ctx.agentOutputs["answerer"]).toEqual({
      answer: "Python is a programming language",
    });
    expect(ctx.executionHistory).toEqual(["answerer"]);
    expect(ctx.currentIteration).toBe(1);
  });

  it("stores outputs from multiple agents", () => {
    const ctx = new WorkflowContext();
    ctx.store("agent1", { result: "first" });
    ctx.store("agent2", { result: "second" });
    ctx.store("agent3", { result: "third" });

    expect(ctx.currentIteration).toBe(3);
    expect(ctx.executionHistory).toEqual(["agent1", "agent2", "agent3"]);
    expect(ctx.agentOutputs["agent1"]["result"]).toBe("first");
    expect(ctx.agentOutputs["agent2"]["result"]).toBe("second");
    expect(ctx.agentOutputs["agent3"]["result"]).toBe("third");
  });
});

describe("WorkflowContext.getLatestOutput", () => {
  it("returns undefined when no outputs", () => {
    const ctx = new WorkflowContext();
    expect(ctx.getLatestOutput()).toBeUndefined();
  });

  it("returns the most recent output", () => {
    const ctx = new WorkflowContext();
    ctx.store("agent1", { result: "first" });
    expect(ctx.getLatestOutput()).toEqual({ result: "first" });
    ctx.store("agent2", { result: "second" });
    expect(ctx.getLatestOutput()).toEqual({ result: "second" });
  });
});

describe("WorkflowContext.buildForAgent - accumulate mode", () => {
  it("includes all prior agent outputs", () => {
    const ctx = new WorkflowContext();
    ctx.setWorkflowInputs({ goal: "test" });
    ctx.store("planner", { plan: "step 1" });
    ctx.store("executor", { result: "done" });

    const agentCtx = ctx.buildForAgent("reviewer", [], "accumulate");

    expect((agentCtx["workflow"] as Record<string, unknown>)["input"]).toEqual({ goal: "test" });
    expect((agentCtx["planner"] as Record<string, unknown>)["output"]).toEqual({ plan: "step 1" });
    expect((agentCtx["executor"] as Record<string, unknown>)["output"]).toEqual({ result: "done" });

    const ctxMeta = agentCtx["context"] as Record<string, unknown>;
    expect(ctxMeta["iteration"]).toBe(2);
    expect(ctxMeta["history"]).toEqual(["planner", "executor"]);
  });

  it("handles empty outputs", () => {
    const ctx = new WorkflowContext();
    ctx.setWorkflowInputs({ input: "value" });

    const agentCtx = ctx.buildForAgent("first_agent", [], "accumulate");

    expect((agentCtx["workflow"] as Record<string, unknown>)["input"]).toEqual({ input: "value" });
    const ctxMeta = agentCtx["context"] as Record<string, unknown>;
    expect(ctxMeta["iteration"]).toBe(0);
    expect(ctxMeta["history"]).toEqual([]);
  });
});

describe("WorkflowContext.buildForAgent - last_only mode", () => {
  it("includes only the most recent output", () => {
    const ctx = new WorkflowContext();
    ctx.setWorkflowInputs({ goal: "test" });
    ctx.store("planner", { plan: "step 1" });
    ctx.store("executor", { result: "done" });

    const agentCtx = ctx.buildForAgent("reviewer", [], "last_only");

    expect((agentCtx["workflow"] as Record<string, unknown>)["input"]).toEqual({ goal: "test" });
    expect(agentCtx).not.toHaveProperty("planner");
    expect((agentCtx["executor"] as Record<string, unknown>)["output"]).toEqual({ result: "done" });
  });

  it("handles empty history in last_only mode", () => {
    const ctx = new WorkflowContext();
    ctx.setWorkflowInputs({ input: "value" });

    const agentCtx = ctx.buildForAgent("first_agent", [], "last_only");

    expect(agentCtx).toHaveProperty("workflow");
    expect(agentCtx).toHaveProperty("context");
  });
});

describe("WorkflowContext.buildForAgent - workflow metadata", () => {
  it("includes dir, file, name in accumulate mode when set", () => {
    const ctx = new WorkflowContext();
    ctx.workflowDir = "/home/user/workflows";
    ctx.workflowFile = "/home/user/workflows/main.yaml";
    ctx.workflowName = "my-workflow";
    ctx.setWorkflowInputs({ key: "val" });

    const agentCtx = ctx.buildForAgent("agent", [], "accumulate");
    const wf = agentCtx["workflow"] as Record<string, unknown>;

    expect(wf["dir"]).toBe("/home/user/workflows");
    expect(wf["file"]).toBe("/home/user/workflows/main.yaml");
    expect(wf["name"]).toBe("my-workflow");
    expect(wf["input"]).toEqual({ key: "val" });
  });

  it("omits empty metadata fields", () => {
    const ctx = new WorkflowContext();

    const agentCtx = ctx.buildForAgent("agent", [], "accumulate");
    const wf = agentCtx["workflow"] as Record<string, unknown>;

    expect(wf).not.toHaveProperty("dir");
    expect(wf).not.toHaveProperty("file");
    expect(wf).not.toHaveProperty("name");
  });

  it("includes metadata in explicit mode", () => {
    const ctx = new WorkflowContext();
    ctx.workflowDir = "/registry/twig";
    ctx.workflowFile = "/registry/twig/sdlc.yaml";
    ctx.workflowName = "twig-sdlc";

    const agentCtx = ctx.buildForAgent("agent", [], "explicit");
    const wf = agentCtx["workflow"] as Record<string, unknown>;

    expect(wf["dir"]).toBe("/registry/twig");
    expect(wf["file"]).toBe("/registry/twig/sdlc.yaml");
    expect(wf["name"]).toBe("twig-sdlc");
  });
});
