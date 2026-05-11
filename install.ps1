# Conductor installer for Windows (PowerShell)
# Usage: irm https://aka.ms/conductor/install.ps1 | iex
#
# This script:
#   1. Checks for uv (installs it if missing)
#   2. Fetches the latest Conductor release from GitHub (or uses -Source override)
#   3. Downloads and verifies the constraints file (SHA-256)
#   4. Installs Conductor via uv tool install with pinned dependencies
#
# Robustness features for upgrade-over-existing-install:
#   - Detects other running conductor processes; with -AutoStop kills them and
#     continues, otherwise prompts (or aborts if no TTY)
#   - Sweeps stale *.exe.old files left by previous failed updates
#   - Retries with backoff (2s, 5s, 10s) to absorb Defender / file-lock blips
#   - Rename-fallback: if uv can't remove the Scripts dir because of locks,
#     renames the whole conductor-cli tool dir out of the way and retries
#   - Verifies the install succeeded by running `conductor --version`
#
# Test hooks (used by tests/integration/test_install_scripts.py):
#   -Source <path-or-url>     OR   $env:CONDUCTOR_INSTALL_SOURCE
#       Install from this source (wheel path, directory, or git+ URL) instead
#       of the latest GitHub release. Skips constraints download.
#   -AutoStop                 OR   $env:CONDUCTOR_INSTALL_AUTO_STOP = '1'
#       If other conductor.exe processes are running, stop them and continue
#       without prompting. Without this flag, the script prompts when a TTY is
#       available, or aborts when running non-interactively.
#   -Force                    OR   $env:CONDUCTOR_INSTALL_FORCE = '1'
#       Skip the running-process check entirely.

[CmdletBinding()]
param(
    [string]$Source,
    [switch]$AutoStop,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$Repo      = 'microsoft/conductor'
$GitHubApi = "https://api.github.com/repos/$Repo/releases/latest"
$GitHubDL  = "https://github.com/$Repo/releases/download"

# Env-var fallbacks so the script works under `irm | iex` (no real params)
if (-not $Source   -and $env:CONDUCTOR_INSTALL_SOURCE)               { $Source   = $env:CONDUCTOR_INSTALL_SOURCE }
if (-not $AutoStop -and $env:CONDUCTOR_INSTALL_AUTO_STOP -eq '1')    { $AutoStop = $true }
if (-not $Force    -and $env:CONDUCTOR_INSTALL_FORCE     -eq '1')    { $Force    = $true }

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Write-Info { param([string]$Msg) Write-Host "  -> $Msg" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Msg) Write-Host "  [OK] $Msg" -ForegroundColor Green }
function Write-Warn { param([string]$Msg) Write-Host "  ! $Msg" -ForegroundColor Yellow }
function Write-Err  { param([string]$Msg) Write-Host "  [X] $Msg" -ForegroundColor Red; exit 1 }

function Get-UvToolsDir {
    # `uv tool dir` returns the canonical tools directory for the current uv install.
    # Fall back to known defaults if the command fails (older uv versions).
    try {
        $out = & uv tool dir 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) {
            $line = ($out | Where-Object { $_ -and $_.Trim() } | Select-Object -First 1).Trim()
            if ($line -and (Test-Path -LiteralPath $line)) { return $line }
        }
    } catch { }
    foreach ($base in @($env:LOCALAPPDATA, $env:APPDATA)) {
        if ($base) {
            $candidate = Join-Path $base 'uv\tools'
            if (Test-Path -LiteralPath $candidate) { return $candidate }
        }
    }
    return $null
}

function Get-ConductorToolDir {
    $tools = Get-UvToolsDir
    if (-not $tools) { return $null }
    $dir = Join-Path $tools 'conductor-cli'
    if (Test-Path -LiteralPath $dir) { return $dir }
    return $null
}

function Get-RunningConductorProcesses {
    # Returns a list of pscustomobjects: @{ Pid; Name; Path }
    # Excludes the current PowerShell process and its ancestors so we don't
    # flag ourselves when the user re-runs the script from inside a conductor
    # workflow shell.
    $excluded = @{}
    $excluded[[int]$PID] = $true
    try {
        $cur = Get-CimInstance Win32_Process -Filter "ProcessId = $PID"
        while ($cur -and $cur.ParentProcessId -gt 0 -and -not $excluded.ContainsKey([int]$cur.ParentProcessId)) {
            $excluded[[int]$cur.ParentProcessId] = $true
            $cur = Get-CimInstance Win32_Process -Filter "ProcessId = $($cur.ParentProcessId)"
        }
    } catch { }

    $results = @()
    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name = 'conductor.exe'" -ErrorAction Stop
        foreach ($p in $procs) {
            if ($excluded.ContainsKey([int]$p.ProcessId)) { continue }
            $results += [pscustomobject]@{
                ProcessId = [int]$p.ProcessId
                Name      = $p.Name
                Path      = $p.ExecutablePath
            }
        }
    } catch { }
    return $results
}

function Remove-StaleOldFiles {
    param([string]$ScriptsDir)
    if (-not $ScriptsDir -or -not (Test-Path -LiteralPath $ScriptsDir)) { return }
    $stale = Get-ChildItem -LiteralPath $ScriptsDir -Filter '*.old' -ErrorAction SilentlyContinue
    foreach ($f in $stale) {
        try {
            Remove-Item -LiteralPath $f.FullName -Force -ErrorAction Stop
        } catch {
            Write-Warn "Could not remove stale file $($f.FullName): $($_.Exception.Message)"
        }
    }
}

function Invoke-UvInstall {
    param(
        [string]$InstallSource,
        [string]$ConstraintsFile,
        [string]$LogDir
    )
    # Returns @{ ExitCode; Stdout; Stderr } for one attempt.
    $stdoutFile = Join-Path $LogDir ("uv-stdout-{0}.log" -f ([guid]::NewGuid().ToString('N').Substring(0,6)))
    $stderrFile = Join-Path $LogDir ("uv-stderr-{0}.log" -f ([guid]::NewGuid().ToString('N').Substring(0,6)))

    $argList = @('tool', 'install', '--force', $InstallSource)
    if ($ConstraintsFile) { $argList += @('-c', $ConstraintsFile) }

    $proc = Start-Process -FilePath 'uv' `
        -ArgumentList $argList `
        -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $stdoutFile `
        -RedirectStandardError  $stderrFile

    $stdout = if (Test-Path -LiteralPath $stdoutFile) { Get-Content -LiteralPath $stdoutFile -Raw } else { '' }
    $stderr = if (Test-Path -LiteralPath $stderrFile) { Get-Content -LiteralPath $stderrFile -Raw } else { '' }
    return @{
        ExitCode = $proc.ExitCode
        Stdout   = $stdout
        Stderr   = $stderr
    }
}

function Test-LockError {
    param([string]$Output)
    if (-not $Output) { return $false }
    $lower = $Output.ToLower()
    foreach ($needle in @('access is denied', 'failed to remove directory', 'used by another process', 'cannot access the file')) {
        if ($lower.Contains($needle)) { return $true }
    }
    return $false
}

function Move-ConductorToolDirAside {
    # Rename the entire conductor-cli tool dir to conductor-cli.old-<ts>.
    # Returns the path of the renamed directory, or $null if it didn't exist
    # or rename failed.
    $current = Get-ConductorToolDir
    if (-not $current) { return $null }
    $stamp = (Get-Date).ToString('yyyyMMddHHmmssfff')
    $renamed = "$current.old-$stamp"
    try {
        # On Windows, Move-Item across same volume is a rename -- atomic and
        # works even if files inside are locked, as long as no handles point
        # at the *directory* itself.
        Move-Item -LiteralPath $current -Destination $renamed -Force -ErrorAction Stop
        return $renamed
    } catch {
        Write-Warn "Could not rename $current aside: $($_.Exception.Message)"
        return $null
    }
}

function Get-NewShellConductorVersion {
    # Run `conductor --version` in a *fresh* PowerShell so we don't pick up
    # our parent's PATH cache. Returns the printed version string or $null.
    $uvBin = Join-Path ([System.IO.Path]::GetDirectoryName((Get-Command uv).Source)) ''
    try {
        # Resolve the freshly installed conductor.exe via `uv tool list` /
        # `where` rather than relying on PATH inheritance.
        $exe = $null
        try {
            $exe = (& uv tool dir 2>$null | Select-Object -First 1).Trim()
            if ($exe) { $exe = Join-Path $exe 'conductor-cli\Scripts\conductor.exe' }
        } catch { }
        if (-not $exe -or -not (Test-Path -LiteralPath $exe)) {
            $cmd = Get-Command conductor -ErrorAction SilentlyContinue
            if ($cmd) { $exe = $cmd.Source }
        }
        if (-not $exe -or -not (Test-Path -LiteralPath $exe)) { return $null }

        $out = & $exe --version 2>&1 | Out-String
        if ($LASTEXITCODE -eq 0 -and $out -match '(\d+\.\d+\.\d+[^\s]*)') { return $Matches[1] }
    } catch { }
    return $null
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

Write-Host "`nConductor Installer`n" -ForegroundColor White

# --- uv ---
$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvCmd) {
    Write-Info "uv not found -- installing..."
    irm https://astral.sh/uv/install.ps1 | iex
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

# --- Determine install source ---
$installSource   = $null  # what we pass to `uv tool install`
$displayVersion  = $null  # for messages
$skipConstraints = $false
$tagName         = $null

if ($Source) {
    $installSource   = $Source
    $displayVersion  = "(local source)"
    $skipConstraints = $true
    Write-Info "Using local source override: $Source"
} else {
    Write-Info "Fetching latest release..."
    $headers = @{ Accept = 'application/vnd.github+json' }
    $release = Invoke-RestMethod -Uri $GitHubApi -Headers $headers
    $tagName = $release.tag_name
    if (-not $tagName) {
        Write-Err "Could not determine latest release tag from GitHub API."
    }
    Write-Ok "Latest release: $tagName"
    $installSource  = "git+https://github.com/$Repo.git@$tagName"
    $displayVersion = $tagName
}

# --- Check existing installation (only meaningful for the GitHub-release path) ---
if (-not $Source) {
    $existingConductor = Get-Command conductor -ErrorAction SilentlyContinue
    if ($existingConductor) {
        $currentVersion = $null
        try {
            $versionOutput = (conductor --version 2>&1) | Out-String
            if ($versionOutput -match '(\d+\.\d+\.\d+[^\s]*)') { $currentVersion = $Matches[1] }
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
            Write-Info "Upgrading Conductor: v$currentVersion -> $tagName"
        }
    }
}

# --- Safety: detect other running conductor processes ---
if (-not $Force) {
    $running = Get-RunningConductorProcesses
    if ($running.Count -gt 0) {
        Write-Warn "Other Conductor processes are running:"
        foreach ($r in $running) {
            $pathOrName = if ($r.Path) { $r.Path } else { $r.Name }
            Write-Host ("    * PID {0}: {1}" -f $r.ProcessId, $pathOrName)
        }
        Write-Host ""
        Write-Host "  These can hold file locks that cause the upgrade to fail."
        Write-Host "  Stop them (e.g. 'conductor stop --all' for background dashboards),"
        Write-Host "  re-run with -AutoStop to stop them automatically,"
        Write-Host "  or re-run with -Force to skip this check entirely."
        Write-Host ""

        $shouldStop = $false
        if ($AutoStop) {
            $shouldStop = $true
        } elseif ([Console]::IsInputRedirected -or -not $Host.UI.RawUI) {
            # No TTY (irm | iex from a script, CI pipe, etc.) -- refuse to guess.
            Write-Err "Aborted (other Conductor processes running; re-run with -AutoStop to stop them, or -Force to skip the check)."
        } else {
            $resp = Read-Host "  Stop them now and continue? [y/N]"
            if ($resp -match '^(y|yes)$') {
                $shouldStop = $true
            } else {
                Write-Err "Aborted."
            }
        }

        if ($shouldStop) {
            foreach ($r in $running) {
                try {
                    Stop-Process -Id $r.ProcessId -Force -ErrorAction Stop
                    Write-Ok "Stopped PID $($r.ProcessId)"
                } catch {
                    Write-Warn "Could not stop PID $($r.ProcessId): $($_.Exception.Message)"
                }
            }
            Start-Sleep -Seconds 1
        }
    }
}

# --- Pre-clean stale *.old files in the existing install ---
$existingTool = Get-ConductorToolDir
if ($existingTool) {
    $scriptsDir = Join-Path $existingTool 'Scripts'
    Remove-StaleOldFiles -ScriptsDir $scriptsDir
}

# --- Working temp dir for logs and constraints ---
$tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) "conductor-install-$([guid]::NewGuid().ToString('N').Substring(0,8))"
New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null

$constraintsFile = $null
$renamedAside    = $null
$installed       = $false
$lastExitCode    = $null
$lastStdout      = $null
$lastStderr      = $null

try {
    # --- Constraints (skipped for -Source overrides) ---
    if (-not $skipConstraints -and $tagName) {
        Write-Info "Downloading constraints..."
        $constraintsUrl  = "$GitHubDL/$tagName/constraints.txt"
        $checksumUrl     = "$GitHubDL/$tagName/constraints.txt.sha256"
        $constraintsFile = Join-Path $tmpDir 'constraints.txt'
        $checksumFile    = Join-Path $tmpDir 'constraints.txt.sha256'
        try {
            Invoke-WebRequest -Uri $constraintsUrl -OutFile $constraintsFile -UseBasicParsing
            Invoke-WebRequest -Uri $checksumUrl    -OutFile $checksumFile    -UseBasicParsing

            Write-Info "Verifying checksum..."
            $expectedHash = (Get-Content $checksumFile -Raw).Trim().Split(' ')[0]
            $actualHash = (Get-FileHash -Path $constraintsFile -Algorithm SHA256).Hash.ToLower()
            if ($actualHash -ne $expectedHash) {
                Write-Err "Checksum verification failed for constraints.txt (expected $expectedHash, got $actualHash)"
            }
            Write-Ok "Checksum verified"
        } catch {
            Write-Warn "Could not download/verify constraints; installing without."
            $constraintsFile = $null
        }
    }

    # --- Install with retries + rename-fallback ---
    $delays = @(2, 5, 10)
    Write-Info "Installing Conductor $displayVersion..."
    for ($attempt = 1; $attempt -le ($delays.Count + 1); $attempt++) {
        if ($attempt -gt 1) {
            $sleep = $delays[[Math]::Min($attempt - 2, $delays.Count - 1)]
            Write-Info "Retrying install (attempt $attempt) after ${sleep}s..."
            Start-Sleep -Seconds $sleep
        }

        $r = Invoke-UvInstall -InstallSource $installSource -ConstraintsFile $constraintsFile -LogDir $tmpDir
        $lastExitCode = $r.ExitCode
        $lastStdout   = $r.Stdout
        $lastStderr   = $r.Stderr
        if ($lastExitCode -eq 0) {
            $installed = $true
            break
        }

        # If we hit a directory-lock error and we haven't already renamed
        # aside, try the rename-fallback before the next retry.
        if (-not $renamedAside -and (Test-LockError -Output ($lastStderr + $lastStdout))) {
            Write-Warn "Install blocked by a file lock; renaming the existing tool dir aside and retrying..."
            $renamedAside = Move-ConductorToolDirAside
            if ($renamedAside) {
                Write-Ok "Moved existing install to $renamedAside"
                # Sweep again in case there's a parallel APPDATA copy
                $existingTool2 = Get-ConductorToolDir
                if ($existingTool2) {
                    Remove-StaleOldFiles -ScriptsDir (Join-Path $existingTool2 'Scripts')
                }
            }
        }
    }

    if (-not $installed) {
        Write-Host ""
        Write-Host "  -- uv tool install output (exit code $lastExitCode) --" -ForegroundColor Yellow
        $combined = (@($lastStdout, $lastStderr) | Where-Object { $_ } | Out-String).Trim()
        if ($combined) {
            $combined -split "`r?`n" | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
        } else {
            Write-Host "  (no output captured)" -ForegroundColor DarkGray
        }
        Write-Host ""
        Write-Info "Install failed."
        Write-Info "If the error mentions 'Access is denied' or 'failed to remove directory':"
        Write-Info "  * Stop any running Conductor processes (try Task Manager or 'Get-Process conductor')"
        Write-Info "  * Windows Defender may be scanning the install directory. Try an exclusion:"
        Write-Info "      Add-MpPreference -ExclusionPath `"`$env:LOCALAPPDATA\uv`""
        if ($renamedAside) {
            Write-Info "  * The previous install was moved to: $renamedAside"
            Write-Info "    You can delete it manually once nothing has it open."
        }
        Write-Host ""
        Write-Err "uv tool install failed"
    }

    Write-Ok "Conductor $displayVersion installed"

    # --- Update PATH for new shells ---
    Write-Info "Ensuring conductor is on PATH for new shells..."
    try {
        & uv tool update-shell 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "PATH updated (restart your terminal to pick up the change)"
        } else {
            Write-Warn "Could not update user PATH automatically. Run 'uv tool update-shell' manually."
        }
    } catch {
        Write-Warn "Could not update user PATH automatically. Run 'uv tool update-shell' manually."
    }

    # --- Verify ---
    $verified = Get-NewShellConductorVersion
    if ($verified) {
        Write-Ok "Verified: conductor $verified responds correctly"
    } else {
        Write-Warn "Could not run conductor --version after install (PATH may need a fresh shell)."
    }

    # --- Best-effort cleanup of the renamed-aside dir ---
    if ($renamedAside -and (Test-Path -LiteralPath $renamedAside)) {
        try {
            Remove-Item -LiteralPath $renamedAside -Recurse -Force -ErrorAction Stop
            Write-Ok "Cleaned up old install dir"
        } catch {
            Write-Warn "Old install dir kept at $renamedAside (delete manually once unlocked)."
        }
    }

    Write-Host ""
    Write-Host "  Run 'conductor --help' to get started."
    Write-Host "  Run 'conductor update' to check for future updates."
    Write-Host ""
} finally {
    Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
}
