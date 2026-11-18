import { expect, test, type Page } from "@playwright/test";

import { loginByApi } from "./support";

const IMAGE = "ubuntu:24.04@sha256:" + "a".repeat(64);

interface Assignment {
  version: string;
  revision: number;
}

async function openEnvironmentEditor(page: Page, content: string, failFirstAssignment = false, shared = false) {
  await loginByApi(page);
  const assignments: Assignment[] = [];
  let assignedRevision = 41;
  let failed = false;
  const revision = { id: 41, content, base_image: "", is_shared: shared, sha256: "b".repeat(64), source: shared ? "shared_default" : "version" };
  await page.route(/\/api\/v2\/aist\/projects\/\d+\/(active-script\/|versions\/\d+\/script)$/, async (route) => {
    if (route.request().method() === "PATCH") {
      assignments.push({
        version: route.request().url().match(/\/versions\/(\d+)\/script$/)![1],
        revision: route.request().postDataJSON().script_id,
      });
      if (failFirstAssignment && !failed) {
        failed = true;
        await route.fulfill({ status: 503, json: { detail: "Assignment temporarily unavailable" } });
        return;
      }
      assignedRevision = route.request().postDataJSON().script_id;
      await route.fulfill({ json: { script_id: assignedRevision } });
      return;
    }
    await route.fulfill({ json: { ...revision, id: assignedRevision } });
  });
  await page.route("**/api/v2/aist/projects/*/scripts/", (route) => route.fulfill({
    status: 201, json: { ...revision, id: 42, is_shared: route.request().postDataJSON().scope === "global", base_image: route.request().postDataJSON().base_image },
  }));
  await page.route("**/aist-admin/aist/projects/*/update/", (route) => route.fulfill({
    json: { ok: true, project: { supported_languages: [], profile: {}, compilable: false } },
  }));
  await page.goto("/aist-admin/aist/projects/");
  const editButton = page.locator(".aist-project-edit-btn").first();
  await editButton.evaluate((button) => {
    const row = button.closest("tr")!;
    row.setAttribute("data-project-versions", JSON.stringify([
      { id: 1001, label: "work" }, { id: 1002, label: "release" },
    ]));
    row.setAttribute("data-project-active-version-id", "1001");
  });
  await editButton.click();
  await expect(page.locator("#ap-script-content")).toHaveValue(content.replaceAll("\r\n", "\n"));
  return { assignments, assignedRevision: () => assignedRevision };
}

for (const [label, newline] of [["LF", "\n"], ["CRLF", "\r\n"]]) {
  test(`changing the builder image preserves a masked ${label} script by revision`, async ({ page }) => {
    const content = ["#!/bin/bash", "export API_TOKEN=********", "echo ready", ""].join(newline);
    const state = await openEnvironmentEditor(page, content);
    await page.locator("#ap-base-image").fill(IMAGE);
    const saved = page.waitForRequest((request) => request.method() === "POST" && request.url().endsWith("/scripts/"));
    await page.locator('#aist-project-edit-form button[type="submit"]').click();
    const body = (await saved).postDataJSON();
    expect(body.base_image).toBe(IMAGE);
    expect(body.source_script_id).toBe(41);
    expect(body).not.toHaveProperty("content");
    await expect(page.locator("#aistProjectModal")).not.toBeVisible();
    expect(state.assignedRevision()).toBe(42);
  });
}

test("retrying a failed assignment binds the revision before reporting a successful save", async ({ page }) => {
  const state = await openEnvironmentEditor(page, "#!/bin/bash\necho ready\n", true);
  await page.locator("#ap-base-image").fill(IMAGE);
  const save = page.locator('#aist-project-edit-form button[type="submit"]');
  await save.click();
  await expect(page.locator("#aist-project-edit-error")).toContainText("Assignment temporarily unavailable");
  expect(state.assignedRevision()).toBe(41);
  await save.click();
  await expect(page.locator("#aistProjectModal")).not.toBeVisible();
  expect(state.assignments).toEqual([
    { version: "1001", revision: 42 }, { version: "1001", revision: 42 },
  ]);
  expect(state.assignedRevision()).toBe(42);
});

test("declining to discard edits retains the loaded branch as the save target", async ({ page }) => {
  const state = await openEnvironmentEditor(page, "#!/bin/bash\necho ready\n");
  await page.locator("#ap-base-image").fill(IMAGE);
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.locator("#ap-script-version").selectOption("1002", { force: true });
  await expect(page.locator("#ap-script-version")).toHaveValue("1001");
  await expect(page.locator("#ap-base-image")).toHaveValue(IMAGE);
  await page.locator('#aist-project-edit-form button[type="submit"]').click();
  await expect(page.locator("#aistProjectModal")).not.toBeVisible();
  expect(state.assignments).toEqual([{ version: "1001", revision: 42 }]);
});

async function openAnotherProject(page: Page) {
  const originalId = await page.locator("#ap-project-id").inputValue();
  const otherId = String(Number(originalId) + 10000);
  await page.keyboard.press("Escape");
  await page.locator(`tr[data-project-id="${originalId}"]`).evaluate((row, id) => {
    const other = row.cloneNode(true) as HTMLElement;
    const oldId = row.getAttribute("data-project-id");
    other.setAttribute("data-project-id", id);
    other.setAttribute("data-project-name", "Another project");
    other.setAttribute("data-project-langs", "Python");
    for (const attribute of ["data-update-url", "data-delete-url", "data-regenerate-url"]) {
      other.setAttribute(attribute, (other.getAttribute(attribute) || "").replace(`/${oldId}/`, `/${id}/`));
    }
    row.parentElement!.appendChild(other);
  }, otherId);
  await page.locator(`tr[data-project-id="${otherId}"] .aist-project-edit-btn`).click();
  await expect(page.locator("#ap-project-id")).toHaveValue(otherId);
  await expect(page.locator("#ap-supported-languages")).toHaveValue("Python");
  await expect(page.locator("#ap-script-loading")).toBeHidden();
  return { originalId, otherId };
}

test("clearing an init script shows its validation error instead of a successful save", async ({ page }) => {
  await openEnvironmentEditor(page, "#!/bin/bash\necho ready\n");
  await page.route("**/api/v2/aist/projects/*/scripts/", (route) => route.fulfill({
    status: 400, json: { content: ["Script content must not be blank."] },
  }));
  await page.locator("#ap-script-content").fill("");
  await page.locator('#aist-project-edit-form button[type="submit"]').click();
  await expect(page.locator("#aist-project-edit-error")).toContainText("must not be blank");
  await expect(page.locator("#aistProjectModal")).toBeVisible();
});

test("a pending save retains its project, branch and submitted settings after another project opens", async ({ page }) => {
  await openEnvironmentEditor(page, "#!/bin/bash\necho ready\n");
  let releaseSave!: () => void;
  const pendingSave = new Promise<void>((resolve) => { releaseSave = resolve; });
  await page.route("**/api/v2/aist/projects/*/scripts/", async (route) => {
    await pendingSave;
    await route.fulfill({ status: 201, json: { id: 42, sha256: "c".repeat(64), is_shared: false, base_image: IMAGE } });
  });
  await page.locator("#ap-base-image").fill(IMAGE);
  await page.locator("#ap-supported-languages").fill("C++");
  const scriptRequest = page.waitForRequest((request) => request.method() === "POST" && request.url().endsWith("/scripts/"));
  await page.locator('#aist-project-edit-form button[type="submit"]').click();
  await scriptRequest;
  const { originalId, otherId } = await openAnotherProject(page);
  const assignment = page.waitForRequest((request) => request.method() === "PATCH" && request.url().endsWith("/script"));
  const settings = page.waitForResponse((response) => response.request().method() === "POST" && response.url().endsWith("/update/"));
  releaseSave();
  expect((await assignment).url()).toContain(`/projects/${originalId}/versions/1001/script`);
  const response = await settings;
  expect(response.url()).toContain(`/projects/${originalId}/update/`);
  expect(response.request().postData()).toContain("C++");
  expect(response.request().postData()).not.toContain("Python");
  await expect(page.locator("#aistProjectModal")).toBeVisible();
  await expect(page.locator("#ap-project-id")).toHaveValue(otherId);
  await expect(page.locator("#ap-supported-languages")).toHaveValue("Python");
});

test("a pending delete removes its own row after another project opens", async ({ page }) => {
  await openEnvironmentEditor(page, "#!/bin/bash\necho ready\n");
  let releaseDelete!: () => void;
  const pendingDelete = new Promise<void>((resolve) => { releaseDelete = resolve; });
  await page.route("**/api/v2/aist/projects/*/", async (route) => {
    if (route.request().method() !== "DELETE") { await route.fallback(); return; }
    await pendingDelete;
    await route.fulfill({ status: 204 });
  });
  const deleting = page.waitForRequest((request) => request.method() === "DELETE");
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator("#aist-project-delete-btn").click();
  await deleting;
  const { originalId, otherId } = await openAnotherProject(page);
  releaseDelete();
  await expect(page.locator(`tr[data-project-id="${originalId}"]`)).toHaveCount(0);
  await expect(page.locator(`tr[data-project-id="${otherId}"]`)).toHaveCount(1);
  await expect(page.locator("#aistProjectModal")).toBeVisible();
  await expect(page.locator("#ap-project-id")).toHaveValue(otherId);
});


for (const scope of ["local", "global"]) {
  test(`saving unchanged shared configuration with ${scope} scope preserves its intended ownership`, async ({ page }) => {
    const state = await openEnvironmentEditor(page, "#!/bin/bash\necho ready\n", false, true);
    const creations: unknown[] = [];
    page.on("request", (request) => {
      if (request.method() === "POST" && request.url().endsWith("/scripts/")) creations.push(request.postDataJSON());
    });
    await page.locator(`#ap-scope-${scope}`).check();
    await page.locator("#ap-supported-languages").fill("C++");
    await page.locator('#aist-project-edit-form button[type="submit"]').click();
    await expect(page.locator("#aistProjectModal")).not.toBeVisible();
    if (scope === "local") {
      expect(creations).toEqual([expect.objectContaining({ source_script_id: 41, scope: "local", set_active: false })]);
      expect(state.assignments).toEqual([{ version: "1001", revision: 42 }]);
    } else {
      expect(creations).toEqual([]);
      expect(state.assignments).toEqual([]);
    }
  });
}

test("a second preset can be saved after closing a successfully saved preset", async ({ page }) => {
  await openEnvironmentEditor(page, "#!/bin/bash\necho ready\n");
  let created = 0;
  await page.route("**/projects/*/launch-configs/create/", (route) => {
    created++;
    return route.fulfill({ json: { ok: true, item: { id: 100 + created } } });
  });
  for (const name of ["First preset", "Second preset"]) {
    await page.locator("#btnOpenCreateLaunchPreset").click();
    const modal = page.locator("#modalCreateLaunchPreset");
    await expect(modal.locator("#btn-save-launch-config")).toBeEnabled();
    await modal.locator('input[name="name"]').fill(name);
    await modal.locator("#btn-save-launch-config").click();
    await expect(modal.locator("#aist-preset-status")).toContainText("Saved");
    await page.keyboard.press("Escape");
  }
  expect(created).toBe(2);
});


test("a previous preset response cannot mark a newly opened preset as saved", async ({ page }) => {
  await openEnvironmentEditor(page, "#!/bin/bash\necho ready\n");
  await page.route("**/projects/*/launch-configs/create/", (route) => route.fulfill({
    json: { ok: true, item: { id: 101 } },
  }));
  await page.evaluate(() => {
    const originalFetch = window.fetch;
    window.fetch = async (...args) => {
      const response = await originalFetch(...args);
      if (!response.url.includes("/launch-configs/create/")) return response;
      const readJson = response.json.bind(response);
      response.json = async () => {
        document.documentElement.setAttribute("data-preset-body-pending", "true");
        await new Promise<void>((resolve) => document.addEventListener("release-preset-body", () => resolve(), { once: true }));
        return readJson();
      };
      return response;
    };
  });
  await page.locator("#btnOpenCreateLaunchPreset").click();
  const modal = page.locator("#modalCreateLaunchPreset");
  await modal.locator('input[name="name"]').fill("Previous preset");
  await modal.locator("#btn-save-launch-config").click();
  await expect(page.locator("html")).toHaveAttribute("data-preset-body-pending", "true");
  await page.keyboard.press("Escape");
  await page.locator("#btnOpenCreateLaunchPreset").click();
  await page.evaluate(async () => {
    document.dispatchEvent(new Event("release-preset-body"));
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  });
  await expect(modal.locator("#aist-preset-status")).toHaveText("Not saved yet.");
  await expect(modal.locator("#btn-save-launch-config")).toBeEnabled();
  await expect(modal.locator(".aist-pipeline-scope[data-aist-project-id]")).toHaveAttribute("data-aist-launch-config-id", "");
});
