import { readFileSync } from "node:fs";
import { test, expect } from "@playwright/test";

const fixture = JSON.parse(readFileSync(new URL("../../chronicle/tests/fixtures/state-contract/complete-v1.json", import.meta.url), "utf8"));
async function open(page, hash = "") {
  await page.addInitScript(() => {
    window.EventSource = class {
      constructor() { setTimeout(() => this.onopen?.(), 0); }
      addEventListener() {}
      close() {}
    };
  });
  await page.route("**/chronicle/state", route => route.fulfill({ json: fixture }));
  await page.goto(`/${hash}`);
}

test("quiet village opens records and requests deliberately on a narrow screen", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await open(page);
  await expect(page.locator('canvas[data-renderer="three"]')).toHaveAttribute("data-ready", "true");
  await expect(page.getByRole("region", { name: "Notice board", exact: true })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Approval knocks", exact: true })).toHaveCount(0);
  await expect(page.getByText("What’s been happening")).toHaveCount(0);
  await page.getByRole("link", { name: "Village records", exact: true }).click();
  await expect(page).toHaveURL(/#records$/);
  await expect(page.getByRole("region", { name: "Notice board", exact: true })).toBeVisible();
  await expect(page.locator('canvas[data-renderer="three"]')).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole("link", { name: /Back to village/ }).click();
  await expect(page.locator('canvas[data-renderer="three"]')).toHaveAttribute("data-ready", "true");
  await expect(page.getByRole("region", { name: "Notice board", exact: true })).toHaveCount(0);
  await page.getByRole("link", { name: "Requests (1)", exact: true }).click();
  await expect(page).toHaveURL(/#approvals$/);
  await expect(page.getByRole("region", { name: "Approval knocks", exact: true })).toContainText("Deploy?");
  await expect(page.getByRole("button", { name: "Approve Deploy?", exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});

test("a direct approval link opens its request and browser back restores the view", async ({ page }) => {
  await open(page, "#approval-approval-1");
  await expect(page.locator("#approval-approval-1")).toBeVisible();
  await expect(page.getByRole("region", { name: "Approval knocks", exact: true })).toContainText("Deploy?");
  await expect(page.locator('canvas[data-renderer="three"]')).toHaveCount(0);
  await page.getByRole("link", { name: "Village records", exact: true }).click();
  await expect(page.getByRole("region", { name: "Notice board", exact: true })).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(/#approval-approval-1$/);
  await expect(page.locator("#approval-approval-1")).toBeVisible();
  await expect(page.getByRole("region", { name: "Notice board", exact: true })).toHaveCount(0);
});
