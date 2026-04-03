import { expect, test, type Locator, type Page } from "@playwright/test";
import { loginByApi } from "./support";

async function openSelectOption(trigger: Locator, optionName: string) {
  await trigger.click();
  await trigger.page().getByRole("option", { name: optionName, exact: true }).click();
}

async function getFirstProjectName(page: Page): Promise<string> {
  const response = await page.request.get("/api/v2/aist/projects/");
  expect(response.status(), await response.text()).toBe(200);
  const payload = await response.json() as { results?: Array<{ product_name: string }>; items?: Array<{ product_name: string }> };
  const first = (payload.results ?? payload.items ?? [])[0];
  expect(first?.product_name).toBeTruthy();
  return first.product_name;
}

test.beforeEach(async ({ page }) => {
  await loginByApi(page);
});

test("integrations page lets a maintainer manage org integrations, providers, and overrides", async ({ page }) => {
  const suffix = Date.now();
  const integrationName = `E2E GitLab ${suffix}`;
  const renamedIntegration = `${integrationName} Updated`;
  const providerName = `E2E Jira ${suffix}`;
  const projectName = await getFirstProjectName(page);
  const orgSection = page.locator("section").filter({ hasText: "Org Integrations" }).first();

  await page.goto("/integrations");
  await expect(page.getByRole("heading", { name: "Integrations" })).toBeVisible({ timeout: 30_000 });
  await expect(orgSection).toBeVisible();

  await orgSection.getByRole("button", { name: "Add" }).click();
  await expect(orgSection.getByText("New Integration")).toBeVisible();
  await orgSection.getByPlaceholder("e.g. Production").fill(integrationName);
  await orgSection.getByPlaceholder("https://gitlab.com").fill(`https://gitlab-${suffix}.example.com`);
  await orgSection.getByRole("button", { name: "Create" }).click();

  await expect(page.getByText("Integration created.")).toBeVisible();
  const integrationRow = orgSection.locator("div").filter({ hasText: integrationName }).first();
  await expect(integrationRow).toContainText("default");

  await integrationRow.getByRole("button", { name: "Edit" }).click();
  await expect(orgSection.getByText("Edit Integration")).toBeVisible();
  await orgSection.getByPlaceholder("e.g. Production").fill(renamedIntegration);
  await orgSection.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByText("Integration updated.")).toBeVisible();
  await expect(orgSection).toContainText(renamedIntegration);

  const providerSection = page.locator("section").filter({ hasText: "Work Item Providers" }).first();
  await providerSection.getByRole("button", { name: "Add" }).click();
  await providerSection.getByPlaceholder("e.g. Production").fill(providerName);
  await providerSection.getByPlaceholder("https://company.atlassian.net").fill(`https://jira-${suffix}.example.com`);
  await providerSection.getByRole("button", { name: "Create" }).click();
  await expect(page.getByText("Provider created.")).toBeVisible();
  await expect(providerSection).toContainText(providerName);

  const overridesSection = page.locator("section").filter({ hasText: "Per-Project Overrides" }).first();
  await openSelectOption(overridesSection.getByRole("combobox").first(), projectName);
  await expect(overridesSection.getByText("GitLab", { exact: true })).toBeVisible();
  await openSelectOption(overridesSection.getByRole("combobox").nth(1), renamedIntegration);
  await expect(page.getByText("Override saved.")).toBeVisible();

  await overridesSection.getByTitle("Clear override").click();
  await expect(page.getByText("Override cleared.")).toBeVisible();
});

test("maintainer can create a VPN integration and link it to a work item provider", async ({ page }) => {
  const suffix = Date.now();
  const vpnName = `E2E VPN ${suffix}`;
  const providerName = `E2E Jira VPN ${suffix}`;

  await page.goto("/integrations");
  await expect(page.getByRole("heading", { name: "Integrations" })).toBeVisible({ timeout: 30_000 });

  const orgSection = page.locator("section").filter({ hasText: "Org Integrations" }).first();

  // --- Create VPN integration ---
  await orgSection.getByRole("button", { name: "Add" }).click();
  await expect(orgSection.getByText("New Integration")).toBeVisible();

  // Select VPN type
  await openSelectOption(orgSection.locator("[role='combobox']").first(), "VPN");

  await orgSection.getByPlaceholder("e.g. Production").fill(vpnName);

  // Fill ovpn_content: toggle show then type into textarea
  const ovpnSection = orgSection.locator("label").filter({ hasText: ".ovpn Config" });
  await ovpnSection.getByRole("button", { name: "Show" }).click();
  await ovpnSection.locator("textarea").fill("client\nremote 10.0.0.1 1194\ndev tun\n");

  await orgSection.getByRole("button", { name: "Create" }).click();
  await expect(page.getByText("Integration created.")).toBeVisible();

  const vpnRow = orgSection.locator("div").filter({ hasText: vpnName }).first();
  await expect(vpnRow).toBeVisible();
  // VPN badge must be rendered
  await expect(vpnRow.locator("span").filter({ hasText: "VPN" }).first()).toBeVisible();
  // Validate button present for VPN integration
  await expect(vpnRow.getByRole("button", { name: "Validate" })).toBeVisible();

  // --- Link VPN to a new Work Item Provider ---
  const providerSection = page.locator("section").filter({ hasText: "Work Item Providers" }).first();
  await providerSection.getByRole("button", { name: "Add" }).click();
  await providerSection.getByPlaceholder("e.g. Production").fill(providerName);
  await providerSection
    .getByPlaceholder("https://company.atlassian.net")
    .fill(`https://jira-${suffix}.example.com`);

  // Select VPN integration in the provider form
  const vpnSelect = providerSection.locator("label").filter({ hasText: "VPN Integration" });
  await openSelectOption(vpnSelect.locator("[role='combobox']"), vpnName);

  await providerSection.getByRole("button", { name: "Create" }).click();
  await expect(page.getByText("Provider created.")).toBeVisible();

  // --- Edit provider — verify VPN selection is preserved ---
  const providerRow = providerSection.locator("div").filter({ hasText: providerName }).first();
  await providerRow.getByRole("button", { name: "Edit" }).click();
  await expect(
    providerSection.locator("label").filter({ hasText: "VPN Integration" }).locator("[role='combobox']"),
  ).toContainText(vpnName);
  await providerSection.getByRole("button", { name: "Cancel" }).click();

  // --- VPN type must NOT appear in per-project overrides dropdown ---
  const overridesSection = page.locator("section").filter({ hasText: "Per-Project Overrides" }).first();
  await overridesSection.locator("[role='combobox']").first().click();
  // The override type rows are rendered per-type; VPN row must not exist
  await expect(page.getByRole("option", { name: "VPN", exact: true })).not.toBeVisible();
  await page.keyboard.press("Escape");

  // --- Cleanup ---
  await providerRow.getByRole("button", { name: "Delete" }).click();
  await vpnRow.getByRole("button", { name: "Delete" }).click();
});
