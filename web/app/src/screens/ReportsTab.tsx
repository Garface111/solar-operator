import { Suspense } from "react";
import { lazyWithRetry } from "../lib/lazyWithRetry";
import { Spinner } from "../ui/Spinner";

// ═══════════════════════════════════════════════════════════════════════════
// Reports — NEPOOL Operator SPA (nepooloperator.com/accounts)
//
// This React SPA is the NEPOOL Operator product. Array Operator offtaker
// billing lives on arrayoperator.com — never in this shell.
//
// Always render Automatic Reports (NepoolReportsTab): cadence, send-now,
// NEPOOL-GIS directory, email templates. Do NOT branch on account.product
// here; a mis-tagged tenant must not swap in AO "Billing / offtakers".
// ═══════════════════════════════════════════════════════════════════════════

const NepoolReportsTab = lazyWithRetry(() => import("./NepoolReportsTab"));

function TabSpinner() {
  return (
    <div className="flex min-h-[40vh] items-center justify-center text-zinc-400">
      <Spinner className="h-6 w-6" />
    </div>
  );
}

export default function ReportsTab() {
  return (
    <Suspense fallback={<TabSpinner />}>
      <NepoolReportsTab />
    </Suspense>
  );
}
