/**
 * Output parsing and validation.
 * Mirrors src/conductor/executor/output.py
 */
import { z } from "zod";
import type { OutputField } from "../config/schema.js";
import { ValidationError } from "../exceptions.js";

/** Extract JSON from a string (handles markdown code fences). */
export function extractJson(text: string): string {
  const fenceMatch = text.match(/```(?:json)?\s*([\s\S]*?)```/);
  if (fenceMatch?.[1]) return fenceMatch[1].trim();
  const firstBrace = text.indexOf("{");
  const lastBrace = text.lastIndexOf("}");
  if (firstBrace !== -1 && lastBrace > firstBrace) {
    return text.slice(firstBrace, lastBrace + 1);
  }
  return text.trim();
}

/** Parse and validate agent output against a declared schema. */
export function parseOutput(
  raw: string,
  schema: Record<string, OutputField> | undefined,
  agentName: string,
): Record<string, unknown> {
  if (!schema) {
    // No schema — try JSON parse, fall back to { output: raw }
    try {
      return JSON.parse(extractJson(raw)) as Record<string, unknown>;
    } catch {
      return { output: raw };
    }
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(extractJson(raw));
  } catch (err) {
    throw new ValidationError(
      `Agent '${agentName}' output is not valid JSON: ${String(err)}`,
      { suggestion: "Ensure the agent returns a JSON object matching the output schema." },
    );
  }

  // Build a Zod schema from the OutputField definitions
  const zodSchema = buildZodSchema(schema);
  const result = zodSchema.safeParse(parsed);
  if (!result.success) {
    const issues = result.error.issues
      .map((i) => `  ${i.path.join(".")}: ${i.message}`)
      .join("\n");
    throw new ValidationError(
      `Agent '${agentName}' output validation failed:\n${issues}`,
    );
  }
  return result.data as Record<string, unknown>;
}

function buildZodSchema(fields: Record<string, OutputField>): z.ZodObject<z.ZodRawShape> {
  const shape: z.ZodRawShape = {};
  for (const [key, field] of Object.entries(fields)) {
    shape[key] = outputFieldToZod(field);
  }
  return z.object(shape);
}

function outputFieldToZod(field: OutputField): z.ZodTypeAny {
  switch (field.type) {
    case "string":
      return z.string();
    case "number":
      return z.number();
    case "boolean":
      return z.boolean();
    case "array": {
      const items = field.items ? outputFieldToZod(field.items) : z.unknown();
      return z.array(items);
    }
    case "object": {
      if (field.properties) {
        const shape: z.ZodRawShape = {};
        for (const [k, v] of Object.entries(field.properties)) {
          shape[k] = outputFieldToZod(v);
        }
        return z.object(shape);
      }
      return z.record(z.unknown());
    }
    default:
      return z.unknown();
  }
}
