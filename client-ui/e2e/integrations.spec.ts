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

function resourceRow(section: Locator, name: string) {
  return section
    .getByText(name, { exact: true })
    .locator("xpath=ancestor::div[.//button[normalize-space()='Edit']][1]");
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
  await openSelectOption(orgSection.getByText("Type").locator("..").getByRole("combobox"), "GitLab");
  await orgSection.getByPlaceholder("e.g. Production").fill(integrationName);
  await orgSection.getByPlaceholder("https://gitlab.com").fill(`https://gitlab-${suffix}.example.com`);
  await orgSection.getByRole("button", { name: "Create" }).click();

  await expect(page.getByText("Integration created.")).toBeVisible();
  await expect(orgSection).toContainText(integrationName);
  await expect(orgSection).toContainText("default");

  await resourceRow(orgSection, integrationName).getByRole("button", { name: "Edit" }).click();
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
  await resourceRow(providerSection, providerName).getByRole("button", { name: "Delete" }).click();
  await expect(page.getByText("Provider deleted.")).toBeVisible();
  acceptNextDialog(page);
  await resourceRow(orgSection, renamedIntegration).getByRole("button", { name: "Delete" }).click();
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
  await resourceRow(providerSection, providerName).getByRole("button", { name: "Edit" }).click();
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
  await resourceRow(providerSection, providerName).getByRole("button", { name: "Delete" }).click();
  await expect(page.getByText("Provider deleted.")).toBeVisible();
  acceptNextDialog(page);
  await resourceRow(orgSection, vpnName).getByRole("button", { name: "Delete" }).click();
  await expect(page.getByText("Integration deleted.")).toBeVisible();
});

test("DAST onboarding imports a strict bundle without retaining the token", async ({ page }) => {
  const suffix = Date.now();
  const integrationName = `E2E DAST ${suffix}`;
  const token = `e2e-public.${suffix}.one-time-token`;
  const bundle = {
    bundle_version: 1,
    gateway_url: `https://dast-${suffix}.example.com`,
    ca_bundle: "",
    contract_major: 2,
    integrator_public_id: `e2e-${suffix}`,
    server_fingerprint: `sha256:e2e-${suffix}`,
    token,
  };
  const orgSection = page.locator("section").filter({ hasText: "Org Integrations" }).first();
  let integrationId: number | undefined;

  try {
    await page.goto("/integrations");
    await expect(page.getByRole("heading", { name: "Integrations" })).toBeVisible({ timeout: 30_000 });
    await orgSection.getByRole("button", { name: "Add" }).click();
    await openSelectOption(orgSection.getByText("Type").locator("..").getByRole("combobox"), "DAST");
    await orgSection.getByPlaceholder("e.g. Production").fill(integrationName);
    await orgSection.getByRole("button", { name: "Show" }).first().click();
    await orgSection.getByPlaceholder(/bundle_version/).fill(JSON.stringify(bundle));
    await orgSection.getByRole("button", { name: "Load bundle" }).click();

    const [response] = await Promise.all([
      page.waitForResponse((item) => item.url().includes("/dast-integration/") && item.request().method() === "POST"),
      orgSection.getByRole("button", { name: "Create" }).click(),
    ]);
    expect(response.status(), await response.text()).toBe(201);
    integrationId = ((await response.json()) as { id: number }).id;

    await expect(page.getByText("DAST onboarding bundle imported.")).toBeVisible();
    await expect(orgSection).toContainText(integrationName);
    await expect(orgSection).toContainText("VALIDATING");
    await expect(orgSection).toContainText(bundle.server_fingerprint);
    const fieldValues = await page.locator("input, textarea").evaluateAll(
      (elements) => elements.map((element) => (element as HTMLInputElement).value),
    );
    expect(fieldValues).not.toContain(token);
    await expect(page.getByText(token, { exact: false })).toHaveCount(0);
  } finally {
    if (integrationId) {
      await page.request.post(`/api/v2/aist/dast-integrations/${integrationId}/disable/`);
      await page.request.delete(`/api/v2/aist/integrations/${integrationId}/`);
    }
  }
});

test("DAST binding form follows provider JSON Schema and sends the complete revision-pinned object", async ({ page }) => {
  const target = {
    id: 501,
    provider_id: "e2e-web",
    display_name: "E2E Web Target",
    contract_revision: "2.0",
    capability_revision: "cap-e2e-8",
    schema_digest: "schema-e2e-8",
    parameter_schema: {
      type: "object",
      additionalProperties: false,
      required: ["scan_mode", "label", "rate_limit", "advanced"],
      properties: {
        scan_mode: { type: "string", title: "Scan mode", enum: ["quick", "deep"] },
        label: { type: "string", title: "Run label" },
        rate_limit: { type: "number", title: "Rate limit", minimum: 1 },
        advanced: { type: "boolean", title: "Advanced" },
      },
      if: { properties: { advanced: { const: true } } },
      then: {
        required: ["note"],
        properties: { note: { type: "string", title: "Advanced note" } },
      },
    },
    provider_defaults: { scan_mode: "quick", label: "baseline", rate_limit: 2, advanced: false },
    repository_keys: ["source"],
    autonomous_ready: true,
    is_available: true,
    last_seen_at: "2026-07-25T00:00:00Z",
  };
  let savedPayload: Record<string, unknown> | undefined;
  let savedBinding: Record<string, unknown> | undefined;

  await page.route(/\/api\/v2\/aist\/organizations\/\d+\/dast-targets\//, async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([target]) });
  });
  await page.route(/\/api\/v2\/aist\/projects\/\d+\/dast-bindings\//, async (route) => {
    if (route.request().method() === "POST") {
      savedPayload = route.request().postDataJSON() as Record<string, unknown>;
      savedBinding = {
        id: 701,
        project: Number(route.request().url().match(/projects\/(\d+)/)?.[1]),
        target,
        source_repo_key: savedPayload.source_repo_key,
        enabled: savedPayload.enabled,
        parameter_snapshot: savedPayload.parameter_snapshot,
        autonomous_enabled: savedPayload.autonomous_enabled,
        readiness: { ready: true, issues: [], checked_at: "2026-07-25T00:00:00Z" },
        created: "2026-07-25T00:00:00Z",
        updated: "2026-07-25T00:00:00Z",
      };
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(savedBinding) });
      return;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(savedBinding ? [savedBinding] : []) });
  });

  await page.goto("/integrations");
  await expect(page.getByRole("heading", { name: "Integrations" })).toBeVisible({ timeout: 30_000 });
  const bindingSection = page.locator("section").filter({ hasText: "DAST Target Bindings" }).first();
  await selectFirstOption(bindingSection.getByRole("combobox").first());
  await bindingSection.getByRole("button", { name: "Add" }).click();

  await bindingSection.getByLabel("Scan mode").selectOption("deep");
  await bindingSection.getByLabel("Run label").fill("release candidate");
  await bindingSection.getByLabel("Rate limit").fill("4");
  await bindingSection.getByLabel("Advanced").check();
  await expect(bindingSection.getByLabel("Advanced note")).toBeVisible();
  await bindingSection.getByLabel("Advanced note").fill("authenticated routes");
  await bindingSection.getByRole("button", { name: "Save binding" }).click();

  await expect(page.getByText("DAST binding created.")).toBeVisible();
  expect(savedPayload).toEqual({
    target_id: 501,
    capability_revision: "cap-e2e-8",
    schema_digest: "schema-e2e-8",
    source_repo_key: "source",
    enabled: true,
    parameter_snapshot: {
      scan_mode: "deep",
      label: "release candidate",
      rate_limit: 4,
      advanced: true,
      note: "authenticated routes",
    },
    autonomous_enabled: false,
  });
  await expect(bindingSection.getByText("E2E Web Target")).toBeVisible();
});
