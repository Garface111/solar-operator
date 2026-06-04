import { ReportsCard } from "../components/ReportsCard";
import { EmailCustomizationCard } from "../components/EmailCustomizationCard";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { useDashboardContext } from "./DashboardLayout";

export default function ReportsTab() {
  const { account, failed, patchAccount, retryLoad } = useDashboardContext();

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

  return (
    <div className="space-y-6">
      <ReportsCard account={account} onAccountChange={patchAccount} />
      <EmailCustomizationCard account={account} onAccountChange={patchAccount} />
    </div>
  );
}
