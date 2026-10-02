**Docker execution backend for containerized script steps**: executes `type: script`
workflow steps inside short-lived containers against a run-scoped shared named
volume (`conductor-ws-<run_id>`). Execution environment documents declare `docker`
profiles with configurable container image, target platform, network mode,
container user, init process, read-only root filesystem, Linux capability drops,
privilege escalation blocks, memory-backed `/tmp` tmpfs mounts, and CPU, memory,
and PID resource constraints. Conductor automatically collects the workflow's
content-addressed run bundle closure and stages it into the shared volume before
execution, isolating script steps while preserving shared workspace state across
steps within the run.
