// Dev defaults; override with VITE_OIDC_ISSUER for another realm. Never secrets: a public client.
export const OIDC_ISSUER: string =
  (import.meta.env.VITE_OIDC_ISSUER as string | undefined) ?? "http://localhost:8090/realms/aeoi";
export const OIDC_CLIENT_ID = "aeoi-console";
export const INBOX_POLL_MS = 15_000; // owner decision (Phase 17): poll, push is [Prod]
