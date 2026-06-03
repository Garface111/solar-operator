import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { TopNav } from "../components/TopNav";
import { AccountSummaryCard } from "../components/AccountSummaryCard";
import { ActivationCodeCard } from "../components/ActivationCodeCard";
import { ClientsSection } from "../components/ClientsSection";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import { type Account, getAccount } from "../lib/api";

interface Props {
  onSignOut: () => void;
}

export default function Dashboard({ onSignOut }: Props) {
  const toast = useToast();
  const { clientId } = useParams();
  const [account, setAccount] = useState<Account | null>(null);
  const [failed, setFailed] = useState(false);

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
  }, []);

  function patchAccount(patch: Partial<Account>) {
    setAccount((a) => (a ? { ...a, ...patch } : a));
  }

  return (
    <div className="min-h-full">
      <TopNav email={account?.email ?? null} onSignOut={onSignOut} />

      <main className="mx-auto max-w-4xl px-4 py-8">
        {account === null ? (
          <div className="flex items-center justify-center py-24 text-zinc-400">
            {failed ? (
              <p className="text-sm">
                Couldn&apos;t load your account. Refresh to try again.
              </p>
            ) : (
              <Spinner className="h-6 w-6" />
            )}
          </div>
        ) : (
          <div className="space-y-6">
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
              <AccountSummaryCard
                account={account}
                onAccountChange={patchAccount}
              />
              <ActivationCodeCard tenantKey={account.tenant_key} />
            </div>

            <ClientsSection
              expandClientId={clientId ? Number(clientId) : undefined}
            />
          </div>
        )}
      </main>

      <footer className="mx-auto max-w-4xl px-4 py-8 text-center text-xs text-zinc-400">
        Solar Operator · support@solaroperator.org
      </footer>
    </div>
  );
}
