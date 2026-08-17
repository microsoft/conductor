"""Fleet Manager — run discovery and (eventually) TUI for local Conductor runs.

This package implements the run-record primitives described in
``docs/projects/fleet-manager/fleet-manager.design.md`` ("The fix"): a
``run_id``-keyed record written for every ``conductor run`` (foreground,
foreground-with-dashboard, or background), so that ``conductor stop`` and a
future ``conductor fleet`` TUI can discover and act on runs regardless of how
they were launched.

See :mod:`conductor.fleet.records` for the write/read/prune primitives.
"""

from conductor.fleet.records import (
    RunRecord,
    read_run_record,
    read_run_records,
    remove_run_record,
    remove_run_record_for_current_process,
    run_records_dir,
    write_run_record,
)

__all__ = [
    "RunRecord",
    "read_run_record",
    "read_run_records",
    "remove_run_record",
    "remove_run_record_for_current_process",
    "run_records_dir",
    "write_run_record",
]
