// ConductorError.cs — .NET helper for raising typed Conductor error
// envelopes from script-type workflow nodes.
//
// Contract: write a single JSON object to the CONDUCTOR_ERROR_OUT env
// variable's file path and exit 0. Conductor reads the file, treats
// the node as raised, and evaluates on_error routes against the
// envelope.
//
// Usage (drop into a script-type node's project):
//   ConductorError.Raise(
//       kind: "external.git.fetch_failed",
//       message: "remote rejected push",
//       details: new { remote = "origin", exit = 128 });
//   return 0;
//
// Targets net6.0+ for System.Text.Json. Raise does NOT call
// Environment.Exit on its own; callers stay in charge of process exit.

using System;
using System.IO;
using System.Text.Json;

public static class ConductorError
{
    public static string Raise(string kind, string message, object? details = null)
    {
        if (string.IsNullOrEmpty(kind))
        {
            throw new ArgumentException("kind is required and must be non-empty", nameof(kind));
        }
        if (message is null)
        {
            throw new ArgumentNullException(nameof(message));
        }

        var path = Environment.GetEnvironmentVariable("CONDUCTOR_ERROR_OUT");
        if (string.IsNullOrEmpty(path))
        {
            throw new InvalidOperationException(
                "CONDUCTOR_ERROR_OUT is not set; this script must be run by Conductor as a script-type node.");
        }

        object envelope = details is null
            ? new { conductor_error = true, kind, message }
            : new { conductor_error = true, kind, message, details };

        File.WriteAllText(path, JsonSerializer.Serialize(envelope));
        return path;
    }
}
