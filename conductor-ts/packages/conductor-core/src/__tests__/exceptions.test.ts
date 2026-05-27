/**
 * Ports tests/test_exceptions.py to TypeScript / vitest.
 */
import { describe, expect, it } from "vitest";
import {
  CheckpointError,
  ConductorError,
  ConfigurationError,
  ExecutionError,
  MaxIterationsError,
  ProviderError,
  RouteError,
  TemplateError,
  TimeoutError,
  ValidationError,
} from "../exceptions.js";

describe("ConductorError", () => {
  it("preserves basic error message", () => {
    const err = new ConductorError("Something went wrong");
    expect(err.message).toBe("Something went wrong");
  });

  it("has suggestion undefined when not provided", () => {
    const err = new ConductorError("Error");
    expect(err.suggestion).toBeUndefined();
  });

  it("stores suggestion when provided", () => {
    const err = new ConductorError("Error", { suggestion: "Try doing X instead" });
    expect(err.suggestion).toBe("Try doing X instead");
  });

  it("stores filePath when provided", () => {
    const err = new ConductorError("Error", { filePath: "/path/to/file.yaml" });
    expect(err.filePath).toBe("/path/to/file.yaml");
  });

  it("stores lineNumber when provided", () => {
    const err = new ConductorError("Error", { lineNumber: 42 });
    expect(err.lineNumber).toBe(42);
  });

  it("errorType returns the class name", () => {
    const err = new ConductorError("Test");
    expect(err.errorType).toBe("ConductorError");
  });

  it("is an instance of Error", () => {
    const err = new ConductorError("Test");
    expect(err).toBeInstanceOf(Error);
  });
});

describe("ConfigurationError", () => {
  it("inherits from ConductorError", () => {
    const err = new ConfigurationError("Bad config");
    expect(err).toBeInstanceOf(ConductorError);
    expect(err.message).toBe("Bad config");
  });

  it("errorType is ConfigurationError", () => {
    const err = new ConfigurationError("Test");
    expect(err.errorType).toBe("ConfigurationError");
  });

  it("accepts suggestion", () => {
    const err = new ConfigurationError("entry_point missing", {
      suggestion: "Check agent names",
    });
    expect(err.suggestion).toBe("Check agent names");
  });

  it("accepts filePath", () => {
    const err = new ConfigurationError("Error", { filePath: "/wf.yaml" });
    expect(err.filePath).toBe("/wf.yaml");
  });
});

describe("ValidationError", () => {
  it("inherits from ConductorError", () => {
    const err = new ValidationError("Invalid data");
    expect(err).toBeInstanceOf(ConductorError);
    expect(err.errorType).toBe("ValidationError");
  });
});

describe("TemplateError", () => {
  it("inherits from ConductorError", () => {
    const err = new TemplateError("Template syntax error");
    expect(err).toBeInstanceOf(ConductorError);
    expect(err.errorType).toBe("TemplateError");
  });
});

describe("ProviderError", () => {
  it("inherits from ConductorError", () => {
    const err = new ProviderError("Provider failed");
    expect(err).toBeInstanceOf(ConductorError);
    expect(err.errorType).toBe("ProviderError");
  });

  it("stores suggestion", () => {
    const err = new ProviderError("Auth failed", { suggestion: "Check API key" });
    expect(err.suggestion).toBe("Check API key");
  });
});

describe("ExecutionError", () => {
  it("inherits from ConductorError", () => {
    const err = new ExecutionError("Execution failed");
    expect(err).toBeInstanceOf(ConductorError);
    expect(err.errorType).toBe("ExecutionError");
  });
});

describe("MaxIterationsError", () => {
  it("inherits from ConductorError", () => {
    const err = new MaxIterationsError("Too many iterations");
    expect(err).toBeInstanceOf(ConductorError);
    expect(err.errorType).toBe("MaxIterationsError");
  });

  it("stores suggestion", () => {
    const err = new MaxIterationsError("Too many", {
      suggestion: "Increase max_iterations",
    });
    expect(err.suggestion).toBe("Increase max_iterations");
  });
});

describe("TimeoutError", () => {
  it("inherits from ConductorError", () => {
    const err = new TimeoutError("Workflow timed out");
    expect(err).toBeInstanceOf(ConductorError);
    expect(err.errorType).toBe("TimeoutError");
  });
});

describe("RouteError", () => {
  it("inherits from ConductorError", () => {
    const err = new RouteError("No matching route");
    expect(err).toBeInstanceOf(ConductorError);
    expect(err.errorType).toBe("RouteError");
  });
});

describe("CheckpointError", () => {
  it("inherits from ConductorError", () => {
    const err = new CheckpointError("Checkpoint failed");
    expect(err).toBeInstanceOf(ConductorError);
    expect(err.errorType).toBe("CheckpointError");
  });
});

describe("error name property", () => {
  it("name matches class name for all error types", () => {
    const cases: Array<[string, Error]> = [
      ["ConductorError", new ConductorError("x")],
      ["ConfigurationError", new ConfigurationError("x")],
      ["ValidationError", new ValidationError("x")],
      ["ExecutionError", new ExecutionError("x")],
      ["ProviderError", new ProviderError("x")],
      ["TemplateError", new TemplateError("x")],
      ["RouteError", new RouteError("x")],
      ["MaxIterationsError", new MaxIterationsError("x")],
      ["TimeoutError", new TimeoutError("x")],
      ["CheckpointError", new CheckpointError("x")],
    ];
    for (const [name, err] of cases) {
      expect(err.name).toBe(name);
    }
  });
});
