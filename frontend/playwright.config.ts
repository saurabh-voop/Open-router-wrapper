import { defineConfig, devices } from "@playwright/test";

// Smoke tests run against the real Next.js app with the backend mocked via
// page.route('**/api/**'), so no API server is needed.
// Browsers are preinstalled (PLAYWRIGHT_BROWSERS_PATH); never run `playwright install` here.
const PORT = Number(process.env.E2E_PORT ?? 3100);
const baseURL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL,
    trace: "retain-on-failure",
    ...(process.env.E2E_CHROMIUM_PATH ? { launchOptions: { executablePath: process.env.E2E_CHROMIUM_PATH } } : {}),
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    // Point the /api rewrite at a closed port: every API call must be mocked.
    command: `next dev --port ${PORT} --hostname 127.0.0.1`,
    env: { API_INTERNAL_URL: "http://127.0.0.1:9", NEXT_TELEMETRY_DISABLED: "1" },
    url: `${baseURL}/login`,
    timeout: 120_000,
    reuseExistingServer: !process.env.CI,
  },
});
