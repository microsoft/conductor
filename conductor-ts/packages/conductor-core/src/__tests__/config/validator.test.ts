/**
 * Ports tests/test_config/test_validator.py to TypeScript / vitest.
 */
import { describe, expect, it } from "vitest";
import { validateConfig } from "../../config/validator.js";
import { ConfigurationError } from "../../exceptions.js";

function baseConfig() {
  return {
    workflow: {
      name: "test",
      entry_point: "agent1",
      runtime: undefined,
      context: undefined,
      limits: undefined,
      metadata: undefined,
      instructions: undefined,
      version: undefined,
    },
    agents: [
      {
        name: "agent1",
        prompt: "",
        input: [] as string[],
        routes: [{ to: "$end" as string }],
        args: [] as string[],
        env: {} as Record<string, string>,
        interactive_input: false,
      },
    ],
    parallel: [] as never[],
    for_each: [] as never[],
    tools: [] as never[],
    output: undefined,
  };
}

describe("validateConfig - valid configs", () => {
  it("does not throw for a valid minimal config", () => {
    expect(() => validateConfig(baseConfig() as Parameters<typeof validateConfig>[0])).not.toThrow();
  });

  it("accepts $end as a valid route target", () => {
    const cfg = baseConfig();
    cfg.agents[0].routes = [{ to: "$end" }];
    expect(() => validateConfig(cfg as Parameters<typeof validateConfig>[0])).not.toThrow();
  });

  it("accepts routing between defined agents", () => {
    const cfg = baseConfig();
    cfg.agents = [
      { name: "a1", prompt: "", input: [], routes: [{ to: "a2" }], args: [], env: {}, interactive_input: false },
      { name: "a2", prompt: "", input: [], routes: [{ to: "$end" }], args: [], env: {}, interactive_input: false },
    ];
    cfg.workflow.entry_point = "a1";
    expect(() => validateConfig(cfg as Parameters<typeof validateConfig>[0])).not.toThrow();
  });
});

describe("validateConfig - entry_point errors", () => {
  it("throws ConfigurationError when entry_point is undefined agent", () => {
    const cfg = baseConfig();
    cfg.workflow.entry_point = "does_not_exist";
    expect(() => validateConfig(cfg as Parameters<typeof validateConfig>[0])).toThrow(
      ConfigurationError,
    );
  });

  it("error message mentions the missing entry_point name", () => {
    const cfg = baseConfig();
    cfg.workflow.entry_point = "missing_agent";
    try {
      validateConfig(cfg as Parameters<typeof validateConfig>[0]);
    } catch (err) {
      expect(err).toBeInstanceOf(ConfigurationError);
      if (err instanceof ConfigurationError) {
        expect(err.message).toContain("missing_agent");
      }
    }
  });
});

describe("validateConfig - route errors", () => {
  it("throws ConfigurationError when agent routes to unknown target", () => {
    const cfg = baseConfig();
    cfg.agents[0].routes = [{ to: "nonexistent" }];
    expect(() => validateConfig(cfg as Parameters<typeof validateConfig>[0])).toThrow(
      ConfigurationError,
    );
  });

  it("error message mentions the unknown route target", () => {
    const cfg = baseConfig();
    cfg.agents[0].routes = [{ to: "unknown_target" }];
    try {
      validateConfig(cfg as Parameters<typeof validateConfig>[0]);
    } catch (err) {
      expect(err).toBeInstanceOf(ConfigurationError);
      if (err instanceof ConfigurationError) {
        expect(err.message).toContain("unknown_target");
      }
    }
  });
});

describe("validateConfig - duplicate names", () => {
  it("throws ConfigurationError for duplicate agent names", () => {
    const cfg = baseConfig();
    cfg.agents = [
      { name: "agent1", prompt: "", input: [], routes: [{ to: "$end" }], args: [], env: {}, interactive_input: false },
      { name: "agent1", prompt: "", input: [], routes: [{ to: "$end" }], args: [], env: {}, interactive_input: false },
    ];
    expect(() => validateConfig(cfg as Parameters<typeof validateConfig>[0])).toThrow(
      ConfigurationError,
    );
  });
});
