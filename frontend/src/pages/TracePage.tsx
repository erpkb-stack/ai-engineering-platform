import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { Link, useParams } from "react-router-dom";
import { api, unwrap, type S } from "../api/client";
import { Loading, Panel, Pill, Problem, Time } from "../components/ui";
import { isActive } from "./incident/Panels";

type Exec = S["ExecutionTraceOut"];

/** Bar position in % of the whole run: where each agent started and how long it took. */
export function waterfall(execs: Exec[], now = Date.now()) {
  const t = (s: string | null | undefined) => (s ? new Date(s).getTime() : now);
  const start = Math.min(...execs.map((e) => t(e.started_at)));
  const end = Math.max(...execs.map((e) => t(e.finished_at)));
  const span = Math.max(end - start, 1);
  return execs.map((e) => ({
    e,
    left: ((t(e.started_at) - start) / span) * 100,
    width: Math.max(((t(e.finished_at) - t(e.started_at)) / span) * 100, 0.5),
  }));
}

export function TracePage() {
  const { id = "" } = useParams();
  const path = { params: { path: { investigation_id: id } } };
  const inv = useQuery({
    queryKey: ["investigation", id],
    queryFn: () => unwrap(api.GET("/api/v1/investigations/{investigation_id}", path)),
    refetchInterval: (q) => (q.state.data && isActive(q.state.data.status) ? 5_000 : false),
  });
  const trace = useQuery({
    queryKey: ["investigation", id, "trace"],
    queryFn: () => unwrap(api.GET("/api/v1/investigations/{investigation_id}/trace", path)),
    refetchInterval: inv.data && isActive(inv.data.status) ? 5_000 : false,
  });
  // the last executions are written as the run ends: fetch the trace once more then (M1)
  const qc = useQueryClient();
  const active = !!inv.data && isActive(inv.data.status);
  const wasActive = useRef(false);
  useEffect(() => {
    if (wasActive.current && !active) void qc.invalidateQueries({ queryKey: ["investigation", id, "trace"] });
    wasActive.current = active;
  }, [active, qc, id]);
  if (inv.isPending) return <Loading what="investigation" />;
  if (inv.error) return <Problem error={inv.error} />;
  const execs = trace.data?.executions ?? [];
  const totalTokens = execs.reduce((n, e) => n + e.input_tokens + e.output_tokens, 0);

  return (
    <Panel title={`Investigation ${id.slice(0, 8)}…`}>
      <p className="small">
        <Pill value={inv.data.status} /> started <Time at={inv.data.started_at} /> · finished <Time at={inv.data.finished_at} /> ·
        spent <span className="mono">${inv.data.spent_usd}</span> · {totalTokens} tokens ·{" "}
        <Link to={`/incidents/${inv.data.incident_id}`}>incident →</Link>
      </p>
      {inv.data.error && <p className="problem">{inv.data.error}</p>}
      <Problem error={trace.error} />
      {execs.length === 0 ? <p className="muted">No agent executions yet.</p> : (
        <table className="grid waterfall">
          <thead>
            <tr><th>Agent</th><th>Status</th><th>Model · prompt</th><th>Tokens in/out</th><th>Latency</th><th>Cost</th><th className="bar-col">Timeline</th></tr>
          </thead>
          <tbody>
            {waterfall(execs).map(({ e, left, width }) => (
              <tr key={e.execution_id}>
                <td>
                  <details>
                    <summary>{e.agent} <span className="muted small">v{e.agent_version}</span></summary>
                    {e.redacted && <p className="muted small">Messages redacted for your role.</p>}
                    {e.error && <p className="problem small">{e.error}</p>}
                    {(e.messages ?? []).map((m) => (
                      <div key={m.seq} className="msg"><span className="muted small">{m.role}</span><pre>{m.content}</pre></div>
                    ))}
                  </details>
                </td>
                <td><Pill value={e.status} /></td>
                <td className="small mono">{e.model ?? "code only"}{e.prompt_id ? ` · ${e.prompt_id} v${e.prompt_version}` : ""}</td>
                <td className="mono small">{e.input_tokens}/{e.output_tokens}</td>
                <td className="mono small">{e.latency_ms ?? "—"} ms</td>
                <td className="mono small">${e.cost_usd}</td>
                <td className="bar-col">
                  <div className="bar-track">
                    <div className={`bar bar-${e.status.toLowerCase()}`} style={{ left: `${left}%`, width: `${width}%` }} />
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
