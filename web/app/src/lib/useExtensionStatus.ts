// useExtensionStatus.ts — global hook that knows whether the Solar
// Operator Chrome extension is installed AND its current version/pairing
// state.
//
// Two detection paths:
//
// 1. PASSIVE — so_bridge.js posts SO_EXTENSION_PRESENT on injection
//    (document_start). If we listen from app boot we catch it on the
//    first hot/cold load.
//
// 2. ACTIVE — when we need a fresh status (e.g. before the Add Client
//    flow), we postMessage SO_STATUS_REQUEST and wait briefly for an
//    ACK. The ACK includes tenantKeySet (paired or not) and lastSyncAt.
//
// Status semantics:
//   "unknown"         — we haven't probed yet
//   "absent"          — probed, no response within timeout (no extension)
//   "present-unpaired"— extension installed but no tenantKey configured
//   "present-paired"  — extension installed and paired with this tenant
//
// Components that need extension state should call useExtensionStatus()
// instead of rolling their own postMessage handlers.

import { useEffect, useState, useCallback } from "react";

export type ExtensionStatus =
  | "unknown"
  | "absent"
  | "present-unpaired"
  | "present-paired";

interface ExtensionState {
  status: ExtensionStatus;
  version: string | null;
  lastSyncAt: string | null;
  /** Force a fresh active probe (returns a promise that resolves when
   *  status updates). */
  probe: () => Promise<ExtensionStatus>;
}

const PROBE_TIMEOUT_MS = 600;

function genReqId(): string {
  try {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const c: any = typeof crypto !== "undefined" ? crypto : null;
    if (c && typeof c.randomUUID === "function") return c.randomUUID();
  } catch { /* fall through */ }
  return `s-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

// Module-level cache so multiple components share one state snapshot
// without each one re-probing on mount.
let cached: { status: ExtensionStatus; version: string | null; lastSyncAt: string | null } = {
  status: "unknown",
  version: null,
  lastSyncAt: null,
};
const subscribers = new Set<() => void>();

function notify() {
  subscribers.forEach((fn) => fn());
}

function setCache(next: typeof cached) {
  cached = next;
  notify();
}

// Always-listening passive detection — set up once at module load.
if (typeof window !== "undefined") {
  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const data = event.data;
    if (!data || typeof data !== "object") return;
    if (data.type === "SO_EXTENSION_PRESENT") {
      // We know it's there; we don't yet know pairing from this msg.
      // Mark as paired-or-not based on existing cache; an active probe
      // will refine it.
      const nextStatus: ExtensionStatus =
        cached.status === "present-paired" || cached.status === "present-unpaired"
          ? cached.status
          : "present-unpaired";
      setCache({
        status: nextStatus,
        version: typeof data.version === "string" ? data.version : cached.version,
        lastSyncAt: cached.lastSyncAt,
      });
    }
    if (data.type === "SO_CAPTURE_LANDED") {
      // A capture just landed → we're definitely paired and just synced.
      setCache({
        status: "present-paired",
        version: cached.version,
        lastSyncAt: typeof data.at === "string" ? data.at : new Date().toISOString(),
      });
    }
  });
}

function activeProbe(): Promise<ExtensionStatus> {
  return new Promise((resolve) => {
    const reqId = genReqId();
    let settled = false;
    const handler = (event: MessageEvent) => {
      if (event.source !== window) return;
      const d = event.data;
      if (!d || d.type !== "SO_STATUS_ACK" || d.reqId !== reqId) return;
      window.removeEventListener("message", handler);
      if (settled) return;
      settled = true;
      const status: ExtensionStatus = d.ok
        ? d.tenantKeySet
          ? "present-paired"
          : "present-unpaired"
        : "present-unpaired";
      setCache({
        status,
        version: d.version || cached.version,
        lastSyncAt: d.lastSyncAt || cached.lastSyncAt,
      });
      resolve(status);
    };
    window.addEventListener("message", handler);
    window.postMessage({ type: "SO_STATUS_REQUEST", reqId }, "*");
    window.setTimeout(() => {
      if (settled) return;
      settled = true;
      window.removeEventListener("message", handler);
      // No ACK → extension absent (unless we already knew it was here
      // from a passive SO_EXTENSION_PRESENT; preserve that).
      const next: ExtensionStatus =
        cached.status === "present-paired" || cached.status === "present-unpaired"
          ? cached.status
          : "absent";
      setCache({ ...cached, status: next });
      resolve(next);
    }, PROBE_TIMEOUT_MS);
  });
}

export function useExtensionStatus(autoProbe = true): ExtensionState {
  const [, force] = useState({});
  useEffect(() => {
    const sub = () => force({});
    subscribers.add(sub);
    return () => {
      subscribers.delete(sub);
    };
  }, []);

  useEffect(() => {
    if (autoProbe && cached.status === "unknown") {
      void activeProbe();
    }
  }, [autoProbe]);

  const probe = useCallback(() => activeProbe(), []);
  return {
    status: cached.status,
    version: cached.version,
    lastSyncAt: cached.lastSyncAt,
    probe,
  };
}
