# Conductor

A CLI tool for defining and running multi-agent workflows with the GitHub Copilot SDK and Anthropic Claude.

[![CI](https://github.com/microsoft/conductor/actions/workflows/ci.yml/badge.svg)](https://github.com/microsoft/conductor/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

## Why Conductor?

A single LLM prompt can answer a question, but it can't review its own work, research from multiple angles, or pause for human approval. You need multi-agent workflows—but building them means coding custom solutions, managing state, handling failures, and hoping you don't create infinite loops.

Conductor provides the patterns that work: evaluator-optimizer loops for iterative refinement, parallel execution with failure modes, and human-in-the-loop gates. Define them in YAML with built-in safety limits. Version control your workflows like code.

## Features

- **YAML-based workflows** - Define multi-agent workflows in readable YAML
- **Multiple providers** - GitHub Copilot or Anthropic Claude with seamless switching
- **Parallel execution** - Run agents concurrently (static groups or dynamic for-each)
- **Script steps** - Run shell commands and route on exit code without an AI agent
- **Conditional routing** - Route between agents based on output conditions
- **Human-in-the-loop** - Pause for human decisions with Rich terminal UI
- **Safety limits** - Max iterations and timeout enforcement
- **Validation** - Validate workflows before execution

## Installation

### Using uv (Recommended)

```bash
# Install from GitHub
uv tool install git+https://github.com/microsoft/conductor.git

# Run the CLI
conductor run workflow.yaml

# Or run directly without installing
uvx --from git+https://github.com/microsoft/conductor.git conductor run workflow.yaml

# Install a specific branch, tag, or commit
uv tool install git+https://github.com/microsoft/conductor.git@branch-name
uv tool install git+https://github.com/microsoft/conductor.git@v1.0.0
uv tool install git+https://github.com/microsoft/conductor.git@abc1234
```

### Using pipx

```bash
pipx install git+https://github.com/microsoft/conductor.git
conductor run workflow.yaml

# Install a specific branch or tag
pipx install git+https://github.com/microsoft/conductor.git@branch-name
```

### Using pip

```bash
pip install git+https://github.com/microsoft/conductor.git
conductor run workflow.yaml

# Install a specific tag or commit
pip install git+https://github.com/microsoft/conductor.git@v1.0.0
```

## Quick Start

### 1. Create a workflow file

```yaml
# my-workflow.yaml
workflow:
  name: simple-qa
  description: A simple question-answering workflow
  entry_point: answerer

agents:
  - name: answerer
    model: gpt-5.2
    prompt: |
      Answer the following question:
      {{ workflow.input.question }}
    output:
      answer:
        type: string
    routes:
      - to: $end

output:
  answer: "{{ answerer.output.answer }}"
```

### 2. Run the workflow

```bash
conductor run my-workflow.yaml --input question="What is Python?"
```

### 3. View the output

```json
{
  "answer": "Python is a high-level, interpreted programming language..."
}
```

## Providers

Conductor supports multiple AI providers. Choose based on your needs:

| Feature | Copilot | Claude |
|---------|---------|--------|
| **Pricing** | Subscription ($10-39/mo) | Pay-per-token |
| **Context Window** | 8K-128K tokens | 200K tokens |
| **Tool Support (MCP)** | Yes | Planned |
| **Streaming** | Yes | Planned |
| **Best For** | Heavy usage, tools | Large context, pay-per-use |

### Using Claude

```yaml
workflow:
  runtime:
    provider: claude
    default_model: claude-sonnet-4.5
```

Set your API key: `export ANTHROPIC_API_KEY=sk-ant-...`

**See also:** [Claude Documentation](docs/providers/claude.md) | [Provider Comparison](docs/providers/comparison.md) | [Migration Guide](docs/providers/migration.md)

## CLI Reference

### `conductor run`

Execute a workflow from a YAML file.

```bash
conductor run <workflow.yaml> [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `-i, --input NAME=VALUE` | Workflow input (repeatable) |
| `-p, --provider PROVIDER` | Override provider |
| `--dry-run` | Preview execution plan |
| `--skip-gates` | Auto-select at human gates |
| `-q, --quiet` | Suppress progress output |
| `-s, --silent` | Suppress all output except errors |
| `-l, --log-file PATH` | Write logs to file |

### `conductor validate`

Validate a workflow file without executing.

```bash
conductor validate <workflow.yaml>
```

### `conductor init`

Create a new workflow from a template.

```bash
conductor init <name> --template <template> --output <path>
```

### `conductor templates`

List available workflow templates.

```bash
conductor templates
```

**Full CLI documentation:** [docs/cli-reference.md](docs/cli-reference.md)

## Examples

See the [`examples/`](./examples/) directory for complete workflows:

| Example | Description |
|---------|-------------|
| [simple-qa.yaml](./examples/simple-qa.yaml) | Basic single-agent Q&A |
| [for-each-simple.yaml](./examples/for-each-simple.yaml) | Dynamic parallel processing |
| [parallel-research.yaml](./examples/parallel-research.yaml) | Static parallel execution |
| [design-review.yaml](./examples/design-review.yaml) | Human gate with loop pattern |
| [script-step.yaml](./examples/script-step.yaml) | Script step with exit_code routing |

**More examples and running instructions:** [examples/README.md](./examples/README.md)

## Documentation

| Document | Description |
|----------|-------------|
| [Workflow Syntax](./docs/workflow-syntax.md) | Complete YAML schema reference |
| [CLI Reference](./docs/cli-reference.md) | Full command-line documentation |
| [Parallel Execution](./docs/parallel-execution.md) | Static parallel groups |
| [Dynamic Parallel](./docs/dynamic-parallel.md) | For-each groups and array processing |
| [Claude Provider](./docs/providers/claude.md) | Claude setup and configuration |
| [Provider Comparison](./docs/providers/comparison.md) | Copilot vs Claude decision guide |

## Development

### Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) for dependency management

### Setup

```bash
git clone https://github.com/microsoft/conductor.git
cd conductor
make dev
```

### Common Commands

```bash
make test             # Run tests
make test-cov         # Run tests with coverage
make lint             # Check linting
make format           # Auto-fix and format code
make typecheck        # Type check
make check            # Run all checks (lint + typecheck)
make validate-examples  # Validate all example workflows
```

### Code Style

- [Ruff](https://github.com/astral-sh/ruff) for linting and formatting
- [ty](https://github.com/astral-sh/ty) for type checking
- Google-style docstrings

## Contributing

This project welcomes contributions and suggestions.  Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit [Contributor License Agreements](https://cla.opensource.microsoft.com).

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

To submit a pull request, follow these steps:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Run tests and checks (`make test && make check`)
5. Commit your changes (`git commit -m 'Add amazing feature'`)
6. Push to the branch (`git push origin feature/amazing-feature`)
7. Open a Pull Request

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft
trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.


## License

MIT License - see [LICENSE](./LICENSE) for details.
