import { useQuery } from "@tanstack/react-query";
import { api, unwrap, type Me } from "../api/client";

export function useMe() {
  return useQuery({
    queryKey: ["me"],
    queryFn: () => unwrap(api.GET("/api/v1/me")),
    staleTime: 60_000,
  });
}

/** UI gating only: the server re-checks every permission anyway (rules: frontend.md). */
export function can(me: Me | undefined, perm: string): boolean {
  return !!me && me.permissions.includes(perm);
}

export const actorOf = (me: Me | undefined): string => (me ? `user:${me.user_id}` : "");
