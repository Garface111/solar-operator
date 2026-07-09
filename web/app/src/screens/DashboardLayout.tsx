import { useCallback, useEffect, useMemo, useState } from "react";
import { Outlet, useLocation, useOutletContext } from "react-router-dom";
import { TrialBanner } from "../components/TrialBanner";
import { TrialEndedGate } from "../components/TrialEndedGate";
import { CancelledGate } from "../components/CancelledGate";
import { AllSetCelebration } from "../components/AllSetCelebration";
import { BottomTabBar } from "../components/BottomTabBar";
import { MindButton } from "../components/MindButton";
import { TabBar, type Tab } from "../ui/TabBar";
import { useToast } from "../ui/Toast";
import { type Account, getAccount } from "../lib/api";
import { openPortalTab } from "../lib/openPortalTab";
import { useAutoPairExtension } from "../lib/useExtensionStatus";
import { brandFor } from "../lib/brand";

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

const BASE_TABS = [
  { key: "account", shortLabel: "Account", to: "/account" },
  { key: "clients", to: "/clients" },
  { key: "reports", shortLabel: "Reports", to: "/reports" },
] as const;

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
    const match = BASE_TABS.find((t) => path.startsWith(t.to));
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
    BASE_TABS.forEach((t) => {
      if (!visited.has(t.to)) s.add(t.to);
    });
    return s;
  }, [visited]);

  const ctx: DashboardContext = { account, failed, patchAccount, retryLoad };

  // No-upfront-payment: a trial that ended with no card on file is paused
  // (read-only). Stop showing the trial banner, surface a resume CTA, and (via
  // the server's active=False gate) reports/scrapes are already halted.
  const pausedNoCard = account?.subscription_status === "paused_no_card";

  // A cancelled account must NOT load the working dashboard, or cancelling
  // appears to do nothing. Lock it behind a full-page gate. Match both
  // spellings the backend uses ("cancelled" from cancel-trial, "canceled" from
  // the Stripe webhook). Guard on active===false so a re-subscribed tenant
  // (status flipped back) is never gated.
  const cancelled =
    account?.active === false &&
    (account?.subscription_status === "cancelled" ||
      account?.subscription_status === "canceled");

  // Auto-pair the extension as soon as we know the operator's tenant_key.
  // The activation-code UI was removed — pairing is fully zero-touch now.
  // NEVER pair a demo/read-only account: its captures are refused server-side
  // (403 demo-read-only), so pairing to it would break capture for an operator who
  // is also viewing the demo or signed into two accounts at once. Only a real
  // account may become the extension's capture target.
  useAutoPairExtension(account?.is_demo ? null : (account?.tenant_key ?? null));

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

  const brand = brandFor(account?.product);
  const tabs: Tab[] = [
    { ...BASE_TABS[0], label: brand.accountTabLabel },
    { ...BASE_TABS[1], label: brand.clientsTabLabel },
    { ...BASE_TABS[2], label: brand.reportsTabLabel },
  ];

  return (
    <div className="min-h-full">
      {account?.is_demo === true && (
        <div
          className="flex flex-wrap items-center justify-center border-b-2 border-wood-300 bg-cream px-4 py-1.5 text-center text-xs text-zinc-700"
          role="status"
        >
          <span>
            Demo mode
            <span className="hidden sm:inline"> — this is a sample account with read-only data</span>.{" "}
            <a
              href="/signup"
              className="font-medium text-wood-600 underline underline-offset-2 hover:text-wood-700"
            >
              Sign up free
            </a>{" "}
            ↗
          </span>
        </div>
      )}

      <TabBar
        tabs={tabs}
        brand={brand}
        unvisited={unvisited}
        email={account?.email ?? null}
        onSignOut={onSignOut}
      />

      {/* Trial ended with no card → HARD full-page gate (replaces dashboard
          content). "Add a card or lose access", but data is held, not deleted.
          The inline paused banner is intentionally omitted here — the gate
          below is the single, unambiguous surface for this state. */}
      {!pausedNoCard &&
        !cancelled &&
        account?.trial_ends_at &&
        new Date(account.trial_ends_at) > new Date() && (
          <TrialBanner
            trialEndsAt={account.trial_ends_at}
            hasPaymentMethod={account.has_payment_method}
          />
        )}

      {heartbeatStale && !cancelled && (
        <div className="border-b border-amber-200 bg-amber-50 px-4 py-2.5">
          <div className="mx-auto flex max-w-4xl flex-wrap items-center justify-between gap-3">
            <p className="text-sm text-amber-800">
              <span className="font-semibold">Extension hasn't been seen in 48+ hours.</span>{" "}
              New bill data may not be flowing. Log into your utility account to reconnect.
            </p>
            <a
              href="https://greenmountainpower.com/account/"
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => {
                e.preventDefault();
                void openPortalTab("https://greenmountainpower.com/account/");
              }}
              className="shrink-0 text-sm font-medium text-amber-900 underline underline-offset-2 hover:text-amber-700"
            >
              Open utility portal →
            </a>
          </div>
        </div>
      )}

      <AllSetCelebration account={account} />

      {/* pb-24 on mobile clears the 56px bottom tab bar + safe area. */}
      <main className="mx-auto max-w-4xl px-4 pt-8 pb-24 sm:pb-8">
        {cancelled ? (
          <CancelledGate onSignOut={onSignOut} />
        ) : pausedNoCard ? (
          <TrialEndedGate onSignOut={onSignOut} />
        ) : (
          <Outlet context={ctx} />
        )}
      </main>

      <footer className="mx-auto max-w-4xl px-4 pt-8 pb-24 sm:pb-8 text-center text-xs text-zinc-400">
        {brand.fullName} · admin@solaroperator.org ·{" "}
        <a
          href={`${brand.marketingUrl}/privacy`}
          target="_blank"
          rel="noopener noreferrer"
          className="underline-offset-2 hover:text-zinc-600 hover:underline"
        >
          Privacy &amp; Data
        </a>
      </footer>

      {/* Mobile-only bottom tab bar — hidden on sm+ where the top TabBar handles navigation. */}
      <BottomTabBar />

      {/* Mind button — position via CSS var that lifts it above BottomTabBar on mobile. */}
      <MindButton account={account} />
    </div>
  );
}

export function useDashboardContext(): DashboardContext {
  return useOutletContext<DashboardContext>();
}
