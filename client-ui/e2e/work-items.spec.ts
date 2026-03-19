import { expect, test, type Locator, type Page } from "@playwright/test";
import { loginByApi } from "./support";

async function getFirstFindingId(page: Page): Promise<number> {
  const response = await page.request.get("/api/v2/aist/findings/?limit=1");
  expect(response.status(), await response.text()).toBe(200);
  const payload = await response.json() as { results?: Array<{ id: number }>; items?: Array<{ id: number }> };
  const first = (payload.results ?? payload.items ?? [])[0];
  expect(first?.id).toBeTruthy();
  return first.id;
}

async function openWorkItemsTab(page: Page, findingId: number) {
  await page.goto(`/findings/${findingId}`);
  await expect(page.getByText("Finding Detail")).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: "Work Items" }).click();
}

async function workItemRow(page: Page, key: string): Promise<Locator> {
  return page.locator("li").filter({ hasText: key }).first();
}

test.beforeEach(async ({ page }) => {
  await loginByApi(page);
});

test("user can add, update, and remove a manual work item from finding detail", async ({ page }) => {
  const findingId = await getFirstFindingId(page);
  const suffix = Date.now();
  const issueKey = `E2E-${suffix}`;
  const issueUrl = `https://example.com/issues/${suffix}`;
  const issueTitle = `E2E linked issue ${suffix}`;

  await openWorkItemsTab(page, findingId);

  await page.getByRole("button", { name: "+ Link issue" }).click();
  await page.getByPlaceholder("Issue URL (required)").fill(issueUrl);
  await page.getByPlaceholder("Key (e.g. PROJ-42)").fill(issueKey);
  await page.getByPlaceholder("Title (optional)").fill(issueTitle);
  await page.getByRole("button", { name: "Add" }).click();

  await expect(page.getByText("Work item linked.")).toBeVisible();
  const row = await workItemRow(page, issueKey);
  await expect(row).toContainText(issueTitle);
  await expect(row.getByRole("link", { name: issueUrl })).toBeVisible();

  await row.getByText("Unknown").click();
  await page.getByText("Done").last().click();
  await expect(row).toContainText("Done");

  await row.getByRole("button", { name: "Remove work item" }).click();
  await expect(page.getByText("Work item removed.")).toBeVisible();
  await expect(row).toHaveCount(0);
});
