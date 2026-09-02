/* The shell, and the write surface hanging off it.
 *
 * These tests drive the real components against a stubbed steward and a stubbed Chronicle,
 * because the two things most worth protecting here cannot be seen by reading the code:
 * that every link survives the `/observatory/` mount, and that a write renders steward's
 * own answer rather than the click's intention.
 */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NavigationProvider } from "./navigation.jsx";
import { StewardProvider } from "./steward/context.jsx";
import { Gate } from "./console/Gate.jsx";
import SkillsPage from "./pages/SkillsPage.jsx";
import BudgetsPage from "./pages/BudgetsPage.jsx";
import ResidentsPage from "./pages/ResidentsPage.jsx";
import App from "./App.jsx";
import fixture from "./fixtures/complete-v1.js";

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

function residentHarness(fetch, initialId = "life-agent") {
  const storage = memoryStorage();
  storage.setItem("townhall.steward.operator", "operator-token");
  const tree = (id) => (
    <NavigationProvider base="/">
      <StewardProvider storage={storage} fetch={fetch}>
        {id ? <ResidentsPage page="residentDeclaration" params={{ id }} /> : <div>away</div>}
      </StewardProvider>
    </NavigationProvider>
  );
  const view = render(tree(initialId));
  return { ...view, show: (id) => view.rerender(tree(id)) };
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

beforeEach(() => {
  window.history.replaceState({}, "", "/");
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("does not create a Chronicle stream after the owning app unmounts", async () => {
  let finishPoll;
  const fetch = vi.fn(() => new Promise((resolve) => { finishPoll = resolve; }));
  const EventSource = vi.fn(class {
    addEventListener() {}
    close() {}
  });
  vi.stubGlobal("fetch", fetch);
  vi.stubGlobal("EventSource", EventSource);
  const mounted = render(<App />);
  mounted.unmount();

  finishPoll({ status: 200, json: async () => fixture });
  await new Promise((resolve) => setTimeout(resolve, 0));

  expect(EventSource).not.toHaveBeenCalled();
});

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
  it("round-trips explicit skill grants and notes while showing inherited defaults", async () => {
    const declaration = {
      ...DECLARATION,
      manifest: { ...DECLARATION.manifest, skills: [{ id: "triage", note: "weekday queue" }] },
      skill_library: [
        { name: "research", description: "Find facts.", default: true },
        { name: "triage", description: "Sort work.", default: false },
      ],
    };
    const fetch = vi.fn().mockImplementation((url, init) => {
      if (init?.method === "PUT") {
        return Promise.resolve(json(200, { status: "accepted", commit: COMMIT, warnings: [], message: "written" }));
      }
      return Promise.resolve(json(200, declaration));
    });
    mount(<ResidentsPage page="residentDeclaration" params={{ id: "life-agent" }} />, { fetch });

    expect(await screen.findByLabelText(/research/i)).toHaveProperty("disabled", true);
    fireEvent.change(screen.getByLabelText(/triage note/i), { target: { value: "evening queue" } });
    fireEvent.click(screen.getByRole("button", { name: /write declaration/i }));

    await waitFor(() => expect(fetch.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(true));
    const sent = JSON.parse(fetch.mock.calls.find(([, call]) => call?.method === "PUT")[1].body);
    expect(sent.manifest.skills).toEqual([{ id: "triage", note: "evening queue" }]);
  });

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

  it("keeps both rejected drafts when a stale revision is re-read", async () => {
    const concurrent = {
      ...DECLARATION,
      manifest: { ...DECLARATION.manifest, summary: "The other operator's edit." },
      text: "version: 0\nsummary: The other operator's edit.\n",
      soul: "---\nagent_id: life-agent\n---\nThe other operator's soul.\n",
      revision: "sha256:concurrent",
    };
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json(200, DECLARATION))
      .mockResolvedValueOnce(
        json(409, {
          detail: {
            error: "stale_revision",
            message: "somebody changed it first — re-read it and reapply your change",
          },
        }),
      )
      .mockResolvedValueOnce(json(200, concurrent));

    mount(<ResidentsPage page="residentDeclaration" params={{ id: "life-agent" }} />, { fetch });
    fireEvent.click(await screen.findByRole("button", { name: /^yaml$/i }));

    const rejectedManifest = "version: 0\nsummary: My complete manifest draft.\n";
    const rejectedSoul = "---\nagent_id: life-agent\n---\nMy complete soul draft.\n";
    const [manifestEditor, soulEditor] = screen.getAllByRole("textbox");
    fireEvent.change(manifestEditor, {
      target: { value: rejectedManifest },
    });
    fireEvent.change(soulEditor, {
      target: { value: rejectedSoul },
    });
    fireEvent.click(screen.getByRole("button", { name: /write declaration/i }));

    expect(await screen.findByText(/409 · stale_revision/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /discard rejected draft/i })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /re-read current server files/i }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3));

    expect(manifestEditor.value).toBe(rejectedManifest);
    expect(soulEditor.value).toBe(rejectedSoul);
    expect(screen.getByText(/The other operator's edit/)).toBeTruthy();
    expect(screen.getAllByText(/My complete manifest draft/).length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: /reapply rejected draft/i }));
    expect(manifestEditor.value).toBe(rejectedManifest);
    expect(soulEditor.value).toBe(rejectedSoul);
    expect(screen.getByText("sha256:concurrent")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /discard rejected draft/i }));
    expect(manifestEditor.value).toBe(concurrent.text);
    expect(soulEditor.value).toBe(concurrent.soul);
    expect(screen.queryByText(/Stale draft recovery/i)).toBeNull();
  });

  it("ignores a pending save result after resident navigation", async () => {
    const pending = deferred();
    const other = {
      ...DECLARATION,
      id: "hob",
      manifest: { ...DECLARATION.manifest, id: "hob" },
      text: "version: 0\nid: hob\n",
      revision: "sha256:hob",
    };
    const fetch = vi.fn().mockImplementation((url, init) => {
      if (init?.method === "PUT") return pending.promise;
      return Promise.resolve(json(200, String(url).includes("/hob/") ? other : DECLARATION));
    });
    const view = residentHarness(fetch);

    fireEvent.click(await screen.findByRole("button", { name: /^yaml$/i }));
    fireEvent.change(screen.getByDisplayValue(/version: 0/), {
      target: { value: "version: 0\nsummary: pending\n" },
    });
    fireEvent.click(screen.getByRole("button", { name: /write declaration/i }));
    await waitFor(() => expect(fetch.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(true));

    view.show("hob");
    fireEvent.click(await screen.findByRole("button", { name: /^yaml$/i }));
    expect(screen.getAllByRole("textbox").some((editor) => editor.value === other.text)).toBe(true);
    await act(async () => pending.resolve(json(409, {
      detail: { error: "stale_revision", message: "changed" },
    })));

    expect(screen.queryByText(/Stale draft recovery/i)).toBeNull();
    expect(screen.getByRole("button", { name: /write declaration/i }).textContent).toBe("Write declaration");
    expect(screen.getAllByRole("textbox").some((editor) => editor.value === other.text)).toBe(true);
  });

  it("does not offer discard until a refresh succeeds", async () => {
    const concurrent = { ...DECLARATION, revision: "sha256:current" };
    const fetch = vi.fn()
      .mockResolvedValueOnce(json(200, DECLARATION))
      .mockResolvedValueOnce(json(409, { detail: { error: "stale_revision", message: "changed" } }))
      .mockResolvedValueOnce(json(503, { detail: { error: "unavailable", message: "try again" } }))
      .mockResolvedValueOnce(json(200, concurrent));
    mount(<ResidentsPage page="residentDeclaration" params={{ id: "life-agent" }} />, { fetch });

    fireEvent.click(await screen.findByRole("button", { name: /write declaration/i }));
    expect(await screen.findByText(/Stale draft recovery/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /discard rejected draft/i })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /re-read current server files/i }));
    expect(await screen.findByText(/503 · unavailable/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /discard rejected draft/i })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /re-read current server files/i }));
    expect(await screen.findByRole("button", { name: /discard rejected draft/i })).toBeTruthy();
  });

  it("preserves edits made while comparing before reapply", async () => {
    const concurrent = { ...DECLARATION, revision: "sha256:current" };
    const fetch = vi.fn()
      .mockResolvedValueOnce(json(200, DECLARATION))
      .mockResolvedValueOnce(json(409, { detail: { error: "stale_revision", message: "changed" } }))
      .mockResolvedValueOnce(json(200, concurrent));
    mount(<ResidentsPage page="residentDeclaration" params={{ id: "life-agent" }} />, { fetch });
    fireEvent.click(await screen.findByRole("button", { name: /^yaml$/i }));
    fireEvent.click(screen.getByRole("button", { name: /write declaration/i }));
    fireEvent.click(await screen.findByRole("button", { name: /re-read current server files/i }));
    await screen.findByRole("button", { name: /reapply rejected draft/i });

    const [manifestEditor, soulEditor] = screen.getAllByRole("textbox");
    fireEvent.change(manifestEditor, { target: { value: "version: 0\nsummary: newer local edit\n" } });
    fireEvent.change(soulEditor, { target: { value: "---\n---\nnewer soul edit\n" } });
    fireEvent.click(screen.getByRole("button", { name: /reapply rejected draft/i }));

    expect(manifestEditor.value).toContain("newer local edit");
    expect(soulEditor.value).toContain("newer soul edit");
    expect(screen.getByText("sha256:current")).toBeTruthy();
  });

  it("one explicit read unlocks recovery after several earlier reads and a remount", async () => {
    const hob = {
      ...DECLARATION,
      id: "hob",
      manifest: { ...DECLARATION.manifest, id: "hob" },
      text: "version: 0\nid: hob\n",
      revision: "sha256:hob",
    };
    let lifeReads = 0;
    let writes = 0;
    const fetch = vi.fn().mockImplementation((url, init) => {
      if (init?.method === "PUT") {
        writes += 1;
        if (writes === 1) {
          return Promise.resolve(json(200, {
            status: "written",
            message: "declaration written",
            commit: COMMIT,
            paths: [],
            warnings: [],
          }));
        }
        return Promise.resolve(json(409, { detail: { error: "stale_revision", message: "changed" } }));
      }
      if (String(url).includes("/hob/")) return Promise.resolve(json(200, hob));
      lifeReads += 1;
      return Promise.resolve(json(200, { ...DECLARATION, revision: `sha256:life-${lifeReads}` }));
    });
    const view = residentHarness(fetch);
    fireEvent.click(await screen.findByRole("button", { name: /^yaml$/i }));

    // A successful write refreshes the query, so the conflict below happens after more
    // than one successful read in this hook lifetime.
    fireEvent.click(screen.getByRole("button", { name: /write declaration/i }));
    await waitFor(() => expect(lifeReads).toBe(2));

    const rejected = "version: 0\nsummary: retained across routes\n";
    fireEvent.change(screen.getByDisplayValue(/version: 0/), { target: { value: rejected } });
    fireEvent.click(screen.getByRole("button", { name: /write declaration/i }));
    expect(await screen.findByText(/Stale draft recovery/i)).toBeTruthy();

    view.show(null);
    expect(screen.getByText("away")).toBeTruthy();
    view.show("hob");
    fireEvent.click(await screen.findByRole("button", { name: /^yaml$/i }));
    expect(screen.getAllByRole("textbox").some((editor) => editor.value === hob.text)).toBe(true);
    expect(screen.queryByText(/Stale draft recovery/i)).toBeNull();
    view.show("life-agent");
    expect(await screen.findByText(/Stale draft recovery/i)).toBeTruthy();
    expect(screen.getAllByRole("textbox").some((editor) => editor.value === rejected)).toBe(true);
    await waitFor(() => expect(lifeReads).toBe(3));
    expect(screen.queryByRole("button", { name: /reapply rejected draft/i })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /re-read current server files/i }));
    expect(await screen.findByRole("button", { name: /reapply rejected draft/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /discard rejected draft/i })).toBeTruthy();
    expect(lifeReads).toBe(4);
  });

  it("shows retained recovery when the return read fails, then unlocks after retry", async () => {
    const rejectedManifest = "version: 0\nsummary: visible through failure\n";
    const rejectedSoul = "---\nagent_id: life-agent\n---\nA soul that must remain visible.\n";
    let reads = 0;
    const fetch = vi.fn().mockImplementation((_url, init) => {
      if (init?.method === "PUT") {
        return Promise.resolve(json(409, { detail: { error: "stale_revision", message: "changed" } }));
      }
      reads += 1;
      if (reads === 2) {
        return Promise.resolve(json(503, { detail: { error: "unavailable", message: "try again" } }));
      }
      return Promise.resolve(json(200, { ...DECLARATION, revision: `sha256:read-${reads}` }));
    });
    const view = residentHarness(fetch);
    fireEvent.click(await screen.findByRole("button", { name: /^yaml$/i }));
    const [manifestEditor, soulEditor] = screen.getAllByRole("textbox");
    fireEvent.change(manifestEditor, { target: { value: rejectedManifest } });
    fireEvent.change(soulEditor, { target: { value: rejectedSoul } });
    fireEvent.click(screen.getByRole("button", { name: /write declaration/i }));
    expect(await screen.findByText(/Stale draft recovery/i)).toBeTruthy();

    view.show(null);
    view.show("life-agent");
    expect(await screen.findByText(/503 · unavailable/i)).toBeTruthy();
    expect(screen.getAllByText(/visible through failure/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/A soul that must remain visible/).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: /copy rejected manifest/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /copy rejected soul/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /discard rejected draft/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /reapply rejected draft/i })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /re-read current server files/i }));
    expect(await screen.findByRole("button", { name: /reapply rejected draft/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /discard rejected draft/i })).toBeTruthy();
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

describe("resident lifecycle", () => {
  const resident = {
    id: "life-agent", agent_id: "claude-code:life-agent", project: "warren", retired: false,
    soul: { name: "Hob", role: "operator", accent: "#4f7ea6", char: "Hob" },
    summary: "Keeps watch.", uid: "7e36d76a-1ad8-4d65-a619-8c6e7fb93ed9", path: "residents/life-agent/manifest.yaml",
    memory: { kind: "directory", path: "/memory" }, runner: { kind: "claude", model: null },
    charter: { mission: "Watch.", duties: ["Watch"], rules: ["Ask"], escalation: "Ask." },
    skills: [], effective_skills: [], routes: [],
  };
  const reads = (url) => {
    if (url.endsWith("/budget")) return { resident: "life-agent", window: { tz: "UTC", day: "2026-09-02", end: "2026-09-03T00:00:00Z" }, spent: {}, budgets: [], paused: false };
    if (url.endsWith("/journal")) return { entries: [] };
    if (url.endsWith("/inbox")) return { inbox: [], routes: [], pending: 0 };
    if (url === "/routines") return { routines: [], scheduler: {} };
    return resident;
  };

  it("requires a successful retirement rehearsal before execute and reports cleanup", async () => {
    let becameRetired = false;
    const fetch = vi.fn().mockImplementation((url, init) => {
      if (init?.method === "POST") {
        const body = JSON.parse(init.body);
        if (!body.dry_run) becameRetired = true;
        return Promise.resolve(json(200, body.dry_run
          ? { dry_run: true, revision: "sha256:plan", commands: ["docker compose down --remove-orphans", "rm -f .env docker-compose.yaml"] }
          : { dry_run: false, stopped: true, scrubbed: true, commands: [] }));
      }
      if (url === "/residents/life-agent") return Promise.resolve(json(200, { ...resident, retired: becameRetired }));
      return Promise.resolve(json(200, reads(url)));
    });
    mount(<ResidentsPage page="resident" params={{ id: "life-agent" }} />, { fetch });

    const execute = await screen.findByRole("button", { name: /retire exactly this plan/i });
    expect(screen.getByRole("button", { name: /provision from declaration/i })).toBeTruthy();
    expect(execute).toHaveProperty("disabled", true);
    fireEvent.click(screen.getByRole("button", { name: /rehearse retirement/i }));
    await waitFor(() => expect(execute.disabled).toBe(false));
    fireEvent.click(execute);
    await waitFor(() => expect(fetch.mock.calls.some(([, init]) => init?.body?.includes("sha256:plan"))).toBe(true));
    const receipt = (await screen.findByText(/^resident retired$/i)).parentElement.parentElement;
    expect(receipt.textContent).toMatch(/container stopped.*\.env.*compose removal completed.*credential directory retained/i);
    expect(await screen.findByRole("link", { name: /begin return in declaration/i })).toBeTruthy();
  });

  it("reports provision as provision, never as retirement", async () => {
    const fetch = vi.fn().mockImplementation((url, init) => Promise.resolve(json(200,
      init?.method === "POST" ? { message: "container and schedule converged" } : reads(url),
    )));
    mount(<ResidentsPage page="resident" params={{ id: "life-agent" }} />, { fetch });
    fireEvent.click(await screen.findByRole("button", { name: /provision from declaration/i }));
    expect(await screen.findByText(/container and schedule converged/i)).toBeTruthy();
    expect(screen.queryByText(/^resident retired$/i)).toBeNull();
  });

  it("offers the provision path instead for a retired resident", async () => {
    const fetch = vi.fn().mockImplementation((url) => Promise.resolve(json(200, url === "/residents/life-agent" ? { ...resident, retired: true } : reads(url))));
    mount(<ResidentsPage page="resident" params={{ id: "life-agent" }} />, { fetch });
    expect(await screen.findByRole("link", { name: /begin return in declaration/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /provision from declaration/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /rehearse retirement/i })).toBeNull();
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
      "08Diagnostics",
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
