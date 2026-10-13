import { useInfiniteQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { api, unwrap, type S } from "../api/client";
import { IncidentLink, Loading, Panel, Pill, Problem, Time } from "../components/ui";

const STATUSES: S["IncidentStatus"][] = ["OPEN", "INVESTIGATING", "AWAITING_REVIEW", "MITIGATED", "RESOLVED", "CLOSED"];
const SEVERITIES: S["Severity"][] = ["SEV1", "SEV2", "SEV3", "SEV4"];

export function IncidentsPage() {
  const [params, setParams] = useSearchParams();
  const status = (params.get("status") || undefined) as S["IncidentStatus"] | undefined;
  const severity = (params.get("severity") || undefined) as S["Severity"] | undefined;

  const q = useInfiniteQuery({
    queryKey: ["incidents", status, severity],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      unwrap(api.GET("/api/v1/incidents", { params: { query: { status, severity, limit: 25, cursor: pageParam } } })),
    getNextPageParam: (last) => last.next_cursor ?? undefined, // cursor paging, never offsets
  });
  const set = (k: string, v: string) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v);
    else next.delete(k);
    setParams(next);
  };
  const rows = q.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <Panel
      title="Incidents"
      actions={
        <div className="filters">
          <label>
            Status{" "}
            <select value={status ?? ""} onChange={(e) => set("status", e.target.value)}>
              <option value="">any</option>
              {STATUSES.map((s) => <option key={s}>{s}</option>)}
            </select>
          </label>
          <label>
            Severity{" "}
            <select value={severity ?? ""} onChange={(e) => set("severity", e.target.value)}>
              <option value="">any</option>
              {SEVERITIES.map((s) => <option key={s}>{s}</option>)}
            </select>
          </label>
        </div>
      }
    >
      <Problem error={q.error} />
      {q.isPending ? <Loading what="incidents" /> : (
        <table className="grid">
          <thead>
            <tr><th>Incident</th><th>Sev</th><th>Status</th><th>Title</th><th>Services</th><th>Detected</th></tr>
          </thead>
          <tbody>
            {rows.map((i) => (
              <tr key={i.id}>
                <td><IncidentLink k={i.key} /></td>
                <td><Pill value={i.severity} /></td>
                <td><Pill value={i.status} /></td>
                <td>{i.title}</td>
                <td className="small">{i.affected_services.join(", ")}</td>
                <td><Time at={i.detected_at} /></td>
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan={6} className="muted">No incidents match.</td></tr>}
          </tbody>
        </table>
      )}
      {q.hasNextPage && (
        <button onClick={() => void q.fetchNextPage()} disabled={q.isFetchingNextPage}>
          {q.isFetchingNextPage ? "Loading…" : "Load more"}
        </button>
      )}
    </Panel>
  );
}
