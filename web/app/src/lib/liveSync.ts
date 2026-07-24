/**
 * liveSync.ts — the single freshness plane.
 *
 * One shared SSE subscription to /v1/events for the whole page, translating
 * server-pushed tenant events into the existing DOM bus so every surface
 * (roster table, canvas, chips, billing, reports) refreshes without waiting
 * for the 15s poll. SandboxCanvas keeps its own stream (it needs per-event
 * reveal animations + toasts); this one exists so the table surfaces stay
 * fresh even when the canvas isn't mounted.
 *
 * Event routing:
 *   clients.changed / arrays.changed / generation.updated / capture.landed
 *     → notifyFleetChanged("server")  (coalesced: one bus fire per 250ms burst)
 *     → so:live-sync CustomEvent, raw payload in detail (per event, uncoalesced)
 *   job.updated
 *     → so:job-updated CustomEvent only (job progress isn't fleet data)
 *   connected / anything else → ignored
 *
 * Reliability: reconnect forever with capped backoff (1s→2s→…→30s). While
 * signed out, quietly re-check for a session every few seconds so the stream
 * self-arms the moment the operator signs in — never a permanent give-up.
 * No extra polling here: the existing 15s roster poll is the gap-fill.
 */
import {
  UNAUTHORIZED_EVENT,
  clearSession,
  eventsUrl,
  getSession,
} from "./api";
import { notifyFleetChanged } from "./fleetEvents";

export const LIVE_SYNC_EVENT = "so:live-sync";
export const JOB_UPDATED_EVENT = "so:job-updated";

/** Server event types that mean "some surface's fleet data went stale". */
const FLEET_EVENT_TYPES: ReadonlySet<string> = new Set([
  "clients.changed",
  "arrays.changed",
  "generation.updated",
  "capture.landed", // legacy — still published alongside the granular types
]);

export interface LiveEvent {
  type: string;
  [key: string]: unknown;
}

/** Pure mapping from a raw SSE payload to what the bus should do with it. */
export function routeLiveEvent(payload: unknown): "fleet" | "job" | "ignore" {
  if (!payload || typeof payload !== "object") return "ignore";
  const type = (payload as LiveEvent).type;
  if (typeof type !== "string") return "ignore";
  if (type === "job.updated") return "job";
  if (FLEET_EVENT_TYPES.has(type)) return "fleet";
  return "ignore"; // "connected" handshake + unknown future types
}

/** Trailing-edge coalescer: N calls within `ms` collapse into one `fn()`.
 *  A burst (an import touching 20 arrays) becomes a single bus fire. */
export function coalesce(fn: () => void, ms: number): {
  fire: () => void;
  cancel: () => void;
} {
  let timer: ReturnType<typeof setTimeout> | null = null;
  return {
    fire() {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        timer = null;
        fn();
      }, ms);
    },
    cancel() {
      if (timer) clearTimeout(timer);
      timer = null;
    },
  };
}

/** Route one parsed SSE payload onto the page. Exported (with injectable
 *  sinks) so the event→bus mapping is unit-testable without a stream. */
export function handleLiveMessage(
  payload: unknown,
  fireFleet: () => void,
  dispatch: (name: string, detail: unknown) => void = (name, detail) => {
    try {
      window.dispatchEvent(new CustomEvent(name, { detail }));
    } catch {
      /* SSR / private mode — ignore */
    }
  },
): void {
  const route = routeLiveEvent(payload);
  if (route === "fleet") {
    fireFleet();
    dispatch(LIVE_SYNC_EVENT, payload);
  } else if (route === "job") {
    dispatch(JOB_UPDATED_EVENT, payload);
  }
}

// One stream per page — startLiveSync is idempotent while a stream is live.
let stopCurrent: (() => void) | null = null;

/**
 * Start the shared SSE subscription. Returns a stop function. Calling again
 * while running returns the same stop (no second stream). Same fetch-stream
 * approach as SandboxCanvas: Authorization header (browser EventSource
 * can't), eventsUrl() so the embed skips the buffering Netlify proxy.
 */
export function startLiveSync(): () => void {
  if (stopCurrent) return stopCurrent;

  let canceled = false;
  let retryDelay = 1000;
  let abort: AbortController | null = null;
  const fleetFire = coalesce(() => notifyFleetChanged("server"), 250);

  const connect = async () => {
    if (canceled) return;
    const token = getSession();
    if (!token) {
      // Signed out (or the session died) — re-check cheaply until one exists
      // so the stream arms itself right after sign-in. No network involved.
      setTimeout(connect, 3000);
      return;
    }

    const ac = new AbortController();
    abort = ac;

    try {
      const resp = await fetch(eventsUrl(), {
        headers: { Authorization: `Bearer ${token}` },
        signal: ac.signal,
      });

      if (resp.status === 401) {
        clearSession();
        window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
        // Fall through to the retry below — getSession() is now empty, so we
        // drop into the cheap signed-out re-check loop instead of hammering.
      } else if (!resp.ok || !resp.body) {
        throw new Error(`SSE response ${resp.status}`);
      } else {
        retryDelay = 1000; // reset backoff on successful connect

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";

        while (!canceled) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const lines = buf.split("\n");
          buf = lines.pop() ?? "";
          for (const line of lines) {
            if (!line.startsWith("data: ")) continue;
            try {
              handleLiveMessage(JSON.parse(line.slice(6)), fleetFire.fire);
            } catch {
              /* malformed JSON — ignore */
            }
          }
        }
      }
    } catch (err) {
      if (canceled) return;
      if (err instanceof DOMException && err.name === "AbortError") return;
      // Network error — fall through to reconnect with backoff.
    }

    if (!canceled) {
      setTimeout(connect, retryDelay);
      retryDelay = Math.min(retryDelay * 2, 30_000);
    }
  };

  void connect();

  const stop = () => {
    if (stopCurrent === stop) stopCurrent = null;
    canceled = true;
    abort?.abort();
    fleetFire.cancel();
  };
  stopCurrent = stop;
  return stop;
}
