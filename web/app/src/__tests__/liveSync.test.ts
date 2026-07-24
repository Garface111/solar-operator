import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  JOB_UPDATED_EVENT,
  LIVE_SYNC_EVENT,
  coalesce,
  handleLiveMessage,
  routeLiveEvent,
} from "../lib/liveSync";

describe("routeLiveEvent", () => {
  it("maps tenant-data event types to the fleet bus", () => {
    for (const type of [
      "clients.changed",
      "arrays.changed",
      "generation.updated",
      "capture.landed",
    ]) {
      expect(routeLiveEvent({ type })).toBe("fleet");
    }
  });

  it("maps job.updated to the job channel only", () => {
    expect(routeLiveEvent({ type: "job.updated" })).toBe("job");
  });

  it("ignores the connected handshake, unknown types, and garbage", () => {
    expect(routeLiveEvent({ type: "connected" })).toBe("ignore");
    expect(routeLiveEvent({ type: "something.future" })).toBe("ignore");
    expect(routeLiveEvent({})).toBe("ignore");
    expect(routeLiveEvent(null)).toBe("ignore");
    expect(routeLiveEvent("clients.changed")).toBe("ignore");
    expect(routeLiveEvent({ type: 42 })).toBe("ignore");
  });
});

describe("handleLiveMessage", () => {
  it("fires the fleet bus AND dispatches so:live-sync for fleet events", () => {
    const fireFleet = vi.fn();
    const dispatch = vi.fn();
    const payload = { type: "arrays.changed", client_id: 7 };
    handleLiveMessage(payload, fireFleet, dispatch);
    expect(fireFleet).toHaveBeenCalledTimes(1);
    expect(dispatch).toHaveBeenCalledExactlyOnceWith(LIVE_SYNC_EVENT, payload);
  });

  it("dispatches so:job-updated only (no fleet fire) for job.updated", () => {
    const fireFleet = vi.fn();
    const dispatch = vi.fn();
    const payload = { type: "job.updated", job_id: "abc" };
    handleLiveMessage(payload, fireFleet, dispatch);
    expect(fireFleet).not.toHaveBeenCalled();
    expect(dispatch).toHaveBeenCalledExactlyOnceWith(JOB_UPDATED_EVENT, payload);
  });

  it("does nothing for the connected handshake", () => {
    const fireFleet = vi.fn();
    const dispatch = vi.fn();
    handleLiveMessage({ type: "connected" }, fireFleet, dispatch);
    expect(fireFleet).not.toHaveBeenCalled();
    expect(dispatch).not.toHaveBeenCalled();
  });

  it("default dispatch reaches real window CustomEvent listeners", () => {
    const seen: unknown[] = [];
    const onLive = (e: Event) => seen.push((e as CustomEvent).detail);
    window.addEventListener(LIVE_SYNC_EVENT, onLive);
    try {
      const payload = { type: "clients.changed", client_id: 3 };
      handleLiveMessage(payload, vi.fn());
      expect(seen).toEqual([payload]);
    } finally {
      window.removeEventListener(LIVE_SYNC_EVENT, onLive);
    }
  });
});

describe("coalesce", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("collapses a burst of fires into one trailing call", () => {
    const fn = vi.fn();
    const c = coalesce(fn, 250);
    c.fire();
    vi.advanceTimersByTime(100);
    c.fire();
    vi.advanceTimersByTime(100);
    c.fire();
    expect(fn).not.toHaveBeenCalled();
    vi.advanceTimersByTime(250);
    expect(fn).toHaveBeenCalledTimes(1);
    // A later, separate event fires again.
    c.fire();
    vi.advanceTimersByTime(250);
    expect(fn).toHaveBeenCalledTimes(2);
  });

  it("cancel() drops a pending fire", () => {
    const fn = vi.fn();
    const c = coalesce(fn, 250);
    c.fire();
    c.cancel();
    vi.advanceTimersByTime(1000);
    expect(fn).not.toHaveBeenCalled();
  });
});
