import { useCallback, useEffect, useState } from "react";
import { getQuarterlyProgress, type QuarterlyProgress } from "../lib/api";

/** Month string "2026-06" → "Jun" */
function shortMonth(ym: string): string {
  const [y, m] = ym.split("-").map(Number);
  if (!y || !m) return ym;
  return new Date(y, m - 1, 1).toLocaleString("en-US", { month: "short" });
}

interface Props {
  clientId: number;
  /** Called when the operator clicks "Send reports" on the all-ready state. */
  onSendReports?: () => void;
}

export function QuarterlyProgressChip({ clientId, onSendReports }: Props) {
  const [progress, setProgress] = useState<QuarterlyProgress | null>(null);
  const [error, setError] = useState(false);

  const load = useCallback(() => {
    let cancelled = false;
    setError(false);
    getQuarterlyProgress(clientId)
      .then((p) => { if (!cancelled) setProgress(p); })
      .catch(() => { if (!cancelled) setError(true); });
    return () => { cancelled = true; };
  }, [clientId]);

  useEffect(() => {
    const cancel = load();
    // Re-fetch when arrays or bills change so the chip stays live.
    window.addEventListener("so:arrays-changed", load);
    return () => {
      cancel?.();
      window.removeEventListener("so:arrays-changed", load);
    };
  }, [load]);

  // Loading skeleton
  if (!progress && !error) {
    return (
      <div className="rounded-xl border border-cream-border bg-cream p-4">
        <div className="flex items-center justify-between gap-2">
          <span className="h-3 w-16 animate-pulse rounded bg-zinc-200" aria-hidden />
          <span className="h-4 w-20 animate-pulse rounded-full bg-zinc-200" aria-hidden />
        </div>
      </div>
    );
  }

  if (error || !progress) {
    return null;
  }

  const { quarter, ready_arrays, missing_arrays, total_arrays, all_ready } = progress;

  // ── All ready ─────────────────────────────────────────────────────────
  if (all_ready) {
    return (
      <div
        data-testid="quarterly-progress-chip"
        className="rounded-xl border border-emerald-200 bg-emerald-50 p-3"
      >
        <div className="flex items-center gap-2">
          <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-emerald-100 text-[11px] font-bold text-emerald-700">
            ✓
          </span>
          <div className="min-w-0 flex-1">
            <span className="block text-[10px] font-semibold uppercase tracking-wider text-emerald-600/80">
              {quarter}
            </span>
            <span className="text-xs font-semibold text-emerald-800">
              Reports ready to ship
            </span>
          </div>
          {onSendReports && (
            <button
              type="button"
              onClick={onSendReports}
              className="shrink-0 rounded-lg border border-emerald-200 bg-white px-2.5 py-1 text-xs font-medium text-emerald-700 transition-colors hover:bg-emerald-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/40"
            >
              Send
            </button>
          )}
        </div>
      </div>
    );
  }

  // ── In progress ───────────────────────────────────────────────────────
  const readyCount = ready_arrays.length;
  return (
    <div
      data-testid="quarterly-progress-chip"
      className="rounded-xl border border-amber-200 bg-amber-50 p-3"
    >
      {/* Header row */}
      <div className="flex items-center gap-2">
        <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-amber-100 text-[11px] font-bold text-amber-700">
          ◆
        </span>
        <div className="min-w-0 flex-1">
          <span className="block text-[10px] font-semibold uppercase tracking-wider text-amber-600/80">
            {quarter}
          </span>
          <span className="text-xs font-semibold text-amber-900">
            {readyCount} of {total_arrays}{" "}
            {total_arrays === 1 ? "array" : "arrays"} ready
          </span>
        </div>
      </div>

      {/* Missing list — max 5 shown to stay compact */}
      {missing_arrays.length > 0 && (
        <div className="mt-2 space-y-0.5 border-t border-amber-200/60 pt-2">
          {missing_arrays.slice(0, 5).map((arr) => (
            <div
              key={arr.id}
              className="flex items-baseline gap-1.5 text-[11px] text-amber-800"
            >
              <span className="shrink-0 text-amber-400">·</span>
              <span className="font-medium">{arr.name}</span>
              <span className="text-amber-600/70">
                missing {arr.missing_months.map(shortMonth).join(", ")}
              </span>
            </div>
          ))}
          {missing_arrays.length > 5 && (
            <div className="text-[11px] text-amber-600/70">
              + {missing_arrays.length - 5} more
            </div>
          )}
        </div>
      )}
    </div>
  );
}
