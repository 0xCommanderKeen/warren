/* The shape half of the steward↔townhall seam (warren#321).
 *
 * warren#242 pinned the *route* half: the dev proxy and the deployed nginx both carry
 * Steward's top-level route segments, and `architecture.test.js` checks both against
 * Steward's own `@app` decorators. What travelled along those routes was still prose —
 * `client.js` is hand-written against `steward/docs/api.md`, so a renamed body field or a
 * moved path shipped and was found by a human clicking.
 *
 * Steward serves no schema to check against: every route there is a write path, so nothing
 * is answered unauthenticated and `openapi_url` is `None`. So the document is exported
 * offline (`cd steward && make openapi-write`) and committed, and this file reads that copy
 * in-tree — the same seam `fixtures/complete-v1.js` uses for Chronicle's snapshot fixture,
 * for the same reason: a vendored copy is a copy that goes stale silently.
 * `.github/workflows/townhall.yml` lists the file among this suite's paths, or the
 * contract would only be checked when townhall itself changed.
 *
 * The run-now receipt, request ledger and routine list publish closed response envelopes.
 * Their fixtures below share the rendering suite's data and pass through the real client.
 * Remaining response endpoints are inventoried in steward/docs/response-migration.md.
 */

import { readFileSync, readdirSync } from "node:fs";
import Ajv2020 from "ajv/dist/2020.js";
import { describe, expect, it, vi } from "vitest";
import { ROUTINE, ROUTINES, RUN_RECEIPT, RUN_REQUEST } from "./run-contract-fixtures.js";
import { confirmRun } from "../console/ledger.jsx";
import { createStewardClient } from "./client.js";
import { createOperatorCredential } from "./credential.js";
import { complaints, declarationBody } from "../pages/ResidentNew.jsx";

/** Steward's own document, read where Steward commits it. Never copied into this tree. */
// Through a variable, not a literal: Vite rewrites a literal `new URL(…, import.meta.url)`
// into an asset URL — `http://localhost/@fs/…` for a path outside this project — and
// `readFileSync` cannot open that. `architecture.test.js` reads Steward's api.py the same way.
const ARTIFACT = "../../../steward/docs/openapi.json";
const CONSOLE_TREE = "../";
const openapi = JSON.parse(readFileSync(new URL(ARTIFACT, import.meta.url), "utf8"));

const held = () => {
  const map = new Map();
  const credential = createOperatorCredential({
    storage: {
      getItem: (key) => (map.has(key) ? map.get(key) : null),
      setItem: (key, value) => map.set(key, String(value)),
      removeItem: (key) => map.delete(key),
    },
  });
  credential.remember("contract");
  return credential;
};

/* -- what the client asks for ---------------------------------------------------------- */

/** Two placeholder arguments, enough for every method's `(id)`, `(id, body)` or `(body)`. */
const PLACEHOLDERS = ["first-id", "second-id"];

/**
 * Every request `client.js` can make, recorded by making it against a stub fetch.
 *
 * Derived rather than listed: a method added to the client is checked the day it is added,
 * where a hand-kept table would be one more thing to forget. Nothing leaves the process —
 * the fetch is a spy that answers `{}` to everything.
 */
async function requestsTheClientMakes() {
  const seen = [];
  const fetch = vi.fn(async (url, init) => {
    const [path, search] = String(url).split("?");
    const query = search ? [...new URLSearchParams(search).keys()] : [];
    seen.push({ method: init.method, path, query });
    return { status: 200, ok: true, text: async () => "{}" };
  });
  const client = createStewardClient({ credential: held(), fetch });

  for (const [name, method] of Object.entries(client)) {
    // `call` is the escape hatch every other method goes through, not a route of its own.
    if (name === "call") continue;
    await method(...PLACEHOLDERS);
  }
  return seen;
}

/** The path template in the document that a concrete path belongs to, if any. */
function templateFor(path) {
  const parts = path.split("/");
  return Object.keys(openapi.paths).find((template) => {
    const segments = template.split("/");
    return (
      segments.length === parts.length &&
      segments.every((segment, at) =>
        segment.startsWith("{") ? parts[at].length > 0 : segment === parts[at],
      )
    );
  });
}

const operations = () =>
  Object.entries(openapi.paths).flatMap(([path, methods]) =>
    Object.keys(methods).map((method) => `${method.toUpperCase()} ${path}`),
  );

/**
 * Routes Steward declares that this console deliberately never calls.
 *
 * Listed so that adding one is a decision somebody makes rather than a drift nobody sees:
 * a new Steward route lands here as a failing test asking whether the console should be
 * showing it. Each of these is a door another client uses — a resident, the CLI, or a
 * human with curl — not one the write surface is missing.
 */
/**
 * Query parameters a *page* adds, on top of what `client.js` sends on its own.
 *
 * `client.js` takes an options bag through to `fetch`, so a page can filter a read the
 * client itself knows nothing about. Those are listed here rather than derived, and the
 * count is checked against the tree below so the list cannot quietly go short.
 */
const PAGE_QUERIES = [
  ["GET", "/queue", ["since"]],
  // ResidentDetail asks for the last eight journal entries, not the whole file.
  ["GET", "/residents/{resident_id}/journal", ["limit"]],
];

const UNCALLED = [
  // The approvals page reads the whole list and decides from it; one approval on its own
  // is what a resident polling its own request asks for.
  "GET /approvals/{request_id}",
  // The console follows one request it just made (`readRequest`); the full log is an
  // operator's audit view, and there is no page for it.
  "GET /requests",
  // Credentials (warren#462) have no page yet: the write path exists so that provisioning a
  // bot needs no ssh, and today the caller is a Claude session or a curl, not this console.
  "GET /secrets",
  // The board renders a task's lineage from the snapshot Chronicle already streams.
  "GET /tasks/{task_id}/lineage",
  // Delegation is resident-to-resident, through the `<delegate>` block in a session.
  "POST /delegate",
  // Rehearsing a declaration (warren#446) is Karen's move inside `raise-resident`, before
  // she knocks: what the console shows is the reply she quotes in it, not a button that
  // spends a model turn on somebody else's behalf.
  "POST /residents/{resident_id}/rehearse",
  "PUT /secrets/{name}",
];

describe("the client speaks the routes Steward declares", () => {
  it("asks only for paths and methods the document carries", async () => {
    const requests = await requestsTheClientMakes();
    expect(requests.length).toBeGreaterThan(10);

    for (const { method, path } of requests) {
      const template = templateFor(path);
      expect(template, `${method} ${path} matches no path in Steward's document`).toBeDefined();
      expect(
        Object.keys(openapi.paths[template]),
        `${template} carries no ${method}`,
      ).toContain(method.toLowerCase());
    }
  });

  it("has a page-query list that has not gone short", () => {
    // PAGE_QUERIES is hand-kept, so it is worth nothing unless something notices a page
    // growing a filter that nobody added to it. Every `query: {` in the console tree is one
    // entry here; `client.js` is excluded because it is the module that defines the option.
    // Through a variable, for the same Vite reason `ARTIFACT` is read that way.
    const sources = readdirSync(new URL(CONSOLE_TREE, import.meta.url), {
      recursive: true, withFileTypes: true,
    })
      .filter((entry) => entry.isFile() && /\.jsx?$/.test(entry.name))
      .map((entry) => `${entry.parentPath}/${entry.name}`)
      .filter((path) => !/steward\/(client|contract\.test)\.js$/.test(path));

    const found = sources.flatMap((path) => [...readFileSync(path, "utf8").matchAll(/\bquery:\s*\{/g)]);
    expect(found).toHaveLength(PAGE_QUERIES.length);
  });

  it("sends only query parameters Steward declares", async () => {
    // The half a path check cannot see. Rename `limit` to `count` in Steward and every
    // assertion above still passes, while the journal quietly shows the whole file: the
    // origin drops an unknown parameter, so the console asks for a filter and is answered
    // as though it had asked for nothing.
    const asked = [
      ...(await requestsTheClientMakes()).map(({ method, path, query }) => [
        method, templateFor(path), query,
      ]),
      ...PAGE_QUERIES,
    ];
    expect(
      asked.some(([, , query]) => query.length),
      "no query parameter reached the recorder — the reader has gone stale",
    ).toBe(true);

    for (const [method, template, names] of asked) {
      const declared = (openapi.paths[template][method.toLowerCase()].parameters || [])
        .filter((parameter) => parameter.in === "query")
        .map((parameter) => parameter.name);
      for (const name of names) {
        expect(declared, `${method} ${template} declares no ?${name}`).toContain(name);
      }
    }
  });

  it("names every route it deliberately does not call", async () => {
    const requests = await requestsTheClientMakes();
    const called = new Set(
      requests.map(({ method, path }) => `${method} ${templateFor(path)}`),
    );

    expect(operations().filter((operation) => !called.has(operation)).sort()).toEqual(UNCALLED);
  });
});

/* -- what the client sends ------------------------------------------------------------- */

const ajv = new Ajv2020({ strict: false, allErrors: true });
ajv.addSchema(openapi, "steward");

/** Validate a body against a named request model, and say which field failed. */
function refusals(component, body) {
  const validate = ajv.getSchema(`steward#/components/schemas/${component}`);
  expect(validate, `Steward's document has no ${component}`).toBeDefined();
  return validate(body)
    ? []
    : validate.errors.map((error) => `${error.instancePath || "/"} ${error.message}`);
}

/** A draft the nursery form itself considers complete. */
const DRAFT = {
  id: "pip",
  name: "Pip",
  char: "Monk",
  role: "errand bot",
  accent: "#a68a4f",
  kind: "claude",
  model: "claude-opus-5",
  agent_id: "",
  summary: "Runs errands.",
  mission: "Fetch what the village asks for.",
  duties: "Fetch things.\nPut them back.",
  rules: "Never leave the burrow unasked.",
  escalation: "Raise needs_human before anything irreversible.",
  soul_body: "A small resident with a large satchel.",
  voice: "Brisk.",
  deploy: false,
  // A granted skill, because the form sends skills as bare *names* and this is the one
  // field where the console's spelling and Steward's model most easily part company: the
  // document said `skills` had to be a list of objects while the API happily took names,
  // and a draft without this key was a test that stepped around its own subject (warren#321).
  skills: ["daily-summary", "write-journal"],
};

/** The skills every resident already holds, which the form subtracts before sending. */
const DEFAULT_SKILLS = new Set(["write-journal"]);

describe("the bodies the console sends are bodies Steward accepts", () => {
  // Every one of these models is `additionalProperties: false`, so this bites in both
  // directions: a field the console invents fails here, and so does one Steward renames.

  it("declares a resident with the body the nursery form builds", () => {
    // The form's own validator and Steward's schema, checked against the same draft: a
    // draft the form calls complete must be a body Steward's model accepts, or the console
    // spends a round trip to be told something it already knew.
    expect(complaints(DRAFT)).toEqual([]);

    const body = declarationBody(DRAFT, DEFAULT_SKILLS);
    expect(body.skills, "the form sends granted skills as bare names").toEqual(["daily-summary"]);
    expect(refusals("ResidentPost", body)).toEqual([]);

    // And the shape the form omits the field entirely for: every skill already a default.
    expect(refusals("ResidentPost", declarationBody(DRAFT, new Set(DRAFT.skills)))).toEqual([]);
  });

  it("writes a declaration as YAML or as data, with the soul and the revision", () => {
    for (const half of [{ text: "version: 0\n" }, { manifest: { version: 0 } }]) {
      const body = { ...half, soul: "---\nname: Pip\n---\n", revision: "sha256:abc" };
      expect(refusals("DeclarationPut", body)).toEqual([]);
    }
  });

  it("creates and updates a skill from the same editor payload", () => {
    const payload = { description: "one line", body: "the instructions", defaults: false };
    expect(refusals("SkillPost", { ...payload, name: "daily-summary" })).toEqual([]);
    expect(refusals("SkillBody", { ...payload, revision: "sha256:abc" })).toEqual([]);
  });

  it("posts a job with the fields the board form collects", () => {
    expect(
      refusals("JobPost", { title: "Sweep the log", detail: "", required_skills: ["triage"] }),
    ).toEqual([]);
  });

  it("decides an approval, with and without an edit", () => {
    expect(refusals("ApprovalDecision", { decision: "approve" })).toEqual([]);
    expect(refusals("ApprovalDecision", { decision: "edit", edit: { to: "somebody" } })).toEqual([]);
    expect(refusals("ApprovalDecision", { decision: "maybe" })).not.toEqual([]);
  });
});

/* -- what the client reads back -------------------------------------------------------- */

function validateResponse(method, path, status, answer) {
  const schema = openapi.paths[path][method.toLowerCase()].responses[status]
    .content["application/json"].schema;
  expect(schema.$ref, `${method} ${path} must name a response model`).toBeDefined();
  const validate = ajv.compile({ ...schema, components: openapi.components });
  return validate(answer) ? [] : validate.errors;
}

describe("the answers the console renders", () => {
  it("validates the routine rows used by the rendering suite, including nullable fields", () => {
    for (const answer of [
      ROUTINES,
      { ...ROUTINES, routines: [] },
      { ...ROUTINES, scheduler: { alive: null, last_tick: null, stale_after_s: 360 },
        routines: [{ ...ROUTINE, anchor: null, next_fire: null, enabled: false,
          last_run: null, last_request: null }] },
      { ...ROUTINES, routines: [{ ...ROUTINE, last_request: RUN_REQUEST }] },
    ]) expect(validateResponse("GET", "/routines", 200, answer)).toEqual([]);

    const missing = structuredClone(ROUTINES);
    delete missing.routines[0].last_run;
    expect(validateResponse("GET", "/routines", 200, missing)).not.toEqual([]);
    const wrong = structuredClone(ROUTINES);
    wrong.scheduler.alive = "yes";
    expect(validateResponse("GET", "/routines", 200, wrong)).not.toEqual([]);
  });

  it("carries an accepted run through the client and polls its typed ledger outcome", async () => {
    let record = structuredClone(RUN_REQUEST);
    const fetch = vi.fn(async (path, init) => {
      const receipt = init.method === "POST";
      const body = receipt ? RUN_RECEIPT : record;
      expect(validateResponse(receipt ? "POST" : "GET",
        receipt ? "/residents/{resident_id}/routines/{routine_id}/run" : "/requests/{request_id}",
        receipt ? 202 : 200, body)).toEqual([]);
      return { status: receipt ? 202 : 200, ok: true, text: async () => JSON.stringify(body) };
    });
    const client = createStewardClient({ credential: held(), fetch });
    const accepted = await client.runRoutine("hob", "daily-summary");
    const confirm = confirmRun(client, accepted.request_id);
    expect(await confirm()).toBeNull();
    record = { ...record, outcome: "ran", detail: { ...record.detail, run_id: "run-7" } };
    expect(await confirm()).toEqual({ state: "confirmed", why: "steward's log: ran (run run-7)." });
    record = { ...record, outcome: "failed", detail: { routine: "hob/daily-summary", error: "timeout" } };
    expect(await confirm()).toEqual({ state: "failed", why: "steward's log: failed — timeout" });
    expect(fetch.mock.calls.at(-1)[0]).toBe("/requests/request-7");
    expect(validateResponse("GET", "/requests", 200, { requests: [record] })).toEqual([]);
    expect(validateResponse("GET", "/requests/{request_id}", 200,
      { ...RUN_REQUEST, detail: {} })).toEqual([]);
    const missing = { ...RUN_REQUEST };
    delete missing.outcome;
    expect(validateResponse("GET", "/requests/{request_id}", 200, missing)).not.toEqual([]);
    expect(validateResponse("GET", "/requests/{request_id}", 200,
      { ...RUN_REQUEST, detail: null })).not.toEqual([]);
    expect(validateResponse("POST", "/residents/{resident_id}/routines/{routine_id}/run", 202,
      { ...RUN_RECEIPT, status: "ran" })).not.toEqual([]);
  });

  it.each([
    ["GET", "/requests/{request_id}", 404, "unknown_request"],
    ["POST", "/residents/{resident_id}/routines/{routine_id}/run", 409, "routine_disabled"],
    ["POST", "/residents/{resident_id}/routines/{routine_id}/run", 403, "session_credential_forbidden"],
    ["GET", "/routines", 401, "unauthorized"],
  ])("validates and preserves a %s %s %s refusal", async (method, path, status, code) => {
    const answer = { detail: { error: code, message: "Steward refused this request." } };
    expect(validateResponse(method, path, status, answer)).toEqual([]);
    expect(validateResponse(method, path, status, { detail: { error: code } })).not.toEqual([]);
    const client = createStewardClient({ credential: held(), fetch: async () => ({
      status, ok: false, text: async () => JSON.stringify(answer),
    }) });
    await expect(client.call(path, { method })).rejects.toMatchObject({ status, code, raw: answer });
  });
});
