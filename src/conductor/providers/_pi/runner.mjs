#!/usr/bin/env node
/** Conductor -> Pi SDK JSONL bridge. One request per process. */
import readline from "node:readline";
import {
  createAgentSession,
  ModelRuntime,
  SessionManager,
} from "@earendil-works/pi-coding-agent";

let activeSession;
let interrupted = false;

function emit(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

function textFromMessage(message) {
  if (!message || message.role !== "assistant") return "";
  if (typeof message.content === "string") return message.content;
  if (!Array.isArray(message.content)) return "";
  return message.content.filter((part) => part.type === "text").map((part) => part.text).join("");
}

function usageFromMessage(message) {
  const usage = message?.usage;
  if (!usage) return {};
  return {
    input_tokens: usage.input ?? 0,
    output_tokens: usage.output ?? 0,
    cache_read_tokens: usage.cacheRead ?? 0,
    cache_write_tokens: usage.cacheWrite ?? 0,
    tokens_used: usage.totalTokens ?? 0,
  };
}

function addUsage(total, message) {
  const usage = usageFromMessage(message);
  for (const key of Object.keys(total)) total[key] += usage[key] ?? 0;
}

function parseJson(text) {
  const fenced = [...text.matchAll(/```(?:json)?\s*\n?([\s\S]*?)\n?```/gi)];
  const candidates = fenced.map((match) => match[1]).concat([text]);
  for (const candidate of candidates) {
    const trimmed = candidate.trim();
    const start = trimmed.search(/[\[{]/);
    if (start < 0) continue;
    try {
      const parsed = JSON.parse(trimmed.slice(start));
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed;
      throw new Error("JSON response must be an object");
    } catch (_) {
      // Try next candidate. Final parse error below stays intentionally concise.
    }
  }
  throw new Error("Response was not a JSON object");
}

function validateValue(value, field, path) {
  const type = field.type;
  const valid = (
    (type === "string" && typeof value === "string") ||
    (type === "number" && typeof value === "number" && !Number.isNaN(value)) ||
    (type === "integer" && Number.isInteger(value)) ||
    (type === "boolean" && typeof value === "boolean") ||
    (type === "array" && Array.isArray(value)) ||
    (type === "object" && value && typeof value === "object" && !Array.isArray(value))
  );
  if (!valid) throw new Error(`${path} must be ${type}`);
  if (type === "object" && field.properties) validateSchema(value, field.properties, `${path}.`);
  if (type === "array" && field.items) value.forEach((item, index) => validateValue(item, field.items, `${path}[${index}]`));
}

function validateSchema(content, schema, prefix = "") {
  for (const [name, field] of Object.entries(schema)) {
    if (!(name in content)) throw new Error(`Missing required output field: ${prefix}${name}`);
    validateValue(content[name], field, `${prefix}${name}`);
  }
}

/** Provider-registering extensions load with the session, so select the model afterwards. */
async function selectModel(session, modelRuntime, requested) {
  if (!requested) return;
  const [provider, ...id] = requested.split("/");
  if (!provider || id.length === 0) throw new Error(`Pi model must be provider/id, got ${JSON.stringify(requested)}`);
  const model = modelRuntime.getModel(provider, id.join("/"));
  if (!model) throw new Error(`Pi model unavailable: ${requested}`);
  await session.setModel(model);
}

async function listModels() {
  const runtime = await ModelRuntime.create();
  const { session } = await createAgentSession({
    modelRuntime: runtime,
    sessionManager: SessionManager.inMemory(),
  });
  try {
    const models = await runtime.getAvailable();
    emit({ type: "models", models: models.map((model) => `${model.provider}/${model.id}`) });
  } finally {
    session.dispose();
  }
}

async function run(request) {
  if (request.action === "list_models") return listModels();

  const modelRuntime = await ModelRuntime.create();
  const manager = request.session_path
    ? SessionManager.open(request.session_path, undefined, request.cwd)
    : SessionManager.create(request.cwd);
  const { session } = await createAgentSession({
    cwd: request.cwd,
    modelRuntime,
    thinkingLevel: request.thinking ?? undefined,
    tools: request.tools ?? undefined,
    sessionManager: manager,
  });
  activeSession = session;
  try {
    await selectModel(session, modelRuntime, request.model);
  } catch (error) {
    session.dispose();
    activeSession = undefined;
    throw error;
  }
  emit({ type: "session", path: session.sessionFile, id: session.sessionId });

  const usage = { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0, tokens_used: 0 };
  let lastAssistant;
  let turnCount = 0;
  let limitError;
  const unsubscribe = session.subscribe((event) => {
    if (event.type === "message_update") {
      const update = event.assistantMessageEvent;
      if (update.type === "text_delta") emit({ type: "text", delta: update.delta });
      if (update.type === "thinking_delta") emit({ type: "reasoning", delta: update.delta });
    }
    if (event.type === "tool_execution_start") emit({ type: "tool_start", name: event.toolName, args: event.args });
    if (event.type === "tool_execution_end") emit({ type: "tool_complete", name: event.toolName, is_error: event.isError });
    if (event.type === "turn_end") {
      lastAssistant = event.message;
      addUsage(usage, event.message);
      turnCount += 1;
      if (request.max_agent_iterations && turnCount >= request.max_agent_iterations) {
        limitError = `Pi agent exceeded max_agent_iterations (${request.max_agent_iterations}).`;
        void session.abort();
      }
    }
  });

  try {
    let text;
    let content;
    const hasSchema = request.output_schema && Object.keys(request.output_schema).length > 0;
    const maxAttempts = request.max_parse_recovery_attempts ?? 2;
    for (let attempt = 0; attempt <= maxAttempts; attempt += 1) {
      await session.prompt(attempt === 0 ? request.prompt : [
        "Your previous answer did not meet required JSON output.",
        `Error: ${request.recovery_error}`,
        `Return only one JSON object matching this schema: ${JSON.stringify(request.output_schema)}`,
      ].join("\n"));
      lastAssistant ??= [...session.messages].reverse().find((message) => message.role === "assistant");
      text = textFromMessage(lastAssistant);
      if (!hasSchema || interrupted || limitError) break;
      try {
        content = parseJson(text);
        validateSchema(content, request.output_schema);
        break;
      } catch (error) {
        request.recovery_error = error instanceof Error ? error.message : String(error);
        emit({ type: "parse_recovery", attempt: attempt + 1, max_attempts: maxAttempts, error: request.recovery_error });
        if (attempt === maxAttempts) {
          throw new Error(`SCHEMA:${request.recovery_error}`);
        }
      }
    }
    if (limitError) throw new Error(limitError);
    emit({
      type: "result",
      text: text ?? "",
      content: content ?? null,
      partial: interrupted,
      model: lastAssistant?.model ?? session.model?.id ?? null,
      session_path: session.sessionFile,
      ...usage,
    });
  } finally {
    unsubscribe();
    session.dispose();
    activeSession = undefined;
  }
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    interrupted = true;
    if (activeSession) void activeSession.abort();
  });
}

const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
let request;
for await (const line of lines) {
  if (line.trim()) { request = JSON.parse(line); break; }
}
if (!request) {
  emit({ type: "error", error: "Expected one JSON request on stdin." });
  process.exitCode = 2;
} else {
  try {
    await run(request);
  } catch (error) {
    emit({ type: "error", error: error instanceof Error ? error.message : String(error) });
    process.exitCode = 1;
  }
}
