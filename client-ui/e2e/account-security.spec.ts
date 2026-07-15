import { expect, test, type Page } from "@playwright/test";

import { credentialsFor, loginThroughUi } from "./support";

const TOKENS_URL = "/api/v2/aist/me/tokens/";

function tokenName(role: string) {
  return `e2e-${role}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

async function openSettings(page: Page, role: "org_reader" | "org_writer") {
  await loginThroughUi(page, credentialsFor(role));
  await page.goto("/settings");
  await expect(page.locator("#tokens")).toBeVisible({ timeout: 30_000 });
}

async function submitPasswordChange(page: Page, currentPassword: string, newPassword: string, confirmation: string) {
  await page.getByLabel("Current password", { exact: true }).fill(currentPassword);
  await page.getByLabel("New password", { exact: true }).fill(newPassword);
  await page.getByLabel("Confirm new password", { exact: true }).fill(confirmation);
  await page.getByRole("button", { name: "Change password" }).click();
}

test("reader is clearly restricted to read-only tokens and can revoke their own token", async ({ page }) => {
  await openSettings(page, "org_reader");

  const tokenSection = page.locator("#tokens");
  await expect(tokenSection.getByText("You have read-only access everywhere, so only read-only tokens are available.")).toBeVisible();

  const scope = tokenSection.getByRole("combobox", { name: "Scope" });
  await scope.click();
  await expect(page.getByRole("option", { name: "Read and write" })).toHaveAttribute("aria-disabled", "true");
  await page.keyboard.press("Escape");

  const name = tokenName("reader");
  await tokenSection.getByPlaceholder("e.g. CI pipeline").fill(name);
  const [createResponse] = await Promise.all([
    page.waitForResponse((response) => response.url().includes(TOKENS_URL) && response.request().method() === "POST"),
    tokenSection.getByRole("button", { name: "Create token" }).click(),
  ]);
  expect(createResponse.status()).toBe(201);
  await expect(tokenSection.getByText("Copy your token now")).toBeVisible();

  // A newly-issued secret must not be exposed until the user explicitly asks to reveal it.
  await expect(tokenSection.locator("code")).toHaveText(/^[•]+$/);
  await tokenSection.getByRole("button", { name: "Done" }).click();

  const tokenRow = tokenSection.getByText(name, { exact: true }).locator("xpath=ancestor::div[.//button[normalize-space()='Revoke']][1]");
  const [revokeResponse] = await Promise.all([
    page.waitForResponse((response) => response.url().includes(TOKENS_URL) && response.request().method() === "DELETE"),
    tokenRow.getByRole("button", { name: "Revoke" }).click(),
  ]);
  expect(revokeResponse.status()).toBe(204);
  await expect(page.getByText("Token revoked.")).toBeVisible();
  await expect(tokenSection.getByText(name)).toHaveCount(0);
});

test("writer can create and revoke a read/write token without exposing its secret by default", async ({ page }) => {
  await openSettings(page, "org_writer");

  const tokenSection = page.locator("#tokens");
  await expect(tokenSection.getByText(/only read-only tokens are available/)).toHaveCount(0);

  await tokenSection.getByRole("combobox", { name: "Scope" }).click();
  await page.getByRole("option", { name: "Read and write" }).click();
  const name = tokenName("writer");
  await tokenSection.getByPlaceholder("e.g. CI pipeline").fill(name);

  const [createResponse] = await Promise.all([
    page.waitForResponse((response) => response.url().includes(TOKENS_URL) && response.request().method() === "POST"),
    tokenSection.getByRole("button", { name: "Create token" }).click(),
  ]);
  expect(createResponse.status()).toBe(201);
  await expect(tokenSection.locator("code")).toHaveText(/^[•]+$/);
  await tokenSection.getByRole("button", { name: "Done" }).click();

  const tokenRow = tokenSection.getByText(name, { exact: true }).locator("xpath=ancestor::div[.//button[normalize-space()='Revoke']][1]");
  await expect(tokenRow).toContainText("Read / write");
  const [revokeResponse] = await Promise.all([
    page.waitForResponse((response) => response.url().includes(TOKENS_URL) && response.request().method() === "DELETE"),
    tokenRow.getByRole("button", { name: "Revoke" }).click(),
  ]);
  expect(revokeResponse.status()).toBe(204);
  await expect(tokenSection.getByText(name)).toHaveCount(0);
});

test.describe("password-change validation", () => {
  test.beforeEach(async ({ page }) => {
    await openSettings(page, "org_reader");
  });

  test("wrong current password produces a clear warning and does not change credentials", async ({ page }) => {
    const [response] = await Promise.all([
      page.waitForResponse((item) => item.url().includes("/api/v2/aist/me/change-password/") && item.request().method() === "POST"),
      submitPasswordChange(page, "incorrect-password", "StrongPassword123!", "StrongPassword123!"),
    ]);
    expect(response.status()).toBe(400);
    await expect(page.getByText(/old password was entered incorrectly/i)).toBeVisible();
  });

  test("mismatched passwords produce a clear warning before credentials change", async ({ page }) => {
    const [response] = await Promise.all([
      page.waitForResponse((item) => item.url().includes("/api/v2/aist/me/change-password/") && item.request().method() === "POST"),
      submitPasswordChange(
        page,
        credentialsFor("org_reader").password,
        "StrongPassword123!",
        "DifferentPassword123!",
      ),
    ]);
    expect(response.status()).toBe(400);
    await expect(page.getByText("New passwords do not match.")).toBeVisible();
  });

  test("weak passwords produce the password-policy warning", async ({ page }) => {
    const [response] = await Promise.all([
      page.waitForResponse((item) => item.url().includes("/api/v2/aist/me/change-password/") && item.request().method() === "POST"),
      submitPasswordChange(page, credentialsFor("org_reader").password, "123", "123"),
    ]);
    expect(response.status()).toBe(400);
    await expect(page.getByText(/password is too short/i)).toBeVisible();
  });
});

test("organization owner receives a clear confirmation when resetting a member password", async ({ page }) => {
  await loginThroughUi(page, credentialsFor("org_owner"));
  await page.goto("/users");
  await expect(page.getByRole("heading", { name: "Users" })).toBeVisible({ timeout: 30_000 });

  const readerRow = page
    .getByText("org_reader@example.local", { exact: true })
    .locator("xpath=ancestor::div[.//button[normalize-space()='Reset password']][1]");
  await expect(readerRow).toBeVisible();

  const [response] = await Promise.all([
    page.waitForResponse((item) => item.url().includes("/reset-password/") && item.request().method() === "POST"),
    readerRow.getByRole("button", { name: "Reset password" }).click(),
  ]);
  expect(response.status()).toBe(200);
  await expect(page.getByText(/Password reset email sent to org_reader@example\.local\. They can use the link to choose a new password\./)).toBeVisible();
});

test("reader cannot reach user management controls by navigating directly to the route", async ({ page }) => {
  await loginThroughUi(page, credentialsFor("org_reader"));
  await page.goto("/users");

  await expect(page.getByText("You do not manage any organizations.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Send invite" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Reset password" })).toHaveCount(0);
});

test("reader cannot reach integration management controls by navigating directly to the route", async ({ page }) => {
  await loginThroughUi(page, credentialsFor("org_reader"));
  await page.goto("/integrations");

  await expect(page.getByText("Access denied.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Add" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Create" })).toHaveCount(0);
});

test("user can terminate the current session from account settings", async ({ page }) => {
  await loginThroughUi(page, credentialsFor("org_reader"));
  await page.goto("/settings");

  const [response] = await Promise.all([
    page.waitForResponse((item) => item.url().includes("/auth/logout/") && item.request().method() === "POST"),
    page.getByRole("button", { name: "Sign out current device" }).click(),
  ]);
  expect(response.status()).toBe(204);
  await expect(page.getByRole("heading", { name: "Client Security Portal" })).toBeVisible();
});

test("user can terminate all sessions from account settings", async ({ page }) => {
  await loginThroughUi(page, credentialsFor("org_writer"));
  await page.goto("/settings");

  const [response] = await Promise.all([
    page.waitForResponse((item) => item.url().includes("/auth/logout-all/") && item.request().method() === "POST"),
    page.getByRole("button", { name: "Sign out all devices" }).click(),
  ]);
  expect(response.status()).toBe(204);
  await expect(page.getByRole("heading", { name: "Client Security Portal" })).toBeVisible();
});

test("reader can inspect a finding but cannot access destructive finding actions", async ({ page }) => {
  await loginThroughUi(page, credentialsFor("org_reader"));
  const response = await page.request.get("/api/v2/aist/findings/?limit=1");
  expect(response.status()).toBe(200);
  const payload = await response.json() as { results?: Array<{ id: number }>; items?: Array<{ id: number }> };
  const findingId = (payload.results ?? payload.items ?? [])[0]?.id;
  expect(findingId).toBeTruthy();

  await page.goto(`/findings/${findingId}`);
  await expect(page.getByText("Finding Detail")).toBeVisible();
  await expect(page.getByRole("button", { name: "Apply Close" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Risk Approval" })).toHaveCount(0);
});

test("readers cannot enumerate or open projects from another organization", async ({ browser }) => {
  const contextOptions = {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:4173",
    ignoreHTTPSErrors: true,
  };
  const acmeContext = await browser.newContext(contextOptions);
  const acmePage = await acmeContext.newPage();
  let novaContext: Awaited<ReturnType<typeof browser.newContext>> | undefined;

  try {
    await loginThroughUi(acmePage, credentialsFor("acme_reader"));
    const acmeResponse = await acmePage.request.get("/api/v2/aist/projects/");
    expect(acmeResponse.status()).toBe(200);
    const getIds = async (response: typeof acmeResponse) => {
      const payload = await response.json() as { results?: Array<{ id: number }>; items?: Array<{ id: number }> };
      return (payload.results ?? payload.items ?? []).map((project) => project.id);
    };
    const acmeProjectIds = await getIds(acmeResponse);
    expect(acmeProjectIds.length).toBeGreaterThan(0);
    const foreignProjectId = acmeProjectIds[0];
    const acmeFindingsResponse = await acmePage.request.get(`/api/v2/aist/findings/?project_id=${foreignProjectId}&limit=1`);
    expect(acmeFindingsResponse.status()).toBe(200);
    const acmeFindingsPayload = await acmeFindingsResponse.json() as { results?: Array<{ id: number }>; items?: Array<{ id: number }> };
    const foreignFindingId = (acmeFindingsPayload.results ?? acmeFindingsPayload.items ?? [])[0]?.id;
    expect(foreignFindingId).toBeTruthy();
    await acmeContext.close();

    novaContext = await browser.newContext(contextOptions);
    const novaPage = await novaContext.newPage();
    await loginThroughUi(novaPage, credentialsFor("org_reader"));
    const novaResponse = await novaPage.request.get("/api/v2/aist/projects/");
    expect(novaResponse.status()).toBe(200);
    const novaProjectIds = await getIds(novaResponse);
    expect(novaProjectIds.length).toBeGreaterThan(0);
    expect(novaProjectIds.some((id) => acmeProjectIds.includes(id))).toBe(false);

    const foreignResponse = await novaPage.request.get(`/api/v2/aist/projects/${foreignProjectId}/meta/`);
    expect([403, 404]).toContain(foreignResponse.status());

    const foreignFindingResponse = await novaPage.request.get(`/api/v2/aist/findings/${foreignFindingId}/`);
    expect([403, 404]).toContain(foreignFindingResponse.status());

    await novaPage.goto(`/findings/${foreignFindingId}`);
    await expect(novaPage.getByText("Finding Detail")).toHaveCount(0);

    await novaPage.goto(`/findings?project_id=${foreignProjectId}`);
    await expect(novaPage.getByText(/Findings · Total/i).first()).toBeVisible();
    await expect(novaPage.locator("article[role='button']")).toHaveCount(0);
  } finally {
    await novaContext?.close();
    await acmeContext.close();
  }
});
