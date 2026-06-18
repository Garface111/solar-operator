import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Button } from "../ui/Button";
import {
  MultiYearLineChart,
  type YearSeries,
} from "../components/reports/trends/MultiYearLineChart";
import { SeasonalYoYRow } from "../components/reports/trends/SeasonalYoYRow";
import {
  formatDeltaPct,
  formatKwh,
  formatUsd,
  deltaTone,
} from "../components/reports/trends/trendUtil";
import { type BillingTrends, getBillingTrends } from "../lib/api";

// ─── back link (shared across every state for consistent navigation) ─────────

function BackLink() {
  return (
    <Link
      to="/reports"
      className="inline-flex items-center gap-1 text-xs font-medium text-zinc-400 hover:text-zinc-600"
    >
      <span aria-hidden>←</span> Back to reports
    </Link>
  );
}

// ─── stat tile ───────────────────────────────────────────────────────────────

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

// ─── skeleton (loading) ──────────────────────────────────────────────────────

function TrendsSkeleton() {
  return (
    <div className="space-y-6" aria-label="Loading trends" aria-busy>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        {Array.from({ length: 3 }).map((_, i) => (
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

// ─── card section wrapper ────────────────────────────────────────────────────

function Section({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-xl border border-cream-border bg-white p-5">
      <div className="mb-3">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-zinc-400">
          {title}
        </h2>
        {hint && <p className="mt-0.5 text-[11px] text-zinc-400">{hint}</p>}
      </div>
      {children}
    </div>
  );
}

// ─── empty / thin state ──────────────────────────────────────────────────────

function EmptyTrends() {
  return (
    <div className="flex flex-col items-center gap-2 rounded-xl border border-cream-border bg-cream px-6 py-12 text-center">
      <div
        aria-hidden
        className="flex h-10 w-10 items-center justify-center rounded-full bg-white text-lg text-zinc-300"
      >
        📈
      </div>
      <p className="text-sm font-medium text-zinc-600">
        Not enough history yet
      </p>
      <p className="max-w-xs text-xs text-zinc-400">
        Trends appear after a couple of reporting periods. Once this customer has
        more billed months, their multi-year lines and seasonal comparison will
        show up here.
      </p>
    </div>
  );
}

// ─── screen ──────────────────────────────────────────────────────────────────

export default function TrendsView() {
  const { subscriptionId } = useParams<{ subscriptionId: string }>();
  const [data, setData] = useState<BillingTrends | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    if (!subscriptionId) return;
    setLoading(true);
    setError(null);
    getBillingTrends(subscriptionId)
      .then(setData)
      .catch((err) =>
        setError(err instanceof Error ? err.message : "Couldn't load trends"),
      )
      .finally(() => setLoading(false));
  }, [subscriptionId]);

  useEffect(() => {
    load();
  }, [load]);

  const title = data?.customer_name || "Billing trends";

  // ── Derived view model (only meaningful once data has loaded) ──────────────
  const years = data?.years ?? [];
  const series: YearSeries[] = years
    .map((year) => ({
      year,
      points: data?.monthly_by_year[String(year)] ?? [],
    }))
    .filter((s) => s.points.length > 0);

  const hasData = series.length > 0;
  const singleYear = years.length === 1;
  const latestYear = years.length ? Math.max(...years) : 0;

  // Latest YoY delta = the YoY change for the most recent month that has data.
  const latestPoints = data?.monthly_by_year[String(latestYear)] ?? [];
  const latestMonth = latestPoints.length
    ? Math.max(...latestPoints.map((p) => p.month))
    : null;
  const latestDelta =
    latestMonth !== null
      ? (data?.seasonal_yoy.find((e) => e.month === latestMonth)
          ?.latest_delta_pct ?? null)
      : null;
  const deltaToneVal = deltaTone(latestDelta);

  return (
    <ScreenLayout>
      <div className="space-y-1">
        <BackLink />
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h1 className="text-lg font-semibold tracking-tight text-zinc-900">
            {title}
          </h1>
          {hasData && (
            <span className="text-xs text-zinc-400">
              Multi-year trends · kWh
            </span>
          )}
        </div>
        {data?.summary_note && (
          <p className="text-xs text-zinc-500">{data.summary_note}</p>
        )}
      </div>

      {loading ? (
        <TrendsSkeleton />
      ) : error ? (
        <div className="flex flex-col items-center gap-3 rounded-xl border border-cream-border bg-cream p-8 text-center">
          <p className="text-sm text-zinc-500">
            We couldn&apos;t load these trends.
          </p>
          <p className="max-w-sm text-xs text-zinc-400">{error}</p>
          <Button variant="secondary" onClick={load}>
            Retry
          </Button>
        </div>
      ) : !hasData ? (
        <EmptyTrends />
      ) : (
        <>
          {/* Stat row — trailing-12mo kWh, lifetime kWh, latest YoY delta. */}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            <StatTile label="Trailing 12 mo" value={`${formatKwh(data?.ttm_kwh)} kWh`} />
            <StatTile label="Lifetime" value={`${formatKwh(data?.lifetime_kwh)} kWh`} />
            <StatTile
              label="Latest YoY"
              value={singleYear || latestDelta === null ? "—" : formatDeltaPct(latestDelta)}
              tone={deltaToneVal === "up" ? "up" : deltaToneVal === "down" ? "down" : "neutral"}
            />
            {data?.ttm_savings !== null && data?.ttm_savings !== undefined && (
              <StatTile
                label="Est. savings (12 mo)"
                value={formatUsd(data.ttm_savings)}
              />
            )}
          </div>

          {/* Multi-year monthly trend lines. */}
          <Section
            title="Monthly kWh by year"
            hint={
              singleYear
                ? "One year of history so far — year-over-year overlays appear next year."
                : "Each line is a year, Jan–Dec, so seasonality and growth read at a glance."
            }
          >
            <MultiYearLineChart series={series} years={years} />
          </Section>

          {/* Seasonal YoY — only meaningful with a prior year to compare. */}
          {!singleYear && data && data.seasonal_yoy.length > 0 && (
            <Section
              title="Seasonal year-over-year"
              hint="Latest year's value per month, with the change vs the prior year."
            >
              <SeasonalYoYRow entries={data.seasonal_yoy} latestYear={latestYear} />
            </Section>
          )}
        </>
      )}
    </ScreenLayout>
  );
}
