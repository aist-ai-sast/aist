import { expect, test, type Locator } from "@playwright/test";

import { loginByApi } from "./support";

async function addOrganizationOptions(organization: Locator, options: Array<[label: string, value: string]>) {
  await organization.evaluate((element, choices) => {
    const select = element as HTMLSelectElement;
    for (const [label, value] of choices) {
      select.add(new Option(label, value));
    }
  }, options);
}

for (const provider of ["gitlab", "gerrit", "gitea"]) {
  test(`${provider} keeps repository selection attached to the organization that supplied it`, async ({ page }) => {
    await loginByApi(page);
    let releaseOldResponse!: () => void;
    let oldRequestStarted!: () => void;
    const oldResponse = new Promise<void>((resolve) => { releaseOldResponse = resolve; });
    const oldStarted = new Promise<void>((resolve) => { oldRequestStarted = resolve; });
    const literalName = '<img src=x onerror="window.importInjected=true">';
    const repository = (name: string) => ({
      id: 73, name, path_with_namespace: name, project_path: name, full_name: name,
      description: "Repository description", default_branch: "main",
    });
    await page.route(`**/projects/${provider}/list/`, async (route) => {
      const old = route.request().postData()?.includes("organization-old");
      if (old) {
        oldRequestStarted();
        await oldResponse;
      }
      await route.fulfill({ json: { ok: true, projects: [repository(old ? "stale-repository" : literalName)] } });
    });
    const imports: Record<string, unknown>[] = [];
    await page.route(`**/import_project_from_${provider}/`, async (route) => {
      imports.push(route.request().postDataJSON());
      await route.fulfill({ status: 503, json: { detail: "Import temporarily unavailable" } });
    });
    await page.goto("/aist-admin/aist/projects/");
    await page.locator(`#${provider}-import-open`).click();
    const organization = page.locator(`#${provider}-org-select`);
    await addOrganizationOptions(organization, [
      ["Old organization", "organization-old"],
      ["Current organization", "organization-current"],
    ]);
    await organization.selectOption("organization-old", { force: true });
    await oldStarted;
    await organization.selectOption("organization-current", { force: true });
    const rows = page.locator(`#${provider}-projects-tbody`);
    await expect(rows).toContainText(literalName);
    const oldFinished = page.waitForResponse(async (response) =>
      response.url().includes(`/projects/${provider}/list/`) &&
      Boolean(response.request().postData()?.includes("organization-old")));
    releaseOldResponse();
    await oldFinished;
    await expect(rows).toContainText(literalName);
    await expect(rows).not.toContainText("stale-repository");
    await expect(rows.locator("img")).toHaveCount(0);
    const search = page.locator(`#${provider}-projects-search`);
    await search.fill("absent");
    await expect(rows.locator("tr")).toBeHidden();
    await search.fill("Repository description");
    await expect(rows.locator("tr")).toBeVisible();
    await rows.locator('input[type="checkbox"]').check();
    await page.locator(`#${provider}-import-btn`).click();
    await expect(page.locator(`#${provider}-import-error`)).toContainText("Some projects failed");
    const key = provider === "gitlab" ? "project_id" : provider === "gerrit" ? "project_path" : "repo_full_name";
    expect(imports).toEqual([{ organization_id: "organization-current", auto_analyze: false, [key]: provider === "gitlab" ? "73" : literalName }]);
    await expect(page.locator(`#${provider}-import-form`)).not.toHaveAttribute("inert", "");
    await expect(page.locator(`#${provider}ImportModal`)).toBeVisible();
    await page.route(`**/projects/${provider}/list/`, (route) => route.fulfill({
      status: 503, json: { ok: false, error: "Listing temporarily unavailable" },
    }));
    await organization.selectOption("organization-old", { force: true });
    await expect(page.locator(`#${provider}-import-error`)).toContainText("Listing temporarily unavailable");
    await expect(rows.locator("tr")).toHaveCount(0);
    await expect(page.locator(`#${provider}-import-btn`)).toBeDisabled();
    expect(imports).toHaveLength(1);
  });
}


test("GitHub invalidates repository selection when its organization changes", async ({ page }) => {
  await loginByApi(page);
  await page.route("**/github/import/repositories/?*", (route) => route.fulfill({
    json: { installation_id: new URL(route.request().url()).searchParams.get("organization_id") === "71" ? 81 : 82, repositories: [{ full_name: "team/runtime", default_branch: "main", private: true }] },
  }));
  const imports: Record<string, unknown>[] = [];
  await page.route("**/github/import/execute/", async (route) => {
    imports.push(route.request().postDataJSON());
    await route.fulfill({ json: { imported: [], failed: [{ repo: "team/runtime", reason: "unavailable" }] } });
  });
  await page.goto("/aist-admin/aist/projects/");
  await page.locator("#github-import-open").click();
  const organization = page.locator("#github-org-select");
  await addOrganizationOptions(organization, [
    ["First organization", "71"],
    ["Current organization", "72"],
  ]);
  await organization.selectOption("71", { force: true });
  await page.locator("#github-list-btn").click();
  const checkbox = page.locator('#github-repos-tbody input[type="checkbox"]');
  await checkbox.check();
  await expect(page.locator("#github-import-btn")).toBeEnabled();
  await organization.selectOption("72", { force: true });
  await expect(page.locator("#github-import-btn")).toBeDisabled();
  await expect(checkbox).toHaveCount(0);
  await expect(page.locator("#github-installation-id")).toHaveValue("");
  await page.locator("#github-list-btn").click();
  await checkbox.check();
  await page.locator("#github-import-btn").click();
  await expect(page.locator("#github-import-error")).toContainText("unavailable");
  expect(imports).toEqual([{ organization_id: 72, installation_id: 82, repositories: ["team/runtime"], auto_analyze: false }]);
  await expect(page.locator("#githubImportModal")).toBeVisible();
});
