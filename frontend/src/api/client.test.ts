import { describe, expect, it, vi } from "vitest";
import { fakeApi } from "../test/harness";
import { renderHook } from "@testing-library/react";
import { api, ApiError, configureAuth, unwrap } from "./client";
import { useAttemptKey } from "./useAttemptKey";
import { safeReturnTo } from "../auth/oidcAuth";

describe("api client", () => {
  it("sends the access token and returns typed data", async () => {
    const f = fakeApi({ "GET /api/v1/me": () => ({ user_id: "u", email: "e", name: "n", roles: ["SRE"], groups: [], permissions: [] }) });
    const me = await unwrap(api.GET("/api/v1/me"));
    expect(me.roles).toEqual(["SRE"]);
    expect(f.calls[0]!.headers.get("Authorization")).toBe("Bearer test-token");
  });

  it("a problem+json becomes an ApiError with the correlation id", async () => {
    fakeApi({ "GET /api/v1/me": () => [403, { title: "Forbidden", status: 403, detail: "Missing permission", correlation_id: "abc" }] });
    const err = await unwrap(api.GET("/api/v1/me")).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 403, title: "Forbidden", detail: "Missing permission", correlationId: "abc" });
  });

  it("a 401 sends the user to sign in again", async () => {
    fakeApi({ "GET /api/v1/me": () => [401, { title: "Authentication required", status: 401 }] });
    const unauthorized = vi.fn();
    configureAuth(async () => "expired", unauthorized);
    await unwrap(api.GET("/api/v1/me")).catch(() => {});
    expect(unauthorized).toHaveBeenCalledOnce();
  });

  it("no token, no Authorization header (never 'Bearer null')", async () => {
    const f = fakeApi({ "GET /api/v1/me": () => [401, {}] });
    configureAuth(async () => null, () => {});
    await unwrap(api.GET("/api/v1/me")).catch(() => {});
    expect(f.calls[0]!.headers.get("Authorization")).toBeNull();
  });

  it("a retry after an error re-sends the SAME idempotency key; a success starts a new one", () => {
    const { result } = renderHook(() => useAttemptKey());
    const first = result.current.headers()["Idempotency-Key"];
    expect(result.current.headers()["Idempotency-Key"]).toBe(first); // network error -> click again
    result.current.done(); // the server answered: that action is over
    expect(result.current.headers()["Idempotency-Key"]).not.toBe(first);
  });
});

describe("after sign-in, go back only to a path on this app", () => {
  it.each([
    [{ returnTo: "/incidents/INC-10019#approval-1" }, "/incidents/INC-10019#approval-1"],
    [{ returnTo: "https://evil.example/" }, "/"],
    [{ returnTo: "//evil.example/" }, "/"],
    [{ returnTo: "/\\evil.example/" }, "/"],
    [{ returnTo: "javascript:alert(1)" }, "/"],
    [{ returnTo: 42 }, "/"],
    [undefined, "/"],
  ])("%j -> %s", (state, expected) => {
    expect(safeReturnTo(state)).toBe(expected);
  });
});
