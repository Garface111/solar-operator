import { useCallback, useEffect, useState } from "react";
import { ReportsCard } from "../components/ReportsCard";
import { EmailCustomizationCard } from "../components/EmailCustomizationCard";
import { QuarterCard } from "../components/reports/QuarterCard";
import { ReportsEmptyState } from "../components/reports/ReportsEmptyState";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { ScreenLayout } from "../ui/ScreenLayout";
import { useToast } from "../ui/Toast";
import { useDashboardContext } from "./DashboardLayout";
import {
  type ClientRow,
  listClients,
  downloadClientReport,
  sendReportNow,
} from "../lib/api";

// ─── Quarter helpers ──────────────────────────────────────────────────────────

interface QuarterInfo {
  year: number;
  q: 1 | 2 | 3 | 4;
  label: string;
  /** Inclusive start (Jan 1, Apr 1, …) */
  startDate: Date;
  /** Inclusive end (Mar 31, Jun 30, …) */
  endDate: Date;
}

function recentQuarters(count = 6): QuarterInfo[] {
  const now = new Date();
  const quarters: QuarterInfo[] = [];
  // 0-based quarter index (0 = Q1)
  let qIdx = Math.floor(now.getMonth() / 3);
  let year = now.getFullYear();

  for (let i = 0; i < count; i++) {
    const q = (qIdx + 1) as 1 | 2 | 3 | 4;
    const startMonth = qIdx * 3; // 0, 3, 6, 9
    quarters.push({
      year,
      q,
      label: `Q${q} ${year}`,
      startDate: new Date(year, startMonth, 1),
      // Month arg beyond 11 wraps correctly; day 0 = last day of prev month.
      endDate: new Date(year, startMonth + 3, 0),
    });
    qIdx -= 1;
    if (qIdx < 0) {
      qIdx = 3;
      year -= 1;
    }
  }

  return quarters;
}

type Status = "draft" | "ready" | "sent";

function deriveStatus(quarter: QuarterInfo, lastDeliveryAt: string | null): Status {
  const now = new Date();
  // Quarter is still in progress
  if (quarter.endDate >= now) return "draft";
  // Quarter complete, no delivery recorded
  if (!lastDeliveryAt) return "ready";
  // Delivery timestamp is on or after this quarter ended → it was sent
  return new Date(lastDeliveryAt) >= quarter.endDate ? "sent" : "ready";
}

// ─── Skeleton card ────────────────────────────────────────────────────────────

function QuarterCardSkeleton() {
  return (
    <div className="rounded-xl border border-cream-border bg-cream p-5 shadow-sm">
      <div className="flex items-center justify-between gap-2">
        <div className="h-5 w-20 animate-pulse rounded bg-zinc-200" />
        <div className="h-5 w-12 animate-pulse rounded-full bg-zinc-200" />
      </div>
      <div className="mt-2 h-3.5 w-44 animate-pulse rounded bg-zinc-100" />
      <div className="mt-4 flex gap-2">
        <div className="h-8 w-28 animate-pulse rounded-xl bg-zinc-200" />
        <div className="h-8 w-24 animate-pulse rounded-xl bg-zinc-100" />
      </div>
    </div>
  );
}

// ─── Component ───────────────────────────────────────────────────────────────

export default function ReportsTab() {
  const { account, failed, patchAccount, retryLoad } = useDashboardContext();
  const toast = useToast();

  const [clients, setClients] = useState<ClientRow[] | null>(null);
  const [clientsLoading, setClientsLoading] = useState(true);
  const [clientsError, setClientsError] = useState<string | null>(null);

  function loadClients() {
    setClientsLoading(true);
    setClientsError(null);
    listClients()
      .then((rows) => setClients(rows))
      .catch((err) => {
        setClientsError(err instanceof Error ? err.message : "Couldn't load clients");
      })
      .finally(() => setClientsLoading(false));
  }

  useEffect(() => {
    loadClients();
  }, []);

  const handleDownload = useCallback(async (client: ClientRow) => {
    try {
      await downloadClientReport(client.id, client.name);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't download report");
    }
  }, [toast]);

  const handleRegenerate = useCallback(async () => {
    try {
      const res = await sendReportNow();
      if (res.client_count === 0) {
        toast.error("No active clients to send to — add a client first.");
        return;
      }
      const failures = res.results.filter((r) => !r.ok);
      if (failures.length === 0) {
        toast.success(
          res.delivered === 1
            ? "Report sent to 1 client."
            : `Report sent to ${res.delivered} clients.`,
        );
      } else if (res.delivered === 0) {
        toast.error(`Report failed for all ${res.client_count} clients.`);
      } else {
        toast.error(
          `Sent to ${res.delivered} of ${res.client_count}. ${failures.length} failed.`,
        );
      }
      patchAccount({ last_delivery_at: new Date().toISOString() });
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't send report");
    }
  }, [patchAccount, toast]);

  // ── Account loading guard ─────────────────────────────────────────────────
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

  // ── Derive quarter data ───────────────────────────────────────────────────
  const quarters = recentQuarters(6);
  const activeClients = clients?.filter((c) => c.active) ?? [];
  const totalArrays = activeClients.reduce((sum, c) => sum + (c.array_count ?? 0), 0);
  const hasData = activeClients.length > 0;

  // Show lastGeneratedAt only on the first (most recent) non-draft quarter
  const firstNonDraftIdx = quarters.findIndex(
    (q) => deriveStatus(q, account.last_delivery_at) !== "draft",
  );

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <ScreenLayout>
      <ReportsCard account={account} onAccountChange={patchAccount} />

      {/* Quarter history section */}
      <div>
        <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-zinc-400">
          Report History
        </h2>

        {clientsLoading ? (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {Array.from({ length: 6 }).map((_, i) => (
              <QuarterCardSkeleton key={i} />
            ))}
          </div>
        ) : clientsError ? (
          <div className="flex flex-col items-center gap-3 rounded-xl border border-cream-border bg-cream p-8 text-center">
            <p className="text-sm text-zinc-500">{clientsError}</p>
            <Button variant="secondary" onClick={loadClients}>
              Retry
            </Button>
          </div>
        ) : !hasData ? (
          <ReportsEmptyState />
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {quarters.map((q, i) => {
              const status: Status = deriveStatus(q, account.last_delivery_at);
              return (
                <QuarterCard
                  key={q.label}
                  label={q.label}
                  status={status}
                  arrayCount={totalArrays}
                  clientCount={activeClients.length}
                  lastGeneratedAt={
                    i === firstNonDraftIdx ? account.last_delivery_at : null
                  }
                  clients={activeClients}
                  onDownload={handleDownload}
                  onRegenerate={handleRegenerate}
                />
              );
            })}
          </div>
        )}
      </div>

      <EmailCustomizationCard account={account} onAccountChange={patchAccount} />
    </ScreenLayout>
  );
}
