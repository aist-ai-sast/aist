import { expect, test, type Page } from "@playwright/test";
import { loginByApi } from "./support";

test.beforeEach(async ({ page }) => {
  await loginByApi(page);
});

type FindingsPage = { count: number; results?: Array<{ severity: string }> };

async function findingsCount(page: Page, query: string): Promise<FindingsPage> {
  const response = await page.request.get(`/api/v2/aist/findings/?limit=1${query ? `&${query}` : ""}`);
  expect(response.status(), await response.text()).toBe(200);
  return response.json() as Promise<FindingsPage>;
}

function kpi(page: Page, label: string) {
  return page.getByText(label, { exact: true }).locator("..");
}

async function openDashboard(page: Page, search = "") {
  await page.goto(`/dashboard${search}`);
  await expect(page.getByRole("heading", { name: "Security Dashboard" })).toBeVisible({ timeout: 30_000 });
}

test("dashboard severity filter matches the Findings list and survives reload", async ({ page }) => {
  const first = await findingsCount(page, "");
  const severity = first.results?.[0]?.severity;
  test.skip(!severity, "no findings seeded");
  const query = `severity=${severity}`;

  await openDashboard(page);
  const filtersButton = page.getByRole("button", { name: /^Filters/ });
  if ((await filtersButton.getAttribute("aria-pressed")) !== "true") {
    await filtersButton.click();
  }
  await page.getByRole("complementary").getByRole("button", { name: severity as string, exact: true }).click();
  await expect(page).toHaveURL(new RegExp(query));

  // Other specs edit shared seed findings in parallel (finding-actions temporarily changes the
  // severity of the first finding), so the shown KPI is compared with a fresh Findings count and
  // the page reloaded while that edit is in flight.
  await expect.poll(async () => {
    await page.reload();
    await page.waitForLoadState("networkidle");
    const shown = Number((await kpi(page, "Total Findings").innerText()).replace(/\D/g, ""));
    const listed = (await findingsCount(page, query)).count;
    return shown === listed ? "match" : `dashboard ${shown} vs findings ${listed}`;
  }, { timeout: 30_000 }).toBe("match");

  await expect(page).toHaveURL(new RegExp(query));
  await expect(page.getByRole("button", { name: /^Filters/ })).toHaveAttribute("aria-pressed", "true");

  await page.getByRole("link", { name: /Open in Findings/ }).click();
  await expect(page).toHaveURL(new RegExp(`/findings\\?.*${query}`));
  await expect(page.getByText(/Findings · Total \d+/).first()).toBeVisible({ timeout: 30_000 });
});

test("collapsed dashboard filters show as chips that remove themselves", async ({ page }) => {
  await openDashboard(page, "?severity=High");

  await page.getByRole("button", { name: /^Filters/ }).click();
  const bar = page.getByLabel("Active filters");
  await expect(bar).toBeVisible();

  await bar.getByRole("button", { name: "Remove filter Severity High" }).click();
  await expect(page).not.toHaveURL(/severity=/);
});

test("non-active status marks active-only charts as not applicable", async ({ page }) => {
  await openDashboard(page, "?active=false");

  // Work Item Coverage stays on screen even when no non-active finding exists in the seed.
  await expect(page.getByText("Not applicable to the current filters").first()).toBeVisible({ timeout: 30_000 });

  await page.getByRole("button", { name: "Reset Status" }).first().click();
  await expect(page).not.toHaveURL(/active=false/);
  await expect(page.getByText("Not applicable to the current filters")).toHaveCount(0);
});

test("pipeline performance is labelled as outside the finding filter", async ({ page }) => {
  await openDashboard(page);

  await expect(page.getByText("Project filter only")).toBeVisible({ timeout: 30_000 });
});
