/**
 * Ports tests/test_config/test_loader.py to TypeScript / vitest.
 *
 * The TypeScript loader does not have a full ConfigLoader class;
 * the main public API is loadConfig(filePath) and loadConfigFromString(yaml).
 * We test loadConfigFromString for most cases and loadConfig for file-level errors.
 */
import { writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { loadConfig, loadConfigFromString } from "../../config/loader.js";
import { ConfigurationError } from "../../exceptions.js";

const VALID_MINIMAL = `
workflow:
  name: test-workflow
  entry_point: agent1
agents:
  - name: agent1
    prompt: Hello
    routes:
      - to: $end
`;

const VALID_FULL = `
workflow:
  name: full-workflow
  entry_point: planner
  runtime:
    provider: copilot
  limits:
    max_iterations: 20
    timeout_seconds: 300
  context:
    mode: accumulate
agents:
  - name: planner
    prompt: Plan the task
    routes:
      - to: $end
`;

describe("loadConfigFromString - valid configs", () => {
  it("loads a minimal valid workflow", () => {
    const config = loadConfigFromString(VALID_MINIMAL, "test.yaml");
    expect(config.workflow.name).toBe("test-workflow");
    expect(config.workflow.entry_point).toBe("agent1");
    expect(config.agents).toHaveLength(1);
    expect(config.agents[0].name).toBe("agent1");
  });

  it("loads a full workflow with limits and context", () => {
    const config = loadConfigFromString(VALID_FULL, "full.yaml");
    expect(config.workflow.name).toBe("full-workflow");
    expect(config.workflow.runtime?.provider).toBe("copilot");
    expect(config.workflow.limits?.max_iterations).toBe(20);
    expect(config.workflow.limits?.timeout_seconds).toBe(300);
    expect(config.workflow.context?.mode).toBe("accumulate");
  });

  it("parses multiple agents", () => {
    const yaml = `
workflow:
  name: multi-agent
  entry_point: a1
agents:
  - name: a1
    prompt: Step 1
    routes:
      - to: a2
  - name: a2
    prompt: Step 2
    routes:
      - to: $end
`;
    const config = loadConfigFromString(yaml, "test.yaml");
    expect(config.agents).toHaveLength(2);
    expect(config.agents[0].name).toBe("a1");
    expect(config.agents[1].name).toBe("a2");
  });

  it("parses agent routes", () => {
    const config = loadConfigFromString(VALID_MINIMAL, "test.yaml");
    expect(config.agents[0].routes).toHaveLength(1);
    expect(config.agents[0].routes[0].to).toBe("$end");
  });
});

describe("loadConfigFromString - env var resolution", () => {
  it("resolves ${VAR} env vars in YAML values", () => {
    const originalVal = process.env["MY_TEST_MODEL"];
    process.env["MY_TEST_MODEL"] = "gpt-4-turbo";
    try {
      const yaml = `
workflow:
  name: env-test
  entry_point: agent1
agents:
  - name: agent1
    model: \${MY_TEST_MODEL}
    prompt: Hello
    routes:
      - to: $end
`;
      const config = loadConfigFromString(yaml, "test.yaml");
      expect(config.agents[0].model).toBe("gpt-4-turbo");
    } finally {
      if (originalVal === undefined) {
        delete process.env["MY_TEST_MODEL"];
      } else {
        process.env["MY_TEST_MODEL"] = originalVal;
      }
    }
  });

  it("uses default value when env var is not set", () => {
    delete process.env["UNSET_CONDUCTOR_TEST_VAR"];
    const yaml = `
workflow:
  name: default-test
  entry_point: agent1
agents:
  - name: agent1
    model: \${UNSET_CONDUCTOR_TEST_VAR:-gpt-3.5}
    prompt: Hello
    routes:
      - to: $end
`;
    const config = loadConfigFromString(yaml, "test.yaml");
    expect(config.agents[0].model).toBe("gpt-3.5");
  });
});

describe("loadConfigFromString - validation errors", () => {
  it("throws ConfigurationError for malformed YAML", () => {
    const yaml = `
workflow:
  name: bad
  entry_point: agent1
  bad: [unclosed
`;
    expect(() => loadConfigFromString(yaml, "bad.yaml")).toThrow(ConfigurationError);
  });

  it("throws ConfigurationError when entry_point doesn't exist", () => {
    const yaml = `
workflow:
  name: bad
  entry_point: does_not_exist
agents:
  - name: agent1
    prompt: Hello
    routes:
      - to: $end
`;
    expect(() => loadConfigFromString(yaml, "test.yaml")).toThrow(ConfigurationError);
  });

  it("throws ConfigurationError when route points to unknown agent", () => {
    const yaml = `
workflow:
  name: bad
  entry_point: agent1
agents:
  - name: agent1
    prompt: Hello
    routes:
      - to: unknown_agent
`;
    expect(() => loadConfigFromString(yaml, "test.yaml")).toThrow(ConfigurationError);
  });
});

describe("loadConfig - file-level errors", () => {
  it("throws ConfigurationError for a nonexistent file", () => {
    expect(() => loadConfig("/nonexistent/path/workflow.yaml")).toThrow(ConfigurationError);
  });

  it("throws ConfigurationError for an empty file", () => {
    const tmpFile = join(tmpdir(), "conductor-test-empty.yaml");
    writeFileSync(tmpFile, "");
    expect(() => loadConfig(tmpFile)).toThrow(ConfigurationError);
  });
});
