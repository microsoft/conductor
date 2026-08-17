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
# Private / mirrored package index:
#   uv resolves Conductor's dependencies from https://pypi.org/simple by
#   default. On networks that block the public index, export UV_DEFAULT_INDEX
#   (uv's own setting — this script does not need a Conductor-specific flag)
#   before running the installer:
#
#       export UV_DEFAULT_INDEX="internal=https://<your-index-host>/simple/"
#
#   uv does NOT read pip's configuration, so `pip config set global.index-url`
#   alone has no effect here.
#
# Test hooks (used by tests/integration/test_install_scripts.py):
#   --source <path-or-url>    OR   $CONDUCTOR_INSTALL_SOURCE
#       Install from this source (wheel path, directory, or git+ URL) instead
#       of the latest GitHub release. Skips constraints download.
#   --auto-stop               OR   $CONDUCTOR_INSTALL_AUTO_STOP=1
#       If other conductor processes are running, stop them and continue
#       without prompting. Without this flag, the script prompts when a TTY
#       is available, or aborts when running non-interactively.
#   --force                   OR   $CONDUCTOR_INSTALL_FORCE=1
#       Skip the running-process check entirely.
#   --skip-path-update        OR   $CONDUCTOR_INSTALL_SKIP_PATH_UPDATE=1
#       Skip the `uv tool update-shell` step so the install never edits the
#       user's shell profiles (.zshenv/.bashrc/…). The install-script tests
#       install into a throwaway UV_TOOL_BIN_DIR that must never leak into the
#       real environment (note: `uv tool update-shell` intentionally modifies
#       the shell and so ignores UV_NO_MODIFY_PATH).
#   --extras <a,b>            OR   $CONDUCTOR_INSTALL_EXTRAS
#       Comma-separated optional extras to install (tui, aca,
#       claude-agent-sdk). These are *added to* the extras already recorded
#       in the existing install's uv receipt, which are preserved
#       automatically -- `uv tool install --force` rewrites the tool's whole
#       requirement set, so an upgrade that named no extras used to silently
#       uninstall `[tui]`/`[aca]` (issue #441).
#   --no-preserve-extras      OR   $CONDUCTOR_INSTALL_NO_PRESERVE_EXTRAS=1
#       Do not carry the existing install's extras forward. Use this to drop
#       back to a bare install.

set -eu

REPO="microsoft/conductor"
GITHUB_API="https://api.github.com/repos/${REPO}/releases/latest"
GITHUB_DL="https://github.com/${REPO}/releases/download"

# ---------------------------------------------------------------------------
# Argument + env parsing
# ---------------------------------------------------------------------------

SOURCE="${CONDUCTOR_INSTALL_SOURCE:-}"
AUTO_STOP="${CONDUCTOR_INSTALL_AUTO_STOP:-0}"
FORCE_FLAG="${CONDUCTOR_INSTALL_FORCE:-0}"
SKIP_PATH_UPDATE="${CONDUCTOR_INSTALL_SKIP_PATH_UPDATE:-0}"
EXTRAS="${CONDUCTOR_INSTALL_EXTRAS:-}"
NO_PRESERVE_EXTRAS="${CONDUCTOR_INSTALL_NO_PRESERVE_EXTRAS:-0}"

# Set by uv_install_with_retry: `network` or `other`. Declared here so the
# script stays safe under `set -u` if the install is never attempted.
INSTALL_FAILURE_KIND="other"

while [ $# -gt 0 ]; do
    case "$1" in
        --source)           SOURCE="$2"; shift 2 ;;
        --source=*)         SOURCE="${1#--source=}"; shift ;;
        --auto-stop)        AUTO_STOP=1; shift ;;
        --force)            FORCE_FLAG=1; shift ;;
        --skip-path-update) SKIP_PATH_UPDATE=1; shift ;;
        --extras)           EXTRAS="$2"; shift 2 ;;
        --extras=*)         EXTRAS="${1#--extras=}"; shift ;;
        --no-preserve-extras) NO_PRESERVE_EXTRAS=1; shift ;;
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

# Report the package index uv will resolve against, if the environment
# overrides the default. uv reads UV_DEFAULT_INDEX (current) and UV_INDEX_URL
# (deprecated but still honored); it does NOT read pip's configuration.
# Printing it makes "did my override actually apply?" answerable from the
# install log alone. Any userinfo is stripped first: an index URL is a
# documented place to put credentials, and this line reaches the terminal
# and every CI log that captures the installer.
redact_url_credentials() {
    printf '%s' "$1" | sed 's#://[^/@]*@#://***@#'
}

report_index_override() {
    if [ -n "${UV_DEFAULT_INDEX:-}" ]; then
        info "Package index (UV_DEFAULT_INDEX): $(redact_url_credentials "$UV_DEFAULT_INDEX")"
    elif [ -n "${UV_INDEX_URL:-}" ]; then
        info "Package index (UV_INDEX_URL): $(redact_url_credentials "$UV_INDEX_URL")"
    fi
}

# True when uv could not reach the *git remote* it fetches Conductor itself
# from. Checked before the index classifier because uv reports a failed git
# fetch with the same "failed to fetch" wording it uses for the index, and
# the remedies are completely different: no index setting fixes a blocked
# github.com. Must be called in a conditional (it returns non-zero as a
# normal answer).
is_git_host_failure() {
    [ -f "$1" ] || return 1
    LC_ALL=C tr '[:upper:]' '[:lower:]' < "$1" | LC_ALL=C grep -qF 'git operation failed'
}

# True when uv's output looks like the package index was unreachable rather
# than a transient local failure: a blocked/filtered network, an intercepting
# proxy, or a TLS-inspecting middlebox. Deliberately generic — Conductor ships
# no default mirror and makes no assumption about whose network this is.
#
# Every needle names a *failure*, never merely a host. Matching on `pypi.org`
# alone reported an integrity failure ("hash mismatch for
# https://files.pythonhosted.org/...") as an unreachable index, which is the
# worst available answer for a supply-chain signal.
#
# Split into two tiers, because "skip the retries" and "explain the failure"
# need different evidence:
#
#   definitive — uv states it gave up, or the condition is a policy, DNS, or
#                TLS fact that a 2-second backoff cannot change. Safe to stop
#                retrying immediately.
#   transient  — connection-level failures that a retry may genuinely fix (a
#                VPN blip, a reset mid-download). These still produce index
#                guidance if they are what the run *finally* failed on, but
#                they never cut the retry schedule short.
#
# Must be called in a conditional (they return non-zero as a normal answer).
is_definitive_index_block() {
    [ -f "$1" ] || return 1
    is_git_host_failure "$1" && return 1
    LC_ALL=C tr '[:upper:]' '[:lower:]' < "$1" | LC_ALL=C grep -qF \
        -e 'failed to fetch' \
        -e 'request failed after' \
        -e '401 unauthorized' \
        -e '403 forbidden' \
        -e '407 proxy authentication required' \
        -e 'dns error' \
        -e 'invalid peer certificate' \
        -e 'self-signed certificate' \
        -e 'certificate verify failed' \
        -e 'proxyerror' \
        -e 'tls connect error'
}

is_network_block_error() {
    [ -f "$1" ] || return 1
    is_git_host_failure "$1" && return 1
    is_definitive_index_block "$1" && return 0
    LC_ALL=C tr '[:upper:]' '[:lower:]' < "$1" | LC_ALL=C grep -qF \
        -e 'error sending request' \
        -e 'connection refused' \
        -e 'connection reset' \
        -e 'operation timed out'
}

# Run `uv tool install` with retry+backoff. Appends combined stdout+stderr of
# every attempt to LOG_FILE and returns the final exit code. Sets
# INSTALL_FAILURE_KIND to `network`, `git`, or `other` so the caller can print
# guidance that matches the actual failure (sh functions can only return an
# exit status).
#
# Classification reads only the *most recent* attempt, never the accumulated
# log: one transient blip anywhere in a run would otherwise relabel whatever
# the run finally failed on.
uv_install_with_retry() {
    install_source="$1"
    constraints="$2"  # may be empty
    log_file="$3"
    attempt_log="${log_file}.attempt"

    ec=0
    attempt=0
    INSTALL_FAILURE_KIND="other"
    : > "$log_file"

    # A leading 0 makes the first attempt share the loop body without sleeping.
    for d in 0 2 5 10; do
        attempt=$((attempt + 1))
        if [ "$d" != "0" ]; then
            info "Retrying install (attempt ${attempt}) after ${d}s…"
            sleep "$d"
            printf '\n--- attempt %s ---\n' "$attempt" >>"$log_file"
        fi

        set +e
        if [ -n "$constraints" ]; then
            uv tool install --force "$install_source" -c "$constraints" >"$attempt_log" 2>&1
        else
            uv tool install --force "$install_source" >"$attempt_log" 2>&1
        fi
        ec=$?
        set -e
        cat "$attempt_log" >>"$log_file" 2>/dev/null || true

        [ "$ec" -eq 0 ] && return 0

        # A definitively blocked index does not heal on a retry — uv has
        # already exhausted its own retries by this point. Backing off three
        # more times just delays the only message that helps, so stop here and
        # let the caller explain. Connection-level failures are deliberately
        # excluded: a VPN blip really can heal, and cutting the schedule short
        # for one would trade a working install for a faster wrong answer. A
        # git failure is excluded too, since uv does not retry git fetches
        # internally, so a retry there is genuinely useful.
        if is_definitive_index_block "$attempt_log"; then
            INSTALL_FAILURE_KIND="network"
            return "$ec"
        fi
    done

    # The retries are spent. Classify whatever the run *finally* failed on.
    if is_network_block_error "$attempt_log"; then
        INSTALL_FAILURE_KIND="network"
    elif is_git_host_failure "$attempt_log"; then
        INSTALL_FAILURE_KIND="git"
    fi
    return "$ec"
}

# Guidance for a failure that looks like a blocked/unreachable package index.
print_index_guidance() {
    printf '\n  \033[1mA package index this installer needs could not be reached.\033[0m\n\n' >&2
    printf '  The most common cause is a network that blocks direct access to the\n' >&2
    printf '  public Python package index (pypi.org / files.pythonhosted.org).\n' >&2
    printf '  Managed or corporate devices often require all packages to come from\n' >&2
    printf '  an internal mirror.\n\n' >&2
    if [ -n "${UV_DEFAULT_INDEX:-}${UV_INDEX_URL:-}" ]; then
        printf '  An index override is already set (shown above), so uv did not use the\n' >&2
        printf '  public index. Check that the URL is correct and reachable from this\n' >&2
        printf '  machine, and that any credentials it needs are supplied.\n\n' >&2
    else
        printf '  If your organization provides a PyPI mirror or proxy, point uv at it\n' >&2
        printf '  and re-run this installer:\n\n' >&2
        printf '      export UV_DEFAULT_INDEX="internal=https://<your-index-host>/simple/"\n' >&2
        printf '      curl -sSfL https://aka.ms/conductor/install.sh | sh\n\n' >&2
        printf '  Add the same export to your shell profile so future installs and\n' >&2
        printf "  'conductor update --apply' inherit it.\n\n" >&2
    fi
    printf '  Notes:\n' >&2
    printf "    * uv does not read pip's configuration. Setting\n" >&2
    printf '      "pip config set global.index-url ..." alone has no effect here.\n' >&2
    printf '    * Ask your IT or platform team for the index URL. Conductor ships no\n' >&2
    printf '      default mirror and never redirects your packages on its own.\n' >&2
    printf '    * For credentials, name the index (the "internal=" prefix above) and\n' >&2
    printf '      set UV_INDEX_INTERNAL_USERNAME / UV_INDEX_INTERNAL_PASSWORD, or\n' >&2
    printf '      embed them in the URL.\n' >&2
    printf '    * If the error mentions a certificate, the network is inspecting TLS.\n' >&2
    printf '      Trust your organization CA via SSL_CERT_FILE, or set UV_NATIVE_TLS=1.\n' >&2
    printf '    * If the error mentions a proxy (407), set HTTPS_PROXY / NO_PROXY.\n\n' >&2
    printf '  Details: https://github.com/microsoft/conductor#installing-behind-a-proxy-or-private-package-index\n\n' >&2
}

# Guidance for a failure fetching Conductor's own git repository. Kept
# separate from the index guidance because no index setting can fix it, and
# saying "your package index is blocked" would send the user to configure
# something that is not the problem.
print_git_host_guidance() {
    printf '\n  \033[1mConductor'"'"'s source repository could not be reached.\033[0m\n\n' >&2
    printf '  This installer fetches Conductor itself from github.com. The error\n' >&2
    printf '  above is a git failure, not a package-index failure, so setting\n' >&2
    printf '  UV_DEFAULT_INDEX will not help.\n\n' >&2
    printf '  Check that this machine can reach github.com — including any proxy\n' >&2
    printf '  (HTTPS_PROXY / NO_PROXY) or TLS inspection your network requires.\n\n' >&2
    printf '  Details: https://github.com/microsoft/conductor#installing-behind-a-proxy-or-private-package-index\n\n' >&2
}

# The directory uv keeps tool venvs in, or empty when uv cannot say.
uv_tools_dir() {
    uv tool dir 2>/dev/null | head -n1 || true
}

# Run `conductor --version` from the freshly installed location and return the
# version string (or empty on failure).
verify_install() {
    tools_dir=$(uv_tools_dir)
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

# Path to the existing install's uv tool receipt, or empty if there isn't one.
# Probes both directory names verify_install knows about, so the two functions
# agree about where the tool lives.
receipt_path() {
    tools_dir=$(uv_tools_dir)
    [ -n "$tools_dir" ] || return 0
    for name in conductor-cli conductor; do
        if [ -f "${tools_dir}/${name}/uv-receipt.toml" ]; then
            printf '%s' "${tools_dir}/${name}/uv-receipt.toml"
            return 0
        fi
    done
}

# Read the extras recorded in the existing install's uv tool receipt.
#
# `uv tool install --force` replaces the tool's entire requirement set, so an
# upgrade that names no extras silently uninstalls `[tui]`/`[aca]` (issue
# #441). The receipt is the only authoritative record of what the current
# install carries; the CLI's `conductor.install_hint` reads the same file to
# build its install hints, so the two must agree.
#
# Flattens newlines (uv may wrap the requirements array), splits the array
# into one requirement object per line, keeps this distribution's entry --
# matched on the whole `name = "conductor-cli"` field, not as a substring, so
# a `conductor-cli-plugin` requirement is not mistaken for it -- and extracts
# its extras. Field order inside the object and additional `uv tool install
# --with` requirements are both tolerated.
# Returns 0 with the extras (possibly empty) when the receipt was understood,
# and non-zero when one exists but could not be read. That distinction is the
# whole point: an unreadable receipt is NOT a bare install, and treating it as
# one rebuilds the tool without the extras it records -- silently causing the
# very data loss this change exists to prevent. `conductor.install_hint`'s
# `read_receipt` draws the same line with `ReceiptContents.readable`.
#
# `-i` on the name match because the Python reader normalises per PEP 503; a
# case-sensitive match here would find nothing for a receipt recording
# `Conductor-CLI` and drop the extras.
receipt_extras() {
    receipt=$(receipt_path)
    [ -n "$receipt" ] || return 0
    [ -r "$receipt" ] || return 1
    flat=$(tr '\n' ' ' < "$receipt") || return 1
    entry=$(printf '%s' "$flat" | tr '}' '\n' \
        | grep -Ei 'name[[:space:]]*=[[:space:]]*"conductor[-_.]cli"' \
        | head -n1) || return 1
    [ -n "$entry" ] || return 1
    printf '%s' "$entry" \
        | sed -n 's/.*extras[[:space:]]*=[[:space:]]*\[\([^]]*\)\].*/\1/p' \
        | tr -d "\"' " \
        || true
}

# Merge comma-separated extras lists, dropping blanks/duplicates and sorting
# so the generated spec is stable between runs (and comparable as a string).
#
# Lower-cases before deduplicating to match `install.ps1`'s Merge-Extras,
# whose Sort-Object -Unique is case-insensitive. Without this the two
# installers disagree, and `--extras TUI` against a recorded `tui` would make
# the up-to-date comparison below never converge.
merge_extras() {
    printf '%s,%s' "$1" "$2" \
        | tr ',' '\n' \
        | tr '[:upper:]' '[:lower:]' \
        | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' \
        | grep -v '^$' \
        | sort -u \
        | tr '\n' ',' \
        | sed 's/,$//' \
        || true
}

# Refuse an extra this package does not declare. uv treats an unknown extra as
# a *warning* and still exits 0, so without this a typo installs nothing and
# reports success -- the same "confident command that does not work" this
# whole change exists to remove.
#
# Split with parameter expansion rather than a `... | while` pipeline: the
# loop body would run in a subshell there, so `error`'s `exit 1` would kill
# only the subshell and the install would carry on with the bad extra.
validate_extras() {
    _rest="$1"
    while [ -n "$_rest" ]; do
        _name="${_rest%%,*}"
        case "$_rest" in
            *,*) _rest="${_rest#*,}" ;;
            *)   _rest="" ;;
        esac
        [ -n "$_name" ] || continue
        _name=$(printf '%s' "$_name" | tr '[:upper:]' '[:lower:]')
        case "$_name" in
            tui|aca|claude-agent-sdk) ;;
            *) error "unknown extra '${_name}' (available: tui, aca, claude-agent-sdk)" ;;
        esac
    done
}

# Wrap an install source in a PEP 508 direct reference carrying the extras.
# uv accepts a git+ URL, a local directory, or a wheel path on the right-hand
# side of the `@`.
apply_extras() {
    if [ -n "$2" ]; then
        printf 'conductor-cli[%s] @ %s' "$2" "$1"
    else
        printf '%s' "$1"
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

    # --- Extras: carry the existing install's extras forward, plus any requested ---
    #
    # `receipt_now` is what is installed; `existing_extras` is what we choose
    # to carry. They differ under --no-preserve-extras, and the up-to-date
    # check below has to compare against the former -- comparing against the
    # latter made the opt-out a no-op, since the flag zeroes it and both sides
    # then match.
    validate_extras "$EXTRAS"
    receipt_readable=1
    raw_extras=$(receipt_extras) || receipt_readable=0
    receipt_now=$(merge_extras "$raw_extras" "")
    if [ "$receipt_readable" = "0" ]; then
        # Warn and keep going rather than aborting: a broken receipt is
        # exactly the state a reinstall is meant to repair, so refusing would
        # take away the remedy. But say plainly that extras cannot be carried,
        # because the alternative -- proceeding silently -- is how [tui]/[aca]
        # disappear without anyone noticing.
        warn "Could not read the existing install's uv receipt; extras cannot be preserved."
        warn "Re-run with --extras <a,b> to reinstate any you had."
    fi
    existing_extras=""
    [ "$NO_PRESERVE_EXTRAS" = "1" ] || existing_extras="$receipt_now"
    resolved_extras=$(merge_extras "$existing_extras" "$EXTRAS")
    if [ -n "$resolved_extras" ]; then
        info "Including extras: ${resolved_extras}"
        install_source=$(apply_extras "$install_source" "$resolved_extras")
    elif [ -n "$receipt_now" ]; then
        info "Dropping extras: ${receipt_now}"
    fi

    # --- Existing-install check (only for the GitHub-release path) ---
    if [ -z "$SOURCE" ] && need_cmd conductor; then
        current_version=$(conductor --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+[^ ]*' | head -1 || true)
        if [ -n "$current_version" ]; then
            latest_version=$(printf '%s' "$tag_name" | sed 's/^v//')
            # An already-current version is only a no-op when the extras on
            # disk already match what this run would install: `--extras tui`
            # (or --no-preserve-extras) still has work to do, and reporting
            # "up to date" would silently skip it.
            if [ "$current_version" = "$latest_version" ] \
                && [ "$resolved_extras" = "$receipt_now" ] \
                && [ "$receipt_readable" = "1" ]; then
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
            printf "  Stop them ('conductor stop --all' for background dashboards),\n"
            printf "  re-run with --auto-stop to stop them automatically,\n"
            printf "  or re-run with --force to skip this check entirely.\n\n"

            should_stop=0
            if [ "$AUTO_STOP" = "1" ]; then
                should_stop=1
            elif [ ! -t 0 ] || [ ! -r /dev/tty ]; then
                # No TTY (curl | sh from a script, CI pipe, etc.) — refuse to guess.
                error "Aborted (other Conductor processes running; re-run with --auto-stop to stop them, or --force to skip the check)."
            else
                printf '  Stop them now and continue? [y/N] '
                read -r ans </dev/tty || ans=''
                case "$ans" in
                    y|Y|yes|YES) should_stop=1 ;;
                    *) error "Aborted." ;;
                esac
            fi

            if [ "$should_stop" = "1" ]; then
                printf '%s\n' "$running" | while IFS= read -r line; do
                    pid=$(printf '%s' "$line" | awk '{print $1}')
                    if kill "$pid" 2>/dev/null; then
                        success "Stopped PID ${pid}"
                    else
                        warn "Could not stop PID ${pid}"
                    fi
                done
                sleep 1
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
    report_index_override
    log_file="${tmpdir}/uv-install.log"
    if ! uv_install_with_retry "$install_source" "$constraints_file" "$log_file"; then
        printf '\n  ── uv tool install output ──\n' >&2
        if [ -s "$log_file" ]; then
            sed 's/^/  /' "$log_file" >&2
        else
            printf '  (no output captured)\n' >&2
        fi
        printf '\n' >&2
        case "${INSTALL_FAILURE_KIND:-other}" in
            network)
                print_index_guidance
                error "uv tool install failed: could not reach the package index"
                ;;
            git)
                print_git_host_guidance
                error "uv tool install failed: could not reach github.com"
                ;;
            *)
                error "uv tool install failed after retries"
                ;;
        esac
    fi
    # uv reports an extra it does not recognise as a warning and still exits
    # 0, and the log is only shown on failure -- so without this the run ends
    # in a green checkmark having installed nothing the user asked for.
    if grep -q 'does not have an extra named' "$log_file" 2>/dev/null; then
        grep 'does not have an extra named' "$log_file" | while IFS= read -r line; do
            warn "$line"
        done
    fi
    success "Conductor ${display_version} installed"

    # --- Update shell PATH ---
    if [ "$SKIP_PATH_UPDATE" = "1" ]; then
        info "Skipping shell PATH update (CONDUCTOR_INSTALL_SKIP_PATH_UPDATE=1)."
    else
        info "Ensuring conductor is on PATH for new shells…"
        if uv tool update-shell >/dev/null 2>&1; then
            success "PATH updated (restart your shell to pick up the change)"
        else
            warn "Could not update shell PATH automatically. Run 'uv tool update-shell' manually."
        fi
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
