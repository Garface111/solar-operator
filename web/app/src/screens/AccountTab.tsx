import { useState } from "react";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { ScreenLayout } from "../ui/ScreenLayout";
import { useDashboardContext } from "./DashboardLayout";
import { AccountProfileCard } from "../components/settings/AccountProfileCard";
import { UtilityConnectionsCard } from "../components/settings/UtilityConnectionsCard";
import { PortalAccessCard } from "../components/settings/PortalAccessCard";
import { CloudCaptureCard } from "../components/settings/CloudCaptureCard";
import { PlanBillingCard } from "../components/settings/PlanBillingCard";
import { DangerZoneCard } from "../components/settings/DangerZoneCard";
import { setCaptureMode } from "../lib/api";

// Bruce Jun 6: Email + schedule prefs moved to /reports ("Automatic reports")
// where they semantically belong. AccountTab now owns only operator identity
// (profile, utility logins, plan/billing, danger zone).

export default function AccountTab() {
  const { account, failed, patchAccount, retryLoad } = useDashboardContext();
  const [cancelled, setCancelled] = useState(false);
  const [modeBusy, setModeBusy] = useState(false);

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

  // Cloud is primary when explicitly chosen. Legacy null / device keep the
  // extension vault as primary so existing operators aren't forced to migrate.
  const isCloud = account.capture_mode === "cloud";

  const switchMode = async (mode: "cloud" | "device") => {
    setModeBusy(true);
    try {
      await setCaptureMode(mode);
      patchAccount({ capture_mode: mode });
    } catch {
      /* toast optional — patch only on success */
    }
    setModeBusy(false);
  };

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
      {/* SpongeProgressCard is Array Operator only — never on NEPOOL SPA. */}
      <UtilityConnectionsCard account={account} />

      {/* Capture mode (AO dual-path) — sits above Auto-refresh like AO Account. */}
      <div className="mb-3 flex flex-wrap items-center gap-2 rounded-2xl border border-zinc-200 bg-white px-4 py-3 text-sm shadow-sm">
        <span className="font-semibold text-zinc-800">How we keep bills fresh</span>
        <button
          type="button"
          disabled={modeBusy || isCloud}
          onClick={() => void switchMode("cloud")}
          className={`rounded-full px-3 py-1 text-xs font-semibold transition-colors ${
            isCloud
              ? "bg-primary-500 text-white"
              : "bg-zinc-100 text-zinc-600 hover:bg-zinc-200"
          }`}
        >
          Store it with us
        </button>
        <button
          type="button"
          disabled={modeBusy || !isCloud}
          onClick={() => void switchMode("device")}
          className={`rounded-full px-3 py-1 text-xs font-semibold transition-colors ${
            !isCloud
              ? "bg-primary-500 text-white"
              : "bg-zinc-100 text-zinc-600 hover:bg-zinc-200"
          }`}
        >
          Keep it on my computer
        </button>
        <span className="text-xs text-zinc-500">
          {isCloud
            ? "Encrypted on our servers · live 24/7 · no tab needed"
            : "Passwords stay in the browser extension · refreshes while a tab is open"}
        </span>
      </div>

      {isCloud ? (
        <>
          {/* AO-style Auto-refresh: utility grid + search + multi-login.
              No extension install/roster nags in cloud mode — vault is enough. */}
          <CloudCaptureCard />
          <p className="mb-6 text-center text-[11px] text-zinc-400">
            Prefer passwords only on your machine? Switch to{" "}
            <button
              type="button"
              disabled={modeBusy}
              onClick={() => void switchMode("device")}
              className="font-semibold text-zinc-600 underline-offset-2 hover:underline"
            >
              Keep it on my computer
            </button>
            .
          </p>
        </>
      ) : (
        <>
          <PortalAccessCard />
          <details className="mb-6 rounded-xl border border-zinc-100 bg-zinc-50 px-4 py-3 text-sm text-zinc-600">
            <summary className="cursor-pointer font-medium text-zinc-800">
              Switch to cloud Auto-refresh (no extension needed)
            </summary>
            <p className="mt-2 text-xs text-zinc-500">
              Store utility logins encrypted on our servers and we pull bills around
              the clock. Switch mode above, then add logins in the vault.
            </p>
            <div className="mt-3">
              <CloudCaptureCard compact />
            </div>
          </details>
        </>
      )}

      <PlanBillingCard account={account} />
      {account.subscription_status === "trialing" && (
        <DangerZoneCard onCancelled={() => setCancelled(true)} />
      )}
    </ScreenLayout>
  );
}
