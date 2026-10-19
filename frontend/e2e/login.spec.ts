import { expect, test } from "@playwright/test";
import { json, me, mockApi, user } from "./mock-api";

test("login submits credentials and redirects to the app", async ({ page }) => {
  let loginBody: unknown = null;
  let signedIn = false;
  await mockApi(page, {
    "POST /auth/login": async (route, request) => {
      loginBody = request.postDataJSON();
      const { password } = loginBody as { password: string };
      if (password !== "correct horse battery") {
        await json(route, { detail: "Wrong email or password" }, 401);
        return;
      }
      signedIn = true;
      await json(route, user);
    },
    "GET /me": (route) => (signedIn ? json(route, me) : json(route, { detail: "Not signed in" }, 401)),
    "GET /conversations": (route) => json(route, []),
  });

  await page.goto("/login");
  await page.getByLabel("Email").fill("sam@example.com");
  await page.getByLabel("Password").fill("wrong");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Wrong email or password" })).toBeVisible();
  await expect(page).toHaveURL(/\/login$/);

  await page.getByLabel("Password").fill("correct horse battery");
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page).toHaveURL(/\/$/);
  expect(loginBody).toEqual({ email: "sam@example.com", password: "correct horse battery" });
  await expect(page.getByTestId("spend")).toHaveText("$3.21 / $50.00");
  await expect(page.getByRole("button", { name: "New chat" })).toBeVisible();
});

test("app shell redirects to /login when /me returns 401", async ({ page }) => {
  await mockApi(page, {
    "GET /me": (route) => json(route, { detail: "Not signed in" }, 401),
    "GET /conversations": (route) => json(route, { detail: "Not signed in" }, 401),
  });
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
});
