
# Conductor

A CLI tool for defining and running multi-agent workflows using the GitHub Copilot SDK.

## Overview

Conductor allows users to define agent workflows in YAML configuration files and execute them from the command line. Agents can be chained together, pass context between each other, use tools, and loop until conditions are met.

### Goals

- **Declarative workflows**: Define complex agent interactions in simple YAML
- **Context passing**: Agents can access outputs from previous agents
- **Conditional routing**: Route to different agents based on output conditions
- **Tool support**: Agents can use tools resolved by the Copilot SDK
- **Human-in-the-loop**: Optional gates for human approval
- **Easy distribution**: Install via `uvx conductor` or `pipx`
- **Provider abstraction**: Pluggable SDK backend (Copilot SDK default, others possible)

### Non-Goals

- GUI or web interface (CLI only)
- Agent training or fine-tuning
- Custom model hosting (uses SDK-provided models)

### Design Decisions

#### Why Python?

We chose Python over Go for the following reasons:

1. **SDK ecosystem alignment**: All major agent SDKs are Python-first:
   - OpenAI Agents SDK → Python only (JS coming)
   - Claude Agent SDK → Python only
   - LiteLLM → Python (or proxy)
   - PydanticAI → Python only
   - Copilot SDK → Available in Python, Go, TS, .NET

2. **Future optionality**: If we need to integrate other SDKs, Python is the only language where all options are available.

3. **Modern Python tooling**: With `uv`, `ruff`, and `ty`, Python development is now fast, type-safe, and ergonomic.

4. **Easy distribution**: `uvx conductor` provides a clean install experience without requiring a compiled binary.

**Trade-off accepted:** No single native binary (though Nuitka/PyInstaller are options if needed).

#### Why a Provider Abstraction?

Rather than hardcoding to a single SDK, we use a thin abstraction layer:

1. **Default to Copilot SDK**: Production-tested, handles agent loop, planning, and tools.

2. **Pluggable architecture**: Other providers (OpenAI Agents SDK, Claude SDK) can be added without rewriting the orchestration layer.

3. **Workflow portability**: Same YAML workflow can run on different backends via configuration.

4. **Adoption story**: "Works with Copilot today, could support others tomorrow" reduces friction.

**Current provider support:**
- ✅ `copilot` - GitHub Copilot SDK (default, implemented first)
- 🔮 `openai-agents` - OpenAI Agents SDK (future, if needed)
- 🔮 `claude` - Claude Agent SDK (future, if needed)
- 🔮 `litellm` - LiteLLM with custom orchestration (future, if needed)

**Implementation approach:** Start with Copilot SDK only. The abstraction layer exists to preserve optionality, but we won't invest in other providers until there's a concrete need.

---

## Configuration Schema

### Minimal Example

```yaml
workflow:
  name: fact-checked-answer
  entry_point: answerer

agents:
  - name: answerer
    model: claude-sonnet-4
    prompt: |
      Answer this question: {{ workflow.input.question }}
    output:
      answer: { type: string }
      claims: { type: array }
    routes:
      - to: fact_checker

  - name: fact_checker
    model: claude-sonnet-4
    input:
      - answerer.output
    prompt: |
      Verify these claims: {{ answerer.output.claims | json }}
    output:
      all_correct: { type: boolean }
      corrections: { type: array }
    routes:
      - to: $end
        when: "{{ output.all_correct }}"
      - to: answerer
        when: "{{ not output.all_correct }}"

output:
  answer: "{{ answerer.output.answer }}"
  verified: "{{ fact_checker.output.all_correct }}"
```

### Full Schema

```yaml
# ============================================
# WORKFLOW DEFINITION
# ============================================
workflow:
  name: string                    # Required: workflow identifier
  description: string             # Optional: human-readable description
  version: string                 # Optional: semver version
  
  entry_point: string             # Required: name of first agent to run
  
  # Workflow-level inputs (injected at runtime via CLI flags)
  input:
    <param_name>:
      type: string | number | boolean | array | object
      required: boolean
      default: any
      description: string
  
  # Context accumulation settings
  context:
    mode: accumulate | last_only | explicit   # Default: accumulate
    max_tokens: number                         # Trim when exceeded
    trim_strategy: summarize | truncate | drop_oldest
  
  # Loop/iteration controls
  limits:
    max_iterations: number        # Default: 10
    timeout_seconds: number       # Default: 600
  
  # Lifecycle hooks (optional)
  hooks:
    on_start: string              # Expression to evaluate
    on_complete: string
    on_error: string

# ============================================
# TOOLS (Optional)
# ============================================
tools:
  - string                        # Tool names available to agents
                                  # SDK resolves these to implementations

# ============================================
# AGENTS
# ============================================
agents:
  - name: string                  # Required: unique identifier
    description: string           # Optional: human-readable description
    model: string                 # Required: model identifier (supports ${ENV:-default})
    
    # What this agent reads from context
    input:
      - string                    # References like: workflow.input.goal, other_agent.output
                                  # Suffix with ? for optional: reviewer.feedback?
    
    # Tools available to this agent
    tools:
      - string                    # Subset of workflow tools
                                  # Omit = all tools, [] = no tools
    
    # System prompt (always included)
    system_prompt: string
    
    # User prompt template (supports Jinja2-style templating)
    prompt: string
    
    # Structured output schema (validated)
    output:
      <field_name>:
        type: string | number | boolean | array | object
        description: string
        # Additional JSON Schema properties supported
    
    # Routing rules (evaluated in order, first match wins)
    routes:
      - to: string                # Agent name, $end, or human gate name
        when: string              # Optional: condition expression
        output: object            # Optional: transform output before routing

  # Special agent type: human approval gate
  - name: string
    type: human_gate
    prompt: string                # Displayed to user
    options:
      - label: string
        value: string
        route: string             # Where to go if selected
        prompt_for: string        # Optional: ask for text input

# ============================================
# WORKFLOW OUTPUT
# ============================================
output:
  <field_name>: string            # Template expressions for final output
```

### Template Expressions

Templates use Jinja2-style syntax:

| Expression | Description |
|------------|-------------|
| `{{ workflow.input.goal }}` | Access workflow input |
| `{{ agent_name.output.field }}` | Access agent output |
| `{{ output.field }}` | Current agent's output (in routes) |
| `{{ context.iteration }}` | Current loop iteration |
| `{{ value \| json }}` | Format as JSON |
| `{% if condition %}...{% endif %}` | Conditional blocks |
| `{% for item in list %}...{% endfor %}` | Loops |

### Routing

Routes are evaluated in order. First matching `when` condition wins.

```yaml
routes:
  - to: reviewer                              # No condition = always matches
  
  - to: planner
    when: "{{ not output.approved }}"         # Condition must be true
    
  - to: $end                                  # Special: terminates workflow
    when: "{{ output.approved }}"
    output:                                   # Transform final output
      status: approved
      result: "{{ planner.output }}"
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         CLI (Typer)                              │
│  conductor run <workflow.yaml> --input.goal="..."               │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Config Loader                               │
│  - Parse YAML (ruamel.yaml)                                      │
│  - Validate schema (Pydantic)                                    │
│  - Resolve environment variables                                 │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Workflow Engine                              │
│  - Manage execution state                                        │
│  - Track context/history                                         │
│  - Evaluate routing conditions                                   │
│  - Enforce limits (iterations, timeout)                          │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Provider Abstraction                           │
│  - AgentProvider protocol (ABC)                                  │
│  - Provider factory based on config                              │
│  - Normalize inputs/outputs across SDKs                          │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                    SDK Implementations                           │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐              │
│  │   Copilot   │  │   OpenAI    │  │   Claude    │  (future)    │
│  │   Provider  │  │   Provider  │  │   Provider  │              │
│  └─────────────┘  └─────────────┘  └─────────────┘              │
└─────────────────────────────────────────────────────────────────┘
```

---

## Project Structure

```
conductor/
├── src/
│   └── conductor/
│       ├── __init__.py
│       ├── __main__.py          # Entry point (python -m conductor)
│       ├── cli/
│       │   ├── __init__.py
│       │   ├── app.py           # Typer CLI app
│       │   ├── run.py           # run command
│       │   ├── validate.py      # validate command
│       │   └── init.py          # init command (scaffold workflow)
│       ├── config/
│       │   ├── __init__.py
│       │   ├── loader.py        # YAML parsing
│       │   ├── schema.py        # Pydantic models for config
│       │   └── validator.py     # Schema validation
│       ├── engine/
│       │   ├── __init__.py
│       │   ├── workflow.py      # Workflow execution loop
│       │   ├── context.py       # Context accumulation
│       │   ├── router.py        # Route evaluation
│       │   └── limits.py        # Iteration/timeout enforcement
│       ├── providers/
│       │   ├── __init__.py
│       │   ├── base.py          # AgentProvider protocol (ABC)
│       │   ├── factory.py       # Provider factory
│       │   ├── copilot.py       # GitHub Copilot SDK provider
│       │   ├── openai_agents.py # OpenAI Agents SDK provider (future)
│       │   └── claude.py        # Claude Agent SDK provider (future)
│       ├── executor/
│       │   ├── __init__.py
│       │   ├── agent.py         # Single agent execution
│       │   ├── template.py      # Jinja2 prompt rendering
│       │   └── output.py        # Output parsing/validation
│       └── gates/
│           ├── __init__.py
│           └── human.py         # Human-in-the-loop prompts
├── tests/
│   ├── __init__.py
│   ├── test_config.py
│   ├── test_engine.py
│   └── test_providers.py
├── examples/
│   ├── simple-qa.yaml
│   ├── design-review.yaml
│   └── research-assistant.yaml
├── pyproject.toml               # Project config (uv, ruff, ty)
├── uv.lock                      # Lockfile
└── README.md
```

---

## Core Components

### 1. Config Schema (Pydantic Models)

```python
# src/conductor/config/schema.py

from typing import Any, Literal
from pydantic import BaseModel, Field


class InputDef(BaseModel):
    type: Literal["string", "number", "boolean", "array", "object"]
    required: bool = True
    default: Any = None
    description: str | None = None


class OutputField(BaseModel):
    type: Literal["string", "number", "boolean", "array", "object"]
    description: str | None = None


class RouteDef(BaseModel):
    to: str
    when: str | None = None
    output: dict[str, str] | None = None


class GateOption(BaseModel):
    label: str
    value: str
    route: str
    prompt_for: str | None = None


class ContextConfig(BaseModel):
    mode: Literal["accumulate", "last_only", "explicit"] = "accumulate"
    max_tokens: int | None = None
    trim_strategy: Literal["summarize", "truncate", "drop_oldest"] | None = None


class LimitsConfig(BaseModel):
    max_iterations: int = 10
    timeout_seconds: int = 600


class HooksConfig(BaseModel):
    on_start: str | None = None
    on_complete: str | None = None
    on_error: str | None = None


class AgentDef(BaseModel):
    name: str
    description: str | None = None
    type: Literal["", "human_gate"] | None = None
    model: str | None = None
    input: list[str] = Field(default_factory=list)
    tools: list[str] | None = None
    system_prompt: str | None = None
    prompt: str
    output: dict[str, OutputField] | None = None
    routes: list[RouteDef] = Field(default_factory=list)
    options: list[GateOption] | None = None  # for human_gate


class RuntimeConfig(BaseModel):
    """Provider configuration for the workflow."""
    provider: Literal["copilot", "openai-agents", "claude"] = "copilot"
    default_model: str | None = None


class WorkflowDef(BaseModel):
    name: str
    description: str | None = None
    version: str | None = None
    entry_point: str
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    input: dict[str, InputDef] = Field(default_factory=dict)
    context: ContextConfig = Field(default_factory=ContextConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    hooks: HooksConfig | None = None


class WorkflowConfig(BaseModel):
    workflow: WorkflowDef
    tools: list[str] = Field(default_factory=list)
    agents: list[AgentDef]
    output: dict[str, str] = Field(default_factory=dict)
```

### 2. Provider Abstraction

```python
# src/conductor/providers/base.py

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from conductor.config.schema import AgentDef


@dataclass
class AgentOutput:
    """Normalized output from any provider."""
    content: dict[str, Any]
    raw_response: Any  # Provider-specific response for debugging


class AgentProvider(ABC):
    """Abstract base class for SDK providers."""
    
    @abstractmethod
    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
    ) -> AgentOutput:
        """Execute an agent and return normalized output."""
        ...
    
    @abstractmethod
    async def close(self) -> None:
        """Clean up provider resources."""
        ...


# src/conductor/providers/copilot.py

from github_copilot_sdk import CopilotClient

from conductor.providers.base import AgentOutput, AgentProvider


class CopilotProvider(AgentProvider):
    """GitHub Copilot SDK provider."""
    
    def __init__(self) -> None:
        self.client = CopilotClient()
    
    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
    ) -> AgentOutput:
        response = await self.client.chat(
            model=agent.model,
            system_prompt=agent.system_prompt,
            prompt=rendered_prompt,
            tools=agent.tools,
            output_schema=agent.output,
        )
        
        return AgentOutput(
            content=self._parse_output(response),
            raw_response=response,
        )
    
    async def close(self) -> None:
        await self.client.close()


# src/conductor/providers/factory.py

from typing import Literal

from conductor.providers.base import AgentProvider
from conductor.providers.copilot import CopilotProvider


def create_provider(
    provider_type: Literal["copilot", "openai-agents", "claude"] = "copilot",
) -> AgentProvider:
    """Factory function to create the appropriate provider."""
    match provider_type:
        case "copilot":
            return CopilotProvider()
        case "openai-agents":
            raise NotImplementedError("OpenAI Agents provider not yet implemented")
        case "claude":
            raise NotImplementedError("Claude provider not yet implemented")
        case _:
            raise ValueError(f"Unknown provider: {provider_type}")
```

### 3. Workflow Engine

```python
# src/conductor/engine/workflow.py

from typing import Any

from conductor.config.schema import WorkflowConfig
from conductor.engine.context import WorkflowContext
from conductor.engine.router import Router
from conductor.executor.agent import AgentExecutor
from conductor.providers.base import AgentProvider


class MaxIterationsError(Exception):
    """Raised when workflow exceeds max iterations."""


class WorkflowEngine:
    def __init__(
        self,
        config: WorkflowConfig,
        provider: AgentProvider,
    ) -> None:
        self.config = config
        self.executor = AgentExecutor(provider)
        self.context = WorkflowContext()
        self.router = Router()
    
    async def run(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.context.set_workflow_inputs(inputs)
        current_agent = self.config.workflow.entry_point
        iteration = 0
        
        while True:
            # Check limits
            if iteration >= self.config.workflow.limits.max_iterations:
                raise MaxIterationsError(
                    f"Exceeded {self.config.workflow.limits.max_iterations} iterations"
                )
            
            # Get agent definition
            agent = self._find_agent(current_agent)
            if agent is None:
                raise ValueError(f"Agent not found: {current_agent}")
            
            # Handle human gates
            if agent.type == "human_gate":
                choice = await self._handle_human_gate(agent)
                current_agent = choice.route
                continue
            
            # Build context for this agent
            agent_ctx = self.context.build_for_agent(agent)
            
            # Execute agent
            output = await self.executor.execute(agent, agent_ctx)
            
            # Store output in context
            self.context.store(agent.name, output.content)
            
            # Evaluate routes
            next_agent, final_output = self.router.evaluate(
                agent.routes,
                output.content,
                self.context,
            )
            
            # Check for termination
            if next_agent == "$end":
                return self._build_result(final_output)
            
            current_agent = next_agent
            iteration += 1
    
    def _find_agent(self, name: str) -> AgentDef | None:
        return next((a for a in self.config.agents if a.name == name), None)
```

### 4. Template Renderer

```python
# src/conductor/executor/template.py

import json
from typing import Any

from jinja2 import Environment, BaseLoader, StrictUndefined


class TemplateRenderer:
    def __init__(self) -> None:
        self.env = Environment(
            loader=BaseLoader(),
            undefined=StrictUndefined,
            autoescape=False,
        )
        self.env.filters["json"] = lambda v: json.dumps(v, indent=2)
    
    def render(self, template: str, data: dict[str, Any]) -> str:
        tmpl = self.env.from_string(template)
        return tmpl.render(**data)
```

---

## CLI Interface

### Commands

```bash
# Run a workflow
conductor run <workflow.yaml> [flags]
  --input.<name>=<value>    Set workflow input
  --dry-run                 Show execution plan without running
  --verbose                 Show detailed output
  --skip-gates              Auto-approve human gates
  --timeout <duration>      Override workflow timeout

# Validate a workflow file
conductor validate <workflow.yaml>

# Initialize a new workflow from template
conductor init <name> [--template=<template>]

# List available templates
conductor templates
```

### Example Usage

```bash
# Install (one-time)
uvx conductor --help

# Or install globally
pipx install conductor

# Simple run
$ conductor run examples/design-review.yaml \
    --input.high_level_goal="Build a REST API for user auth"

# With environment variable for model
$ MODEL_PLANNING=claude-opus-4 conductor run workflow.yaml \
    --input.goal="Complex task"

# Dry run to see execution plan
$ conductor run workflow.yaml --dry-run \
    --input.question="What is the capital of France?"

# Output:
# Execution Plan:
#   1. answerer (claude-sonnet-4)
#   2. fact_checker (claude-sonnet-4)
#   3. → $end (if all_correct) OR → answerer (loop)

# Specify a different provider (future)
$ conductor run workflow.yaml --provider openai-agents \
    --input.question="Hello"
```

---

## Implementation Plan

### Phase 1: Core MVP
1. Config loader with YAML parsing and validation
2. Basic workflow engine (linear execution)
3. Agent executor with Copilot SDK integration
4. Simple template rendering
5. CLI with `run` command

### Phase 2: Routing & Loops
1. Conditional routing with expression evaluation
2. Loop detection and iteration limits
3. Context accumulation
4. `$end` termination handling

### Phase 3: Advanced Features
1. Human gates with interactive prompts
2. Tool support
3. Streaming output
4. `validate` and `init` commands

### Phase 4: Polish
1. Error handling and retries
2. Observability hooks
3. Context trimming/summarization
4. PyPI release and `uvx` distribution

---

## Dependencies

```toml
# pyproject.toml

[project]
name = "conductor"
version = "0.1.0"
description = "A CLI tool for defining and running multi-agent workflows"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "github-copilot-sdk>=0.1.0",  # Copilot SDK
    "typer>=0.12.0",              # CLI framework
    "rich>=13.0.0",               # Terminal formatting
    "pydantic>=2.0.0",            # Config validation
    "ruamel.yaml>=0.18.0",        # YAML parsing (preserves comments)
    "jinja2>=3.1.0",              # Template rendering
    "simpleeval>=1.0.0",          # Expression evaluation (safe)
]

[project.optional-dependencies]
# Future provider support
openai-agents = ["openai-agents>=0.1.0"]
claude = ["claude-agent-sdk>=0.1.0"]

[project.scripts]
conductor = "conductor.cli.app:app"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

# =============================================================================
# Tool Configuration
# =============================================================================

[tool.uv]
dev-dependencies = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.24.0",
    "pytest-cov>=5.0.0",
]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = [
    "E",    # pycodestyle errors
    "W",    # pycodestyle warnings
    "F",    # pyflakes
    "I",    # isort
    "B",    # flake8-bugbear
    "C4",   # flake8-comprehensions
    "UP",   # pyupgrade
    "SIM",  # flake8-simplify
]

[tool.ruff.format]
# Use ruff format instead of black (faster, compatible)
quote-style = "double"
indent-style = "space"

[tool.ty]
# ty - Astral's new type checker (faster than mypy)
python-version = "3.12"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

### Tooling Notes

| Tool | Purpose | Why |
|------|---------|-----|
| **uv** | Package/project management | 10-100x faster than pip, replaces pip/venv/pip-tools |
| **ruff** | Linting + formatting | Replaces flake8, isort, black - 10-100x faster |
| **ty** | Type checking | Astral's new type checker, faster than mypy |
| **pytest** | Testing | Standard, async support via pytest-asyncio |

### Development Workflow

```bash
# Setup
uv sync                          # Install dependencies

# Development
uv run conductor run workflow.yaml   # Run the CLI
uv run pytest                    # Run tests

# Quality checks
uv run ruff check .              # Lint
uv run ruff format .             # Format
uv run ty check                  # Type check

# Distribution
uv build                         # Build wheel
uv publish                       # Publish to PyPI

# Users install via:
uvx conductor run workflow.yaml
# or
pipx install conductor
```

---

## Example Workflows

### 1. Simple Q&A with Fact-Check

```yaml
workflow:
  name: fact-checked-answer
  entry_point: answerer
  
  # Provider configuration (optional - defaults to copilot)
  runtime:
    provider: copilot
    default_model: claude-sonnet-4

agents:
  - name: answerer
    model: claude-sonnet-4
    prompt: |
      Answer this question: {{ workflow.input.question }}
    output:
      answer: { type: string }
      claims: { type: array }
    routes:
      - to: fact_checker

  - name: fact_checker
    model: claude-sonnet-4
    input: [answerer.output]
    prompt: |
      Verify these claims: {{ answerer.output.claims | json }}
    output:
      all_correct: { type: boolean }
      corrections: { type: array }
    routes:
      - to: $end
        when: "{{ output.all_correct }}"
      - to: answerer

output:
  answer: "{{ answerer.output.answer }}"
  verified: "{{ fact_checker.output.all_correct }}"
```

### 2. Research Assistant with Tools

```yaml
workflow:
  name: research-assistant
  entry_point: planner
  runtime:
    provider: copilot

tools:
  - web_search
  - scrape_url

agents:
  - name: planner
    model: claude-sonnet-4
    tools: []
    prompt: |
      Create a research plan for: {{ workflow.input.question }}
    output:
      searches: { type: array }
    routes:
      - to: researcher

  - name: researcher
    model: claude-sonnet-4
    input: [planner.output.searches]
    prompt: |
      Execute these searches: {{ planner.output.searches | json }}
    output:
      findings: { type: array }
      sources: { type: array }
    routes:
      - to: synthesizer

  - name: synthesizer
    model: claude-sonnet-4
    tools: []
    input: [workflow.input.question, researcher.output]
    prompt: |
      Answer: {{ workflow.input.question }}
      Using: {{ researcher.output.findings | json }}
    output:
      answer: { type: string }
      confidence: { type: string }
    routes:
      - to: $end

output:
  answer: "{{ synthesizer.output.answer }}"
  sources: "{{ researcher.output.sources }}"
```

### 3. Design Review Loop with Human Approval

```yaml
workflow:
  name: design-review
  entry_point: planner
  limits:
    max_iterations: 5

agents:
  - name: planner
    model: claude-sonnet-4
    input:
      - workflow.input.goal
      - reviewer.feedback?
    prompt: |
      {% if reviewer.feedback %}
      Revise based on: {{ reviewer.feedback | json }}
      {% endif %}
      
      Goal: {{ workflow.input.goal }}
    output:
      design: { type: object }
      tasks: { type: array }
    routes:
      - to: reviewer

  - name: reviewer
    model: claude-sonnet-4
    input: [planner.output, workflow.input.goal]
    prompt: |
      Review this design for: {{ workflow.input.goal }}
      {{ planner.output | json }}
    output:
      approved: { type: boolean }
      score: { type: integer }
      feedback: { type: object }
    routes:
      - to: approval
        when: "{{ output.approved }}"
      - to: planner

  - name: approval
    type: human_gate
    prompt: |
      Design scored {{ reviewer.output.score }}/10
      Approve?
    options:
      - label: Approve
        value: approved
        route: $end
      - label: Request Changes
        value: changes
        route: planner
        prompt_for: feedback

output:
  design: "{{ planner.output.design }}"
  tasks: "{{ planner.output.tasks }}"
```