# ADR-046: The evidence graph is plain SVG over the hypotheses API; no graph library, no new endpoint

Status: Proposed (29d built 2026-10-28; sandbox only)
Date: 2026-10-28
Refines: ADR-028 (console), ADR-043 (demo step 8:00)

## Context
- The demo script (minute 8:00) promises "hypothesis <-> LOG/METRIC/DEPLOY/INCIDENT nodes".
  The edges already exist: GET /hypotheses returns per hypothesis its evidence links
  (key, kind, stance), filtered by the reader's permissions on the server (`hidden_edges`).
- Each hypothesis card already lists its own evidence chips. What a card cannot show is
  evidence SHARED across causes: the demo deploy SUPPORTS "the deploy caused it" and
  CONTRADICTS "the pool caused it". That is the reason for a graph, and the only one.

## Decision
- One component, `EvidenceGraph`, plain SVG: hypotheses left (by rank, rejected last and
  dashed), evidence right (grouped by kind; one node per evidence key however many links),
  one curve per stored edge. Stance = line style AND colour (solid green supports, dashed red
  contradicts), so it reads without colour.
- No new endpoint and no new data: it draws exactly what GET /hypotheses returned. Visibility
  is the server's decision; the graph only says how many links were hidden. A node is a link
  to the evidence card only when the reader's evidence list has that key.
- Hover/keyboard focus on a node keeps it and its neighbours bright and dims the rest. The
  same links are in a table under the picture (screen readers, copying).
- At most 60 evidence nodes are drawn (the most-linked first); the rest are counted and are in
  the table. Statements are SVG text (React escapes them); no foreignObject, no HTML.
- Demo: `evidence_graph` checks the data the screen draws: every node is evidence the reader
  can open, and the top cause is supported by >= 3 evidence kinds including DEPLOY.

## Tradeoffs
- No layout engine (dagre, cytoscape, d3-force): two fixed columns cannot show chains
  (evidence -> evidence) - there are none in the data model. A force layout would add a
  dependency and move nodes between renders.
- A two-column layout gets tall with many evidence items; the 60 cap and the table keep it
  usable. On a phone it scales down to unreadable; the console is desktop-first (ADR-028).
- The browser test needs an investigated incident first; with none it SKIPS and says why.
