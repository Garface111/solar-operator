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
  type QuarterReport,
  listClients,
  downloadClientReport,
  getReports,
  regenerateReport,
} from "../lib/api";

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
  const [reports, setReports] = useState<QuarterReport[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  function loadData() {
    setLoading(true);
    setLoadError(null);
    Promise.all([listClients(), getReports(6)])
      .then(([rows, reps]) => {
        setClients(rows);
        setReports(reps);
      })
      .catch((err) => {
        setLoadError(err instanceof Error ? err.message : "Couldn't load report data");
      })
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    loadData();
  }, []);

  const handleDownload = useCallback(
    async (client: ClientRow, quarter?: string) => {
      try {
        await downloadClientReport(client.id, client.name, quarter);
      } catch (err) {
        toast.error(err instanceof Error ? err.message : "Couldn't download report");
      }
    },
    [toast],
  );

  const handleRegenerate = useCallback(
    async (quarter?: string) => {
      try {
        await regenerateReport(quarter);
        toast.success("Report regenerated.");
        // Refresh report statuses so the cards reflect the updated state
        getReports(6).then(setReports).catch(() => {});
      } catch (err) {
        toast.error(err instanceof Error ? err.message : "Regeneration failed");
      }
    },
    [toast],
  );

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

  const activeClients = clients?.filter((c) => c.active) ?? [];
  const hasData = activeClients.length > 0;

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <ScreenLayout>
      <ReportsCard account={account} onAccountChange={patchAccount} />

      {/* Quarter history section */}
      <div>
        <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-zinc-400">
          Report History
        </h2>

        {loading ? (
          <div
            className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3"
            aria-label="Loading report history"
            aria-busy="true"
          >
            {Array.from({ length: 6 }).map((_, i) => (
              <QuarterCardSkeleton key={i} />
            ))}
          </div>
        ) : loadError ? (
          <div className="flex flex-col items-center gap-3 rounded-xl border border-cream-border bg-cream p-8 text-center">
            <p className="text-sm text-zinc-500">{loadError}</p>
            <Button variant="secondary" onClick={loadData}>
              Retry
            </Button>
          </div>
        ) : !hasData ? (
          <ReportsEmptyState />
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {(reports ?? []).map((rep) => (
              <QuarterCard
                key={rep.quarter}
                label={`Q${rep.quarter_num} ${rep.year}`}
                status={rep.status}
                arrayCount={rep.array_count}
                clientCount={activeClients.length}
                lastGeneratedAt={rep.last_delivered_at}
                mwhTotal={rep.mwh_total}
                clients={activeClients}
                onDownload={(client) => handleDownload(client, rep.quarter)}
                onRegenerate={() => handleRegenerate(rep.quarter)}
              />
            ))}
          </div>
        )}
      </div>

      <EmailCustomizationCard account={account} onAccountChange={patchAccount} />
    </ScreenLayout>
  );
}
