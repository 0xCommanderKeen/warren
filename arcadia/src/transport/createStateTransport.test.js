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

  open() {
    this.onopen?.();
  }
}

beforeEach(() => {
  FakeEventSource.instances = [];
});

describe("createStateTransport", () => {
  it("aborts a pending fetch and ignores its late result after close", async () => {
    let resolveFetch;
    const fetch = vi.fn(() => new Promise((resolve) => { resolveFetch = resolve; }));
    const onEnvelope = vi.fn();
    const onStatus = vi.fn();
    const onError = vi.fn();
    const transport = createStateTransport({ fetch, EventSource: FakeEventSource, onEnvelope, onStatus, onError });
    const starting = transport.start();
    transport.close();
    const statuses = onStatus.mock.calls.slice();
    resolveFetch({ status: 200, json: async () => envelope(1, "cursor:1") });
    await starting;

    expect(onEnvelope).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
    expect(onStatus.mock.calls).toEqual(statuses);
    expect(transport.snapshot()).toBeNull();
    expect(FakeEventSource.instances).toHaveLength(0);
    expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
  });

  it.each(["success", "malformed", "rejection"])("ignores %s after closing during response decoding", async (outcome) => {
    let resolveJson;
    let rejectJson;
    const json = vi.fn(() => new Promise((resolve, reject) => { resolveJson = resolve; rejectJson = reject; }));
    const fetch = vi.fn().mockResolvedValue({ status: 200, json });
    const onEnvelope = vi.fn();
    const onStatus = vi.fn();
    const onError = vi.fn();
    const transport = createStateTransport({ fetch, EventSource: FakeEventSource, onEnvelope, onStatus, onError });
    const starting = transport.start();
    await Promise.resolve();
    expect(json).toHaveBeenCalledOnce();
    transport.close();
    onStatus.mockClear();
    if (outcome === "rejection") rejectJson(new Error("decoding failed"));
    else resolveJson(outcome === "success" ? envelope(1, "cursor:1") : { invalid: true });
    await expect(starting).resolves.toBeUndefined();
    expect(onEnvelope).not.toHaveBeenCalled();
    expect(onStatus).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
    expect(transport.snapshot()).toBeNull();
    expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
    expect(FakeEventSource.instances).toHaveLength(0);
  });

  it("requires a fresh instance after close, even while the old fetch is pending", async () => {
    let resolveFetch;
    const fetch = vi.fn(() => new Promise((resolve) => { resolveFetch = resolve; }));
    const onStatus = vi.fn();
    const onEnvelope = vi.fn();
    const onError = vi.fn();
    const transport = createStateTransport({ fetch, EventSource: FakeEventSource, onStatus, onEnvelope, onError });
    const starting = transport.start();
    transport.close();
    onStatus.mockClear();
    await expect(transport.start()).rejects.toThrow("Cannot start a closed state transport");
    resolveFetch({ status: 200, json: async () => envelope(2, "cursor:2") });
    await starting;
    transport.close();
    expect(fetch).toHaveBeenCalledOnce();
    expect(onStatus).not.toHaveBeenCalled();
    expect(onEnvelope).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
    expect(transport.snapshot()).toBeNull();
    expect(FakeEventSource.instances).toHaveLength(0);
  });

  it("stops callback delivery when an envelope observer closes then throws", async () => {
    const onStatus = vi.fn();
    const onError = vi.fn();
    const transport = createStateTransport({
      fetch: vi.fn().mockResolvedValue({ status: 204 }),
      EventSource: FakeEventSource,
      onStatus,
      onError,
      onEnvelope: () => { transport.close(); throw new Error("observer disposed"); },
    });
    await transport.start();
    const stream = FakeEventSource.instances[0];
    onStatus.mockClear();
    stream.emit("snapshot", envelope(1, "cursor:1"));
    stream.open();
    await stream.onerror();
    expect(onStatus.mock.calls).toEqual([["disconnected"]]);
    expect(onError).not.toHaveBeenCalled();
    expect(transport.snapshot()?.generation).toBe(1);
  });

  it.each(["abort", "network", "HTTP"])("silences a pending catch-up %s failure after close", async (failure) => {
    vi.useFakeTimers();
    try {
      let resolveFetch;
      let rejectFetch;
      const fetch = vi.fn()
        .mockResolvedValueOnce({ status: 200, json: async () => envelope(1, "cursor:1") })
        .mockImplementationOnce((_url, { signal }) => new Promise((resolve, reject) => {
          resolveFetch = resolve;
          rejectFetch = reject;
          if (failure === "abort") signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
        }));
      const onEnvelope = vi.fn();
      const onStatus = vi.fn();
      const onError = vi.fn();
      const transport = createStateTransport({ fetch, EventSource: FakeEventSource, onEnvelope, onStatus, onError });
      await transport.start();
      const catchingUp = FakeEventSource.instances[0].onerror();
      transport.close();
      onEnvelope.mockClear();
      onStatus.mockClear();
      if (failure === "network") rejectFetch(new Error("network failed"));
      if (failure === "HTTP") resolveFetch({ status: 500 });
      await catchingUp;
      await vi.runAllTimersAsync();
      expect(onEnvelope).not.toHaveBeenCalled();
      expect(onStatus).not.toHaveBeenCalled();
      expect(onError).not.toHaveBeenCalled();
      expect(fetch.mock.calls[1][1].signal.aborted).toBe(true);
      expect(FakeEventSource.instances).toHaveLength(1);
      expect(transport.snapshot()?.generation).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("loads state and opens the prefixed stream with its resume query", async () => {
    const initial = envelope(4, "cursor 4");
    const fetch = vi.fn().mockResolvedValue({ status: 200, json: async () => initial });
    const onEnvelope = vi.fn();

    const transport = createStateTransport({
      fetch,
      EventSource: FakeEventSource,
      baseUrl: "/chronicle/",
      onEnvelope,
    });
    await transport.start();

    expect(fetch).toHaveBeenCalledWith("/chronicle/state", { cache: "no-store", signal: expect.any(AbortSignal) });
    expect(onEnvelope).toHaveBeenCalledWith(initial);
    expect(FakeEventSource.instances[0].url).toBe(
      "/chronicle/state/stream?generation=4&cursor=cursor%204",
    );
  });

  it("catches up before reconnecting a dropped stream", async () => {
    vi.useFakeTimers();
    const fetch = vi.fn()
      .mockResolvedValueOnce({ status: 200, json: async () => envelope(7, "cursor:7") })
      .mockResolvedValueOnce({ status: 200, json: async () => envelope(9, "cursor:9") });
    try {
      const transport = createStateTransport({
        fetch,
        EventSource: FakeEventSource,
        random: () => 1,
        retryBaseMs: 100,
      });
      await transport.start();
      const dropped = FakeEventSource.instances[0];
      dropped.emit("snapshot", envelope(8, "cursor:8"));

      await dropped.onerror();
      await vi.advanceTimersByTimeAsync(100);

      expect(fetch).toHaveBeenLastCalledWith(
        "/state?generation=8&cursor=cursor%3A8",
        { cache: "no-store", signal: expect.any(AbortSignal) },
      );
      expect(FakeEventSource.instances[1].url).toBe(
        "/state/stream?generation=9&cursor=cursor%3A9",
      );
      transport.close();
    } finally {
      vi.useRealTimers();
    }
  });

  it("backs off repeated stream and catch-up failures instead of reconnecting immediately", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    try {
      const openedAt = [];
      class TimedEventSource extends FakeEventSource {
        constructor(url) {
          super(url);
          openedAt.push(Date.now());
        }
      }
      const fetch = vi.fn()
        .mockResolvedValueOnce({ status: 204 })
        .mockRejectedValue(new Error("Chronicle unavailable"));
      const transport = createStateTransport({
        fetch,
        EventSource: TimedEventSource,
        random: () => 1,
        retryBaseMs: 100,
        retryMaxMs: 400,
      });
      await transport.start();

      await FakeEventSource.instances[0].onerror();
      expect(FakeEventSource.instances).toHaveLength(1);
      await vi.advanceTimersByTimeAsync(100);
      expect(FakeEventSource.instances).toHaveLength(2);

      await FakeEventSource.instances[1].onerror();
      expect(FakeEventSource.instances).toHaveLength(2);
      await vi.advanceTimersByTimeAsync(200);
      expect(FakeEventSource.instances).toHaveLength(3);

      await FakeEventSource.instances[2].onerror();
      await vi.advanceTimersByTimeAsync(400);
      await FakeEventSource.instances[3].onerror();
      await vi.advanceTimersByTimeAsync(400);

      expect(openedAt).toEqual([0, 100, 300, 700, 1_100]);
      transport.close();
    } finally {
      vi.useRealTimers();
    }
  });

  it("resets the retry delay after a healthy live event", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    try {
      const openedAt = [];
      class TimedEventSource extends FakeEventSource {
        constructor(url) {
          super(url);
          openedAt.push(Date.now());
        }
      }
      const fetch = vi.fn()
        .mockResolvedValueOnce({ status: 204 })
        .mockRejectedValue(new Error("Chronicle unavailable"));
      const transport = createStateTransport({
        fetch,
        EventSource: TimedEventSource,
        random: () => 1,
        retryBaseMs: 100,
        retryMaxMs: 400,
      });
      await transport.start();

      await FakeEventSource.instances[0].onerror();
      await vi.advanceTimersByTimeAsync(100);
      await FakeEventSource.instances[1].onerror();
      await vi.advanceTimersByTimeAsync(200);
      FakeEventSource.instances[2].emit("snapshot", envelope(1, "cursor:1"));
      await FakeEventSource.instances[2].onerror();
      await vi.advanceTimersByTimeAsync(100);

      expect(openedAt).toEqual([0, 100, 300, 400]);
      transport.close();
    } finally {
      vi.useRealTimers();
    }
  });

  it("uses deterministic equal jitter without allowing a zero-delay retry", async () => {
    vi.useFakeTimers();
    try {
      const fetch = vi.fn()
        .mockResolvedValueOnce({ status: 204 })
        .mockRejectedValue(new Error("Chronicle unavailable"));
      const transport = createStateTransport({
        fetch,
        EventSource: FakeEventSource,
        random: () => 0,
        retryBaseMs: 100,
      });
      await transport.start();
      await FakeEventSource.instances[0].onerror();

      await vi.advanceTimersByTimeAsync(49);
      expect(FakeEventSource.instances).toHaveLength(1);
      await vi.advanceTimersByTimeAsync(1);
      expect(FakeEventSource.instances).toHaveLength(2);
      transport.close();
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    ["zero base", { retryBaseMs: 0 }],
    ["infinite base", { retryBaseMs: Infinity }],
    ["NaN cap", { retryMaxMs: Number.NaN }],
    ["cap below base", { retryBaseMs: 100, retryMaxMs: 99 }],
  ])("rejects invalid retry configuration: %s", (_label, retryOptions) => {
    expect(() => createStateTransport({
      fetch: vi.fn(),
      EventSource: FakeEventSource,
      ...retryOptions,
    })).toThrow(RangeError);
  });

  it.each([Number.NaN, Infinity, -1, 2])(
    "normalizes an invalid random sample (%s) to a bounded non-zero delay",
    async (sample) => {
      vi.useFakeTimers();
      try {
        const fetch = vi.fn()
          .mockResolvedValueOnce({ status: 204 })
          .mockRejectedValue(new Error("Chronicle unavailable"));
        const transport = createStateTransport({
          fetch,
          EventSource: FakeEventSource,
          random: () => sample,
          retryBaseMs: 1,
        });
        await transport.start();
        await FakeEventSource.instances[0].onerror();

        expect(FakeEventSource.instances).toHaveLength(1);
        await vi.advanceTimersByTimeAsync(1);
        expect(FakeEventSource.instances).toHaveLength(2);
        transport.close();
      } finally {
        vi.useRealTimers();
      }
    },
  );

  it("falls back to a bounded non-zero delay when the random source throws", async () => {
    vi.useFakeTimers();
    try {
      const onError = vi.fn();
      const fetch = vi.fn()
        .mockResolvedValueOnce({ status: 204 })
        .mockRejectedValue(new Error("Chronicle unavailable"));
      const transport = createStateTransport({
        fetch,
        EventSource: FakeEventSource,
        onError,
        random: () => { throw new Error("entropy unavailable"); },
        retryBaseMs: 1,
      });
      await transport.start();
      await FakeEventSource.instances[0].onerror();

      expect(onError).toHaveBeenCalledWith(
        expect.objectContaining({ message: "entropy unavailable" }),
      );
      expect(FakeEventSource.instances).toHaveLength(1);
      await vi.advanceTimersByTimeAsync(1);
      expect(FakeEventSource.instances).toHaveLength(2);
      transport.close();
    } finally {
      vi.useRealTimers();
    }
  });

  it("lets a quiet opened stream reset the retry budget and ignores a retired stream open", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    try {
      const openedAt = [];
      class TimedEventSource extends FakeEventSource {
        constructor(url) {
          super(url);
          openedAt.push(Date.now());
        }
      }
      const fetch = vi.fn()
        .mockResolvedValueOnce({ status: 204 })
        .mockRejectedValue(new Error("Chronicle unavailable"));
      const transport = createStateTransport({
        fetch,
        EventSource: TimedEventSource,
        random: () => 1,
        retryBaseMs: 100,
        retryMaxMs: 400,
      });
      await transport.start();

      const first = FakeEventSource.instances[0];
      await first.onerror();
      await vi.advanceTimersByTimeAsync(100);
      await FakeEventSource.instances[1].onerror();
      await vi.advanceTimersByTimeAsync(200);
      const healthy = FakeEventSource.instances[2];
      healthy.open();
      first.open();
      await healthy.onerror();
      await vi.advanceTimersByTimeAsync(100);

      expect(openedAt).toEqual([0, 100, 300, 400]);
      transport.close();
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not let throwing callbacks stop polling, catch-up, or reconnect scheduling", async () => {
    vi.useFakeTimers();
    try {
      const callbackError = new Error("observer failed");
      const fetch = vi.fn()
        .mockResolvedValueOnce({ status: 200, json: async () => envelope(1, "cursor:1") })
        .mockResolvedValueOnce({ status: 204 });
      const transport = createStateTransport({
        fetch,
        EventSource: FakeEventSource,
        onEnvelope: () => { throw callbackError; },
        onStatus: () => { throw callbackError; },
        onError: () => { throw callbackError; },
        random: () => 1,
        retryBaseMs: 100,
      });

      await expect(transport.start()).resolves.toBeUndefined();
      expect(transport.snapshot()?.generation).toBe(1);
      await expect(FakeEventSource.instances[0].onerror()).resolves.toBeUndefined();
      await vi.advanceTimersByTimeAsync(100);
      expect(FakeEventSource.instances).toHaveLength(2);
      expect(() => transport.close()).not.toThrow();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps backoff after malformed catch-up but resets it after valid stale state", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    try {
      const openedAt = [];
      class TimedEventSource extends FakeEventSource {
        constructor(url) {
          super(url);
          openedAt.push(Date.now());
        }
      }
      const fetch = vi.fn()
        .mockResolvedValueOnce({ status: 200, json: async () => envelope(5, "cursor:5") })
        .mockRejectedValueOnce(new Error("Chronicle unavailable"))
        .mockResolvedValueOnce({ status: 200, json: async () => ({ nope: true }) })
        .mockResolvedValueOnce({ status: 200, json: async () => envelope(5, "cursor:5") })
        .mockRejectedValue(new Error("Chronicle unavailable"));
      const transport = createStateTransport({
        fetch,
        EventSource: TimedEventSource,
        random: () => 1,
        retryBaseMs: 100,
        retryMaxMs: 400,
      });
      await transport.start();

      await FakeEventSource.instances[0].onerror();
      await vi.advanceTimersByTimeAsync(100);
      await FakeEventSource.instances[1].onerror();
      await vi.advanceTimersByTimeAsync(200);
      await FakeEventSource.instances[2].onerror();
      await vi.advanceTimersByTimeAsync(100);

      expect(openedAt).toEqual([0, 100, 300, 400]);
      transport.close();
    } finally {
      vi.useRealTimers();
    }
  });

  it("cancels a pending reconnect when closed", async () => {
    vi.useFakeTimers();
    try {
      const onStatus = vi.fn();
      const fetch = vi.fn()
        .mockResolvedValueOnce({ status: 204 })
        .mockRejectedValue(new Error("Chronicle unavailable"));
      const transport = createStateTransport({
        fetch,
        EventSource: FakeEventSource,
        onStatus,
        random: () => 1,
        retryBaseMs: 100,
      });
      await transport.start();
      await FakeEventSource.instances[0].onerror();
      expect(onStatus).toHaveBeenLastCalledWith("reconnecting");

      transport.close();
      await vi.runAllTimersAsync();

      expect(FakeEventSource.instances).toHaveLength(1);
      expect(onStatus).toHaveBeenLastCalledWith("disconnected");
    } finally {
      vi.useRealTimers();
    }
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

  it("rejects a malformed nested snapshot without replacing the last good snapshot", async () => {
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
    stream.emit("snapshot", envelope(2, "cursor:2", {
      tasks: [{ ...fixture.snapshot.tasks[0], required_skills: null }],
    }));

    expect(onEnvelope).toHaveBeenCalledTimes(1);
    expect(transport.snapshot()).toMatchObject({ generation: 1 });
    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({ message: expect.stringMatching(/snapshot\.tasks/) }),
    );
  });
});
