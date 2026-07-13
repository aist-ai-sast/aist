import { expect, test } from "@playwright/test";

import { loginByApi } from "./support";

test("settings page cards align in consistent row heights", async ({ page }) => {
  await loginByApi(page);
  await page.goto("/settings");
  await expect(page.locator("#account")).toBeVisible({ timeout: 30_000 });

  const accountCard = page.locator("#account .aist-card");
  const securityCard = page.locator("#security .aist-card");
  const accessCard = page.locator("#access .aist-card");
  const tokensCard = page.locator("#tokens .aist-card");
  await expect(accountCard).toBeVisible();
  await expect(securityCard).toBeVisible();
  await expect(accessCard).toBeVisible();
  await expect(tokensCard).toBeVisible();

  const [accountBox, securityBox, accessBox, tokensBox] = await Promise.all([
    accountCard.boundingBox(),
    securityCard.boundingBox(),
    accessCard.boundingBox(),
    tokensCard.boundingBox(),
  ]);

  // Cards sharing a grid row must share the same height now that the grid
  // uses items-stretch and each Card fills its wrapping <section> — before
  // this fix, Access and API Tokens were bare grid children with no shared
  // height rule and visibly mismatched the row above.
  expect(Math.abs(accountBox!.height - securityBox!.height)).toBeLessThanOrEqual(1);
  expect(Math.abs(accessBox!.height - tokensBox!.height)).toBeLessThanOrEqual(1);

  await page.screenshot({ path: "test-results/settings-grid.png", fullPage: true });
});
