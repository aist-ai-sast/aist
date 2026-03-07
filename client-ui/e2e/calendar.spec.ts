import { expect, test } from "@playwright/test";
import { loginByApi, openCalendar } from "./support";

test.beforeEach(async ({ page }) => {
  await loginByApi(page);
  await openCalendar(page);
});

test("calendar opens and event detail panel is visible", async ({ page }) => {
  const events = page.locator(".fc-event");
  await events.first().click();
  await expect(page).toHaveURL(/event=/);
  await expect(page.getByText("Event details")).toBeVisible();
  await expect(page.locator(".aist-calendar-detail-hero")).toBeVisible();
});

test("calendar keeps url state for filters", async ({ page }) => {
  const before = page.url();
  await page.getByRole("button", { name: "Pipeline scheduled" }).click();
  await expect(page).toHaveURL(/view=/);
  await expect(page).toHaveURL(/types=/);
  await page.waitForFunction((prev) => window.location.href !== prev, before);
});

test("event hover and keyboard behavior works", async ({ page }) => {
  const event = page.locator(".fc-event").first();
  await event.hover();
  await expect(page.locator(".aist-calendar-hover-card")).toBeVisible();
  await event.click();
  await expect(page).toHaveURL(/event=/);
  await page.keyboard.press("Escape");
  await expect(page).not.toHaveURL(/event=/);
});

test("event action opens business page", async ({ page }) => {
  await page.locator(".fc-event").first().click();
  const detailsPanel = page.locator("aside").filter({ hasText: "Event details" });
  const action = detailsPanel.getByRole("button", { name: /^Open / }).first();
  await expect(action).toBeVisible();
  await action.click();
  await expect(page).toHaveURL(/\/(findings|pipelines)(\/|\?|$)/);
});
