import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, unwrap, type Approval, type Me } from "../../api/client";
import { actorOf, can } from "../../auth/me";
import { EvidenceChip, Pill, Problem, Time } from "../../components/ui";

type Decision = "APPROVED" | "REJECTED" | "MORE_INFO";

/** Why THIS user may not decide THIS request (null = may decide). The server re-checks all. */
export function whyCannotDecide(a: Approval, me: Me | undefined): string | null {
  if (a.status !== "PENDING") return `This request is ${a.status}.`;
  if (!can(me, "actions:approve")) return "Only an incident commander can decide.";
  if (a.requested_by === actorOf(me)) return "You requested this — another incident commander must decide (separation of duties).";
  return null;
}

const asLines = (v: unknown): string[] =>
  Array.isArray(v) ? v.map(String) : typeof v === "string" && v ? [v] : [];

/** A RECOMMENDATION (type), with its evidence, the way back, and its approval state. */
export function ApprovalCard({ a, me, visible }: { a: Approval; me: Me | undefined; visible: Set<string> }) {
  const qc = useQueryClient();
  const [reason, setReason] = useState("");
  const rec = a.recommendation as Record<string, unknown>;
  const blocked = whyCannotDecide(a, me);

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["incident"] });
    void qc.invalidateQueries({ queryKey: ["approvals"] });
    void qc.invalidateQueries({ queryKey: ["incidents"] }); // the list shows the new status too
  };
  const decide = useMutation({
    mutationFn: (decision: Decision) =>
      unwrap(api.POST("/api/v1/approvals/{approval_id}/decision", {
        params: { path: { approval_id: a.id } },
        body: { decision, reason: reason.trim() },
      })),
    onSuccess: refresh,
  });
  const execute = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/approvals/{approval_id}/execute", { params: { path: { approval_id: a.id } } })),
    onSuccess: refresh,
  });
  const result = decide.data ?? execute.data;
  const reasonOk = reason.trim().length >= 3;
  const canRetry = a.status === "APPROVED" && !a.outcome && !!me && a.decided_by === me.user_id;

  return (
    <article className="finding" id={`approval-${a.id}`} aria-label={`approval ${a.action}`}>
      <header>
        <Pill value="Recommendation" kind="type" />
        {typeof rec.confidence === "string" && <Pill value={rec.confidence} kind={`conf-${rec.confidence}`} />}
        <Pill value={a.status} />
        <span className="mono">{a.action} {String(a.action_args.deploy_key ?? "")}</span>
        <span className="muted small">
          requested by {a.requested_by} ({a.source}) · <Time at={a.requested_at} /> · expires <Time at={a.expires_at} />
        </span>
      </header>
      {typeof rec.statement === "string" && <p className="statement">{rec.statement}</p>}
      <p className="small">Reason given: {String(a.action_args.reason ?? "")}</p>
      {asLines(rec.way_back).length > 0 && (
        <div className="small">
          <strong>Way back:</strong>
          <ul>{asLines(rec.way_back).map((l) => <li key={l}>{l}</li>)}</ul>
        </div>
      )}
      <div className="edges">
        <span className="edge-label">Evidence ({a.evidence_keys.length})</span>
        {a.evidence_keys.length === 0 ? <span className="muted small">none attached (manual request)</span> :
          a.evidence_keys.map((k) => <EvidenceChip key={k} evidenceKey={k} stance="SUPPORTS" visible={visible.has(k)} />)}
      </div>

      {a.decided_by && (
        <p className="small">
          Decided by user:{a.decided_by} ({a.decider_role}) <Time at={a.decided_at} />: “{a.reason}”
        </p>
      )}
      {a.outcome && <p className="small">Outcome: <Pill value={a.outcome} /></p>}

      {a.status === "PENDING" && (
        <div className="decide">
          <label className="small" htmlFor={`reason-${a.id}`}>Reason (recorded with your decision)</label>
          <textarea id={`reason-${a.id}`} value={reason} onChange={(e) => setReason(e.target.value)} rows={2}
            disabled={!!blocked || decide.isPending} />
          <div className="buttons">
            {(["APPROVED", "REJECTED", "MORE_INFO"] as const).map((d) => (
              <button key={d} className={d === "APPROVED" ? "primary" : ""}
                disabled={!!blocked || !reasonOk || decide.isPending}
                title={blocked ?? (reasonOk ? "" : "Give a reason (3+ characters)")}
                onClick={() => decide.mutate(d)}>
                {d === "APPROVED" ? "Approve and run" : d === "REJECTED" ? "Reject" : "Ask for more info"}
              </button>
            ))}
          </div>
          {blocked && <p className="muted small">{blocked}</p>}
        </div>
      )}
      {canRetry && (
        <p className="small">
          Approved, but no outcome was recorded (the result was unknown).{" "}
          <button onClick={() => execute.mutate()} disabled={execute.isPending}>Run again (safe: runs at most once)</button>
        </p>
      )}
      <Problem error={decide.error ?? execute.error} />
      {result && (
        <p className="small" role="status">
          {result.executed ? "Executed." : result.execution_error ? `Not executed: ${result.execution_error}` : `Recorded: ${result.approval.status}.`}
        </p>
      )}
    </article>
  );
}
