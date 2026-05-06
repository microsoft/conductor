# Conductor installer for Windows (PowerShell)
# Usage: irm https://aka.ms/conductor/install.ps1 | iex
#
# This script:
#   1. Checks for uv (installs it if missing)
#   2. Fetches the latest Conductor release from GitHub
#   3. Downloads and verifies the constraints file (SHA-256)
#   4. Installs Conductor via uv tool install with pinned dependencies

$ErrorActionPreference = 'Stop'

$Repo = 'microsoft/conductor'
$GitHubApi = "https://api.github.com/repos/$Repo/releases/latest"
$GitHubDL = "https://github.com/$Repo/releases/download"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Write-Info  { param([string]$Msg) Write-Host "  → $Msg" -ForegroundColor Cyan }
function Write-Ok    { param([string]$Msg) Write-Host "  ✓ $Msg" -ForegroundColor Green }
function Write-Err   { param([string]$Msg) Write-Host "  ✗ $Msg" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

Write-Host "`nConductor Installer`n" -ForegroundColor White -NoNewline
Write-Host ""

# --- uv ---
$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvCmd) {
    Write-Info "uv not found — installing…"
    irm https://astral.sh/uv/install.ps1 | iex
    # Refresh PATH so uv is available
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'User') + ';' +
                [System.Environment]::GetEnvironmentVariable('Path', 'Machine')
    $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uvCmd) {
        Write-Err "uv installation succeeded but 'uv' is not on PATH. Please restart your terminal and retry."
    }
    Write-Ok "uv installed"
} else {
    Write-Ok "uv found at $($uvCmd.Source)"
}

# --- Detect latest release ---
Write-Info "Fetching latest release…"
$headers = @{ Accept = 'application/vnd.github+json' }
$release = Invoke-RestMethod -Uri $GitHubApi -Headers $headers
$tagName = $release.tag_name

if (-not $tagName) {
    Write-Err "Could not determine latest release tag from GitHub API."
}

Write-Ok "Latest release: $tagName"

# --- Check existing installation ---
$existingConductor = Get-Command conductor -ErrorAction SilentlyContinue
if ($existingConductor) {
    $currentVersion = $null
    try {
        $versionOutput = (conductor --version 2>&1) | Out-String
        if ($versionOutput -match '(\d+\.\d+\.\d+[^ ]*)') {
            $currentVersion = $Matches[1]
        }
    } catch { }

    if ($currentVersion) {
        $latestVersion = $tagName -replace '^v', ''
        if ($currentVersion -eq $latestVersion) {
            Write-Ok "Conductor v$currentVersion is already installed and up to date."
            Write-Host ""
            Write-Host "  Run 'conductor --help' to get started."
            Write-Host ""
            return
        }
        Write-Info "Upgrading Conductor: v$currentVersion → $tagName"
    }
}

# --- Download constraints + checksum to temp directory ---
$tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) "conductor-install-$([guid]::NewGuid().ToString('N').Substring(0,8))"
New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null

try {
    Write-Info "Downloading constraints…"
    $constraintsUrl = "$GitHubDL/$tagName/constraints.txt"
    $checksumUrl    = "$GitHubDL/$tagName/constraints.txt.sha256"
    $constraintsFile = Join-Path $tmpDir 'constraints.txt'
    $checksumFile    = Join-Path $tmpDir 'constraints.txt.sha256'

    Invoke-WebRequest -Uri $constraintsUrl -OutFile $constraintsFile -UseBasicParsing
    Invoke-WebRequest -Uri $checksumUrl    -OutFile $checksumFile    -UseBasicParsing

    # --- Verify checksum ---
    Write-Info "Verifying checksum…"
    $expectedHash = (Get-Content $checksumFile -Raw).Trim().Split(' ')[0]
    $actualHash = (Get-FileHash -Path $constraintsFile -Algorithm SHA256).Hash.ToLower()

    if ($actualHash -ne $expectedHash) {
        Write-Err "Checksum verification failed for constraints.txt (expected $expectedHash, got $actualHash)"
    }
    Write-Ok "Checksum verified"

    # --- Install (with retry for Windows Defender file locking) ---
    $maxRetries = 3
    $installed = $false
    $lastOutput = $null
    $lastExitCode = $null
    for ($attempt = 1; $attempt -le $maxRetries; $attempt++) {
        if ($attempt -eq 1) {
            Write-Info "Installing Conductor $tagName…"
        } else {
            Write-Info "Retrying install (attempt $attempt/$maxRetries)…"
            Start-Sleep -Seconds 2
        }

        # Run uv via temp files so we capture stdout AND stderr regardless of
        # PowerShell's $ErrorActionPreference behavior on native stderr.
        # Previous approach (`& uv ... 2>&1` inside try/catch with Stop) lost
        # the output when PS threw before the assignment completed, leaving
        # the user with "(no output captured)" and no way to diagnose.
        $stdoutFile = Join-Path $tmpDir "uv-stdout-$attempt.log"
        $stderrFile = Join-Path $tmpDir "uv-stderr-$attempt.log"
        $proc = Start-Process -FilePath 'uv' `
            -ArgumentList @('tool', 'install', '--force', "git+https://github.com/$Repo.git@$tagName", '-c', $constraintsFile) `
            -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $stdoutFile `
            -RedirectStandardError $stderrFile

        $stdout = if (Test-Path $stdoutFile) { Get-Content $stdoutFile -Raw } else { '' }
        $stderr = if (Test-Path $stderrFile) { Get-Content $stderrFile -Raw } else { '' }
        $lastOutput = (@($stdout, $stderr) | Where-Object { $_ } | Out-String).Trim()
        $lastExitCode = $proc.ExitCode
        if ($lastExitCode -eq 0) {
            $installed = $true
            break
        }
    }

    if (-not $installed) {
        Write-Host ""
        Write-Host "  ── uv tool install output (exit code $lastExitCode) ──" -ForegroundColor Yellow
        if ($lastOutput) {
            $lastOutput -split "`r?`n" | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
        } else {
            Write-Host "  (no output captured — run 'uv tool install --force `"git+https://github.com/$Repo.git@$tagName`"' manually to see the error)" -ForegroundColor DarkGray
        }
        Write-Host ""
        Write-Info "Install failed after $maxRetries attempts."
        Write-Info "If the output above mentions a locked file or 'access is denied',"
        Write-Info "Windows Defender may be scanning the install directory. Try adding"
        Write-Info "an exclusion (run PowerShell as Administrator):"
        Write-Info "  Add-MpPreference -ExclusionPath `"$env:LOCALAPPDATA\uv`""
        Write-Info "Then re-run this installer."
        Write-Host ""
        Write-Err "uv tool install failed"
    }

    Write-Ok "Conductor $tagName installed"

    # --- Ensure uv's tool bin directory is on the persistent user PATH ---
    # `uv tool install` only modifies the *current* process PATH. New terminals,
    # sub-processes, CI agents, and IDE extensions inherit PATH from the user
    # registry (HKCU\Environment\Path) and won't find `conductor` unless we run
    # `uv tool update-shell`, which writes the bin dir to the registry. See #115.
    Write-Info "Ensuring conductor is on PATH for new shells…"
    try {
        & uv tool update-shell 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "PATH updated (restart your terminal to pick up the change)"
        } else {
            Write-Info "Could not update user PATH automatically. Run 'uv tool update-shell' manually."
        }
    } catch {
        Write-Info "Could not update user PATH automatically. Run 'uv tool update-shell' manually."
    }

    Write-Host ""
    Write-Host "  Run 'conductor --help' to get started."
    Write-Host "  Run 'conductor update' to check for future updates."
    Write-Host ""
} finally {
    Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
}
