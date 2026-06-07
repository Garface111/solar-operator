import { useEffect, useState } from "react";
import { type ProductionData, type ProductionMonthEntry, getClientProduction } from "../../lib/api";

// ─── SVG bar chart ───────────────────────────────────────────────────────────

const W = 600, H = 176;
const PAD = { top: 8, right: 8, bottom: 32, left: 40 };
const PLOT_W = W - PAD.left - PAD.right;
const PLOT_H = H - PAD.top - PAD.bottom;

const MONTH_ABBR = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

function niceMax(v: number): number {
  if (v <= 0) return 1;
  const exp = Math.pow(10, Math.floor(Math.log10(v)));
  return Math.ceil(v / exp) * exp;
}

function fmtMwh(v: number, decimals = 2): string {
  return v.toFixed(v >= 10 ? 1 : decimals);
}

function BarChart({
  months,
  selected,
  onSelect,
}: {
  months: ProductionMonthEntry[];
  selected: number | null;
  onSelect: (i: number | null) => void;
}) {
  if (months.length === 0) return null;

  const maxMwh = niceMax(Math.max(...months.map((m) => m.mwh)));
  const ticks = [0, maxMwh / 2, maxMwh];

  const slotW = PLOT_W / months.length;
  const gap = Math.max(2, slotW * 0.15);
  const barW = Math.max(2, slotW - gap);

  function bx(i: number) { return PAD.left + i * slotW + gap / 2; }
  function bh(mwh: number) { return (mwh / maxMwh) * PLOT_H; }
  function by(mwh: number) { return PAD.top + PLOT_H - bh(mwh); }

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      style={{ width: "100%", display: "block" }}
      aria-label="Monthly solar production bar chart"
    >
      {/* Y-axis ticks + grid */}
      {ticks.map((t, i) => {
        const y = PAD.top + PLOT_H - (t / maxMwh) * PLOT_H;
        return (
          <g key={i}>
            <line
              x1={PAD.left - 3} x2={PAD.left + PLOT_W}
              y1={y} y2={y}
              stroke={i === 0 ? "#d4d4d8" : "#f4f4f5"}
              strokeWidth={1}
            />
            <text x={PAD.left - 5} y={y + 4} textAnchor="end" fontSize={9} fill="#a1a1aa">
              {fmtMwh(t, t === 0 ? 0 : 1)}
            </text>
          </g>
        );
      })}

      {/* Y-axis unit label */}
      <text
        x={4} y={PAD.top + PLOT_H / 2}
        textAnchor="middle" fontSize={8} fill="#a1a1aa"
        transform={`rotate(-90, 6, ${PAD.top + PLOT_H / 2})`}
      >
        MWh
      </text>

      {/* Bars */}
      {months.map((m, i) => {
        const isSelected = selected === i;
        const h = bh(m.mwh);
        if (h < 0.5) return null;
        return (
          <rect
            key={m.month}
            x={bx(i)} y={by(m.mwh)}
            width={barW} height={h}
            rx={2}
            fill={isSelected ? "#0284c7" : "#7dd3fc"}
            style={{ cursor: "pointer", transition: "fill 0.1s" }}
            onClick={() => onSelect(isSelected ? null : i)}
          />
        );
      })}

      {/* X-axis labels */}
      {months.map((m, i) => {
        const cx = bx(i) + barW / 2;
        const [yr, mo] = m.month.split("-").map(Number);
        const label = MONTH_ABBR[mo - 1];
        const showYear = i === 0 || (mo === 1);
        return (
          <g key={m.month}>
            <text x={cx} y={H - 14} textAnchor="middle" fontSize={9} fill="#71717a">
              {label}
            </text>
            {showYear && (
              <text x={cx} y={H - 4} textAnchor="middle" fontSize={8} fill="#a1a1aa">
                {String(yr).slice(2)}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

// ─── Stat card ───────────────────────────────────────────────────────────────

function StatBox({
  label,
  mwh,
  pctLabel,
  pct,
}: {
  label: string;
  mwh: number;
  pctLabel: string;
  pct: number | null;
}) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] font-medium uppercase tracking-wide text-zinc-400">{label}</div>
      <div className="text-lg font-semibold leading-tight text-zinc-800">
        {fmtMwh(mwh)} <span className="text-xs font-normal text-zinc-500">MWh</span>
      </div>
      {pct !== null ? (
        <div className={`text-[11px] font-medium ${pct >= 0 ? "text-emerald-600" : "text-red-500"}`}>
          {pct >= 0 ? "+" : ""}{pct}% {pctLabel}
        </div>
      ) : (
        <div className="text-[11px] text-zinc-400">— {pctLabel}</div>
      )}
    </div>
  );
}

// ─── Drill-down table ────────────────────────────────────────────────────────

function DrillDown({ month }: { month: ProductionMonthEntry }) {
  const [yr, mo] = month.month.split("-").map(Number);
  const label = `${MONTH_ABBR[mo - 1]} ${yr}`;
  return (
    <div className="mt-2 rounded-lg border border-sky-100 bg-sky-50/60 px-3 py-2">
      <div className="mb-1.5 text-[11px] font-semibold text-sky-700">{label} — per array</div>
      <table className="w-full text-xs">
        <tbody>
          {month.by_array.map((a) => (
            <tr key={a.array_id}>
              <td className="py-0.5 pr-3 text-zinc-600">{a.array_name}</td>
              <td className="py-0.5 text-right tabular-nums text-zinc-800">{fmtMwh(a.mwh)} MWh</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ─── Main component ──────────────────────────────────────────────────────────

interface Props {
  clientId: number;
}

export function ProductionChart({ clientId }: Props) {
  const [data, setData] = useState<ProductionData | null>(null);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getClientProduction(clientId, 12).then((d) => {
      if (!cancelled) { setData(d); setLoading(false); }
    }).catch(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [clientId]);

  if (loading) {
    return (
      <div className="flex h-20 items-center justify-center text-xs text-zinc-400">
        Loading production data…
      </div>
    );
  }

  if (!data || data.months.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-zinc-200 px-4 py-4 text-center text-xs text-zinc-400">
        No production data yet — more coming as bills arrive.
      </div>
    );
  }

  const { stats } = data;
  const selectedMonth = selected !== null ? data.months[selected] : null;

  return (
    <div>
      {/* Stats row */}
      <div className="mb-3 grid grid-cols-3 gap-3">
        <StatBox
          label="Last month"
          mwh={stats.last_30_days.mwh}
          pctLabel="vs prior yr"
          pct={stats.last_30_days.vs_prev_year_pct}
        />
        <StatBox
          label="Last 12 mo"
          mwh={stats.last_12_months.mwh}
          pctLabel="vs prior TTM"
          pct={stats.last_12_months.vs_prev_ttm_pct}
        />
        <StatBox
          label="YTD"
          mwh={stats.ytd.mwh}
          pctLabel=""
          pct={null}
        />
      </div>

      {/* Chart */}
      <BarChart months={data.months} selected={selected} onSelect={setSelected} />

      {/* Drill-down */}
      {selectedMonth && <DrillDown month={selectedMonth} />}

      <p className="mt-1.5 text-[10px] text-zinc-400">
        Click a bar to see per-array breakdown. Data from bill PDFs — source of truth for net-metered generation.
      </p>
    </div>
  );
}
