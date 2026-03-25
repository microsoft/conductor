#!/bin/sh
# Conductor installer for macOS and Linux
# Usage: curl -sSfL https://aka.ms/conductor/install.sh | sh
#
# This script:
#   1. Checks for uv (installs it if missing)
#   2. Fetches the latest Conductor release from GitHub
#   3. Downloads and verifies the constraints file (SHA-256)
#   4. Installs Conductor via uv tool install with pinned dependencies

set -eu

REPO="microsoft/conductor"
GITHUB_API="https://api.github.com/repos/${REPO}/releases/latest"
GITHUB_DL="https://github.com/${REPO}/releases/download"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

info() {
    printf '  \033[1;34m→\033[0m %s\n' "$1"
}

success() {
    printf '  \033[1;32m✓\033[0m %s\n' "$1"
}

error() {
    printf '  \033[1;31m✗\033[0m %s\n' "$1" >&2
    exit 1
}

need_cmd() {
    if ! command -v "$1" > /dev/null 2>&1; then
        return 1
    fi
}

# ---------------------------------------------------------------------------
# Download helper — works with curl or wget
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# SHA-256 verification — works on macOS and Linux
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    printf '\n\033[1mConductor Installer\033[0m\n\n'

    # --- uv ---
    if ! need_cmd uv; then
        info "uv not found — installing…"
        curl -sSfL https://astral.sh/uv/install.sh | sh
        # Source the env so uv is on PATH for the rest of this script
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

    # --- Detect latest release ---
    info "Fetching latest release…"
    release_json=$(download_stdout "$GITHUB_API")
    tag_name=$(printf '%s' "$release_json" | grep -o '"tag_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | cut -d'"' -f4)

    if [ -z "$tag_name" ]; then
        error "Could not determine latest release tag from GitHub API."
    fi

    success "Latest release: ${tag_name}"

    # --- Check existing installation ---
    if need_cmd conductor; then
        current_version=$(conductor --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+[^ ]*' | head -1)
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

    # --- Download constraints + checksum ---
    tmpdir=$(mktemp -d)
    trap 'rm -rf "$tmpdir"' EXIT

    info "Downloading constraints…"
    download "${GITHUB_DL}/${tag_name}/constraints.txt" "${tmpdir}/constraints.txt"
    download "${GITHUB_DL}/${tag_name}/constraints.txt.sha256" "${tmpdir}/constraints.txt.sha256"

    # --- Verify checksum ---
    info "Verifying checksum…"
    expected_hash=$(cut -d' ' -f1 "${tmpdir}/constraints.txt.sha256")
    verify_checksum "${tmpdir}/constraints.txt" "$expected_hash"
    success "Checksum verified"

    # --- Install ---
    info "Installing Conductor ${tag_name}…"
    uv tool install --force "git+https://github.com/${REPO}.git@${tag_name}" \
        -c "${tmpdir}/constraints.txt"

    success "Conductor ${tag_name} installed"
    printf '\n  Run \033[1mconductor --help\033[0m to get started.\n'
    printf '  Run \033[1mconductor update\033[0m to check for future updates.\n\n'
}

main
