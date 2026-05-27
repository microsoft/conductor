/**
 * YAML configuration loader with environment variable resolution and !file tag support.
 * Mirrors src/conductor/config/loader.py
 */
import fs from "node:fs";
import path from "node:path";
import yaml from "js-yaml";
import { WorkflowConfigSchema, type WorkflowConfig } from "./schema.js";
import { validateConfig } from "./validator.js";
import { ConfigurationError } from "../exceptions.js";

// ${VAR} or ${VAR:-default}
const ENV_VAR_RE = /\$\{([^}:]+)(?::-((?:[^}]|\}(?!\}))*))?\}/g;

export function resolveEnvVars(value: string, maxDepth = 10): string {
  if (maxDepth <= 0) {
    throw new ConfigurationError(
      `Maximum recursion depth exceeded resolving env vars in: ${value}`,
    );
  }
  const result = value.replace(ENV_VAR_RE, (_match, name: string, def?: string) => {
    const env = process.env[name];
    if (env !== undefined) return env;
    if (def !== undefined) return def;
    throw new ConfigurationError(
      `Required environment variable '${name}' is not set`,
      { suggestion: `Set ${name} or provide a default: \${${name}:-default}` },
    );
  });
  // Re-run if new refs were introduced
  if (ENV_VAR_RE.test(result)) {
    ENV_VAR_RE.lastIndex = 0;
    return resolveEnvVars(result, maxDepth - 1);
  }
  ENV_VAR_RE.lastIndex = 0;
  return result;
}

function resolveEnvVarsDeep(data: unknown): unknown {
  if (typeof data === "string") return resolveEnvVars(data);
  if (Array.isArray(data)) return data.map(resolveEnvVarsDeep);
  if (data !== null && typeof data === "object") {
    return Object.fromEntries(
      Object.entries(data as Record<string, unknown>).map(([k, v]) => [
        k,
        resolveEnvVarsDeep(v),
      ]),
    );
  }
  return data;
}

function createFileTagType(baseDir: string, fileStack: string[] = []): yaml.Type {
  return new yaml.Type("!file", {
    kind: "scalar",
    construct(relPath: string): unknown {
      const absPath = path.resolve(baseDir, relPath);
      if (fileStack.includes(absPath)) {
        throw new ConfigurationError(
          `Circular !file reference: ${relPath}`,
          { suggestion: "Remove the circular !file reference." },
        );
      }
      let content: string;
      try {
        content = fs.readFileSync(absPath, "utf-8");
      } catch {
        throw new ConfigurationError(
          `File not found: '${relPath}' (resolved to '${absPath}')`,
          { suggestion: "Check the path is relative to the workflow YAML file." },
        );
      }
      // If the file looks like YAML, parse it recursively; otherwise return as string
      const trimmed = content.trimStart();
      if (trimmed.startsWith("{") || trimmed.startsWith("- ") || trimmed.includes(": ")) {
        try {
          const schema = yaml.DEFAULT_SCHEMA.extend([
            createFileTagType(path.dirname(absPath), [...fileStack, absPath]),
          ]);
          return yaml.load(content, { schema });
        } catch {
          // Not valid YAML — return raw string
        }
      }
      return content;
    },
  });
}

export function loadConfigFromString(yamlText: string, sourceFile = "<string>"): WorkflowConfig {
  const baseDir = sourceFile === "<string>" ? process.cwd() : path.dirname(path.resolve(sourceFile));
  const schema = yaml.DEFAULT_SCHEMA.extend([createFileTagType(baseDir)]);

  let raw: unknown;
  try {
    raw = yaml.load(yamlText, { schema });
  } catch (err) {
    throw new ConfigurationError(`Failed to parse YAML: ${String(err)}`, {
      filePath: sourceFile,
    });
  }

  const resolved = resolveEnvVarsDeep(raw);

  const parsed = WorkflowConfigSchema.safeParse(resolved);
  if (!parsed.success) {
    const issues = parsed.error.issues
      .map((i) => `  ${i.path.join(".")}: ${i.message}`)
      .join("\n");
    throw new ConfigurationError(`Workflow configuration invalid:\n${issues}`, {
      filePath: sourceFile,
    });
  }
  validateConfig(parsed.data, sourceFile === "<string>" ? undefined : sourceFile);
  return parsed.data;
}

export function loadConfig(filePath: string): WorkflowConfig {
  const abs = path.resolve(filePath);
  let text: string;
  try {
    text = fs.readFileSync(abs, "utf-8");
  } catch {
    throw new ConfigurationError(`Workflow file not found: '${filePath}'`, {
      filePath: abs,
    });
  }
  return loadConfigFromString(text, abs);
}
