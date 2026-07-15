import { expect, type Page } from "@playwright/test";

const cachedAuthCookies = new Map<string, Awaited<ReturnType<Page["context"]["cookies"]>>>();

export type E2ECredentials = {
  username: string;
  password: string;
};

export function credentialsFor(role: "admin" | "org_reader" | "org_writer" | "org_owner" | "acme_reader"): E2ECredentials {
  if (role === "admin") {
    return {
      username: process.env.PLAYWRIGHT_USERNAME ?? "admin",
      password: process.env.PLAYWRIGHT_PASSWORD ?? "AdminsLoveIntegrationtests!",
    };
  }

  const envPrefix = role.toUpperCase();
  return {
    username: process.env[`PLAYWRIGHT_${envPrefix}_USERNAME`] ?? role,
    password: process.env[`PLAYWRIGHT_${envPrefix}_PASSWORD`] ?? "pass",
  };
}

export async function loginByApi(page: Page, credentials = credentialsFor("admin")) {
  const { username, password } = credentials;
  const credentialsKey = `${username}\u0000${password}`;
  const cachedCookies = cachedAuthCookies.get(credentialsKey);

  if (cachedCookies && cachedCookies.length > 0) {
    await page.context().addCookies(cachedCookies);
    const meWithCached = await page.request.get("/api/v2/aist/me/");
    if (meWithCached.status() === 200) {
      return;
    }
    cachedAuthCookies.delete(credentialsKey);
  }

  await page.request.get("/auth/login/");
  const csrfToken = (await page.context().cookies()).find((cookie) => cookie.name === "csrftoken")?.value;
  expect(csrfToken).toBeTruthy();

  for (let attempt = 0; attempt < 3; attempt += 1) {
    const response = await page.request.post("/api/v2/aist/auth/login/", {
      data: { username, password },
      headers: { "X-CSRFToken": csrfToken as string },
    });
    const body = await response.text();
    if (response.status() === 204) {
      break;
    }
    if (response.status() !== 429 || attempt === 2) {
      expect(response.status(), body).toBe(204);
      break;
    }
    const waitSeconds = Number(body.match(/available in (\d+) seconds/i)?.[1] ?? "2");
    await page.waitForTimeout((waitSeconds + 1) * 1000);
  }

  const meResponse = await page.request.get("/api/v2/aist/me/");
  expect(meResponse.status(), await meResponse.text()).toBe(200);
  cachedAuthCookies.set(credentialsKey, await page.context().cookies());
}

export async function loginThroughUi(page: Page, credentials: E2ECredentials) {
  await page.goto("/dashboard");

  const signIn = page.getByRole("button", { name: "Sign in" });
  await expect(signIn).toBeVisible({ timeout: 30_000 });
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await page.getByPlaceholder("username").fill(credentials.username);
    await page.getByPlaceholder("password").fill(credentials.password);
    const [response] = await Promise.all([
      page.waitForResponse((item) => item.url().includes("/api/v2/aist/auth/login/") && item.request().method() === "POST"),
      signIn.click(),
    ]);
    if (response.status() === 204) break;
    const body = await response.text();
    if (response.status() !== 429 || attempt === 2) expect(response.status(), body).toBe(204);
    const seconds = Number(body.match(/available in (\d+) seconds/i)?.[1] ?? "1");
    await page.waitForTimeout((seconds + 1) * 1000);
  }

  await expect(page.getByRole("heading", { name: "Security Dashboard" })).toBeVisible({ timeout: 30_000 });
}

export async function openCalendar(page: Page) {
  await page.goto("/calendar");
  await expect(page.getByRole("heading", { name: "Calendar" })).toBeVisible({ timeout: 30_000 });
  const events = page.locator(".fc-event");
  await expect.poll(async () => events.count(), { timeout: 30_000 }).toBeGreaterThan(0);
}

export async function openPipelines(page: Page) {
  await page.goto("/pipelines");
  await expect(page.getByRole("main").getByText("Pipelines", { exact: true })).toBeVisible({ timeout: 30_000 });
  const cards = page.locator("article[role='button']");
  await expect.poll(async () => cards.count(), { timeout: 30_000 }).toBeGreaterThan(0);
}

export async function openFindings(
  page: Page,
  search = "",
  options: { requireCards?: boolean } = {},
) {
  const { requireCards = true } = options;
  await page.goto(`/findings${search}`);
  await expect(page.getByText(/Findings · Total/i).first()).toBeVisible({ timeout: 30_000 });
  if (requireCards) {
    const cards = page.locator("article[role='button']");
    await expect.poll(async () => cards.count(), { timeout: 30_000 }).toBeGreaterThan(0);
  }
}
