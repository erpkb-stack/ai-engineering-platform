import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { approval, fakeApi, me, renderAt } from "../test/harness";
import { ApprovalsInbox } from "./ApprovalsInbox";
import { minutesLeft } from "./approvals";
import { IncidentsPage } from "./IncidentsPage";
import { ReleaseRiskPage, toRequest } from "./ReleaseRiskPage";
import { waterfall } from "./TracePage";

const inc = (n: number) => ({
  id: `id-${n}`, key: `INC-${n}`, title: `incident ${n}`, description: "", severity: "SEV2", status: "OPEN",
  affected_services: ["checkout-api"], detected_at: "2026-10-02T09:50:00Z", resolved_at: null, created_by: "x",
  created_at: "2026-10-02T09:50:00Z", updated_at: "2026-10-02T09:50:00Z", version: 1,
});

describe("approvals inbox", () => {
  it("polls every 15 s with ONE request for the whole inbox", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const api = fakeApi({ "GET /api/v1/me": () => me("SRE"), "GET /api/v1/approvals": () => [approval()] });
    renderAt(<ApprovalsInbox />);
    await screen.findByText("INC-10060");
    const polls = () => api.calls.filter((c) => c.path === "/api/v1/approvals");
    expect(polls()).toHaveLength(1);
    expect(polls()[0]!.query).toBe("?status=PENDING&limit=100");
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    await waitFor(() => expect(polls()).toHaveLength(2));
    // an SRE sees the queue but is told who decides
    expect(screen.getByText(/only an incident commander can decide/)).toBeInTheDocument();
    // the decision is made next to the evidence, not from the list
    expect(screen.getByRole("link", { name: /Review evidence/ })).toHaveAttribute("href", `/incidents/INC-10060#approval-${approval().id}`);
    expect(screen.queryByRole("button", { name: /Approve/ })).toBeNull();
  });

  it("minutes left never goes negative", () => {
    const now = Date.parse("2026-10-13T01:00:00Z");
    expect(minutesLeft("2026-10-13T01:12:00Z", now)).toBe(12);
    expect(minutesLeft("2026-10-13T00:50:00Z", now)).toBe(0);
  });
});

describe("incidents list", () => {
  it("filters go to the api, and 'Load more' follows the cursor (no offsets)", async () => {
    const api = fakeApi({
      "GET /api/v1/incidents": (c) => (c.query.includes("cursor=")
        ? { items: [inc(2)], next_cursor: null } : { items: [inc(1)], next_cursor: "cur-1" }),
    });
    renderAt(<IncidentsPage />, "/incidents?status=OPEN");
    await screen.findByText("INC-1");
    expect(api.calls[0]!.query).toContain("status=OPEN");
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    await screen.findByText("INC-2");
    expect(api.calls[1]!.query).toContain("cursor=cur-1");
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });
});

describe("release risk", () => {
  it("a MANAGER is told why (no code:read), and nothing is asked", async () => {
    const api = fakeApi({ "GET /api/v1/me": () => me("MANAGER") });
    renderAt(<Routes><Route path="/" element={<ReleaseRiskPage />} /></Routes>);
    await screen.findByText(/needs the code:read permission/);
    expect(api.calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("one target per request", () => {
    expect(toRequest("deploy", { deploy_key: " DEPLOY-4821 " })).toEqual({ deploy_key: "DEPLOY-4821" });
    expect(toRequest("pr", { repository: "checkout-api", pull_request: "812" })).toEqual({ repository: "checkout-api", pull_request: 812 });
    expect(toRequest("range", { service_key: "checkout-api", base_sha: "0742889", head_sha: "42d79c2" }))
      .toEqual({ service_key: "checkout-api", base_sha: "0742889", head_sha: "42d79c2" });
  });

  it("sends an Idempotency-Key and opens the report", async () => {
    const api = fakeApi({
      "GET /api/v1/me": () => me("SRE"),
      "POST /api/v1/release-risk": () => [201, { id: "r1", status: "SUCCEEDED", band: "HIGH", requested_by: "u", requested_at: "2026-10-13T00:00:00Z", report: null, evidence: [], error: null, agent_version: "1" }],
    });
    renderAt(<Routes><Route path="/" element={<ReleaseRiskPage />} /><Route path="/release-risk/:id" element={<p>report page</p>} /></Routes>);
    await userEvent.type(await screen.findByPlaceholderText("DEPLOY-4821"), "DEPLOY-4821");
    await userEvent.click(screen.getByRole("button", { name: "Assess" }));
    await screen.findByText("report page");
    const post = api.calls.find((c) => c.method === "POST")!;
    expect(post.body).toEqual({ deploy_key: "DEPLOY-4821" });
    expect(post.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
  });
});

describe("inbox at its limit", () => {
  it("a full page reads 100+, and the screen says only the soonest are shown", async () => {
    fakeApi({ "GET /api/v1/me": () => me("INCIDENT_COMMANDER"),
      "GET /api/v1/approvals": () => Array.from({ length: 100 }, (_, i) => approval({ id: `01a13300-0000-7000-8000-${String(i).padStart(12, "0")}` })) });
    renderAt(<ApprovalsInbox />);
    expect(await screen.findByText(/100\+ pending/)).toBeInTheDocument();
    expect(screen.getByText(/Showing the 100 that expire first/)).toBeInTheDocument();
  });
});

describe("trace waterfall", () => {
  it("a still-running agent extends to now (no NaN, no zero-width bar)", () => {
    const e = (id: string, s: string, f: string | null) => ({ execution_id: id, started_at: s, finished_at: f }) as never;
    const now = Date.parse("2026-10-10T17:59:40Z");
    const bars = waterfall([e("a", "2026-10-10T17:59:00Z", "2026-10-10T17:59:20Z"), e("b", "2026-10-10T17:59:20Z", null)], now);
    expect(bars.map((b) => [Math.round(b.left), Math.round(b.width)])).toEqual([[0, 50], [50, 50]]);
  });

  it("places each agent on the run's time axis", () => {
    const e = (id: string, s: string, f: string | null) => ({ execution_id: id, started_at: s, finished_at: f }) as never;
    const bars = waterfall([e("a", "2026-10-10T17:59:00Z", "2026-10-10T17:59:10Z"), e("b", "2026-10-10T17:59:10Z", "2026-10-10T17:59:20Z")]);
    expect(bars.map((b) => [Math.round(b.left), Math.round(b.width)])).toEqual([[0, 50], [50, 50]]);
  });
});
