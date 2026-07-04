// Unit tests for vaultStashLogin (dashboard → extension vault bridge helper).
//
// Verifies:
//   1. Posts SO_VAULT { op:"set", vendor, username, password } to the page.
//   2. Resolves "pending" when the extension stashes an intent (v1.9.109+).
//   3. Resolves "saved" on an immediate ok ACK (older extension).
//   4. Resolves "unavailable" on a refused ACK and on timeout (no extension).
//   5. Ignores ACKs with a non-matching reqId.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { vaultStashLogin } from "../lib/vaultBridge";

describe("vaultStashLogin", () => {
  let lastPostMessage: unknown;
  const messageListeners: Array<(e: MessageEvent) => void> = [];

  beforeEach(() => {
    vi.useFakeTimers();
    lastPostMessage = undefined;
    messageListeners.length = 0;

    vi.spyOn(window, "postMessage").mockImplementation((data) => {
      lastPostMessage = data;
    });
    vi.spyOn(window, "addEventListener").mockImplementation(
      (type: string, listener: EventListenerOrEventListenerObject) => {
        if (type === "message") messageListeners.push(listener as (e: MessageEvent) => void);
      },
    );
    vi.spyOn(window, "removeEventListener").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  function fireAck(reqId: string, ack: Record<string, unknown>) {
    const event = new MessageEvent("message", {
      source: window,
      data: { type: "SO_VAULT_ACK", reqId, op: "set", ...ack },
    });
    for (const l of messageListeners) l(event);
  }

  it("posts SO_VAULT set with the credential and never leaks it to our API", async () => {
    const promise = vaultStashLogin("gmp", "client@x.com", "hunter2");
    const posted = lastPostMessage as Record<string, unknown>;
    expect(posted.type).toBe("SO_VAULT");
    expect(posted.op).toBe("set");
    expect(posted.vendor).toBe("gmp");
    expect(posted.username).toBe("client@x.com");
    expect(posted.password).toBe("hunter2");
    expect(typeof posted.reqId).toBe("string");
    fireAck(posted.reqId as string, { ok: false, pending: true });
    await promise;
  });

  it("resolves 'pending' when the extension stashes the intent", async () => {
    const promise = vaultStashLogin("gmp", "u@x.com", "pw");
    const posted = lastPostMessage as { reqId: string };
    fireAck(posted.reqId, { ok: false, pending: true });
    await expect(promise).resolves.toBe("pending");
  });

  it("resolves 'saved' on an immediate ok ACK", async () => {
    const promise = vaultStashLogin("vec", "u@x.com", "pw");
    const posted = lastPostMessage as { reqId: string };
    fireAck(posted.reqId, { ok: true });
    await expect(promise).resolves.toBe("saved");
  });

  it("resolves 'unavailable' on a refused ACK", async () => {
    const promise = vaultStashLogin("gmp", "u@x.com", "pw");
    const posted = lastPostMessage as { reqId: string };
    fireAck(posted.reqId, { ok: false });
    await expect(promise).resolves.toBe("unavailable");
  });

  it("ignores a non-matching reqId, then resolves 'unavailable' on timeout", async () => {
    const promise = vaultStashLogin("gmp", "u@x.com", "pw");
    fireAck("some-other-id", { ok: true });
    let resolved = false;
    void promise.then(() => { resolved = true; });
    await vi.advanceTimersByTimeAsync(1490);
    expect(resolved).toBe(false);
    await vi.advanceTimersByTimeAsync(20);
    await expect(promise).resolves.toBe("unavailable");
  });
});
