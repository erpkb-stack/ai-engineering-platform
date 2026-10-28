import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { api, unwrap } from "../api/client";
import { useAttemptKey } from "../api/useAttemptKey";
import { can, useMe } from "../auth/me";
import { Loading, Panel, Pill, Problem, Time } from "../components/ui";
import { ApprovalCard } from "./incident/Approvals";
import { EvidenceGraph } from "./incident/EvidenceGraph";
import { HypothesesList } from "./incident/Hypotheses";
import { EvidenceList, isActive, PostmortemView, TimelineList } from "./incident/Panels";

export function IncidentPage() {
  const { ref = "" } = useParams();
  const qc = useQueryClient();
  const me = useMe();
  const path = { params: { path: { ref } } };
  const [showPm, setShowPm] = useState(false);

  const incident = useQuery({ queryKey: ["incident", ref], queryFn: () => unwrap(api.GET("/api/v1/incidents/{ref}", path)) });
  const investigations = useQuery({
    queryKey: ["incident", ref, "investigations"],
    queryFn: () => unwrap(api.GET("/api/v1/incidents/{ref}/investigations", path)),
    refetchInterval: (q) => (q.state.data?.some((i) => isActive(i.status)) ? 5_000 : false),
  });
  const running = investigations.data?.some((i) => isActive(i.status)) ?? false;
  const live = running ? 5_000 : false; // findings arrive while an investigation runs
  const hypotheses = useQuery({ queryKey: ["incident", ref, "hypotheses"], queryFn: () => unwrap(api.GET("/api/v1/incidents/{ref}/hypotheses", path)), refetchInterval: live });
  const evidence = useQuery({ queryKey: ["incident", ref, "evidence"], queryFn: () => unwrap(api.GET("/api/v1/incidents/{ref}/evidence", path)), refetchInterval: live });
  const timeline = useQuery({ queryKey: ["incident", ref, "timeline"], queryFn: () => unwrap(api.GET("/api/v1/incidents/{ref}/timeline", path)), refetchInterval: live });
  const approvals = useQuery({
    queryKey: ["incident", ref, "approvals"],
    queryFn: () => unwrap(api.GET("/api/v1/incidents/{ref}/approvals", path)),
    // another IC may decide while you read: keep a pending request fresh (inbox cadence)
    refetchInterval: (q) => (running ? 5_000 : q.state.data?.some((a) => a.status === "PENDING") ? 15_000 : false),
  });
  const postmortem = useQuery({
    queryKey: ["incident", ref, "postmortem"],
    queryFn: () => unwrap(api.GET("/api/v1/incidents/{ref}/postmortem", path)),
    enabled: showPm,
  });

  const attempt = useAttemptKey();
  const investigate = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/incidents/{ref}/investigate", { ...path, headers: attempt.headers() })),
    onSuccess: () => {
      attempt.done();
      void qc.invalidateQueries({ queryKey: ["incident", ref] });
    },
  });

  // the run writes its findings at the END: when it stops, fetch everything once more (M1)
  const wasRunning = useRef(false);
  useEffect(() => {
    if (wasRunning.current && !running) void qc.invalidateQueries({ queryKey: ["incident", ref] });
    wasRunning.current = running;
  }, [running, qc, ref]);

  // evidence keys this reader may see: a chip for anything else says "not visible to you"
  const visible = useMemo(() => new Set((evidence.data ?? []).map((e) => e.evidence_key)), [evidence.data]);

  // arriving from the inbox (#approval-…) or a chip (#ev-…): scroll ONCE per hash, when the
  // target exists - not again on every poll (M2)
  const { hash } = useLocation();
  const scrolled = useRef("");
  useEffect(() => {
    if (!hash || scrolled.current === hash) return;
    const el = document.getElementById(decodeURIComponent(hash.slice(1)));
    if (el) {
      el.scrollIntoView({ block: "center" });
      scrolled.current = hash;
    }
  }, [hash, incident.data, approvals.data, evidence.data]);

  if (incident.isPending) return <Loading what={ref} />;
  if (incident.error) return <Problem error={incident.error} />;
  const inc = incident.data;
  const pending = (approvals.data ?? []).filter((a) => a.status === "PENDING");

  return (
    <div className="incident">
      <header className="incident-head">
        <h1>{inc.key} <Pill value={inc.severity} /> <Pill value={inc.status} /></h1>
        <p className="title">{inc.title}</p>
        <p className="small muted">
          {inc.affected_services.join(", ")} · detected <Time at={inc.detected_at} />
          {inc.resolved_at && <> · resolved <Time at={inc.resolved_at} /></>}
        </p>
        {can(me.data, "investigations:run") && (
          <button className="primary" disabled={running || investigate.isPending} onClick={() => investigate.mutate()}>
            {running ? "Investigation running…" : "Investigate"}
          </button>
        )}
        <Problem error={investigate.error} />
      </header>

      {pending.length > 0 && (
        <p className="banner" role="status">{pending.length} action(s) waiting for a decision — see Approvals below.</p>
      )}

      <Panel title="Hypotheses">
        <Problem error={hypotheses.error} />
        {hypotheses.isPending ? <Loading what="hypotheses" /> : <HypothesesList items={hypotheses.data ?? []} visible={visible} />}
      </Panel>

      <Panel title="Evidence graph" id="graph">
        {hypotheses.data && <EvidenceGraph items={hypotheses.data} visible={visible} />}
      </Panel>

      <Panel title="Approvals" id="approvals">
        <Problem error={approvals.error} />
        {approvals.data?.length === 0 && <p className="muted">No actions requested.</p>}
        {(approvals.data ?? []).map((a) => <ApprovalCard key={a.id} a={a} me={me.data} visible={visible} />)}
      </Panel>

      <Panel title="Evidence">
        <Problem error={evidence.error} />
        {evidence.isPending ? <Loading what="evidence" /> : <EvidenceList items={evidence.data ?? []} />}
      </Panel>

      <Panel title="Timeline">
        <Problem error={timeline.error} />
        {timeline.data && <TimelineList items={timeline.data} visible={visible} />}
      </Panel>

      <Panel title="Investigations">
        <Problem error={investigations.error} />
        <table className="grid">
          <thead><tr><th>Started</th><th>Status</th><th>Agents</th><th>Cost</th><th>By</th><th /></tr></thead>
          <tbody>
            {(investigations.data ?? []).map((i) => (
              <tr key={i.investigation_id}>
                <td><Time at={i.started_at} /></td>
                <td><Pill value={i.status} /></td>
                <td className="small">{(i.tasks ?? []).map((t) => `${t.agent}:${t.status}`).join(" · ")}</td>
                <td className="mono">${i.spent_usd}</td>
                <td className="mono small">{i.requested_by}</td>
                <td><Link to={`/investigations/${i.investigation_id}`}>Trace →</Link></td>
              </tr>
            ))}
            {investigations.data?.length === 0 && <tr><td colSpan={6} className="muted">Not investigated yet.</td></tr>}
          </tbody>
        </table>
      </Panel>

      <Panel title="Postmortem">
        {showPm ? (
          postmortem.isPending ? <Loading what="postmortem" /> :
            <PostmortemView doc={postmortem.data} error={postmortem.error} visible={visible} />
        ) : <button onClick={() => setShowPm(true)}>Show postmortem</button>}
      </Panel>
    </div>
  );
}
