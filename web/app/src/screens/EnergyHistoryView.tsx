import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Button } from "../ui/Button";
import {
  MultiYearLineChart,
  type YearSeries,
} from "../components/reports/trends/MultiYearLineChart";
import { formatKwh, formatUsd } from "../components/reports/trends/trendUtil";
import {
  type EnergyHistory,
  type EnergyPeriod,
  type TrendMonthPoint,
  getEnergyHistory,
  downloadFleetReport,
} from "../lib/api";

// ─── shared back link (mirrors TrendsView for consistent nav) ────────────────

function BackLink() {
  return (
    <Link
      to="/account"
      className="inline-flex items-center gap-1 text-xs font-medium text-zinc-400 hover:text-zinc-600"
    >
      <span aria-hidden>←</span> Back to account
    </Link>
  );
}

// ─── stat tile (matches TrendsView's StatTile) ───────────────────────────────

function StatTile({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: string;
  tone?: "neutral" | "up" | "down";
}) {
  const valueClass =
    tone === "up"
      ? "text-primary-700"
      : tone === "down"
        ? "text-red-600"
        : "text-zinc-900";
  return (
    <div className="rounded-xl border border-cream-border bg-cream px-4 py-3">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-zinc-400">
        {label}
      </div>
      <div className={`mt-1 text-lg font-semibold tabular-nums ${valueClass}`}>
        {value}
      </div>
    </div>
  );
}

function Section({
  title,
  hint,
  right,
  children,
}: {
  title: string;
  hint?: string;
  right?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-xl border border-cream-border bg-white p-5">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <h2 className="text-xs font-semibold uppercase tracking-wide text-zinc-400">
            {title}
          </h2>
          {hint && <p className="mt-0.5 text-[11px] text-zinc-400">{hint}</p>}
        </div>
        {right}
      </div>
      {children}
    </div>
  );
}

// ─── transform absorbed periods → multi-year monthly series ───────────────────

type Metric = "generated" | "consumed";

/** Attribute a billing period's kWh to a (year, month). A period is keyed by its
 *  end date's month (matches how the billing trends tab buckets). Sums multiple
 *  periods landing in the same month. */
function buildSeries(periods: EnergyPeriod[], metric: Metric): {
  series: YearSeries[];
  years: number[];
} {
  // year → month(1-12) → kWh
  const byYearMonth = new Map<number, Map<number, number>>();
  for (const p of periods) {
    const end = p.period_end || p.bill_date;
    if (!end) continue;
    const d = new Date(end);
    if (Number.isNaN(d.getTime())) continue;
    const year = d.getUTCFullYear();
    const month = d.getUTCMonth() + 1;
    const val = metric === "generated" ? p.kwh_generated : p.kwh_consumed;
    if (val === null || val === undefined || !Number.isFinite(val)) continue;
    if (!byYearMonth.has(year)) byYearMonth.set(year, new Map());
    const mm = byYearMonth.get(year)!;
    mm.set(month, (mm.get(month) ?? 0) + val);
  }
  const years = [...byYearMonth.keys()].sort((a, b) => a - b);
  const series: YearSeries[] = years
    .map((year) => {
      const mm = byYearMonth.get(year)!;
      const points: TrendMonthPoint[] = [...mm.entries()]
        .map(([month, kwh]) => ({ month, kwh, savings: null }))
        .sort((a, b) => a.month - b.month);
      return { year, points };
    })
    .filter((s) => s.points.length > 0);
  return { series, years };
}

// ─── recent periods table ────────────────────────────────────────────────────

function PeriodsTable({ periods }: { periods: EnergyPeriod[] }) {
  const rows = periods.slice(0, 24);
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-xs">
        <thead>
          <tr className="text-[10px] uppercase tracking-wide text-zinc-400">
            <th className="py-1.5 pr-3 font-semibold">Period</th>
            <th className="py-1.5 pr-3 text-right font-semibold">Generated</th>
            <th className="py-1.5 pr-3 text-right font-semibold">Consumed</th>
            <th className="py-1.5 pr-3 text-right font-semibold">To grid</th>
            <th className="py-1.5 pr-3 text-right font-semibold">Bill</th>
            <th className="py-1.5 pr-0 text-right font-semibold">Rate</th>
          </tr>
        </thead>
        <tbody className="tabular-nums">
          {rows.map((p, i) => {
            const end = p.period_end || p.bill_date;
            const label = end
              ? new Date(end).toLocaleDateString("en-US", {
                  month: "short",
                  year: "numeric",
                  timeZone: "UTC",
                })
              : "—";
            const isCredit = (p.total_cost ?? 0) < 0;
            return (
              <tr
                key={i}
                className="border-t border-cream-border text-zinc-700"
              >
                <td className="py-1.5 pr-3 font-medium text-zinc-800">{label}</td>
                <td className="py-1.5 pr-3 text-right">
                  {p.kwh_generated != null ? formatKwh(p.kwh_generated) : "—"}
                </td>
                <td className="py-1.5 pr-3 text-right">
                  {p.kwh_consumed != null ? formatKwh(p.kwh_consumed) : "—"}
                </td>
                <td className="py-1.5 pr-3 text-right">
                  {p.kwh_sent_to_grid != null ? formatKwh(p.kwh_sent_to_grid) : "—"}
                </td>
                <td
                  className={`py-1.5 pr-3 text-right ${
                    isCredit ? "text-primary-700" : ""
                  }`}
                >
                  {p.total_cost != null
                    ? isCredit
                      ? `+${formatUsd(Math.abs(p.total_cost))}`
                      : formatUsd(p.total_cost)
                    : "—"}
                </td>
                <td className="py-1.5 pr-0 text-right text-zinc-500">
                  {p.avg_rate_cents_kwh != null
                    ? `${p.avg_rate_cents_kwh.toFixed(1)}¢`
                    : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ─── empty state ─────────────────────────────────────────────────────────────

function EmptyHistory() {
  return (
    <div className="flex flex-col items-center gap-2 rounded-xl border border-cream-border bg-cream px-6 py-12 text-center">
      <div
        aria-hidden
        className="flex h-10 w-10 items-center justify-center rounded-full bg-white text-lg text-zinc-300"
      >
        🪣
      </div>
      <p className="text-sm font-medium text-zinc-600">No energy history yet</p>
      <p className="max-w-xs text-xs text-zinc-400">
        Connect a Green Mountain Power login and we&apos;ll automatically absorb
        every billing period you have — years of your energy history, organized
        here.
      </p>
    </div>
  );
}

function HistorySkeleton() {
  return (
    <div className="space-y-6" aria-busy aria-label="Loading energy history">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <div
            key={i}
            className="rounded-xl border border-cream-border bg-cream px-4 py-3"
          >
            <div className="h-2.5 w-16 animate-pulse rounded bg-zinc-200" />
            <div className="mt-2 h-5 w-20 animate-pulse rounded bg-zinc-100" />
          </div>
        ))}
      </div>
      <div className="rounded-xl border border-cream-border bg-white p-5">
        <div className="h-3 w-24 animate-pulse rounded bg-zinc-200" />
        <div className="mt-4 h-48 w-full animate-pulse rounded-lg bg-zinc-100" />
      </div>
    </div>
  );
}

// ─── all-time fleet report download ──────────────────────────────────────────

/** Prominent 'Download report' control with Excel / PDF options. Streams the
 *  server-built blob (read live from the DB, so always current) to a download. */
function DownloadReport() {
  const [busy, setBusy] = useState<"xlsx" | "pdf" | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const onDownload = useCallback((fmt: "xlsx" | "pdf") => {
    setBusy(fmt);
    setErr(null);
    downloadFleetReport(fmt)
      .catch((e) =>
        setErr(e instanceof Error ? e.message : "Couldn't build the report"),
      )
      .finally(() => setBusy(null));
  }, []);

  return (
    <div className="rounded-xl border border-cream-border bg-cream p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-zinc-800">
            Download all-time fleet report
          </div>
          <p className="mt-0.5 text-xs text-zinc-500">
            Every array, every year — aggregated live, so it always reflects your
            latest absorbed month.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={busy !== null}
            onClick={() => onDownload("xlsx")}
            className="inline-flex items-center gap-1.5 rounded-xl bg-primary-600 px-3.5 py-2 text-xs font-semibold text-white shadow-sm transition-colors hover:bg-primary-700 disabled:opacity-60"
          >
            {busy === "xlsx" ? "Building…" : "Excel"}
          </button>
          <button
            type="button"
            disabled={busy !== null}
            onClick={() => onDownload("pdf")}
            className="inline-flex items-center gap-1.5 rounded-xl border border-primary-600 bg-white px-3.5 py-2 text-xs font-semibold text-primary-700 transition-colors hover:bg-primary-50 disabled:opacity-60"
          >
            {busy === "pdf" ? "Building…" : "PDF"}
          </button>
        </div>
      </div>
      {err && <p className="mt-2 text-xs text-red-600">{err}</p>}
    </div>
  );
}

// ─── screen ──────────────────────────────────────────────────────────────────

export default function EnergyHistoryView() {
  const [data, setData] = useState<EnergyHistory | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [metric, setMetric] = useState<Metric>("generated");

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    getEnergyHistory()
      .then(setData)
      .catch((err) =>
        setError(err instanceof Error ? err.message : "Couldn't load history"),
      )
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const periods = useMemo(() => data?.periods ?? [], [data]);
  const { series, years } = useMemo(
    () => buildSeries(periods, metric),
    [periods, metric],
  );
  const hasData = series.length > 0;

  const summary = data?.summary;
  const yearsLabel =
    summary?.years_covered != null ? `${summary.years_covered}` : "—";
  // Lifetime net dollars (credits are negative cost → flip sign for "earned").
  const lifetimeNet = useMemo(
    () => periods.reduce((acc, p) => acc + (p.total_cost ?? 0), 0),
    [periods],
  );
  const earnedCredit = lifetimeNet < 0;

  return (
    <ScreenLayout>
      <div className="space-y-1">
        <BackLink />
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h1 className="text-lg font-semibold tracking-tight text-zinc-900">
            Your energy history
          </h1>
          {hasData && summary?.years_covered != null && (
            <span className="text-xs text-zinc-400">
              {summary.years_covered} years · Green Mountain Power
            </span>
          )}
        </div>
        <p className="text-xs text-zinc-500">
          Every billing period we&apos;ve absorbed from your utility — generation,
          consumption, what you sent to the grid, and what it cost.
        </p>
      </div>

      {loading ? (
        <HistorySkeleton />
      ) : error ? (
        <div className="flex flex-col items-center gap-3 rounded-xl border border-cream-border bg-cream p-8 text-center">
          <p className="text-sm text-zinc-500">We couldn&apos;t load your history.</p>
          <p className="max-w-sm text-xs text-zinc-400">{error}</p>
          <Button variant="secondary" onClick={load}>
            Retry
          </Button>
        </div>
      ) : !hasData ? (
        <>
          {/* Even with no absorbed BILLS yet, the all-time fleet report
              aggregates per-array GENERATION (DailyGeneration), so the export
              is still useful — show it above the connect-your-utility prompt. */}
          <DownloadReport />
          <EmptyHistory />
        </>
      ) : (
        <>
          {/* Headline stats — the "data sponge" payoff. */}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <StatTile label="Years of history" value={yearsLabel} />
            <StatTile label="Bills absorbed" value={formatKwh(summary?.bills)} />
            <StatTile
              label="Lifetime generated"
              value={`${formatKwh(summary?.total_kwh_generated)} kWh`}
            />
            <StatTile
              label={earnedCredit ? "Lifetime credits" : "Lifetime billed"}
              value={formatUsd(Math.abs(lifetimeNet))}
              tone={earnedCredit ? "up" : "neutral"}
            />
          </div>

          {/* All-time fleet report export — the absorbed history as a
              downloadable Excel/PDF, generated live so it's always current. */}
          <DownloadReport />

          {/* Multi-year monthly trend lines — reuses the same chart as the
              billing Trends tab so the two views read as one product. */}
          <Section
            title={metric === "generated" ? "Monthly generation by year" : "Monthly consumption by year"}
            hint="Each line is a year, Jan–Dec — seasonality and growth at a glance."
            right={
              <div className="inline-flex overflow-hidden rounded-lg border border-cream-border text-[11px] font-medium">
                <button
                  type="button"
                  onClick={() => setMetric("generated")}
                  className={`px-2.5 py-1 ${
                    metric === "generated"
                      ? "bg-primary-600 text-white"
                      : "bg-white text-zinc-500 hover:bg-cream"
                  }`}
                >
                  Generated
                </button>
                <button
                  type="button"
                  onClick={() => setMetric("consumed")}
                  className={`px-2.5 py-1 ${
                    metric === "consumed"
                      ? "bg-primary-600 text-white"
                      : "bg-white text-zinc-500 hover:bg-cream"
                  }`}
                >
                  Consumed
                </button>
              </div>
            }
          >
            <MultiYearLineChart series={series} years={years} />
          </Section>

          {/* The record itself — recent billing periods. */}
          <Section
            title="Billing periods"
            hint="Most recent first. The full record is preserved in your account."
          >
            <PeriodsTable periods={periods} />
          </Section>
        </>
      )}
    </ScreenLayout>
  );
}
