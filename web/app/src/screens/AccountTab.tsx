import { useState } from "react";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { ScreenLayout } from "../ui/ScreenLayout";
import { useDashboardContext } from "./DashboardLayout";
import { AccountProfileCard } from "../components/settings/AccountProfileCard";
import { UtilityConnectionsCard } from "../components/settings/UtilityConnectionsCard";
import { SpongeProgressCard } from "../components/settings/SpongeProgressCard";
import { PlanBillingCard } from "../components/settings/PlanBillingCard";
import { DangerZoneCard } from "../components/settings/DangerZoneCard";

// Bruce Jun 6: Email + schedule prefs moved to /reports ("Automatic reports")
// where they semantically belong. AccountTab now owns only operator identity
// (profile, utility logins, plan/billing, danger zone) — the things that
// describe *who you are*, not *how reports go out*.

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
      {/* Energy history ("data sponge") belongs to Array Operator only — its
          multi-year absorbed history is a core AO feature. NEPOOL operators
          don't surface it on their master account. */}
      {account.product === "array_operator" && <SpongeProgressCard />}
      <UtilityConnectionsCard account={account} />
      <PlanBillingCard account={account} />
      {account.subscription_status === "trialing" && (
        <DangerZoneCard onCancelled={() => setCancelled(true)} />
      )}
    </ScreenLayout>
  );
}
