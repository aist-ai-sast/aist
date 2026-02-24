import { expect, test } from "@playwright/test";

test("calendar opens and event detail panel is visible", async ({ page }) => {
  await page.goto("/calendar");
  await expect(page.getByRole("heading", { name: "Calendar" })).toBeVisible();
  await expect(page.getByText("Event details")).toBeVisible();
  await expect(page.getByText("Pipeline finished")).toBeVisible();
});

test("calendar keeps url state for filters", async ({ page }) => {
  await page.goto("/calendar");
  await expect(page).toHaveURL(/view=/);
  await expect(page).toHaveURL(/types=/);
});

test("event hover card appears", async ({ page }) => {
  await page.goto("/calendar");
  const events = page.locator(".fc-event");
  if (await events.count() === 0) {
    test.skip(true, "No events available in current environment");
  }
  const event = events.first();
  await event.hover();
  await expect(page.locator(".aist-calendar-hover-card")).toBeVisible();
});

test("event click opens side panel and action link navigates with query", async ({ page }) => {
  await page.goto("/calendar");
  const events = page.locator(".fc-event");
  if (await events.count() === 0) {
    test.skip(true, "No events available in current environment");
  }

  await events.first().click();
  const action = page
    .getByRole("button", { name: /Open (findings|pipelines).*date/i })
    .first();
  if (await action.count() === 0) {
    test.skip(true, "No date-based action available for selected event");
  }

  await action.click();
  await expect(page).toHaveURL(/created_(from|gte)=/);
});

test("keyboard esc closes selected event panel", async ({ page }) => {
  await page.goto("/calendar");
  const events = page.locator(".fc-event");
  if (await events.count() === 0) {
    test.skip(true, "No events available in current environment");
  }

  await events.first().click();
  await expect(page).toHaveURL(/event=/);
  await page.keyboard.press("Escape");
  await expect(page).not.toHaveURL(/event=/);
});
