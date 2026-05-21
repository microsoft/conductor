// conductor-error.mjs — Node.js helper for raising typed Conductor
// error envelopes from script-type workflow nodes.
//
// Contract: write a single JSON object to process.env.CONDUCTOR_ERROR_OUT
// and exit 0. Conductor reads the file, treats the node as raised, and
// evaluates on_error routes against the envelope.
//
// Usage:
//   import { raiseError } from "./conductor-error.mjs";
//   raiseError({
//     kind: "external.git.fetch_failed",
//     message: "remote rejected push",
//     details: { remote: "origin", exit: 128 },
//   });
//   process.exit(0);
//
// raiseError does NOT call process.exit on its own; callers stay in
// charge of process exit so they can do their own teardown first.

import { writeFileSync } from "node:fs";

/**
 * @param {{ kind: string, message: string, details?: Record<string, unknown> }} envelope
 * @returns {string} the path the envelope was written to
 */
export function raiseError({ kind, message, details } = {}) {
  if (typeof kind !== "string" || kind.length === 0) {
    throw new TypeError("raiseError: 'kind' is required and must be a non-empty string");
  }
  if (typeof message !== "string") {
    throw new TypeError("raiseError: 'message' is required and must be a string");
  }

  const out = process.env.CONDUCTOR_ERROR_OUT;
  if (!out) {
    throw new Error(
      "CONDUCTOR_ERROR_OUT is not set; this script must be run by Conductor as a script-type node."
    );
  }

  /** @type {Record<string, unknown>} */
  const payload = {
    conductor_error: true,
    kind,
    message,
  };
  if (details !== undefined) {
    payload.details = details;
  }

  writeFileSync(out, JSON.stringify(payload), { encoding: "utf8" });
  return out;
}
