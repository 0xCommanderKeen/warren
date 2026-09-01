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

  it.each([
    ["villager history", (snapshot) => { snapshot.villagers[0].history = {}; }],
    ["approval options", (snapshot) => { snapshot.approvals[0].options = null; }],
    ["task skills", (snapshot) => { snapshot.tasks[0].required_skills = null; }],
    ["routine artifacts", (snapshot) => { snapshot.routines[0].artifacts = {}; }],
    ["resident capabilities", (snapshot) => { snapshot.residents[0].capabilities = null; }],
    ["capacity metadata", (snapshot) => { snapshot.capacity.tasks = "200"; }],
    ["control capabilities", (snapshot) => { snapshot.capabilities.jobs = "yes"; }],
  ])("rejects malformed nested %s", (_name, mutate) => {
    const envelope = structuredClone(fixture);
    mutate(envelope.snapshot);

    expect(() => parseSnapshot(envelope)).toThrow(/snapshot/);
  });
});
