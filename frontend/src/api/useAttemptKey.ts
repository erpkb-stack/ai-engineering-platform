import { useRef } from "react";
import { newIdempotencyKey } from "./client";

/** The same Idempotency-Key until the action SUCCEEDS (or its input changes): a retry after
 *  an error or a timeout is the same attempt, so the server runs it at most once. */
export function useAttemptKey() {
  const key = useRef(newIdempotencyKey());
  return {
    headers: () => ({ "Idempotency-Key": key.current }),
    done: () => {
      key.current = newIdempotencyKey();
    },
  };
}
