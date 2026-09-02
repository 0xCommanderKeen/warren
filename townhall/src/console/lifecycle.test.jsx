/* Retiring and provisioning from the resident's record (warren#331).
 *
 * The thing worth protecting here cannot be seen by reading the page: that nothing
 * irreversible is ever one click away. Both acts rehearse first, and the button that does it
 * for real does not exist on the screen until steward's own plan is on it — so a stray click,
 * a repeated keypress or a test that stopped reading halfway cannot stop a container.
 */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NavigationProvider } from "../navigation.jsx";
import { StewardProvider } from "../steward/context.jsx";
import ResidentDetail from "../pages/ResidentDetail.jsx";

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

const RESIDENT = {
  id: "life-agent",
  uid: "0198-uid",
  retired: false,
  path: "residents/life-agent/manifest.yaml",
  soul: { name: "Hob", char: "Keeper", role: "household agent", accent: "#a68a4f" },
  charter: { mission: "Keep the house.", duties: ["Read the inbox"], rules: ["Ask first"], escalation: "needs_human" },
  skills: [],
  effective_skills: [],
  runner: { kind: "claude", model: "claude-opus-5" },
};

/** Enough of a budget for the record page's own panel; this file is not about spending. */
const BUDGET = {
  resident: "life-agent",
  window: { tz: "Europe/Ljubljana", day: "2026-09-02", end: "2026-09-02T22:00:00.000Z" },
  spent: { runs: 0, tokens: 0, cost_usd: 0, duration_s: 0, unreported_runs: 0 },
  budgets: [],
  max_run_seconds: null,
  paused: false,
  summary: "no cap declared",
};

/** The plan steward answers a `dry_run: true` retire with. */
const RETIRE_PLAN = {
  request_id: "req-1",
  message: "nothing was marked, committed, stopped, or removed: this is the plan",
  resident: "life-agent",
  manifest_path: "residents/life-agent/manifest.yaml",
  marked: true,
  stopped: false,
  scrubbed: false,
  commands: [
    "ssh Miha@dxp2800 docker compose -f ~/docker/steward-life-agent/docker-compose.yaml down --remove-orphans",
    "ssh Miha@dxp2800 rm -f ~/docker/steward-life-agent/.env ~/docker/steward-life-agent/docker-compose.yaml",
  ],
  commit: null,
  dry_run: true,
  note: "a dry run stops nothing and commits nothing",
};

const RETIRE_DONE = {
  ...RETIRE_PLAN,
  request_id: "req-2",
  message: "the manifest now says retired and that decision is committed; the container is down",
  stopped: true,
  scrubbed: true,
  commit: "9f1c0a77bbddee",
  dry_run: false,
  note: "retired",
};

const PROVISION_PLAN = {
  request_id: "req-3",
  message: "nothing was sent, run, or written: this is the plan",
  resident: "life-agent",
  act: "provision",
  dry_run: true,
  changed: false,
  provision: {
    target: { host: "dxp2800", user: "Miha", path: "~/docker/steward-life-agent", container: "steward-life-agent", image: "steward-resident:latest" },
    commands: ["ssh Miha@dxp2800 docker compose … up -d"],
    sent: false,
  },
  register: { ok: true, problems: [], next_fires: [] },
};

/** Steward, scripted by path; the five record reads answer the same empty-but-valid shapes. */
function steward({ resident = RESIDENT, posts = {} } = {}) {
  return vi.fn((url, init = {}) => {
    const path = String(url).split("?")[0];
    if (init.method === "POST") {
      const answer = posts[path];
      if (!answer) return Promise.resolve(json(404, { detail: { error: "no stub", message: path } }));
      const body = init.body ? JSON.parse(init.body) : {};
      const chosen = typeof answer === "function" ? answer(body) : answer;
      return Promise.resolve(chosen);
    }
    if (path.endsWith("/budget")) return Promise.resolve(json(200, BUDGET));
    if (path.endsWith("/journal")) return Promise.resolve(json(200, { entries: [] }));
    if (path.endsWith("/inbox")) return Promise.resolve(json(200, { letters: [] }));
    if (path === "/routines") return Promise.resolve(json(200, { routines: [], scheduler: null }));
    return Promise.resolve(json(200, resident));
  });
}

function mount(fetch) {
  const storage = memoryStorage();
  storage.setItem("townhall.steward.operator", "operator-token");
  return render(
    <NavigationProvider base="/">
      <StewardProvider storage={storage} fetch={fetch}>
        <ResidentDetail id="0198-uid" />
      </StewardProvider>
    </NavigationProvider>,
  );
}

const posted = (fetch, path) =>
  fetch.mock.calls.filter(([url, init]) => init?.method === "POST" && String(url) === path);

afterEach(cleanup);

describe("retiring a resident from its record", () => {
  const RETIRE = "/residents/life-agent/retire";

  it("shows steward's plan before it offers to do it", async () => {
    const fetch = steward({
      posts: { [RETIRE]: (body) => json(200, body.dry_run ? RETIRE_PLAN : RETIRE_DONE) },
    });
    mount(fetch);

    // Nothing that retires anything exists yet: the only button is the rehearsal.
    expect(await screen.findByRole("button", { name: /^retire…$/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /retire hob for real/i })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /^retire…$/i }));

    expect(await screen.findByRole("button", { name: /retire hob for real/i })).toBeTruthy();
    expect(posted(fetch, RETIRE)).toHaveLength(1);
    expect(JSON.parse(posted(fetch, RETIRE)[0][1].body).dry_run).toBe(true);
  });

  it("names what stops, what is removed, and what is deliberately left", async () => {
    const fetch = steward({ posts: { [RETIRE]: (body) => json(200, body.dry_run ? RETIRE_PLAN : RETIRE_DONE) } });
    mount(fetch);

    fireEvent.click(await screen.findByRole("button", { name: /^retire…$/i }));
    await screen.findByRole("button", { name: /retire hob for real/i });

    // steward's own argv, not a description of it.
    expect(screen.getByText(/down --remove-orphans/)).toBeTruthy();
    expect(screen.getByText(/rm -f/)).toBeTruthy();
    // The .env is the one worth being sure about, and claude/ is the one worth being told
    // about — a login steward never wrote and does not restore.
    expect(screen.getByText(/BURROW_TOKEN/)).toBeTruthy();
    expect(screen.getByText(/claude\//)).toBeTruthy();
  });

  it("retires only after the plan has been shown, and only once asked again", async () => {
    const fetch = steward({ posts: { [RETIRE]: (body) => json(200, body.dry_run ? RETIRE_PLAN : RETIRE_DONE) } });
    mount(fetch);

    fireEvent.click(await screen.findByRole("button", { name: /^retire…$/i }));
    fireEvent.click(await screen.findByRole("button", { name: /retire hob for real/i }));

    await waitFor(() => expect(posted(fetch, RETIRE)).toHaveLength(2));
    expect(JSON.parse(posted(fetch, RETIRE)[1][1].body).dry_run).toBe(false);
    expect(await screen.findByText(/that decision is committed/)).toBeTruthy();
    // And the confirm is gone: what it confirmed has happened.
    expect(screen.queryByRole("button", { name: /retire hob for real/i })).toBeNull();
  });

  it("puts the plan away on cancel without having sent anything", async () => {
    const fetch = steward({ posts: { [RETIRE]: (body) => json(200, body.dry_run ? RETIRE_PLAN : RETIRE_DONE) } });
    mount(fetch);

    fireEvent.click(await screen.findByRole("button", { name: /^retire…$/i }));
    fireEvent.click(await screen.findByRole("button", { name: /^cancel$/i }));

    expect(screen.queryByRole("button", { name: /retire hob for real/i })).toBeNull();
    expect(posted(fetch, RETIRE)).toHaveLength(1);
  });

  it("renders steward's refusal rather than claiming a retirement", async () => {
    const fetch = steward({
      posts: {
        [RETIRE]: (body) =>
          body.dry_run
            ? json(200, RETIRE_PLAN)
            : json(409, {
                detail: {
                  error: "worktree_refused",
                  message: "the worktree at /srv/steward has uncommitted changes (residents/pip/manifest.yaml)",
                },
              }),
      },
    });
    mount(fetch);

    fireEvent.click(await screen.findByRole("button", { name: /^retire…$/i }));
    fireEvent.click(await screen.findByRole("button", { name: /retire hob for real/i }));

    expect(await screen.findByText(/409 · worktree_refused/)).toBeTruthy();
    expect(
      screen.getByText("the worktree at /srv/steward has uncommitted changes (residents/pip/manifest.yaml)"),
    ).toBeTruthy();
    // The plan stays: the refusal is something to fix and try again, not a reason to start
    // the whole flow over.
    expect(screen.getByRole("button", { name: /retire hob for real/i })).toBeTruthy();
  });
});

describe("a retired resident's record", () => {
  const retired = { ...RESIDENT, retired: true };

  it("offers provision instead of retire, and names the step before it", async () => {
    mount(steward({ resident: retired }));

    expect(await screen.findByRole("button", { name: /^provision…$/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^retire…$/i })).toBeNull();
    // Provision alone is not the way back: steward refuses to build a container for a
    // manifest that says retired, so the page says what has to happen first.
    expect(screen.getByText(/untick/i)).toBeTruthy();
    expect(screen.getByRole("link", { name: /edit the declaration/i })).toBeTruthy();
  });

  it("rehearses a provision before it runs one, exactly as retire does", async () => {
    const fetch = steward({
      resident: retired,
      posts: { "/residents/life-agent/provision": json(200, PROVISION_PLAN) },
    });
    mount(fetch);

    fireEvent.click(await screen.findByRole("button", { name: /^provision…$/i }));

    expect(await screen.findByRole("button", { name: /provision hob for real/i })).toBeTruthy();
    // Addressed by id, never by the uid this page is routed on: the provision route reads
    // `residents/<id>/manifest.yaml` off the disk and a uid names no directory.
    expect(posted(fetch, "/residents/life-agent/provision")).toHaveLength(1);
    expect(screen.getByText("Miha@dxp2800:~/docker/steward-life-agent")).toBeTruthy();
  });
});
