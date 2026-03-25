# Conductor Setup

## Installation

Conductor does not need to be checked before every use. Simply run `conductor` commands directly. If the command fails with "command not found", install it:

**macOS / Linux:**
```bash
curl -sSfL https://aka.ms/conductor/install.sh | sh
```

**Windows (PowerShell):**
```powershell
irm https://aka.ms/conductor/install.ps1 | iex
```

The installer checks for uv (installs it if missing), fetches the latest release with pinned dependencies, and verifies integrity via SHA-256 checksum.

### Manual Install

If you prefer to install manually:

```bash
uv tool install git+https://github.com/microsoft/conductor.git
```

If `uv` is also not available, install it first:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Updating

```bash
conductor update
```

Or re-run the install script — it detects existing installs and upgrades automatically.

After installation, retry the original `conductor` command.

