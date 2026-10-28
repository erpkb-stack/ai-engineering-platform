import type { Hypothesis } from "../../api/client";
import { EvidenceChip, Pill } from "../../components/ui";

const str = (v: unknown): string | null => (typeof v === "string" && v ? v : null);

/** One AI finding: TYPE, confidence band, status, supporting AND contradicting evidence. */
export function HypothesisCard({ h, visible }: { h: Hypothesis; visible: Set<string> }) {
  const d = h.detail as Record<string, unknown>;
  const supports = h.edges.filter((e) => e.stance === "SUPPORTS");
  const contra = h.edges.filter((e) => e.stance === "CONTRADICTS");
  return (
    <article className={`finding finding-${h.status.toLowerCase()}`} aria-label={`hypothesis ${h.rank}`}>
      <header>
        <span className="rank">#{h.rank}</span>
        <Pill value="Hypothesis" kind="type" />
        <Pill value={h.confidence} kind={`conf-${h.confidence}`} />
        <Pill value={h.status} />
        <span className="muted small">
          by {h.produced_by}
          {str(d.ranked_by) ? ` · ranked by ${str(d.ranked_by)}` : ""}
        </span>
      </header>
      <p className="statement">{h.statement}</p>
      {str(d.rubric) && <p className="small">Rubric (code): {str(d.rubric)}</p>}
      {str(d.explanation) && <p className="small">{str(d.explanation)}</p>}
      {str(d.explanation_removed) && (
        <p className="small removed">
          Removed by code ({str(d.explanation_removed_why) ?? "failed a check"}): <del>{str(d.explanation_removed)}</del>
        </p>
      )}
      {str(d.critic_verdict) && (
        <p className="small">
          Critic: <Pill value={str(d.critic_verdict)} kind="critic" />
          {str(d.critic_missing) && (
            <>
              {" "}missing: <em>{str(d.critic_missing)}</em>
            </>
          )}
        </p>
      )}
      <div className="edges">
        <span className="edge-label">Supporting ({supports.length})</span>
        {supports.map((e) => (
          <EvidenceChip key={e.evidence_key} evidenceKey={e.evidence_key} kind={e.kind} stance="SUPPORTS" visible={visible.has(e.evidence_key)} />
        ))}
      </div>
      <div className="edges">
        <span className="edge-label">Contradicting ({contra.length})</span>
        {contra.length === 0 ? (
          <span className="muted small">none found</span>
        ) : (
          contra.map((e) => (
            <EvidenceChip key={e.evidence_key} evidenceKey={e.evidence_key} kind={e.kind} stance="CONTRADICTS" visible={visible.has(e.evidence_key)} />
          ))
        )}
      </div>
      {h.hidden_edges > 0 && (
        <p className="muted small">{h.hidden_edges} more evidence link(s) cite evidence your role may not read.</p>
      )}
    </article>
  );
}

export function HypothesesList({ items, visible }: { items: Hypothesis[]; visible: Set<string> }) {
  const live = items.filter((h) => h.status !== "REJECTED").sort((a, b) => a.rank - b.rank);
  const out = items.filter((h) => h.status === "REJECTED");
  if (items.length === 0) return <p className="muted">No hypotheses yet — run an investigation.</p>;
  return (
    <>
      {live.map((h) => (
        <HypothesisCard key={h.id} h={h} visible={visible} />
      ))}
      {out.length > 0 && (
        <details>
          <summary>Ruled out ({out.length})</summary>
          {out.map((h) => (
            <HypothesisCard key={h.id} h={h} visible={visible} />
          ))}
        </details>
      )}
    </>
  );
}
