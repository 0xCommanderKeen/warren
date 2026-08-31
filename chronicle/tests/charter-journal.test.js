"use strict";

const assert = require("node:assert/strict");
const identity = require("../viewer/charter-journal.js");
const transport = require("../viewer/routine-ledger.js");

const charter = { mission: "Keep the house calm.", duties: ["Read the inbox"],
  rules: ["Never send without approval"],
  escalation: { when: ["A reply is needed"], how: "needs_human", note: "Bring context" } };
const locals = [{ file: "life.resident.json", valid: true,
  match: { agent_id: "claude-code:life-agent" } }];
function response(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, async json() { return body; } };
}
function directory(overrides = {}) {
  return { residents: [{ id: "life-agent", agent_id: "claude-code:life-agent",
    project: "life", charter, ...overrides }], errors: [] };
}

(async () => {
  const calls = [];
  const fetcher = async (url, options) => {
    calls.push({ url, options });
    if (url.endsWith("/residents")) return response(directory());
    return response({ resident: "life-agent", entries: [
      { date: "2026-08-23", routine: "close", resident: "life-agent", text: "Earlier", path: "/private" },
      { date: "2026-08-25", routine: "close", resident: "life-agent", text: "Latest", path: "/private" },
    ] });
  };
  const loaded = await identity.refresh(identity.createState(),
    { url: "http://steward.test/", token: "memory-only" }, locals, fetcher, 1234);
  const record = identity.recordFor(loaded, locals[0]);
  assert.equal(loaded.status, "loaded");
  assert.equal(record.status, "configured");
  assert.deepEqual(record.charter, { ...charter,
    escalation: { kind: "policy", when: charter.escalation.when,
      how: "needs_human", note: "Bring context" } });
  assert.deepEqual(record.journal.entries.map(entry => entry.date), ["2026-08-25", "2026-08-23"]);
  assert.equal(calls[1].url,
    `http://steward.test/residents/life-agent/journal?limit=${identity.JOURNAL_LIMIT}`);
  assert.equal(calls[1].options.headers.Authorization, "Bearer memory-only");
  assert.equal(Object.hasOwn(record.journal.entries[0], "path"), false,
    "private Steward paths are not retained by the viewer cache");

  const feedUnavailable = identity.localFeedUnavailable(loaded, locals);
  const unavailableRecord = identity.recordFor(feedUnavailable, locals[0]);
  assert.equal(feedUnavailable.status, "local-unavailable");
  assert.equal(unavailableRecord.status, "local-unavailable");
  assert.equal(unavailableRecord.stale, true);
  assert.equal(unavailableRecord.journal.status, "local-unavailable");
  assert.equal(unavailableRecord.charter.mission, charter.mission,
    "cached charter is retained only as stale while its local declaration cannot be revalidated");
  const unseenRotation = [{ ...locals[0], home: 17 }];
  assert.equal(identity.recordFor(feedUnavailable, unseenRotation[0]), null,
    "a declaration changed while the feed was down cannot inherit the old association on recovery");
  const recoveredRotation = await identity.refresh(feedUnavailable,
    { url: "http://steward", token: "x" }, unseenRotation,
    async url => url.endsWith("/residents") ? response(directory()) :
      response({ resident: "life-agent", entries: [] }), 1500);
  assert.equal(identity.recordFor(recoveredRotation, unseenRotation[0]).status, "configured");

  const unreachable = await identity.refresh(loaded, { url: "http://down", token: "x" },
    locals, async () => { throw new Error("network down"); }, 2000);
  const stale = identity.recordFor(unreachable, locals[0]);
  assert.equal(unreachable.status, "unreachable");
  assert.equal(stale.stale, true);
  assert.equal(stale.lastSuccessAt, 1234);
  assert.equal(stale.journal.status, "unreachable");
  assert.equal(stale.journal.lastSuccessAt, 1234);
  assert.deepEqual(stale.journal.entries.map(entry => entry.text), ["Latest", "Earlier"]);

  const neverLoaded = await identity.refresh(identity.createState(),
    { url: "http://down", token: "x" }, locals,
    async () => { throw new Error("offline"); }, 2500);
  assert.equal(identity.recordFor(neverLoaded, locals[0]).journal.status, "unreachable");
  assert.equal(identity.recordFor(neverLoaded, locals[0]).journal.lastSuccessAt, null);

  const malformedJournal = await identity.refresh(loaded, { url: "http://steward", token: "x" },
    locals, async url => url.endsWith("/residents") ? response(directory()) :
      response({ resident: "somebody-else", entries: [] }), 3000);
  const malformed = identity.recordFor(malformedJournal, locals[0]);
  assert.equal(malformed.journal.status, "malformed");
  assert.equal(malformed.journal.stale, true);
  assert.match(malformed.journal.diagnostic, /could not read journal/i);

  const empty = await identity.refresh(identity.createState(), { url: "http://steward", token: "x" },
    locals, async url => url.endsWith("/residents") ? response(directory()) :
      response({ resident: "life-agent", entries: [] }), 4000);
  assert.deepEqual(identity.recordFor(empty, locals[0]).journal.entries, []);
  assert.equal(identity.recordFor(empty, locals[0]).journal.status, "loaded");

  const badCharter = await identity.refresh(identity.createState(),
    { url: "http://steward", token: "x" }, locals,
    async url => url.endsWith("/residents") ? response(directory({ charter: { mission: "partial" } })) :
      response({ resident: "life-agent", entries: [] }), 5000);
  assert.equal(identity.recordFor(badCharter, locals[0]).status, "invalid");
  assert.equal(identity.recordFor(badCharter, locals[0]).charter, null);
  const missingCharter = await identity.refresh(identity.createState(),
    { url: "http://steward", token: "x" }, locals,
    async url => url.endsWith("/residents") ? response(directory({ charter: null })) :
      response({ resident: "life-agent", entries: [] }), 5100);
  assert.equal(identity.recordFor(missingCharter, locals[0]).status, "missing");
  assert.match(identity.recordFor(missingCharter, locals[0]).diagnostic, /declares no charter/);

  const ambiguous = identity.matchRemote({ match: { project: "life" } }, [
    { project: "life" }, { project: "life" }]);
  assert.match(ambiguous.error, /more than one/);

  assert.equal(identity.parseJournal({ resident: "r", entries: [
    { date: "not-a-date", text: "partial" }, { date: "2026-08-25", text: "valid" },
  ] }, "r"), null, "one malformed entry rejects the complete response");
  assert.equal(identity.parseJournal({ resident: "r", entries: [
    { date: "2026-08-25", resident: "somebody-else", text: "cross-resident" },
  ] }, "r"), null, "one cross-resident entry rejects the complete response");
  assert.equal(identity.parseCharter({ ...charter, duties: [] }), null);
  for (const note of ["", null]) {
    const parsed = identity.parseCharter({ ...charter,
      escalation: { ...charter.escalation, note } });
    assert.ok(parsed, `the pinned Steward contract accepts escalation note ${JSON.stringify(note)}`);
    assert.equal(parsed.escalation.note, note);
  }
  const timedOut = await identity.refresh(identity.createState(),
    { url: "http://slow", token: "x" }, locals, () => new Promise(() => {}), 5500,
    { timeoutMs: 2 });
  assert.equal(timedOut.status, "unreachable");
  assert.match(timedOut.diagnostics[0], /timed out/);

  assert.equal(identity.recordFor(loaded,
    { ...locals[0], file: "rotated.resident.json" }), null,
  "a manifest rotation cannot inherit another file's cached identity");

  let invalidSlugJournalCalls = 0;
  const invalidSlug = await identity.refresh(identity.createState(),
    { url: "http://steward", token: "x" },
    [{ file: "x", valid: true, match: { project: "odd" } }], async url => {
      if (url.endsWith("/residents")) return response({ residents: [{ id: "odd/id",
        agent_id: null, project: "odd", charter }], errors: [] });
      invalidSlugJournalCalls += 1; return response({ resident: "odd/id", entries: [] });
    }, 6000);
  assert.equal(identity.recordFor(invalidSlug,
    { file: "x", valid: true, match: { project: "odd" } }).status, "invalid");
  assert.equal(invalidSlugJournalCalls, 0, "an invalid Steward slug is never used in a journal URL");

  const agentOnlyLocal = [{ file: "agent-only.json", valid: true,
    match: { agent_id: "codex:agent-only" } }];
  const agentOnly = await identity.refresh(identity.createState(),
    { url: "http://steward", token: "x" }, agentOnlyLocal, async url =>
      url.endsWith("/residents") ? response({ residents: [{ id: "agent-only",
        agent_id: "codex:agent-only", project: null, charter }], errors: [] }) :
        response({ resident: "agent-only", entries: [] }), 6050);
  assert.equal(identity.recordFor(agentOnly, agentOnlyLocal[0]).status, "configured",
    "the Steward contract permits an agent identity with a null project");

  const mixedLocals = [
    { file: "exact.json", valid: true, match: { agent_id: "codex:mixed" } },
    { file: "fallback.json", valid: true, match: { project: "shared.project" } },
  ];
  let mixedJournalCalls = 0;
  const mixed = await identity.refresh(identity.createState(),
    { url: "http://steward", token: "x" }, mixedLocals, async url => {
      if (url.endsWith("/residents")) return response({ residents: [{ id: "mixed-resident",
        agent_id: "codex:mixed", project: "shared.project", charter }], errors: [] });
      mixedJournalCalls += 1; return response({ resident: "mixed-resident", entries: [] });
    }, 6075);
  assert.equal(identity.recordFor(mixed, mixedLocals[0]).status, "configured",
    "exact agent_id declarations reserve their Steward identity first");
  assert.equal(identity.recordFor(mixed, mixedLocals[1]).status, "invalid",
    "a project fallback cannot reuse an exact agent_id reservation");
  assert.equal(mixedJournalCalls, 1, "one remote identity configures at most one local resident");
  const collidingExactLocals = [mixedLocals[0],
    { file: "duplicate-exact.json", valid: true, match: { agent_id: "codex:mixed" } },
    mixedLocals[1]];
  const collidingExact = await identity.refresh(identity.createState(),
    { url: "http://steward", token: "x" }, collidingExactLocals, async url =>
      url.endsWith("/residents") ? response({ residents: [{ id: "mixed-resident",
        agent_id: "codex:mixed", project: "shared.project", charter }], errors: [] }) :
        response({ resident: "mixed-resident", entries: [] }), 6080);
  for (const local of collidingExactLocals) assert.equal(
    identity.recordFor(collidingExact, local).status, "invalid",
    "a collided exact reservation cannot leak to a project fallback");

  const removed = await identity.refresh(loaded, { url: "http://steward", token: "x" },
    locals, async url => url.endsWith("/residents") ? response({ residents: [], errors: [] }) :
      response({}), 6100);
  const removedRecord = identity.recordFor(removed, locals[0]);
  assert.equal(removedRecord.status, "missing");
  assert.equal(removedRecord.journal.status, "missing");
  assert.equal(removedRecord.journal.stale, true);
  assert.equal(removedRecord.journal.lastSuccessAt, 1234);

  const rotated = [{ ...locals[0], home: 9 }];
  assert.equal(identity.recordFor(loaded, rotated[0]), null,
    "same-filename declaration rotation is quarantined before another request");
  const rotationRead = await identity.refresh(loaded, { url: "http://steward", token: "x" },
    rotated, async url => url.endsWith("/residents") ? response(directory()) :
      response({ resident: "life-agent", entries: [] }), 6200);
  assert.equal(identity.recordFor(rotationRead, rotated[0]).localFingerprint,
    identity.localFingerprint(rotated[0]));

  const during = [{ ...locals[0] }];
  let releaseJournal;
  const inFlight = identity.refresh(identity.createState(), { url: "http://steward", token: "x" },
    during, async url => url.endsWith("/residents") ? response(directory()) :
      new Promise(resolve => { releaseJournal = () => resolve(response({ resident: "life-agent", entries: [] })); }),
    6300);
  while (!releaseJournal) await new Promise(resolve => setImmediate(resolve));
  during[0].home = 11;
  releaseJournal();
  const changedDuringRead = await inFlight;
  assert.equal(identity.recordFor(changedDuringRead, during[0]), null,
    "a result cannot attach after the local declaration changes during its request");

  const malformedCandidate = await identity.refresh(identity.createState(),
    { url: "http://steward", token: "x" }, locals, async url => url.endsWith("/residents") ?
      response({ residents: [{ id: null, agent_id: "claude-code:life-agent", project: "life" }], errors: [] }) :
      response({}), 6400);
  assert.equal(identity.recordFor(malformedCandidate, locals[0]).status, "invalid");
  assert.match(malformedCandidate.diagnostics.join(" "), /malformed resident identity/);
  for (const [match, malformed] of [
    [{ agent_id: "claude-code:life-agent" },
      { id: null, agent_id: "claude-code:life-agent", project: "other" }],
    [{ project: "life" }, { id: null, agent_id: "other", project: "life" }],
  ]) {
    const local = [{ file: "collision.json", valid: true, match }];
    const colliding = await identity.refresh(identity.createState(),
      { url: "http://steward", token: "x" }, local, async url => url.endsWith("/residents") ?
        response({ residents: [{ id: "valid", agent_id: "claude-code:life-agent",
          project: "life", charter }, malformed], errors: [] }) :
        response({ resident: "valid", entries: [] }), 6450);
    assert.equal(identity.recordFor(colliding, local[0]).status, "invalid",
      "safe evidence from a malformed matching row quarantines a valid candidate");
    assert.match(identity.recordFor(colliding, local[0]).diagnostic, /more than one/);
  }
  const malformedErrors = await identity.refresh(identity.createState(),
    { url: "http://steward", token: "x" }, locals, async () =>
      response({ residents: [], errors: [{ message: "not the contract" }] }), 6500);
  assert.equal(malformedErrors.status, "malformed");
  const reportedInvalidManifest = await identity.refresh(identity.createState(),
    { url: "http://steward", token: "x" }, locals, async () =>
      response({ residents: [], errors: ["life.resident.json $.match: invalid identity"] }), 6550);
  assert.equal(identity.recordFor(reportedInvalidManifest, locals[0]).status, "invalid",
    "Steward validation errors prevent a false claim that an unmatched resident is absent");

  const duplicateLocals = [
    { file: "first.json", valid: true, match: { agent_id: "agent:first" } },
    { file: "second.json", valid: true, match: { agent_id: "agent:second" } },
  ];
  let duplicateJournalCalls = 0;
  const duplicatedRemote = await identity.refresh(identity.createState(),
    { url: "http://steward", token: "x" }, duplicateLocals, async url => {
      if (url.endsWith("/residents")) return response({ residents: [
        { id: "shared-id", agent_id: "agent:first", project: null, charter },
        { id: "shared-id", agent_id: "agent:second", project: null, charter },
      ], errors: [] });
      duplicateJournalCalls += 1;
      return response({ resident: "shared-id", entries: [] });
    }, 6575);
  for (const local of duplicateLocals) {
    assert.equal(identity.recordFor(duplicatedRemote, local).status, "invalid",
      "every local match affected by a duplicated remote id is quarantined");
  }
  assert.equal(duplicateJournalCalls, 0, "a duplicated remote id is never used as journal authority");
  assert.match(duplicatedRemote.diagnostics.join(" "), /duplicated resident identity/);
  const malformedDuplicate = await identity.refresh(identity.createState(),
    { url: "http://steward", token: "x" }, [duplicateLocals[0]], async url =>
      url.endsWith("/residents") ? response({ residents: [
        { id: "shared-id", agent_id: "agent:first", project: null, charter },
        { id: "shared-id", agent_id: 7, project: "unrelated" },
      ], errors: [] }) : response({ resident: "shared-id", entries: [] }), 6580);
  assert.equal(identity.recordFor(malformedDuplicate, duplicateLocals[0]).status, "invalid",
    "remote-id uniqueness includes malformed rows elsewhere in the directory");

  for (const [field, invalid, localMatch] of [
    ["id", "Bad_Slug", { agent_id: "codex:valid-agent" }],
    ["agent_id", "Codex:bad", { project: "valid-project" }],
    ["project", "bad:project", { agent_id: "codex:valid-agent" }],
  ]) {
    let journalCalls = 0;
    const row = { id: "valid-id", agent_id: "codex:valid-agent",
      project: "valid-project", charter, [field]: invalid };
    const local = { file: `invalid-${field}.json`, valid: true, match: localMatch };
    const invalidIdentity = await identity.refresh(identity.createState(),
      { url: "http://steward", token: "x" }, [local], async url => {
        if (url.endsWith("/residents")) return response({ residents: [row], errors: [] });
        journalCalls += 1; return response({ resident: row.id, entries: [] });
      }, 6590);
    assert.equal(identity.recordFor(invalidIdentity, local).status, "invalid",
      `a contract-invalid Steward ${field} is quarantined`);
    assert.equal(journalCalls, 0, `a contract-invalid Steward ${field} triggers no journal read`);
  }

  for (const [status, code, expected] of [[401, "invalid_token", "authentication"],
    [404, "unknown_resident", "missing"], [409, "journal_unreadable", "malformed"],
    [409, "resident_invalid", "malformed"]]) {
    const classified = await identity.refresh(identity.createState(),
      { url: "http://steward", token: "x" }, locals, async url => url.endsWith("/residents") ?
        response(directory()) : response({ detail: { error: code, message: `exact ${code}` } }, status), 6600);
    const classifiedRecord = identity.recordFor(classified, locals[0]);
    assert.equal(classifiedRecord.journal.status, expected);
    assert.match(classifiedRecord.journal.diagnostic, new RegExp(`exact ${code}`));
  }
  for (const body of [
    { detail: { error: "not_found", message: "wrong code" } },
    { detail: "not the error envelope" }, null,
  ]) {
    const wrong404 = await identity.refresh(loaded,
      { url: "http://steward", token: "x" }, locals, async url =>
        url.endsWith("/residents") ? response(directory()) : response(body, 404), 6650);
    const wrongRecord = identity.recordFor(wrong404, locals[0]);
    assert.equal(wrongRecord.journal.status, "error",
      "only the exact unknown_resident 404 envelope establishes absence");
    assert.equal(wrongRecord.journal.stale, true);
    assert.equal(wrongRecord.journal.lastSuccessAt, 1234,
      "an unrecognized 404 retains the last successful journal as stale");
  }
  const rejectedDirectory = await identity.refresh(identity.createState(),
    { url: "http://steward", token: "bad" }, locals, async () =>
      response({ detail: { error: "invalid_token", message: "invalid bearer token" } }, 401), 6700);
  assert.equal(rejectedDirectory.status, "authentication");
  assert.notEqual(rejectedDirectory.status, "unreachable");
  const missingDirectory = await identity.refresh(identity.createState(),
    { url: "http://steward", token: "x" }, locals, async () =>
      response({ detail: { error: "not_found", message: "directory route absent" } }, 404), 6710);
  assert.equal(missingDirectory.status, "error",
    "GET /residents 404 is a directory read failure, not proof an individual resident is missing");
  assert.equal(identity.recordFor(missingDirectory, locals[0]).status, "error");
  const refusedDirectory = await identity.refresh(loaded,
    { url: "http://steward", token: "x" }, locals, async () =>
      response({ detail: { error: "maintenance", message: "definitive refusal" } }, 503), 6725);
  assert.equal(refusedDirectory.status, "error");
  assert.equal(identity.recordFor(refusedDirectory, locals[0]).stale, true);
  assert.equal(identity.recordFor(refusedDirectory, locals[0]).charter.mission, charter.mission,
    "a definitive failure keeps cached identity while distinguishing it from transport failure");

  const oversized = "ø".repeat(identity.MAX_DIAGNOSTIC_BYTES);
  const exactBytes = "ø".repeat(identity.MAX_DIAGNOSTIC_BYTES / 2);
  assert.equal(transport.boundedRemoteText(exactBytes, identity.MAX_DIAGNOSTIC_BYTES), exactBytes);
  assert.equal(transport.boundedRemoteText(exactBytes + "ø", identity.MAX_DIAGNOSTIC_BYTES), null,
    "the shared transport authority enforces UTF-8 byte boundaries");
  const oversizedDirectory = await identity.refresh(identity.createState(),
    { url: "http://steward", token: "x" }, locals, async () =>
      response({ residents: [], errors: [oversized] }), 6750);
  assert.equal(oversizedDirectory.status, "malformed");
  assert.equal(oversizedDirectory.diagnostics.some(item => item.includes(oversized)), false,
    "oversized directory diagnostics never enter viewer state");
  const multipliedLocals = Array.from({ length: 60 }, (_, index) => ({
    file: `oversized-${index}.json`, valid: true, match: { agent_id: `agent:test-${index}` } }));
  const multipliedDirectory = { residents: multipliedLocals.map((_, index) => ({
    id: `resident-${index}`, agent_id: `agent:test-${index}`, project: null, charter })), errors: [] };
  const oversizedHttp = await identity.refresh(identity.createState(),
    { url: "http://steward", token: "x" }, multipliedLocals, async url =>
      url.endsWith("/residents") ? response(multipliedDirectory) :
        response({ detail: { error: "journal_unreadable", message: oversized } }, 409), 6775);
  assert.equal(oversizedHttp.diagnostics.length, identity.MAX_DIAGNOSTICS);
  assert.equal(oversizedHttp.diagnostics.some(item => item.includes(oversized)), false,
    "multiplied resident failures retain only bounded safe diagnostics");

  let active = 0, peak = 0, releases = [];
  const many = Array.from({ length: 13 }, (_, index) => ({ file: `r${index}.json`, valid: true,
    match: { agent_id: `agent:test-${index}` } }));
  const manyDirectory = { residents: many.map((_, index) => ({ id: `r${index}`,
    agent_id: `agent:test-${index}`, project: "many", charter })), errors: [] };
  const bounded = identity.refresh(identity.createState(), { url: "http://steward", token: "x" },
    many, async url => {
      if (url.endsWith("/residents")) return response(manyDirectory);
      active += 1; peak = Math.max(peak, active);
      return new Promise(resolve => releases.push(() => { active -= 1;
        resolve(response({ resident: decodeURIComponent(url.match(/residents\/([^/]+)\/journal/)[1]), entries: [] })); }));
    }, 6800);
  while (releases.length < identity.MAX_JOURNAL_CONCURRENCY) await new Promise(resolve => setImmediate(resolve));
  while (active || releases.length) {
    const batch = releases; releases = []; batch.forEach(done => done());
    await new Promise(resolve => setImmediate(resolve));
  }
  await bounded;
  assert.equal(peak, identity.MAX_JOURNAL_CONCURRENCY);

  const controller = new AbortController();
  let sawAbort = false;
  const cancelled = identity.refresh(loaded, { url: "http://steward", token: "x" }, locals,
    (_url, options) => new Promise((resolve, reject) => options.signal.addEventListener("abort", () => {
      sawAbort = true; reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
    })), 6900, { signal: controller.signal });
  controller.abort();
  assert.equal(await cancelled, loaded, "an obsolete refresh cannot pollute visible state");
  assert.equal(sawAbort, true);

  let localFeedReads = 0;
  const blockedByLocalFeed = await identity.refresh(loaded,
    { url: "http://steward", token: "replacement" }, locals, async () => {
      localFeedReads += 1; return response(directory());
    }, 7000, { localAvailable: false });
  assert.equal(localFeedReads, 0, "credential refresh cannot read Steward while local truth is unavailable");
  assert.equal(identity.recordFor(blockedByLocalFeed, locals[0]).status, "local-unavailable");
  let feedAvailableDuringRead = true, journalAfterLoss = 0;
  const lostDuringRemoteSuccess = await identity.refresh(loaded,
    { url: "http://steward", token: "replacement" }, locals, async url => {
      if (url.endsWith("/residents")) {
        feedAvailableDuringRead = false;
        return response(directory());
      }
      journalAfterLoss += 1; return response({ resident: "life-agent", entries: [] });
    }, 7050, { localAvailable: () => feedAvailableDuringRead });
  assert.equal(journalAfterLoss, 0);
  assert.equal(identity.recordFor(lostDuringRemoteSuccess, locals[0]).status, "local-unavailable",
    "a successful remote directory response cannot reauthorize identity after local truth is lost");
  const recoveredLocalFeed = await identity.refresh(blockedByLocalFeed,
    { url: "http://steward", token: "replacement" }, locals, async url =>
      url.endsWith("/residents") ? response(directory()) :
        response({ resident: "life-agent", entries: [] }), 7100, { localAvailable: true });
  assert.equal(identity.recordFor(recoveredLocalFeed, locals[0]).status, "configured",
    "identity reads recover only after the local manifest feed is available again");

  console.log("charter and journal reads stay direct, bounded, strict, and stale-aware");
})().catch(error => { console.error(error); process.exitCode = 1; });
