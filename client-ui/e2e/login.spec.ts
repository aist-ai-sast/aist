import { expect, test } from "@playwright/test";

// Deliberately does NOT use loginByApi() — every other e2e spec bootstraps
// its session via a direct API call, so nothing exercises the actual login
// form (username/password fields, submit, session bootstrap through
// /api/v2/aist/me/) end to end. These two tests drive that flow for real.

test("anonymous visitor sees the login form and can sign in through it", async ({ page }) => {
  const username = process.env.PLAYWRIGHT_USERNAME ?? "admin";
  const password = process.env.PLAYWRIGHT_PASSWORD ?? "AdminsLoveIntegrationtests!";

  await page.goto("/dashboard");

  await expect(page.getByRole("heading", { name: "Client Security Portal" })).toBeVisible({ timeout: 30_000 });
  const signInButton = page.getByRole("button", { name: "Sign in" });
  await expect(signInButton).toBeVisible();

  await page.getByPlaceholder("username").fill(username);
  await page.getByPlaceholder("password").fill(password);
  await signInButton.click();

  await expect(page.getByText("Welcome back.")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "Security Dashboard" })).toBeVisible({ timeout: 30_000 });
});

test("invalid credentials show an error and keep the visitor on the login form", async ({ page }) => {
  await page.goto("/dashboard");

  const signInButton = page.getByRole("button", { name: "Sign in" });
  await expect(signInButton).toBeVisible({ timeout: 30_000 });

  await page.getByPlaceholder("username").fill("nonexistent-e2e-user");
  await page.getByPlaceholder("password").fill("definitely-wrong-password");
  await signInButton.click();

  await expect(page.getByText("Invalid username or password.")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
});
