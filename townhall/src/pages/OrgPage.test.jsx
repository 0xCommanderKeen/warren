/* The Org page draws steward's projection, and nothing it made up itself.
 *
 * Two things are worth pinning. The chart is *steward's* — the rows come from the `rank`
 * it computed, so the page cannot decide a different hierarchy from the one `steward org`
 * prints. And a declared handoff steward would refuse to carry is on the screen with its
 * reason: the whole reason to compute a chart from manifests is to see the grants that do
 * not work, so a page that hid them would be worse than the drawing it replaces.
 */

import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NavigationProvider } from "../navigation.jsx";
import { StewardProvider } from "../steward/context.jsx";
import { LedgerProvider } from "../console/ledger.jsx";
import OrgPage from "./OrgPage.jsx";
import { broken, budgetLine, rows, wiring } from "../org.js";

function memoryStorage() {
  const map = new Map();
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => map.set(key, String(value)),
    removeItem: (key) => map.delete(key),
  };
}

const json = (status, body) => ({
  status,
  ok: status >= 200 && status < 300,
  text: async () => JSON.stringify(body),
});

const node = (id, overrides = {}) => ({
  id,
  uid: `uid-${id}`,
  name: id[0].toUpperCase() + id.slice(1),
  role: `${id} bot`,
  accent: "#a68a4f",
  summary: null,
  retired: false,
  rank: 0,
  session_grants: [],
  app_grants: [],
  mounts: [],
  budget: { declared: false, daily_cost_usd: null, daily_tokens: null, max_run_seconds: null },
  delegates: false,
  accepts: [],
  ...overrides,
});

const CHART = {
  nodes: [
    node("hob", {
      rank: 0,
      delegates: true,
      session_grants: ["skills.write"],
      mounts: [{ host: "~/Life", container: "/vault", mode: "rw" }],
      budget: { declared: true, daily_cost_usd: 10, daily_tokens: null, max_run_seconds: null },
    }),
    node("pip", { rank: 1, accepts: ["inbox"] }),
  ],
  edges: [
    { sender: "hob", receiver: "pip", named: true, deliverable: true, reason: null },
  ],
  errors: [],
};

function mount(chart, { token = "steward-operator-abc" } = {}) {
  window.history.replaceState({}, "", "/org");
  const storage = memoryStorage();
  if (token !== null) storage.setItem("townhall.steward.operator", token);
  const fetch = vi.fn().mockImplementation(() => Promise.resolve(json(200, chart)));
  return {
    fetch,
    ...render(
      <NavigationProvider base="/">
        <StewardProvider storage={storage} fetch={fetch}>
          <LedgerProvider>
            <OrgPage />
          </LedgerProvider>
        </StewardProvider>
      </NavigationProvider>,
    ),
  };
}

afterEach(cleanup);

describe("the org layout, as functions", () => {
  it("puts each resident in the row steward's rank names, not one it worked out", () => {
    const chart = { nodes: [node("z", { rank: 1 }), node("a", { rank: 0 }), node("b", { rank: 1 })] };

    expect(rows(chart).map((row) => [row.rank, row.nodes.map((entry) => entry.id)])).toEqual([
      [0, ["a"]],
      [1, ["b", "z"]],
    ]);
  });

  it("indexes an edge under both of its ends", () => {
    const links = wiring(CHART);

    expect(links.sends("hob").map((edge) => edge.receiver)).toEqual(["pip"]);
    expect(links.receives("pip").map((edge) => edge.sender)).toEqual(["hob"]);
    expect(links.sends("pip")).toEqual([]);
  });

  it("collects only the handoffs steward said it would not carry", () => {
    const refused = { ...CHART.edges[0], deliverable: false, reason: "the door is shut" };

    expect(broken({ edges: [CHART.edges[0], refused] })).toEqual([refused]);
  });

  it("says 'no cap' out loud rather than rendering unlimited as nothing", () => {
    expect(budgetLine(null)).toBe("no cap");
    expect(budgetLine({ declared: false, daily_cost_usd: null })).toBe("no cap");
    expect(budgetLine({ declared: true, daily_cost_usd: 10, max_run_seconds: 900 })).toBe(
      "$10/day · 900s/run",
    );
    // A cap of zero is falsy, and "no cap" over a resident told to spend nothing would be
    // the exact inversion this line exists to prevent.
    expect(budgetLine({ declared: true, daily_cost_usd: 0 })).toBe("$0/day");
  });
});

describe("the org page", () => {
  it("reads the chart from steward rather than assembling one from /residents", async () => {
    const { fetch } = mount(CHART);

    await screen.findByText("hob");
    expect(fetch.mock.calls.map(([url]) => String(url).split("?")[0])).toEqual(["/org"]);
  });

  it("draws a resident's grants, mounts and declared cap on its card", async () => {
    mount(CHART);

    const card = (await screen.findByText("hob")).closest("article");
    expect(within(card).getByText("skills.write")).toBeTruthy();
    expect(within(card).getByText("/vault (rw)")).toBeTruthy();
    expect(within(card).getByText("$10/day")).toBeTruthy();
  });

  it("names who hands work to whom on both ends of the edge", async () => {
    mount(CHART);

    const manager = (await screen.findByText("hob")).closest("article");
    const worker = screen.getByText("pip").closest("article");
    expect(within(manager).getByText(/hands work to pip/)).toBeTruthy();
    expect(within(worker).getByText(/takes work from hob/)).toBeTruthy();
  });

  it("does not let a card claim a handoff steward would refuse", async () => {
    // The card used to read "hands work to pip" off every edge, deliverable or not — so it
    // asserted a working handoff two sections above the panel saying it would not happen.
    mount({
      ...CHART,
      edges: [
        { sender: "hob", receiver: "pip", named: true, deliverable: false, reason: "shut" },
      ],
    });

    const manager = (await screen.findByText("hob")).closest("article");
    const worker = screen.getByText("pip").closest("article");
    expect(within(manager).queryByText(/^hands work to pip$/)).toBeNull();
    expect(within(manager).getByText(/declared, but refused: pip/)).toBeTruthy();
    expect(within(worker).queryByText(/^takes work from hob$/)).toBeNull();
    expect(within(worker).getByText(/claimed by, but refused: hob/)).toBeTruthy();
  });

  it("keeps a declared handoff steward would refuse, with the reason", async () => {
    mount({
      ...CHART,
      edges: [
        {
          sender: "hob",
          receiver: "pip",
          named: true,
          deliverable: false,
          reason: "the receiver declares no active route of kind 'delegation'",
        },
      ],
    });

    await screen.findByText("hob → pip");
    expect(
      screen.getByText(/the receiver declares no active route of kind 'delegation'/),
    ).toBeTruthy();
  });

  it("shows a retired resident rather than dropping it off the chart", async () => {
    mount({ ...CHART, nodes: [node("gone", { retired: true })], edges: [] });

    const card = (await screen.findByText("gone")).closest("article");
    expect(within(card).getByText("retired")).toBeTruthy();
  });

  it("says what steward could not read rather than drawing a quiet half-fleet", async () => {
    mount({ nodes: [], edges: [], errors: ["residents/broken/manifest.yaml: error: id"] });

    await waitFor(() =>
      expect(screen.getByText(/residents\/broken\/manifest.yaml/)).toBeTruthy(),
    );
    expect(screen.getByText("No chart to draw.")).toBeTruthy();
  });

  it("asks for a credential before reading anything", () => {
    const { fetch } = mount(CHART, { token: null });

    expect(screen.getByText("Unlock the write path")).toBeTruthy();
    expect(fetch).not.toHaveBeenCalled();
  });
});
