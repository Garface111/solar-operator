import { useNavigate } from "react-router-dom";
import { AccountSummaryCard } from "../components/AccountSummaryCard";
import { ActivationCodeCard } from "../components/ActivationCodeCard";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { useDashboardContext } from "./DashboardLayout";

export default function AccountTab() {
  const { account, failed, patchAccount, retryLoad } = useDashboardContext();
  const navigate = useNavigate();

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

  const step1Done = !!account.tenant_key;
  const step2Done = account.clients_count > 0;
  const step3Done = !!account.last_pull_at || !!account.extension_heartbeat_at;
  const setupComplete = step1Done && step2Done && step3Done;

  return (
    <div className="space-y-6">
      {!setupComplete && (
        <div className="rounded-xl border border-primary-200 bg-primary-50 px-5 py-5">
          <h3 className="text-base font-semibold text-zinc-900">
            Finish setting up your account
          </h3>
          <p className="mt-1 text-sm text-zinc-600">
            Complete these steps to start generating reports automatically.
          </p>
          <ol className="mt-4 space-y-3">
            {[
              {
                done: step1Done,
                label: "Activate the Chrome extension",
                hint: "Paste your activation code from the card below into the extension.",
              },
              {
                done: step2Done,
                label: "Add at least one client",
                hint: (
                  <button
                    type="button"
                    onClick={() => navigate("/clients")}
                    className="font-medium text-primary-600 underline-offset-2 hover:underline"
                  >
                    Go to Clients →
                  </button>
                ),
              },
              {
                done: step3Done,
                label: "Log into Green Mountain Power once",
                hint: "The extension will capture your GMP session and begin pulling bill data automatically.",
              },
            ].map((step, i) => (
              <li key={i} className="flex items-start gap-3 text-sm">
                <span
                  aria-hidden
                  className={[
                    "mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs font-semibold",
                    step.done
                      ? "bg-primary-500 text-white"
                      : "border border-zinc-300 bg-white text-zinc-400",
                  ].join(" ")}
                >
                  {step.done ? "✓" : i + 1}
                </span>
                <div>
                  <span className={step.done ? "text-zinc-400 line-through" : "text-zinc-800"}>
                    {step.label}
                  </span>
                  {!step.done && step.hint && (
                    <div className="mt-0.5 text-xs text-zinc-500">{step.hint}</div>
                  )}
                </div>
              </li>
            ))}
          </ol>
        </div>
      )}
      <AccountSummaryCard account={account} onAccountChange={patchAccount} />
      <ActivationCodeCard
        tenantKey={account.tenant_key}
        onKeyRegenerated={(newKey) => patchAccount({ tenant_key: newKey })}
      />
    </div>
  );
}
