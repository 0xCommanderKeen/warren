import { describe, expect, it } from "vitest";
import { NAV, matchPath, matchRoute, navFor, normalizeBase, routeTo, stripBase, withBase } from "./routes.js";

describe("base-prefix routing", () => {
  it("reduces every spelling of a Vite base to one form", () => {
    expect(normalizeBase("/")).toBe("");
    expect(normalizeBase("")).toBe("");
    expect(normalizeBase(undefined)).toBe("");
    expect(normalizeBase("/observatory/")).toBe("/observatory");
    expect(normalizeBase("observatory")).toBe("/observatory");
    expect(normalizeBase("/townhall")).toBe("/townhall");
  });

  it("writes address-bar paths under the mount the build was made for", () => {
    expect(withBase("/", "/")).toBe("/");
    expect(withBase("/skills", "/")).toBe("/skills");
    // The mount root keeps its slash: nginx 301s /observatory to /observatory/, and a
    // link that takes the redirect is a link that reloads the bundle.
    expect(withBase("/", "/observatory/")).toBe("/observatory/");
    expect(withBase("/skills/triage", "/observatory/")).toBe("/observatory/skills/triage");
  });

  it("reads the app route back out of a prefixed path", () => {
    expect(stripBase("/observatory", "/observatory/")).toBe("/");
    expect(stripBase("/observatory/", "/observatory/")).toBe("/");
    expect(stripBase("/observatory/skills/triage", "/observatory/")).toBe("/skills/triage");
    expect(stripBase("/skills", "/")).toBe("/skills");
  });

  it("refuses a path outside the mount rather than guessing at a route", () => {
    // /observatoryfoo is a different mount, not this app's /foo.
    expect(stripBase("/observatoryfoo", "/observatory/")).toBeNull();
    expect(stripBase("/chronicle/state", "/observatory/")).toBeNull();
  });

  it("round-trips every built route through the deployed prefix", () => {
    const base = "/observatory/";
    const routes = [
      routeTo.fleet(), routeTo.agent("d29c-…"), routeTo.residents(),
      routeTo.resident("hob"), routeTo.residentNew(),
      routeTo.residentDeclaration("hob"), routeTo.skills(),
      routeTo.skill("read-inbox"), routeTo.skillNew(), routeTo.org(), routeTo.routines(),
      routeTo.approvals(), routeTo.board(), routeTo.budgets(), routeTo.budgets("hob"),
      routeTo.diagnostics(),
    ];
    for (const route of routes) {
      const path = withBase(route, base);
      expect(stripBase(path, base)).toBe(route);
      expect(matchPath(path, base)).toEqual({ route, ...matchRoute(route) });
    }
  });

  it("matches unknown paths only when they are inside this build's mount", () => {
    expect(matchPath("/observatory/nope", "/observatory/")).toEqual({
      route: "/nope", page: "unknown", params: {},
    });
    expect(matchPath("/chronicle/state", "/observatory/")).toEqual({
      route: null, page: "unknown", params: {},
    });
  });
});

describe("route matching", () => {
  it("names a page and its parameters", () => {
    expect(matchRoute("/")).toEqual({ page: "fleet", params: {} });
    expect(matchRoute("/agents/abc")).toEqual({ page: "agent", params: { uuid: "abc" } });
    expect(matchRoute("/residents")).toEqual({ page: "residents", params: {} });
    expect(matchRoute("/residents/hob")).toEqual({ page: "resident", params: { id: "hob" } });
    expect(matchRoute("/residents/new")).toEqual({ page: "residentNew", params: {} });
    expect(matchRoute("/residents/hob/declaration")).toEqual({
      page: "residentDeclaration",
      params: { id: "hob" },
    });
    expect(matchRoute("/org")).toEqual({ page: "org", params: {} });
    expect(matchRoute("/org/hob")).toEqual({ page: "unknown", params: {} });
    expect(matchRoute("/routines")).toEqual({ page: "routines", params: {} });
    expect(matchRoute("/approvals")).toEqual({ page: "approvals", params: {} });
    expect(matchRoute("/board")).toEqual({ page: "board", params: {} });
    expect(matchRoute("/skills")).toEqual({ page: "skills", params: {} });
    expect(matchRoute("/skills/new")).toEqual({ page: "skillNew", params: {} });
    expect(matchRoute("/skills/read-inbox")).toEqual({ page: "skill", params: { name: "read-inbox" } });
    expect(matchRoute("/budgets")).toEqual({ page: "budgets", params: {} });
    expect(matchRoute("/budgets/hob")).toEqual({ page: "budgets", params: { id: "hob" } });
    expect(matchRoute("/diagnostics")).toEqual({ page: "diagnostics", params: {} });
  });

  it("decodes a percent-encoded name back to the name steward knows", () => {
    expect(matchRoute(routeTo.skill("a b/c"))).toEqual({ page: "skill", params: { name: "a b/c" } });
  });

  it("says a stale deep link is unknown rather than quietly showing the fleet", () => {
    expect(matchRoute("/nope").page).toBe("unknown");
    expect(matchRoute("/skills/a/b").page).toBe("unknown");
    expect(navFor("unknown")).toBeNull();
  });

  it("lights exactly one sidebar entry for every page it owns", () => {
    for (const entry of NAV) {
      for (const page of entry.pages) expect(navFor(page)).toBe(entry.nav);
    }
    expect(NAV.map((entry) => entry.label)).toEqual([
      "Fleet", "Residents", "Org", "Routines", "Approvals", "Board", "Skills", "Budgets", "Queue",
      "Diagnostics",
    ]);
  });

  it("keeps the form that declares a resident out of the id branch", () => {
    // A resident may legitimately be called "new"; the form's address is claimed first, so
    // declaring one could never take this page away from itself.
    expect(matchRoute("/residents/new").page).toBe("residentNew");
    expect(matchRoute("/residents/new/declaration")).toEqual({
      page: "residentDeclaration",
      params: { id: "new" },
    });
  });
});
