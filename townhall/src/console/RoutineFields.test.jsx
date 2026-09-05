import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NavigationProvider } from "../navigation.jsx";
import { StewardProvider } from "../steward/context.jsx";
import ResidentDeclaration from "../pages/ResidentDeclaration.jsx";
import RoutinesPage from "../pages/RoutinesPage.jsx";
import { LedgerProvider } from "./ledger.jsx";

const DECLARATION = {
  id: "hob", revision: "sha256:original", text: "version: 0\n", soul: "Hob's soul\n",
  manifest: {
    version: 0, id: "hob", soul: { name: "Hob" },
    deploy: { host: "dxp2800", extra: "  leave this alone\n" },
    routes: [{ transport: "telegram", reference: "123" }],
    app_grants: { calendar: ["read"] },
    routines: [{
      id: "daily", schedule: "0 9 * * *", prompt: "Write it.\n", timeout_s: 900,
      deliver: "chat", quiet_word: "NOTHING", extra: { text: "  unchanged\n" },
    }],
  },
};
const json = (status, body) => Promise.resolve({
  status, ok: status >= 200 && status < 300, text: async () => JSON.stringify(body),
});

function mount({ manifest = DECLARATION.manifest, diagnostics } = {}) {
  let declaration = { ...DECLARATION, manifest };
  let reloaded = false;
  const fetch = vi.fn((url, init = {}) => {
    if (init.method === "PUT") {
      if (diagnostics) return json(422, { detail: {
        error: "manifest_invalid", message: "the manifest does not validate", diagnostics,
      } });
      const body = JSON.parse(init.body);
      declaration = { ...declaration, manifest: body.manifest, revision: "sha256:saved" };
      return json(200, { status: "accepted", message: "written and validated", warnings: [] });
    }
    if (url === "/reload") {
      reloaded = true;
      return json(200, { status: "reloaded", residents: 1, routines: declaration.manifest.routines.length });
    }
    if (url === "/routines") return json(200, { routines: reloaded ? declaration.manifest.routines.map((routine) => ({
      ...routine, key: `hob/${routine.id}`, resident: "hob", resident_name: "Hob",
      routine: routine.id, enabled: routine.enabled !== false, schedule_tz: routine.schedule_tz || "UTC",
      next_fire: "2026-09-06T09:00:00Z",
    })) : [] });
    if (url === "/skills") return json(200, { library: null, skills: [], errors: [] });
    return json(200, declaration);
  });
  const storage = { getItem: () => "operator-token", setItem() {}, removeItem() {} };
  const wrap = (child) => <NavigationProvider base="/">
    <StewardProvider storage={storage} fetch={fetch}><LedgerProvider>{child}</LedgerProvider></StewardProvider>
  </NavigationProvider>;
  const view = render(wrap(<ResidentDeclaration id="hob" />));
  return { fetch, showRoutines: () => view.rerender(wrap(<RoutinesPage />)) };
}

const control = (row, key) => screen.getByLabelText(`routine ${row} · ${key}`);
const change = (row, key, value) => fireEvent.change(control(row, key), { target: { value } });
const drawn = () => screen.findByRole("button", { name: "Add routine" });
async function save(fetch) {
  fireEvent.click(screen.getByRole("button", { name: "Write declaration" }));
  await waitFor(() => expect(fetch.mock.calls.some(([, init]) => init.method === "PUT")).toBe(true));
  const [url, init] = fetch.mock.calls.find(([, init]) => init.method === "PUT");
  expect(url).toBe("/residents/hob/declaration");
  return JSON.parse(init.body);
}
afterEach(cleanup);

describe("routine declarations in fields mode", () => {
  it("saves an untouched declaration without materialising defaults or rewriting extras", async () => {
    const { fetch } = mount();
    await drawn();
    expect(control(1, "enabled").checked).toBe(true);
    expect(control(1, "schedule_tz").value).toBe("");
    expect(await save(fetch)).toEqual({
      manifest: DECLARATION.manifest, soul: DECLARATION.soul, revision: DECLARATION.revision,
    });
  });

  it("edits all routine fields and disables it while preserving unfamiliar data", async () => {
    const { fetch } = mount();
    await drawn();
    change(1, "id", "evening");
    change(1, "schedule", "30 18 * * *");
    change(1, "schedule_tz", "Europe/Ljubljana");
    change(1, "prompt", "Read the inbox.\nThen write a summary.");
    change(1, "requires", "read-inbox\nwrite-journal");
    change(1, "timeout_s", "1200");
    fireEvent.click(control(1, "enabled"));
    const { manifest } = await save(fetch);
    expect(manifest).toEqual({ ...DECLARATION.manifest, routines: [{
      ...DECLARATION.manifest.routines[0], id: "evening", schedule: "30 18 * * *",
      schedule_tz: "Europe/Ljubljana", prompt: "Read the inbox.\nThen write a summary.",
      requires: ["read-inbox", "write-journal"], timeout_s: 1200, enabled: false,
    }] });
    expect(JSON.stringify(manifest.routines[0].extra)).toBe(JSON.stringify(DECLARATION.manifest.routines[0].extra));
  });

  it("adds the first routine, saves, reloads, and reads its next occurrence on the Routines page", async () => {
    const { routines: _removed, ...manifest } = DECLARATION.manifest;
    const { fetch, showRoutines } = mount({ manifest });
    fireEvent.click(await drawn());
    change(1, "id", "morning");
    change(1, "schedule", "0 9 * * *");
    change(1, "prompt", "Write the morning summary.");
    expect((await save(fetch)).manifest.routines).toEqual([{
      id: "morning", schedule: "0 9 * * *", prompt: "Write the morning summary.", timeout_s: 900,
    }]);
    fireEvent.click(await screen.findByRole("button", { name: "reload steward's own copy" }));
    await screen.findByText(/reloaded: 1 residents, 1 routines/);
    showRoutines();
    expect(await screen.findByText("morning")).toBeTruthy();
    expect(document.querySelector('time[datetime="2026-09-06T09:00:00Z"]')).toBeTruthy();
  });

  it("removes a row, edits the remaining row, and removes the last routine", async () => {
    const { fetch } = mount();
    fireEvent.click(await drawn());
    change(2, "id", "second");
    fireEvent.click(screen.getByRole("button", { name: "Remove routine 1" }));
    expect(control(1, "id").value).toBe("second");
    change(1, "prompt", "Second routine.");
    fireEvent.click(screen.getByRole("button", { name: "Remove routine 1" }));
    expect(screen.getByText("No routines declared.")).toBeTruthy();
    expect((await save(fetch)).manifest.routines).toEqual([]);
  });

  it("clears optional fields and leaves an invalid timeout for steward to reject", async () => {
    const { fetch } = mount({ manifest: { ...DECLARATION.manifest, routines: [{
      ...DECLARATION.manifest.routines[0], schedule_tz: "Europe/Ljubljana", requires: ["read-inbox"], enabled: false,
    }] } });
    await drawn();
    change(1, "schedule_tz", "");
    change(1, "requires", "");
    change(1, "timeout_s", "1.5");
    fireEvent.click(control(1, "enabled"));
    const routine = (await save(fetch)).manifest.routines[0];
    expect(routine).not.toHaveProperty("schedule_tz");
    expect(routine.requires).toEqual([]);
    expect(routine.timeout_s).toBe(1.5);
    expect(routine.enabled).toBe(true);
  });

  it("places bracketed and dotted diagnostics on the right row, including required skills", async () => {
    const diagnostics = [
      { field: "routines[1].schedule", problem: "Invalid cron" },
      { field: "routines.1.prompt", problem: "Prompt is required" },
      { field: "routines[1].id", problem: "Duplicate id" },
      { field: "routines[1].requires[0]", problem: "Skill is not granted" },
    ];
    const { fetch } = mount({ diagnostics });
    fireEvent.click(await drawn());
    await save(fetch);
    await screen.findAllByText("Invalid cron");
    for (const [key, message] of [["schedule", "Invalid cron"], ["prompt", "Prompt is required"],
      ["id", "Duplicate id"], ["requires", "Skill is not granted"]]) {
      expect(control(2, key).getAttribute("aria-invalid")).toBe("true");
      expect(within(control(2, key).closest("label")).getByText(message)).toBeTruthy();
      expect(control(1, key).getAttribute("aria-invalid")).toBeNull();
    }
    // Removing the first row shifts the second row into its place. A refusal from the
    // old array must not remain attached to a position a later routine can occupy.
    fireEvent.click(screen.getByRole("button", { name: "Remove routine 1" }));
    fireEvent.click(screen.getByRole("button", { name: "Add routine" }));
    expect(control(2, "schedule").getAttribute("aria-invalid")).toBeNull();
  });
});
