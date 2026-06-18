import type { SeasonalYoYEntry } from "../../../lib/api";
import { formatDeltaPct, formatKwh, deltaTone } from "./trendUtil";

interface Props {
  entries: SeasonalYoYEntry[];
  /** Latest year present — the value shown big in each month tile. */
  latestYear: number;
}

const TONE_CLASS: Record<string, string> = {
  // Green up, muted-red down, neutral flat — consistent with the rest of the app.
  up: "bg-primary-50 text-primary-700 border-primary-200",
  down: "bg-red-50 text-red-600 border-red-200",
  flat: "bg-zinc-100 text-zinc-500 border-zinc-200",
  none: "bg-zinc-50 text-zinc-400 border-zinc-200",
};

const TONE_ARROW: Record<string, string> = {
  up: "▲",
  down: "▼",
  flat: "→",
  none: "",
};

/** Pick the value to feature for a month: the latest year if present, else the
 *  highest year that has a value for that month. */
function featuredValue(entry: SeasonalYoYEntry, latestYear: number): number | null {
  const keys = Object.keys(entry.by_year);
  if (keys.length === 0) return null;
  if (entry.by_year[String(latestYear)] !== undefined) {
    return entry.by_year[String(latestYear)];
  }
  const maxYear = Math.max(...keys.map(Number));
  return entry.by_year[String(maxYear)] ?? null;
}

/**
 * Seasonal year-over-year — a responsive small-multiples grid, one tile per
 * calendar month that has data. Each tile shows the latest year's value for
 * that month and the YoY delta (green up / muted-red down). Months without a
 * prior-year comparison simply omit the delta badge.
 */
export function SeasonalYoYRow({ entries, latestYear }: Props) {
  const months = entries.filter((e) => Object.keys(e.by_year).length > 0);
  if (months.length === 0) return null;

  return (
    <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-6">
      {months.map((e) => {
        const tone = deltaTone(e.latest_delta_pct);
        const value = featuredValue(e, latestYear);
        return (
          <div
            key={e.month}
            className="rounded-lg border border-cream-border bg-white px-2.5 py-2"
          >
            <div className="text-[10px] font-semibold uppercase tracking-wide text-zinc-400">
              {e.label}
            </div>
            <div className="mt-0.5 text-sm font-semibold tabular-nums text-zinc-800">
              {formatKwh(value)}
            </div>
            {e.latest_delta_pct !== null ? (
              <span
                className={[
                  "mt-1 inline-flex items-center gap-0.5 rounded-full border px-1.5 py-0.5",
                  "text-[10px] font-medium tabular-nums",
                  TONE_CLASS[tone],
                ].join(" ")}
                title={`Year-over-year change for ${e.label}`}
              >
                {TONE_ARROW[tone] && <span aria-hidden>{TONE_ARROW[tone]}</span>}
                {formatDeltaPct(e.latest_delta_pct)}
              </span>
            ) : (
              <span className="mt-1 inline-block text-[10px] text-zinc-300">
                no prior yr
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}
