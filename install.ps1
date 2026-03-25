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
        $versionOutput = conductor --version 2>$null
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
            exit 0
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
    for ($attempt = 1; $attempt -le $maxRetries; $attempt++) {
        if ($attempt -eq 1) {
            Write-Info "Installing Conductor $tagName…"
        } else {
            Write-Info "Retrying install (attempt $attempt/$maxRetries)…"
            Start-Sleep -Seconds 2
        }
        uv tool install --force "git+https://github.com/$Repo.git@$tagName" -c $constraintsFile 2>$null
        if ($LASTEXITCODE -eq 0) {
            $installed = $true
            break
        }
    }

    if (-not $installed) {
        Write-Host ""
        Write-Info "Install failed after $maxRetries attempts. This is often caused by"
        Write-Info "Windows Defender scanning files during install. Try:"
        Write-Info "  1. Add a Defender exclusion: Add-MpExclusion -Path `"$env:LOCALAPPDATA\uv`""
        Write-Info "  2. Re-run this installer"
        Write-Host ""
        Write-Err "uv tool install failed"
    }

    Write-Ok "Conductor $tagName installed"
    Write-Host ""
    Write-Host "  Run 'conductor --help' to get started."
    Write-Host "  Run 'conductor update' to check for future updates."
    Write-Host ""
} finally {
    Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
}
