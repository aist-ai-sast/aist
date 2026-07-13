import { expect, test, type Page } from "@playwright/test";
import { loginByApi } from "./support";

// Exercises the AIST-native finding endpoints (AISTFindingDetailAPI GET/PATCH,
// AISTFindingCloseAPI POST) by driving the same buttons a real user clicks,
// not just by asserting the detail page renders.

type FindingApi = { id: number; severity: string; active: boolean; is_mitigated?: boolean };

async function getFirstActiveFinding(page: Page): Promise<FindingApi> {
  const response = await page.request.get("/api/v2/aist/findings/?limit=1&active=true");
  expect(response.status(), await response.text()).toBe(200);
  const payload = await response.json() as { results?: FindingApi[]; items?: FindingApi[] };
  const first = (payload.results ?? payload.items ?? [])[0];
  expect(first?.id).toBeTruthy();
  return first;
}

async function fetchFinding(page: Page, id: number): Promise<FindingApi> {
  const response = await page.request.get(`/api/v2/aist/findings/${id}/`);
  expect(response.status(), await response.text()).toBe(200);
  return response.json();
}

test.beforeEach(async ({ page }) => {
  await loginByApi(page);
});

test("changing severity via the UI persists through the AIST finding-detail endpoint", async ({ page }) => {
  const finding = await getFirstActiveFinding(page);
  const originalSeverity = finding.severity;
  const nextSeverity = originalSeverity === "Critical" ? "High" : "Critical";

  await page.goto(`/findings/${finding.id}`);
  await expect(page.getByText("Finding Detail")).toBeVisible({ timeout: 30_000 });

  // The severity Select.Trigger has role="combobox" but no associated
  // accessible label — scope by its current displayed value instead.
  const severityTrigger = page.getByRole("combobox").filter({ hasText: originalSeverity });
  await expect(severityTrigger).toBeVisible();
  await severityTrigger.click();
  await page.getByRole("option", { name: nextSeverity, exact: true }).click();

  await expect(page.getByText("Severity updated.")).toBeVisible({ timeout: 30_000 });
  expect((await fetchFinding(page, finding.id)).severity).toBe(nextSeverity);

  // Restore original severity so shared seed data is unaffected for other tests.
  const changedTrigger = page.getByRole("combobox").filter({ hasText: nextSeverity });
  await changedTrigger.click();
  await page.getByRole("option", { name: originalSeverity, exact: true }).click();
  await expect(page.getByText("Severity updated.")).toBeVisible({ timeout: 30_000 });
  expect((await fetchFinding(page, finding.id)).severity).toBe(originalSeverity);
});

test("closing a finding via Apply Close persists through the AIST finding-close endpoint", async ({ page }) => {
  const finding = await getFirstActiveFinding(page);

  await page.goto(`/findings/${finding.id}`);
  await expect(page.getByText("Finding Detail")).toBeVisible({ timeout: 30_000 });

  const applyCloseButton = page.getByRole("button", { name: "Apply Close" });
  await expect(applyCloseButton).toBeVisible();
  await applyCloseButton.click();

  await expect(page.getByText("Finding closed.")).toBeVisible({ timeout: 30_000 });
  const reopenButton = page.getByRole("button", { name: "Reopen" });
  await expect(reopenButton).toBeVisible({ timeout: 30_000 });

  const closed = await fetchFinding(page, finding.id);
  expect(closed.active).toBe(false);
  expect(closed.is_mitigated).toBe(true);

  // Reopen so shared seed data is unaffected for other tests.
  await reopenButton.click();
  await expect(page.getByRole("button", { name: "Apply Close" })).toBeVisible({ timeout: 30_000 });
  expect((await fetchFinding(page, finding.id)).active).toBe(true);
});
