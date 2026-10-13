import { expect, test, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { login } from "./users";

// The approve-in-the-console flow, end to end: the rollback runs AS the approving IC.
// It creates its OWN pending request on a THROWAWAY service (scripts/ui-demo-request.sh) and
// removes the planted deploy rows afterwards - no hand-picked incident that may already be
// decided or expired. Needs the Phase 16 services: run with AEOI_UI_DECIDE=1 make ui-browser-test
test.skip(!process.env.AEOI_UI_DECIDE, "set AEOI_UI_DECIDE=1 (needs agents + tool-gateway running)");
test.describe.configure({ mode: "serial" }); // test 2 decides the request test 1 looks at

const ROOT = resolve(process.cwd(), "..");
let INC = "";

test.beforeAll(() => {
  const out = execFileSync("bash", ["scripts/ui-demo-request.sh"], { cwd: ROOT, encoding: "utf8", timeout: 60_000 });
  const m = /✔ (INC-\d+): rollback/.exec(out);
  if (!m?.[1]) throw new Error(`ui-demo-request.sh did not create a request:\n${out}`);
  INC = m[1];
});

test.afterAll(() => {
  execFileSync("uv", ["run", "--quiet", "python", "scripts/phase16_fixture.py", "clean"], { cwd: ROOT, timeout: 60_000 });
});

async function signInAs(page: Page, role: string, nth: number, path: string) {
  await page.goto(path);
  const { email, password } = login(role, nth);
  await page.locator("#username").fill(email);
  await page.locator("#password").fill(password);
  await page.locator("#kc-login").click();
}

const pendingCard = (page: Page) =>
  page.getByRole("article", { name: /approval rollback_deployment/ }).filter({ hasText: "PENDING" });

test("the requester cannot decide their own request", async ({ page }) => {
  await signInAs(page, "INCIDENT_COMMANDER", 1, `/incidents/${INC}`);
  await expect(pendingCard(page), `${INC} should have a PENDING request`).toHaveCount(1);
  await expect(pendingCard(page).getByRole("button", { name: "Approve and run" })).toBeDisabled();
  await expect(pendingCard(page)).toContainText("separation of duties");
});

test("another IC finds it in the inbox, approves it next to the evidence, and it runs", async ({ page }) => {
  await signInAs(page, "INCIDENT_COMMANDER", 2, "/approvals");
  const row = page.getByRole("row").filter({ hasText: INC });
  await expect(row, `${INC} should be in the inbox`).toBeVisible();
  await expect(page.locator(".badge")).toBeVisible();
  await row.getByRole("link", { name: /Review evidence/ }).click();
  await expect(page).toHaveURL(new RegExp(`/incidents/${INC}#approval-`));
  await pendingCard(page).getByLabel(/Reason/).fill("UI e2e: rollback is the safest mitigation");
  await pendingCard(page).getByRole("button", { name: "Approve and run" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Executed." })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("heading", { level: 1 })).toContainText("MITIGATED");
  await expect(page.getByRole("article", { name: /approval rollback_deployment/ }).first()).toContainText("EXECUTED");
});
