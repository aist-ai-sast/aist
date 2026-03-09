import { expect, test, type Page } from "@playwright/test";
import { loginByApi, openFindings } from "./support";

test.beforeEach(async ({ page }) => {
  await loginByApi(page);
});

async function getFirstFindingId(page: Page): Promise<number> {
  const response = await page.request.get("/api/v2/aist/findings/?limit=1");
  expect(response.status(), await response.text()).toBe(200);
  const payload = await response.json() as { results?: Array<{ id: number }>; items?: Array<{ id: number }> };
  const first = (payload.results ?? payload.items ?? [])[0];
  expect(first?.id).toBeTruthy();
  return first.id;
}

test("clicking file in card applies file filter and clear all resets it", async ({ page }) => {
  await openFindings(page);

  const firstCard = page.locator("article[role='button']").first();
  const fileButton = firstCard.getByRole("button", { name: /^File:/ });
  await fileButton.click();

  await expect(page).toHaveURL(/file=/);
  await page.getByRole("button", { name: "Clear all" }).click();
  await expect(page).not.toHaveURL(/file=/);
});

test("clicking project in card applies project filter", async ({ page }) => {
  await openFindings(page);

  const firstCard = page.locator("article[role='button']").first();
  await firstCard.getByRole("button", { name: /^Project:/ }).click();

  await expect(page).toHaveURL(/project_id=/);
  await expect(page.getByText(/Findings · Total/i).first()).toBeVisible();
});

test("clear all resets prefilled findings filters in URL", async ({ page }) => {
  await openFindings(page, "?project_id=1&active=false", { requireCards: false });
  await expect(page).toHaveURL(/project_id=1/);
  await expect(page).toHaveURL(/active=false/);

  await page.getByRole("button", { name: "Clear all" }).click();
  await expect(page).not.toHaveURL(/project_id=/);
  await expect(page).not.toHaveURL(/active=false/);
});

test("bulk edit mode supports selecting and deselecting visible findings", async ({ page }) => {
  await openFindings(page);

  await page.getByRole("button", { name: "Start Bulk Edit" }).click();
  await expect(page.getByText(/^Selected:\s*0$/)).toBeVisible();

  await page.getByRole("button", { name: /Select Visible \(/ }).click();
  await expect(page.getByText(/^Selected:\s*[1-9]\d*$/)).toBeVisible();

  await page.getByRole("button", { name: /Deselect Visible \(/ }).click();
  await expect(page.getByText(/^Selected:\s*0$/)).toBeVisible();
});

test("bulk close requires reason before apply", async ({ page }) => {
  await openFindings(page);

  await page.getByRole("button", { name: "Start Bulk Edit" }).click();
  await page.getByRole("button", { name: /Select Visible \(/ }).click();

  const applyButton = page.getByRole("button", { name: /Apply to \d+/ });
  await expect(applyButton).toBeDisabled();

  await page.getByPlaceholder("Enter reason for audit log").fill("E2E bulk close reason");
  await expect(applyButton).toBeEnabled();
});

test("severity chip updates URL and toggles off", async ({ page }) => {
  await openFindings(page);

  const highChip = page.getByRole("button", { name: "High" }).first();
  await highChip.click();
  await expect(page).toHaveURL(/severity=High/);

  await highChip.click();
  await expect(page).not.toHaveURL(/severity=/);
});

test("finding detail deep-link exposes triage controls", async ({ page }) => {
  const findingId = await getFirstFindingId(page);
  await page.goto(`/findings/${findingId}`);

  await expect(page).toHaveURL(new RegExp(`/findings/${findingId}$`));
  await expect(page.getByText("Finding Detail")).toBeVisible();
  await expect(page.getByRole("button", { name: "Apply Close" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Risk Approval" })).toBeVisible();
});

test("empty findings state offers clear filters recovery", async ({ page }) => {
  await openFindings(page, "?file=__e2e_nonexistent_path__", { requireCards: false });
  await expect(page.getByText("No findings match the current filters")).toBeVisible();
  await page.getByRole("button", { name: "Clear filters" }).click();
  await expect(page).not.toHaveURL(/file=/);
  await expect(page.getByText(/Findings · Total/i).first()).toBeVisible();
});
