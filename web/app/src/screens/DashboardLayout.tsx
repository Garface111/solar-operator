import { useCallback, useEffect, useState } from "react";
import { Outlet, useOutletContext } from "react-router-dom";
import { TopNav } from "../components/TopNav";
import { TabBar, type Tab } from "../ui/TabBar";
import { useToast } from "../ui/Toast";
import { type Account, getAccount } from "../lib/api";

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

  const ctx: DashboardContext = { account, failed, patchAccount, retryLoad };

  // Show a banner if the extension hasn't phoned home in 48+ hours. After 48h
  // without a heartbeat, new bill captures aren't flowing — the operator should
  // know so they can reconnect the extension.
  const heartbeatStale = account
    ? !account.extension_heartbeat_at ||
      Date.now() - new Date(account.extension_heartbeat_at).getTime() >
        48 * 60 * 60 * 1000
    : false;

  return (
    <div className="min-h-full">
      <TopNav email={account?.email ?? null} onSignOut={onSignOut} />
      <TabBar tabs={TABS} />

      {heartbeatStale && (
        <div className="border-b border-amber-200 bg-amber-50 px-4 py-2.5">
          <div className="mx-auto flex max-w-4xl items-center justify-between gap-4">
            <p className="text-sm text-amber-800">
              <span className="font-semibold">GMP extension hasn't been seen in 48+ hours.</span>{" "}
              New bill data may not be flowing. Log into your GMP account to reconnect.
            </p>
            <a
              href="https://mypower.greenmountainpower.com/"
              target="_blank"
              rel="noopener noreferrer"
              className="shrink-0 text-sm font-medium text-amber-900 underline underline-offset-2 hover:text-amber-700"
            >
              Open GMP →
            </a>
          </div>
        </div>
      )}

      <main className="mx-auto max-w-4xl px-4 py-8">
        <Outlet context={ctx} />
      </main>

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
