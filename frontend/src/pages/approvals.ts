import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, unwrap } from "../api/client";
import { INBOX_POLL_MS } from "../config";

/** ONE query for the nav badge and the inbox page: one request per 15 s, not two. Paused while
 *  the tab is hidden (TanStack default), so a forgotten tab does not poll all night. */
export function usePendingApprovals(enabled = true) {
  return useQuery({
    queryKey: ["approvals", "pending"],
    queryFn: () => unwrap(api.GET("/api/v1/approvals", { params: { query: { status: "PENDING", limit: INBOX_LIMIT } } })),
    refetchInterval: INBOX_POLL_MS,
    refetchIntervalInBackground: false,
    enabled,
  });
}

export const INBOX_LIMIT = 100; // the api's maximum: a full page means "100 or more"

/** Re-render every `ms` (identical poll data does not re-render: the countdown would freeze). */
export function useNow(ms = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

export const countLabel = (n: number) => (n >= INBOX_LIMIT ? `${INBOX_LIMIT}+` : String(n));

export function minutesLeft(expiresAt: string, now = Date.now()): number {
  return Math.max(0, Math.round((new Date(expiresAt).getTime() - now) / 60_000));
}
