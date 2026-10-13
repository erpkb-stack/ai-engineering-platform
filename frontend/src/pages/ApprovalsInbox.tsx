import { Link } from "react-router-dom";
import { can, useMe } from "../auth/me";
import { IncidentLink, Loading, Panel, Pill, Problem, Time } from "../components/ui";
import { INBOX_POLL_MS } from "../config";
import { countLabel, INBOX_LIMIT, minutesLeft, useNow, usePendingApprovals } from "./approvals";

/**
 * Pending requests across incidents, the one about to lapse first. Deciding happens on the
 * incident page, NEXT TO the evidence - an approval from a list of titles is a rubber stamp.
 */
export function ApprovalsInbox() {
  const me = useMe();
  const q = usePendingApprovals();
  const approver = can(me.data, "actions:approve");
  const now = useNow();
  const rows = q.data ?? [];
  return (
    <Panel title="Approvals inbox" actions={<span className="muted small">{countLabel(rows.length)} pending · refreshes every {INBOX_POLL_MS / 1000} s</span>}>
      {!approver && me.data && <p className="muted">You can see pending requests; only an incident commander can decide them.</p>}
      <Problem error={q.error} />
      {q.isPending ? <Loading what="approvals" /> : (
        <table className="grid">
          <thead>
            <tr><th>Expires in</th><th>Incident</th><th>Action</th><th>Target</th><th>Requested by</th><th>Requested</th><th /></tr>
          </thead>
          <tbody>
            {rows.map((a) => (
              <tr key={a.id} className={minutesLeft(a.expires_at, now) <= 5 ? "urgent" : ""}>
                <td><strong>{minutesLeft(a.expires_at, now)} min</strong></td>
                <td><IncidentLink k={a.incident_key} /></td>
                <td><Pill value={a.action} kind="action" /></td>
                <td className="mono">{String(a.action_args.deploy_key ?? "")}</td>
                <td className="mono small">{a.requested_by}{a.source === "investigation" ? " (investigation)" : ""}</td>
                <td><Time at={a.requested_at} /></td>
                <td><Link to={`/incidents/${a.incident_key}#approval-${a.id}`}>Review evidence →</Link></td>
              </tr>
            ))}
            {q.data?.length === 0 && <tr><td colSpan={7} className="muted">Nothing is waiting for a decision.</td></tr>}
          </tbody>
        </table>
      )}
      {rows.length >= INBOX_LIMIT && <p className="muted small">Showing the {INBOX_LIMIT} that expire first.</p>}
    </Panel>
  );
}
