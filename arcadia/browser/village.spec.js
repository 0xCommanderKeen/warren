import { readFileSync } from "node:fs";
import { test, expect } from "@playwright/test";

const fixture = JSON.parse(readFileSync(new URL(
  "../../chronicle/tests/fixtures/state-contract/complete-v1.json", import.meta.url,
), "utf8"));

// Real WebGL scenes share one worker so population measurements do not compete
// with other browser instances for the GPU.
test.describe.configure({ mode: "default", timeout: 60_000 });

function population(count) {
  const envelope = structuredClone(fixture);
  const template = envelope.snapshot.villagers[0];
  const resident = envelope.snapshot.residents[0];
  envelope.snapshot.villagers = Array.from({ length: count }, (_, i) => ({
    ...structuredClone(template), id: `claude:resident-${i}`, name: `Villager ${String(i).padStart(3, "0")}`,
    resident_file: `resident-${i}.resident.json`, home: i,
    state: i % 3 === 0 ? "working" : "resting", project: `project-${i % 5}`,
    pending_approval_ids: [], history: [], last_line: `Recorded activity for villager ${i}`,
  }));
  envelope.snapshot.residents = envelope.snapshot.villagers.map(v => ({
    ...structuredClone(resident), file: v.resident_file, home: v.home,
    match: { agent_id: v.id }, meta: { ...resident.meta, name: v.name },
  }));
  for (const field of ["artifacts", "tasks", "approvals", "journals", "routines", "diagnostics", "diagnostic_residents"]) envelope.snapshot[field] = [];
  envelope.snapshot.capacity.villagers = Math.max(100, count);
  return envelope;
}

async function load(page, envelope = fixture) {
  const errors = [];
  const missing = [];
  const requests = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("response", response => { if (response.status() === 404) missing.push(response.url()); });
  page.on("request", request => requests.push(request.url()));
  await page.addInitScript(() => {
    window.EventSource = class {
      constructor() {
        this.listeners = {};
        window.villageStream = this;
        setTimeout(() => this.onopen?.(), 10);
      }
      addEventListener(name, listener) { this.listeners[name] = listener; }
      close() {}
    };
  });
  await page.route("**/chronicle/state", route => route.fulfill({ json: envelope }));
  await page.goto("/?backend=https://untrusted.invalid");
  return { errors, missing, requests };
}

async function ready(page, count = fixture.snapshot.villagers.length) {
  const canvas = page.locator('canvas[data-renderer="three"]');
  await expect(canvas).toBeVisible();
  await expect(canvas).toHaveAttribute("data-ready", "true");
  await expect(canvas).toHaveAttribute("data-agents", String(count));
  // A mounted canvas is insufficient: wait for a measured, rendered WebGL frame.
  await expect.poll(() => canvas.getAttribute("data-draw-calls").then(Number), { timeout: 20_000 }).toBeGreaterThan(0);
  await expect(page.getByRole("status", { name: "Village connection" })).toHaveText("Live village");
  return canvas;
}

async function fits(page) {
  const canvas = await page.locator('canvas[data-renderer="three"]').boundingBox();
  const host = await page.locator(".ve-canvas").boundingBox();
  expect(canvas.width).toBeGreaterThan(100);
  expect(canvas.height).toBeGreaterThan(100);
  expect(Math.abs(canvas.width - host.width)).toBeLessThan(2);
  expect(Math.abs(canvas.height - host.height)).toBeLessThan(2);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
}

function clean(observed) {
  expect(observed.errors).toEqual([]);
  expect(observed.missing).toEqual([]);
  expect(observed.requests.some(url => new URL(url).hostname === "untrusted.invalid")).toBe(false);
  expect(observed.requests.some(url => /Monk-idle|Ninja.?Adventure/.test(url))).toBe(false);
}

for (const width of [1440, 768, 390, 320]) {
  test(`real village fits and stays usable at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    const observed = await load(page);
    const canvas = await ready(page);
    await fits(page);
    await page.getByRole("searchbox").fill("nobody matches");
    await expect(page.locator(".ve-person")).toHaveCount(0);
    await page.getByRole("searchbox").fill("Keeper");
    await page.locator(".ve-person").click();
    await expect(page.getByRole("region", { name: "Selected villager" })).toContainText("Keeper");
    await page.getByRole("button", { name: "Pause motion", exact: true }).click();
    await expect(page.getByRole("button", { name: "Resume motion", exact: true })).toHaveAttribute("aria-pressed", "true");
    await expect(canvas).toHaveAttribute("data-paused", "true");
    await page.getByRole("button", { name: "Close villager details" }).click();
    await expect(page.getByRole("region", { name: "Selected villager" })).toHaveCount(0);
    await fits(page);
    clean(observed);
  });
}

test("same mounted canvas follows viewport resize", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const observed = await load(page);
  await ready(page);
  await page.locator("canvas").evaluate(canvas => { window.originalVillageCanvas = canvas; });
  for (const width of [768, 390, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await expect.poll(async () => {
      const canvas = await page.locator("canvas").boundingBox();
      const host = await page.locator(".ve-canvas").boundingBox();
      return Math.abs(canvas.width - host.width) + Math.abs(canvas.height - host.height);
    }).toBeLessThan(2);
    await fits(page);
    expect(await page.locator("canvas").evaluate(canvas => canvas === window.originalVillageCanvas)).toBe(true);
  }
  clean(observed);
});

test("selection survives stream updates and removed villagers disappear", async ({ page }) => {
  const observed = await load(page);
  const canvas = await ready(page);
  await page.locator(".ve-person").filter({ hasText: "Keeper" }).click();
  const next = structuredClone(fixture);
  next.snapshot.generation += 1;
  next.snapshot.villagers[0].last_line = "New activity from Chronicle";
  await page.evaluate(envelope => window.villageStream.listeners.snapshot({ data: JSON.stringify(envelope) }), next);
  await expect(page.getByRole("region", { name: "Selected villager" })).toContainText("New activity from Chronicle");
  next.snapshot.generation += 1;
  next.snapshot.villagers = [];
  await page.evaluate(envelope => window.villageStream.listeners.snapshot({ data: JSON.stringify(envelope) }), next);
  await expect(canvas).toHaveAttribute("data-agents", "0");
  await expect(page.locator(".ve-person")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Selected villager" })).toHaveCount(0);
  await expect(page.getByText(/The village is quiet/)).toBeVisible();
  clean(observed);
});

test("reduced motion starts actual renderer paused", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const observed = await load(page);
  const canvas = await ready(page);
  await expect(page.getByRole("button", { name: "Resume motion", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(canvas).toHaveAttribute("data-paused", "true");
  clean(observed);
});

for (const count of [0, 5, 25, 100]) {
  test(`renders ${count} agents with a usable directory`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    const observed = await load(page, population(count));
    const canvas = await ready(page, count);
    await expect(page.locator(".ve-person")).toHaveCount(count);
    await fits(page);
    if (count) {
      const name = `Villager ${String(count - 1).padStart(3, "0")}`;
      await page.getByRole("searchbox").fill(name);
      await page.locator(".ve-person").click();
      await expect(page.getByRole("region", { name: "Selected villager" })).toContainText(name);
    } else await expect(page.getByText(/The village is quiet/)).toBeVisible();
    const metrics = await canvas.evaluate(node => ({ agents: node.dataset.agents, buildings: node.dataset.buildings, fps: node.dataset.fps, drawCalls: node.dataset.drawCalls }));
    await testInfo.attach(`population-${count}-render-metrics`, { body: JSON.stringify(metrics, null, 2), contentType: "application/json" });
    expect(Number(metrics.fps)).toBeGreaterThan(0);
    clean(observed);
  });
}

test("unsupported snapshots show a visible contract fallback", async ({ page }) => {
  const invalid = structuredClone(fixture);
  invalid.snapshot.schema_version = 999;
  const observed = await load(page, invalid);
  await expect(page.getByRole("alert")).toContainText("Arcadia cannot enter this village");
  await expect(page.getByRole("alert")).toContainText("Unsupported village schema version: 999");
  await expect(page.locator("canvas")).toHaveCount(0);
  clean(observed);
});

test("lost WebGL context leaves the agent directory usable", async ({ page }) => {
  const observed = await load(page);
  const canvas = await ready(page);
  await canvas.evaluate(node => {
    const gl = node.getContext("webgl2");
    const extension = gl?.getExtension("WEBGL_lose_context");
    if (!extension) throw new Error("Browser must support WEBGL_lose_context for this regression test");
    extension.loseContext();
  });
  await expect(page.getByText("The village view couldn't open.")).toBeVisible();
  await expect(page.getByText("Everyone is still available in the directory.")).toBeVisible();
  await page.getByRole("searchbox").fill("Keeper");
  await page.locator(".ve-person").click();
  await expect(page.getByRole("region", { name: "Selected villager" })).toContainText("Keeper");
  clean(observed);
});


for (const count of [0, 5, 100]) {
  test(`shared workshop interior shows ${count} actual occupants`, async ({ page }) => {
    await page.setViewportSize({ width: count === 5 ? 390 : 1440, height: 1000 });
    const envelope = population(count);
    envelope.snapshot.villagers.forEach(agent => { agent.state = "working"; });
    const observed = await load(page, envelope);
    const outdoor = await ready(page, count);
    await expect(outdoor).toHaveAttribute("data-outside-agents", "0");
    await outdoor.evaluate(canvas => { window.originalVillageCanvas = canvas; });
    await page.getByRole("button", { name: "Workshop", exact: true }).click();
    const room = page.getByTestId("interior-canvas");
    await expect(room).toBeVisible();
    await expect(room).toHaveAttribute("data-ready", "true");
    await expect(room).toHaveAttribute("data-agents", String(count));
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    if (count) {
      await page.getByRole("region", { name: "Building interior" }).getByRole("button", { name: /Villager 000/ }).click();
      await expect(page.getByRole("region", { name: "Selected villager" })).toContainText("Villager 000");
      await expect(room).toBeVisible();
      envelope.snapshot.generation += 1;
      envelope.snapshot.villagers[0].state = "resting";
      await page.evaluate(next => window.villageStream.listeners.snapshot({ data: JSON.stringify(next) }), envelope);
      await expect(room).toHaveAttribute("data-agents", String(count - 1));
    }
    await page.getByRole("button", { name: "Back to village", exact: true }).click();
    await expect(outdoor).toBeVisible();
    expect(await outdoor.evaluate(canvas => canvas === window.originalVillageCanvas)).toBe(true);
    await fits(page);
    clean(observed);
  });
}


test("homes and lodge open with their occupants and retain a directory on room graphics loss", async ({ page }) => {
  const envelope = population(3);
  envelope.snapshot.villagers[1].residency = "resident";
  envelope.snapshot.villagers[2].residency = "visitor";
  const observed = await load(page, envelope);
  await ready(page, 3);
  await page.getByRole("searchbox").fill("Villager 001");
  await page.locator(".ve-person").click();
  const room = page.getByTestId("interior-canvas");
  await expect(room).toHaveAttribute("data-building", "home:claude:resident-1");
  await expect(room).toHaveAttribute("data-agents", "1");
  await page.getByRole("button", { name: "Back to village", exact: true }).click();
  await page.getByRole("button", { name: "Visitor lodge", exact: true }).click();
  await expect(room).toHaveAttribute("data-building", "lodge:0");
  await expect(room).toHaveAttribute("data-agents", "1");
  await room.evaluate(canvas => canvas.getContext("webgl2").getExtension("WEBGL_lose_context").loseContext());
  await expect(page.getByText("The room view couldn't open.")).toBeVisible();
  await page.getByRole("region", { name: "Building interior" }).getByRole("button", { name: /Villager 002/ }).click();
  await expect(page.getByRole("region", { name: "Selected villager" })).toContainText("Villager 002");
  await page.getByRole("button", { name: "Back to village", exact: true }).click();
  await expect(page.locator('canvas[data-renderer="three"]')).toBeVisible();
  clean(observed);
});

test("personal workstations stay put through departures, return navigation and reload", async ({ page }) => {
  const envelope = population(3);
  envelope.snapshot.villagers.forEach(agent => { agent.state = "working"; });
  const observed = await load(page, envelope);
  await ready(page, 3);
  await page.getByRole("button", { name: "Workshop", exact: true }).click();
  const room = page.getByTestId("interior-canvas");
  const station = async () => {
    const stations = JSON.parse(await room.getAttribute("data-stations"));
    return stations.find(item => item.id === "claude:resident-1").position;
  };
  await expect(room).toHaveAttribute("data-agents", "3");
  const original = await station();
  envelope.snapshot.generation += 1;
  envelope.snapshot.villagers.shift();
  await page.evaluate(next => window.villageStream.listeners.snapshot({ data: JSON.stringify(next) }), envelope);
  await expect(room).toHaveAttribute("data-agents", "2");
  expect(await station()).toEqual(original);
  await page.getByRole("button", { name: "Back to village", exact: true }).click();
  await page.getByRole("button", { name: "Workshop", exact: true }).click();
  await expect(room).toHaveAttribute("data-agents", "2");
  expect(await station()).toEqual(original);
  await page.reload();
  await ready(page, 2);
  await page.getByRole("button", { name: "Workshop", exact: true }).click();
  await expect(room).toHaveAttribute("data-agents", "2");
  expect(await station()).toEqual(original);
  clean(observed);
});

test("camera and display preferences survive a reload", async ({ page }) => {
  const observed = await load(page, population(5));
  const canvas = await ready(page, 5);
  await page.getByRole("button", { name: "Zoom in", exact: true }).click();
  await page.getByRole("combobox", { name: "Rendering quality" }).selectOption("low");
  await page.getByRole("button", { name: "Pause motion", exact: true }).click();
  await expect.poll(() => page.evaluate(() => JSON.parse(localStorage.getItem("arcadia:camera:v1"))?.zoom)).toBeGreaterThan(1);
  const saved = JSON.parse(await canvas.getAttribute("data-camera"));
  await page.reload();
  await ready(page, 5);
  const restored = JSON.parse(await canvas.getAttribute("data-camera"));
  expect(restored.zoom).toBeCloseTo(saved.zoom, 5);
  for (const field of ["position", "target"]) restored[field].forEach((value, i) => expect(value).toBeCloseTo(saved[field][i], 5));
  await expect(page.getByRole("combobox", { name: "Rendering quality" })).toHaveValue("low");
  await expect(canvas).toHaveAttribute("data-paused", "true");
  clean(observed);
});

test("following enters the agent's next room and stops at overview", async ({ page }) => {
  const envelope = population(1);
  envelope.snapshot.villagers[0].residency = "resident";
  const observed = await load(page, envelope);
  await ready(page, 1);
  await page.locator(".ve-person").click();
  await page.getByRole("button", { name: "Follow agent ↗" }).click();
  const room = page.getByTestId("interior-canvas");
  await expect(room).toHaveAttribute("data-building", "workshop");
  envelope.snapshot.generation += 1;
  envelope.snapshot.villagers[0].state = "resting";
  await page.evaluate(next => window.villageStream.listeners.snapshot({ data: JSON.stringify(next) }), envelope);
  await expect(room).toHaveAttribute("data-building", "home:claude:resident-0");
  await expect(room).toHaveAttribute("data-focus-agent", "claude:resident-0");
  await expect(page.getByRole("navigation", { name: "Your location" })).toContainText("Following Villager 000");
  await page.getByRole("button", { name: "Village overview", exact: true }).click();
  await expect(page.locator('canvas[data-renderer="three"]')).toBeVisible();
  await expect(page.getByRole("button", { name: "Stop following" })).toHaveCount(0);
  clean(observed);
});

test("archive opens recorded work and returns to the village", async ({ page }) => {
  const observed = await load(page);
  await ready(page);
  await page.getByRole("button", { name: "Archive", exact: true }).click();
  const archive = page.getByRole("region", { name: "Village archive" });
  await expect(archive).toBeVisible();
  await expect(archive).toContainText("Artifact");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await archive.getByRole("button", { name: "Back to village", exact: true }).click();
  await expect(page.locator('canvas[data-renderer="three"]')).toBeVisible();
  clean(observed);
});

test("workshop board opens a real task and locates its claimant", async ({ page }) => {
  const observed = await load(page);
  await ready(page);
  await page.getByRole("button", { name: "Workshop", exact: true }).click();
  const board = page.getByRole("region", { name: "Workshop task board" });
  await board.getByRole("button", { name: /Freeze contract/ }).click();
  await expect(board.getByRole("region", { name: "Selected task" })).toContainText("python");
  await expect(page.getByRole("region", { name: "Selected villager" })).toContainText("Keeper");
  await board.getByRole("button", { name: "Locate claimant Keeper" }).click();
  await expect(page.getByRole("region", { name: "Selected villager" })).toContainText("Keeper");
  clean(observed);
});

test("return briefing reports retained changes and can be marked seen", async ({ page }) => {
  const envelope = structuredClone(fixture);
  const observed = await load(page, envelope);
  await ready(page);
  await expect(page.getByRole("region", { name: "Since your last visit" })).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => Boolean(localStorage.getItem("arcadia:visit:v1")))).toBe(true);
  envelope.snapshot.generation += 1;
  envelope.snapshot.tasks[0].state = "done";
  envelope.snapshot.tasks[1].state = "failed";
  await page.reload();
  await ready(page);
  const briefing = page.getByRole("region", { name: "Since your last visit" });
  await briefing.locator("summary").click();
  await expect(briefing).toContainText("Freeze contract");
  await expect(briefing).toContainText("Deploy?");
  await briefing.getByRole("button", { name: "Draft the letter" }).click();
  await expect(page.getByRole("region", { name: "Selected task" })).toContainText("Draft the letter");
  await briefing.getByRole("button", { name: "Mark seen" }).click();
  await expect(briefing).toHaveCount(0);
  clean(observed);
});

test("zoomed overview map and keyboard shortcuts navigate without stealing search input", async ({ page }) => {
  const observed = await load(page);
  await ready(page);
  await page.getByRole("button", { name: "Zoom in", exact: true }).click();
  await page.getByRole("button", { name: "Zoom in", exact: true }).click();
  const map = page.getByRole("group", { name: "Village overview map" });
  await expect(map).toBeVisible();
  await map.getByRole("button", { name: "Locate Visitor lodge on map" }).click();
  await expect(page.getByTestId("interior-canvas")).toHaveAttribute("data-building", "lodge:0");
  await page.getByRole("button", { name: "Back to village", exact: true }).click();
  await page.getByRole("searchbox").focus();
  await page.keyboard.press("Alt+Digit3");
  await expect(page.getByRole("region", { name: "Village archive" })).toHaveCount(0);
  await page.getByRole("button", { name: "Go to Workshop" }).focus();
  await page.keyboard.press("Alt+Digit3");
  await expect(page.getByRole("region", { name: "Village archive" })).toBeVisible();
  clean(observed);
});
