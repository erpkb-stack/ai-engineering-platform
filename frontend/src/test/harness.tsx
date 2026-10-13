import { QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import { makeQueryClient } from "../App";
import { configureAuth } from "../api/client";
import type { S } from "../api/client";

export type Call = { method: string; path: string; query: string; body: unknown; headers: Headers };
type Handler = (c: Call) => [number, unknown] | unknown;

/** A fake api: "GET /api/v1/me" -> handler. Records every call; unknown routes are 404. */
export function fakeApi(routes: Record<string, Handler>) {
  const calls: Call[] = [];
  const fetch = vi.fn(async (req: Request) => {
    const url = new URL(req.url);
    const text = req.method === "GET" ? "" : await req.text();
    const call = { method: req.method, path: url.pathname, query: url.search, body: text ? JSON.parse(text) : undefined, headers: req.headers };
    calls.push(call);
    const h = routes[`${req.method} ${url.pathname}`];
    const out = h ? h(call) : [404, { title: "Not found", status: 404, correlation_id: "c-404" }];
    const [status, body] = Array.isArray(out) && typeof out[0] === "number" ? (out as [number, unknown]) : [200, out];
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetch);
  configureAuth(async () => "test-token", () => {});
  return { calls, fetch };
}

export function renderAt(ui: ReactElement, route = "/") {
  const client = makeQueryClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const ROLE_PERMS: Record<string, string[]> = {
  SRE: ["incidents:read", "investigations:run", "actions:request", "code:read", "docs:read"],
  INCIDENT_COMMANDER: ["incidents:read", "investigations:run", "actions:request", "actions:approve", "code:read", "docs:read"],
  MANAGER: ["incidents:read", "docs:read"],
};

export function me(role: string, user_id = "0198fd6c-7400-782d-8b48-fe5423e816ef"): S["Me"] {
  return { user_id, email: `${role.toLowerCase()}@northwind.example`, name: role, roles: [role], groups: [], permissions: ROLE_PERMS[role] ?? [] };
}

export function approval(over: Partial<S["ApprovalOut"]> = {}): S["ApprovalOut"] {
  return {
    id: "01a13300-0000-7000-8000-000000000001", incident_id: "01a13300-0000-7000-8000-0000000000aa",
    incident_key: "INC-10060", action: "rollback_deployment",
    action_args: { deploy_key: "DEPLOY-77798661", reason: "errors started with this deploy" },
    args_sha256: "a".repeat(64),
    recommendation: { statement: "Deploy DEPLOY-77798661 caused it", confidence: "HIGH", way_back: ["redeploy fc9a779f75fe"] },
    evidence_keys: ["LOG-01a11e669d4170b19af65018367a8222-4"], source: "investigation", investigation_id: null,
    requested_by: "user:11111111-1111-7111-8111-111111111111",
    requested_at: "2026-10-13T01:00:00Z", expires_at: "2026-10-13T01:30:00Z", status: "PENDING",
    ...over,
  } as S["ApprovalOut"];
}
