import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { type SpongeStatus, getSpongeStatus } from "../../lib/api";

/**
 * Live "data sponge" progress — the moment the owner connects GMP, the backend
 * absorbs their entire energy history (years of bills). This polls the absorb's
 * progress and renders a progress bar ("Importing your N years…"), then settles
 * into a done state that links to the full energy-history view.
 *
 * Renders nothing in the idle state (no absorb has ever run) so it stays out of
 * the way until there's something to show.
 */
export function SpongeProgressCard() {
  const [status, setStatus] = useState<SpongeStatus | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dismissedRef = useRef(false);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    let alive = true;
    const poll = () => {
      getSpongeStatus()
        .then((s) => {
          if (!alive) return;
          setStatus(s);
          // Keep polling while the absorb is running; stop once done/error/idle.
          if (s.status === "running") {
            timer.current = setTimeout(poll, 1500);
          }
        })
        .catch(() => {
          // network blip — retry softly a couple of times while running
          if (alive) timer.current = setTimeout(poll, 3000);
        });
    };
    poll();
    return () => {
      alive = false;
      if (timer.current) clearTimeout(timer.current);
    };
  }, []);

  if (!status || status.status === "idle" || dismissed) return null;

  const running = status.status === "running";
  const errored = status.status === "error";
  const pct = Math.max(0, Math.min(100, running ? status.pct : errored ? 100 : 100));

  const yearsText =
    status.years_covered != null ? `${status.years_covered} years` : "your history";

  const headline = running
    ? "Importing your energy history…"
    : errored
      ? "We couldn't finish importing your history"
      : `${yearsText} of energy history imported`;

  const sub =
    status.message ??
    (running
      ? `${status.bills_absorbed} bills so far…`
      : errored
        ? "Reconnect your utility login to try again."
        : `${status.bills_absorbed} billing periods are now in your account.`);

  return (
    <div className="mb-6">
      <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-zinc-400">
        Energy history
      </h2>
      <div
        className={`rounded-2xl border p-5 shadow-sm ${
          errored
            ? "border-amber-200 bg-amber-50/50"
            : "border-cream-border bg-cream"
        }`}
      >
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span aria-hidden className="text-base">
                {running ? "🪣" : errored ? "⚠️" : "✅"}
              </span>
              <p className="text-sm font-semibold text-zinc-800">{headline}</p>
            </div>
            <p className="mt-0.5 text-xs text-zinc-500">{sub}</p>
          </div>
          {!running && !errored && (
            <button
              type="button"
              onClick={() => {
                dismissedRef.current = true;
                setDismissed(true);
              }}
              className="shrink-0 text-xs text-zinc-400 hover:text-zinc-600"
              aria-label="Dismiss"
            >
              ✕
            </button>
          )}
        </div>

        {/* Progress bar */}
        <div className="mt-3 h-2 w-full overflow-hidden rounded-full bg-white/70">
          <div
            className={`h-full rounded-full transition-all duration-500 ${
              errored
                ? "bg-amber-400"
                : running
                  ? "bg-primary-500"
                  : "bg-primary-600"
            } ${running ? "animate-pulse" : ""}`}
            style={{ width: `${pct}%` }}
            role="progressbar"
            aria-valuenow={pct}
            aria-valuemin={0}
            aria-valuemax={100}
          />
        </div>

        {running && status.accounts_total > 0 && (
          <p className="mt-2 text-[11px] text-zinc-400">
            {status.accounts_done}/{status.accounts_total} accounts ·{" "}
            {status.bills_absorbed} bills
          </p>
        )}

        {!running && !errored && (
          <div className="mt-3">
            <Link
              to="/account/energy-history"
              className="inline-flex items-center gap-1 rounded-lg bg-primary-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-primary-700"
            >
              View your energy history →
            </Link>
          </div>
        )}
      </div>
    </div>
  );
}
