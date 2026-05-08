#!/bin/sh
# Conductor installer for macOS and Linux
# Usage: curl -sSfL https://aka.ms/conductor/install.sh | sh
#
# This script:
#   1. Checks for uv (installs it if missing)
#   2. Fetches the latest Conductor release from GitHub (or uses --source override)
#   3. Downloads and verifies the constraints file (SHA-256)
#   4. Installs Conductor via uv tool install with pinned dependencies
#
# Test hooks (used by tests/integration/test_install_scripts.py):
#   --source <path-or-url>    OR   $CONDUCTOR_INSTALL_SOURCE
#       Install from this source (wheel path, directory, or git+ URL) instead
#       of the latest GitHub release. Skips constraints download.
#   --yes                     OR   $CONDUCTOR_INSTALL_YES=1
#       Non-interactive mode.
#   --force                   OR   $CONDUCTOR_INSTALL_FORCE=1
#       Skip the running-process check.

set -eu

REPO="microsoft/conductor"
GITHUB_API="https://api.github.com/repos/${REPO}/releases/latest"
GITHUB_DL="https://github.com/${REPO}/releases/download"

# ---------------------------------------------------------------------------
# Argument + env parsing
# ---------------------------------------------------------------------------

SOURCE="${CONDUCTOR_INSTALL_SOURCE:-}"
YES="${CONDUCTOR_INSTALL_YES:-0}"
FORCE_FLAG="${CONDUCTOR_INSTALL_FORCE:-0}"

while [ $# -gt 0 ]; do
    case "$1" in
        --source)        SOURCE="$2"; shift 2 ;;
        --source=*)      SOURCE="${1#--source=}"; shift ;;
        --yes|-y)        YES=1; shift ;;
        --force)         FORCE_FLAG=1; shift ;;
        *) shift ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

info()    { printf '  \033[1;34m→\033[0m %s\n' "$1"; }
success() { printf '  \033[1;32m✓\033[0m %s\n' "$1"; }
warn()    { printf '  \033[1;33m!\033[0m %s\n' "$1" >&2; }
error()   { printf '  \033[1;31m✗\033[0m %s\n' "$1" >&2; exit 1; }

need_cmd() {
    command -v "$1" > /dev/null 2>&1
}

download() {
    url="$1"
    dest="$2"
    if need_cmd curl; then
        curl -sSfL -o "$dest" "$url"
    elif need_cmd wget; then
        wget -qO "$dest" "$url"
    else
        error "Neither curl nor wget found. Please install one and retry."
    fi
}

download_stdout() {
    url="$1"
    if need_cmd curl; then
        curl -sSfL "$url"
    elif need_cmd wget; then
        wget -qO- "$url"
    else
        error "Neither curl nor wget found. Please install one and retry."
    fi
}

verify_checksum() {
    file="$1"
    expected="$2"
    if need_cmd sha256sum; then
        actual=$(sha256sum "$file" | cut -d' ' -f1)
    elif need_cmd shasum; then
        actual=$(shasum -a 256 "$file" | cut -d' ' -f1)
    else
        info "Warning: cannot verify checksum (sha256sum/shasum not found), skipping"
        return 0
    fi
    if [ "$actual" != "$expected" ]; then
        error "Checksum verification failed for constraints.txt (expected ${expected}, got ${actual})"
    fi
}

# Find other running conductor processes (excluding self + ancestors).
# Outputs lines: "PID COMMAND"
find_running_conductor() {
    self_pid=$$
    parent_pid=$(ps -o ppid= -p "$self_pid" 2>/dev/null | tr -d ' ' || echo '')
    # Match the conductor entrypoint (binary or `python -m conductor`).
    ps -axo pid=,command= 2>/dev/null | awk -v self="$self_pid" -v parent="$parent_pid" '
        $1 == self || $1 == parent { next }
        {
            cmd = $0
            sub(/^[ \t]*[0-9]+[ \t]+/, "", cmd)
            base = cmd
            sub(/[ \t].*$/, "", base)
            n = split(base, parts, "/")
            leaf = parts[n]
            if (leaf == "conductor" || leaf ~ /^conductor[._-]/) { print $1, cmd; next }
            if (leaf ~ /python/ && cmd ~ /[ \t]-m[ \t]+conductor/) { print $1, cmd; next }
        }
    '
}

# Run `uv tool install` with retry+backoff. Echoes combined stdout+stderr to
# the LOG_FILE and returns the final exit code.
uv_install_with_retry() {
    install_source="$1"
    constraints="$2"  # may be empty
    log_file="$3"

    delays="2 5 10"
    attempt=1
    ec=0
    : > "$log_file"
    set +e
    if [ -n "$constraints" ]; then
        uv tool install --force "$install_source" -c "$constraints" >>"$log_file" 2>&1
    else
        uv tool install --force "$install_source" >>"$log_file" 2>&1
    fi
    ec=$?
    set -e
    [ "$ec" -eq 0 ] && return 0

    for d in $delays; do
        attempt=$((attempt + 1))
        info "Retrying install (attempt ${attempt}) after ${d}s…"
        sleep "$d"
        printf '\n--- attempt %s ---\n' "$attempt" >>"$log_file"
        set +e
        if [ -n "$constraints" ]; then
            uv tool install --force "$install_source" -c "$constraints" >>"$log_file" 2>&1
        else
            uv tool install --force "$install_source" >>"$log_file" 2>&1
        fi
        ec=$?
        set -e
        [ "$ec" -eq 0 ] && return 0
    done
    return "$ec"
}

# Run `conductor --version` from the freshly installed location and return the
# version string (or empty on failure).
verify_install() {
    tools_dir=$(uv tool dir 2>/dev/null | head -n1 || true)
    if [ -n "$tools_dir" ]; then
        # uv tool venvs put the entrypoint at <tool_dir>/<pkg>/bin/<exe> on POSIX
        for candidate in "$tools_dir/conductor-cli/bin/conductor" "$tools_dir/conductor/bin/conductor"; do
            if [ -x "$candidate" ]; then
                "$candidate" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+[^ ]*' | head -1
                return
            fi
        done
    fi
    if need_cmd conductor; then
        conductor --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+[^ ]*' | head -1
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    printf '\n\033[1mConductor Installer\033[0m\n\n'

    # --- uv ---
    if ! need_cmd uv; then
        info "uv not found — installing…"
        curl -sSfL https://astral.sh/uv/install.sh | sh
        if [ -f "$HOME/.local/bin/env" ]; then
            . "$HOME/.local/bin/env"
        fi
        export PATH="$HOME/.local/bin:$PATH"
        if ! need_cmd uv; then
            error "uv installation succeeded but 'uv' is not on PATH. Please add ~/.local/bin to your PATH and retry."
        fi
        success "uv installed"
    else
        success "uv found at $(command -v uv)"
    fi

    # --- Determine install source ---
    install_source=""
    display_version=""
    skip_constraints=0
    tag_name=""

    if [ -n "$SOURCE" ]; then
        install_source="$SOURCE"
        display_version="(local source)"
        skip_constraints=1
        info "Using local source override: $SOURCE"
    else
        info "Fetching latest release…"
        release_json=$(download_stdout "$GITHUB_API")
        tag_name=$(printf '%s' "$release_json" | grep -o '"tag_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | cut -d'"' -f4)
        if [ -z "$tag_name" ]; then
            error "Could not determine latest release tag from GitHub API."
        fi
        success "Latest release: ${tag_name}"
        install_source="git+https://github.com/${REPO}.git@${tag_name}"
        display_version="$tag_name"
    fi

    # --- Existing-install check (only for the GitHub-release path) ---
    if [ -z "$SOURCE" ] && need_cmd conductor; then
        current_version=$(conductor --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+[^ ]*' | head -1 || true)
        if [ -n "$current_version" ]; then
            latest_version=$(printf '%s' "$tag_name" | sed 's/^v//')
            if [ "$current_version" = "$latest_version" ]; then
                success "Conductor v${current_version} is already installed and up to date."
                printf '\n  Run \033[1mconductor --help\033[0m to get started.\n\n'
                return 0
            fi
            info "Upgrading Conductor: v${current_version} → ${tag_name}"
        fi
    fi

    # --- Safety: detect other running conductor processes ---
    if [ "$FORCE_FLAG" != "1" ]; then
        running=$(find_running_conductor || true)
        if [ -n "$running" ]; then
            warn "Other Conductor processes are running:"
            printf '%s\n' "$running" | while IFS= read -r line; do
                printf '    • %s\n' "$line"
            done
            printf '\n  These can hold file locks that may cause the upgrade to fail.\n'
            printf "  Stop them ('conductor stop --all' for background dashboards)\n"
            printf "  or re-run with --force to skip this check.\n\n"
            if [ "$YES" != "1" ]; then
                printf '  Continue anyway? [y/N] '
                read -r ans </dev/tty || ans=''
                case "$ans" in
                    y|Y|yes|YES) ;;
                    *) error "Aborted." ;;
                esac
            else
                error "Aborted (other Conductor processes running; re-run with --force to override)."
            fi
        fi
    fi

    # --- Working temp dir ---
    tmpdir=$(mktemp -d)
    trap 'rm -rf "$tmpdir"' EXIT

    constraints_file=""

    # --- Constraints (skipped for local-source overrides) ---
    if [ "$skip_constraints" -eq 0 ] && [ -n "$tag_name" ]; then
        info "Downloading constraints…"
        download "${GITHUB_DL}/${tag_name}/constraints.txt" "${tmpdir}/constraints.txt" 2>/dev/null \
            && download "${GITHUB_DL}/${tag_name}/constraints.txt.sha256" "${tmpdir}/constraints.txt.sha256" 2>/dev/null \
            && {
                info "Verifying checksum…"
                expected_hash=$(cut -d' ' -f1 "${tmpdir}/constraints.txt.sha256")
                verify_checksum "${tmpdir}/constraints.txt" "$expected_hash"
                success "Checksum verified"
                constraints_file="${tmpdir}/constraints.txt"
            } || {
                warn "Could not download/verify constraints; installing without."
                constraints_file=""
            }
    fi

    # --- Install with retries ---
    info "Installing Conductor ${display_version}…"
    log_file="${tmpdir}/uv-install.log"
    if ! uv_install_with_retry "$install_source" "$constraints_file" "$log_file"; then
        printf '\n  ── uv tool install output ──\n' >&2
        sed 's/^/  /' "$log_file" >&2
        printf '\n' >&2
        error "uv tool install failed after retries"
    fi
    success "Conductor ${display_version} installed"

    # --- Update shell PATH ---
    info "Ensuring conductor is on PATH for new shells…"
    if uv tool update-shell >/dev/null 2>&1; then
        success "PATH updated (restart your shell to pick up the change)"
    else
        warn "Could not update shell PATH automatically. Run 'uv tool update-shell' manually."
    fi

    # --- Verify ---
    verified_version=$(verify_install || true)
    if [ -n "$verified_version" ]; then
        success "Verified: conductor v${verified_version} responds correctly"
    else
        warn "Could not run conductor --version after install (PATH may need a fresh shell)."
    fi

    printf '\n  Run \033[1mconductor --help\033[0m to get started.\n'
    printf '  Run \033[1mconductor update\033[0m to check for future updates.\n\n'
}

main
