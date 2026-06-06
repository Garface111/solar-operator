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
import { openPortalTab } from "../lib/openPortalTab";
import { listClients, listArrays, type ClientRow } from "../lib/api";

type Provider = "gmp" | "vec";

interface CaptureEvent {
  id: number;
  provider: Provider;
  accountCount: number;
  at: string;
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
  const nextIdRef = useRef(1);
  const knownClientIdsRef = useRef<Set<number>>(new Set());
  const pollAbortRef = useRef<{ canceled: boolean } | null>(null);

  // Resolve a capture event into a Client row so we can render names + arrays.
  // /v1/sync runs synchronously before sending SO_CAPTURE_LANDED, so the
  // tenant's clients list will already reflect the new state when we fetch.
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
      // Fetch this client's arrays so we can render real names in the cascade.
      let arrayNames: string[] = [];
      try {
        const arrays = await listArrays(top.id);
        arrayNames = arrays.map((a) => a.name);
      } catch { /* non-fatal — chips just won't render */ }
      setEvents((prev) =>
        prev.map((p) =>
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
        ),
      );
    } catch {
      // Non-fatal — ceremony still renders the bare "N accounts captured" line.
    }
  }, []);

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
      const isNew = data.is_new_client !== false; // default true for backward compat
      if (isNew) {
        const ev: CaptureEvent = {
          id: nextIdRef.current++,
          provider: (data.provider as Provider) || "gmp",
          accountCount: Number(data.accountCount ?? 0),
          at: String(data.at || new Date().toISOString()),
        };
        setEvents((prev) => [...prev, ev]);
        setPendingProvider(null);
        setPendingSince(null);
        if (pollAbortRef.current) pollAbortRef.current.canceled = true;
        setDismissed(false);
        try { sessionStorage.removeItem(DISMISS_KEY); } catch { /* ignore */ }
        void resolveEvent(ev);
        onCaptureLanded();
      } else {
        // Re-capture: clear pending state but don't add a ceremony event
        setPendingProvider(null);
        setPendingSince(null);
        if (pollAbortRef.current) pollAbortRef.current.canceled = true;
      }
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [resolveEvent, onCaptureLanded]);

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
  }, [resolveEvent, onCaptureLanded]);

  // Cleanup on unmount.
  useEffect(() => {
    return () => {
      if (pollAbortRef.current) pollAbortRef.current.canceled = true;
    };
  }, []);

  function dismiss() {
    setDismissed(true);
    try {
      sessionStorage.setItem(DISMISS_KEY, "true");
    } catch { /* ignore */ }
  }

  function openPortal(provider: Provider) {
    const url =
      provider === "gmp"
        ? "https://greenmountainpower.com/account/"
        : "https://vermontelectric.smarthub.coop/";
    void openPortalTab(url);
  }

  // Render nothing if dismissed AND no live events; spare returning users.
  // Render nothing if dismissed AND no live activity; spare returning users.
  if (dismissed && events.length === 0 && !pendingProvider) return null;
  if (!freshVisit && events.length === 0 && !pendingProvider) return null;

  const totalClients = new Set(events.map((e) => e.client?.id).filter(Boolean)).size;
  const totalArrays = events.reduce((sum, e) => sum + (e.accountCount || 0), 0);

  return (
    <div className="mb-6 overflow-hidden rounded-2xl border border-primary-200 bg-gradient-to-b from-primary-50 to-cream shadow-sm">
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
