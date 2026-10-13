/** Typed api client. Every type comes from openapi.json (generated) - no hand-typed DTOs. */
import createClient, { type Middleware } from "openapi-fetch";
import type { components, paths } from "./schema";

export type S = components["schemas"];
export type Incident = S["IncidentOut"];
export type Approval = S["ApprovalOut"];
export type Hypothesis = S["HypothesisRecord"];
export type Evidence = S["aeoi_models__api__incidents__EvidenceOut"];
export type Me = S["Me"];

/** RFC 7807 problem from the api, with the correlation id the user should quote. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly title: string,
    readonly detail: string,
    readonly correlationId: string | null,
  ) {
    super(detail || title);
  }
}

type TokenSource = () => Promise<string | null>;
let tokenSource: TokenSource = async () => null;
let onUnauthorized: () => void = () => {};

/** Wired by the auth layer: where the access token comes from, what to do on a 401. */
export function configureAuth(source: TokenSource, unauthorized: () => void): void {
  tokenSource = source;
  onUnauthorized = unauthorized;
}

const auth: Middleware = {
  async onRequest({ request }) {
    const token = await tokenSource();
    if (token) request.headers.set("Authorization", `Bearer ${token}`);
    return request;
  },
  async onResponse({ response }) {
    if (response.status === 401) onUnauthorized();
    return response;
  },
};

export const api = createClient<paths>({
  baseUrl: typeof window === "undefined" ? "http://localhost" : window.location.origin,
  fetch: (req) => globalThis.fetch(req), // resolved per call (tests replace fetch)
});
api.use(auth);

function toError(response: Response, body: unknown): ApiError {
  const p = (body ?? {}) as { title?: string; detail?: unknown; correlation_id?: string };
  const detail = typeof p.detail === "string" ? p.detail : p.detail ? JSON.stringify(p.detail) : "";
  return new ApiError(response.status, p.title ?? response.statusText, detail,
    p.correlation_id ?? response.headers.get("X-Correlation-ID"));
}

/** Data or a thrown ApiError: React Query then shows the problem, never a blank screen. */
export async function unwrap<T>(
  call: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await call;
  if (!response.ok || error !== undefined) throw toError(response, error);
  return data as T;
}

/** Idempotency key for ONE attempt of a user action. Hold it with `useAttemptKey` so that a
 *  click after a network error RE-SENDS the same key (the server dedupes) - review M5. */
export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}
