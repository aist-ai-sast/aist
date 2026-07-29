import { expect, test } from "@playwright/test";
import { loginByApi } from "./support";

test.beforeEach(async ({ page }) => {
  await loginByApi(page);
});

test("manual SAST start shows queueing state and redirects to the durable request", async ({ page }) => {
  await page.goto("/aist-admin/aist/start/");
  await expect(page.getByRole("heading", { name: "Run pipeline" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("link", { name: "SAST", exact: true })).toHaveClass(/btn-primary/);
  await expect(page.getByRole("link", { name: "DAST", exact: true })).toHaveAttribute(
    "href",
    "?execution_type=DAST",
  );

  const project = page.locator("#id_project");
  const projectId = await project.locator('option:not([value=""])').first().getAttribute("value");
  expect(projectId).toBeTruthy();

  const projectMeta = page.waitForResponse(
    (response) => response.url().includes(`/projects/${projectId}/meta.json`) && response.status() === 200,
  );
  await project.selectOption(projectId as string);
  await projectMeta;

  const version = page.locator("#id_project_version");
  await expect.poll(async () => version.inputValue(), { timeout: 30_000 }).not.toBe("");

  const start = page.getByRole("button", { name: "Start pipeline" });
  await expect(start).toBeEnabled();

  let releasePost: () => void;
  const postReleased = new Promise<void>((resolve) => {
    releasePost = resolve;
  });
  await page.route("**/aist-admin/aist/start/", async (route) => {
    if (route.request().method() === "POST") {
      await postReleased;
    }
    await route.continue();
  });

  const redirected = page.waitForURL(/\/aist-admin\/aist\/launching\/\?queued_request=\d+$/);
  const queueingState = page.evaluate(() => new Promise<{ disabled: boolean; text: string }>((resolve) => {
    const button = document.querySelector<HTMLButtonElement>("#btn-start");
    if (!button) {
      throw new Error("Manual launch button not found");
    }
    const observer = new MutationObserver(() => {
      if (button.disabled && button.textContent?.trim() === "Queueing…") {
        observer.disconnect();
        resolve({ disabled: button.disabled, text: button.textContent.trim() });
      }
    });
    observer.observe(button, { attributes: true, childList: true, subtree: true });
  }));
  await page.evaluate(() => document.querySelector<HTMLButtonElement>("#btn-start")?.click());
  expect(await queueingState).toEqual({ disabled: true, text: "Queueing…" });
  releasePost!();
  await redirected;

  const requestId = new URL(page.url()).searchParams.get("queued_request");
  expect(requestId).toMatch(/^\d+$/);
  await expect(page.locator("#launch-dashboard-toast")).toContainText(`Launch request #${requestId} queued.`);
});
