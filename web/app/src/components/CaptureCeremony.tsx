// CaptureCeremony.tsx — the "holy shit, my data appeared" moment.
//
// Triggered for fresh post-onboarding users (URL has ?fresh=1) and every
// time a SO_CAPTURE_LANDED broadcast arrives from the extension. Renders a
// cascading list of captured clients+arrays with a soft "log into another
// account to add the next client" prompt — turning the 50-account dream
// into a feedback loop where every login is a small dopamine spike.
//
// Lives near the top of ClientsSection so it's the first thing a fresh
// user sees. Dismissable; remembers dismissal in sessionStorage so it
// doesn't reappear after manual refresh during the same session.

import { useCallback, useEffect, useRef, useState } from "react";
import { openPortalTab, gmpPortalUrl } from "../lib/openPortalTab";
import { useExtensionStatus } from "../lib/useExtensionStatus";
import { listClients, listArrays, type ClientRow } from "../lib/api";
import { useToast } from "../ui/Toast";

type Provider = "gmp" | "vec";

interface CaptureEvent {
  id: number;
  provider: Provider;
  accountCount: number;
  at: string;
  /** Set when backend signals result=updated — resolveEvent will toast instead of showing a row. */
  isUpdate?: boolean;
  // Resolved after we refetch /v1/account/clients post-capture:
  client?: { id: number; name: string; arrays: string[] };
}

interface Props {
  /** When true, ceremony is rendered even before any capture event arrives
   *  (post-onboarding fresh visit). Otherwise we wait silently for the
   *  first capture so it doesn't get in the way of returning users. */
  freshVisit: boolean;
  /** Bumped after each ceremony event so the parent ClientsSection can
   *  refresh its list and the new card appears in real-time. */
  onCaptureLanded: () => void;
}

const DISMISS_KEY = "so_capture_ceremony_dismissed";

export function CaptureCeremony({ freshVisit, onCaptureLanded }: Props) {
  const toast = useToast();
  // Read cached extension version (no active probe — version was set when the
  // extension announced itself at page load via SO_EXTENSION_PRESENT).
  const ext = useExtensionStatus(false);
  const [events, setEvents] = useState<CaptureEvent[]>([]);
  const [pendingProvider, setPendingProvider] = useState<Provider | null>(null);
  const [pendingSince, setPendingSince] = useState<number | null>(null);
  const [dismissed, setDismissed] = useState(() => {
    try {
      return sessionStorage.getItem(DISMISS_KEY) === "true";
    } catch {
      return false;
    }
  });
  const [fadingOut, setFadingOut] = useState(false);
  const nextIdRef = useRef(1);
  const knownClientIdsRef = useRef<Set<number>>(new Set());
  const pollAbortRef = useRef<{ canceled: boolean } | null>(null);
  // Refs so timer callbacks can read current state without stale closures.
  const pendingProviderRef = useRef<Provider | null>(null);
  const autoDismissTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fadingTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => { pendingProviderRef.current = pendingProvider; }, [pendingProvider]);

  // Clear only the raw timers (safe to call on unmount — no setState).
  function clearAutoDismissTimers() {
    if (autoDismissTimerRef.current) {
      clearTimeout(autoDismissTimerRef.current);
      autoDismissTimerRef.current = null;
    }
    if (fadingTimerRef.current) {
      clearTimeout(fadingTimerRef.current);
      fadingTimerRef.current = null;
    }
  }

  // Cancel any in-progress auto-dismiss (timers + fade state).
  // Only call while component is mounted.
  function clearAutoDismiss() {
    clearAutoDismissTimers();
    setFadingOut(false);
  }

  // Start (or restart) the 30s inactivity auto-dismiss. Respects pending state.
  function scheduleAutoDismiss() {
    clearAutoDismissTimers();
    autoDismissTimerRef.current = setTimeout(() => {
      if (pendingProviderRef.current) {
        // Still scraping — reschedule until the capture lands.
        scheduleAutoDismiss();
        return;
      }
      setFadingOut(true);
      fadingTimerRef.current = setTimeout(() => {
        setFadingOut(false);
        setDismissed(true);
        try { sessionStorage.setItem(DISMISS_KEY, "true"); } catch { /* ignore */ }
      }, 350);
    }, 30_000);
  }

  // Resolve a capture event into a Client row so we can render names + arrays.
  // Also handles:
  //   • Backward-compat update detection — if the resolved client was known
  //     before the capture (knownClientIdsRef), treat as update: remove the
  //     event row and show a quiet toast instead.
  //   • Dedup — if another event already resolved to the same client, merge
  //     accountCount/arrays into that row (preserving the latest at timestamp).
  const resolveEvent = useCallback(async (ev: CaptureEvent) => {
    try {
      const clients = await listClients();
      const syncedField: keyof ClientRow =
        ev.provider === "gmp" ? "gmp_last_sync_at" : "vec_last_sync_at";
      const candidates = clients
        .filter((c) => !!c[syncedField])
        .sort((a, b) => {
          const av = String(a[syncedField] || "");
          const bv = String(b[syncedField] || "");
          return bv.localeCompare(av);
        });
      const top = candidates[0];
      if (!top) return;
      let arrayNames: string[] = [];
      try {
        const arrays = await listArrays(top.id);
        arrayNames = arrays.map((a) => a.name);
      } catch { /* non-fatal — chips just won't render */ }

      // Backward-compat update detection: we had a pre-capture snapshot AND the
      // resolved client was already in it — this is a re-scrape, not a new client.
      const isKnownClient =
        knownClientIdsRef.current.size > 0 &&
        knownClientIdsRef.current.has(top.id);
      if (ev.isUpdate || isKnownClient) {
        setEvents((prev) => prev.filter((p) => p.id !== ev.id));
        toast.show(
          `Updated ${top.name} — ${arrayNames.length} array${arrayNames.length === 1 ? "" : "s"} refreshed`,
          "info",
        );
        return;
      }

      // Dedup: if another event already resolved to this client, merge into it.
      setEvents((prev) => {
        const existingIdx = prev.findIndex(
          (p) => p.id !== ev.id && p.client?.id === top.id,
        );
        if (existingIdx >= 0) {
          const existing = prev[existingIdx];
          return prev
            .map((p, i) =>
              i === existingIdx
                ? {
                    ...p,
                    accountCount: Math.max(existing.accountCount, ev.accountCount),
                    at: existing.at > ev.at ? existing.at : ev.at,
                    client: {
                      id: top.id,
                      name: top.name,
                      arrays: arrayNames.slice(
                        0,
                        Math.max(existing.accountCount, ev.accountCount, arrayNames.length),
                      ),
                    },
                  }
                : p,
            )
            .filter((p) => p.id !== ev.id);
        }
        // Normal case: update this event in-place with resolved client data.
        return prev.map((p) =>
          p.id === ev.id
            ? {
                ...p,
                client: {
                  id: top.id,
                  name: top.name,
                  arrays: arrayNames.slice(0, Math.max(ev.accountCount || 0, arrayNames.length)),
                },
              }
            : p,
        );
      });
    } catch {
      // Non-fatal — ceremony still renders the bare "N accounts captured" line.
    }
  }, [toast]);

  // Listen for SO_CAPTURE_LANDED broadcasts forwarded by so_bridge.js.
  useEffect(() => {
    function onMessage(e: MessageEvent) {
      if (e.source !== window) return;
      const data = e.data;
      if (!data || typeof data !== "object") return;
      if (data.type !== "SO_CAPTURE_LANDED") return;

      if (!data.ok) {
        // Capture failed → clear pending so we don't loop on it.
        setPendingProvider(null);
        setPendingSince(null);
        if (pollAbortRef.current) pollAbortRef.current.canceled = true;
        return;
      }

      const provider = (data.provider as Provider) || "gmp";

      // Clear pending state (applies to both paths).
      setPendingProvider(null);
      setPendingSince(null);
      if (pollAbortRef.current) pollAbortRef.current.canceled = true;
      clearAutoDismiss(); // abort any in-progress fade

      if (data.result === "updated") {
        // Backend explicitly says this was a re-capture — quiet toast, no ceremony row.
        void (async () => {
          try {
            const freshClients = await listClients();
            const syncedField: keyof ClientRow =
              provider === "gmp" ? "gmp_last_sync_at" : "vec_last_sync_at";
            const top = freshClients
              .filter((c) => !!c[syncedField])
              .sort((a, b) =>
                String(b[syncedField] || "").localeCompare(String(a[syncedField] || "")),
              )[0];
            if (top) {
              const arrays = await listArrays(top.id).catch(() => []);
              toast.show(
                `Updated ${top.name} — ${arrays.length} array${arrays.length === 1 ? "" : "s"} refreshed`,
                "info",
              );
            }
          } catch { /* non-fatal */ }
        })();
        return;
      }

      const ev: CaptureEvent = {
        id: nextIdRef.current++,
        provider,
        accountCount: Number(data.accountCount ?? 0),
        at: String(data.at || new Date().toISOString()),
      };
      setEvents((prev) => [...prev, ev]);
      setDismissed(false); // un-dismiss on new event so user sees the magic
      try {
        sessionStorage.removeItem(DISMISS_KEY);
      } catch { /* ignore */ }
      scheduleAutoDismiss(); // start/reset 30s auto-dismiss timer
      void resolveEvent(ev);
      onCaptureLanded();
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resolveEvent, onCaptureLanded, toast]);

  // Listen for so:capture-pending — fired the moment the operator picks
  // GMP/VEC in the Add Client modal. Show a "Scraping…" pending row
  // INSTANTLY so they know something's happening when they tab back here.
  useEffect(() => {
    async function onPending(e: Event) {
      const ev = e as CustomEvent<{ provider?: Provider }>;
      const provider = (ev.detail?.provider as Provider) || "gmp";
      setPendingProvider(provider);
      setPendingSince(Date.now());
      setDismissed(false);
      clearAutoDismiss(); // scraping active — don't auto-dismiss while waiting
      try { sessionStorage.removeItem(DISMISS_KEY); } catch { /* ignore */ }
      // Snapshot known client IDs so we can detect the newcomer if the
      // postMessage gets lost in the tab handoff.
      try {
        const snap = await listClients();
        knownClientIdsRef.current = new Set(snap.map((c) => c.id));
      } catch { /* non-fatal */ }
      // Kick off a polling fallback: every 1.5s, refetch clients and look
      // for a new ID. If we find one, synthesize a CaptureEvent. Caps at
      // 60s to avoid running forever on a failed sign-in.
      if (pollAbortRef.current) pollAbortRef.current.canceled = true;
      const abortToken = { canceled: false };
      pollAbortRef.current = abortToken;
      const startedAt = Date.now();
      const POLL_MS = 1500;
      const MAX_MS = 60_000;
      const tick = async () => {
        if (abortToken.canceled) return;
        if (Date.now() - startedAt > MAX_MS) {
          setPendingProvider(null);
          setPendingSince(null);
          return;
        }
        try {
          const fresh = await listClients();
          const newcomer = fresh.find((c) => !knownClientIdsRef.current.has(c.id));
          if (newcomer && !abortToken.canceled) {
            const synth: CaptureEvent = {
              id: nextIdRef.current++,
              provider,
              accountCount: 0, // we don't know yet — resolveEvent will fill in arrays
              at: new Date().toISOString(),
            };
            setEvents((prev) => [...prev, synth]);
            setPendingProvider(null);
            setPendingSince(null);
            abortToken.canceled = true;
            scheduleAutoDismiss();
            void resolveEvent(synth);
            onCaptureLanded();
            return;
          }
        } catch { /* keep polling */ }
        setTimeout(() => void tick(), POLL_MS);
      };
      setTimeout(() => void tick(), POLL_MS);
    }
    window.addEventListener("so:capture-pending", onPending as EventListener);
    return () => window.removeEventListener("so:capture-pending", onPending as EventListener);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resolveEvent, onCaptureLanded]);

  // Cleanup on unmount — clear raw timers only, no setState.
  useEffect(() => {
    return () => {
      if (pollAbortRef.current) pollAbortRef.current.canceled = true;
      clearAutoDismissTimers();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function dismiss() {
    clearAutoDismiss();
    setDismissed(true);
    try {
      sessionStorage.setItem(DISMISS_KEY, "true");
    } catch { /* ignore */ }
  }

  function openPortal(provider: Provider) {
    const url =
      provider === "gmp"
        ? gmpPortalUrl(ext.version)
        : "https://vermontelectric.smarthub.coop/";
    void openPortalTab(url);
  }

  // While fading out: still render so the CSS transition plays.
  if (!fadingOut && dismissed) return null;
  if (!fadingOut && !freshVisit && events.length === 0 && !pendingProvider) return null;

  const totalClients = new Set(events.map((e) => e.client?.id).filter(Boolean)).size;
  const totalArrays = events.reduce((sum, e) => sum + (e.accountCount || 0), 0);

  return (
    <div
      className="mb-6 overflow-hidden rounded-2xl border border-primary-200 bg-gradient-to-b from-primary-50 to-cream shadow-sm transition-opacity duration-300"
      style={fadingOut ? { opacity: 0 } : undefined}
    >
      <div className="flex items-start justify-between gap-4 px-5 pt-5">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wider text-primary-700">
            {pendingProvider
              ? "Scraping…"
              : events.length === 0
              ? "Waiting for your first capture"
              : "Live capture"}
          </p>
          <h2 className="mt-1 text-lg font-semibold text-zinc-900">
            {pendingProvider
              ? `Reading ${pendingProvider.toUpperCase()} — your new client will appear here in a moment`
              : events.length === 0
              ? "Sign into a utility portal and watch your clients land here"
              : totalClients > 0
              ? `${totalClients} client${totalClients === 1 ? "" : "s"} · ${totalArrays} array${totalArrays === 1 ? "" : "s"} captured`
              : `${totalArrays} array${totalArrays === 1 ? "" : "s"} captured`}
          </h2>
          {pendingProvider && pendingSince && (
            <p className="mt-1 text-xs text-zinc-500">
              {`Started ${Math.max(1, Math.round((Date.now() - pendingSince) / 1000))}s ago · usually takes 5–30 seconds`}
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={dismiss}
          aria-label="Dismiss"
          className="text-xl leading-none text-zinc-400 transition-colors hover:text-zinc-700"
        >
          ×
        </button>
      </div>

      {/* Pending shimmer row — shows immediately when a capture starts so
          the operator knows the system is working even before SO_CAPTURE_LANDED. */}
      {pendingProvider && (
        <ol className="mt-4 space-y-2 px-5">
          <li className="rounded-xl border border-primary-100 bg-white/80 px-4 py-3 text-sm">
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 truncate font-medium text-zinc-900">
                  <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-primary-300 border-t-primary-700" />
                  <span>Capturing client data…</span>
                </div>
                <div className="mt-1.5 flex flex-wrap gap-1.5">
                  {[0, 1, 2].map((i) => (
                    <span
                      key={i}
                      className="so-shimmer inline-block h-4 w-16 rounded-md bg-primary-50"
                      style={{ animationDelay: `${i * 150}ms` } as React.CSSProperties}
                    />
                  ))}
                </div>
              </div>
              <span className="shrink-0 text-xs font-semibold uppercase tracking-wider text-primary-600">
                {pendingProvider}
              </span>
            </div>
          </li>
        </ol>
      )}

      {/* Cascading event list */}
      {events.length > 0 && (
        <ol className="mt-4 space-y-2 px-5">
          {events.map((ev, idx) => (
            <li
              key={ev.id}
              className="so-cascade-row rounded-xl border border-primary-100 bg-white/80 px-4 py-3 text-sm"
              style={{ animationDelay: `${idx * 80}ms` } as React.CSSProperties}
            >
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="truncate font-medium text-zinc-900">
                    {ev.client ? ev.client.name : "Capturing…"}
                  </div>
                  {ev.client && ev.client.arrays.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1.5">
                      {ev.client.arrays.map((name, i) => (
                        <span
                          key={i}
                          className="so-cascade-chip inline-flex items-center rounded-md bg-primary-50 px-2 py-0.5 text-xs font-medium text-primary-800"
                          style={{ animationDelay: `${idx * 80 + 200 + i * 60}ms` } as React.CSSProperties}
                        >
                          {name}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
                <span className="shrink-0 text-xs font-semibold uppercase tracking-wider text-primary-600">
                  {ev.provider}
                </span>
                <span className="shrink-0 text-sm text-primary-600">✓</span>
              </div>
            </li>
          ))}
        </ol>
      )}

      {/* Soft prompt */}
      <div className="mt-5 border-t border-primary-100 bg-primary-50/40 px-5 py-4">
        <p className="text-sm text-zinc-700">
          {events.length === 0
            ? "Once you log in, your client will appear here with all their arrays already attached. Want to start now?"
            : "Log into another utility account to add the next client. Each login = one more client, automatically."}
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => openPortal("gmp")}
            className="inline-flex items-center justify-center rounded-xl bg-primary-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-primary-700"
          >
            Open Green Mountain Power →
          </button>
          <button
            type="button"
            onClick={() => openPortal("vec")}
            className="inline-flex items-center justify-center rounded-xl border border-primary-300 bg-white px-4 py-2 text-sm font-semibold text-primary-700 transition-colors hover:bg-primary-50"
          >
            Open Vermont Electric Coop →
          </button>
          {events.length > 0 && (
            <button
              type="button"
              onClick={dismiss}
              className="ml-auto text-sm text-zinc-500 underline-offset-2 hover:underline"
            >
              I&apos;m done for now
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
