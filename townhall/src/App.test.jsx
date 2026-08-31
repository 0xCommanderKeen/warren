/* The shell, and the write surface hanging off it.
 *
 * These tests drive the real components against a stubbed steward and a stubbed Chronicle,
 * because the two things most worth protecting here cannot be seen by reading the code:
 * that every link survives the `/observatory/` mount, and that a write renders steward's
 * own answer rather than the click's intention.
 */

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NavigationProvider } from "./navigation.jsx";
import { StewardProvider } from "./steward/context.jsx";
import { Gate } from "./console/Gate.jsx";
import SkillsPage from "./pages/SkillsPage.jsx";
import BudgetsPage from "./pages/BudgetsPage.jsx";
import ResidentsPage from "./pages/ResidentsPage.jsx";

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

const COMMIT = {
  committed: true,
  sha: "9f1c0a77bb31e4d5",
  message: "chore(skills): update triage via the API",
  note: "committed as 9f1c0a7",
};

/** Mount a page under a chosen mount path with a scripted steward behind it. */
function mount(ui, { path = "/", base = "/", fetch, token = "operator-token" } = {}) {
  window.history.replaceState({}, "", path);
  const storage = memoryStorage();
  if (token !== null) storage.setItem("townhall.steward.operator", token);
  return render(
    <NavigationProvider base={base}>
      <StewardProvider storage={storage} fetch={fetch}>
        {ui}
      </StewardProvider>
    </NavigationProvider>,
  );
}

beforeEach(() => {
  window.history.replaceState({}, "", "/");
});
afterEach(cleanup);

/* -- the mount ----------------------------------------------------------------------- */

describe("the deployed mount", () => {
  it("writes every link under the prefix the build was made for", async () => {
    const fetch = vi.fn().mockResolvedValue(
      json(200, { skills: [{ name: "triage", description: "Sort it.", default: false, path: "skills/triage/SKILL.md", body_chars: 12, holders: [] }], errors: [] }),
    );
    mount(<SkillsPage page="skills" params={{}} />, { path: "/observatory/skills", base: "/observatory/", fetch });

    const link = await screen.findByRole("link", { name: "triage" });
    // Not "/skills/triage": under the mount that is somebody else's URL.
    expect(link.getAttribute("href")).toBe("/observatory/skills/triage");
    expect(screen.getByRole("link", { name: /new skill/i }).getAttribute("href")).toBe("/observatory/skills/new");
  });

  it("keeps plain links unprefixed when the app owns the root", async () => {
    const fetch = vi.fn().mockResolvedValue(json(200, { skills: [], errors: [] }));
    mount(<SkillsPage page="skills" params={{}} />, { path: "/skills", fetch });
    expect((await screen.findByRole("link", { name: /new skill/i })).getAttribute("href")).toBe("/skills/new");
  });
});

/* -- the credential ------------------------------------------------------------------ */

describe("the write path's front door", () => {
  it("asks for a token before it calls steward at all", async () => {
    const fetch = vi.fn();
    mount(<SkillsPage page="skills" params={{}} />, { fetch, token: null });

    expect(screen.getByText(/Unlock the write path/i)).toBeTruthy();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("sends the token a human typed, and never one baked in", async () => {
    const fetch = vi.fn().mockResolvedValue(json(200, { skills: [], errors: [] }));
    mount(
      <>
        <Gate what="Skills" />
        <SkillsPage page="skills" params={{}} />
      </>,
      { fetch, token: null },
    );

    fireEvent.change(screen.getAllByPlaceholderText(/steward-operator-/i)[0], {
      target: { value: "typed-at-runtime" },
    });
    fireEvent.click(screen.getAllByRole("button", { name: /^unlock$/i })[0]);

    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(fetch.mock.calls[0][1].headers.Authorization).toBe("Bearer typed-at-runtime");
  });

  it("says out loud that a session credential is refused here", () => {
    mount(<Gate what="Skills" />, { token: null });
    expect(screen.getByText(/session_credential_forbidden/)).toBeTruthy();
  });
});

/* -- what steward said --------------------------------------------------------------- */

describe("rendering steward's answer, not the click's intention", () => {
  it("shows the commit a save produced", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(json(200, { name: "triage", description: "Sort it.", body: "Read every message.", defaults: false, revision: "sha256:one", path: "skills/triage/SKILL.md" }))
      .mockResolvedValueOnce(json(200, { request_id: "r1", status: "accepted", name: "triage", revision: "sha256:two", paths: ["skills/triage/SKILL.md"], commit: COMMIT, warnings: [], message: "written and validated against the fleet" }));

    mount(<SkillsPage page="skill" params={{ name: "triage" }} />, { fetch });

    fireEvent.change(await screen.findByDisplayValue("Sort it."), { target: { value: "Sort the inbox." } });
    fireEvent.click(screen.getByRole("button", { name: /replace skill/i }));

    expect(await screen.findByText("9f1c0a77bb")).toBeTruthy();
    expect(screen.getByText(/chore\(skills\): update triage via the API/)).toBeTruthy();
    // The second call is the PUT, carrying the revision the GET returned.
    const [, put] = fetch.mock.calls[1];
    expect(put.method).toBe("PUT");
    expect(JSON.parse(put.body)).toMatchObject({ description: "Sort the inbox.", revision: "sha256:one" });
  });

  it("calls an uncommitted save converged rather than failed", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(json(200, { name: "triage", description: "Sort it.", body: "x", defaults: false, revision: "sha256:one" }))
      .mockResolvedValueOnce(json(200, { status: "accepted", name: "triage", commit: { committed: false, sha: null, note: "nothing to commit" }, warnings: [], message: "written" }));

    mount(<SkillsPage page="skill" params={{ name: "triage" }} />, { fetch });
    fireEvent.click(await screen.findByRole("button", { name: /replace skill/i }));

    expect(await screen.findByText(/nothing to commit/)).toBeTruthy();
    expect(screen.queryByText(/could not|failed/i)).toBeNull();
  });

  it("puts a refusal's diagnostics on the field they name", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(json(200, { name: "triage", description: "Sort it.", body: "x", defaults: false, revision: "sha256:one" }))
      .mockResolvedValueOnce(json(422, {
        detail: {
          error: "skill_invalid",
          message: "the skill does not validate",
          diagnostics: [{ file: "skills/triage/SKILL.md", field: "description", problem: "must be one line", example: "description: Sort the inbox.", severity: "error" }],
        },
      }));

    mount(<SkillsPage page="skill" params={{ name: "triage" }} />, { fetch });
    fireEvent.click(await screen.findByRole("button", { name: /replace skill/i }));

    expect(await screen.findAllByText(/must be one line/)).not.toHaveLength(0);
    expect(screen.getByText(/422 · skill_invalid/)).toBeTruthy();
    // The box itself is marked, which is the whole reason the diagnostics are structured.
    expect(screen.getByDisplayValue("Sort it.").getAttribute("aria-invalid")).toBe("true");
  });

  it("names an unproxied route as never having reached steward", async () => {
    const fetch = vi.fn().mockResolvedValue({ status: 200, ok: true, text: async () => "<!doctype html><html></html>" });
    mount(<SkillsPage page="skills" params={{}} />, { fetch });
    expect(await screen.findByText(/did not reach steward/i)).toBeTruthy();
  });

  it("tells an editor that somebody changed the file first", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(json(200, { name: "triage", description: "d", body: "x", defaults: false, revision: "sha256:one" }))
      .mockResolvedValueOnce(json(409, { detail: { error: "stale_revision", message: "somebody changed it first — re-read it and reapply your change" } }));

    mount(<SkillsPage page="skill" params={{ name: "triage" }} />, { fetch });
    fireEvent.click(await screen.findByRole("button", { name: /replace skill/i }));
    expect(await screen.findByText(/409 · stale_revision/)).toBeTruthy();
  });
});

/* -- budgets ------------------------------------------------------------------------- */

const BUDGET = {
  resident: "life-agent",
  window: { tz: "Europe/Ljubljana", day: "2026-08-31", end: "2026-08-31T22:00:00.000Z" },
  spent: { runs: 6, tokens: 20400, cost_usd: 5.2, duration_s: 812.4, unreported_runs: 0 },
  budgets: [
    { budget: "daily_cost_usd", spent: 5.2, limit: 10, remaining: 4.8, exhausted: false },
    { budget: "daily_tokens", spent: 20400, limit: null, remaining: null, exhausted: false },
  ],
  max_run_seconds: 900,
  paused: false,
  summary: "5.20 of 10 daily_cost_usd",
};

const DECLARATION = {
  id: "life-agent",
  manifest: { version: 0, id: "life-agent", budgets: { daily_cost_usd: 10 }, deploy: { host: "dxp2800" } },
  text: "version: 0\n",
  soul: "---\n---\n",
  soul_file: "soul.md",
  revision: "sha256:decl",
  paths: ["residents/life-agent/manifest.yaml"],
};

describe("budget controls", () => {
  const budgetFetch = (writeAnswer) =>
    vi.fn().mockImplementation((url, init) => {
      if (init?.method === "PUT") return Promise.resolve(writeAnswer);
      if (String(url).endsWith("/budget")) return Promise.resolve(json(200, BUDGET));
      return Promise.resolve(json(200, DECLARATION));
    });

  it("shows the spend numbers beside the knob", async () => {
    mount(<BudgetsPage params={{ id: "life-agent" }} />, { fetch: budgetFetch() });

    // The cap, editable.
    expect(await screen.findByDisplayValue("10")).toBeTruthy();
    // And the measured spend against it, from steward's own ledger.
    expect(screen.getByText("5.2")).toBeTruthy();
    expect(screen.getByText("$5.2000")).toBeTruthy();
    expect(screen.getByText(/Europe\/Ljubljana/)).toBeTruthy();
    expect(screen.getByText("no cap declared")).toBeTruthy();
  });

  it("writes a cap through the declaration, not through a budget endpoint", async () => {
    const fetch = budgetFetch(json(200, { status: "accepted", commit: COMMIT, warnings: [], message: "written and validated" }));
    mount(<BudgetsPage params={{ id: "life-agent" }} />, { fetch });

    fireEvent.change(await screen.findByDisplayValue("10"), { target: { value: "12.5" } });
    fireEvent.click(screen.getByRole("button", { name: /write caps/i }));

    await waitFor(() => expect(fetch.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(true));
    // The commit survives the re-read that follows the save; a receipt swept away by a
    // refresh has told the operator nothing.
    expect(await screen.findByText("9f1c0a77bb")).toBeTruthy();
    const [url, init] = fetch.mock.calls.find(([, call]) => call?.method === "PUT");
    expect(url).toBe("/residents/life-agent/declaration");
    const sent = JSON.parse(init.body);
    expect(sent.manifest.budgets.daily_cost_usd).toBe(12.5);
    // Everything the form has no field for survives the round trip.
    expect(sent.manifest.deploy).toEqual({ host: "dxp2800" });
    expect(sent.revision).toBe("sha256:decl");
  });

  it("clears a cap to unlimited by deleting the key, never by writing a zero", async () => {
    const fetch = budgetFetch(json(200, { status: "accepted", commit: COMMIT, warnings: [], message: "ok" }));
    mount(<BudgetsPage params={{ id: "life-agent" }} />, { fetch });

    fireEvent.change(await screen.findByDisplayValue("10"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: /write caps/i }));

    await waitFor(() => expect(fetch.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(true));
    const [, init] = fetch.mock.calls.find(([, call]) => call?.method === "PUT");
    expect(JSON.parse(init.body).manifest.budgets).toEqual({});
  });

  it("will not send a save that changes nothing", async () => {
    mount(<BudgetsPage params={{ id: "life-agent" }} />, { fetch: budgetFetch() });
    expect(await screen.findByRole("button", { name: /nothing changed/i })).toHaveProperty("disabled", true);
  });
});

/* -- residents ----------------------------------------------------------------------- */

describe("the resident editor", () => {
  it("offers both spellings and sends exactly one of them", async () => {
    const fetch = vi.fn().mockImplementation((url, init) =>
      init?.method === "PUT"
        ? Promise.resolve(json(200, { status: "accepted", commit: COMMIT, warnings: [], message: "written" }))
        : Promise.resolve(json(200, DECLARATION)),
    );
    mount(<ResidentsPage page="residentDeclaration" params={{ id: "life-agent" }} />, { fetch });

    fireEvent.click(await screen.findByRole("button", { name: /^yaml$/i }));
    fireEvent.change(screen.getByDisplayValue(/version: 0/), { target: { value: "version: 0\n# kept\n" } });
    fireEvent.click(screen.getByRole("button", { name: /write declaration/i }));

    await waitFor(() => expect(fetch.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(true));
    const sent = JSON.parse(fetch.mock.calls.find(([, call]) => call?.method === "PUT")[1].body);
    expect(sent.text).toBe("version: 0\n# kept\n");
    expect(sent.manifest).toBeUndefined();
    expect(sent.soul).toBe(DECLARATION.soul);
  });

  it("lists a manifest that did not validate rather than hiding it", async () => {
    const fetch = vi.fn().mockResolvedValue(
      json(200, { residents: [], errors: ["residents/broken/manifest.yaml: duplicate uid"] }),
    );
    mount(<ResidentsPage page="residents" params={{}} />, { fetch });
    expect(await screen.findByText(/duplicate uid/)).toBeTruthy();
    expect(screen.getByText(/manifest does not validate/i)).toBeTruthy();
  });
});

/* -- the whole shell ----------------------------------------------------------------- */

describe("the shell itself", () => {
  it("mounts the rail, the fleet, and nothing that needs a credential", async () => {
    const App = (await import("./App.jsx")).default;
    // Chronicle answers nothing useful; the fleet says so rather than breaking.
    vi.spyOn(window, "fetch").mockResolvedValue({ status: 503, json: async () => ({}) });
    window.history.replaceState({}, "", "/");

    render(<App />);

    const nav = screen.getByRole("navigation", { name: /sections/i });
    expect(within(nav).getAllByRole("link").map((link) => link.textContent)).toEqual([
      "01Fleet", "02Residents", "03Routines", "04Approvals", "05Board", "06Skills", "07Budgets",
    ]);
    expect(within(nav).getByRole("link", { name: /fleet/i }).getAttribute("aria-current")).toBe("page");
    // The read path needs no token: the fleet renders without one being asked for.
    expect(screen.queryByText(/Unlock the write path/i)).toBeNull();
    expect(screen.getByText(/Listening\./)).toBeTruthy();
  });

  it("says a stale deep link is unknown rather than showing the fleet", async () => {
    const App = (await import("./App.jsx")).default;
    vi.spyOn(window, "fetch").mockResolvedValue({ status: 503, json: async () => ({}) });
    window.history.replaceState({}, "", "/nope");

    render(<App />);
    expect(screen.getByText(/No such page/i)).toBeTruthy();
  });
});

describe("reloading steward's own copy", () => {
  it("is offered after a declaration write, and reports what steward answered", async () => {
    const fetch = vi.fn().mockImplementation((url, init) => {
      if (init?.method === "PUT") return Promise.resolve(json(200, { status: "accepted", commit: COMMIT, warnings: [], message: "written" }));
      if (init?.method === "POST") return Promise.resolve(json(200, { status: "reloaded", residents: 3, routines: 7, skills: ["research"] }));
      return Promise.resolve(json(200, DECLARATION));
    });
    mount(<ResidentsPage page="residentDeclaration" params={{ id: "life-agent" }} />, { fetch });

    fireEvent.click(await screen.findByRole("button", { name: /write declaration/i }));
    fireEvent.click(await screen.findByRole("button", { name: /reload steward's own copy/i }));

    expect(await screen.findByText(/reloaded: 3 residents, 7 routines, 1 skills/)).toBeTruthy();
    const [url, init] = fetch.mock.calls.find(([, call]) => call?.method === "POST");
    expect(url).toBe("/reload");
    expect(init.headers.Authorization).toBe("Bearer operator-token");
  });
});
