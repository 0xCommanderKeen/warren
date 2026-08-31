import { beforeEach, describe, expect, it, vi } from "vitest";

import fixture from "../contract/fixtures/complete-v1.js";
import { createStateTransport } from "./createStateTransport.js";

function envelope(generation, cursor, extra = {}) {
  return {
    kind: "snapshot",
    snapshot: { ...fixture.snapshot, generation, cursor, ...extra },
  };
}

class FakeEventSource {
  static instances = [];

  constructor(url) {
    this.url = url;
    this.listeners = new Map();
    this.close = vi.fn();
    FakeEventSource.instances.push(this);
  }

  addEventListener(name, listener) {
    this.listeners.set(name, listener);
  }

  emit(name, value) {
    this.listeners.get(name)?.({ data: JSON.stringify(value) });
  }
}

beforeEach(() => {
  FakeEventSource.instances = [];
});

describe("createStateTransport", () => {
  it("loads state and opens the prefixed stream with its resume query", async () => {
    const initial = envelope(4, "cursor 4");
    const fetch = vi.fn().mockResolvedValue({ status: 200, json: async () => initial });
    const onEnvelope = vi.fn();

    const transport = createStateTransport({
      fetch,
      EventSource: FakeEventSource,
      baseUrl: "/burrow/",
      onEnvelope,
    });
    await transport.start();

    expect(fetch).toHaveBeenCalledWith("/burrow/state", { cache: "no-store" });
    expect(onEnvelope).toHaveBeenCalledWith(initial);
    expect(FakeEventSource.instances[0].url).toBe(
      "/burrow/state/stream?generation=4&cursor=cursor%204",
    );
  });

  it("catches up before reconnecting a dropped stream", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce({ status: 200, json: async () => envelope(7, "cursor:7") })
      .mockResolvedValueOnce({ status: 200, json: async () => envelope(9, "cursor:9") });
    const transport = createStateTransport({ fetch, EventSource: FakeEventSource });
    await transport.start();
    const dropped = FakeEventSource.instances[0];
    dropped.emit("snapshot", envelope(8, "cursor:8"));

    await dropped.onerror();

    expect(fetch).toHaveBeenLastCalledWith(
      "/state?generation=8&cursor=cursor%3A8",
      { cache: "no-store" },
    );
    expect(FakeEventSource.instances[1].url).toBe(
      "/state/stream?generation=9&cursor=cursor%3A9",
    );
  });

  it("rejects an unsupported streamed schema without replacing current state", async () => {
    const onEnvelope = vi.fn();
    const onError = vi.fn();
    const transport = createStateTransport({
      fetch: vi.fn().mockResolvedValue({ status: 204 }),
      EventSource: FakeEventSource,
      onEnvelope,
      onError,
    });
    await transport.start();
    const stream = FakeEventSource.instances[0];
    stream.emit("snapshot", envelope(1, "cursor:1"));
    stream.emit("snapshot", envelope(2, "cursor:2", { schema_version: 2 }));

    expect(onEnvelope).toHaveBeenCalledTimes(1);
    expect(transport.snapshot()).toMatchObject({ generation: 1, schema_version: 1 });
    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({ message: "Unsupported village schema version: 2" }),
    );
  });
});
