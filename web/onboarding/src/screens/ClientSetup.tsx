import { useEffect, useState } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import { startOnboarding, setToken } from "../lib/onboarding";
import { SO_OPERATOR_KEY, SO_OPERATOR_PASSWORD_KEY, type OperatorInfo } from "./Info";

/**
 * Page 3 — Array count estimate + checkout handoff.
 *
 * Previously this page only collected the array-count estimate and then
 * handed off to a separate "Plan" review screen. The review screen showed
 * the same setup fee + monthly math already visible here, so it was
 * redundant friction — we now call createCheckout() directly from this
 * screen's Continue button.
 *
 * The previous version of this screen also made the operator enter each
 * client's name, contact email, and per-array NEPOOL-GIS IDs BEFORE
 * checkout — 5-15 minutes of pointless friction. Every one of those
 * fields gets auto-populated by the extension or entered post-payment on
 * the dashboard, where the operator already has the context.
 *
 * All we actually need is a number to seed Stripe Checkout's quantity.
 * The post-payment dashboard reconciles to the real count automatically
 * as the extension captures land (see
 * stripe_helpers.reconcile_subscription_quantity).
 */
export const SO_ARRAY_ESTIMATE_KEY = "so_array_estimate";

// Back-compat key kept so older sessions don't blow up on read.
export const SO_CLIENTS_DRAFT_KEY = "so_clients_draft";

export interface ClientDraftEntry {
  name: string;
  contact_email?: string;
  arrays?: { name: string; nepool_gis_id?: string }[];
}

const ARRAY_PRICE = 15;
const SETUP_FEE = 250;

const QUICK_PICKS = [5, 10, 25, 50, 100, 250, 500];

function readOperatorInfo(locationState: unknown): OperatorInfo | null {
  if (locationState && typeof (locationState as OperatorInfo).email === "string") {
    return locationState as OperatorInfo;
  }
  try {
    const raw = sessionStorage.getItem(SO_OPERATOR_KEY);
    return raw ? (JSON.parse(raw) as OperatorInfo) : null;
  } catch {
    return null;
  }
}

export default function ClientSetup() {
  const navigate = useNavigate();
  const location = useLocation();
  const toast = useToast();

  const info = readOperatorInfo(location.state);

  useEffect(() => {
    if (!info?.email) {
      navigate("/info", { replace: true });
    }
  }, []);

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
  const [submitting, setSubmitting] = useState(false);

  if (!info?.email) return null;

  const monthly = Math.max(0, count) * ARRAY_PRICE;
  const canContinue = count >= 1 && !submitting;

  async function handleContinue() {
    if (!canContinue || !info) return;
    sessionStorage.setItem(SO_ARRAY_ESTIMATE_KEY, String(count));
    // Clear any old per-client draft so backend uses the array_count path.
    sessionStorage.setItem(SO_CLIENTS_DRAFT_KEY, JSON.stringify([]));

    setSubmitting(true);
    try {
      // No card collected — start a live trial immediately. The password chosen
      // on /info (if any) is hashed server-side here; it's also re-sent at
      // /complete (Done.tsx), so we leave it stashed for that step.
      let password: string | undefined;
      try {
        password = sessionStorage.getItem(SO_OPERATOR_PASSWORD_KEY) || undefined;
      } catch {
        /* sessionStorage may be locked down — password still set at /complete */
      }
      const { onboarding_token } = await startOnboarding({
        full_name: info.fullName,
        email: info.email,
        company: info.company,
        password,
        array_count: count,
      });
      setToken(onboarding_token);
      navigate("/extension");
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : "Couldn't start your trial. Check your connection and try again.",
      );
      setSubmitting(false);
    }
  }

  return (
    <ScreenLayout current={2}>
      <Card active>
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">
          About how many arrays do you manage?
        </h1>
        <p className="mt-2 text-sm text-zinc-500">
          A ballpark is fine — we use it to set up your subscription. No payment
          today; your 14-day free trial begins right now. You&apos;ll add your
          real clients and arrays in the dashboard, and the extension
          auto-populates most of it when you log into your utility portal. We
          reconcile your subscription quantity automatically as the real count
          comes in.
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

        {/* Billing breakdown — no payment today. Nothing is collected at
            signup; the operator adds a card later from the dashboard. */}
        <div className="mt-8 rounded-xl border border-primary-200 bg-primary-50 p-5 space-y-3">
          <div className="flex items-center justify-between text-sm font-semibold text-primary-800">
            <span>After your trial</span>
            <span>${SETUP_FEE + monthly}</span>
          </div>
          <p className="text-xs text-primary-700">
            Your 14-day free trial starts right now — no payment today. Use it
            to add your clients and arrays. We&apos;ll email you a few days
            before the trial ends so you can add a card.
          </p>
          <div className="border-t border-primary-200 pt-3 space-y-2 text-xs text-primary-800">
            <div className="flex items-center justify-between">
              <span>After trial — one-time setup</span>
              <span>${SETUP_FEE}</span>
            </div>
            <div className="flex items-center justify-between">
              <span>
                Monthly ({count} {count === 1 ? "array" : "arrays"} × ${ARRAY_PRICE})
              </span>
              <span>${monthly}/month</span>
            </div>
            <div className="flex items-center justify-between font-semibold border-t border-primary-200 pt-2">
              <span>Total at trial end</span>
              <span>${SETUP_FEE + monthly}</span>
            </div>
            <p className="text-xs text-primary-700 pt-1">
              If you have zero arrays at trial end, a one-array minimum ($15) applies so your subscription stays active.
            </p>
            <p className="text-xs text-primary-700 pt-1">
              You&apos;ll add your card later from the dashboard.
            </p>
          </div>
        </div>

        <div className="mt-8 flex items-center justify-between">
          <Button variant="ghost" onClick={() => navigate("/info")} disabled={submitting}>
            ← Back
          </Button>
          <Button onClick={handleContinue} disabled={!canContinue}>
            {submitting ? (
              <>
                <Spinner />
                Starting…
              </>
            ) : (
              "Start my free trial →"
            )}
          </Button>
        </div>

        <p className="mt-4 text-xs text-zinc-500">
          Chrome desktop required — extension scrapes utility portals
        </p>

        <p className="mt-2 text-xs text-zinc-400">
          Not sure yet? Pick the closest estimate — your subscription
          quantity reconciles automatically once your real arrays are set
          up in the dashboard. No payment today, and you won&apos;t be charged
          later without notice. You&apos;ll add your card from the dashboard
          when you&apos;re ready; it&apos;s stored securely by Stripe — we never
          see it.
        </p>
      </Card>
    </ScreenLayout>
  );
}
