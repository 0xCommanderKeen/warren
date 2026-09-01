/* What the projection could not do cleanly, and who knocked (warren#279).
 *
 * Chronicle has carried a bounded `diagnostics` array since long before the chat bridge,
 * and until now nothing in this repo read it: the snapshot key was listed in the transport
 * and dropped on the floor. warren#276 put a knock at a resident's chat door in there — the
 * one entry an *outsider* causes — which is what made rendering the whole array worth doing.
 *
 * Two things are worth protecting here and neither can be seen by reading the page:
 * that a knock storm reads as one fact rather than two hundred rows, and that no amount of
 * arriving text ever reaches the screen.
 */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import fixture from "./fixtures/complete-v1.js";
import { viewModel } from "./model.js";
import { KNOCK, UNNAMED, fields, foldKnocks, groupDiagnostics } from "./diagnostics.js";
import DiagnosticsPage from "./pages/DiagnosticsPage.jsx";

const knock = (overrides = {}) => ({
  kind: KNOCK,
  agent_id: "claude:keeper",
  project: "warren",
  route: "chat",
  address: "telegram:pip",
  from: "88213311",
  reason: "not an operator",
  ts: "2026-09-01T09:00:00.000Z",
  ...overrides,
});

/** N knocks a second apart, all from one sender at one door. */
const storm = (count, overrides = {}) =>
  Array.from({ length: count }, (_, index) =>
    knock({ ts: new Date(Date.parse("2026-09-01T09:00:00.000Z") + index * 1000).toISOString(), ...overrides }),
  );

const model = (diagnostics) => viewModel({ ...fixture.snapshot, diagnostics });

const show = (diagnostics) => render(<DiagnosticsPage model={model(diagnostics)} />);

afterEach(cleanup);

/* -- folding a knock storm ------------------------------------------------------------ */

describe("folding knocks", () => {
  it("reads repeated knocks from one sender as one line with a count", () => {
    const folded = foldKnocks(storm(200));

    expect(folded).toHaveLength(1);
    expect(folded[0].count).toBe(200);
    expect(folded[0].first).toBe("2026-09-01T09:00:00.000Z");
    expect(folded[0].last).toBe("2026-09-01T09:03:19.000Z");
  });

  it("keeps a different sender, a different door and a different reason apart", () => {
    const folded = foldKnocks([
      knock(),
      knock({ from: "5150" }),
      knock({ address: "telegram:librarian" }),
      knock({ reason: "not a private conversation" }),
      knock({ agent_id: "claude:other" }),
    ]);

    expect(folded).toHaveLength(5);
    expect(folded.every((line) => line.count === 1)).toBe(true);
  });

  it("carries the named fields and nothing else", () => {
    // The event has no message text by design (steward/docs/chat.md), and `DiagnosticWire`
    // is `extra="allow"` — so a record could arrive carrying one. Folding whitelists, which
    // is why the panel cannot grow a field for text by accident.
    const [line] = foldKnocks([knock({ text: "hello from a stranger", payload: { text: "x" } })]);

    expect(line.text).toBeUndefined();
    expect(line.payload).toBeUndefined();
    expect(Object.keys(line).sort()).toEqual(
      ["address", "agent_id", "count", "first", "from", "key", "last", "project", "reason", "route"].sort(),
    );
  });

  it("puts the most recent knock first", () => {
    const folded = foldKnocks([
      knock({ from: "old", ts: "2026-09-01T08:00:00.000Z" }),
      knock({ from: "new", ts: "2026-09-01T10:00:00.000Z" }),
    ]);

    expect(folded.map((line) => line.from)).toEqual(["new", "old"]);
  });

  it("keeps a knock whose timestamp is unreadable rather than dropping it", () => {
    const folded = foldKnocks([knock({ ts: undefined }), knock({ from: "5150" })]);

    expect(folded).toHaveLength(2);
    // An unreadable clock sorts last; it must not become the newest thing on the page.
    expect(folded[0].from).toBe("5150");
  });
});

/* -- grouping the whole array --------------------------------------------------------- */

describe("grouping diagnostics by kind", () => {
  it("puts knocks first, whatever order the projection appended them in", () => {
    const groups = groupDiagnostics([
      { kind: "malformed_event", ordinal: 3, message: "invalid record" },
      ...storm(4),
      { kind: "journal_collision", day: "2026-09-01", agent_id: "claude:keeper" },
    ]);

    expect(groups.map((group) => group.kind)).toEqual([
      KNOCK, "malformed_event", "journal_collision",
    ]);
  });

  it("counts records rather than folded lines", () => {
    const [knocks] = groupDiagnostics(storm(200));

    expect(knocks.records).toBe(200);
    expect(knocks.entries).toHaveLength(1);
  });

  it("keeps a kind chronicle grew after this page was written", () => {
    // `DiagnosticWire` is `extra="allow"` and new kinds need no contract re-record, so an
    // unknown kind is a thing that will happen. Dropping it would make this page lie by
    // omission the way the missing render did.
    const groups = groupDiagnostics([{ kind: "lease_swept", task_id: "t-9" }]);

    expect(groups.map((group) => group.kind)).toEqual(["lease_swept"]);
    expect(groups[0].entries).toEqual([{ kind: "lease_swept", task_id: "t-9" }]);
  });

  it("gathers the records that carry no kind at all", () => {
    // Chronicle seeds the array with the resident report's own diagnostics, and those are
    // `{file, path, message}` with no kind (chronicle/residents.py `_diagnostic`).
    const groups = groupDiagnostics([
      { file: "residents/broken.resident.json", path: "$.soul.name", message: "is required" },
    ]);

    expect(groups.map((group) => group.kind)).toEqual([UNNAMED]);
  });

  it("orders the newest record of an ordinary kind first", () => {
    // The array is append-ordered and bounded from the end, so later is newer.
    const groups = groupDiagnostics([
      { kind: "approval_collision", request_id: "first" },
      { kind: "approval_collision", request_id: "second" },
    ]);

    expect(groups[0].entries.map((item) => item.request_id)).toEqual(["second", "first"]);
  });
});

describe("the named fields of an ordinary record", () => {
  it("lists what the record carries and leaves the kind to the heading", () => {
    expect(fields({ kind: "journal_collision", day: "2026-09-01", agent_id: "claude:keeper" })).toEqual([
      ["day", "2026-09-01"],
      ["agent_id", "claude:keeper"],
    ]);
  });

  it("renders a structured value rather than [object Object]", () => {
    expect(fields({ kind: "x", detail: { a: 1 } })).toEqual([["detail", '{"a":1}']]);
  });
});

/* -- the page ------------------------------------------------------------------------- */

describe("the diagnostics page", () => {
  it("waits for a snapshot rather than claiming the village is clean", () => {
    render(<DiagnosticsPage model={null} />);

    expect(screen.getByText(/reading/i)).toBeTruthy();
    expect(screen.queryByText(/nothing to report/i)).toBeNull();
  });

  it("says the channel is empty when the projection reported nothing", () => {
    show([]);

    expect(screen.getByText(/Nothing to report/i)).toBeTruthy();
  });

  it("names the door, the route, who knocked and why they got silence", () => {
    show([knock()]);

    for (const fact of ["claude:keeper", "warren", "chat", "telegram:pip", "88213311", "not an operator"]) {
      expect(screen.getByText(fact), fact).toBeTruthy();
    }
  });

  it("draws a knock storm as one line with a count, not two hundred rows", () => {
    show(storm(200));

    expect(screen.getAllByText("telegram:pip")).toHaveLength(1);
    expect(screen.getByText("200×")).toBeTruthy();
    // How long it went on for: one knock and a knock every second for three minutes are
    // different facts, and the folded line is the only place left to say which.
    expect(screen.getByText("over 3m")).toBeTruthy();
  });

  it("renders no message text, even from a record that arrived carrying one", () => {
    show([knock({ text: "hello from a stranger" })]);

    expect(screen.queryByText(/hello from a stranger/)).toBeNull();
  });

  it("says the channel is full, because a full one has already lost the oldest", () => {
    show(storm(fixture.snapshot.capacity.diagnostics));

    expect(screen.getByText(/full/i)).toBeTruthy();
  });

  it("renders a manifest that did not validate by its file and its path", () => {
    show([{ file: "residents/broken.resident.json", path: "$.soul.name", message: "is required" }]);

    expect(screen.getByText("residents/broken.resident.json")).toBeTruthy();
    expect(screen.getByText("$.soul.name")).toBeTruthy();
    expect(screen.getByText("is required")).toBeTruthy();
  });

  it("renders a kind it has never heard of by the fields it carries", () => {
    show([{ kind: "lease_swept", task_id: "t-9" }]);

    expect(screen.getByText("lease_swept")).toBeTruthy();
    expect(screen.getByText("t-9")).toBeTruthy();
  });
});

/* -- the rail ------------------------------------------------------------------------- */

describe("the rail", () => {
  beforeEach(() => window.history.replaceState({}, "", "/"));

  it("says how many knocks the snapshot is carrying, and links to them", async () => {
    const App = (await import("./App.jsx")).default;
    vi.spyOn(window, "fetch").mockResolvedValue({
      status: 200,
      json: async () => ({
        kind: "snapshot",
        snapshot: { ...fixture.snapshot, diagnostics: [...storm(3), { kind: "malformed_event", ordinal: 1 }] },
      }),
    });

    render(<App />);

    const link = await screen.findByRole("link", { name: /4 diagnostics · 3 knocks/i });
    expect(link.getAttribute("href")).toBe("/diagnostics");
  });
});
