import { useState } from "react";
import { AccountSummaryCard } from "../components/AccountSummaryCard";
import { ActivationCodeCard } from "../components/ActivationCodeCard";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { ScreenLayout } from "../ui/ScreenLayout";
import { cancelTrial, clearSession } from "../lib/api";
import { useToast } from "../ui/Toast";
import { useDashboardContext } from "./DashboardLayout";

interface CancelTrialCardProps {
  onCancelled: () => void;
}

function CancelTrialCard({ onCancelled }: CancelTrialCardProps) {
  const toast = useToast();
  const [confirming, setConfirming] = useState(false);
  const [loading, setLoading] = useState(false);

  async function handleCancel() {
    setLoading(true);
    try {
      await cancelTrial();
      clearSession();
      toast.success("Trial cancelled — you won't be charged.");
      onCancelled();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Cancellation failed");
      setLoading(false);
      setConfirming(false);
    }
  }

  return (
    <div className="rounded-xl border border-zinc-200 bg-white p-6">
      <h2 className="mb-1 text-base font-semibold text-zinc-800">Cancel trial</h2>
      <p className="mb-4 text-sm text-zinc-500">
        You won't be charged. Your data will be removed. This can't be undone.
      </p>
      {!confirming ? (
        <button
          onClick={() => setConfirming(true)}
          className="text-sm text-red-600 underline underline-offset-2 hover:text-red-800"
        >
          Cancel my trial
        </button>
      ) : (
        <div className="flex items-center gap-3">
          <Button
            variant="secondary"
            onClick={() => setConfirming(false)}
            disabled={loading}
          >
            Keep trial
          </Button>
          <Button
            variant="primary"
            onClick={handleCancel}
            disabled={loading}
          >
            {loading ? "Cancelling…" : "Yes, cancel my trial"}
          </Button>
        </div>
      )}
    </div>
  );
}

export default function AccountTab() {
  const { account, failed, patchAccount, retryLoad } = useDashboardContext();
  const [cancelled, setCancelled] = useState(false);

  if (account === null) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 py-24 text-zinc-400">
        {failed ? (
          <>
            <p className="text-sm">Couldn&apos;t load your account.</p>
            <Button variant="secondary" onClick={retryLoad}>
              Retry
            </Button>
          </>
        ) : (
          <Spinner className="h-6 w-6" />
        )}
      </div>
    );
  }

  if (cancelled) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 py-24 text-zinc-500">
        <p className="text-sm">Trial cancelled. Signing you out…</p>
      </div>
    );
  }

  return (
    <ScreenLayout>
      <AccountSummaryCard account={account} onAccountChange={patchAccount} />
      <ActivationCodeCard
        tenantKey={account.tenant_key}
        onKeyRegenerated={(newKey) => patchAccount({ tenant_key: newKey })}
      />
      {account.subscription_status === "trialing" && (
        <CancelTrialCard onCancelled={() => setCancelled(true)} />
      )}
    </ScreenLayout>
  );
}
