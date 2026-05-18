"""JSONL event log subscriber for structured workflow diagnostics.

Subscribes to the ``WorkflowEventEmitter`` and writes every event as a
JSON line to a file in ``$TMPDIR/conductor/``.  The log file is always
created — no CLI flag required — so diagnostic data is available for
every run.

Example::

    from conductor.engine.event_log import EventLogSubscriber

    subscriber = EventLogSubscriber(workflow_name="my-workflow")
    emitter.subscribe(subscriber.on_event)
    # ... run workflow ...
    subscriber.close()
    print(f"Logs at: {subscriber.path}")
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
from pathlib import Path
from typing import Any

from conductor.events import WorkflowEvent

logger = logging.getLogger(__name__)


def _make_json_safe(obj: Any) -> Any:
    """Recursively convert non-serializable values to strings."""
    if isinstance(obj, dict):
        return {k: _make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_make_json_safe(v) for v in obj]
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, Path):
        return str(obj)
    return str(obj)


class EventLogSubscriber:
    """Writes workflow events to a JSONL file.

    Each line is a JSON object with ``type``, ``timestamp``, and ``data``
    fields — the same shape as ``WorkflowEvent.to_dict()``.

    By default a fresh log file is created under
    ``$TMPDIR/conductor/`` with a random ``run_id`` suffix. When the
    optional ``existing_path``/``existing_run_id`` kwargs are provided
    and the file is writable, the subscriber appends to the existing log
    and reuses the run id — used by the CLI's resume flow so a workflow
    that is paused and resumed (possibly multiple times) produces a
    single continuous log instead of one file per resume generation.
    """

    def __init__(
        self,
        workflow_name: str,
        *,
        existing_path: Path | None = None,
        existing_run_id: str | None = None,
    ) -> None:
        """Initialise the subscriber.

        Args:
            workflow_name: Used in the default filename for easy
                identification when no ``existing_path`` is provided.
            existing_path: When provided alongside ``existing_run_id``
                and the file is writable, open it in append mode and
                continue writing to the original log instead of creating
                a new one. Used by ``resume_workflow_async`` so a
                resumed run produces one continuous JSONL log across
                resume generations.
            existing_run_id: The run identifier associated with
                ``existing_path``. Reused (not regenerated) so log /
                timeline correlation tools see one continuous run.
        """
        import secrets

        if (
            existing_path is not None
            and existing_run_id
            and existing_path.exists()
            and existing_path.is_file()
        ):
            try:
                # Append mode preserves the original events; rely on the
                # caller (the dashboard replay step) to seed the in-memory
                # state from the existing contents.
                self._handle = open(existing_path, "a", encoding="utf-8")  # noqa: SIM115
                self._path = existing_path
                self._run_id = existing_run_id
                return
            except OSError:
                logger.warning(
                    "Cannot append to existing event log %s; creating a new log instead",
                    existing_path,
                    exc_info=True,
                )

        ts = time.strftime("%Y%m%d-%H%M%S")
        # Append random suffix to avoid filename collisions
        # when multiple runs start in the same second
        self._run_id = secrets.token_hex(4)
        ts = f"{ts}-{self._run_id}"
        self._path = (
            Path(tempfile.gettempdir())
            / "conductor"
            / f"conductor-{workflow_name}-{ts}.events.jsonl"
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self._path, "w", encoding="utf-8")  # noqa: SIM115

    @property
    def run_id(self) -> str:
        """Unique run identifier (8-char hex)."""
        return self._run_id

    @property
    def path(self) -> Path:
        """Path to the JSONL log file."""
        return self._path

    def on_event(self, event: WorkflowEvent) -> None:
        """Write a single event as a JSON line.

        Safe to call from any thread — individual ``write`` + ``flush``
        calls are atomic at the OS level for lines under PIPE_BUF.
        """
        if self._handle is None or self._handle.closed:
            return
        try:
            line = json.dumps(_make_json_safe(event.to_dict()), separators=(",", ":"))
            self._handle.write(line + "\n")
            self._handle.flush()
        except Exception:
            logger.debug("Failed to write event to log", exc_info=True)

    def close(self) -> None:
        """Close the log file handle."""
        if self._handle is not None and not self._handle.closed:
            self._handle.close()
