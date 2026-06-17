import type { TrendMonthPoint } from "../../../lib/api";
import {
  MONTH_ABBR,
  MONTH_INITIALS,
  formatKwh,
  formatKwhCompact,
  yearColor,
} from "./trendUtil";

export interface YearSeries {
  year: number;
  points: TrendMonthPoint[];
}

interface Props {
  series: YearSeries[];
  /** All years present (for stable color assignment + legend ordering). */
  years: number[];
}

// Fixed coordinate system; the SVG scales to its container via width:100%.
const VB_W = 480;
const VB_H = 240;
const PAD = { top: 14, right: 14, bottom: 26, left: 44 };
const PLOT_W = VB_W - PAD.left - PAD.right;
const PLOT_H = VB_H - PAD.top - PAD.bottom;

/** x for a calendar month 1–12, spread Jan(left)…Dec(right). */
function xForMonth(month: number): number {
  return PAD.left + ((month - 1) / 11) * PLOT_W;
}

/** "nice" tick values across [min,max]; 4 evenly spaced steps. */
function ticks(min: number, max: number, count = 4): number[] {
  if (max <= min) return [min];
  const step = (max - min) / count;
  return Array.from({ length: count + 1 }, (_, i) => min + i * step);
}

/**
 * Multi-year monthly trend lines — one polyline per year over Jan–Dec, overlaid
 * so seasonality and year-over-year growth read at a glance. Pure SVG, no deps.
 * Single-point years still render a dot so a one-period year isn't invisible.
 */
export function MultiYearLineChart({ series, years }: Props) {
  const withData = series.filter((s) => s.points.length > 0);
  const allKwh = withData.flatMap((s) => s.points.map((p) => p.kwh));

  // Domain: anchor the baseline at 0 for honest scale (kWh is ≥ 0 in practice,
  // but Math.min guards a stray negative net-metering value).
  const dataMax = allKwh.length ? Math.max(...allKwh) : 1;
  const dataMin = allKwh.length ? Math.min(...allKwh) : 0;
  const yMin = Math.min(0, dataMin);
  const yMax = dataMax > yMin ? dataMax : yMin + 1; // avoid zero-height domain

  function yForKwh(kwh: number): number {
    return PAD.top + (1 - (kwh - yMin) / (yMax - yMin)) * PLOT_H;
  }

  const yTicks = ticks(yMin, yMax);
  const orderedYears = [...years].sort((a, b) => a - b);

  // A1y summary for screen readers.
  const ariaLabel =
    `Monthly kWh by year. ${withData.length} ` +
    `${withData.length === 1 ? "year" : "years"}: ` +
    orderedYears.join(", ") + ".";

  return (
    <div>
      {/* Legend */}
      <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1">
        {orderedYears.map((year) => (
          <span key={year} className="inline-flex items-center gap-1.5 text-xs text-zinc-600">
            <span
              aria-hidden
              className="inline-block h-2.5 w-2.5 rounded-full"
              style={{ backgroundColor: yearColor(year, years) }}
            />
            {year}
          </span>
        ))}
      </div>

      <svg
        viewBox={`0 0 ${VB_W} ${VB_H}`}
        className="h-auto w-full"
        preserveAspectRatio="xMidYMid meet"
        role="img"
        aria-label={ariaLabel}
      >
        {/* Horizontal gridlines + y labels */}
        {yTicks.map((t, i) => {
          const y = yForKwh(t);
          return (
            <g key={i}>
              <line
                x1={PAD.left}
                x2={VB_W - PAD.right}
                y1={y}
                y2={y}
                stroke="#e8e2d9"
                strokeWidth={1}
              />
              <text
                x={PAD.left - 6}
                y={y + 3}
                textAnchor="end"
                fontSize={9}
                fill="#a1a1aa"
              >
                {formatKwhCompact(t)}
              </text>
            </g>
          );
        })}

        {/* x-axis month labels */}
        {MONTH_INITIALS.map((m, i) => (
          <text
            key={i}
            x={xForMonth(i + 1)}
            y={VB_H - 8}
            textAnchor="middle"
            fontSize={9}
            fill="#a1a1aa"
          >
            {m}
          </text>
        ))}

        {/* One line (+ dots) per year */}
        {orderedYears.map((year) => {
          const s = withData.find((x) => x.year === year);
          if (!s) return null;
          const color = yearColor(year, years);
          const isLatest = year === orderedYears[orderedYears.length - 1];
          const pts = s.points
            .map((p) => `${xForMonth(p.month)},${yForKwh(p.kwh)}`)
            .join(" ");
          return (
            <g key={year}>
              {s.points.length > 1 && (
                <polyline
                  points={pts}
                  fill="none"
                  stroke={color}
                  strokeWidth={isLatest ? 2.5 : 1.5}
                  strokeLinejoin="round"
                  strokeLinecap="round"
                  opacity={isLatest ? 1 : 0.85}
                />
              )}
              {s.points.map((p) => (
                <circle
                  key={p.month}
                  cx={xForMonth(p.month)}
                  cy={yForKwh(p.kwh)}
                  r={isLatest ? 2.5 : 2}
                  fill={color}
                >
                  <title>{`${year} · ${MONTH_ABBR[p.month - 1]} · ${formatKwh(p.kwh)} kWh`}</title>
                </circle>
              ))}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
