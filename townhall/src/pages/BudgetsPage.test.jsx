import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { NavigationProvider } from "../navigation.jsx";
import { StewardProvider } from "../steward/context.jsx";
import BudgetsPage from "./BudgetsPage.jsx";

const json = (body) => ({ status: 200, ok: true, text: async () => JSON.stringify(body) });
const declaration = (cap) => ({ manifest: { budgets: { daily_cost_usd: cap } }, soul: "Hob", revision: `rev-${cap}` });
const budget = (summary) => ({
  window: { tz: "UTC", day: "2026-09-05", end: "2026-09-06T00:00:00Z" },
  spent: {}, budgets: [], summary,
});
function deferred() {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, reject, resolve: (body) => resolve(json(body)) };
}
function setup() {
  const reads = [];
  const fetch = vi.fn((url, options) => {
    if (options.method === "PUT") return Promise.resolve(json({
      status: "written", message: "Budget saved.", commit: { committed: true, sha: "abcdef123456789", message: "Set cap" },
    }));
    const request = { url, ...deferred() };
    reads.push(request);
    return request.promise;
  });
  const storage = { getItem: () => "steward-operator-test", setItem() {}, removeItem() {} };
  const view = (id) => (
    <NavigationProvider base="/">
      <StewardProvider storage={storage} fetch={fetch}><BudgetsPage params={{ id }} /></StewardProvider>
    </NavigationProvider>
  );
  const rendered = render(view("hob"));
  return { reads, navigate: (id) => rendered.rerender(view(id)) };
}
async function resolveReads(reads, cap, summary) {
  await act(async () => {
    for (const read of reads) read.resolve(read.url.endsWith("/budget") ? budget(summary) : declaration(cap));
  });
}
async function saveCaps() {
  fireEvent.change(screen.getByDisplayValue("10"), { target: { value: "20" } });
  fireEvent.click(screen.getByRole("button", { name: "Write caps" }));
}
function expectReceipt() {
  expect(screen.getByText("caps written")).toBeTruthy();
  expect(screen.getByText(/abcdef1/)).toBeTruthy();
  expect(screen.getByText(/Budget saved\./)).toBeTruthy();
}
afterEach(cleanup);

it.each(["budget", "declaration"])("keeps the receipt through both refreshes when %s finishes first", async (first) => {
  const { reads } = setup();
  expect(screen.getByText("reading the ledger…")).toBeTruthy();
  expect(screen.queryByText("caps written")).toBeNull();
  await waitFor(() => expect(reads).toHaveLength(2));
  await resolveReads(reads, 10, "original spend");
  await saveCaps();
  await waitFor(() => expect(reads).toHaveLength(4));
  expectReceipt();
  const pending = reads.slice(2).sort((a) => a.url.endsWith(`/${first}`) ? -1 : 1);
  await resolveReads([pending[0]], 20, "refreshed spend");
  expectReceipt();
  await resolveReads([pending[1]], 20, "refreshed spend");
  await screen.findByText("refreshed spend");
  await waitFor(() => expect(screen.getByRole("button", { name: "Nothing changed" }).disabled).toBe(true));
  expectReceipt();
});

it("clears the old resident's receipt and data while the next resident loads", async () => {
  const { reads, navigate } = setup();
  await waitFor(() => expect(reads).toHaveLength(2));
  await resolveReads(reads, 10, "hob spend");
  await saveCaps();
  await waitFor(() => expect(reads).toHaveLength(4));
  expectReceipt();
  navigate("pip");
  await waitFor(() => expect(reads).toHaveLength(6));
  expect(screen.getByText("reading the ledger…")).toBeTruthy();
  expect(screen.queryByText("caps written")).toBeNull();
  expect(screen.queryByText("hob spend")).toBeNull();
  await resolveReads(reads.slice(2, 4), 20, "late hob spend");
  expect(screen.queryByText("late hob spend")).toBeNull();
  await resolveReads(reads.slice(4), 30, "pip spend");
  await screen.findByText("pip spend");
  expect(screen.getByDisplayValue("30")).toBeTruthy();
  expect(screen.queryByText("caps written")).toBeNull();
  expect(screen.queryByText(/abcdef1/)).toBeNull();
});


it.each(["budget", "declaration"])("keeps the receipt and blocks writes after a failed %s refresh until retry succeeds", async (failed) => {
  const { reads } = setup();
  await waitFor(() => expect(reads).toHaveLength(2));
  await resolveReads(reads, 10, "original spend");
  await saveCaps();
  await waitFor(() => expect(reads).toHaveLength(4));
  expect(screen.getByRole("button", { name: "Write caps" }).disabled).toBe(true);
  await act(async () => {
    for (const read of reads.slice(2)) {
      if (read.url.endsWith(`/${failed}`)) read.reject(new Error("Refresh unavailable"));
      else read.resolve(read.url.endsWith("/budget") ? budget("updated spend") : declaration(20));
    }
  });
  await screen.findByText(/Refresh unavailable/);
  expectReceipt();
  fireEvent.change(screen.getByDisplayValue("20"), { target: { value: "25" } });
  expect(screen.getByRole("button", { name: "Write caps" }).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Retry refresh" }));
  await waitFor(() => expect(reads).toHaveLength(6));
  expectReceipt();
  await resolveReads(reads.slice(4), 20, "retry spend");
  await screen.findByText("retry spend");
  expect(screen.queryByText(/Refresh unavailable/)).toBeNull();
  expectReceipt();
  fireEvent.change(screen.getByDisplayValue("20"), { target: { value: "25" } });
  expect(screen.getByRole("button", { name: "Write caps" }).disabled).toBe(false);
});
