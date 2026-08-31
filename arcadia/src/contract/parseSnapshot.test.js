import { existsSync } from "node:fs";
import { describe, expect, it } from "vitest";

import fixture from "./fixtures/complete-v1.js";
import { parseSnapshot } from "./parseSnapshot.js";

describe("parseSnapshot", () => {
  it("accepts Chronicle's complete version 1 fixture", () => {
    const snapshot = parseSnapshot(fixture);

    expect(snapshot.schema_version).toBe(1);
    expect(snapshot.villagers).toEqual([
      expect.objectContaining({ id: "claude:keeper", name: "Keeper" }),
    ]);
  });

  it("reads the contract fixture from Chronicle itself, never a vendored copy", () => {
    expect(existsSync("src/contract/fixtures/complete-v1.json")).toBe(false);
  });
});
