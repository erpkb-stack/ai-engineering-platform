import type { Evidence, S } from "../../api/client";
import { ApiError } from "../../api/client";
import { EvidenceChip, Excerpt, Pill, Problem, shortKey, Time } from "../../components/ui";

/** Evidence text and links come from logs, tickets and tools: UNTRUSTED. Text is rendered as
 *  text (React escapes it); a link only when it is http(s) - never `javascript:` or `data:`. */
export function safeHref(uri: string | null | undefined): string | null {
  if (!uri) return null;
  try {
    const u = new URL(uri);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : null;
  } catch {
    return null;
  }
}

export function EvidenceList({ items }: { items: Evidence[] }) {
  if (items.length === 0) return <p className="muted">No evidence you may read.</p>;
  return (
    <table className="grid">
      <thead><tr><th>Key</th><th>Kind</th><th>Source</th><th>Observed</th><th>What</th></tr></thead>
      <tbody>
        {items.map((e) => {
          const href = safeHref(e.uri);
          return (
            <tr key={e.id} id={`ev-${e.evidence_key}`} className="evidence-row">
              <td className="mono small nowrap" title={e.evidence_key}>{shortKey(e.evidence_key)}</td>
              <td><Pill value={e.kind} kind="kind" /></td>
              <td className="small">{e.source_system}<div className="muted">{e.retrieved_by}</div></td>
              <td><Time at={e.observed_at} /></td>
              <td>
                <div>{href ? <a href={href} rel="noopener noreferrer" target="_blank">{e.title}</a> : e.title}</div>
                <Excerpt text={e.excerpt} />
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export function TimelineList({ items, visible }: { items: S["TimelineEntry"][]; visible: Set<string> }) {
  if (items.length === 0) return <p className="muted">No events.</p>;
  return (
    <ol className="timeline">
      {items.map((t) => (
        <li key={t.id}>
          <Time at={t.occurred_at} /> <Pill value={t.source} kind="source" /> <strong>{t.event_type}</strong> {t.summary}
          <span className="muted small"> — {t.actor}</span>
          {t.evidence_key && <> <EvidenceChip evidenceKey={t.evidence_key} visible={visible.has(t.evidence_key)} /></>}
        </li>
      ))}
    </ol>
  );
}

const SECTION_TITLES: Record<S["PmSection"]["name"], string> = {
  summary: "Summary", what_happened: "What happened", why: "Why", detection: "Detection",
  went_well: "What went well", went_poorly: "What went poorly",
};

// Phase 29c (ADR-045): why code removed a model sentence (the server's RemovedSentence.reason)
const REMOVAL_REASON: Record<S["RemovedSentence"]["reason"], string> = {
  markup: "link or markup syntax", too_short: "too short", no_valid_ref: "cites no observation",
  no_evidence: "no evidence behind its refs", unknown_number: "a number not in the facts",
  bad_category: "unknown action category",
};

/** What code took out of the model's draft, and why: model text that FAILED a check, so it is
 *  shown struck through as plain text, apart from the report - never as part of it. */
export function RemovedList({ doc }: { doc: S["PostmortemDoc"] }) {
  const removed = doc.removed ?? [];
  if (!doc.removed_kept) {
    return <p className="muted small">Removed text was not kept for this postmortem (written before Phase 29c): counts only.</p>;
  }
  if (removed.length === 0) return <p className="muted small">Code removed nothing from the model&apos;s draft.</p>;
  return (
    <details className="removed">
      <summary>Removed by code ({removed.length}) - not part of the report</summary>
      {doc.removed_redacted > 0 && (
        <p className="muted small">{doc.removed_redacted} removed text(s) hidden: they need read access to every evidence kind of this incident.</p>
      )}
      <ul>
        {removed.map((r, i) => (
          <li key={i}>
            <span className="small">{r.section === "action_items" ? "Action items" : SECTION_TITLES[r.section]} · {REMOVAL_REASON[r.reason]}{r.detail ? ` (${r.detail})` : ""}: </span>
            <del>{r.text}</del>
          </li>
        ))}
      </ul>
    </details>
  );
}

export function PostmortemView({ doc, error, visible }: { doc: S["PostmortemDoc"] | undefined; error: unknown; visible: Set<string> }) {
  if (error instanceof ApiError && error.status === 404) return <p className="muted">No postmortem yet.</p>;
  if (error) return <Problem error={error} />;
  if (!doc) return null;
  const chips = (keys: string[] | undefined) => (keys ?? []).map((k) => <EvidenceChip key={k} evidenceKey={k} visible={visible.has(k)} />);
  return (
    <div className="postmortem">
      <p className="small muted">
        Written by {doc.written_by}{doc.model ? ` (${doc.model})` : ""} · generated <Time at={doc.generated_at} /> ·
        every sentence cites observations; code dropped {doc.sentences_dropped} uncited and rejected {doc.numbers_rejected} unknown number(s)
      </p>
      <dl className="facts">
        <dt>Trigger → symptom</dt><dd>{doc.minutes_trigger_to_symptom ?? "—"} min</dd>
        <dt>Time to detect</dt><dd>{doc.minutes_to_detect ?? "—"} min</dd>
        <dt>Time to resolve</dt><dd>{doc.minutes_to_resolve ?? "—"} min</dd>
      </dl>
      <h3>Root cause <Pill value={doc.root_cause_state} kind="state" /></h3>
      {doc.root_cause ? (
        <p>
          <Pill value="Hypothesis" kind="type" /> <Pill value={doc.root_cause.confidence} kind={`conf-${doc.root_cause.confidence}`} />{" "}
          {doc.root_cause.statement} {doc.root_cause.confirmed_by_human && <Pill value="confirmed by a human" kind="human" />}
        </p>
      ) : <p className="muted">No root cause survived validation.</p>}
      {(doc.sections ?? []).map((s) => (
        <section key={s.name}>
          <h3>{SECTION_TITLES[s.name]}</h3>
          {(s.sentences ?? []).map((x, i) => <p key={i}>{x.text} {chips(x.evidence_keys)}</p>)}
        </section>
      ))}
      {doc.redacted_sentences > 0 && <p className="muted small">{doc.redacted_sentences} sentence(s) cite evidence your role may not read.</p>}
      <h3>Action items</h3>
      <ul>{(doc.action_items ?? []).map((a, i) => <li key={i}><Pill value={a.category} kind="category" /> {a.text} {chips(a.evidence_keys)}</li>)}</ul>
      {(doc.ruled_out ?? []).length > 0 && <><h3>Ruled out</h3><ul>{(doc.ruled_out ?? []).map((r) => <li key={r}>{r}</li>)}</ul></>}
      <RemovedList doc={doc} />
    </div>
  );
}

const ACTIVE = new Set(["PENDING", "QUEUED", "RUNNING"]);
export const isActive = (status: string) => ACTIVE.has(status);
