// Integration test: "user clicks add-login twice in a row."
//
// Simulates the pick() flow from AddClientByLoginModal by directly
// exercising wipeCookiesAndWait + the about:blank → navigate sequence.
// Verifies that the second click also awaits the wipe before navigating,
// i.e. neither click ever sets location.href before its ack resolves.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { wipeCookiesAndWait, WIPE_TIMEOUT_MS } from "../lib/openPortalTab";

const GMP_URL = "https://greenmountainpower.com/account/";
const GMP_HOST = "greenmountainpower.com";

// Simulate pick() for one provider click:
//   1. "open about:blank" (mocked — returns a fake tab with a trackable href)
//   2. await wipeCookiesAndWait
//   3. set fakeTab.location.href = portalUrl
// Returns { fakeTab, navigatedAfterWipe: Promise<boolean> }
function simulatePick(
  postMessageSpy: (data: unknown) => void,
) {
  const fakeTab = { location: { href: "about:blank" } };
  let wipeReqId: string | null = null;
  let navigatedAfterWipeDone = false;
  let wipeResolved = false;

  // Intercept postMessage to capture reqId.
  (postMessageSpy as ReturnType<typeof vi.fn>).mockImplementationOnce((data: { reqId?: string }) => {
    wipeReqId = data?.reqId ?? null;
  });

  const navigatedAfterWipe = new Promise<boolean>((resolve) => {
    (async () => {
      await wipeCookiesAndWait(GMP_HOST);
      wipeResolved = true;
      fakeTab.location.href = GMP_URL;
      navigatedAfterWipeDone = fakeTab.location.href === GMP_URL && wipeResolved;
      resolve(navigatedAfterWipeDone);
    })();
  });

  return { fakeTab, navigatedAfterWipe, getReqId: () => wipeReqId };
}

describe("add-login twice in a row", () => {
  const listeners: Array<(e: MessageEvent) => void> = [];
  let postMessageSpy: (data: unknown) => void;

  beforeEach(() => {
    vi.useFakeTimers();
    listeners.length = 0;

    postMessageSpy = vi.fn((data: unknown) => { void data; });
    vi.spyOn(window, "postMessage").mockImplementation(postMessageSpy as typeof window.postMessage);
    vi.spyOn(window, "addEventListener").mockImplementation(
      (type: string, listener: EventListenerOrEventListenerObject) => {
        if (type === "message") listeners.push(listener as (e: MessageEvent) => void);
      },
    );
    vi.spyOn(window, "removeEventListener").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  function deliverAck(reqId: string) {
    const event = new MessageEvent("message", {
      source: window,
      data: { type: "SO_WIPE_COOKIES_ACK", reqId, ok: true, wiped: 2 },
    });
    for (const l of [...listeners]) l(event);
  }

  it("first click: navigates only after wipe ACK", async () => {
    const click1 = simulatePick(postMessageSpy);
    expect(click1.fakeTab.location.href).toBe("about:blank"); // not yet navigated

    // Deliver the ACK.
    deliverAck(click1.getReqId()!);
    const result = await click1.navigatedAfterWipe;
    expect(result).toBe(true);
    expect(click1.fakeTab.location.href).toBe(GMP_URL);
  });

  it("second click: also navigates only after its own wipe ACK", async () => {
    // First click, ack it immediately.
    const click1 = simulatePick(postMessageSpy);
    deliverAck(click1.getReqId()!);
    await click1.navigatedAfterWipe;

    // Second click — new wipe, new reqId.
    const click2 = simulatePick(postMessageSpy);
    expect(click2.fakeTab.location.href).toBe("about:blank"); // still blank

    deliverAck(click2.getReqId()!);
    const result = await click2.navigatedAfterWipe;
    expect(result).toBe(true);
    expect(click2.fakeTab.location.href).toBe(GMP_URL);
  });

  it("second click uses a fresh reqId (not polluted by the first)", async () => {
    const click1 = simulatePick(postMessageSpy);
    const reqId1 = click1.getReqId();

    // ACK click1.
    deliverAck(reqId1!);
    await click1.navigatedAfterWipe;

    const click2 = simulatePick(postMessageSpy);
    const reqId2 = click2.getReqId();

    expect(reqId1).not.toBe(reqId2);

    // Delivering click1's old reqId should NOT unblock click2.
    deliverAck(reqId1!);
    let click2Done = false;
    void click2.navigatedAfterWipe.then(() => { click2Done = true; });
    await vi.advanceTimersByTimeAsync(WIPE_TIMEOUT_MS - 10);
    expect(click2Done).toBe(false);

    // Deliver click2's own reqId — now it resolves.
    deliverAck(reqId2!);
    await click2.navigatedAfterWipe;
    expect(click2Done).toBe(true);
  });

  it("second click falls back to navigation after timeout if no ACK", async () => {
    // First click, ack it.
    const click1 = simulatePick(postMessageSpy);
    deliverAck(click1.getReqId()!);
    await click1.navigatedAfterWipe;

    // Second click — extension is silent this time.
    const click2 = simulatePick(postMessageSpy);
    expect(click2.fakeTab.location.href).toBe("about:blank");

    // Advance past timeout.
    await vi.advanceTimersByTimeAsync(WIPE_TIMEOUT_MS + 10);
    await click2.navigatedAfterWipe;

    // Should have navigated via the grace-period fallback.
    expect(click2.fakeTab.location.href).toBe(GMP_URL);
  });
});
