import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { lazyWithRetry } from "../lib/lazyWithRetry";
import { QuarterCard, clientDeliveryStatus } from "../components/reports/QuarterCard";
import { ReportsEmptyState } from "../components/reports/ReportsEmptyState";
import { StatusPill, type ShipStatus } from "../components/reports/StatusPill";
import { FailureStrip, type DeliveryFailure } from "../components/reports/FailureStrip";
import { NextRunCard } from "../components/reports/NextRunCard";
import { AutoReportsSettingsCard } from "../components/reports/AutoReportsSettingsCard";

const EmailTemplateStudio = lazyWithRetry(() =>
  import("../components/reports/EmailTemplateStudio").then((m) => ({
    default: m.EmailTemplateStudio,
  })),
);
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { ScreenLayout } from "../ui/ScreenLayout";
import { useDashboardContext } from "./DashboardLayout";
import {
  type ClientRow,
  type QuarterReport,
  listClients,
  getReports,
  sendSampleReport,
  downloadDirectoryReport,
  sendDirectoryReport,
  recentReportQuarters,
} from "../lib/api";

// ─── helpers ─────────────────────────────────────────────────────────────────

function computeStatus(reports: QuarterReport[]): ShipStatus {
  const complete = reports.find((r) => r.status !== "draft");
  if (!complete) return "in_progress";
  if (complete.status === "sent") return "all_shipped";
  if (complete.status === "ready") return "not_yet";
  return "in_progress";
}

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

type StatusFilter = "all" | "sent" | "bounced" | "in_progress";

function passesFilter(
  rep: QuarterReport,
  filter: StatusFilter,
  clients: ClientRow[],
): boolean {
  if (filter === "all") return true;
  if (filter === "sent") return rep.status === "sent" || rep.status === "ready";
  if (filter === "in_progress") return rep.status === "draft";
  if (filter === "bounced")
    return clients.some(
      (c) => clientDeliveryStatus(c) === "bounced",
    );
  return true;
}

// ─── Timeline dot ─────────────────────────────────────────────────────────────

function TimelineDot({ status }: { status: QuarterReport["status"] }) {
  if (status === "draft") {
    return (
      <div className="h-3 w-3 rounded-full border-2 border-wood-300 bg-cream" />
    );
  }
  if (status === "empty") {
    return <div className="h-3 w-3 rounded-full bg-zinc-200" />;
  }
  return <div className="h-3 w-3 rounded-full bg-primary-600" />;
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

export default function NepoolReportsTab() {
  const { account, failed, patchAccount, retryLoad } = useDashboardContext();

  const [clients, setClients] = useState<ClientRow[] | null>(null);
  const [reports, setReports] = useState<QuarterReport[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [studioOpen, setStudioOpen] = useState(false);

  // ── History filter + expand state ─────────────────────────────────────────
  const [searchQuery, setSearchQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  // null = "use default" (most-recent expanded). Set to a concrete Set on
  // first user interaction so subsequent loadData calls don't reset it.
  const [expandedSet, setExpandedSet] = useState<Set<string> | null>(null);
  const defaultApplied = useRef(false);

  // ── "Send myself a test report" — a real [SAMPLE] workbook to the operator's
  // own inbox, no client ever contacted (POST /v1/account/send-sample-report). ──
  const [sampleSending, setSampleSending] = useState(false);
  const [sampleMsg, setSampleMsg] = useState<{ ok: boolean; text: string } | null>(null);
  // NEPOOL-GIS directory (all clients × arrays) for operator bulk upload.
  const dirQuarters = recentReportQuarters(8);
  const [dirQuarter, setDirQuarter] = useState(dirQuarters[0]?.value ?? "");
  const [dirBusy, setDirBusy] = useState<"dl" | "mail" | null>(null);
  const [dirMsg, setDirMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const handleSendSample = useCallback(() => {
    setSampleSending(true);
    setSampleMsg(null);
    sendSampleReport()
      .then((r) => {
        setSampleMsg({
          ok: true,
          text: r.sent_to
            ? `Sent a [SAMPLE] report to ${r.sent_to}.`
            : "Sample report sent to your email.",
        });
      })
      .catch((err) =>
        // The endpoint 4xx's with helpful guidance ("Add a client and array
        // first", "Add an email address first") — surface it verbatim.
        setSampleMsg({
          ok: false,
          text: err instanceof Error ? err.message : "Couldn't send the sample.",
        }),
      )
      .finally(() => setSampleSending(false));
  }, []);

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
  const hasArrays =
    (reports?.[0]?.array_count ?? 0) > 0 || activeClients.length > 0;

  const failures: DeliveryFailure[] = computeFailures(activeClients);

  // Order: in-progress current → most-recent complete (expanded by default) →
  // older completes (collapsed). Preserved from batch 9 ordering decision.
  const completeReports = (reports ?? []).filter((r) => r.status !== "draft");
  const inProgressReports = (reports ?? []).filter(
    (r) => r.status === "draft",
  );
  const [mostRecent, ...olderReports] = completeReports;
  const allReports: QuarterReport[] = [
    ...inProgressReports,
    ...(mostRecent ? [mostRecent] : []),
    ...olderReports,
  ];

  // Apply default expansion once after first data load.
  if (mostRecent && !defaultApplied.current) {
    defaultApplied.current = true;
    if (expandedSet === null) {
      // Will be set synchronously on this render path; schedule via state.
      // Use a lazy fallback below instead.
    }
  }
  // Resolve the active expansion set: null → default (mostRecent open).
  const activeExpandedSet: Set<string> =
    expandedSet ?? (mostRecent ? new Set([mostRecent.quarter]) : new Set());

  function toggleQuarter(q: string) {
    const next = new Set(activeExpandedSet);
    if (next.has(q)) next.delete(q);
    else next.add(q);
    setExpandedSet(next);
  }

  function expandAll() {
    setExpandedSet(new Set(allReports.map((r) => r.quarter)));
  }

  function collapseAll() {
    setExpandedSet(new Set());
  }

  const filteredReports = allReports.filter((rep) =>
    passesFilter(rep, statusFilter, activeClients),
  );

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
      </div>

      {/* 3. Delivery settings — cadence, CC-me, email template */}
      <AutoReportsSettingsCard
        account={account}
        onAccountChange={patchAccount}
        onOpenStudio={() => setStudioOpen(true)}
      />

      {/* 5. Next run countdown + send-now */}
      <NextRunCard onSent={loadData} />

      {/* NEPOOL-GIS directory — full book of every client array for GIS upload.
          Also emailed automatically whenever client reports go out. */}
      <div className="rounded-xl border border-primary-200 bg-primary-50/40 px-5 py-4 shadow-sm">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <p className="text-sm font-semibold text-zinc-800">
              NEPOOL-GIS directory
            </p>
            <p className="mt-0.5 text-xs leading-relaxed text-zinc-600">
              One workbook with a sheet for every array across all clients (same
              NEPOOL report form). Upload it to the NEPOOL-GIS site. When you send
              client reports, this directory is also emailed to you automatically.
            </p>
            {dirMsg && (
              <p
                className={`mt-1.5 text-xs ${dirMsg.ok ? "text-primary-700" : "text-red-600"}`}
              >
                {dirMsg.text}
              </p>
            )}
          </div>
          <div className="flex flex-col items-stretch gap-2 sm:items-end">
            <label className="flex items-center gap-2 text-xs text-zinc-600">
              <span className="font-semibold uppercase tracking-wide">Quarter</span>
              <select
                value={dirQuarter}
                onChange={(e) => setDirQuarter(e.target.value)}
                className="rounded-lg border border-zinc-200 bg-white px-2 py-1.5 text-sm font-medium text-zinc-800"
              >
                {dirQuarters.map((q) => (
                  <option key={q.value} value={q.value}>
                    {q.label}
                  </option>
                ))}
              </select>
            </label>
            <div className="flex flex-wrap gap-2">
              <Button
                variant="secondary"
                disabled={dirBusy !== null}
                onClick={() => {
                  setDirBusy("dl");
                  setDirMsg(null);
                  downloadDirectoryReport(dirQuarter || undefined)
                    .then(() =>
                      setDirMsg({ ok: true, text: "Directory downloaded." }),
                    )
                    .catch((err) =>
                      setDirMsg({
                        ok: false,
                        text:
                          err instanceof Error
                            ? err.message
                            : "Couldn't download directory.",
                      }),
                    )
                    .finally(() => setDirBusy(null));
                }}
              >
                {dirBusy === "dl" ? "Building…" : "Download directory"}
              </Button>
              <Button
                disabled={dirBusy !== null}
                onClick={() => {
                  setDirBusy("mail");
                  setDirMsg(null);
                  sendDirectoryReport(dirQuarter || undefined)
                    .then((r) =>
                      setDirMsg({
                        ok: true,
                        text: r.recipient
                          ? `Directory (${r.sheet_count ?? "?"} arrays) emailed to ${r.recipient}.`
                          : "Directory emailed to you.",
                      }),
                    )
                    .catch((err) =>
                      setDirMsg({
                        ok: false,
                        text:
                          err instanceof Error
                            ? err.message
                            : "Couldn't email directory.",
                      }),
                    )
                    .finally(() => setDirBusy(null));
                }}
              >
                {dirBusy === "mail" ? "Sending…" : "Email me directory"}
              </Button>
            </div>
          </div>
        </div>
      </div>

      {/* Send myself a test report — a real [SAMPLE] workbook to the operator's
          own inbox so they can preview before any client is ever contacted. */}
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-cream-border bg-cream px-5 py-4 shadow-sm">
        <div className="min-w-0">
          <p className="text-sm font-semibold text-zinc-700">
            Send myself a test report
          </p>
          <p className="mt-0.5 text-xs text-zinc-500">
            Emails a real <span className="font-medium">[SAMPLE]</span> workbook to your
            address only — no client is contacted.
          </p>
          {sampleMsg && (
            <p
              className={`mt-1.5 text-xs ${sampleMsg.ok ? "text-primary-600" : "text-red-600"}`}
            >
              {sampleMsg.text}
            </p>
          )}
        </div>
        <Button
          variant="secondary"
          onClick={handleSendSample}
          disabled={sampleSending}
        >
          {sampleSending ? "Sending…" : "Send test report"}
        </Button>
      </div>

      {/* 5. Report history — vertical timeline */}
      <div>
        {/* Header + control bar */}
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-zinc-400">
            History
          </h2>
          {!loading && !loadError && hasArrays && (
            <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto">
              <input
                type="text"
                placeholder="Search clients…"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="h-9 min-w-0 flex-1 rounded-lg border border-cream-border bg-white px-3 text-xs text-zinc-700 placeholder:text-zinc-400 focus:border-zinc-300 focus:outline-none focus:ring-1 focus:ring-zinc-200 sm:flex-none sm:w-44"
              />
              <select
                value={statusFilter}
                onChange={(e) =>
                  setStatusFilter(e.target.value as StatusFilter)
                }
                className="h-9 rounded-lg border border-cream-border bg-white px-2 text-xs text-zinc-500 focus:outline-none"
              >
                <option value="all">All</option>
                <option value="sent">Sent</option>
                <option value="bounced">Bounced</option>
                <option value="in_progress">In progress</option>
              </select>
              <button
                type="button"
                onClick={expandAll}
                className="hidden h-9 rounded-lg border border-cream-border px-3 text-xs text-zinc-500 hover:bg-zinc-50 hover:text-zinc-700 sm:block"
              >
                Expand all
              </button>
              <button
                type="button"
                onClick={collapseAll}
                className="hidden h-9 rounded-lg border border-cream-border px-3 text-xs text-zinc-500 hover:bg-zinc-50 hover:text-zinc-700 sm:block"
              >
                Collapse all
              </button>
            </div>
          )}
        </div>

        {loading ? (
          <div
            className="space-y-3"
            aria-label="Loading history"
            aria-busy
          >
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
          /* Timeline */
          <div className="relative" data-testid="reports-timeline">
            {/* Vertical spine — desktop only, runs between first and last dot */}
            {filteredReports.length > 1 && (
              <div
                aria-hidden
                className="pointer-events-none absolute hidden w-0.5 bg-wood-300/40 sm:block"
                style={{ left: "11px", top: "20px", bottom: "20px" }}
              />
            )}

            {filteredReports.length === 0 && (
              <p className="text-xs text-zinc-400">
                No quarters match this filter.
              </p>
            )}

            {filteredReports.map((rep) => {
              const isInProgress = rep.status === "draft";
              const label = isInProgress
                ? `${quarterLabel(rep)} (current)`
                : quarterLabel(rep);

              return (
                <div
                  key={rep.quarter}
                  className="relative flex gap-4 pb-5 last:pb-0"
                >
                  {/* Dot column — hidden on mobile, timeline rail on sm+ */}
                  <div
                    aria-hidden
                    className="relative z-10 hidden w-6 flex-shrink-0 items-start justify-center pt-1 sm:flex"
                  >
                    <TimelineDot status={rep.status} />
                  </div>

                  {/* Card */}
                  <div className="min-w-0 flex-1">
                    <QuarterCard
                      label={label}
                      quarter={rep.quarter}
                      status={rep.status}
                      arrayCount={rep.array_count}
                      lastDeliveredAt={rep.last_delivered_at}
                      lastGeneratedAt={rep.last_generated_at}
                      mwhTotal={rep.mwh_total}
                      clients={activeClients}
                      expanded={activeExpandedSet.has(rep.quarter)}
                      onToggle={() => toggleQuarter(rep.quarter)}
                      searchQuery={searchQuery}
                      onRefresh={loadData}
                    />
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {studioOpen && (
        <Suspense fallback={null}>
          <EmailTemplateStudio
            open={studioOpen}
            onClose={() => setStudioOpen(false)}
          />
        </Suspense>
      )}
    </ScreenLayout>
  );
}
