import { defineConfig } from "@playwright/test";

// e2e against the REAL stack: Keycloak (make keycloak-up), the services, and Vite (started here).
// It signs in through Keycloak's login page with the dev users (secrets/keycloak/users.txt).
export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  workers: 1, // one browser; the stack is a laptop
  use: {
    baseURL: "http://localhost:5173",
    trace: "retain-on-failure",
    launchOptions: process.env.PW_CHROMIUM_PATH ? { executablePath: process.env.PW_CHROMIUM_PATH } : {},
  },
  webServer: { command: "npm run dev", url: "http://localhost:5173", reuseExistingServer: true, timeout: 60_000 },
});
