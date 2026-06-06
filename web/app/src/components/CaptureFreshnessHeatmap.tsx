import { useCallback, useEffect, useState } from "react";
import { listArrays, type UtilityAccount } from "../lib/api";

interface Props {
  clientId: number;
  /** Total number of accounts so we can render a placeholder skeleton at the
   *  right size. (ClientCard passes array_count — a close-enough proxy.) */
  accountCount: number;
}

// One dot per account. Color encodes recency.
// fresh   <= 7d   emerald-500
// recent  8-30d   emerald-200
// stale   31-90d  wood-300
// cold    > 90d   red-300
// unknown null    zinc-200 (no sync ever recorded)

type Status = "fresh" | "recent" | "stale" | "cold" | "unknown";

const DOT_CLASS: Record<Status, string> = {
  fresh: "bg-emerald-500",
  recent: "bg-emerald-200",
  stale: "bg-wood-300",
  cold: "bg-red-300",
  unknown: "bg-zinc-200",
};

const DAY_MS = 86_400_000;

/** Map a last-synced timestamp to a freshness bucket. */
function statusFor(lastSynced: string | null | undefined): Status {
  if (!lastSynced) return "unknown";
  const t = new Date(lastSynced).getTime();
  if (Number.isNaN(t)) return "unknown";
  const days = (Date.now() - t) / DAY_MS;
  if (days <= 7) return "fresh";
  if (days <= 30) return "recent";
  if (days <= 90) return "stale";
  return "cold";
}

/** "today" / "yesterday" / "4 days ago" / "3 months ago" / "never". */
function relativeCapture(lastSynced: string | null | undefined): string {
  if (!lastSynced) return "never";
  const t = new Date(lastSynced).getTime();
  if (Number.isNaN(t)) return "never";
  const days = Math.floor((Date.now() - t) / DAY_MS);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 30) return `${days} days ago`;
  const months = Math.round(days / 30);
  if (months < 12) return `${months} month${months === 1 ? "" : "s"} ago`;
  const years = Math.round(days / 365);
  return `${years} year${years === 1 ? "" : "s"} ago`;
}

/** Show only the last 4 chars of an account number — same restraint the rest of
 *  the UI uses for utility identifiers in passing. */
function maskAccount(num: string): string {
  const clean = (num || "").trim();
  if (clean.length <= 4) return clean || "account";
  return `••••${clean.slice(-4)}`;
}

// Worst-status precedence (worst first). Drives the summary chip: we surface the
// most actionable problem present. `recent` is genuinely fine (≤30d), so a board
// that's only fresh+recent still reads as "All fresh".
const PRECEDENCE: Status[] = ["cold", "stale", "unknown", "recent"];

interface ChipSpec {
  label: string;
  className: string;
}

function summaryChip(counts: Record<Status, number>): ChipSpec {
  const allFresh: ChipSpec = {
    label: "All fresh",
    className: "bg-emerald-50 text-emerald-700",
  };
  const worst = PRECEDENCE.find((s) => counts[s] > 0);
  if (!worst) return allFresh;
  switch (worst) {
    case "cold":
      return { label: `${counts.cold} cold`, className: "bg-red-50 text-red-700" };
    case "stale":
      return { label: `${counts.stale} stale`, className: "bg-wood-50 text-wood-600" };
    case "unknown":
      return {
        label: `${counts.unknown} never synced`,
        className: "bg-zinc-100 text-zinc-600",
      };
    case "recent":
      // Everything is fresh-or-recent — still the rewarding "on top of it" state.
      return allFresh;
    default:
      return allFresh;
  }
}

/** Scroll to the matching array row below and flash it. Reuses the existing
 *  `data-nepool-array-id` hook on ArrayRow and the `so-row-highlight` flash
 *  animation (the same one the reports bounce-strip uses) — no ArrayList change. */
function flashArrayRow(arrayId: number | null | undefined): void {
  if (arrayId == null) return;
  const el = document.querySelector<HTMLElement>(
    `[data-nepool-array-id="${arrayId}"]`,
  );
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.classList.remove("so-row-highlight");
  // Force reflow so re-adding the class restarts the animation on repeat clicks.
  void el.offsetWidth;
  el.classList.add("so-row-highlight");
  window.setTimeout(() => el.classList.remove("so-row-highlight"), 1600);
}

interface AccountFreshness {
  id: number;
  arrayId: number;
  status: Status;
  title: string;
}

export function CaptureFreshnessHeatmap({ clientId, accountCount }: Props) {
  const [accounts, setAccounts] = useState<AccountFreshness[] | null>(null);
  const [error, setError] = useState(false);

  const load = useCallback(() => {
    let cancelled = false;
    setError(false);
    listArrays(clientId)
      .then((rows) => {
        if (cancelled) return;
        const flat: AccountFreshness[] = [];
        for (const a of rows) {
          for (const ac of a.accounts as UtilityAccount[]) {
            flat.push({
              id: ac.id,
              arrayId: a.id,
              status: statusFor(ac.last_synced_at),
              title: `${maskAccount(ac.account_number)} · Last captured: ${relativeCapture(
                ac.last_synced_at,
              )}`,
            });
          }
        }
        setAccounts(flat);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      });
    return () => {
      cancelled = true;
    };
  }, [clientId]);

  useEffect(() => {
    const cancel = load();
    // Refetch on the same broadcast ArrayList listens for, so a fresh capture
    // (autopop / import / manual edit) repaints the dots without a reload.
    window.addEventListener("so:arrays-changed", load);
    return () => {
      cancel?.();
      window.removeEventListener("so:arrays-changed", load);
    };
  }, [load]);

  return (
    <div className="rounded-xl border border-cream-border bg-cream p-4 sm:p-5">
      <div className="flex items-center justify-between gap-2">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-zinc-400">
          Last capture
        </h4>
        {error ? null : accounts === null ? (
          <span className="h-5 w-16 animate-pulse rounded-full bg-zinc-200" aria-hidden />
        ) : (
          accounts.length > 0 &&
          (() => {
            const counts: Record<Status, number> = {
              fresh: 0,
              recent: 0,
              stale: 0,
              cold: 0,
              unknown: 0,
            };
            for (const a of accounts) counts[a.status]++;
            const chip = summaryChip(counts);
            return (
              <span
                className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${chip.className}`}
              >
                {chip.label}
              </span>
            );
          })()
        )}
      </div>

      {error ? (
        <div className="mt-4 flex items-center gap-3">
          <span className="text-sm text-red-500">Couldn't load capture freshness</span>
          <button
            type="button"
            onClick={() => {
              setAccounts(null);
              load();
            }}
            className="rounded text-xs font-medium text-primary-600 transition-colors hover:text-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-1"
          >
            Retry
          </button>
        </div>
      ) : accounts === null ? (
        // Loading skeleton — sized off accountCount, capped so it stays calm.
        <div className="mt-4 flex flex-wrap gap-1.5" aria-hidden>
          {Array.from({ length: Math.min(Math.max(accountCount, 1), 12) || 5 }).map(
            (_, i) => (
              <span
                key={i}
                className="h-3 w-3 animate-pulse rounded-full bg-zinc-200"
              />
            ),
          )}
        </div>
      ) : accounts.length === 0 ? (
        <p className="mt-3 text-sm leading-snug text-zinc-500">
          No utility accounts captured yet — the extension will populate this once
          you log in to your portal.
        </p>
      ) : (
        <>
          <div className="mt-4 flex flex-wrap items-center gap-1.5">
            {accounts.map((a) => (
              <button
                key={a.id}
                type="button"
                title={a.title}
                aria-label={a.title}
                onClick={() => flashArrayRow(a.arrayId)}
                data-account-id={a.id}
                data-status={a.status}
                className={`h-3 w-3 rounded-full opacity-90 transition-opacity hover:opacity-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/50 focus-visible:ring-offset-1 ${DOT_CLASS[a.status]}`}
              />
            ))}
            <span className="ml-1 text-xs tabular-nums text-zinc-400">
              {accounts.length}
            </span>
          </div>

          {/* Legend */}
          <div className="mt-4 flex flex-wrap gap-x-4 gap-y-1.5 text-[11px] text-zinc-500">
            <LegendItem status="fresh" label="Fresh ≤7d" />
            <LegendItem status="recent" label="Recent ≤30d" />
            <LegendItem status="stale" label="Stale ≤90d" />
            <LegendItem status="cold" label="Cold >90d" />
          </div>
        </>
      )}
    </div>
  );
}

function LegendItem({ status, label }: { status: Status; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={`h-2 w-2 rounded-full ${DOT_CLASS[status]}`} aria-hidden />
      {label}
    </span>
  );
}
