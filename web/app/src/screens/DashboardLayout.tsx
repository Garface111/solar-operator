import { useCallback, useEffect, useMemo, useState } from "react";
import { Outlet, useLocation, useOutletContext } from "react-router-dom";
import { TopNav } from "../components/TopNav";
import { TrialBanner } from "../components/TrialBanner";
import { WalkthroughOverlay } from "../components/WalkthroughOverlay";
import { TabBar, type Tab } from "../ui/TabBar";
import { useToast } from "../ui/Toast";
import { type Account, getAccount } from "../lib/api";
import { hasSeenWalkthrough } from "../lib/walkthrough";
import { openPortalTab } from "../lib/openPortalTab";
import { useAutoPairExtension } from "../lib/useExtensionStatus";

interface Props {
  onSignOut: () => void;
}

/** Shared state handed to each tab via the router <Outlet> context. */
export interface DashboardContext {
  account: Account | null;
  /** True once the account fetch has failed (vs. still loading). */
  failed: boolean;
  patchAccount: (patch: Partial<Account>) => void;
  retryLoad: () => void;
}

const TABS: Tab[] = [
  { label: "Account", to: "/account" },
  { label: "Clients", to: "/clients" },
  { label: "Automatic Reports", to: "/reports" },
  { label: "Sandbox", to: "/sandbox" },
];

/**
 * Persistent dashboard chrome: top nav + tab bar wrap an <Outlet> that renders
 * the active tab. The account is loaded once here and shared with the tabs that
 * need it (Account, Reports) via outlet context; the Clients tab loads its own
 * data, so it never waits on the account fetch.
 */
export default function DashboardLayout({ onSignOut }: Props) {
  const toast = useToast();
  const [account, setAccount] = useState<Account | null>(null);
  const [failed, setFailed] = useState(false);
  const [loadKey, setLoadKey] = useState(0);
  const [showWalkthrough, setShowWalkthrough] = useState(() => !hasSeenWalkthrough());

  const retryLoad = useCallback(() => {
    setFailed(false);
    setLoadKey((k) => k + 1);
  }, []);

  useEffect(() => {
    let cancelled = false;
    getAccount()
      .then((a) => {
        if (!cancelled) setAccount(a);
      })
      .catch((err) => {
        // 401s are handled globally (UNAUTHORIZED_EVENT bounces to login).
        if (err?.name === "UnauthorizedError") return;
        if (!cancelled) {
          setFailed(true);
          toast.error(
            err instanceof Error ? err.message : "Couldn't load your account",
          );
        }
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadKey]);

  function patchAccount(patch: Partial<Account>) {
    setAccount((a) => (a ? { ...a, ...patch } : a));
  }

  // ── First-visit dots on the tab nav ────────────────────────────────────
  // Per-tenant localStorage key so multiple operators on the same machine
  // don't share visit state. Key updates only after we know which tenant
  // we're looking at.
  const visitKey = account?.tenant_key
    ? `so:visited_tabs:${account.tenant_key}`
    : null;
  const [visited, setVisited] = useState<Set<string>>(() => new Set());
  // Hydrate visited set once the tenant id is known.
  useEffect(() => {
    if (!visitKey) return;
    try {
      const raw = window.localStorage.getItem(visitKey);
      if (raw) setVisited(new Set(JSON.parse(raw)));
    } catch {
      /* ignore corrupt storage */
    }
  }, [visitKey]);
  // Mark the current tab as visited any time the route changes.
  const location = useLocation();
  useEffect(() => {
    if (!visitKey) return;
    const path = location.pathname;
    const match = TABS.find((t) => path.startsWith(t.to));
    if (!match || visited.has(match.to)) return;
    const next = new Set(visited);
    next.add(match.to);
    setVisited(next);
    try {
      window.localStorage.setItem(visitKey, JSON.stringify(Array.from(next)));
    } catch {
      /* ignore quota */
    }
  }, [location.pathname, visitKey, visited]);
  const unvisited = useMemo(() => {
    const s = new Set<string>();
    TABS.forEach((t) => {
      if (!visited.has(t.to)) s.add(t.to);
    });
    return s;
  }, [visited]);

  const ctx: DashboardContext = { account, failed, patchAccount, retryLoad };

  // Auto-pair the extension as soon as we know the operator's tenant_key.
  // The activation-code UI was removed — pairing is fully zero-touch now.
  useAutoPairExtension(account?.tenant_key ?? null);

  // Show a banner if the extension hasn't phoned home in 48+ hours. After 48h
  // without a heartbeat, new bill captures aren't flowing — the operator should
  // know so they can reconnect the extension.
  // Only fire for accounts older than 2 days — new operators who haven't
  // finished setting up the extension yet shouldn't see this immediately.
  const accountAgeMs = account?.created_at
    ? Date.now() - new Date(account.created_at).getTime()
    : 0;
  const accountMature = accountAgeMs > 2 * 24 * 60 * 60 * 1000;
  const heartbeatStale = account && accountMature
    ? !account.extension_heartbeat_at ||
      Date.now() - new Date(account.extension_heartbeat_at).getTime() >
        48 * 60 * 60 * 1000
    : false;

  return (
    <div className="min-h-full">
      <TopNav
        email={account?.email ?? null}
        onSignOut={onSignOut}
        onShowWalkthrough={() => setShowWalkthrough(true)}
      />
      <TabBar tabs={TABS} unvisited={unvisited} />

      {account?.trial_ends_at &&
        new Date(account.trial_ends_at) > new Date() && (
          <TrialBanner trialEndsAt={account.trial_ends_at} />
        )}

      {heartbeatStale && (
        <div className="border-b border-amber-200 bg-amber-50 px-4 py-2.5">
          <div className="mx-auto flex max-w-4xl items-center justify-between gap-4">
            <p className="text-sm text-amber-800">
              <span className="font-semibold">Extension hasn't been seen in 48+ hours.</span>{" "}
              New bill data may not be flowing. Log into your utility account to reconnect.
            </p>
            <a
              href="https://mypower.greenmountainpower.com/"
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => {
                e.preventDefault();
                void openPortalTab("https://mypower.greenmountainpower.com/");
              }}
              className="shrink-0 text-sm font-medium text-amber-900 underline underline-offset-2 hover:text-amber-700"
            >
              Open utility portal →
            </a>
          </div>
        </div>
      )}

      <main className="mx-auto max-w-4xl px-4 py-8">
        <Outlet context={ctx} />
      </main>

      {showWalkthrough && (
        <WalkthroughOverlay onClose={() => setShowWalkthrough(false)} />
      )}

      <footer className="mx-auto max-w-4xl px-4 py-8 text-center text-xs text-zinc-400">
        Solar Operator · support@solaroperator.org ·{" "}
        <a
          href="https://solaroperator.org/privacy"
          target="_blank"
          rel="noopener noreferrer"
          className="underline-offset-2 hover:text-zinc-600 hover:underline"
        >
          Privacy &amp; Data
        </a>
      </footer>
    </div>
  );
}

export function useDashboardContext(): DashboardContext {
  return useOutletContext<DashboardContext>();
}
