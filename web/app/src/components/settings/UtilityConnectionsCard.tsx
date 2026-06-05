import { useEffect, useState } from "react";
import { Card } from "../../ui/Card";
import { Spinner } from "../../ui/Spinner";
import { type Account, type Provider, type CaptureEntry, type UtilitySessionStatus, listProviders, getRecentCaptures } from "../../lib/api";
import { openPortalTab } from "../../lib/openPortalTab";
import { timeAgo } from "./utils";

const PORTAL_URLS: Record<string, string> = {
  gmp: "https://mypower.greenmountainpower.com/",
  vec: "https://vermontelectric.smarthub.coop",
};

interface Props {
  account: Account;
}

export function UtilityConnectionsCard({ account }: Props) {
  const [providers, setProviders] = useState<Provider[]>([]);
  const [captures, setCaptures] = useState<CaptureEntry[] | null>(null);
  const [reconnecting, setReconnecting] = useState<string | null>(null);

  useEffect(() => {
    listProviders()
      .then((ps) => setProviders(ps.filter((p) => p.scrape_status === "live")))
      .catch(() => {});
    getRecentCaptures(5)
      .then(setCaptures)
      .catch(() => {});
  }, []);

  const lastSeen = account.extension_heartbeat_at
    ? new Date(account.extension_heartbeat_at)
    : null;
  const extensionStale =
    lastSeen ? Date.now() - lastSeen.getTime() > 48 * 60 * 60 * 1000 : false;

  async function reconnect(code: string) {
    const url = PORTAL_URLS[code];
    if (!url) return;
    setReconnecting(code);
    await openPortalTab(url);
    setReconnecting(null);
  }

  // Deduplicate captures by array name, keep most recent per array.
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

  return (
    <Card>
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold tracking-tight text-zinc-900">
            Utility connections
          </h2>
          <p className="mt-0.5 text-sm text-zinc-500">
            Portals the Chrome extension pulls billing data from.
          </p>
        </div>
        {lastSeen && (
          <span
            className={[
              "shrink-0 inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium",
              extensionStale
                ? "bg-amber-100 text-amber-800"
                : "bg-primary-100 text-primary-700",
            ].join(" ")}
          >
            {extensionStale ? "Stale" : "Active"}
          </span>
        )}
      </div>

      {lastSeen && (
        <p className="mt-1 text-xs text-zinc-400">
          Extension last seen {timeAgo(lastSeen)}.
        </p>
      )}
      {!lastSeen && (
        <p className="mt-1 text-xs text-zinc-400">
          Extension not yet connected — install it and log into your utility portal.
        </p>
      )}

      <div className="mt-4 divide-y divide-zinc-100 border-t border-zinc-100">
        {providers.map((p) => {
          const sess: UtilitySessionStatus | null =
            p.code === "gmp" ? (account.session ?? null) : null;
          const lastRefresh = sess?.last_refresh_at
            ? new Date(sess.last_refresh_at)
            : null;
          const needsReauth = (sess?.refresh_failures ?? 0) > 0;
          return (
            <div key={p.code} className="flex items-center justify-between gap-4 py-3">
              <div>
                <div className="flex items-center gap-2">
                  <p className="text-sm font-medium text-zinc-800">{p.label}</p>
                  {needsReauth && (
                    <span className="inline-flex items-center rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800">
                      Re-auth needed
                    </span>
                  )}
                </div>
                <p className="text-xs capitalize text-zinc-400">
                  {p.scrape_status === "live" ? "Automated capture" : p.scrape_status}
                  {lastRefresh && (
                    <> · auto-refreshed {timeAgo(lastRefresh)}</>
                  )}
                </p>
              </div>
              {PORTAL_URLS[p.code] && (
                <button
                  type="button"
                  disabled={reconnecting === p.code}
                  onClick={() => reconnect(p.code)}
                  className="shrink-0 text-sm font-medium text-primary-600 hover:text-primary-800 focus:outline-none focus-visible:underline disabled:opacity-50"
                >
                  {reconnecting === p.code ? (
                    <Spinner className="h-4 w-4" />
                  ) : (
                    "Open portal →"
                  )}
                </button>
              )}
            </div>
          );
        })}
        {providers.length === 0 && (
          <div className="flex items-center gap-2 py-4 text-sm text-zinc-400">
            <Spinner className="h-4 w-4" />
            Loading…
          </div>
        )}
      </div>

      {extensionStale && lastSeen && (
        <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3">
          <p className="text-sm font-medium text-amber-900">
            Extension hasn't checked in for 48+ hours.
          </p>
          <p className="mt-0.5 text-xs text-amber-800">
            New bill data may not be flowing. Click &quot;Open portal&quot; above to log in and
            trigger a reconnect.
          </p>
        </div>
      )}

      {account.extension_heartbeat_at && account.bills_count === 0 && account.accounts_count > 0 && (
        <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3">
          <p className="text-sm font-medium text-amber-900">
            Extension is active but no bills captured yet.
          </p>
          <p className="mt-0.5 text-xs text-amber-800">
            Make sure you&apos;re signed into a utility portal account with community solar
            billing. If nothing appears,{" "}
            <a
              href="mailto:support@solaroperator.org"
              className="underline underline-offset-2 hover:text-amber-700"
            >
              contact support
            </a>
            .
          </p>
        </div>
      )}

      {recentArrays.length > 0 && (
        <div className="mt-4 rounded-xl border border-cream-border bg-cream px-4 py-3">
          <div className="text-[11px] font-medium uppercase tracking-wide text-zinc-500">
            Recent activity
          </div>
          {(() => {
            const ats = recentArrays
              .map((c) => c.pulled_at)
              .filter(Boolean)
              .sort() as string[];
            const latestAt = ats.length > 0 ? ats[ats.length - 1] : undefined;
            const names = recentArrays.map((c) => c.array_name).join(", ");
            return (
              <p className="mt-1.5 text-xs text-zinc-700">
                {latestAt ? (
                  <>
                    <span className="font-medium">{timeAgo(new Date(latestAt))}</span>
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
    </Card>
  );
}
