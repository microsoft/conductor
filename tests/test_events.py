"""Tests for the WorkflowEventEmitter pub/sub system."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from conductor.events import WorkflowEvent, WorkflowEventEmitter


class TestWorkflowEvent:
    """Tests for the WorkflowEvent dataclass."""

    def test_event_fields(self) -> None:
        """Test that event stores type, timestamp, and data."""
        event = WorkflowEvent(type="agent_started", timestamp=1234567890.0, data={"name": "a1"})
        assert event.type == "agent_started"
        assert event.timestamp == 1234567890.0
        assert event.data == {"name": "a1"}

    def test_event_default_data(self) -> None:
        """Test that data defaults to empty dict."""
        event = WorkflowEvent(type="test", timestamp=0.0)
        assert event.data == {}

    def test_event_is_frozen(self) -> None:
        """Test that event is immutable."""
        event = WorkflowEvent(type="test", timestamp=0.0)
        try:
            event.type = "modified"  # type: ignore[misc]
            raise AssertionError("Should not allow mutation")
        except AttributeError:
            pass

    def test_to_dict(self) -> None:
        """Test serialization to dictionary."""
        event = WorkflowEvent(type="agent_started", timestamp=123.456, data={"key": "value"})
        d = event.to_dict()
        assert d == {
            "type": "agent_started",
            "timestamp": 123.456,
            "data": {"key": "value"},
        }

    def test_to_dict_empty_data(self) -> None:
        """Test serialization with default empty data."""
        event = WorkflowEvent(type="test", timestamp=0.0)
        d = event.to_dict()
        assert d == {"type": "test", "timestamp": 0.0, "data": {}}


class TestWorkflowEventEmitter:
    """Tests for the WorkflowEventEmitter pub/sub class."""

    def test_subscribe_and_emit(self) -> None:
        """Test that subscribed callback receives emitted events."""
        emitter = WorkflowEventEmitter()
        received: list[WorkflowEvent] = []
        emitter.subscribe(received.append)

        event = WorkflowEvent(type="test", timestamp=time.time(), data={"x": 1})
        emitter.emit(event)

        assert len(received) == 1
        assert received[0] is event

    def test_multiple_subscribers(self) -> None:
        """Test that all subscribers receive the event."""
        emitter = WorkflowEventEmitter()
        received_a: list[WorkflowEvent] = []
        received_b: list[WorkflowEvent] = []
        emitter.subscribe(received_a.append)
        emitter.subscribe(received_b.append)

        event = WorkflowEvent(type="test", timestamp=time.time())
        emitter.emit(event)

        assert len(received_a) == 1
        assert len(received_b) == 1
        assert received_a[0] is event
        assert received_b[0] is event

    def test_subscriber_order(self) -> None:
        """Test that subscribers are called in registration order."""
        emitter = WorkflowEventEmitter()
        order: list[int] = []
        emitter.subscribe(lambda _: order.append(1))
        emitter.subscribe(lambda _: order.append(2))
        emitter.subscribe(lambda _: order.append(3))

        emitter.emit(WorkflowEvent(type="test", timestamp=time.time()))
        assert order == [1, 2, 3]

    def test_unsubscribe(self) -> None:
        """Test that unsubscribed callback no longer receives events."""
        emitter = WorkflowEventEmitter()
        received: list[WorkflowEvent] = []
        emitter.subscribe(received.append)
        emitter.unsubscribe(received.append)

        emitter.emit(WorkflowEvent(type="test", timestamp=time.time()))
        assert len(received) == 0

    def test_unsubscribe_unknown_callback(self) -> None:
        """Test that unsubscribing a non-registered callback is a no-op."""
        emitter = WorkflowEventEmitter()
        emitter.unsubscribe(lambda _: None)  # Should not raise

    def test_emit_with_no_subscribers(self) -> None:
        """Test that emitting with no subscribers does not raise."""
        emitter = WorkflowEventEmitter()
        emitter.emit(WorkflowEvent(type="test", timestamp=time.time()))

    def test_callback_exception_isolation(self) -> None:
        """Test that one failing callback doesn't prevent others from executing."""
        emitter = WorkflowEventEmitter()
        received: list[WorkflowEvent] = []

        def failing_callback(event: WorkflowEvent) -> None:
            raise RuntimeError("Callback failed")

        emitter.subscribe(failing_callback)
        emitter.subscribe(received.append)

        event = WorkflowEvent(type="test", timestamp=time.time())
        emitter.emit(event)

        # Second callback should still have received the event
        assert len(received) == 1
        assert received[0] is event

    def test_multiple_failing_callbacks(self) -> None:
        """Test that multiple failing callbacks don't affect healthy ones."""
        emitter = WorkflowEventEmitter()
        received: list[str] = []

        def fail_1(event: WorkflowEvent) -> None:
            raise ValueError("fail 1")

        def good(event: WorkflowEvent) -> None:
            received.append("good")

        def fail_2(event: WorkflowEvent) -> None:
            raise TypeError("fail 2")

        emitter.subscribe(fail_1)
        emitter.subscribe(good)
        emitter.subscribe(fail_2)

        emitter.emit(WorkflowEvent(type="test", timestamp=time.time()))
        assert received == ["good"]

    def test_thread_safety_concurrent_emit(self) -> None:
        """Test that concurrent emit calls don't corrupt the subscriber list."""
        emitter = WorkflowEventEmitter()
        call_count = MagicMock()
        emitter.subscribe(lambda _: call_count())

        threads = []
        barrier = threading.Barrier(10)

        def emit_events() -> None:
            barrier.wait()
            for _ in range(100):
                emitter.emit(WorkflowEvent(type="test", timestamp=time.time()))

        for _ in range(10):
            t = threading.Thread(target=emit_events)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # 10 threads × 100 emits = 1000 calls
        assert call_count.call_count == 1000

    def test_thread_safety_concurrent_subscribe(self) -> None:
        """Test that concurrent subscribe calls don't corrupt the list."""
        emitter = WorkflowEventEmitter()
        barrier = threading.Barrier(10)
        callbacks: list[MagicMock] = [MagicMock() for _ in range(10)]

        def subscribe_callback(cb: MagicMock) -> None:
            barrier.wait()
            emitter.subscribe(cb)

        threads = []
        for cb in callbacks:
            t = threading.Thread(target=subscribe_callback, args=(cb,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        event = WorkflowEvent(type="test", timestamp=time.time())
        emitter.emit(event)

        for cb in callbacks:
            cb.assert_called_once_with(event)

    def test_multiple_events(self) -> None:
        """Test that multiple events are delivered independently."""
        emitter = WorkflowEventEmitter()
        received: list[WorkflowEvent] = []
        emitter.subscribe(received.append)

        e1 = WorkflowEvent(type="start", timestamp=1.0)
        e2 = WorkflowEvent(type="end", timestamp=2.0)
        emitter.emit(e1)
        emitter.emit(e2)

        assert len(received) == 2
        assert received[0] is e1
        assert received[1] is e2

    def test_subscribe_during_emit_doesnt_affect_current(self) -> None:
        """Test that subscribing during emit doesn't affect the current broadcast.

        Because emit() copies the subscriber list before iteration, a new
        subscriber added by a callback won't receive the current event.
        """
        emitter = WorkflowEventEmitter()
        late_received: list[WorkflowEvent] = []
        late_callback = late_received.append

        def subscribing_callback(event: WorkflowEvent) -> None:
            emitter.subscribe(late_callback)

        emitter.subscribe(subscribing_callback)

        event = WorkflowEvent(type="test", timestamp=time.time())
        emitter.emit(event)

        # Late subscriber should NOT have received the first event
        assert len(late_received) == 0

        # But should receive subsequent events
        event2 = WorkflowEvent(type="test2", timestamp=time.time())
        emitter.emit(event2)
        assert len(late_received) == 1
        assert late_received[0] is event2
