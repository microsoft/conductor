#!/usr/bin/env bash
# conductor-error.sh — Bash/sh helper for raising typed Conductor error
# envelopes from script-type workflow nodes.
#
# Contract: write a single JSON object to $CONDUCTOR_ERROR_OUT and exit
# 0. Conductor reads the file, treats the node as raised, and evaluates
# on_error routes against the envelope.
#
# Usage (source then call):
#   . ./conductor-error.sh
#   conductor_error "external.git.fetch_failed" \
#                   "remote rejected push" \
#                   '{"remote":"origin","exit":128}'
#   exit 0
#
# Arguments:
#   $1 = kind     (required, dotted-namespace string e.g. "external.git.drift")
#   $2 = message  (required, human-readable)
#   $3 = details  (optional, raw JSON object; defaults to omitted)
#
# Notes:
#   - $3 is inlined verbatim. The caller is responsible for valid JSON.
#   - We use a tiny python one-liner to escape kind/message safely.
#     Python is assumed available; if not, fall back to jq or printf.

conductor_error() {
    if [ -z "${CONDUCTOR_ERROR_OUT:-}" ]; then
        echo "conductor-error: CONDUCTOR_ERROR_OUT is not set; this script must be run by Conductor as a script-type node." >&2
        return 1
    fi
    if [ $# -lt 2 ]; then
        echo "conductor-error: usage: conductor_error <kind> <message> [<details-json>]" >&2
        return 1
    fi

    _ce_kind="$1"
    _ce_msg="$2"
    _ce_details="${3:-}"

    if [ -n "$_ce_details" ]; then
        python3 - "$_ce_kind" "$_ce_msg" "$_ce_details" "$CONDUCTOR_ERROR_OUT" <<'PY'
import json, sys
kind, msg, details_raw, out = sys.argv[1:5]
envelope = {"conductor_error": True, "kind": kind, "message": msg, "details": json.loads(details_raw)}
with open(out, "w", encoding="utf-8") as f:
    json.dump(envelope, f)
PY
    else
        python3 - "$_ce_kind" "$_ce_msg" "$CONDUCTOR_ERROR_OUT" <<'PY'
import json, sys
kind, msg, out = sys.argv[1:4]
envelope = {"conductor_error": True, "kind": kind, "message": msg}
with open(out, "w", encoding="utf-8") as f:
    json.dump(envelope, f)
PY
    fi
}
