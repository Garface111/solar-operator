import { useState } from "react";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { ScreenLayout } from "../ui/ScreenLayout";
import { useDashboardContext } from "./DashboardLayout";
import { AccountProfileCard } from "../components/settings/AccountProfileCard";
import { EmailPrefsCard } from "../components/settings/EmailPrefsCard";
import { UtilityConnectionsCard } from "../components/settings/UtilityConnectionsCard";
import { PlanBillingCard } from "../components/settings/PlanBillingCard";
import { DangerZoneCard } from "../components/settings/DangerZoneCard";

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
      <div className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">
          Master account
        </h1>
        <p className="mt-1 text-sm text-zinc-500">
          This is your operator workspace — billing, branding, and the email reports go out under.
        </p>
      </div>
      <AccountProfileCard account={account} onAccountChange={patchAccount} />
      <EmailPrefsCard account={account} onAccountChange={patchAccount} />
      <UtilityConnectionsCard account={account} />
      <PlanBillingCard account={account} />
      {account.subscription_status === "trialing" && (
        <DangerZoneCard onCancelled={() => setCancelled(true)} />
      )}
    </ScreenLayout>
  );
}
