import { Suspense } from "react";
import { lazyWithRetry } from "../lib/lazyWithRetry";
import { Spinner } from "../ui/Spinner";
import { useDashboardContext } from "./DashboardLayout";

// ═══════════════════════════════════════════════════════════════════════════
// Reports — product dispatcher
//
// The /reports route is shared by TWO products that happen to ride the same
// backend + dashboard shell:
//
//   • NEPOOL Operator  → quarterly NEPOOL-GIS net-metering credit reports
//                         (the original "Automatic Reports" surface).
//   • Array Operator   → the per-period customer "Billing Run" (offtaker
//                         invoices, allocation %, Review drawer, etc.).
//
// They are isolated UIs. Branch on account.product so a NEPOOL tenant never
// sees the Array Operator billing layout and vice-versa. (The Jun-17 billing
// redesign had replaced the NEPOOL surface outright — this dispatcher restores
// the separation.)
// ═══════════════════════════════════════════════════════════════════════════

const NepoolReportsTab = lazyWithRetry(() => import("./NepoolReportsTab"));
const BillingReportsTab = lazyWithRetry(() => import("./BillingReportsTab"));

function TabSpinner() {
  return (
    <div className="flex min-h-[40vh] items-center justify-center text-zinc-400">
      <Spinner className="h-6 w-6" />
    </div>
  );
}

export default function ReportsTab() {
  const { account } = useDashboardContext();
  const isArrayOperator = account?.product === "array_operator";

  return (
    <Suspense fallback={<TabSpinner />}>
      {isArrayOperator ? <BillingReportsTab /> : <NepoolReportsTab />}
    </Suspense>
  );
}
