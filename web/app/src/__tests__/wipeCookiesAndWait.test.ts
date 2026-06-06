// Unit tests for wipeCookiesAndWait (Pattern A cookie-wipe helper).
//
// Verifies:
//   1. Resolves when SO_WIPE_COOKIES_ACK arrives with the matching reqId.
//   2. Ignores ACKs with non-matching reqIds.
//   3. Resolves after WIPE_TIMEOUT_MS when no ACK arrives (extension absent).
//   4. Posts SO_WIPE_COOKIES with the supplied domain.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { wipeCookiesAndWait, WIPE_TIMEOUT_MS } from "../lib/openPortalTab";

describe("wipeCookiesAndWait", () => {
  // Capture the last postMessage call so tests can inspect it.
  let lastPostMessage: unknown;
  // Capture all addEventListener calls so we can fire synthetic events.
  const messageListeners: Array<(e: MessageEvent) => void> = [];

  beforeEach(() => {
    vi.useFakeTimers();
    lastPostMessage = undefined;
    messageListeners.length = 0;

    vi.spyOn(window, "postMessage").mockImplementation((data) => {
      lastPostMessage = data;
      // Prevent the real postMessage from firing — we'll deliver ACKs manually.
    });

    vi.spyOn(window, "addEventListener").mockImplementation(
      (type: string, listener: EventListenerOrEventListenerObject) => {
        if (type === "message") {
          messageListeners.push(listener as (e: MessageEvent) => void);
        }
      },
    );

    vi.spyOn(window, "removeEventListener").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  function fireAck(reqId: string) {
    const event = new MessageEvent("message", {
      source: window,
      data: { type: "SO_WIPE_COOKIES_ACK", reqId, ok: true, wiped: 3 },
    });
    for (const l of messageListeners) l(event);
  }

  it("posts SO_WIPE_COOKIES with the correct domain", async () => {
    const promise = wipeCookiesAndWait("greenmountainpower.com");
    // Fire the ACK immediately.
    const posted = lastPostMessage as { type: string; domain: string; reqId: string };
    expect(posted.type).toBe("SO_WIPE_COOKIES");
    expect(posted.domain).toBe("greenmountainpower.com");
    expect(typeof posted.reqId).toBe("string");

    fireAck(posted.reqId);
    await promise;
  });

  it("resolves when the matching ACK arrives", async () => {
    const promise = wipeCookiesAndWait("greenmountainpower.com");
    const posted = lastPostMessage as { reqId: string };
    fireAck(posted.reqId);
    await expect(promise).resolves.toBeUndefined();
  });

  it("ignores ACKs with a different reqId", async () => {
    const promise = wipeCookiesAndWait("smarthub.coop");
    fireAck("not-the-right-id");

    let resolved = false;
    void promise.then(() => { resolved = true; });

    // Tick just under the timeout — should still be pending.
    await vi.advanceTimersByTimeAsync(WIPE_TIMEOUT_MS - 10);
    expect(resolved).toBe(false);

    // Now advance past the timeout — should resolve via fallback.
    await vi.advanceTimersByTimeAsync(20);
    await promise;
    expect(resolved).toBe(true);
  });

  it(`resolves after ${WIPE_TIMEOUT_MS}ms when no ACK arrives (extension absent)`, async () => {
    const promise = wipeCookiesAndWait("greenmountainpower.com");

    let resolved = false;
    void promise.then(() => { resolved = true; });

    await vi.advanceTimersByTimeAsync(WIPE_TIMEOUT_MS - 1);
    expect(resolved).toBe(false);

    await vi.advanceTimersByTimeAsync(2);
    await promise;
    expect(resolved).toBe(true);
  });

  it("does not resolve twice if ACK arrives after timeout fires", async () => {
    const promise = wipeCookiesAndWait("greenmountainpower.com");
    const posted = lastPostMessage as { reqId: string };

    // Let the timeout fire first.
    await vi.advanceTimersByTimeAsync(WIPE_TIMEOUT_MS + 10);
    await promise; // resolved by timeout

    // Now fire the ACK late — should be ignored (no double-resolve crash).
    expect(() => fireAck(posted.reqId)).not.toThrow();
  });
});
