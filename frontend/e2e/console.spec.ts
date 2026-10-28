import { expect, test, type Page } from "@playwright/test";
import { login } from "./users";

async function signIn(page: Page, role: string, path = "/") {
  await page.goto(path);
  // a real redirect to Keycloak (Authorization Code + PKCE), not a token pasted in
  // 20 s, not the 5 s default: the FIRST test of a run meets a cold Vite + a just-started
  // Keycloak (Mac 2026-10-28: the redirect was in flight to the right URL when 5 s ran out)
  await expect(page).toHaveURL(/localhost:8090\/realms\/aeoi\/protocol\/openid-connect\/auth/, { timeout: 20_000 });
  expect(new URL(page.url()).searchParams.get("code_challenge_method")).toBe("S256");
  const { email, password } = login(role);
  await page.locator("#username").fill(email);
  await page.locator("#password").fill(password);
  await page.locator("#kc-login").click();
}

test("an SRE signs in through Keycloak and lands on the incidents", async ({ page }) => {
  await signIn(page, "SRE");
  await expect(page).toHaveURL(/\/incidents$/);
  await expect(page.getByRole("navigation")).toContainText("SRE");
  await expect(page.locator("table.grid tbody tr").first()).toContainText("INC-");
  // tokens live in memory only: nothing in the browser's storage once signed in
  const stored = await page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }));
  expect(stored).not.toContain("access_token");
  expect(stored).not.toContain("refresh_token");
});

test("a deep link survives the sign-in round trip (and only as a path on this app)", async ({ page }) => {
  await signIn(page, "SRE", "/approvals");
  await expect(page).toHaveURL(/\/approvals$/);
  await expect(page.getByRole("heading", { name: "Approvals inbox" })).toBeVisible();
});

test("an incident page shows findings with their evidence", async ({ page }) => {
  await signIn(page, "SRE");
  await page.locator("table.grid tbody tr a").first().click();
  await expect(page.getByRole("heading", { level: 1 })).toContainText("INC-");
  for (const panel of ["Hypotheses", "Evidence graph", "Approvals", "Evidence", "Timeline", "Investigations"]) {
    await expect(page.getByRole("region", { name: panel, exact: true })).toBeVisible();
  }
});

test("the evidence graph links a hypothesis to its evidence (Phase 29d)", async ({ page }) => {
  await signIn(page, "SRE");
  // the newest investigated incident: its row links to the incident page
  await page.locator("table.grid tbody tr a").first().click();
  const graph = page.getByRole("region", { name: "Evidence graph", exact: true });
  const picture = graph.getByRole("img", { name: /^Evidence graph: \d+ hypotheses/ });
  const none = graph.getByText(/^No evidence links yet/);
  // WAIT for the hypotheses to load: an instant isVisible() read the panel while it was still
  // empty and skipped the test on the Mac (2026-10-28) - a skip that hid nothing but proved nothing
  await expect(picture.or(none)).toBeVisible({ timeout: 15_000 });
  test.skip(await none.isVisible(), "the newest incident has no evidence links yet (run make demo first)");
  await expect(graph.locator(".node-h").first()).toBeVisible();
  // a readable evidence node jumps to the evidence card on the same page
  const first = graph.locator("a.node-e").first();
  const href = await first.getAttribute("href");
  expect(href).toMatch(/^#ev-[A-Z]+-/);
  await first.click();
  await expect(page.locator(href!)).toBeInViewport();
});

test("a MANAGER is told release risk needs code:read - and the api refuses them too", async ({ page, request }) => {
  await signIn(page, "MANAGER", "/release-risk");
  await expect(page.getByText("needs the code:read permission")).toBeVisible();
  // the UI gate is a convenience; the server is the gate: a real MANAGER token gets 403
  const { email, password } = login("MANAGER");
  const tok = await request.post("http://localhost:8090/realms/aeoi/protocol/openid-connect/token", {
    form: { grant_type: "password", client_id: "aeoi-cli", username: email, password },
  });
  expect(tok.ok()).toBe(true);
  const { access_token } = (await tok.json()) as { access_token: string };
  const r = await request.get("/api/v1/release-risk?deploy_key=DEPLOY-4821", { headers: { Authorization: `Bearer ${access_token}` } });
  expect(r.status()).toBe(403);
});

test("Keycloak sends the code only to the exact callback (no wildcard redirect)", async ({ page }) => {
  const bad = "http://localhost:5173/anything-else";
  await page.goto(`http://localhost:8090/realms/aeoi/protocol/openid-connect/auth?client_id=aeoi-console&response_type=code&scope=openid&redirect_uri=${encodeURIComponent(bad)}&code_challenge=x&code_challenge_method=S256`);
  await expect(page.getByText(/Invalid parameter: redirect_uri/i)).toBeVisible();
});

test("signing out ends the Keycloak session", async ({ page }) => {
  await signIn(page, "SRE");
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.goto("/incidents");
  await expect(page).toHaveURL(/localhost:8090\/realms\/aeoi/); // asked to sign in again
  await expect(page.locator("#username")).toBeVisible();
});
