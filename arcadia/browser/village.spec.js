import { readFileSync } from "node:fs";
import { test, expect } from "@playwright/test";

const fixture = JSON.parse(
  readFileSync(
    new URL(
      "../../chronicle/tests/fixtures/state-contract/complete-v1.json",
      import.meta.url,
    ),
    "utf8",
  ),
);

async function village(page, envelope = fixture) {
  await page.addInitScript(() => {
    window.EventSource = class {
      constructor() {
        this.listeners = {};
        window.villageStream = this;
        setTimeout(() => this.onopen?.(), 10);
      }
      addEventListener(name, listener) {
        this.listeners[name] = listener;
      }
      close() {}
    };
  });
  await page.route("**/chronicle/state", (route) =>
    route.fulfill({ json: envelope }),
  );
  await page.goto("/?backend=https://untrusted.invalid");
  await expect(page.locator("canvas")).toBeVisible();
  // A canvas existing does not mean Phaser loaded its sprites or created its scene.
  await page.waitForFunction(() =>
    performance
      .getEntriesByType("resource")
      .some((entry) => entry.name.includes("Monk-idle.png")),
  );
  await expect(
    page.getByRole("status", { name: "Village connection" }),
  ).toHaveText("Live village");
}

for (const width of [1440, 768, 390, 320]) {
  test(`village fits and stays usable at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    const errors = [];
    page.on("pageerror", (error) => {
      errors.push(error.message);
      console.log("Browser error:", error.message);
    });
    const requests = [];
    page.on("request", (request) => requests.push(request.url()));
    await village(page);
    const canvas = page.locator("canvas");
    await expect(canvas).toHaveAttribute("width", "640");
    await expect(canvas).toHaveAttribute("height", "384");
    const actual = await canvas.boundingBox();
    const host = await page.locator(".village-canvas").boundingBox();
    expect(Math.abs(actual.width - host.width)).toBeLessThan(2);
    expect(Math.abs(actual.height - host.height)).toBeLessThan(2);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    expect(
      requests.some((url) => new URL(url).hostname === "untrusted.invalid"),
    ).toBe(false);
    await page.getByRole("searchbox").fill("Keeper");
    await page.locator(".person").click();
    await expect(
      page.getByRole("region", { name: "Selected villager" }),
    ).toContainText("Keeper");
    await page.getByRole("button", { name: "Pause motion" }).click();
    await expect(
      page.getByRole("button", { name: "Resume motion" }),
    ).toHaveAttribute("aria-pressed", "true");
    await page.getByRole("button", { name: "Close villager details" }).click();
    await expect(
      page.getByRole("region", { name: "Selected villager" }),
    ).toHaveCount(0);
    const separate = await page.evaluate(() => {
      const map = document.querySelector("canvas").getBoundingClientRect();
      return (
        document.querySelector(".approval-knocks").getBoundingClientRect().top >
        map.bottom
      );
    });
    expect(separate).toBe(true);
    expect(errors).toEqual([]);
  });
}

test("selection survives stream updates and removed villagers disappear", async ({
  page,
}) => {
  await village(page);
  await page.locator(".person").filter({ hasText: "Keeper" }).click();
  const next = structuredClone(fixture);
  next.snapshot.generation += 1;
  next.snapshot.villagers[0].last_line = "New activity from Chronicle";
  await page.evaluate(
    (envelope) =>
      window.villageStream.listeners.snapshot({
        data: JSON.stringify(envelope),
      }),
    next,
  );
  await expect(
    page.getByRole("region", { name: "Selected villager" }),
  ).toContainText("New activity from Chronicle");
  next.snapshot.generation += 1;
  next.snapshot.villagers = [];
  await page.evaluate(
    (envelope) =>
      window.villageStream.listeners.snapshot({
        data: JSON.stringify(envelope),
      }),
    next,
  );
  await expect(page.locator(".person")).toHaveCount(0);
  await expect(
    page.getByRole("region", { name: "Selected villager" }),
  ).toHaveCount(0);
  await expect(page.getByText(/The village is quiet/)).toBeVisible();
});

test("reduced motion starts paused", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await village(page);
  await expect(
    page.getByRole("button", { name: "Resume motion" }),
  ).toHaveAttribute("aria-pressed", "true");
});
