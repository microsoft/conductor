"""Event system for Conductor workflow execution.

This module provides the pub/sub event system that decouples workflow
execution events from output rendering, enabling multiple simultaneous
consumers (console logging, web dashboard, etc.).

Example:
    Create an emitter and subscribe to events::

        emitter = WorkflowEventEmitter()
        emitter.subscribe(lambda event: print(event.type))
        emitter.emit(WorkflowEvent(type="agent_started", timestamp=time.time(), data={}))
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkflowEvent:
    """An event emitted during workflow execution.

    Attributes:
        type: The event type identifier (e.g., "agent_started", "workflow_completed").
        timestamp: Unix timestamp when the event was created.
        data: Event-specific payload data.
    """

    type: str
    timestamp: float
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the event to a dictionary.

        Returns:
            Dictionary with type, timestamp, and data fields.
        """
        return {
            "type": self.type,
            "timestamp": self.timestamp,
            "data": self.data,
        }


class WorkflowEventEmitter:
    """Pub/sub event emitter for workflow execution events.

    Subscribers are called synchronously in registration order when an
    event is emitted. A threading.Lock protects the subscriber list during
    iteration to prevent corruption from concurrent modifications.

    Note:
        The Lock protects only the emitter's own subscriber list. It does
        NOT make downstream consumers (e.g., asyncio.Queue.put_nowait())
        thread-safe. In the current single-threaded asyncio architecture
        this is fine. See NFR-2 in the implementation plan.
    """

    def __init__(self) -> None:
        """Initialize the event emitter with an empty subscriber list."""
        self._subscribers: list[Callable[[WorkflowEvent], None]] = []
        self._lock = threading.Lock()

    def subscribe(self, callback: Callable[[WorkflowEvent], None]) -> None:
        """Register a callback to receive events.

        Args:
            callback: Function called with each emitted WorkflowEvent.
        """
        with self._lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[WorkflowEvent], None]) -> None:
        """Remove a previously registered callback.

        Args:
            callback: The callback to remove. No-op if not found.
        """
        with self._lock, contextlib.suppress(ValueError):
            self._subscribers.remove(callback)

    def emit(self, event: WorkflowEvent) -> None:
        """Emit an event to all registered subscribers.

        Callbacks are invoked synchronously in registration order. If a
        callback raises an exception, it is logged and the remaining
        callbacks still execute.

        Args:
            event: The event to broadcast.
        """
        with self._lock:
            subscribers = list(self._subscribers)

        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                logger.exception(
                    "Event subscriber raised an exception for event '%s'",
                    event.type,
                )
