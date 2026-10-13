import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { approval, fakeApi, me, renderAt } from "../../test/harness";
import { ApprovalCard, whyCannotDecide } from "./Approvals";

const IC = me("INCIDENT_COMMANDER");
const visible = new Set(["LOG-01a11e669d4170b19af65018367a8222-4"]);

describe("who may decide (UI gate; the server re-checks)", () => {
  it("an IC who did not request it may decide", () => {
    expect(whyCannotDecide(approval(), IC)).toBeNull();
  });
  it("the requester may not - separation of duties", () => {
    expect(whyCannotDecide(approval({ requested_by: `user:${IC.user_id}` }), IC)).toMatch(/another incident commander/);
  });
  it("an SRE or a MANAGER may not", () => {
    expect(whyCannotDecide(approval(), me("SRE"))).toMatch(/Only an incident commander/);
    expect(whyCannotDecide(approval(), me("MANAGER"))).toMatch(/Only an incident commander/);
  });
  it("nothing that is no longer PENDING", () => {
    for (const status of ["APPROVED", "REJECTED", "EXPIRED", "EXECUTED"] as const) {
      expect(whyCannotDecide(approval({ status }), IC)).toMatch(status);
    }
  });
});

describe("ApprovalCard", () => {
  it("shows a Recommendation with confidence, state, the way back and its evidence", () => {
    fakeApi({});
    renderAt(<ApprovalCard a={approval()} me={IC} visible={visible} />);
    expect(screen.getByText("Recommendation")).toBeInTheDocument();
    expect(screen.getByText("HIGH")).toBeInTheDocument();
    expect(screen.getByText("PENDING")).toBeInTheDocument();
    expect(screen.getByText("redeploy fc9a779f75fe")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /LOG…8222-4/ })).toHaveAttribute("href", "#ev-LOG-01a11e669d4170b19af65018367a8222-4");
  });

  it("needs a reason, then sends exactly the decision and the reason", async () => {
    const api = fakeApi({
      "POST /api/v1/approvals/01a13300-0000-7000-8000-000000000001/decision": (c) => ({
        approval: approval({ status: "REJECTED" }), executed: false, execution_error: null, result: null, echo: c.body,
      }),
    });
    renderAt(<ApprovalCard a={approval()} me={IC} visible={visible} />);
    const reject = screen.getByRole("button", { name: "Reject" });
    expect(reject).toBeDisabled(); // no reason yet
    await userEvent.type(screen.getByLabelText(/Reason/), "  not this deploy  ");
    expect(reject).toBeEnabled();
    await userEvent.click(reject);
    await waitFor(() => expect(api.calls).toHaveLength(1));
    expect(api.calls[0]!.body).toEqual({ decision: "REJECTED", reason: "not this deploy" });
    expect(api.calls[0]!.headers.get("Authorization")).toBe("Bearer test-token");
    expect(await screen.findByRole("status")).toHaveTextContent("Recorded: REJECTED");
  });

  it("the requester sees why the buttons are off, and nothing can be sent", async () => {
    const api = fakeApi({});
    renderAt(<ApprovalCard a={approval({ requested_by: `user:${IC.user_id}` })} me={IC} visible={visible} />);
    for (const name of ["Approve and run", "Reject", "Ask for more info"]) {
      expect(screen.getByRole("button", { name })).toBeDisabled();
    }
    expect(screen.getByLabelText(/Reason/)).toBeDisabled();
    expect(screen.getByText(/separation of duties/)).toBeInTheDocument();
    expect(api.calls).toHaveLength(0);
  });

  it("an api refusal is shown with its correlation id (not swallowed)", async () => {
    fakeApi({
      "POST /api/v1/approvals/01a13300-0000-7000-8000-000000000001/decision": () => [409, { title: "Conflict", status: 409, detail: "Approval is EXPIRED", correlation_id: "corr-1" }],
    });
    renderAt(<ApprovalCard a={approval()} me={IC} visible={visible} />);
    await userEvent.type(screen.getByLabelText(/Reason/), "rollback now");
    await userEvent.click(screen.getByRole("button", { name: "Approve and run" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("409 Conflict");
    expect(alert).toHaveTextContent("Approval is EXPIRED");
    expect(alert).toHaveTextContent("corr-1");
  });

  it("'run again' is offered only to the approver, only when the outcome is unknown", () => {
    fakeApi({});
    const approved = approval({ status: "APPROVED", decided_by: IC.user_id, outcome: null });
    const { unmount } = renderAt(<ApprovalCard a={approved} me={IC} visible={visible} />);
    expect(screen.getByRole("button", { name: /Run again/ })).toBeInTheDocument();
    unmount();
    renderAt(<ApprovalCard a={approved} me={me("INCIDENT_COMMANDER", "22222222-2222-7222-8222-222222222222")} visible={visible} />);
    expect(screen.queryByRole("button", { name: /Run again/ })).toBeNull();
  });
});
