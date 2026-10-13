/**
 * Sign-in with Keycloak: Authorization Code + PKCE (S256) with the PUBLIC client aeoi-console
 * (ADR-027). Tokens live in MEMORY only (never localStorage): a reload signs in again through
 * the Keycloak session, which is a redirect, not a password prompt. The PKCE verifier and state
 * sit in sessionStorage for the length of one redirect (oidc-client-ts removes them after).
 */
import { InMemoryWebStorage, UserManager, WebStorageStateStore } from "oidc-client-ts";
import { OIDC_CLIENT_ID, OIDC_ISSUER } from "../config";

export const userManager = new UserManager({
  authority: OIDC_ISSUER,
  client_id: OIDC_CLIENT_ID,
  redirect_uri: `${window.location.origin}/callback`,
  post_logout_redirect_uri: `${window.location.origin}/`,
  response_type: "code",
  scope: "openid",
  userStore: new WebStorageStateStore({ store: new InMemoryWebStorage() }),
  stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
  automaticSilentRenew: true, // refresh token, before the 5-minute access token expires
  monitorSession: false,
});
