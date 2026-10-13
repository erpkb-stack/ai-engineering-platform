import { useMutation, useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { api, unwrap, type S } from "../api/client";
import { can, useMe } from "../auth/me";
import { Excerpt, Loading, Panel, Pill, Problem, Time } from "../components/ui";
import { safeHref } from "./incident/Panels";

/** Knowledge search. Permission filtering happens in SQL, BEFORE retrieval (rag): the page
 *  only ever receives chunks this user may read - it hides nothing itself. */
export function SearchPage() {
  const me = useMe();
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<S["SearchMode"]>("hybrid");
  const search = useMutation({
    mutationFn: (body: S["SearchRequest"]) => unwrap(api.POST("/api/v1/search", { body })),
  });
  if (me.data && !can(me.data, "docs:read")) {
    return <Panel title="Knowledge search"><p className="muted">Search needs the docs:read permission.</p></Panel>;
  }
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (query.trim()) search.mutate({ query: query.trim(), k: 10, mode, rerank: null, filters: {} });
  };
  const r = search.data;
  return (
    <Panel title="Knowledge search">
      <form onSubmit={submit} className="form">
        <input aria-label="query" className="wide" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="e.g. checkout latency after deploy" />
        <select aria-label="mode" value={mode} onChange={(e) => setMode(e.target.value as S["SearchMode"])}>
          <option value="hybrid">hybrid</option><option value="keyword">keyword</option><option value="vector">vector</option>
        </select>
        <button className="primary" type="submit" disabled={search.isPending}>Search</button>
      </form>
      <Problem error={search.error} />
      {r && (
        <>
          <p className="small muted">
            {r.results.length} result(s) · mode {r.mode}{r.embedding_model ? ` · ${r.embedding_model}` : ""} ·
            rerank {r.rerank.applied ? `on (${r.rerank.model ?? "?"})` : "off"}
            {r.degraded && <> · <strong>degraded: {r.degraded}</strong></>}
          </p>
          <ol className="hits">
            {r.results.map((h) => (
              <li key={h.chunk_id}>
                <Link to={`/documents/${h.document_id}`}>{h.title}</Link> <Pill value={h.source} kind="source" />
                <span className="muted small"> rank {h.rank} · rrf {h.rrf_score.toFixed(3)}{h.similarity != null ? ` · sim ${h.similarity.toFixed(2)}` : ""}</span>
                <Excerpt text={h.content} />
              </li>
            ))}
          </ol>
        </>
      )}
    </Panel>
  );
}

export function DocumentPage() {
  const { id = "" } = useParams();
  const q = useQuery({
    queryKey: ["document", id],
    queryFn: () => unwrap(api.GET("/api/v1/documents/{document_id}", { params: { path: { document_id: id } } })),
  });
  if (q.isPending) return <Loading what="document" />;
  if (q.error) return <Problem error={q.error} />;
  const d = q.data;
  const href = safeHref(d.source_uri);
  return (
    <Panel title={d.title} actions={<Link to="/search">← search</Link>}>
      <p className="small muted">
        {d.source} · {d.department} · owner {d.owner} · v{d.version} · <Pill value={d.sensitivity} kind="sensitivity" /> ·
        indexed <Time at={d.indexed_at} /> · {d.chunk_count} chunks{href && <> · <a href={href} rel="noopener noreferrer" target="_blank">source</a></>}
      </p>
      <pre className="document">{d.content}</pre>
    </Panel>
  );
}
