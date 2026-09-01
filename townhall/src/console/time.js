/* Telling the time honestly, as the steward console told it.
 *
 * Pure functions, separated from the components that draw them, because two of the
 * console's audit findings live in here and a finding you cannot write a test for is a
 * finding you get to have twice:
 *
 * **#155 — compare instants, never ISO text.** steward returns `next_fire` in *the
 * routine's own zone*, so a fleet spanning time zones produces strings with different
 * offsets. `2026-08-27T09:00:00+02:00` sorts after `2026-08-27T08:00:00-05:00`
 * lexicographically and happens five hours earlier. Every comparison here parses first.
 *
 * **#154 — expiry is a predicate, not a badge.** The console rebuilt its pending list
 * client-side and left the decision buttons live on requests the server was about to
 * close, so an operator was offered a click that answers `409 approval_expired`.
 * `expired()` is the predicate; the pages fetch `status=pending` so steward applies it
 * too, and re-apply it here because a tab left open crosses the deadline on its own.
 */

/** Milliseconds since the epoch, or `NaN` for anything that is not a time. */
export function instant(iso) {
  return Date.parse(String(iso ?? ""));
}

/** Whether `iso` names a moment at all. `null`, `""` and prose are all "no". */
export const isTime = (iso) => !Number.isNaN(instant(iso));

/**
 * Order two ISO timestamps by the moment they name (#155).
 *
 * Unparseable values sort last rather than throwing: a ledger with one broken row should
 * still answer "what is next", and putting the broken one first would make it the answer.
 */
export function bySoonest(left, right) {
  const a = instant(left);
  const b = instant(right);
  if (Number.isNaN(a)) return Number.isNaN(b) ? 0 : 1;
  if (Number.isNaN(b)) return -1;
  return a - b;
}

/**
 * Order two ISO timestamps newest first, with an unreadable one last (#155, the other way).
 *
 * Not `bySoonest` reversed: reversing it puts the unparseable values at the *top*, which is
 * the one place a record nobody can date must not be — "what happened most recently" would
 * answer with the row whose clock is broken.
 */
export function byLatest(left, right) {
  const a = instant(left);
  const b = instant(right);
  if (Number.isNaN(a)) return Number.isNaN(b) ? 0 : 1;
  if (Number.isNaN(b)) return -1;
  return b - a;
}

/** The row whose `pick(row)` names the soonest moment, or `null` when none does. */
export function soonest(rows, pick) {
  return (
    (rows || [])
      .filter((row) => isTime(pick(row)))
      .sort((left, right) => bySoonest(pick(left), pick(right)))[0] ?? null
  );
}

/**
 * Whether a deadline has passed (#154).
 *
 * `null` — no expiry declared — is *not* expired, which is the whole reason this is a
 * function rather than a comparison written out at each call site: `null > now` is false
 * and would quietly retire every request that never had a deadline.
 */
export function expired(iso, now = Date.now()) {
  if (!isTime(iso)) return false;
  return instant(iso) <= now;
}

/** A rough duration between two instants, in the console's own vocabulary. */
export function span(fromMs, toMs) {
  const seconds = Math.max(0, Math.round((toMs - fromMs) / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
  return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h`;
}

/**
 * The words a clock shows.
 *
 * `mode: "until"` is for a deadline, and says "expired 4m ago" rather than "4m ago" once
 * it passes — a countdown that silently becomes a count-up reads as though the thing is
 * still coming. Anything unparseable is returned as it arrived: steward said it, and
 * rewriting it into "Invalid Date" would hide which field is wrong.
 */
export function words(iso, { mode = "ago", now = Date.now() } = {}) {
  if (!iso) return "—";
  const at = instant(iso);
  if (Number.isNaN(at)) return String(iso);
  if (at > now) return `in ${span(now, at)}`;
  return mode === "until" ? `expired ${span(at, now)} ago` : `${span(at, now)} ago`;
}

/** The absolute moment, for the tooltip behind every relative one. */
export function stamp(iso) {
  if (!iso) return null;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return String(iso);
  return at.toLocaleString([], {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
