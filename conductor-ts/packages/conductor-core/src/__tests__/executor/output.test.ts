/**
 * Ports tests/test_executor/test_output.py to TypeScript / vitest.
 *
 * The TypeScript output module uses Zod instead of a custom _check_type helper.
 * We test extractJson and parseOutput (the public API).
 */
import { describe, expect, it } from "vitest";
import { ValidationError } from "../../exceptions.js";
import { extractJson, parseOutput } from "../../executor/output.js";

describe("extractJson", () => {
  it("extracts JSON from a plain object string", () => {
    const result = extractJson('{"key": "value"}');
    expect(result).toBe('{"key": "value"}');
  });

  it("extracts JSON from a markdown json code fence", () => {
    const result = extractJson("```json\n{\"answer\": 42}\n```");
    expect(result).toBe('{"answer": 42}');
  });

  it("extracts JSON from a plain code fence", () => {
    const result = extractJson("```\n{\"answer\": 42}\n```");
    expect(result).toBe('{"answer": 42}');
  });

  it("extracts JSON when surrounded by extra text", () => {
    const result = extractJson('Here is the result: {"key": "value"} done');
    expect(JSON.parse(result)).toEqual({ key: "value" });
  });
});

describe("parseOutput - no schema", () => {
  it("parses plain JSON when no schema provided", () => {
    const result = parseOutput('{"answer": "hello"}', undefined, "agent1");
    expect(result).toEqual({ answer: "hello" });
  });

  it("returns { output: raw } when no schema and not JSON", () => {
    const result = parseOutput("plain text response", undefined, "agent1");
    expect(result).toEqual({ output: "plain text response" });
  });

  it("parses JSON from code fence when no schema", () => {
    const result = parseOutput("```json\n{\"x\": 1}\n```", undefined, "agent1");
    expect(result).toEqual({ x: 1 });
  });
});

describe("parseOutput - with schema", () => {
  it("parses and validates a valid string field", () => {
    const schema = { answer: { type: "string" as const } };
    const result = parseOutput('{"answer": "hello"}', schema, "agent1");
    expect(result).toEqual({ answer: "hello" });
  });

  it("parses and validates a valid number field", () => {
    const schema = { count: { type: "number" as const } };
    const result = parseOutput('{"count": 42}', schema, "agent1");
    expect(result).toEqual({ count: 42 });
  });

  it("parses and validates a valid boolean field", () => {
    const schema = { is_valid: { type: "boolean" as const } };
    const result = parseOutput('{"is_valid": true}', schema, "agent1");
    expect(result).toEqual({ is_valid: true });
  });

  it("parses and validates a valid array field", () => {
    const schema = { items: { type: "array" as const } };
    const result = parseOutput('{"items": [1, 2, 3]}', schema, "agent1");
    expect(result).toEqual({ items: [1, 2, 3] });
  });

  it("parses and validates a valid object field", () => {
    const schema = { data: { type: "object" as const } };
    const result = parseOutput('{"data": {"key": "value"}}', schema, "agent1");
    expect(result).toEqual({ data: { key: "value" } });
  });

  it("throws ValidationError for invalid JSON with schema", () => {
    const schema = { answer: { type: "string" as const } };
    expect(() => parseOutput("not json at all", schema, "agent1")).toThrow(ValidationError);
  });

  it("throws ValidationError when required field missing", () => {
    const schema = { answer: { type: "string" as const } };
    expect(() => parseOutput("{}", schema, "agent1")).toThrow(ValidationError);
  });

  it("throws ValidationError when field has wrong type", () => {
    const schema = { count: { type: "number" as const } };
    expect(() =>
      parseOutput('{"count": "not a number"}', schema, "agent1"),
    ).toThrow(ValidationError);
  });

  it("allows extra fields not in schema", () => {
    const schema = { required: { type: "string" as const } };
    // Extra fields are stripped by Zod passthrough or just allowed via z.object
    // (Zod strips extra fields by default but does not throw)
    const result = parseOutput(
      '{"required": "value", "extra": "ignored"}',
      schema,
      "agent1",
    );
    expect(result.required).toBe("value");
  });

  it("validates JSON from code fence with schema", () => {
    const schema = { answer: { type: "string" as const } };
    const result = parseOutput(
      '```json\n{"answer": "hello"}\n```',
      schema,
      "agent1",
    );
    expect(result).toEqual({ answer: "hello" });
  });

  it("booleans are not accepted as numbers", () => {
    const schema = { count: { type: "number" as const } };
    expect(() =>
      parseOutput('{"count": true}', schema, "agent1"),
    ).toThrow(ValidationError);
  });
});

describe("parseOutput - multiple fields", () => {
  it("validates multiple fields at once", () => {
    const schema = {
      name: { type: "string" as const },
      age: { type: "number" as const },
      active: { type: "boolean" as const },
    };
    const result = parseOutput(
      '{"name": "Alice", "age": 30, "active": true}',
      schema,
      "agent1",
    );
    expect(result).toEqual({ name: "Alice", age: 30, active: true });
  });
});
