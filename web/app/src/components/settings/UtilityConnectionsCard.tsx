import { useEffect, useRef, useState } from "react";
import { Spinner } from "../../ui/Spinner";
import {
  type Account,
  type Provider,
  type CaptureEntry,
  type ClientRow,
  listProviders,
  getRecentCaptures,
  listClients,
} from "../../lib/api";
import { openPortalTab } from "../../lib/openPortalTab";
import { useExtensionStatus } from "../../lib/useExtensionStatus";
import { timeAgo } from "./utils";

const PORTAL_URLS: Record<string, string> = {
  gmp: "https://greenmountainpower.com/account/",
  vec: "https://vermontelectric.smarthub.coop",
};

const EXTENSION_INSTALL_URL =
  "https://chromewebstore.google.com/detail/solar-operator-sync/ocohbimolfpnkjcjhiodopjjlhclinpl";

/**
 * Split the live-provider catalog into the operator's connected portals vs the
 * rest. `connectedProviderCodes` is the authoritative set from the backend
 * (account.connected_providers); `legacyCodes` is a fallback (e.g. ["gmp"])
 * derived from legacy login presence when the backend list is empty/absent.
 * Exported for unit testing.
 */
export function splitPortals<T extends { code: string }>(
  providers: T[],
  connectedProviderCodes: string[],
  legacyCodes: string[] = [],
): {
  connectedCodes: Set<string>;
  connected: T[];
  others: T[];
} {
  const connectedCodes = new Set<string>(
    connectedProviderCodes.map((c) => c.toLowerCase()),
  );
  if (connectedCodes.size === 0) {
    for (const c of legacyCodes) connectedCodes.add(c.toLowerCase());
  }
  const connected = providers.filter((p) =>
    connectedCodes.has(p.code.toLowerCase()),
  );
  const others = providers.filter(
    (p) => !connectedCodes.has(p.code.toLowerCase()),
  );
  return { connectedCodes, connected, others };
}

const UTILITY_TAG_STYLES: Record<string, string> = {
  GMP: "bg-emerald-50 text-emerald-600",
  VEC: "bg-sky-50 text-sky-700",
};

interface LoginRowProps {
  utility: string;
  client: ClientRow;
  loginIdentity: string | null;
  lastSyncAt: string | null;
  portalCode: string;
  reconnecting: boolean;
  onReconnect: () => void;
}

function LoginRow({
  utility,
  client,
  loginIdentity,
  lastSyncAt,
  portalCode,
  reconnecting,
  onReconnect,
}: LoginRowProps) {
  const syncDate = lastSyncAt ? new Date(lastSyncAt) : null;
  const isStale =
    !syncDate || Date.now() - syncDate.getTime() > 48 * 60 * 60 * 1000;
  const tagStyle =
    UTILITY_TAG_STYLES[utility] ?? "bg-zinc-100 text-zinc-600";

  return (
    <div className="flex items-center gap-3 rounded-lg border border-cream-border bg-white px-3 py-2">
      <span
        className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${tagStyle}`}
      >
        {utility}
      </span>
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-zinc-800">{client.name}</p>
        <p className="truncate text-xs text-zinc-500">
          {loginIdentity ?? "—"}
          {syncDate && <> · last sync {timeAgo(syncDate)}</>}
        </p>
      </div>
      <span
        className={`h-2 w-2 shrink-0 rounded-full ${isStale ? "bg-amber-400" : "bg-emerald-400"}`}
        title={isStale ? "No sync in 48h" : "Synced recently"}
      />
      {PORTAL_URLS[portalCode] && (
        <button
          type="button"
          disabled={reconnecting}
          onClick={onReconnect}
          className="shrink-0 text-xs font-medium text-primary-600 hover:underline focus:outline-none disabled:opacity-50"
        >
          {reconnecting ? <Spinner className="h-3 w-3" /> : "Reconnect"}
        </button>
      )}
    </div>
  );
}

interface Props {
  account: Account;
  /** Re-fetch the account so server-side heartbeat/last-seen fills in once
   *  the extension pairs live. Optional — card still works without it. */
  onRefresh?: () => void;
}

export function UtilityConnectionsCard({ account, onRefresh }: Props) {
  const [providers, setProviders] = useState<Provider[]>([]);
  const [captures, setCaptures] = useState<CaptureEntry[] | null>(null);
  const [clients, setClients] = useState<ClientRow[] | null>(null);
  const [reconnecting, setReconnecting] = useState<string | null>(null);
  // Live portals section: collapsed/expanded, and whether the not-connected
  // portals are revealed. Default: section open, connected-only shown.
  const [portalsOpen, setPortalsOpen] = useState(true);
  const [showAllPortals, setShowAllPortals] = useState(false);

  // Live extension presence (postMessage bridge). Updates the instant the
  // extension injects / pairs / lands a capture — no page reload required.
  const ext = useExtensionStatus();

  useEffect(() => {
    listProviders()
      .then((ps) => setProviders(ps.filter((p) => p.scrape_status === "live")))
      .catch(() => {});
    getRecentCaptures(5)
      .then(setCaptures)
      .catch(() => {});
    listClients()
      .then(setClients)
      .catch(() => setClients([]));
  }, []);

  const serverLastSeen = account.extension_heartbeat_at
    ? new Date(account.extension_heartbeat_at)
    : null;
  // Live signal that the extension is here right now, even if the server
  // heartbeat hasn't been written/refetched yet.
  const liveConnected =
    ext.status === "present-paired" || ext.status === "present-unpaired";
  const liveLastSync = ext.lastSyncAt ? new Date(ext.lastSyncAt) : null;
  // The card considers the extension connected when EITHER the server has a
  // heartbeat OR the live bridge sees it on the page.
  const lastSeen = serverLastSeen ?? liveLastSync;
  const connected = Boolean(serverLastSeen) || liveConnected;

  // When the extension pairs live but the server account still shows no
  // heartbeat, pull a fresh account once so last-seen / banners reconcile.
  const refreshedRef = useRef(false);
  useEffect(() => {
    if (
      liveConnected &&
      !account.extension_heartbeat_at &&
      onRefresh &&
      !refreshedRef.current
    ) {
      refreshedRef.current = true;
      onRefresh();
    }
  }, [liveConnected, account.extension_heartbeat_at, onRefresh]);

  const extensionStale =
    serverLastSeen && !liveConnected
      ? Date.now() - serverLastSeen.getTime() > 48 * 60 * 60 * 1000
      : false;

  async function reconnect(code: string) {
    const url = PORTAL_URLS[code];
    if (!url) return;
    setReconnecting(code);
    await openPortalTab(url);
    setReconnecting(null);
  }

  // Deduplicate captures by array, keep most recent per array.
  const recentArrays: CaptureEntry[] = [];
  if (captures && captures.length > 0) {
    const seen = new Map<string, CaptureEntry>();
    for (const c of captures) {
      const key = `${c.client_name}|${c.array_name}`;
      if (
        !seen.has(key) ||
        (c.pulled_at &&
          (!seen.get(key)!.pulled_at || c.pulled_at > seen.get(key)!.pulled_at!))
      ) {
        seen.set(key, c);
      }
    }
    recentArrays.push(...seen.values());
  }

  const gmpLogins = (clients ?? [])
    .filter((c) => c.gmp_email || c.gmp_username)
    .sort((a, b) => a.name.localeCompare(b.name));

  const vecLogins = (clients ?? [])
    .filter((c) => c.vec_email || c.vec_username)
    .sort((a, b) => a.name.localeCompare(b.name));

  const hasAnyLogin = gmpLogins.length > 0 || vecLogins.length > 0;

  // Connected = providers this tenant actually has utility accounts for. The
  // backend supplies the authoritative set (account.connected_providers); we
  // fall back to legacy login presence so an older API still highlights GMP/VEC.
  const legacyConnected: string[] = [];
  if (gmpLogins.length > 0) legacyConnected.push("gmp");
  if (vecLogins.length > 0) legacyConnected.push("vec");
  const {
    connectedCodes,
    connected: connectedPortals,
    others: otherPortals,
  } = splitPortals(providers, account.connected_providers ?? [], legacyConnected);
  // What the list renders: connected by default; the full catalog when the
  // operator clicks "Show all". When nothing is connected yet, fall back to
  // showing all so the section isn't mysteriously empty.
  const haveConnected = connectedPortals.length > 0;
  const visiblePortals =
    showAllPortals || !haveConnected ? providers : connectedPortals;

  const gmpSess = account.session ?? null;
  const needsReauth = (gmpSess?.refresh_failures ?? 0) > 0;
  const lastRefresh = gmpSess?.last_refresh_at
    ? new Date(gmpSess.last_refresh_at)
    : null;

  return (
    <div>
      <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-zinc-400">
        Utility connections
      </h2>

      <div className="rounded-2xl border border-cream-border bg-cream shadow-sm">
        {/* Extension heartbeat */}
        <div className="px-5 py-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="flex items-center gap-2">
                <p className="text-sm font-medium text-zinc-800">Chrome extension</p>
                <a
                  href={EXTENSION_INSTALL_URL}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-xs font-medium text-emerald-700 underline decoration-emerald-700/40 underline-offset-2 hover:decoration-emerald-700"
                >
                  Install extension →
                </a>
              </div>
              <p className="mt-0.5 text-xs text-zinc-400">
                {connected ? (
                  <>
                    {lastSeen ? (
                      <>Last seen {timeAgo(lastSeen)} · </>
                    ) : (
                      <>Connected · </>
                    )}
                    Portals the extension pulls billing data from. Need it on
                    another computer? Use the install link above.
                  </>
                ) : (
                  <>
                    Extension not yet connected —{" "}
                    <a
                      href={EXTENSION_INSTALL_URL}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="font-medium text-emerald-700 underline decoration-emerald-700/40 underline-offset-2 hover:decoration-emerald-700"
                    >
                      install it from the Chrome Web Store
                    </a>{" "}
                    and log into your utility portal.
                  </>
                )}
              </p>
            </div>
            {connected && (
              <span
                className={[
                  "inline-flex shrink-0 items-center rounded-full px-2.5 py-0.5 text-xs font-medium",
                  extensionStale
                    ? "bg-amber-100 text-amber-800"
                    : "bg-primary-100 text-primary-700",
                ].join(" ")}
              >
                {extensionStale ? "Stale" : "Active"}
              </span>
            )}
          </div>
        </div>

        {/* Live portals — collapsible. Auto-shows the portals this operator is
            connected to; the not-connected national catalog stays hidden behind
            "Show all" so the list isn't a 400-utility wall. */}
        {providers.length > 0 && (
          <div className="border-t border-cream-border px-5 py-4">
            <button
              type="button"
              onClick={() => setPortalsOpen((o) => !o)}
              aria-expanded={portalsOpen}
              aria-controls="live-portals-panel"
              className="flex w-full items-center justify-between gap-2 text-left focus:outline-none"
            >
              <span className="flex items-center gap-2">
                <span className="text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
                  Live portals
                </span>
                {haveConnected && (
                  <span className="rounded-full bg-primary-100 px-2 py-0.5 text-[11px] font-medium text-primary-700">
                    {connectedPortals.length} connected
                  </span>
                )}
              </span>
              <span
                aria-hidden
                className={`text-zinc-400 transition-transform duration-150 ${
                  portalsOpen ? "rotate-180" : ""
                }`}
              >
                ▾
              </span>
            </button>

            {portalsOpen && (
              <div id="live-portals-panel" className="mt-2.5">
                <div className="flex flex-wrap gap-2">
                  {visiblePortals.map((p) => {
                    const isConnected = connectedCodes.has(p.code.toLowerCase());
                    return (
                      <div
                        key={p.code}
                        className={`flex items-center gap-2.5 rounded-lg border px-3 py-2 ${
                          isConnected
                            ? "border-primary-200 bg-primary-50/40"
                            : "border-cream-border bg-white"
                        }`}
                      >
                        {isConnected && (
                          <span
                            className="h-2 w-2 shrink-0 rounded-full bg-emerald-400"
                            title="Connected"
                          />
                        )}
                        <span className="text-sm font-medium text-zinc-800">
                          {p.label}
                        </span>
                        {needsReauth && p.code === "gmp" && (
                          <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800">
                            Re-auth needed
                          </span>
                        )}
                        <span className="text-xs text-zinc-400">
                          {p.scrape_status === "live"
                            ? "Automated capture"
                            : p.scrape_status}
                          {lastRefresh && p.code === "gmp" && (
                            <> · refreshed {timeAgo(lastRefresh)}</>
                          )}
                        </span>
                        {PORTAL_URLS[p.code] && (
                          <button
                            type="button"
                            disabled={reconnecting === p.code}
                            onClick={() => reconnect(p.code)}
                            className="text-xs font-medium text-primary-600 hover:underline focus:outline-none disabled:opacity-50"
                          >
                            {reconnecting === p.code ? (
                              <Spinner className="h-3 w-3" />
                            ) : (
                              "Open portal →"
                            )}
                          </button>
                        )}
                      </div>
                    );
                  })}
                </div>

                {/* Show-all / collapse-to-connected toggle. Only meaningful when
                    there are connected portals AND additional ones to reveal. */}
                {haveConnected && otherPortals.length > 0 && (
                  <button
                    type="button"
                    onClick={() => setShowAllPortals((s) => !s)}
                    className="mt-2.5 text-xs font-medium text-primary-600 hover:underline focus:outline-none"
                  >
                    {showAllPortals
                      ? "Show only my connected portals"
                      : `Show all ${providers.length} supported portals →`}
                  </button>
                )}
                {!haveConnected && (
                  <p className="mt-2 text-[11px] text-zinc-400">
                    No portals connected yet. Add a client and sign into their
                    utility to connect one.
                  </p>
                )}
              </div>
            )}
          </div>
        )}

        {/* Per-login list */}
        <div className="border-t border-cream-border px-5 py-4">
          <p className="mb-2.5 text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
            Logins by client
          </p>
          {clients === null ? (
            <div className="flex items-center gap-2 text-sm text-zinc-400">
              <Spinner className="h-4 w-4" />
              Loading logins…
            </div>
          ) : !hasAnyLogin ? (
            <p className="text-sm text-zinc-400">
              No logins yet.{" "}
              <a
                href="/accounts/clients"
                className="text-primary-600 hover:underline"
              >
                Add a client
              </a>{" "}
              and sign into their utility portal to populate this list.
            </p>
          ) : (
            <div className="space-y-1.5">
              {gmpLogins.map((c) => (
                <LoginRow
                  key={`gmp-${c.id}`}
                  utility="GMP"
                  client={c}
                  loginIdentity={c.gmp_email || c.gmp_username}
                  lastSyncAt={c.gmp_last_sync_at}
                  portalCode="gmp"
                  reconnecting={reconnecting === "gmp"}
                  onReconnect={() => reconnect("gmp")}
                />
              ))}
              {vecLogins.map((c) => (
                <LoginRow
                  key={`vec-${c.id}`}
                  utility="VEC"
                  client={c}
                  loginIdentity={c.vec_email || c.vec_username}
                  lastSyncAt={c.vec_last_sync_at}
                  portalCode="vec"
                  reconnecting={reconnecting === "vec"}
                  onReconnect={() => reconnect("vec")}
                />
              ))}
              <p className="pt-1 text-[11px] text-zinc-400">
                WEC support coming soon
              </p>
            </div>
          )}
        </div>

        {/* Recent activity */}
        {recentArrays.length > 0 && (
          <div className="border-t border-cream-border px-5 py-4">
            <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
              Recent activity
            </p>
            {(() => {
              const ats = recentArrays
                .map((c) => c.pulled_at)
                .filter(Boolean)
                .sort() as string[];
              const latestAt = ats.length > 0 ? ats[ats.length - 1] : undefined;
              const names = recentArrays.map((c) => c.array_name).join(", ");
              return (
                <p className="text-xs text-zinc-700">
                  {latestAt ? (
                    <>
                      <span className="font-medium">
                        {timeAgo(new Date(latestAt))}
                      </span>
                      {" — collected data from "}
                      <span className="font-medium">
                        {recentArrays.length}{" "}
                        {recentArrays.length === 1 ? "array" : "arrays"}
                      </span>
                      <span className="text-zinc-400"> ({names})</span>
                    </>
                  ) : (
                    <>
                      Collected data from{" "}
                      <span className="font-medium">
                        {recentArrays.length}{" "}
                        {recentArrays.length === 1 ? "array" : "arrays"}
                      </span>
                      <span className="text-zinc-400"> ({names})</span>
                    </>
                  )}
                </p>
              );
            })()}
          </div>
        )}
      </div>

      {/* Warning banners */}
      {extensionStale && lastSeen && (
        <div className="mt-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3">
          <p className="text-sm font-medium text-amber-900">
            Extension hasn&apos;t checked in for 48+ hours.
          </p>
          <p className="mt-0.5 text-xs text-amber-800">
            New bill data may not be flowing. Click &quot;Open portal&quot; above to log in
            and trigger a reconnect.
          </p>
        </div>
      )}

      {account.extension_heartbeat_at &&
        account.bills_count === 0 &&
        account.accounts_count > 0 && (
          <div className="mt-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3">
            <p className="text-sm font-medium text-amber-900">
              Extension is active but no bills captured yet.
            </p>
            <p className="mt-0.5 text-xs text-amber-800">
              Make sure you&apos;re signed into a utility portal account with community
              solar billing. If nothing appears,{" "}
              <a
                href="mailto:admin@solaroperator.org"
                className="underline underline-offset-2 hover:text-amber-700"
              >
                contact support
              </a>
              .
            </p>
          </div>
        )}
    </div>
  );
}
