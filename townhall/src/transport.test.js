import { describe, expect, it, vi } from "vitest";
import fixture from "./fixtures/complete-v1.js";
import { createStateTransport, validateSnapshot } from "./transport.js";

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function eventSources() {
  const instances = [];
  class EventSource {
    constructor(url) {
      this.url = url;
      this.listeners = {};
      this.close = vi.fn();
      instances.push(this);
    }

    addEventListener(name, callback) {
      this.listeners[name] = callback;
    }
  }
  return { EventSource, instances };
}

describe("read-only Chronicle transport", () => {
  it("validates the complete versioned snapshot", () => {
    expect(validateSnapshot(fixture.snapshot)).toBeNull();
    expect(validateSnapshot({ ...fixture.snapshot, schema_version: 2 })).toBe("unsupported snapshot schema");
  });

  it("polls only the public state endpoint", async () => {
    const onState = vi.fn();
    const fetch = vi.fn().mockResolvedValue({ status: 200, json: async () => fixture });
    const transport = createStateTransport({ fetch, onState });
    await transport.poll();
    expect(fetch).toHaveBeenCalledWith("/state", {
      cache: "no-store",
      signal: expect.any(AbortSignal),
    });
    expect(onState).toHaveBeenCalledWith(fixture.snapshot);
  });

  it.each(["resolve", "reject"])("is terminal when an initial poll later %ss", async (outcome) => {
    const request = deferred();
    const fetch = vi.fn(() => request.promise);
    const onState = vi.fn();
    const onStatus = vi.fn();
    const warn = vi.fn();
    const { EventSource, instances } = eventSources();
    const transport = createStateTransport({ fetch, EventSource, onState, onStatus, warn });
    const started = transport.poll().then(() => transport.connect());

    transport.close();
    expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
    const callbackCounts = [onState.mock.calls.length, onStatus.mock.calls.length, warn.mock.calls.length];
    if (outcome === "resolve") request.resolve({ status: 200, json: async () => fixture });
    else request.reject(new Error("late failure"));
    await started;

    expect(instances).toHaveLength(0);
    expect([onState.mock.calls.length, onStatus.mock.calls.length, warn.mock.calls.length]).toEqual(callbackCounts);
  });

  it("makes close idempotently terminal for direct transport calls", async () => {
    const fetch = vi.fn();
    const onState = vi.fn();
    const onStatus = vi.fn();
    const { EventSource, instances } = eventSources();
    const transport = createStateTransport({ fetch, EventSource, onState, onStatus });

    transport.close();
    transport.close();
    await transport.poll();
    transport.connect();
    expect(transport.apply(fixture)).toBe(false);

    expect(fetch).not.toHaveBeenCalled();
    expect(instances).toHaveLength(0);
    expect(onState).not.toHaveBeenCalled();
    expect(onStatus).toHaveBeenCalledTimes(1);
  });

  it("does not resume a reconnect after close while its poll is deferred", async () => {
    const reconnectPoll = deferred();
    const fetch = vi.fn()
      .mockResolvedValueOnce({ status: 200, json: async () => fixture })
      .mockImplementationOnce(() => reconnectPoll.promise);
    const onState = vi.fn();
    const onStatus = vi.fn();
    const warn = vi.fn();
    const { EventSource, instances } = eventSources();
    const transport = createStateTransport({ fetch, EventSource, onState, onStatus, warn });
    await transport.poll();
    transport.connect();
    expect(instances).toHaveLength(1);

    const reconnecting = instances[0].onerror();
    const callbackCounts = [onState.mock.calls.length, onStatus.mock.calls.length, warn.mock.calls.length];
    transport.close();
    expect([onState.mock.calls.length, onStatus.mock.calls.length, warn.mock.calls.length]).toEqual(callbackCounts);
    reconnectPoll.resolve({ status: 200, json: async () => fixture });
    await reconnecting;

    expect(instances).toHaveLength(1);
    expect(instances[0].close).toHaveBeenCalledTimes(1);
    expect([onState.mock.calls.length, onStatus.mock.calls.length, warn.mock.calls.length]).toEqual(callbackCounts);
  });
});
