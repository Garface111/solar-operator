// Regression tests for the "login looks broken" bug: a single expired session
// fanned out across several concurrent authed requests, each dispatching
// UNAUTHORIZED_EVENT and (in some tabs) raising a sticky red error toast — so
// the user saw a STACK of "session expired" messages that lingered over the
// login screen and even survived a successful sign-in.
//
// The fix: UNAUTHORIZED_EVENT is dispatched at most ONCE per session (re-armed
// on setSession), and UnauthorizedError is the typed signal screens use to stay
// silent. These tests lock in the dedupe + re-arm behavior.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  UNAUTHORIZED_EVENT,
  UnauthorizedError,
  getAccount,
  setSession,
  clearSession,
} from "../lib/api";

function mock401Once() {
  return vi.fn().mockResolvedValue({
    ok: false,
    status: 401,
    json: async () => ({ detail: "Session expired — sign in again" }),
    text: async () => "",
    clone() {
      return this;
    },
  } as unknown as Response);
}

describe("401 handling — single bounce per session", () => {
  beforeEach(() => {
    clearSession();
    setSession("stale-token"); // arm the notifier as if we were logged in
  });

  afterEach(() => {
    vi.restoreAllMocks();
    clearSession();
  });

  it("dispatches UNAUTHORIZED_EVENT only ONCE across concurrent 401s", async () => {
    vi.stubGlobal("fetch", mock401Once());
    const handler = vi.fn();
    window.addEventListener(UNAUTHORIZED_EVENT, handler);

    // Three concurrent authed calls all hit 401 on the same dead session.
    const results = await Promise.allSettled([
      getAccount(),
      getAccount(),
      getAccount(),
    ]);

    window.removeEventListener(UNAUTHORIZED_EVENT, handler);

    // Every call rejects with the typed UnauthorizedError…
    for (const r of results) {
      expect(r.status).toBe("rejected");
      expect((r as PromiseRejectedResult).reason).toBeInstanceOf(UnauthorizedError);
    }
    // …but the user is only bounced to login ONCE.
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("re-arms after a fresh setSession so the NEXT expiry bounces again", async () => {
    vi.stubGlobal("fetch", mock401Once());
    const handler = vi.fn();
    window.addEventListener(UNAUTHORIZED_EVENT, handler);

    await getAccount().catch(() => {}); // first expiry → 1 bounce
    expect(handler).toHaveBeenCalledTimes(1);

    await getAccount().catch(() => {}); // still same session → no new bounce
    expect(handler).toHaveBeenCalledTimes(1);

    setSession("new-token-after-login"); // user signs back in
    await getAccount().catch(() => {}); // a later expiry → bounces again
    expect(handler).toHaveBeenCalledTimes(2);

    window.removeEventListener(UNAUTHORIZED_EVENT, handler);
  });
});
