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
 * The functions are here rather than in the page because both rules worth having are about
 * data and not about pixels: a knock storm is *one* fact, and what a stranger typed is not
 * a fact this system holds at all.
 */

import { bySoonest, instant } from "./console/time.js";

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
 * A whitelist, deliberately, and it is the whole of warren#279's "do not render message
 * text" requirement made structural. The event carries no text by design
 * (`steward/docs/chat.md`) and `DiagnosticWire` is `extra="allow"`, so the only way a
 * stranger's words could ever reach an operator's screen is a renderer that spreads the
 * record. Nothing here spreads it.
 */
const KNOCK_FIELDS = ["agent_id", "project", "route", "address", "from", "reason"];

/** What each kind is, in the words an operator needs to decide whether to care. */
export const KIND_WORDS = {
  [KNOCK]: {
    title: "Knocks nobody answered",
    note:
      "Somebody who is not a named operator messaged a resident's chat bot, or messaged it " +
      "in a group, and was answered with silence — a reply of any kind would confirm to a " +
      "scanner that something is listening. What they wrote is not recorded anywhere: the " +
      "sender id is the part steward keeps, and it is enough to recognise a wrong number, " +
      "add a second account to STEWARD_CHAT_OPERATORS, or notice a stranger.",
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
  "journal_collision",
  "orphan_routine_terminal",
  UNNAMED,
];

const kindOf = (record) =>
  typeof record?.kind === "string" && record.kind ? record.kind : UNNAMED;

/**
 * Newest first, with an unreadable clock sorting last rather than becoming the newest.
 *
 * Not `bySoonest` reversed: reversing it would put the unreadable ones at the *top*, which
 * is the one place a record nobody can date must not be.
 */
function byNewest(left, right) {
  const a = instant(left.last);
  const b = instant(right.last);
  if (Number.isNaN(a)) return Number.isNaN(b) ? 0 : 1;
  if (Number.isNaN(b)) return -1;
  return b - a;
}

/**
 * One line per sender, per door, per reason — however many times they knocked.
 *
 * Two hundred knocks from one bot scanner is one thing that happened, and a page that drew
 * two hundred rows for it would bury the two knocks that matter under the storm. The reason
 * is part of the key rather than folded away: "not an operator" and "not a private
 * conversation" are different mistakes, and one person can make both.
 *
 * Nothing rate-limits the event yet (warren#278), so the storm this folds is a real shape
 * and not a defensive one.
 */
export function foldKnocks(records) {
  const lines = new Map();
  for (const record of records || []) {
    const named = Object.fromEntries(KNOCK_FIELDS.map((field) => [field, record?.[field]]));
    const key = JSON.stringify(KNOCK_FIELDS.map((field) => named[field] ?? null));
    const seen = lines.get(key);
    if (!seen) {
      lines.set(key, { key, ...named, count: 1, first: record?.ts, last: record?.ts });
      continue;
    }
    seen.count += 1;
    if (bySoonest(record?.ts, seen.first) < 0) seen.first = record?.ts;
    if (bySoonest(record?.ts, seen.last) > 0 || Number.isNaN(instant(seen.last))) {
      seen.last = record?.ts;
    }
  }
  return [...lines.values()].sort(byNewest);
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
