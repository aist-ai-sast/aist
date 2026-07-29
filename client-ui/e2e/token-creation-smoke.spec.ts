import { expect, test } from "@playwright/test";

import { loginByApi } from "./support";

test("create-token button works end to end (regression for reported 500)", async ({ page }) => {
  await loginByApi(page);
  await page.goto("/settings");
  await expect(page.locator("#tokens")).toBeVisible({ timeout: 30_000 });

  await page.getByPlaceholder(/CI pipe/i).fill(`smoke-test-token-${Date.now()}`);
  await page.getByRole("combobox").last().click();
  await page.getByRole("option", { name: "Read and write" }).click();

  const [response] = await Promise.all([
    page.waitForResponse((r) => r.url().includes("/api/v2/aist/me/tokens/") && r.request().method() === "POST"),
    page.getByRole("button", { name: "Create token" }).click(),
  ]);

  expect(response.status()).toBe(201);
  await expect(page.getByText("Copy your token now")).toBeVisible();
  await page.screenshot({ path: "test-results/token-created.png" });
});
