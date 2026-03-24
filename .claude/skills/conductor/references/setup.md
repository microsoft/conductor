# Conductor Setup

## Installation

Conductor does not need to be checked before every use. Simply run `conductor` commands directly. If the command fails with "command not found", install it:

```bash
uv tool install --locked git+https://github.com/microsoft/conductor.git
```

If `uv` is also not available, install it first:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

After installation, retry the original `conductor` command.

