import { expect, test, type Locator, type Page } from "@playwright/test";
import { loginByApi } from "./support";

async function openSelectOption(trigger: Locator, optionName: string) {
  await trigger.click();
  await trigger.page().getByRole("option", { name: optionName, exact: true }).click();
}

async function selectFirstOption(trigger: Locator) {
  await trigger.click();
  await trigger.page().getByRole("option").first().click();
}

function acceptNextDialog(page: Page) {
  page.once("dialog", (dialog) => dialog.accept());
}

test.beforeEach(async ({ page }) => {
  await loginByApi(page);
});

test("integrations page lets a maintainer manage org integrations, providers, and overrides", async ({ page }) => {
  const suffix = Date.now();
  const integrationName = `E2E GitLab ${suffix}`;
  const renamedIntegration = `${integrationName} Updated`;
  const providerName = `E2E Jira ${suffix}`;
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
  await expect(orgSection).toContainText(integrationName);
  await expect(orgSection).toContainText("default");

  await orgSection.getByRole("button", { name: "Edit" }).first().click();
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
  await selectFirstOption(overridesSection.getByRole("combobox").first());
  await expect(overridesSection.getByText("GitLab", { exact: true })).toBeVisible();
  await openSelectOption(overridesSection.getByRole("combobox").nth(1), renamedIntegration);
  await expect(page.getByText("Override saved.")).toBeVisible();

  await overridesSection.getByTitle("Clear override").click();
  await expect(page.getByText("Override cleared.")).toBeVisible();

  acceptNextDialog(page);
  await providerSection.getByRole("button", { name: "Delete" }).first().click();
  await expect(page.getByText("Provider deleted.")).toBeVisible();
  acceptNextDialog(page);
  await orgSection.getByRole("button", { name: "Delete" }).first().click();
  await expect(page.getByText("Integration deleted.")).toBeVisible();
});

test("maintainer can create a Gerrit integration with base URL and username", async ({ page }) => {
  const suffix = Date.now();
  const gerritName = `E2E Gerrit ${suffix}`;

  await page.goto("/integrations");
  await expect(page.getByRole("heading", { name: "Integrations" })).toBeVisible({ timeout: 30_000 });

  const orgSection = page.locator("section").filter({ hasText: "Org Integrations" }).first();

  await orgSection.getByRole("button", { name: "Add" }).click();
  await expect(orgSection.getByText("New Integration")).toBeVisible();

  // Select Gerrit type
  await openSelectOption(orgSection.locator("[role='combobox']").first(), "Gerrit");

  await orgSection.getByPlaceholder("e.g. Production").fill(gerritName);
  await orgSection.getByPlaceholder("https://gerrit.example.com").fill(`https://gerrit-${suffix}.example.com`);
  await orgSection.getByPlaceholder("Gerrit HTTP account username").fill("svc-user");

  // Gerrit must offer VPN routing, same as GitLab — servers reachable only
  // over VPN would otherwise time out with no way to route through it.
  await expect(orgSection.getByText("VPN Integration", { exact: true })).toBeVisible();

  await orgSection.getByRole("button", { name: "Create" }).click();
  await expect(page.getByText("Integration created.")).toBeVisible();

  await expect(orgSection).toContainText(gerritName);
  // Gerrit badge must be rendered
  await expect(orgSection.getByText("Gerrit", { exact: true }).first()).toBeVisible();

  // Cleanup
  acceptNextDialog(page);
  await orgSection.getByRole("button", { name: "Delete" }).first().click();
  await expect(page.getByText("Integration deleted.")).toBeVisible();
});

test("maintainer can create a Gitea integration with base URL", async ({ page }) => {
  const suffix = Date.now();
  const giteaName = `E2E Gitea ${suffix}`;

  await page.goto("/integrations");
  await expect(page.getByRole("heading", { name: "Integrations" })).toBeVisible({ timeout: 30_000 });

  const orgSection = page.locator("section").filter({ hasText: "Org Integrations" }).first();

  await orgSection.getByRole("button", { name: "Add" }).click();
  await expect(orgSection.getByText("New Integration")).toBeVisible();

  // Select Gitea type
  await openSelectOption(orgSection.locator("[role='combobox']").first(), "Gitea");

  await orgSection.getByPlaceholder("e.g. Production").fill(giteaName);
  await orgSection.getByPlaceholder("https://gitea.example.com").fill(`https://gitea-${suffix}.example.com`);

  // Gitea must offer VPN routing too — same self-hosted-behind-VPN case as GitLab/Gerrit.
  await expect(orgSection.getByText("VPN Integration", { exact: true })).toBeVisible();

  await orgSection.getByRole("button", { name: "Create" }).click();
  await expect(page.getByText("Integration created.")).toBeVisible();

  await expect(orgSection).toContainText(giteaName);
  // Gitea badge must be rendered
  await expect(orgSection.getByText("Gitea", { exact: true }).first()).toBeVisible();

  // Cleanup
  acceptNextDialog(page);
  await orgSection.getByRole("button", { name: "Delete" }).first().click();
  await expect(page.getByText("Integration deleted.")).toBeVisible();
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

  await expect(orgSection).toContainText(vpnName);
  // VPN badge must be rendered
  await expect(orgSection.getByText("VPN", { exact: true }).first()).toBeVisible();
  // Validate button present for VPN integration
  await expect(orgSection.getByRole("button", { name: "Validate" }).first()).toBeVisible();

  // --- Link VPN to a new Work Item Provider ---
  const providerSection = page.locator("section").filter({ hasText: "Work Item Providers" }).first();
  await providerSection.getByRole("button", { name: "Add" }).click();
  await providerSection.getByPlaceholder("e.g. Production").fill(providerName);
  await providerSection
    .getByPlaceholder("https://company.atlassian.net")
    .fill(`https://jira-${suffix}.example.com`);

  // Select VPN integration in the provider form
  await openSelectOption(providerSection.getByRole("combobox").nth(1), vpnName);

  await providerSection.getByRole("button", { name: "Create" }).click();
  await expect(page.getByText("Provider created.")).toBeVisible();

  // --- Edit provider — verify VPN selection is preserved ---
  await providerSection.getByRole("button", { name: "Edit" }).first().click();
  await expect(providerSection.getByRole("combobox").nth(1)).toContainText(vpnName);
  await providerSection.getByRole("button", { name: "Cancel" }).click();

  // --- VPN type must NOT appear in per-project overrides dropdown ---
  const overridesSection = page.locator("section").filter({ hasText: "Per-Project Overrides" }).first();
  await overridesSection.locator("[role='combobox']").first().click();
  // The override type rows are rendered per-type; VPN row must not exist
  await expect(page.getByRole("option", { name: "VPN", exact: true })).not.toBeVisible();
  await page.keyboard.press("Escape");

  // --- Cleanup ---
  acceptNextDialog(page);
  await providerSection.getByRole("button", { name: "Delete" }).first().click();
  await expect(page.getByText("Provider deleted.")).toBeVisible();
  acceptNextDialog(page);
  await orgSection.getByRole("button", { name: "Delete" }).first().click();
  await expect(page.getByText("Integration deleted.")).toBeVisible();
});
