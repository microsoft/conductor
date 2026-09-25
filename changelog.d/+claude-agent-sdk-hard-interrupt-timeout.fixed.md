Interrupting a `claude-agent-sdk` agent, or hitting its `max_session_seconds`
limit, no longer waits for the SDK to deliver another message before taking
effect — a run blocked waiting on the model now stops when asked. The session
limit is a single deadline measured from the start of the execution, so a
steady stream of messages can no longer extend it indefinitely.

Conductor now also owns the shutdown of the Claude CLI session rather than
leaving it to the SDK's own stream teardown, which an interrupt or an expired
deadline could previously cut short and leave a CLI process running. Shutdown
is never cancelled, is retried once if it does not complete, and finishes
before that execution's temporary MCP configuration is deleted. If it cannot
be confirmed, the failure is logged; on a run that would otherwise have
succeeded the result is discarded and a non-retryable error is raised instead
of reporting success while the CLI may still be running.

Two consequences are worth knowing: a signal raised while the CLI is still
starting up takes effect only once startup finishes, and returning after an
interrupt or a timeout may take additional time while the SDK releases its
resources. Startup is normally quick, but nothing places an overall limit on
either delay.
