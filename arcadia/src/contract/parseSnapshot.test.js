import { existsSync } from "node:fs";
import { describe, expect, it } from "vitest";

import fixture from "./fixtures/complete-v1.js";
import openapi from "../../../chronicle/docs/openapi.json" with { type: "json" };
import { ContractValidationError, parseSnapshot } from "./parseSnapshot.js";

const schemas = openapi.components.schemas;
const samples = [
  ["VillageState", (snapshot) => snapshot],
  ["VillagerWire", (snapshot) => snapshot.villagers[0]],
  ["ProtocolEvent", (snapshot) => snapshot.villagers[0].history[0]],
  ["ResidentWire", (snapshot) => snapshot.residents[0]],
  ["ResidentDiagnosticWire", (snapshot) => snapshot.diagnostic_residents[0]],
  ["ArtifactWire", (snapshot) => snapshot.artifacts[0]],
  ["TaskWire", (snapshot) => snapshot.tasks[0]],
  ["ApprovalWire", (snapshot) => snapshot.approvals[0]],
  ["JournalWire", (snapshot) => snapshot.journals[0]],
  ["RoutineWire", (snapshot) => snapshot.routines[0]],
  ["DiagnosticWire", (snapshot) => snapshot.diagnostics[0]],
  ["ProjectionCapacity", (snapshot) => snapshot.capacity],
];

function acceptsNull(schema) {
  if (schema === true) return true;
  if (schema.$ref === "#/components/schemas/JsonValue") return true;
  if (schema.$ref) return acceptsNull(schemas[schema.$ref.split("/").at(-1)]);
  return schema.type === "null" || schema.anyOf?.some(acceptsNull);
}

describe("parseSnapshot", () => {
  it("accepts Chronicle's complete version 2 fixture", () => {
    const snapshot = parseSnapshot(fixture);

    expect(snapshot.schema_version).toBe(2);
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

  it.each(samples)("pins every required and nullable %s field to Chronicle OpenAPI", (name, select) => {
    const schema = schemas[name];
    expect(Object.keys(schema.properties).length).toBeGreaterThan(0);
    for (const field of schema.required || []) {
      const missing = structuredClone(fixture);
      delete select(missing.snapshot)[field];
      expect(() => parseSnapshot(missing), `${name}.${field} must remain required`).toThrow();
    }
    for (const [field, propertySchema] of Object.entries(schema.properties)) {
      const nullable = structuredClone(fixture);
      select(nullable.snapshot)[field] = null;
      if (acceptsNull(propertySchema)) {
        expect(() => parseSnapshot(nullable), `${name}.${field} must accept null`).not.toThrow();
      } else {
        expect(() => parseSnapshot(nullable), `${name}.${field} must reject null`).toThrow();
      }
      if (!(schema.required || []).includes(field)) {
        const omitted = structuredClone(fixture);
        delete select(omitted.snapshot)[field];
        expect(() => parseSnapshot(omitted), `${name}.${field} must remain optional`).not.toThrow();
      }
      if (propertySchema.type === "array") {
        const item = structuredClone(fixture);
        select(item.snapshot)[field] = [null];
        const assertion = expect(() => parseSnapshot(item), `${name}.${field} item contract`);
        if (acceptsNull(propertySchema.items)) assertion.not.toThrow();
        else assertion.toThrow();
      }
      if (propertySchema.type === "object" && propertySchema.additionalProperties) {
        const record = structuredClone(fixture);
        select(record.snapshot)[field] = { probe: null };
        const assertion = expect(() => parseSnapshot(record), `${name}.${field} value contract`);
        if (acceptsNull(propertySchema.additionalProperties)) assertion.not.toThrow();
        else assertion.toThrow();
      }
    }
  });

  it.each(samples)("pins every %s literal to Chronicle OpenAPI", (name, select) => {
    for (const [field, propertySchema] of Object.entries(schemas[name].properties)) {
      if (!("const" in propertySchema) && !propertySchema.enum) continue;
      const envelope = structuredClone(fixture);
      select(envelope.snapshot)[field] = "not-a-contract-literal";
      expect(() => parseSnapshot(envelope), `${name}.${field} literal drift`).toThrow();
    }
  });

  it("accepts nested additive fields but forbids every own top-level extra", () => {
    const nested = structuredClone(fixture);
    nested.snapshot.tasks[0].future_field = { supported: true };
    expect(parseSnapshot(nested)).toBe(nested.snapshot);

    for (const field of ["future_field", "__proto__"]) {
      const envelope = structuredClone(fixture);
      Object.defineProperty(envelope.snapshot, field, {
        configurable: true, enumerable: true, value: { injected: true }, writable: true,
      });
      expect(() => parseSnapshot(envelope)).toThrow(ContractValidationError);
    }
  });

  it("accepts recursive JSON values while rejecting non-JSON records and controlled cycles", () => {
    const recursive = structuredClone(fixture);
    recursive.snapshot.approvals[0].detail = {
      object: { array: [null, true, 1, "value", { deep: [] }] },
    };
    recursive.snapshot.approvals[0].options = [Object.assign(Object.create(null), { ok: true })];
    expect(parseSnapshot(recursive)).toBe(recursive.snapshot);

    const dated = structuredClone(fixture);
    dated.snapshot.approvals[0].detail = new Date();
    expect(() => parseSnapshot(dated)).toThrow(/snapshot\.approvals\[0\]\.detail.*plain object/);

    const cyclic = structuredClone(fixture);
    cyclic.snapshot.approvals[0].detail = {};
    cyclic.snapshot.approvals[0].detail.self = cyclic.snapshot.approvals[0].detail;
    expect(() => parseSnapshot(cyclic)).toThrow(/snapshot\.approvals\[0\]\.detail\.self.*acyclic/);
  });
});
