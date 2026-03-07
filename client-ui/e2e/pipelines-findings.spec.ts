import { expect, test } from "@playwright/test";
import { loginByApi, openPipelines } from "./support";

test.beforeEach(async ({ page }) => {
  await loginByApi(page);
});

test("pipeline detail opens findings with project and pipeline filters", async ({ page }) => {
  await openPipelines(page);

  const firstCard = page.locator("article[role='button']").first();
  await firstCard.click();

  await firstCard.getByRole("link", { name: "Open Findings" }).first().click();
  await expect(page).toHaveURL(/\/findings\?/);
  await expect(page).toHaveURL(/project_id=/);
  await expect(page).toHaveURL(/pipeline_id=/);
  await expect(page.getByText(/Findings · Total/i)).toBeVisible();
});

test("product click on pipeline card opens product findings", async ({ page }) => {
  await openPipelines(page);

  const firstCard = page.locator("article[role='button']").first();
  const productButton = firstCard.getByRole("button").first();
  await productButton.click();

  await expect(page).toHaveURL(/\/findings\?/);
  await expect(page).toHaveURL(/project_id=/);
  await expect(page.getByText(/Findings · Total/i)).toBeVisible();
});
