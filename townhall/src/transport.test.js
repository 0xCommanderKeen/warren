import { describe, expect, it, vi } from "vitest";
import fixture from "./fixtures/complete-v1.json";
import { createStateTransport, validateSnapshot } from "./transport.js";

describe("read-only Burrow transport", () => {
  it("validates the complete versioned snapshot", () => {
    expect(validateSnapshot(fixture.snapshot)).toBeNull();
    expect(validateSnapshot({ ...fixture.snapshot, schema_version: 2 })).toBe("unsupported snapshot schema");
  });

  it("polls only the public state endpoint", async () => {
    const onState = vi.fn();
    const fetch = vi.fn().mockResolvedValue({ status: 200, json: async () => fixture });
    const transport = createStateTransport({ fetch, onState });
    await transport.poll();
    expect(fetch).toHaveBeenCalledWith("/state", { cache: "no-store" });
    expect(onState).toHaveBeenCalledWith(fixture.snapshot);
  });
});
