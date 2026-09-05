# OpenTelemetry Tracing

Conductor supports OpenTelemetry (OTel) tracing to help you monitor, debug, and optimize multi-agent workflows. When tracing is enabled, Conductor tracks execution flow and performance across workflows, agents, parallel groups, for-each loops, and tools.

## Overview

Tracing runs on an event-driven system. As steps execute, Conductor emits telemetry spans representing the active phase of the run. This includes:

* Workflow runs (`invoke_workflow`)
* Agent execution loops (`invoke_agent`)
* Dynamic or static parallel group execution
* Sub-workflow invocations
* Tool calls (`execute_tool`)
* Human-in-the-loop gates

For agents running on `copilot`, `claude`, or `openai` providers, Conductor activates native provider-level instrumentation. This generates detailed spans for LLM requests, token counts, and API calls.

## Quickstart

Follow these steps to trace a workflow execution using a local Jaeger collector.

### 1. Install Tracing Dependencies

Tracing is optional. Install the `telemetry` extra to add OpenTelemetry SDK and exporters to your environment:

```bash
uv sync --extra telemetry
```

### 2. Start a Collector

Run Jaeger in a Docker container to collect and visualize your traces:

```bash
docker run --rm -d \
  -p 4317:4317 \
  -p 4318:4318 \
  -p 16686:16686 \
  jaegertracing/all-in-one:latest
```

This starts the OTLP receiver (gRPC on port 4317, HTTP on port 4318) and the Jaeger Web UI on port 16686.

### 3. Configure Tracing

Set the endpoint environment variable to enable tracing. You can set `OTEL_SERVICE_NAME`
to name the service in your tracing backend:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
export OTEL_SERVICE_NAME=greeting-workflow
```

### 4. Run the Workflow

```bash
conductor run workflow.yaml
```

Open `http://localhost:16686` in your browser, select your service name, and click "Find Traces" to inspect the workflow execution.

## Configuration

Telemetry is configured exclusively with standard OTel environment variables.
Set `OTEL_EXPORTER_OTLP_ENDPOINT` to enable tracing; leave it unset to run
without tracing. Configure connection endpoints, export protocols, and privacy
policies using these environment variables:

| Variable | Description |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | The OTLP collector base URL (e.g. `http://localhost:4317`). Setting this enables tracing. |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | The export protocol to use. Accepts `grpc` (default), `http/protobuf`, or `http/json` (supported by Copilot CLI). |
| `OTEL_SERVICE_NAME` | The service name reported to the collector. |
| `OTEL_RESOURCE_ATTRIBUTES` | Key-value pairs for resource attributes (e.g. `deployment.environment=testing`). |
| `OTEL_SDK_DISABLED` | Set to `true` to disable all tracing in the process. |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | Controls prompt and response content capture. Set to `true`, `SPAN_ONLY`, or `SPAN_AND_EVENT` to capture content. |

> **Warning on Profile-Level Configuration:**
> Setting `OTEL_EXPORTER_OTLP_ENDPOINT` in your shell profile (such as `~/.bashrc` or `~/.zshrc`) enables tracing on every single Conductor run in that environment. This can add unnecessary execution overhead and send unwanted traces to your collector. It's usually better to set this variable only for active sessions or individual command runs.

### Service Name Precedence

Conductor uses `OTEL_SERVICE_NAME` when it is set; otherwise, the service name
is `"conductor"`.

## Unified Traces

For `copilot`, `claude`, and `openai` providers, Conductor automatically registers native OpenTelemetry instrumentation. Conductor unifies high-level orchestration spans and low-level LLM call spans into a single unified trace tree, instead of generating separate, disconnected trace trees.

### Parenting and Trace Structure

- **Orchestration Spans as Parents:** The high-level Conductor orchestration spans, such as `invoke_agent` and `execute_tool`, act as parent spans.
- **Provider Spans as Children:** Native provider spans, including LLM API calls and token counts, are nested directly under the corresponding Conductor spans. They share the same `trace_id` and trace context.
- **Correlation Identifier:** Conductor propagates the workflow `run_id` as the `gen_ai.conversation.id` attribute across all spans in the tree.

This single-tree structure allows you to drill down from high-level agent routing directly into the underlying model requests and tools in a single visualization.

### Copilot Provider

When using the `copilot` provider, Conductor captures native spans directly from the Copilot CLI child process. The generated span hierarchy follows the execution path from agent invocation to tool execution, nesting `chat` and `execute_tool` (with tool arguments and results) directly inside the Conductor `invoke_agent` span. Parenting is established automatically via W3C trace-context propagation over environment variables.

To capture native spans from the Copilot CLI, you must configure the exporter to use an HTTP-based OTLP protocol. Standard gRPC (the default protocol) is not supported by the Copilot CLI. Set `OTEL_EXPORTER_OTLP_PROTOCOL` to `http/protobuf` (recommended) or `http/json`. If you run with `grpc`, native Copilot spans are disabled, and Conductor logs a warning once per run. Conductor's own exporter treats any non-grpc value as HTTP protobuf, which matches what the Copilot CLI expects.

When content capture is enabled, `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` values of `SPAN_ONLY` and `SPAN_AND_EVENT` collapse to plain content capture in the Copilot CLI spans.

If you connect to an external runtime via `runtime_url`, the operator must start that runtime with `COPILOT_OTEL_ENABLED=true` and `OTEL_EXPORTER_OTLP_ENDPOINT` set in its environment. Trace context still propagates automatically to the external process, ensuring spans parent correctly under Conductor.

### MCP Tool Spans

Conductor emits its own spans for MCP tool calls from the workflow's tool events. The `mcp` Python SDK can additionally export native spans starting with version `2.0.0`; Conductor currently depends on `mcp>=1.28.1,<2`, so those native spans become available only once that upgrade lands.

### Human-Gate Spans

When a workflow hits a human-in-the-loop gate, the active `invoke_agent` span for that gate agent ends immediately at presentation. Conductor then emits zero-duration event spans for `gate_presented` and `gate_resolved` to track the state transitions. This prevents the agent span from remaining active and showing a misleading duration while waiting for human input.

## Privacy and Content Capture

By default, Conductor does not record prompt text, system messages, or model responses in OTel spans. This protects sensitive data and keeps secrets out of your tracing backend.

Native spans for Pydantic AI (Claude, OpenAI) and Copilot CLI also exclude message content by default. If you need to inspect raw prompts and responses for debugging, set `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` to `true`, `SPAN_ONLY`, or `SPAN_AND_EVENT`.

Make sure your collector and its data retention policy comply with your security guidelines before enabling content capture.

> **Note on Event-Only Capture:** Pydantic AI does not support OTel event-based content capture. If you set the variable to `EVENT_ONLY`, the provider degrades to capturing no content for native spans.

## Experimental Conv Disclaimer

The attributes generated by Conductor tracing follow the experimental OpenTelemetry GenAI semantic conventions. These conventions are subject to change in future versions of the OpenTelemetry specification. Supplementary workflow metadata is exported using custom `conductor.*` attributes.
