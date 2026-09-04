/* Chronicle's bounded `diagnostics` array, read as facts rather than as JSON.
 *
 * The projection has always carried this: the newest 200 records of everything the reducer
 * could not fold cleanly — a line of the event log that did not validate, two requests
 * claiming one approval id, a routine that finished without ever starting. Nothing in this
 * repo read it (warren#279), so every one of them was `curl`-only.
 *
 * warren#276 added the entry that made that worth fixing. A `chat_message_dropped` record
 * says an outsider knocked on a resident's chat bot and was deliberately answered with
 * silence — the only diagnostic somebody outside the fleet causes, and the only one whose
 * entire reason for existing is that an operator should notice it.
 *
 * The rules are here rather than in the page because they are about the data and not about
 * pixels: a knock storm is *one* fact, and what a stranger typed is not a fact this system
 * holds at all. `fields` sits with them because it answers the same question — what does a
 * record carry — for the kinds nobody has written a renderer for.
 *
 * warren#278 then bounded the storm at both ends — Steward records one knock per stranger
 * per door per window, Chronicle caps what knocks may take of the channel — which means the
 * number of records here stopped being the number of knocks. `suppressed` is the
 * difference, and folding it back in is why this file reads a field the whitelist does not
 * carry.
 */

import { byLatest, bySoonest } from "./console/time.js";

/** The one record an outsider causes. Spelled once; the page and the rail both read it. */
export const KNOCK = "chat_message_dropped";

/**
 * The heading for records that carry no `kind`.
 *
 * Not hypothetical: Chronicle seeds the array with the resident report's own diagnostics,
 * and those are `{file, path, message}` — a manifest that did not validate, named by file
 * and JSON path (`chronicle/residents.py`).
 */
export const UNNAMED = "(no kind)";

/**
 * The fields a knock is allowed to keep.
 *
 * A whitelist, deliberately: warren#279 asks that the panel never render a stranger's
 * message and never grow a field for one. The event carries no text by design
 * (`steward/docs/chat.md`), `DiagnosticWire` is `extra="allow"`, and this is what stops a
 * record that arrived carrying some from reaching a screen.
 *
 * **Say what this covers, exactly**: a record whose `kind` is `chat_message_dropped`. The
 * `fields` renderer below spreads any *other* record, which is its job — a kind nobody has
 * written a panel for is still worth showing. What keeps that honest is Chronicle's own
 * rule rather than this list: a diagnostic names what went wrong without quoting the input
 * that caused it (`chronicle/docs/resident-manifest.md` — rejected values are refused
 * "without echoing their values in diagnostics"). Two halves, in two repositories'
 * directories, and this is the half townhall owns.
 */
const KNOCK_FIELDS = ["agent_id", "project", "route", "address", "from", "reason"];

/** What each kind is, in the words an operator needs to decide whether to care. */
export const KIND_WORDS = {
  [KNOCK]: {
    title: "Knocks nobody answered",
    note:
      "Somebody who is not a named operator messaged a resident's chat bot, or messaged it " +
      "in a group, and was answered with silence — a reply of any kind would confirm to a " +
      "scanner that something is listening. What they wrote is recorded nowhere; the sender " +
      "id is enough to recognise a wrong number or add a second account to " +
      "STEWARD_CHAT_OPERATORS. Steward records one knock per sender per door per catch-up " +
      "window and counts the rest, so the number beside a line is knocks and not records.",
  },
  malformed_event: {
    title: "Event log lines that did not validate",
    note:
      "A record in the log failed the protocol and was left out of the projection entirely. " +
      "The ordinal is its position in the log this snapshot read.",
  },
  approval_collision: {
    title: "Approval ids claimed twice",
    note:
      "Two different requests arrived under one request id. Neither is offered as pending — " +
      "the village cannot say which one a decision would answer.",
  },
  orphan_approval_resolution: {
    title: "Decisions with nothing to decide",
    note: "A resolution arrived for a request this snapshot never saw opened.",
  },
  conflicting_approval_resolution: {
    title: "Approvals answered twice, differently",
    note:
      "One request was closed again with a different decision. The first decision is the " +
      "one the village shows and the one Steward recorded; the later close changed " +
      "nothing. Steward emits no second close of its own, so a line here is a replayed or " +
      "combined log rather than somebody changing their mind.",
  },
  journal_collision: {
    title: "Two journals for one day",
    note:
      "A resident wrote a second journal entry for a day it had already closed. The first " +
      "is the one the village kept.",
  },
  orphan_routine_terminal: {
    title: "Runs that ended without starting",
    note: "A routine reported finishing or failing, and this snapshot never saw it start.",
  },
  [UNNAMED]: {
    title: "Manifests that did not validate",
    note:
      "Records the projection carried through without a kind. Today these are Chronicle's " +
      "resident-manifest diagnostics: a manifest that failed validation is omitted from " +
      "residency and named here by file and JSON path.",
  },
};

/** Knocks first — the one an outsider causes — then the reducer's own findings. */
const KIND_ORDER = [
  KNOCK,
  "malformed_event",
  "approval_collision",
  "orphan_approval_resolution",
  "conflicting_approval_resolution",
  "journal_collision",
  "orphan_routine_terminal",
  UNNAMED,
];

const kindOf = (record) =>
  typeof record?.kind === "string" && record.kind ? record.kind : UNNAMED;

/**
 * How many knocks one record stands for: itself, plus the ones Steward counted into it.
 *
 * Chronicle validates `suppressed` as a non-negative integer before a record can reach a
 * snapshot, but `DiagnosticWire` is `extra="allow"` and this number is drawn on a screen as
 * a count of things that happened — so anything that is not a whole positive number stands
 * for nothing beyond the record itself.
 */
const knocksIn = (record) => {
  const suppressed = record?.suppressed;
  return Number.isSafeInteger(suppressed) && suppressed > 0 ? suppressed + 1 : 1;
};

/** "3 knocks", "1 knock" — the counting both the page and the rail do. */
export const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

/**
 * How many times somebody outside the fleet rang a doorbell.
 *
 * Knocks, not records — the rail and the page have to agree, and since warren#278 one
 * record can stand for two hundred knocks. A rail reading "1 knock" beside a page reading
 * "200×" would make the count look like a bug in whichever the operator saw second.
 */
export const knockCount = (records) =>
  (records || [])
    .filter((record) => kindOf(record) === KNOCK)
    .reduce((total, record) => total + knocksIn(record), 0);

/**
 * One line per sender, per door, per reason — however many times they knocked.
 *
 * Two hundred knocks from one bot scanner is one thing that happened, and a page that drew
 * two hundred rows for it would bury the two knocks that matter under the storm. The reason
 * is part of the key rather than folded away: "not an operator" and "not a private
 * conversation" are different mistakes, and one person can make both.
 *
 * Since warren#278 the fold is done twice over: Steward already collapses a storm into one
 * event per stranger per door per catch-up window, and the knocks it swallowed arrive as
 * `suppressed` on the record it did emit. So `count` adds those back — three records
 * standing for two hundred knocks read "200×", not "3×", which is the same lie this fold
 * exists to avoid told the other way round.
 */
export function foldKnocks(records) {
  const lines = new Map();
  for (const record of records || []) {
    const named = Object.fromEntries(KNOCK_FIELDS.map((field) => [field, record?.[field]]));
    const key = JSON.stringify(KNOCK_FIELDS.map((field) => named[field] ?? null));
    const seen = lines.get(key);
    if (!seen) {
      const count = knocksIn(record);
      lines.set(key, { key, ...named, count, first: record?.ts, last: record?.ts });
      continue;
    }
    seen.count += knocksIn(record);
    if (bySoonest(record?.ts, seen.first) < 0) seen.first = record?.ts;
    if (byLatest(record?.ts, seen.last) < 0) seen.last = record?.ts;
  }
  return [...lines.values()].sort((left, right) => byLatest(left.last, right.last));
}

/**
 * The array, in groups an operator can read one at a time.
 *
 * `records` counts what the projection reported and `entries` is what the page draws, and
 * for knocks those are deliberately different numbers: the storm is still 200 knocks, it is
 * just not 200 rows.
 *
 * An unknown kind is kept and drawn from its own fields. Chronicle's `DiagnosticWire` is
 * `extra="allow"`, so a kind added later needs no contract re-record and no change here —
 * and dropping it would repeat the failure this page exists to fix.
 */
export function groupDiagnostics(records) {
  const buckets = new Map();
  for (const record of records || []) {
    const kind = kindOf(record);
    if (!buckets.has(kind)) buckets.set(kind, []);
    // Reversed: the array is append-ordered and bounded from the end, so later is newer.
    buckets.get(kind).unshift(record);
  }
  const rank = (kind) => {
    const at = KIND_ORDER.indexOf(kind);
    return at < 0 ? KIND_ORDER.length : at;
  };
  return [...buckets.entries()]
    .sort(([left], [right]) => rank(left) - rank(right))
    .map(([kind, held]) => ({
      kind,
      records: held.length,
      entries: kind === KNOCK ? foldKnocks(held) : held,
    }));
}

/** The named facts of an ordinary record, for the renderer that has no idea what it is. */
export function fields(record) {
  return Object.entries(record || {})
    .filter(([name, value]) => name !== "kind" && value !== null && value !== undefined)
    .map(([name, value]) => [
      name,
      typeof value === "object" ? JSON.stringify(value) : String(value),
    ]);
}
