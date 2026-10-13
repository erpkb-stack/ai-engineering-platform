import { createContext, useContext } from "react";

/** What the app needs from sign-in. The real one is OIDC; tests pass a fake. */
export interface Auth {
  token(): Promise<string | null>;
  signIn(returnTo: string): Promise<void>;
  completeSignIn(): Promise<string>; // -> the path to go back to
  signOut(): Promise<void>;
  isSignedIn(): Promise<boolean>;
}

export const AuthContext = createContext<Auth | null>(null);

export function useAuth(): Auth {
  const a = useContext(AuthContext);
  if (!a) throw new Error("AuthContext missing");
  return a;
}
