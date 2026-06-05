import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { QuarterCard } from "../components/reports/QuarterCard";
import { ReportsEmptyState } from "../components/reports/ReportsEmptyState";
import { StatusPill, type ShipStatus } from "../components/reports/StatusPill";
import { FailureStrip, type DeliveryFailure } from "../components/reports/FailureStrip";
import { NextRunCard } from "../components/reports/NextRunCard";
import { EmailTemplateStudio } from "../components/reports/EmailTemplateStudio";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { ScreenLayout } from "../ui/ScreenLayout";
import { useDashboardContext } from "./DashboardLayout";
import {
  type ClientRow,
  type QuarterReport,
  listClients,
  getReports,
} from "../lib/api";

// ─── helpers ─────────────────────────────────────────────────────────────────

/** Derive the one-glance status from the most recent COMPLETE quarter. */
function computeStatus(reports: QuarterReport[]): ShipStatus {
  const complete = reports.find((r) => r.status !== "draft");
  if (!complete) return "in_progress";
  if (complete.status === "sent") return "all_shipped";
  if (complete.status === "ready") return "not_yet";
  return "in_progress";
}

/** Build failure list from clients that have bounced and not recovered. */
function computeFailures(clients: ClientRow[]): DeliveryFailure[] {
  return clients
    .filter((c) => {
      if (!c.last_bounced_at) return false;
      const bouncedMs = new Date(c.last_bounced_at).getTime();
      const deliveredMs = c.last_delivered_at
        ? new Date(c.last_delivered_at).getTime()
        : 0;
      return bouncedMs > deliveredMs;
    })
    .map((c) => ({
      clientName: c.name,
      reason: c.last_bounce_reason,
      bouncedAt: c.last_bounced_at as string,
    }));
}

function quarterLabel(r: QuarterReport): string {
  return `Q${r.quarter_num} ${r.year}`;
}

// ─── Skeleton ────────────────────────────────────────────────────────────────

function RowSkeleton() {
  return (
    <div className="flex items-center justify-between rounded-xl border border-cream-border bg-cream px-5 py-3.5 shadow-sm">
      <div>
        <div className="h-4 w-20 animate-pulse rounded bg-zinc-200" />
        <div className="mt-1.5 h-3 w-52 animate-pulse rounded bg-zinc-100" />
      </div>
      <div className="h-5 w-12 animate-pulse rounded-full bg-zinc-200" />
    </div>
  );
}

// ─── Component ───────────────────────────────────────────────────────────────

export default function ReportsTab() {
  const { account, failed, retryLoad } = useDashboardContext();

  const [clients, setClients] = useState<ClientRow[] | null>(null);
  const [reports, setReports] = useState<QuarterReport[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [studioOpen, setStudioOpen] = useState(false);

  const loadData = useCallback(() => {
    setLoading(true);
    setLoadError(null);
    Promise.all([listClients(), getReports(6)])
      .then(([rows, reps]) => {
        setClients(rows);
        setReports(reps);
      })
      .catch((err) => {
        setLoadError(
          err instanceof Error ? err.message : "Couldn't load reports",
        );
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  // ── Account loading guard ─────────────────────────────────────────────────
  if (account === null) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 py-24 text-zinc-400">
        {failed ? (
          <>
            <p className="text-sm">Couldn't load your account.</p>
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
  const hasArrays = (reports?.[0]?.array_count ?? 0) > 0 || activeClients.length > 0;

  const failures: DeliveryFailure[] = computeFailures(activeClients);

  // Most recent complete quarter (not the in-progress current one)
  const completeReports = (reports ?? []).filter((r) => r.status !== "draft");
  const inProgressReports = (reports ?? []).filter((r) => r.status === "draft");
  // The last complete quarter is expanded by default; older ones collapsed.
  const [mostRecent, ...olderReports] = completeReports;

  const overallStatus: ShipStatus = reports
    ? computeStatus(reports)
    : "in_progress";

  const mostRecentLabel = mostRecent ? quarterLabel(mostRecent) : undefined;

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <ScreenLayout>
      {/* 1. Failure strip — always first if any delivery bounced */}
      {failures.length > 0 && <FailureStrip failures={failures} />}

      {/* 2. One-glance status */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <StatusPill status={overallStatus} quarter={mostRecentLabel} />
        <div className="flex items-center gap-2">
          <Button
            variant="secondary"
            onClick={() => setStudioOpen(true)}
            className="text-xs"
          >
            Customize email template
          </Button>
          <Link
            to="/account"
            className="text-xs text-zinc-400 hover:text-zinc-600"
          >
            Schedule &amp; email settings ↗
          </Link>
        </div>
      </div>

      {/* 3. Next run countdown + send-now */}
      <NextRunCard onSent={loadData} />

      {/* 4. Report history */}
      <div>
        <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-zinc-400">
          History
        </h2>

        {loading ? (
          <div className="space-y-2" aria-label="Loading history" aria-busy>
            {Array.from({ length: 4 }).map((_, i) => (
              <RowSkeleton key={i} />
            ))}
          </div>
        ) : loadError ? (
          <div className="flex flex-col items-center gap-3 rounded-xl border border-cream-border bg-cream p-8 text-center">
            <p className="text-sm text-zinc-500">{loadError}</p>
            <Button variant="secondary" onClick={loadData}>
              Retry
            </Button>
          </div>
        ) : !hasArrays ? (
          <ReportsEmptyState />
        ) : (
          <div className="space-y-2">
            {/* Most recent complete quarter — expanded */}
            {mostRecent && (
              <QuarterCard
                key={mostRecent.quarter}
                label={quarterLabel(mostRecent)}
                quarter={mostRecent.quarter}
                status={mostRecent.status}
                arrayCount={mostRecent.array_count}
                lastDeliveredAt={mostRecent.last_delivered_at}
                mwhTotal={mostRecent.mwh_total}
                clients={activeClients}
                defaultExpanded
                onRefresh={loadData}
              />
            )}

            {/* Older complete quarters — collapsed */}
            {olderReports.map((rep) => (
              <QuarterCard
                key={rep.quarter}
                label={quarterLabel(rep)}
                quarter={rep.quarter}
                status={rep.status}
                arrayCount={rep.array_count}
                lastDeliveredAt={rep.last_delivered_at}
                mwhTotal={rep.mwh_total}
                clients={activeClients}
                onRefresh={loadData}
              />
            ))}

            {/* In-progress current quarter(s) — collapsed at bottom */}
            {inProgressReports.map((rep) => (
              <QuarterCard
                key={rep.quarter}
                label={`${quarterLabel(rep)} (current)`}
                quarter={rep.quarter}
                status={rep.status}
                arrayCount={rep.array_count}
                lastDeliveredAt={rep.last_delivered_at}
                mwhTotal={rep.mwh_total}
                clients={activeClients}
                onRefresh={loadData}
              />
            ))}
          </div>
        )}
      </div>

      <EmailTemplateStudio
        open={studioOpen}
        onClose={() => setStudioOpen(false)}
      />
    </ScreenLayout>
  );
}
