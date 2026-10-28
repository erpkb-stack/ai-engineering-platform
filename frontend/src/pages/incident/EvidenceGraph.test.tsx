import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Hypothesis } from "../../api/client";
import { EvidenceGraph } from "./EvidenceGraph";

const DEPLOY = "DEPLOY-01a11e669d3d73c387bac8a6c3a1b460-0";
const METRIC = "METRIC-01a11e669d6b78a7a5f08109ccb76fdc-0";
const LOG = "LOG-01a11e669d4170b19af65018367a8222-4";

const h = (over: Partial<Hypothesis>): Hypothesis => ({
  id: "h1", investigation_id: "i1", key: "h1", rank: 1, statement: "Deploy DEPLOY-4821 may have caused the regression",
  confidence: "HIGH", status: "VALIDATED", produced_by: "hypothesis", created_at: "2026-10-09T02:03:00Z",
  detail: {}, edges: [], hidden_edges: 0, ...over,
});

// the demo shape: the deploy SUPPORTS the deploy cause and CONTRADICTS the pool cause
const demo = () => [
  h({ edges: [{ evidence_key: DEPLOY, kind: "DEPLOY", stance: "SUPPORTS" }, { evidence_key: LOG, kind: "LOG", stance: "SUPPORTS" }] }),
  h({ id: "h2", key: "h2", rank: 2, confidence: "MEDIUM", status: "PROPOSED", statement: "DB connection pool saturation",
      edges: [{ evidence_key: METRIC, kind: "METRIC", stance: "SUPPORTS" }, { evidence_key: DEPLOY, kind: "DEPLOY", stance: "CONTRADICTS" }] }),
];

describe("evidence graph (Phase 29d, ADR-046)", () => {
  it("one node per hypothesis and per linked evidence item, one line per stored edge", () => {
    const { container } = render(<EvidenceGraph items={demo()} visible={new Set([DEPLOY, LOG, METRIC])} />);
    expect(screen.getByRole("img", { name: "Evidence graph: 2 hypotheses, 3 evidence items, 4 links" })).toBeInTheDocument();
    expect(container.querySelectorAll(".node-h")).toHaveLength(2);
    expect(container.querySelectorAll(".node-e")).toHaveLength(3); // the shared DEPLOY is ONE node
    expect(container.querySelectorAll("path.edge")).toHaveLength(4);
    // grouped by kind, kinds in order
    expect([...container.querySelectorAll(".kind-head")].map((e) => e.textContent)).toEqual(["DEPLOY", "LOG", "METRIC"]);
  });

  it("stance is drawn by line style, not by colour alone", () => {
    const { container } = render(<EvidenceGraph items={demo()} visible={new Set()} />);
    const contra = container.querySelectorAll('path[data-stance="CONTRADICTS"]');
    expect(contra).toHaveLength(1);
    expect(contra[0]).toHaveClass("edge-contra"); // dashed in CSS
    expect(container.querySelectorAll('path[data-stance="SUPPORTS"].edge-support')).toHaveLength(3);
  });

  it("evidence this reader may read links to it; the rest says so and links nowhere", () => {
    const { container } = render(<EvidenceGraph items={demo()} visible={new Set([DEPLOY])} />);
    const links = container.querySelectorAll("a.node-e");
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute("href", `#ev-${DEPLOY}`);
    expect(container.querySelectorAll(".node-hidden")).toHaveLength(2);
    expect(container.querySelector(".node-hidden title")?.textContent).toMatch(/not in the evidence you may read/);
  });

  it("focusing a hypothesis dims every link that is not its own", () => {
    const { container } = render(<EvidenceGraph items={demo()} visible={new Set()} />);
    const pool = screen.getByLabelText("hypothesis 2: PROPOSED, MEDIUM");
    fireEvent.focus(pool);
    const lit = [...container.querySelectorAll("path.edge:not(.dim)")];
    expect(lit).toHaveLength(2); // the pool's METRIC support + the DEPLOY contradiction
    // its own evidence stays bright, the rest dims (the LOG is only the deploy cause's)
    const bright = [...container.querySelectorAll(".node-e:not(.dim) title")].map((t) => t.textContent!.split(" ")[0]);
    expect(bright.sort()).toEqual([DEPLOY, METRIC].sort());
    expect(container.querySelector(".node-h.dim")).not.toBeNull(); // the other hypothesis
    fireEvent.blur(pool);
    expect(container.querySelectorAll("path.edge.dim")).toHaveLength(0);
  });

  it("says how many links the server hid, and shows nothing it did not send", () => {
    const items = demo();
    items[1] = { ...items[1]!, hidden_edges: 3 };
    render(<EvidenceGraph items={items} visible={new Set()} />);
    expect(screen.getByText(/3 link\(s\) hidden: evidence your role may not read/)).toBeInTheDocument();
  });

  it("model text in a statement is text inside the SVG, never markup", () => {
    const evil = '<img src=x onerror="alert(1)"> [x](javascript:alert(2))';
    const { container } = render(<EvidenceGraph items={[h({ statement: evil, produced_by: "critic",
      edges: [{ evidence_key: LOG, kind: "LOG", stance: "SUPPORTS" }] })]} visible={new Set()} />);
    expect(container.querySelector("img, script, foreignObject")).toBeNull();
    expect(container.querySelector('a[href^="javascript"]')).toBeNull();
    expect(container.querySelector(".node-h title")?.textContent).toContain('onerror="alert(1)"');
  });

  it("no edges: a sentence instead of an empty picture", () => {
    render(<EvidenceGraph items={[h({ hidden_edges: 2 })]} visible={new Set()} />);
    expect(screen.getByText("No evidence links yet (2 hidden: evidence your role may not read).")).toBeInTheDocument();
  });

  it("the same links are in a table for screen readers and for copying", () => {
    render(<EvidenceGraph items={demo()} visible={new Set()} />);
    const table = screen.getByText("The same links as a table (4)").closest("details")!;
    const rows = within(table).getAllByRole("row").slice(1).map((r) => r.textContent);
    expect(rows).toContain(`#2 DB connection pool saturationcontradicts${DEPLOY}DEPLOY`);
  });

  it("too many evidence items: the most-linked are drawn and the rest are counted", () => {
    const many = Array.from({ length: 65 }, (_, i) => ({ evidence_key: `LOG-01a11e669d4170b19af650183${String(i).padStart(5, "0")}-0`, kind: "LOG", stance: "SUPPORTS" as const }));
    const { container } = render(<EvidenceGraph items={[h({ edges: many })]} visible={new Set()} />);
    expect(container.querySelectorAll(".node-e")).toHaveLength(60);
    expect(screen.getByText(/5 less-linked evidence item\(s\) not drawn/)).toBeInTheDocument();
  });
});
