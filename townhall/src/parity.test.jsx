/* The console's own behaviour, on this stack.
 *
 * warren#225 moved the steward console into townhall as a *port*, so the tests worth having
 * are the ones that say the ported thing still behaves the way the console did — and, for
 * the five findings its audit turned up, that it behaves the way the console *should* have.
 * Each of those has a test here that fails if the bug is reintroduced.
 */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NavigationProvider } from "./navigation.jsx";
import { StewardProvider } from "./steward/context.jsx";
import { LedgerProvider } from "./console/ledger.jsx";
import ResidentsPage from "./pages/ResidentsPage.jsx";
import RoutinesPage from "./pages/RoutinesPage.jsx";
import ApprovalsPage from "./pages/ApprovalsPage.jsx";
import BoardPage from "./pages/BoardPage.jsx";

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

/** Mount a page with a scripted steward behind it and the pending ledger around it. */
function mount(ui, { path = "/", base = "/", fetch, token = "steward-operator-abc" } = {}) {
  window.history.replaceState({}, "", path);
  const storage = memoryStorage();
  if (token !== null) storage.setItem("townhall.steward.operator", token);
  return render(
    <NavigationProvider base={base}>
      <StewardProvider storage={storage} fetch={fetch}>
        <LedgerProvider>{ui}</LedgerProvider>
      </StewardProvider>
    </NavigationProvider>,
  );
}

/** Route a stubbed fetch by path, so a page making five reads is one line per read. */
function router(table) {
  return vi.fn().mockImplementation((url, init = {}) => {
    const path = String(url).split("?")[0];
    const key = `${init.method || "GET"} ${path}`;
    const handler = table[key] ?? table[path];
    if (!handler) return Promise.resolve(json(404, { detail: { error: "no stub", message: key } }));
    const answer = typeof handler === "function" ? handler(url, init) : handler;
    return Promise.resolve(answer);
  });
}

const SOUL = { name: "Hob", char: "Keeper", role: "household agent", accent: "#a68a4f" };

const RESIDENT = {
  id: "life-agent",
  uid: "0198-uid",
  agent_id: "claude-code:life-agent",
  project: "household",
  summary: "keeps the house",
  retired: false,
  path: "residents/life-agent/manifest.yaml",
  soul: SOUL,
  voice: "Plain sentences.",
  memory: { kind: "directory", path: "memory/" },
  runner: { kind: "claude", model: "claude-opus-5" },
  charter: {
    mission: "Keep the household running.",
    duties: ["Read the inbox"],
    rules: ["Never send mail unasked"],
    escalation: { when: ["Anything irreversible"], how: "needs_human", note: "ask first" },
  },
  skills: [{ id: "read-inbox", note: "granted here" }],
  effective_skills: ["read-inbox", "write-journal"],
};

const ROUTINE = {
  key: "life-agent/daily-summary",
  resident: "life-agent",
  resident_name: "Hob",
  accent: "#a68a4f",
  routine: "daily-summary",
  schedule: "0 9 * * *",
  schedule_tz: "Europe/Ljubljana",
  enabled: true,
  retired: false,
  timeout_s: 600,
  journal: null,
  anchor: "2026-08-30T07:00:00+00:00",
  next_fire: "2026-09-01T09:00:00+02:00",
  last_request: null,
  last_run: {
    run_id: "run-7",
    trigger: "schedule",
    outcome: "ok",
    recorded_at: "2026-08-31T07:00:41.220Z",
    duration_s: 41.2,
  },
};

const ROUTINES = {
  routines: [ROUTINE],
  state_path: "/srv/steward/.steward/state/scheduler.json",
  scheduler: { last_tick: "2026-08-31T09:31:02+00:00", stale_after_s: 360, alive: true },
  errors: [],
};

const BUDGET = {
  resident: "life-agent",
  window: { tz: "Europe/Ljubljana", day: "2026-08-31", end: "2026-08-31T22:00:00.000Z" },
  spent: { runs: 6, tokens: 20400, cost_usd: 5.2, duration_s: 812.4, unreported_runs: 0 },
  budgets: [{ budget: "daily_cost_usd", spent: 5.2, limit: 10, remaining: 4.8, exhausted: false }],
  max_run_seconds: 900,
  paused: false,
};

const JOURNAL = {
  entries: [{ date: "2026-08-30", routine: "close-of-day", text: "Read the post. Nothing urgent." }],
};

const INBOX = {
  inbox: [
    {
      task_id: "t-1",
      status: "open",
      title: "Look at the boiler quote",
      detail: "Two of them, both plausible.",
      delegated_by: "Miha",
      route: "letters",
      depth: 1,
    },
  ],
  routes: [{ id: "letters", status: "active", accepts: true }],
  pending: 1,
};

const residentStubs = {
  "/residents/0198-uid": json(200, RESIDENT),
  "/routines": json(200, ROUTINES),
  "/residents/0198-uid/budget": json(200, BUDGET),
  "/residents/0198-uid/journal": json(200, JOURNAL),
  "/residents/0198-uid/inbox": json(200, INBOX),
};

beforeEach(() => window.history.replaceState({}, "", "/"));
afterEach(() => {
  cleanup();
  // A leaked fake timer makes every later `findBy*` hang on a clock nobody advances.
  vi.useRealTimers();
});

/* -- the resident record -------------------------------------------------------------- */

describe("a resident's record", () => {
  it("shows the matching resident's Chronicle events at the bottom, newest first", async () => {
    const model = {
      people: [
        {
          id: "resident:0198-uid",
          residency: "resident",
          history: [
            {
              ts: "2026-09-03T10:00:00Z",
              type: "routine_started",
              payload: { task: "Morning round" },
            },
            { ts: "2026-09-03T10:05:00Z", type: "idle", payload: { status: "Waiting" } },
          ],
        },
        {
          id: "resident:somebody-else",
          residency: "resident",
          history: [
            {
              ts: "2026-09-03T10:06:00Z",
              type: "tool_called",
              payload: { tool: "SecretOtherTool" },
            },
          ],
        },
      ],
    };
    mount(<ResidentsPage page="resident" params={{ id: "0198-uid" }} model={model} />, {
      fetch: router(residentStubs),
    });

    const feed = await screen.findByLabelText("Hob event feed");
    expect(feed.textContent).toContain("Waiting");
    expect(feed.textContent).toContain("Morning round");
    expect(feed.textContent.indexOf("Waiting")).toBeLessThan(
      feed.textContent.indexOf("Morning round"),
    );
    expect(feed.textContent).not.toContain("SecretOtherTool");
  });

  it("shows the journal, the inbox and the budget steward holds", async () => {
    // None of these three is in Chronicle's projection: it carries journal *metadata* but
    // no text, no inbox, and no spend. So this page is steward's or it is nothing.
    mount(<ResidentsPage page="resident" params={{ id: "0198-uid" }} />, {
      fetch: router(residentStubs),
    });

    expect(await screen.findByText(/Read the post\. Nothing urgent\./)).toBeTruthy();
    expect(screen.getByText(/Look at the boiler quote/)).toBeTruthy();
    expect(screen.getByText(/Open routes: letters/)).toBeTruthy();
    expect(screen.getByText("$5.2000")).toBeTruthy();
    // Named twice on this page — the routine's schedule zone and the budget window's.
    expect(screen.getAllByText(/Europe\/Ljubljana/).length).toBeGreaterThan(0);
  });

  it("renders a panel steward refused without losing the panels it answered", async () => {
    mount(<ResidentsPage page="resident" params={{ id: "0198-uid" }} />, {
      fetch: router({
        ...residentStubs,
        "/residents/0198-uid/journal": json(404, {
          detail: { error: "no_journal", message: "this resident writes no journal" },
        }),
      }),
    });

    expect((await screen.findAllByText(/this resident writes no journal/)).length).toBeGreaterThan(0);
    // The charter is still there: one unreadable panel is not a broken page.
    expect(screen.getByText(/Keep the household running/)).toBeTruthy();
  });

  it("says an empty journal means a day nobody closed, rather than showing nothing", async () => {
    mount(<ResidentsPage page="resident" params={{ id: "0198-uid" }} />, {
      fetch: router({ ...residentStubs, "/residents/0198-uid/journal": json(200, { entries: [] }) }),
    });

    expect(await screen.findByText(/Nothing written yet/)).toBeTruthy();
    expect(screen.getByText(/a day that was never closed/)).toBeTruthy();
  });

  it("names a closed delegation route as closed rather than omitting it", async () => {
    mount(<ResidentsPage page="resident" params={{ id: "0198-uid" }} />, {
      fetch: router({
        ...residentStubs,
        "/residents/0198-uid/inbox": json(200, {
          inbox: [],
          routes: [{ id: "letters", status: "disabled", accepts: false }],
          pending: 2,
        }),
      }),
    });

    // Letters delivered before somebody shut the door stay open and nothing picks them up;
    // a panel listing only accepting routes would leave the pile unexplained.
    expect(await screen.findByText(/Every declared route is closed — letters \(disabled\)/)).toBeTruthy();
    expect(screen.getByText(/2 waiting behind it/)).toBeTruthy();
  });

  it("puts the resident's declared accent on the element whose CSS consumes it (#151)", async () => {
    // The console passed this through `Object.assign(node.style, …)`, which cannot set a
    // custom property, so every accent silently fell back to the generic ember. A React
    // style object can, and this asserts it actually did.
    const { container } = mount(<ResidentsPage page="resident" params={{ id: "0198-uid" }} />, {
      fetch: router(residentStubs),
    });

    await screen.findAllByText("Hob");
    const head = container.querySelector('[style*="--accent"]');
    expect(head).toBeTruthy();
    expect(head.style.getPropertyValue("--accent")).toBe("#a68a4f");
  });
});

/* -- run now, and the confirm that follows it ----------------------------------------- */

describe("running a routine now", () => {
  const accepted = json(202, {
    request_id: "req-1",
    status: "accepted",
    message: "queued one run",
  });

  it("says accepted until steward's own log says it ran", async () => {
    vi.useFakeTimers();
    const log = vi.fn().mockResolvedValue(json(200, { request_id: "req-1", outcome: "queued" }));
    mount(<RoutinesPage />, {
      fetch: router({
        "/routines": json(200, ROUTINES),
        "POST /residents/life-agent/routines/daily-summary/run": accepted,
        "/requests/req-1": (url, init) => log(url, init),
      }),
    });

    await vi.waitFor(() => expect(screen.getByRole("button", { name: /run now/i })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /run now/i }));
    await vi.waitFor(() => expect(screen.getByText("accepted")).toBeTruthy());

    // A 202 is steward accepting a request, not a run happening. While its log says
    // "queued", the word "confirmed" must not be anywhere on the screen.
    await vi.advanceTimersByTimeAsync(3000);
    expect(log).toHaveBeenCalled();
    expect(screen.queryByText("confirmed")).toBeNull();
    expect(screen.getByText(/request req-1/)).toBeTruthy();
    vi.useRealTimers();
  });

  it("confirms only after reading the request log back", async () => {
    vi.useFakeTimers();
    let outcome = "queued";
    mount(<RoutinesPage />, {
      fetch: router({
        "/routines": json(200, ROUTINES),
        "POST /residents/life-agent/routines/daily-summary/run": accepted,
        "/requests/req-1": () =>
          json(200, { request_id: "req-1", outcome, detail: { run_id: "run-9" } }),
      }),
    });

    await vi.waitFor(() => expect(screen.getByRole("button", { name: /run now/i })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /run now/i }));
    await vi.waitFor(() => expect(screen.getByText("accepted")).toBeTruthy());

    outcome = "ran";
    await vi.waitFor(() => expect(screen.getByText("confirmed")).toBeTruthy());

    expect(screen.getByText(/steward's log: ran \(run run-9\)/)).toBeTruthy();
    vi.useRealTimers();
  });

  it("stops polling the moment a ticket is dismissed (#153)", async () => {
    vi.useFakeTimers();
    const log = vi.fn().mockResolvedValue(json(200, { request_id: "req-1", outcome: "queued" }));
    mount(<RoutinesPage />, {
      fetch: router({
        "/routines": json(200, ROUTINES),
        "POST /residents/life-agent/routines/daily-summary/run": accepted,
        "/requests/req-1": (url, init) => log(url, init),
      }),
    });

    await vi.waitFor(() => expect(screen.getByRole("button", { name: /run now/i })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /run now/i }));
    await vi.waitFor(() => expect(screen.getByText("accepted")).toBeTruthy());
    await vi.advanceTimersByTimeAsync(3000);
    const before = log.mock.calls.length;
    expect(before).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: /dismiss/i }));
    await vi.advanceTimersByTimeAsync(30_000);

    // The console kept the closure alive for three minutes after the node was detached,
    // polling the two most expensive endpoints and then forcing a full re-render.
    expect(log.mock.calls.length).toBe(before);
    expect(screen.queryByText("accepted")).toBeNull();
    vi.useRealTimers();
  });

  it("greys out run-now for a retired resident and says what steward would answer", async () => {
    mount(<RoutinesPage />, {
      fetch: router({
        "/routines": json(200, {
          ...ROUTINES,
          routines: [{ ...ROUTINE, retired: true, next_fire: null }],
        }),
      }),
    });

    const button = await screen.findByRole("button", { name: /run now/i });
    expect(button.disabled).toBe(true);
    expect(button.getAttribute("title")).toMatch(/409 resident_retired/);
  });

  it("greys out run-now for a routine the manifest disables", async () => {
    mount(<RoutinesPage />, {
      fetch: router({
        "/routines": json(200, { ...ROUTINES, routines: [{ ...ROUTINE, enabled: false }] }),
      }),
    });

    const button = await screen.findByRole("button", { name: /run now/i });
    expect(button.disabled).toBe(true);
    expect(button.getAttribute("title")).toMatch(/disabled in the manifest/);
  });
});

/* -- the routine ledger --------------------------------------------------------------- */

describe("what a routine actually did (#104)", () => {
  it("shows the last run with its trigger, not only the request log", async () => {
    mount(<RoutinesPage />, { fetch: router({ "/routines": json(200, ROUTINES) }) });

    // The bug: last_request is the API request log, so a routine firing on its own
    // schedule left it null and the panel read as "never runs unless I press this".
    expect(await screen.findByText("ok")).toBeTruthy();
    expect(screen.getByText(/schedule ·/)).toBeTruthy();
    expect(screen.getByText(/none through this API/)).toBeTruthy();
  });

  it("calls a routine that has never finished never run, not failed", async () => {
    mount(<RoutinesPage />, {
      fetch: router({
        "/routines": json(200, { ...ROUTINES, routines: [{ ...ROUTINE, last_run: null }] }),
      }),
    });

    expect(await screen.findByText("never run")).toBeTruthy();
  });

  it("says out loud when nothing has ever ticked the scheduler", async () => {
    mount(<RoutinesPage />, {
      fetch: router({
        "/routines": json(200, {
          ...ROUTINES,
          scheduler: { last_tick: null, stale_after_s: 360, alive: null },
        }),
      }),
    });

    // `alive: null` is a third answer, and a page full of next-fire promises has to say
    // whether anybody is keeping them.
    expect(await screen.findByText(/scheduler has never ticked/)).toBeTruthy();
    expect(screen.getByText(/a promise with nobody to keep it/)).toBeTruthy();
  });
});

/* -- approvals ------------------------------------------------------------------------ */

const PENDING = {
  request_id: "ap-1",
  action: "send_email",
  message: "Hob wants to email the plumber",
  resident: "life-agent",
  detail: { to: "plumber@example.com" },
  options: ["approve", "deny", "edit"],
  created_at: "2026-08-31T09:00:00.000Z",
  expires_at: "2099-01-01T00:00:00.000Z",
};

const approvalStubs = (pending = [PENDING], resolved = []) => ({
  "/approvals": (url) =>
    json(200, {
      approvals: String(url).includes("status=pending") ? pending : resolved,
    }),
});

describe("deciding an approval", () => {
  it("sends only the first decision while steward is still answering (#294)", async () => {
    let answer;
    const delayed = new Promise((resolve) => { answer = resolve; });
    const fetch = router({
      ...approvalStubs(),
      "POST /approvals/ap-1": () => delayed,
    });
    mount(<ApprovalsPage />, { fetch });

    const approve = await screen.findByRole("button", { name: /approve/i });
    const deny = screen.getByRole("button", { name: /^deny$/i });
    const edit = screen.getByRole("button", { name: /edit/i });
    fireEvent.click(approve);
    fireEvent.click(deny);
    fireEvent.click(edit);

    expect(approve.disabled).toBe(true);
    expect(deny.disabled).toBe(true);
    expect(edit.disabled).toBe(true);
    expect(fetch.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);

    answer(json(202, {
      request_id: "req-294",
      status: "recorded_announcement_pending",
      message: "recorded; announcement pending",
    }));
  });

  it("offers only the decisions the request itself offered", async () => {
    mount(<ApprovalsPage />, {
      fetch: router(approvalStubs([{ ...PENDING, options: ["approve"] }])),
    });

    expect(await screen.findByRole("button", { name: /approve/i })).toBeTruthy();
    // steward answers 409 approval_decision_not_offered to anything else, so drawing the
    // button would be drawing a refusal.
    expect(screen.queryByRole("button", { name: /^deny$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /edit/i })).toBeNull();
  });

  it("offers no decision on a request whose deadline has passed (#154)", async () => {
    // The console asked for status=all and rebuilt "pending" client-side, dropping the
    // expiry filter steward applies precisely so a human is never offered a click that
    // answers 409 approval_expired.
    mount(<ApprovalsPage />, {
      fetch: router(approvalStubs([{ ...PENDING, expires_at: "2020-01-01T00:00:00.000Z" }])),
    });

    expect(await screen.findByText(/passed its deadline while the page was open/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /approve/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^deny$/i })).toBeNull();
  });

  it("asks steward for pending rather than filtering `all` itself (#154)", async () => {
    const fetch = router(approvalStubs());
    mount(<ApprovalsPage />, { fetch });

    await screen.findByText(/Hob wants to email the plumber/);
    const asked = fetch.mock.calls.map(([url]) => String(url));
    expect(asked.some((url) => url.includes("status=pending"))).toBe(true);
    expect(asked.some((url) => url.includes("status=resolved"))).toBe(true);
    expect(asked.some((url) => url.includes("status=all"))).toBe(false);
  });

  it("says accepted until steward's log records the decision", async () => {
    vi.useFakeTimers();
    let outcome = "recorded_announcement_pending";
    mount(<ApprovalsPage />, {
      fetch: router({
        ...approvalStubs(),
        "POST /approvals/ap-1": json(202, {
          request_id: "req-9",
          status: "recorded_announcement_pending",
          message: "recorded; announcement pending",
        }),
        "/requests/req-9": () => json(200, { request_id: "req-9", outcome }),
      }),
    });

    await vi.waitFor(() => expect(screen.getByRole("button", { name: /approve/i })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /approve/i }));
    await vi.waitFor(() => expect(screen.getByText("accepted")).toBeTruthy());
    expect(screen.getByRole("button", { name: /approve/i }).disabled).toBe(true);
    expect(screen.getByRole("button", { name: /^deny$/i }).disabled).toBe(true);
    await vi.advanceTimersByTimeAsync(3000);
    expect(screen.queryByText("confirmed")).toBeNull();

    outcome = "recorded";
    await vi.advanceTimersByTimeAsync(3000);
    await vi.waitFor(() => expect(screen.getByText("confirmed")).toBeTruthy());
    vi.useRealTimers();
  });

  it("names the operator who decided, in the audit row", async () => {
    mount(<ApprovalsPage />, {
      fetch: router(
        approvalStubs(
          [],
          [{ ...PENDING, decision: "approve", decided_by: "Miha", decided_at: "2026-08-31T10:00:00.000Z" }],
        ),
      ),
    });

    // Before warren#225 every row here said "api", because the master token names nobody.
    expect(await screen.findByText("Miha")).toBeTruthy();
  });

  it.each([
    [404, { detail: { error: "unknown_approval", message: "no such approval" } }],
    [422, { detail: { error: "invalid_body", message: "choose a decision" } }],
    [409, { detail: { error: "approval_expired", message: "it expired and denies by default" } }],
  ])("reopens after a trusted definite %s refusal", async (status, body) => {
    mount(<ApprovalsPage />, {
      fetch: router({
        ...approvalStubs(),
        "POST /approvals/ap-1": json(status, body),
      }),
    });

    const approve = await screen.findByRole("button", { name: /approve/i });
    fireEvent.click(approve);
    expect(await screen.findByText(new RegExp(`${status} ·`))).toBeTruthy();
    expect(approve.disabled).toBe(false);
    expect(screen.getByRole("button", { name: /^deny$/i }).disabled).toBe(false);
  });

  it("returns to the credential retry path after a definite 401 refusal", async () => {
    mount(<ApprovalsPage />, {
      fetch: router({
        ...approvalStubs(),
        "POST /approvals/ap-1": json(401, {
          detail: { error: "unauthorized", message: "use another credential" },
        }),
      }),
    });

    fireEvent.click(await screen.findByRole("button", { name: /approve/i }));
    expect(await screen.findByRole("heading", { name: /unlock the write path/i })).toBeTruthy();
  });

  it.each([
    [408, { detail: { error: "request_timeout", message: "the proxy timed out" } }],
    [409, { detail: { error: "approval_expired", message: "generated", proxy: true } }],
    [409, { detail: { error: "approval_decision_not_offered", message: "not offered" } }],
  ])("stays locked after an ambiguous %s response", async (status, body) => {
    const fetch = router({
      ...approvalStubs(),
      "POST /approvals/ap-1": json(status, body),
    });
    mount(<ApprovalsPage />, { fetch });

    const approve = await screen.findByRole("button", { name: /approve/i });
    const deny = screen.getByRole("button", { name: /^deny$/i });
    fireEvent.click(approve);
    expect(await screen.findByText(new RegExp(`${status} ·`))).toBeTruthy();
    expect(approve.disabled).toBe(true);
    expect(deny.disabled).toBe(true);
    fireEvent.click(deny);
    expect(fetch.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  });
});

/* -- the board ------------------------------------------------------------------------ */

describe("posting a job", () => {
  const boardStubs = (jobs = []) => ({
    "/jobs": json(200, { jobs }),
    "/skills": json(200, { skills: [{ name: "research", description: "Look it up." }], errors: [] }),
    "/residents": json(200, { residents: [], errors: [] }),
  });

  it("refuses to send a task with no title, and says nothing was sent", async () => {
    const fetch = router(boardStubs());
    mount(<BoardPage />, { fetch });

    fireEvent.click(await screen.findByRole("button", { name: /post to the board/i }));

    expect(screen.getByText(/A task needs a title\. Nothing was sent\./)).toBeTruthy();
    expect(fetch.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });

  it("confirms a post by finding the task on steward's own board", async () => {
    vi.useFakeTimers();
    let jobs = [];
    mount(<BoardPage />, {
      fetch: router({
        "/jobs": (url, init) =>
          init?.method === "POST"
            ? json(202, { request_id: "req-3", task_id: "task-5", message: "queued on the board" })
            : json(200, { jobs }),
        "/skills": json(200, { skills: [], errors: [] }),
        "/residents": json(200, { residents: [], errors: [] }),
      }),
    });

    await vi.waitFor(() => expect(screen.getByPlaceholderText("Research X")).toBeTruthy());
    fireEvent.change(screen.getByPlaceholderText("Research X"), { target: { value: "Fix it" } });
    fireEvent.click(screen.getByRole("button", { name: /post to the board/i }));
    await vi.waitFor(() => expect(screen.getByText("accepted")).toBeTruthy());

    await vi.advanceTimersByTimeAsync(3000);
    expect(screen.queryByText("confirmed")).toBeNull();

    jobs = [{ task_id: "task-5", title: "Fix it", status: "open", posted_by: "Miha" }];
    await vi.advanceTimersByTimeAsync(3000);
    await vi.waitFor(() => expect(screen.getByText("confirmed")).toBeTruthy());

    // And the truth about dispatch, which is the thing operators get wrong.
    expect(screen.getByText(/No resident has been prompted/)).toBeTruthy();
    vi.useRealTimers();
  });

  it("sends the required skills a claimant must hold", async () => {
    const fetch = router(boardStubs());
    mount(<BoardPage />, { fetch });

    fireEvent.change(await screen.findByPlaceholderText("Research X"), {
      target: { value: "Look into it" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: /research/i }));
    fireEvent.click(screen.getByRole("button", { name: /post to the board/i }));

    await waitFor(() => expect(fetch.mock.calls.some(([, init]) => init?.method === "POST")).toBe(true));
    const [, init] = fetch.mock.calls.find(([, call]) => call?.method === "POST");
    expect(JSON.parse(init.body)).toMatchObject({
      title: "Look into it",
      required_skills: ["research"],
    });
  });
});

/* -- declaring a resident ------------------------------------------------------------- */

describe("declaring a resident", () => {
  const library = json(200, {
    skills: [
      { name: "write-journal", description: "Close the day.", default: true },
      { name: "research", description: "Look it up.", default: false },
    ],
    errors: [],
  });

  const fill = () => {
    const type = (placeholder, value) =>
      fireEvent.change(screen.getByPlaceholderText(placeholder), { target: { value } });
    type("note-keeper", "quill");
    type("Quill", "Quill");
    type("Scribe", "Scribe");
    type("note bot", "note bot");
    type("Raise needs_human before anything irreversible.", "Ask before anything irreversible.");
    const areas = screen.getAllByRole("textbox").filter((box) => box.tagName === "TEXTAREA");
    fireEvent.change(areas[0], { target: { value: "Keep the notes." } });
    fireEvent.change(areas[1], { target: { value: "Write things down" } });
    fireEvent.change(areas[2], { target: { value: "Never delete a note" } });
  };

  it("refuses locally what steward's validator would refuse, and sends nothing", async () => {
    const fetch = router({ "/skills": library });
    mount(<ResidentsPage page="residentNew" params={{}} />, { fetch });

    await screen.findByPlaceholderText("note-keeper");
    fireEvent.click(screen.getByRole("button", { name: /declare resident/i }));

    expect(screen.getByText(/nothing has been sent/i)).toBeTruthy();
    expect(screen.getAllByText(/Required: the village draws this/).length).toBeGreaterThan(0);
    expect(fetch.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });

  it("shows the commit steward made, the way the editors do", async () => {
    // #235 made this endpoint commit; the receipt has to say so or the operator is left
    // wondering whether they still have to.
    const fetch = router({
      "/skills": library,
      "POST /residents": json(201, {
        request_id: "req-4",
        status: "accepted",
        id: "quill",
        manifest_path: "residents/quill/manifest.yaml",
        soul_path: "residents/quill/soul.md",
        message: "declared",
        declare: { written: true, note: "two files written" },
        commit: {
          committed: true,
          sha: "9f1c0a77bb31e4d5",
          message: "feat(residents): declare quill via the API",
          note: "committed as 9f1c0a7",
        },
      }),
      "/residents": json(200, { residents: [], errors: [] }),
    });
    mount(<ResidentsPage page="residentNew" params={{}} />, { fetch });

    await screen.findByPlaceholderText("note-keeper");
    fill();
    fireEvent.click(screen.getByRole("button", { name: /declare resident/i }));

    expect(await screen.findByText("9f1c0a77bb")).toBeTruthy();
    expect(screen.getByText(/feat\(residents\): declare quill via the API/)).toBeTruthy();
  });

  it("sends deploy as a boolean and never grants a library default", async () => {
    const fetch = router({
      "/skills": library,
      "POST /residents": json(201, { request_id: "r", id: "quill", message: "declared", commit: {} }),
      "/residents": json(200, { residents: [], errors: [] }),
    });
    mount(<ResidentsPage page="residentNew" params={{}} />, { fetch });

    await screen.findByPlaceholderText("note-keeper");
    fill();
    fireEvent.click(screen.getByRole("checkbox", { name: /research/i }));
    fireEvent.click(screen.getByRole("button", { name: /declare resident/i }));

    await waitFor(() => expect(fetch.mock.calls.some(([, init]) => init?.method === "POST")).toBe(true));
    const [, init] = fetch.mock.calls.find(([, call]) => call?.method === "POST");
    const sent = JSON.parse(init.body);
    // `deploy: false` is a thing this form means to say. Asking a machine to start a
    // container is never a field left out.
    expect(sent.deploy).toBe(false);
    // A default is held without a grant, so granting it would be noise in the manifest.
    expect(sent.skills).toEqual(["research"]);
    expect(sent.charter.duties).toEqual(["Write things down"]);
  });

  it("puts steward's own refusal on the field it names", async () => {
    const fetch = router({
      "/skills": library,
      "POST /residents": json(409, {
        detail: {
          error: "resident_exists",
          message: "residents/quill already exists",
          diagnostics: [
            { field: "id", problem: "a resident with this id is already declared", severity: "error" },
          ],
        },
      }),
    });
    mount(<ResidentsPage page="residentNew" params={{}} />, { fetch });

    await screen.findByPlaceholderText("note-keeper");
    fill();
    fireEvent.click(screen.getByRole("button", { name: /declare resident/i }));

    expect(await screen.findByText(/409 · resident_exists/)).toBeTruthy();
    expect(screen.getAllByText(/already declared/).length).toBeGreaterThan(0);
  });
});

/* -- the residents list --------------------------------------------------------------- */

describe("the residents list", () => {
  const listing = {
    residents: [
      {
        ...RESIDENT,
        skills: [{ id: "read-inbox" }],
        budget: { declared: true, paused: false, summary: "5.20 of 10", budgets: [], runs: 6 },
        board: { claim: true },
        delegation: { send: false },
      },
    ],
    errors: [],
  };

  it("addresses a resident by its uid, which outlives its name", async () => {
    mount(<ResidentsPage page="residents" params={{}} />, {
      fetch: router({ "/residents": json(200, listing), "/routines": json(200, ROUTINES) }),
    });

    const link = await screen.findByRole("link", { name: /Hob/ });
    // Not /residents/life-agent: an id is a directory name and can be reused.
    expect(link.getAttribute("href")).toBe("/residents/0198-uid");
  });

  it("names the soonest fire by instant, across time zones (#155)", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-01T00:00:00Z"));
    mount(<ResidentsPage page="residents" params={{}} />, {
      fetch: router({
        "/residents": json(200, listing),
        "/routines": json(200, {
          ...ROUTINES,
          routines: [
            // As text the +02:00 row sorts last; as instants it is first. 08:00-05:00 is
            // 13:00Z, thirteen hours away; 09:00+02:00 is 07:00Z, seven.
            { ...ROUTINE, key: "a", routine: "late", next_fire: "2026-09-01T08:00:00-05:00" },
            { ...ROUTINE, key: "b", routine: "early", next_fire: "2026-09-01T09:00:00+02:00" },
          ],
        }),
      }),
    });

    await vi.waitFor(() => expect(screen.getByText(/next/)).toBeTruthy());
    // The console's localeCompare picked "13h 0m" here, which is not the soonest fire.
    expect(screen.getByText("in 7h 0m")).toBeTruthy();
    expect(screen.queryByText("in 13h 0m")).toBeNull();
  });

  it("still lists residents when the routine ledger cannot be read", async () => {
    mount(<ResidentsPage page="residents" params={{}} />, {
      fetch: router({
        "/residents": json(200, listing),
        "/routines": json(503, { detail: { error: "unavailable", message: "no" } }),
      }),
    });

    // A fleet list that vanishes because a second endpoint failed is worse than one that
    // simply says nothing about next fires.
    expect(await screen.findByText("Hob")).toBeTruthy();
  });
});
