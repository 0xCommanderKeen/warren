import { describe, expect, it } from "vitest";

import fixture from "./fixtures/complete-v1.json";
import { parseSnapshot } from "./parseSnapshot.js";

describe("parseSnapshot", () => {
  it("accepts Burrow's complete version 1 fixture", () => {
    const snapshot = parseSnapshot(fixture);

    expect(snapshot.schema_version).toBe(1);
    expect(snapshot.villagers).toEqual([
      expect.objectContaining({ id: "claude:keeper", name: "Keeper" }),
    ]);
  });
});
