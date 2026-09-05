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
for (const viewport of [
  { width: 1440, height: 1000 },
  { width: 390, height: 844 },
]) {
  test(`agent profile is centered, readable and keyboard-contained at ${viewport.width}px`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    await page.addInitScript(() => {
      window.EventSource = class {
        addEventListener() {}
        close() {}
      };
    });
    await page.route("**/chronicle/state", (route) =>
      route.fulfill({ json: fixture }),
    );
    await page.goto("/");
    const opener = page.locator(".ve-person").first();
    await opener.click();
    const profile = page.getByRole("dialog", { name: "Keeper" });
    await expect(profile).toBeVisible();
    const box = await profile.boundingBox();
    expect(box.width).toBeGreaterThan(viewport.width < 650 ? 350 : 900);
    expect(Math.abs(box.x - (viewport.width - box.width) / 2)).toBeLessThan(2);
    expect(box.y).toBeGreaterThanOrEqual(9);
    expect(box.y + box.height).toBeLessThanOrEqual(viewport.height - 9);
    const face = await profile.locator(".ve-portrait-face").boundingBox();
    expect(face.width).toBeGreaterThan(viewport.width < 650 ? 25 : 35);
    for (let i = 0; i < 12; i++) {
      await page.keyboard.press("Tab");
      expect(
        await profile.evaluate((dialog) =>
          dialog.contains(document.activeElement),
        ),
      ).toBe(true);
    }
    await profile.locator(".agent-profile-content").evaluate((element) => {
      element.scrollTop = element.scrollHeight;
    });
    await expect(
      profile.getByRole("button", { name: "Close villager details" }),
    ).toBeInViewport();
    await page.keyboard.press("Escape");
    await expect(profile).not.toBeVisible();
    await expect(opener).toBeFocused();
    expect(await page.evaluate(() => document.body.style.overflow)).toBe("");
  });
}
