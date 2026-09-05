import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { StewardProvider } from "../steward/context.jsx";
import QueuePage from "./QueuePage.jsx";

afterEach(cleanup);
const issue = (number, labels = []) => ({
  number,
  title: `Issue ${number}`,
  url: `https://github.com/owner/repo/issues/${number}`,
  labels,
  blockers: [],
  unknown_blockers: [],
  chains: [],
  stale_blocked: false,
});
function queue() {
  return {
    repository: "owner/repo",
    reporter: "karen",
    observed_at: "2026-09-05T12:00:00Z",
    issues: [
      issue(1, ["priority:high"]),
      {
        ...issue(2, ["status:blocked"]),
        stale_blocked: true,
        blockers: [{ number: 3, state: "closed" }],
      },
    ],
    pull_requests: [
      {
        number: 4,
        title: "A PR",
        url: "https://github.com/owner/repo/pull/4",
        mergeability: "UNKNOWN",
      },
    ],
    recently_closed: [],
    ranked_items: [{ number: 3, state: "closed" }],
    report: { note: null, message: "No queue-review run has been recorded." },
  };
}
function mount(fetch, token = "test") {
  const storage = {
    getItem: () => token,
    setItem: vi.fn(),
    removeItem: vi.fn(),
  };
  return render(
    <StewardProvider storage={storage} fetch={fetch}>
      <QueuePage />
    </StewardProvider>,
  );
}
const response = (body, status = 200) => ({
  ok: status === 200,
  status,
  text: async () => JSON.stringify(body),
});

it("keeps a locked queue private", () => {
  const fetch = vi.fn();
  mount(fetch, null);
  expect(screen.getByText(/The work queue/)).toBeTruthy();
  expect(fetch).not.toHaveBeenCalled();
});

it("shows computed stale labels, unknown mergeability and absence of a resident note", async () => {
  mount(async () => response(queue()));
  expect(await screen.findByText("1 blocked labels need review")).toBeTruthy();
  expect(screen.getByText("unknown")).toBeTruthy();
  expect(
    screen.getByText(/No queue-review run has been recorded/),
  ).toBeTruthy();
  fireEvent.change(screen.getByLabelText("Issue label"), {
    target: { value: "priority:high" },
  });
  expect(screen.getByText("#1 · Issue 1")).toBeTruthy();
  expect(screen.queryByText("#2 · Issue 2")).toBeNull();
});

it("attributes a recommendation to its receipt while rendering the tracker's current state", async () => {
  const data = queue();
  data.report = {
    run: {
      resident: "karen",
      run_id: "real-run",
      recorded_at: "2026-09-05T10:00:00Z",
    },
    note: {
      commit: "a".repeat(40),
      recommendations: [
        {
          number: 3,
          reason: "This used to be next.",
          evidence: [
            { source: "git show abc:file.py", quote: "recorded excerpt" },
          ],
        },
      ],
    },
  };
  mount(async () => response(data));
  expect(await screen.findByText("This used to be next.")).toBeTruthy();
  expect(screen.getByText("real-run")).toBeTruthy();
  expect(screen.getAllByText("closed").length).toBeGreaterThan(0);
  fireEvent.click(screen.getByText("Evidence"));
  expect(screen.getByText("recorded excerpt")).toBeTruthy();
});

it("allows a failed tracker read to be retried", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(
      response({ detail: { message: "Tracker unavailable" } }, 503),
    )
    .mockResolvedValue(response(queue()));
  mount(fetch);
  expect(await screen.findByText("Tracker unavailable")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  await waitFor(() => expect(screen.getByText("#1 · Issue 1")).toBeTruthy());
});
