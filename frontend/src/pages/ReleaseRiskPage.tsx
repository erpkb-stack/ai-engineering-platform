import { useMutation, useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, unwrap, type S } from "../api/client";
import { useAttemptKey } from "../api/useAttemptKey";
import { can, useMe } from "../auth/me";
import { EvidenceChip, Excerpt, Loading, Panel, Pill, Problem, shortKey, Time } from "../components/ui";

type Req = S["ReleaseRiskRequest"];

/** What the form asks for -> the api's request (one target: a deploy, a PR, or a range). */
export function toRequest(kind: "deploy" | "pr" | "range", f: Record<string, string>): Req {
  if (kind === "deploy") return { deploy_key: f.deploy_key?.trim() || null };
  if (kind === "pr") return { repository: f.repository?.trim() || null, pull_request: Number(f.pull_request) || null };
  return { service_key: f.service_key?.trim() || null, base_sha: f.base_sha?.trim() || null, head_sha: f.head_sha?.trim() || null };
}

export function ReleaseRiskPage() {
  const me = useMe();
  const nav = useNavigate();
  const [kind, setKind] = useState<"deploy" | "pr" | "range">("deploy");
  const [f, setF] = useState<Record<string, string>>({});
  const attempt = useAttemptKey();
  const ask = useMutation({
    mutationFn: (body: Req) => unwrap(api.POST("/api/v1/release-risk", { body, headers: attempt.headers() })),
    onSuccess: (r) => {
      attempt.done();
      nav(`/release-risk/${r.id}`);
    },
  });
  if (me.data && !can(me.data, "code:read")) {
    return <Panel title="Release risk"><p className="muted">Release risk quotes code: it needs the code:read permission.</p></Panel>;
  }
  const field = (name: string, label: string, ph: string) => (
    <label>{label} <input name={name} placeholder={ph} value={f[name] ?? ""} onChange={(e) => { attempt.done(); setF({ ...f, [name]: e.target.value }); }} /></label>
  );
  const submit = (e: FormEvent) => {
    e.preventDefault();
    ask.mutate(toRequest(kind, f));
  };
  return (
    <Panel title="Release risk" actions={<span className="muted small">advisory — never blocks a deploy</span>}>
      <form onSubmit={submit} className="form">
        <div className="tabs" role="tablist">
          {(["deploy", "pr", "range"] as const).map((k) => (
            <button type="button" role="tab" aria-selected={kind === k} key={k} className={kind === k ? "on" : ""} onClick={() => { attempt.done(); setKind(k); }}>
              {k === "deploy" ? "Deploy" : k === "pr" ? "Pull request" : "Commit range"}
            </button>
          ))}
        </div>
        {kind === "deploy" && field("deploy_key", "Deploy", "DEPLOY-4821")}
        {kind === "pr" && <>{field("repository", "Repository", "checkout-api")}{field("pull_request", "PR #", "812")}</>}
        {kind === "range" && <>{field("service_key", "Service", "checkout-api")}{field("base_sha", "Base", "0742889")}{field("head_sha", "Head", "42d79c2")}</>}
        <button className="primary" type="submit" disabled={ask.isPending}>{ask.isPending ? "Checking…" : "Assess"}</button>
      </form>
      <Problem error={ask.error} />
    </Panel>
  );
}

export function ReleaseRiskReportPage() {
  const { id = "" } = useParams();
  const q = useQuery({
    queryKey: ["release-risk", id],
    queryFn: () => unwrap(api.GET("/api/v1/release-risk/{assessment_id}", { params: { path: { assessment_id: id } } })),
  });
  if (q.isPending) return <Loading what="report" />;
  if (q.error) return <Problem error={q.error} />;
  return <ReleaseRiskReport r={q.data} />;
}

export function ReleaseRiskReport({ r }: { r: S["ReleaseRiskOut"] }) {
  const rep = r.report;
  const evidence = r.evidence ?? [];
  const ev = new Set(evidence.map((e) => e.evidence_key));
  const target = rep ? rep.deploy_key ?? (rep.pull_request ? `${rep.repository} PR #${rep.pull_request}` : `${rep.service_key} ${rep.base_sha?.slice(0, 7)}..${rep.head_sha?.slice(0, 7)}`) : "";
  return (
    <Panel title={`Release risk ${target}`} actions={<Link to="/release-risk">New assessment</Link>}>
      <p>
        Band <Pill value={r.band ?? "UNKNOWN"} kind={`band-${r.band ?? "unknown"}`} /> · <Pill value={r.status} /> ·
        requested <Time at={r.requested_at} /> · <span className="muted small">advisory only — this never blocks a deploy</span>
      </p>
      {r.error && <p className="problem">{r.error}</p>}
      {rep && (
        <>
          <p className="small">
            Coverage <Pill value={rep.coverage} kind="coverage" /> · risk FOUND: <Pill value={rep.findings_band ?? "none"} kind={`band-${rep.findings_band ?? "none"}`} />
            {(rep.not_checked ?? []).length > 0 && <> · <strong>not checked:</strong> {(rep.not_checked ?? []).join(", ")} (unknown is never LOW)</>}
          </p>
          <h3>Factors</h3>
          <ul className="factors">
            {(rep.factors ?? []).map((x, i) => (
              <li key={i}>
                <Pill value={x.severity} kind={`sev-${x.severity}`} /> <span className="muted small">{x.kind}</span> {x.statement}{" "}
                {(x.evidence_ids ?? []).map((k) => <EvidenceChip key={k} evidenceKey={k} visible={ev.has(k)} />)}
              </li>
            ))}
          </ul>
          <h3>The way back</h3>
          <ul>{(rep.rollback ?? []).map((s, i) => <li key={i}><Pill value={s.kind} kind="rollback" /> {s.statement}</li>)}</ul>
        </>
      )}
      <h3>Evidence</h3>
      <ul className="evidence-simple">
        {evidence.map((e) => (
          <li key={e.evidence_key} id={`ev-${e.evidence_key}`}>
            <span className="mono small" title={e.evidence_key}>{shortKey(e.evidence_key)}</span> <Pill value={e.kind} kind="kind" /> {e.title}
            <Excerpt text={e.excerpt} />
          </li>
        ))}
      </ul>
    </Panel>
  );
}
