import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";

/**
 * Page 3 — Array count estimate.
 *
 * The previous version of this screen made the operator enter each client's
 * name, contact email, and per-array NEPOOL-GIS IDs BEFORE checkout. That's
 * 5-15 minutes of friction with zero payoff — every one of those fields
 * gets auto-populated by the extension or entered post-payment on the
 * dashboard, where the operator already has the context.
 *
 * All we actually need from this page is a number to seed Stripe Checkout's
 * quantity: roughly how many arrays does this operator manage? The Plan
 * screen uses that for billing math, the post-payment dashboard reconciles
 * to the real count automatically as the extension captures land.
 *
 * If the operator literally doesn't know yet, defaulting to 1 keeps them
 * moving — the subscription reconciles upward later via
 * stripe_helpers.reconcile_subscription_quantity.
 */
export const SO_ARRAY_ESTIMATE_KEY = "so_array_estimate";

// Backward-compat: the Plan screen still reads SO_CLIENTS_DRAFT_KEY to decide
// whether to send a seeded clients payload or just an array_count. We write
// an empty draft here so Plan falls into the array_count path cleanly.
export const SO_CLIENTS_DRAFT_KEY = "so_clients_draft";

/**
 * Kept for back-compat with Plan.tsx's legacy seeded-clients path. Anyone
 * still reading SO_CLIENTS_DRAFT_KEY (e.g. a session that started before
 * this screen was simplified) will see this shape.
 */
export interface ClientDraftEntry {
  name: string;
  contact_email?: string;
  arrays?: { name: string; nepool_gis_id?: string }[];
}

const ARRAY_PRICE = 45;

const QUICK_PICKS = [10, 25, 50, 100, 250, 500];

export default function ClientSetup() {
  const navigate = useNavigate();

  const initial = (() => {
    try {
      const raw = sessionStorage.getItem(SO_ARRAY_ESTIMATE_KEY);
      const n = raw ? parseInt(raw, 10) : NaN;
      return Number.isFinite(n) && n > 0 ? n : 1;
    } catch {
      return 1;
    }
  })();

  const [count, setCount] = useState<number>(initial);

  const monthly = Math.max(0, count) * ARRAY_PRICE;
  const canContinue = count >= 1;

  function handleContinue() {
    if (!canContinue) return;
    sessionStorage.setItem(SO_ARRAY_ESTIMATE_KEY, String(count));
    // Clear any old per-client draft so Plan uses the estimate path.
    sessionStorage.setItem(SO_CLIENTS_DRAFT_KEY, JSON.stringify([]));
    navigate("/plan");
  }

  return (
    <ScreenLayout current={2}>
      <Card active>
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">
          About how many arrays do you manage?
        </h1>
        <p className="mt-2 text-sm text-zinc-500">
          A ballpark is fine — we just need it to set up your subscription.
          You&apos;ll add the exact clients and arrays from your dashboard
          after checkout, and the extension will auto-populate most of it
          when you log into your utility portal. Billing reconciles
          automatically as the real count is established.
        </p>

        {/* Quick picks */}
        <div className="mt-8">
          <span className="text-xs font-medium uppercase tracking-wide text-zinc-500">
            Quick pick
          </span>
          <div className="mt-2 flex flex-wrap gap-2">
            {QUICK_PICKS.map((n) => {
              const selected = count === n;
              return (
                <button
                  key={n}
                  type="button"
                  onClick={() => setCount(n)}
                  className={`rounded-xl border px-4 py-2 text-sm font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2 ${
                    selected
                      ? "border-primary-500 bg-primary-50 text-primary-700"
                      : "border-zinc-200 bg-white text-zinc-700 hover:border-zinc-300 hover:bg-zinc-50"
                  }`}
                >
                  {n}
                </button>
              );
            })}
          </div>
        </div>

        {/* Manual number input */}
        <div className="mt-6">
          <label
            htmlFor="array-count"
            className="block text-xs font-medium uppercase tracking-wide text-zinc-500"
          >
            Or enter a specific number
          </label>
          <div className="mt-2 flex items-center gap-3">
            <input
              id="array-count"
              type="number"
              inputMode="numeric"
              min={1}
              value={String(count)}
              onChange={(e) => {
                const n = parseInt(e.target.value, 10);
                setCount(Number.isFinite(n) && n > 0 ? n : 1);
              }}
              className="w-32 rounded-xl border border-zinc-300 px-4 py-3 text-lg font-semibold text-zinc-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500/30"
            />
            <span className="text-sm text-zinc-500">
              {count === 1 ? "array" : "arrays"}
            </span>
          </div>
        </div>

        {/* Pricing preview */}
        <div className="mt-8 rounded-xl border border-primary-200 bg-primary-50 px-5 py-4">
          <div className="flex items-center justify-between">
            <span className="text-sm text-primary-800">
              {count} {count === 1 ? "array" : "arrays"} × $45/month
            </span>
            <span className="text-lg font-semibold text-primary-900">
              ${monthly}/month
            </span>
          </div>
          <p className="mt-1 text-[11px] text-primary-700">
            Plus a one-time $250 setup fee. You&apos;ll see the full breakdown
            on the next screen.
          </p>
        </div>

        <div className="mt-8 flex items-center justify-between">
          <Button variant="ghost" onClick={() => navigate("/info")}>
            ← Back
          </Button>
          <Button onClick={handleContinue} disabled={!canContinue}>
            Review pricing →
          </Button>
        </div>

        <p className="mt-6 text-xs text-zinc-400">
          Not sure yet? Pick the closest estimate — your subscription
          quantity is reconciled automatically once your real arrays are
          set up in the dashboard. You won&apos;t be charged extra without
          notice.
        </p>
      </Card>
    </ScreenLayout>
  );
}
