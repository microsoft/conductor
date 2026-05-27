/**
 * Ports tests/test_events.py to TypeScript / vitest.
 *
 * Key differences from Python:
 *  - WorkflowEventEmitter.subscribe() returns an unsubscribe function
 *    (no separate .unsubscribe() method on the emitter).
 *  - emit() is async — tests use await.
 *  - The TypeScript emitter does NOT expose a to_dict() / toDict() method
 *    on the event; WorkflowEvent is a plain interface.
 */
import { describe, expect, it, vi } from "vitest";
import type { WorkflowEvent } from "../events.js";
import { WorkflowEventEmitter } from "../events.js";

// Helper: build a WorkflowEvent directly (mirrors Python WorkflowEvent)
function makeEvent(type: WorkflowEvent["type"] = "agent_started"): WorkflowEvent {
  return { type, timestamp: Date.now(), data: { key: "value" } };
}

describe("WorkflowEvent", () => {
  it("stores type, timestamp, data", () => {
    const ev: WorkflowEvent = { type: "agent_started", timestamp: 1234567890.0, data: { name: "a1" } };
    expect(ev.type).toBe("agent_started");
    expect(ev.timestamp).toBe(1234567890.0);
    expect(ev.data).toEqual({ name: "a1" });
  });

  it("data defaults to empty object when not supplied", async () => {
    const emitter = new WorkflowEventEmitter();
    let received: WorkflowEvent | undefined;
    emitter.subscribe((ev) => { received = ev; });
    await emitter.emit("workflow_started");
    expect(received?.data).toEqual({});
  });
});

describe("WorkflowEventEmitter.subscribe / emit", () => {
  it("subscribed callback receives emitted event", async () => {
    const emitter = new WorkflowEventEmitter();
    const received: WorkflowEvent[] = [];
    emitter.subscribe((ev) => received.push(ev));

    await emitter.emit("agent_started", { x: 1 });

    expect(received).toHaveLength(1);
    expect(received[0].type).toBe("agent_started");
    expect(received[0].data).toEqual({ x: 1 });
  });

  it("all subscribers receive the event", async () => {
    const emitter = new WorkflowEventEmitter();
    const a: WorkflowEvent[] = [];
    const b: WorkflowEvent[] = [];
    emitter.subscribe((ev) => a.push(ev));
    emitter.subscribe((ev) => b.push(ev));

    await emitter.emit("agent_started");

    expect(a).toHaveLength(1);
    expect(b).toHaveLength(1);
    expect(a[0]).toBe(b[0]);
  });

  it("subscribers are called in registration order", async () => {
    const emitter = new WorkflowEventEmitter();
    const order: number[] = [];
    emitter.subscribe(() => order.push(1));
    emitter.subscribe(() => order.push(2));
    emitter.subscribe(() => order.push(3));

    await emitter.emit("agent_started");

    expect(order).toEqual([1, 2, 3]);
  });

  it("unsubscribe function stops receiving events", async () => {
    const emitter = new WorkflowEventEmitter();
    const received: WorkflowEvent[] = [];
    const unsub = emitter.subscribe((ev) => received.push(ev));

    unsub();
    await emitter.emit("agent_started");

    expect(received).toHaveLength(0);
  });

  it("emitting with no subscribers does not throw", async () => {
    const emitter = new WorkflowEventEmitter();
    await expect(emitter.emit("workflow_started")).resolves.toBeUndefined();
  });

  it("multiple events are delivered independently", async () => {
    const emitter = new WorkflowEventEmitter();
    const received: WorkflowEvent[] = [];
    emitter.subscribe((ev) => received.push(ev));

    await emitter.emit("workflow_started");
    await emitter.emit("workflow_completed");

    expect(received).toHaveLength(2);
    expect(received[0].type).toBe("workflow_started");
    expect(received[1].type).toBe("workflow_completed");
  });

  it("callback exception does not prevent other subscribers from running", async () => {
    const emitter = new WorkflowEventEmitter();
    const received: WorkflowEvent[] = [];

    emitter.subscribe(() => { throw new Error("boom"); });
    emitter.subscribe((ev) => received.push(ev));

    await emitter.emit("agent_started");

    expect(received).toHaveLength(1);
  });

  it("multiple failing callbacks don't affect healthy ones", async () => {
    const emitter = new WorkflowEventEmitter();
    const received: string[] = [];

    emitter.subscribe(() => { throw new Error("fail 1"); });
    emitter.subscribe(() => { received.push("good"); });
    emitter.subscribe(() => { throw new Error("fail 2"); });

    await emitter.emit("agent_started");

    expect(received).toEqual(["good"]);
  });

  it("subscribing during emit does not affect the current broadcast", async () => {
    const emitter = new WorkflowEventEmitter();
    const lateReceived: WorkflowEvent[] = [];

    emitter.subscribe(() => {
      // Register another subscriber during the current emit
      emitter.subscribe((ev) => lateReceived.push(ev));
    });

    await emitter.emit("agent_started");

    // Late subscriber should NOT have received the first event
    expect(lateReceived).toHaveLength(0);

    // But should receive subsequent events
    await emitter.emit("agent_completed");
    expect(lateReceived).toHaveLength(1);
    expect(lateReceived[0].type).toBe("agent_completed");
  });

  it("concurrent emits reach subscriber (basic)", async () => {
    const emitter = new WorkflowEventEmitter();
    const fn = vi.fn();
    emitter.subscribe(fn);

    await Promise.all(
      Array.from({ length: 20 }, () => emitter.emit("agent_started")),
    );

    expect(fn).toHaveBeenCalledTimes(20);
  });
});
