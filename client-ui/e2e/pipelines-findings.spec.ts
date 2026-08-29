import { expect, test } from "@playwright/test";
import type { DastProjectBinding } from "../src/lib/queries";
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

test("manual DAST import requires a binding and never sends a commit override", async ({ page }) => {
  let validateBody = "";
  let importBody = "";
  await page.route(/\/api\/v2\/aist\/projects\/\d+\/dast-bindings\//, async (route) => {
    const projectId = Number(route.request().url().match(/projects\/(\d+)/)?.[1]);
    const binding = {
      id: 901,
      project: projectId,
      target: {
        id: 77,
        provider_id: "e2e-cloud-app",
        display_name: "E2E cloud app",
        contract_revision: "2.0",
        capability_revision: "cap-e2e-import",
        schema_digest: "schema-e2e-import",
        parameter_schema: { type: "object", additionalProperties: false, properties: {} },
        provider_defaults: {},
        repository_keys: ["backend"],
        launch_requirements: ["repository-trigger"],
        autonomous_ready: true,
        is_available: true,
        last_seen_at: "2026-08-29T00:00:00Z",
      },
      source_repo_key: "backend",
      enabled: true,
      parameter_snapshot: {},
      readiness: { ready: true, issues: [], checked_at: "2026-08-29T00:00:00Z" },
      created: "2026-08-29T00:00:00Z",
      updated: "2026-08-29T00:00:00Z",
    } satisfies DastProjectBinding;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([binding]),
    });
  });
  await page.route("**/api/v2/aist/pipelines/import/validate/", async (route) => {
    validateBody = route.request().postData() ?? "";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        findings_count: 1,
        severity_breakdown: { High: 1 },
        name: "DAST",
        version: "backend@fd5b25aa1234",
        actual_source_commit: "fd5b25aa1234567890abcdef1234567890abcdef",
      }),
    });
  });
  await page.route("**/api/v2/aist/pipelines/import/", async (route) => {
    importBody = route.request().postData() ?? "";
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ pipeline_id: "e2e-dast-import", run_task_id: "e2e-task" }),
    });
  });
  await page.route("**/api/v2/aist/pipeline/e2e-dast-import/status/", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ id: "e2e-dast-import", status: "UPLOADING_RESULTS", run_task_id: "e2e-task" }),
    });
  });

  await openPipelines(page);
  await page.getByRole("button", { name: "Import pipeline launch" }).click();
  const dialog = page.getByRole("dialog", { name: "Import report" });
  const selects = dialog.getByRole("combobox");
  await selects.nth(0).click();
  await page.getByRole("option").first().click();
  await selects.nth(1).click();
  await page.getByRole("option", { name: "E2E cloud app · backend" }).click();
  const fileInput = dialog.locator('input[type="file"]');
  await expect(fileInput).toBeEnabled();
  await fileInput.setInputFiles({
    name: "dast-v2-terminal.json",
    mimeType: "application/json",
    buffer: Buffer.from('{"contract_version":"2.0"}'),
  });

  await expect(dialog.getByText("fd5b25aa1234567890abcdef1234567890abcdef")).toBeVisible();
  await expect(dialog.getByRole("textbox")).toHaveCount(0);
  expect(validateBody).toContain('name="binding_id"');
  expect(validateBody).toContain("901");
  expect(validateBody).not.toContain('name="commit_hash"');

  await dialog.getByRole("button", { name: "Create pipeline" }).click();
  await expect(dialog.getByText(/Importing report/)).toBeVisible();
  expect(importBody).toContain('name="binding_id"');
  expect(importBody).toContain("901");
  expect(importBody).not.toContain('name="commit_hash"');
});
