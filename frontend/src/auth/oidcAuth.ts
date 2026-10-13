import type { Auth } from "./AuthContext";

/** Where to go after sign-in: only a path on THIS app, never an absolute or protocol-relative
 *  URL carried in the state (no open redirect through the sign-in round trip). */
export function safeReturnTo(state: unknown): string {
  const to = (state as { returnTo?: unknown } | undefined)?.returnTo;
  return typeof to === "string" && to.startsWith("/") && !to.startsWith("//") && !to.startsWith("/\\") ? to : "/";
}
import { userManager } from "./oidc";

export const oidcAuth: Auth = {
  async token() {
    const u = await userManager.getUser();
    return u && !u.expired ? u.access_token : null;
  },
  async signIn(returnTo) {
    await userManager.signinRedirect({ state: { returnTo } });
  },
  async completeSignIn() {
    const u = await userManager.signinRedirectCallback();
    return safeReturnTo(u.state);
  },
  async signOut() {
    await userManager.signoutRedirect();
  },
  async isSignedIn() {
    const u = await userManager.getUser();
    return !!u && !u.expired;
  },
};
