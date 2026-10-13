import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { renderAt } from "../test/harness";
import { AuthContext, type Auth } from "./AuthContext";
import { RequireAuth, unauthorizedHandler } from "./RequireAuth";

const fakeAuth = (over: Partial<Auth> = {}): Auth => ({
  token: async () => "t", signIn: vi.fn(async () => {}), completeSignIn: async () => "/",
  signOut: vi.fn(async () => {}), isSignedIn: async () => true, ...over,
});

describe("a 401 from the api (review H1)", () => {
  it("with a VALID token: the directory refuses this user - say so, never re-sign-in (no loop)", async () => {
    const auth = fakeAuth({ isSignedIn: async () => true });
    const refuse = vi.fn();
    const onUnauthorized = unauthorizedHandler(auth, refuse, vi.fn());
    await Promise.all([onUnauthorized(), onUnauthorized(), onUnauthorized()]); // 3 queries got 401
    expect(refuse).toHaveBeenCalled();
    expect(auth.signIn).not.toHaveBeenCalled();
  });

  it("with an expired token: sign in again, ONCE however many queries failed", async () => {
    const auth = fakeAuth({ isSignedIn: async () => false });
    const onUnauthorized = unauthorizedHandler(auth, vi.fn(), vi.fn());
    await Promise.all([onUnauthorized(), onUnauthorized(), onUnauthorized(), onUnauthorized()]);
    await onUnauthorized();
    expect(auth.signIn).toHaveBeenCalledOnce();
  });

  it("if that sign-in fails, the error is reported (and a later 401 may try again)", async () => {
    const auth = fakeAuth({ isSignedIn: async () => false, signIn: vi.fn(async () => { throw new Error("Keycloak down"); }) });
    const fail = vi.fn();
    const onUnauthorized = unauthorizedHandler(auth, vi.fn(), fail);
    await onUnauthorized();
    expect(fail).toHaveBeenCalledWith(new Error("Keycloak down"));
    await onUnauthorized();
    expect(auth.signIn).toHaveBeenCalledTimes(2);
  });
});

describe("RequireAuth", () => {
  it("shows nothing protected before sign-in, and reports a failed sign-in (no endless 'Signing in…')", async () => {
    const auth = fakeAuth({ isSignedIn: async () => false, signIn: async () => { throw new Error("metadata fetch failed"); } });
    renderAt(<AuthContext.Provider value={auth}><RequireAuth><p>secret page</p></RequireAuth></AuthContext.Provider>);
    expect(await screen.findByRole("alert")).toHaveTextContent("metadata fetch failed");
    expect(screen.queryByText("secret page")).toBeNull();
  });

  it("renders the app once signed in", async () => {
    renderAt(<AuthContext.Provider value={fakeAuth()}><RequireAuth><p>the app</p></RequireAuth></AuthContext.Provider>);
    expect(await screen.findByText("the app")).toBeInTheDocument();
  });
});
