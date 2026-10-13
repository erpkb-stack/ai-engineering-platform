import { useEffect, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { configureAuth } from "../api/client";
import { useAuth, type Auth } from "./AuthContext";

const here = () => window.location.pathname + window.location.search + window.location.hash;

/**
 * What to do when the api says 401 (review H1). Only a MISSING or EXPIRED token is fixed by
 * signing in again. A valid token the api still refuses means our directory refuses this user
 * (deactivated, or uid/subject changed - ADR-027): signing in again would loop forever
 * (Keycloak's session is alive, so it hands out a new token at once). Say so instead, and
 * redirect at most once however many queries got the 401.
 */
export function unauthorizedHandler(auth: Auth, refuse: () => void, fail: (e: unknown) => void) {
  let inFlight: Promise<void> | null = null; // 401s that arrive together share ONE check
  let redirected = false;
  return (): Promise<void> => {
    if (redirected) return Promise.resolve();
    inFlight ??= (async () => {
      try {
        if (await auth.isSignedIn()) {
          refuse();
          return;
        }
        redirected = true;
        await auth.signIn(here());
      } catch (e) {
        redirected = false; // a later 401 may try again
        fail(e);
      } finally {
        inFlight = null;
      }
    })();
    return inFlight;
  };
}

/** Nothing renders and no api call is made before there is a signed-in user. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const [ready, setReady] = useState(false);
  const [refused, setRefused] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const started = useRef(false); // StrictMode runs effects twice

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const fail = (e: unknown) => setError(String(e));
    const onUnauthorized = unauthorizedHandler(auth, () => setRefused(true), fail);
    configureAuth(() => auth.token(), () => void onUnauthorized());
    auth
      .isSignedIn()
      .then((ok) => (ok ? setReady(true) : auth.signIn(here())))
      .catch(fail); // e.g. Keycloak down: say so, never "Signing in…" forever (review M3)
  }, [auth]);

  if (refused) {
    return (
      <div className="problem" role="alert">
        <strong>Your sign-in worked, but the platform does not accept your account.</strong>
        <p>It may be deactivated, or your identity changed in the directory. Ask an administrator.</p>
        <button onClick={() => void auth.signOut()}>Sign out</button>
      </div>
    );
  }
  if (error) return <p className="problem" role="alert">Sign-in failed: {error} <a href="/">Try again</a></p>;
  if (!ready) return <p className="muted">Signing in…</p>;
  return <>{children}</>;
}

export function Callback() {
  const auth = useAuth();
  const nav = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const once = useRef(false); // StrictMode runs effects twice; the code can be redeemed ONCE
  useEffect(() => {
    if (once.current) return;
    once.current = true;
    auth.completeSignIn().then((to) => nav(to, { replace: true }), (e: unknown) => setError(String(e)));
  }, [auth, nav]);
  return error ? (
    <p className="problem" role="alert">
      Sign-in failed: {error} <a href="/">Try again</a>
    </p>
  ) : (
    <p className="muted">Completing sign-in…</p>
  );
}
