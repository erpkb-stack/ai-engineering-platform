import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "../api/client";

export function Pill({ value, kind }: { value: string | null | undefined; kind?: string }) {
  if (!value) return <span className="pill pill-none">—</span>;
  return <span className={`pill pill-${(kind ?? value).toLowerCase().replace(/[^a-z0-9]+/g, "-")}`}>{value}</span>;
}

export function Time({ at }: { at: string | null | undefined }) {
  if (!at) return <span className="muted">—</span>;
  const d = new Date(at);
  return (
    <time dateTime={at} title={d.toISOString()} className="nowrap">
      {d.toISOString().slice(0, 16).replace("T", " ")}Z
    </time>
  );
}

/** Shows the api's problem+json (and its correlation id), never a blank panel. */
export function Problem({ error }: { error: unknown }) {
  if (!error) return null;
  if (error instanceof ApiError) {
    return (
      <div className="problem" role="alert">
        <strong>{error.status} {error.title}</strong>
        {error.detail && <span> — {error.detail}</span>}
        {error.correlationId && <div className="muted small">correlation id: {error.correlationId}</div>}
      </div>
    );
  }
  return <div className="problem" role="alert">{String(error)}</div>;
}

export function Loading({ what }: { what: string }) {
  return <p className="muted">Loading {what}…</p>;
}

export function Panel({ title, children, actions, id }: { title: string; children: ReactNode; actions?: ReactNode; id?: string }) {
  return (
    <section className="panel" id={id} aria-label={title}>
      <header className="panel-head">
        <h2>{title}</h2>
        {actions}
      </header>
      {children}
    </section>
  );
}

/**
 * One piece of evidence a finding cites. Links to the evidence panel when the reader can see
 * it; otherwise says so (evidence is readable per kind: the API hides what you may not read).
 */
export function EvidenceChip({ evidenceKey, kind, stance, visible }: {
  evidenceKey: string; kind?: string; stance?: "SUPPORTS" | "CONTRADICTS"; visible: boolean;
}) {
  const cls = `chip ${stance === "CONTRADICTS" ? "chip-contra" : "chip-support"}${visible ? "" : " chip-hidden"}`;
  const label = kind && !evidenceKey.startsWith(kind.toUpperCase()) ? `${kind} ${shortKey(evidenceKey)}` : shortKey(evidenceKey);
  return visible ? (
    <a className={cls} href={`#ev-${evidenceKey}`} title={`${evidenceKey}${stance ? ` — ${stance}` : ""} — jump to the evidence`}>
      {label}
    </a>
  ) : (
    <span className={cls} title={`${evidenceKey} — not in the evidence you may read`}>{label}</span>
  );
}

/** LOG-01a11e669d4170b19af65018367a8222-4 -> LOG…8222-4 (the full key is in the tooltip). */
export function shortKey(key: string): string {
  const m = /^([A-Z]+)-([0-9a-f]{24,})(-\d+)?$/.exec(key);
  return m ? `${m[1]}…${m[2]!.slice(-4)}${m[3] ?? ""}` : key;
}

/** Long untrusted text (a metric series, a log dump) folds after a few lines. */
export function Excerpt({ text }: { text: string }) {
  if (text.length <= 280) return <pre className="excerpt">{text}</pre>;
  return (
    <details className="excerpt-fold">
      <summary><pre className="excerpt">{text.slice(0, 280)}…</pre></summary>
      <pre className="excerpt">{text}</pre>
    </details>
  );
}

export function IncidentLink({ k }: { k: string }) {
  return <Link to={`/incidents/${k}`}>{k}</Link>;
}
