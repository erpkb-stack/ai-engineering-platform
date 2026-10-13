import { act, screen, waitFor } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { approval, fakeApi, me, renderAt } from "../test/harness";
import { IncidentPage } from "./IncidentPage";

const incident = { id: "i1", key: "INC-10019", title: "Checkout 5xx", description: "", severity: "SEV2", status: "INVESTIGATING",
  affected_services: ["checkout-api"], detected_at: "2026-10-02T09:50:00Z", resolved_at: null, created_by: "x",
  created_at: "2026-10-02T09:50:00Z", updated_at: "2026-10-02T09:50:00Z", version: 1 };
const run = (status: string) => ({ investigation_id: "inv1", incident_id: "i1", status, requested_by: "user:x",
  started_at: "2026-10-02T10:00:00Z", finished_at: null, deadline_at: null, spent_usd: "0", error: null, plan: {}, tasks: [] });
const hyp = { id: "h1", investigation_id: "inv1", key: "h1", rank: 1, statement: "Deploy DEPLOY-4821 caused it",
  confidence: "HIGH", status: "VALIDATED", produced_by: "hypothesis", created_at: "2026-10-02T10:01:00Z", detail: {}, edges: [], hidden_edges: 0 };

function page() {
  return renderAt(<Routes><Route path="/incidents/:ref" element={<IncidentPage />} /></Routes>, "/incidents/INC-10019");
}

describe("incident page", () => {
  it("fetches the findings once more when the investigation finishes (they are written at the end)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let finished = false;
    fakeApi({
      "GET /api/v1/me": () => me("SRE"),
      "GET /api/v1/incidents/INC-10019": () => incident,
      "GET /api/v1/incidents/INC-10019/investigations": () => [run(finished ? "COMPLETE" : "RUNNING")],
      "GET /api/v1/incidents/INC-10019/hypotheses": () => (finished ? [hyp] : []),
      "GET /api/v1/incidents/INC-10019/evidence": () => [],
      "GET /api/v1/incidents/INC-10019/timeline": () => [],
      "GET /api/v1/incidents/INC-10019/approvals": () => [],
    });
    page();
    expect(await screen.findByText(/No hypotheses yet/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Investigation running/ })).toBeDisabled();
    finished = true; // the run ends and writes its hypotheses
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(await screen.findByText("Deploy DEPLOY-4821 caused it")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Investigate" })).toBeEnabled();
  });

  it("a MANAGER gets no Investigate button; the approvals panel shows why they cannot decide", async () => {
    fakeApi({
      "GET /api/v1/me": () => me("MANAGER"),
      "GET /api/v1/incidents/INC-10019": () => incident,
      "GET /api/v1/incidents/INC-10019/investigations": () => [],
      "GET /api/v1/incidents/INC-10019/hypotheses": () => [],
      "GET /api/v1/incidents/INC-10019/evidence": () => [],
      "GET /api/v1/incidents/INC-10019/timeline": () => [],
      "GET /api/v1/incidents/INC-10019/approvals": () => [approval({ incident_key: "INC-10019" })],
    });
    page();
    expect(await screen.findByText(/Only an incident commander can decide/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Investigate" })).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("1 action(s) waiting");
  });

  it("an incident that does not exist shows the api's problem, not a blank page", async () => {
    fakeApi({ "GET /api/v1/me": () => me("SRE"), "GET /api/v1/incidents/INC-10019": () => [404, { title: "Resource not found", status: 404, correlation_id: "c9" }] });
    page();
    expect(await screen.findByRole("alert")).toHaveTextContent("404 Resource not found");
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("c9"));
  });
});
