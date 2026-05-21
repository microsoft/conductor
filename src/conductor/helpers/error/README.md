# Conductor error helpers

These are **optional** convenience modules for raising typed
[`on_error`](../../../../docs/projects/error-routing/on-error-routing.brainstorm.md)
envelopes from `type: script` workflow nodes. Each file is ~15 lines
of language-native code that:

1. Reads `CONDUCTOR_ERROR_OUT` from the environment (which Conductor
   sets to a per-invocation file path before running the script).
2. Writes a JSON object of the shape
   `{ "conductor_error": true, "kind": "...", "message": "...", "details"?: {...} }`
   to that path.
3. Returns — leaving exit-code management to the caller (helpers
   never call `exit` / `Environment.Exit` themselves).

Conductor then reads the file after the script exits, treats the node
as having raised, and evaluates `on_error` routes against the
envelope. The script can exit `0` after writing the envelope; the
non-zero / fallback rules apply *only* when no envelope was written.

Nothing here is auto-loaded. None of these files are on `PATH`, on
`PYTHONPATH`, or otherwise injected into the script's environment.
Script authors that want them must copy or reference them explicitly
(`Import-Module`, `source`, `import`, etc.). Script authors that don't
want them write the JSON themselves — it's three lines in every
supported engine.

## Files

| Engine          | File                       | Surface                                                                          |
| --------------- | -------------------------- | -------------------------------------------------------------------------------- |
| PowerShell      | `Conductor.Error.psm1`     | `Write-ConductorError -Kind x.y -Message m [-Details @{...}]`                    |
| Bash / sh       | `conductor-error.sh`       | `conductor_error x.y "message" '{"k":"v"}'` (source first)                       |
| Python          | `conductor_error.py`       | `conductor_error.raise_kind("x.y", "message", details={...})`                    |
| Node            | `conductor-error.mjs`      | `raiseError({ kind: "x.y", message: "m", details: {} })`                         |
| .NET (net6.0+)  | `ConductorError.cs`        | `ConductorError.Raise("x.y", "message", new { ... })`                            |

## Example (PowerShell)

```powershell
Import-Module ./Conductor.Error.psm1

git fetch origin 2>&1 | Tee-Object -Variable gitOut | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-ConductorError -Kind 'external.git.fetch_failed' `
                         -Message "git fetch failed: $($gitOut[-1])" `
                         -Details @{ remote = 'origin'; exit = $LASTEXITCODE }
    exit 0
}
```

## Example (Bash)

```bash
. ./conductor-error.sh

if ! git fetch origin 2>/tmp/err; then
    conductor_error \
        "external.git.fetch_failed" \
        "git fetch failed: $(head -1 /tmp/err)" \
        '{"remote":"origin"}'
    exit 0
fi
```

## Example (Python)

```python
import conductor_error
import subprocess
import sys

result = subprocess.run(["git", "fetch", "origin"], capture_output=True, text=True)
if result.returncode != 0:
    conductor_error.raise_kind(
        "external.git.fetch_failed",
        f"git fetch failed: {result.stderr.splitlines()[0] if result.stderr else ''}",
        details={"remote": "origin", "exit": result.returncode},
    )
    sys.exit(0)
```

## Notes

- The contract is `kind`, `message`, optional `details`. Conductor
  validates the shape of the envelope itself — see
  `conductor.engine.errors.coerce_envelope`.
- Helpers do not validate the *value* of `kind` beyond requiring a
  non-empty string. Whether `kind` is allowed at runtime is governed
  by the node's `raises:` list in the workflow YAML; an undeclared
  kind is rewritten to `internal.undeclared_kind` by the engine.
- Helpers do not call `exit`. Callers stay in charge of process exit
  so they can do their own teardown (close handles, flush logs)
  before returning control to Conductor.
- New engines do not need a helper to use the contract — write the
  JSON yourself. The contract is the API.
