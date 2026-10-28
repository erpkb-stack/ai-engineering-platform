import { useState } from "react";
import type { Hypothesis } from "../../api/client";
import { shortKey } from "../../components/ui";

/* Phase 29d (ADR-046): hypothesis <-> evidence as a graph. Plain SVG, no graph library: two
 * columns (hypotheses by rank, evidence grouped by kind), one line per stored edge. The data
 * is exactly GET /hypotheses: the server already removed edges this reader may not see
 * (`hidden_edges`), so nothing here decides visibility - it only draws what came back.
 * Stance is shown by line style AND colour (solid = supports, dashed = contradicts). */

const ROW = 30;
const TOP = 34;
const W = 760;
const LEFT_W = 270;
const RIGHT_X = 500;
const RIGHT_W = 250;
const MAX_EVIDENCE = 60;

type Edge = { h: string; key: string; kind: string; stance: "SUPPORTS" | "CONTRADICTS" };

const clip = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

export function EvidenceGraph({ items, visible }: { items: Hypothesis[]; visible: Set<string> }) {
  const [focus, setFocus] = useState<string | null>(null);
  const hs = [...items].sort((a, b) => Number(a.status === "REJECTED") - Number(b.status === "REJECTED") || a.rank - b.rank);
  const edges: Edge[] = hs.flatMap((h) => h.edges.map((e) => ({ h: h.id, key: e.evidence_key, kind: e.kind, stance: e.stance })));
  const hidden = hs.reduce((n, h) => n + h.hidden_edges, 0);
  if (edges.length === 0) {
    return <p className="muted">No evidence links yet{hidden > 0 ? ` (${hidden} hidden: evidence your role may not read)` : ""}.</p>;
  }
  // evidence nodes: the most-linked first when there are too many, then grouped by kind
  const degree = new Map<string, number>();
  for (const e of edges) degree.set(e.key, (degree.get(e.key) ?? 0) + 1);
  const kindOf = new Map(edges.map((e) => [e.key, e.kind]));
  const ranked = [...degree.keys()].sort((a, b) => degree.get(b)! - degree.get(a)! || a.localeCompare(b));
  const drawn = new Set(ranked.slice(0, MAX_EVIDENCE));
  const notDrawn = ranked.length - drawn.size;
  const kinds = [...new Set([...drawn].map((k) => kindOf.get(k)!))].sort();
  const rows: ({ kind: string } | { key: string })[] = [];
  for (const kind of kinds) {
    rows.push({ kind });
    for (const key of [...drawn].filter((k) => kindOf.get(k) === kind).sort()) rows.push({ key });
  }
  const yE = new Map<string, number>();
  rows.forEach((r, i) => { if ("key" in r) yE.set(r.key, TOP + i * ROW); });
  const yH = new Map(hs.map((h, i) => [h.id, TOP + i * ROW]));
  const height = TOP + Math.max(rows.length, hs.length) * ROW + 6;
  const lit = (e: Edge) => focus === null || focus === e.h || focus === e.key;
  // a focused node keeps itself AND the nodes at the other end of its links bright
  const near = new Set(focus === null ? [] : [focus, ...edges.filter(lit).flatMap((e) => [e.h, e.key])]);
  const dim = (id: string) => (focus !== null && !near.has(id) ? " dim" : "");

  return (
    <div className="evidence-graph">
      <p className="small muted legend">
        <span className="lg lg-support">supports</span> <span className="lg lg-contra">contradicts</span> · hover or focus a node to follow its links
        {hidden > 0 && <> · {hidden} link(s) hidden: evidence your role may not read</>}
        {notDrawn > 0 && <> · {notDrawn} less-linked evidence item(s) not drawn (see the table)</>}
      </p>
      <svg role="img" aria-label={`Evidence graph: ${hs.length} hypotheses, ${drawn.size} evidence items, ${edges.length} links`}
        viewBox={`0 0 ${W} ${height}`} width="100%" preserveAspectRatio="xMinYMin meet">
        <text x={10} y={18} className="col-head">Hypotheses (by rank)</text>
        <text x={RIGHT_X} y={18} className="col-head">Evidence (by kind)</text>
        {edges.filter((e) => drawn.has(e.key)).map((e, i) => {
          const y1 = yH.get(e.h)! + ROW / 2 - 4;
          const y2 = yE.get(e.key)! + ROW / 2 - 4;
          const mid = (LEFT_W + RIGHT_X) / 2;
          return (
            <path key={i} data-stance={e.stance} className={`edge ${e.stance === "CONTRADICTS" ? "edge-contra" : "edge-support"}${lit(e) ? "" : " dim"}`}
              d={`M${LEFT_W + 10},${y1} C${mid},${y1} ${mid},${y2} ${RIGHT_X},${y2}`} />
          );
        })}
        {hs.map((h) => {
          const y = yH.get(h.id)!;
          return (
            <g key={h.id} className={`node node-h status-${h.status.toLowerCase()}${dim(h.id)}`} tabIndex={0}
              aria-label={`hypothesis ${h.rank}: ${h.status}, ${h.confidence}`}
              onMouseEnter={() => setFocus(h.id)} onMouseLeave={() => setFocus(null)} onFocus={() => setFocus(h.id)} onBlur={() => setFocus(null)}>
              <title>{`#${h.rank} [${h.confidence}, ${h.status}] ${h.statement}`}</title>
              <rect x={10} y={y} width={LEFT_W} height={ROW - 8} rx={4} />
              <text x={18} y={y + 15}>{clip(`#${h.rank} ${h.confidence} · ${h.statement}`, 44)}</text>
            </g>
          );
        })}
        {rows.map((r, i) => {
          const y = TOP + i * ROW;
          if ("kind" in r) return <text key={`k-${r.kind}`} x={RIGHT_X} y={y + 15} className="kind-head">{r.kind}</text>;
          const seen = visible.has(r.key);
          const box = (
            <>
              <title>{`${r.key}${seen ? " — jump to the evidence" : " — not in the evidence you may read"}`}</title>
              <rect x={RIGHT_X + 14} y={y} width={RIGHT_W - 14} height={ROW - 8} rx={4} />
              <text x={RIGHT_X + 22} y={y + 15}>{shortKey(r.key)}</text>
            </>
          );
          const handlers = { onMouseEnter: () => setFocus(r.key), onMouseLeave: () => setFocus(null), onFocus: () => setFocus(r.key), onBlur: () => setFocus(null) };
          return seen ? (
            <a key={r.key} href={`#ev-${r.key}`} className={`node node-e${dim(r.key)}`} {...handlers}>{box}</a>
          ) : (
            <g key={r.key} className={`node node-e node-hidden${dim(r.key)}`} tabIndex={0} {...handlers}>{box}</g>
          );
        })}
      </svg>
      <details>
        <summary>The same links as a table ({edges.length})</summary>
        <table className="grid">
          <thead><tr><th>Hypothesis</th><th>Stance</th><th>Evidence</th><th>Kind</th></tr></thead>
          <tbody>
            {edges.map((e, i) => {
              const h = hs.find((x) => x.id === e.h)!;
              return (
                <tr key={i}>
                  <td>#{h.rank} {clip(h.statement, 80)}</td>
                  <td>{e.stance === "SUPPORTS" ? "supports" : "contradicts"}</td>
                  <td className="mono">{e.key}</td>
                  <td>{e.kind}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </details>
    </div>
  );
}
