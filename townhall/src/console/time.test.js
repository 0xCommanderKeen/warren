import { describe, expect, it } from "vitest";
import { bySoonest, expired, instant, isTime, soonest, span, stamp, words } from "./time.js";

/* The steward console's audit found two bugs that live in exactly these functions, so they
 * are tested as functions rather than looked for on a screen. */

describe("ordering by instant, not by text (#155)", () => {
  // steward returns next_fire in the routine's own schedule_tz, so a fleet spanning zones
  // produces strings with different offsets. These two are the audit's own example.
  const ljubljana = "2026-08-27T09:00:00+02:00";
  const chicago = "2026-08-27T08:00:00-05:00";

  it("orders the earlier instant first even when the text sorts the other way", () => {
    // The bug, stated: lexicographically "2026-08-27T09…" sorts after "2026-08-27T08…",
    // and the 09:00+02:00 fire happens five hours earlier than the 08:00-05:00 one.
    expect(ljubljana.localeCompare(chicago)).toBeGreaterThan(0);
    expect(bySoonest(ljubljana, chicago)).toBeLessThan(0);
  });

  it("picks the soonest fire across mixed offsets", () => {
    const rows = [
      { routine: "chicago", next_fire: chicago },
      { routine: "ljubljana", next_fire: ljubljana },
    ];
    expect(soonest(rows, (row) => row.next_fire).routine).toBe("ljubljana");
  });

  it("sorts a row whose time cannot be parsed last rather than first", () => {
    const rows = [{ id: "broken", at: "soon" }, { id: "real", at: ljubljana }];
    expect(soonest(rows, (row) => row.at).id).toBe("real");
  });

  it("answers nothing when no row names a time at all", () => {
    expect(soonest([{ at: null }], (row) => row.at)).toBeNull();
    expect(soonest([], (row) => row.at)).toBeNull();
  });

  it("knows what is and is not a time", () => {
    expect(isTime(ljubljana)).toBe(true);
    expect(isTime(null)).toBe(false);
    expect(isTime("nothing scheduled")).toBe(false);
    expect(Number.isNaN(instant(undefined))).toBe(true);
  });
});

describe("expiry as a predicate (#154)", () => {
  const now = Date.parse("2026-08-31T12:00:00Z");

  it("is expired once the deadline has passed", () => {
    expect(expired("2026-08-31T11:59:59Z", now)).toBe(true);
    expect(expired("2026-08-31T12:00:00Z", now)).toBe(true);
  });

  it("is not expired before it", () => {
    expect(expired("2026-08-31T12:00:01Z", now)).toBe(false);
  });

  it("treats no declared deadline as never expiring", () => {
    // The reason this is a function: `null > now` is false, so writing the comparison out
    // at each call site would quietly retire every request that has no expiry at all.
    expect(expired(null, now)).toBe(false);
    expect(expired(undefined, now)).toBe(false);
  });
});

describe("the words a clock shows", () => {
  const now = Date.parse("2026-08-31T12:00:00Z");

  it("counts down and then says the deadline passed", () => {
    expect(words("2026-08-31T12:05:00Z", { mode: "until", now })).toBe("in 5m");
    // Not "5m ago": a countdown that silently becomes a count-up reads as still coming.
    expect(words("2026-08-31T11:55:00Z", { mode: "until", now })).toBe("expired 5m ago");
  });

  it("counts up for a moment that has passed", () => {
    expect(words("2026-08-31T11:55:00Z", { now })).toBe("5m ago");
  });

  it("hands back what steward said when it is not a time", () => {
    expect(words("never", { now })).toBe("never");
    expect(words(null)).toBe("—");
  });

  it("names a span in the console's own vocabulary", () => {
    expect(span(0, 30_000)).toBe("30s");
    expect(span(0, 5 * 60_000)).toBe("5m");
    expect(span(0, 3 * 3_600_000 + 4 * 60_000)).toBe("3h 4m");
    expect(span(0, 26 * 3_600_000)).toBe("1d 2h");
  });

  it("has an absolute moment behind every relative one", () => {
    expect(stamp("2026-08-31T12:00:00Z")).toMatch(/2026/);
    expect(stamp(null)).toBeNull();
  });
});
