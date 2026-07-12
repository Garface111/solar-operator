import { useEffect, useState } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import { startOnboarding, setToken, cloudCaptureUiEnabled } from "../lib/onboarding";
import { SO_OPERATOR_KEY, SO_OPERATOR_PASSWORD_KEY, type OperatorInfo } from "./Info";
import {
  monthlyDollars,
  blendedUnitCents,
  savingsCents,
  discountPct,
  hasVolumeDiscount,
  tierBreakdown,
  FULL_UNIT_CENTS,
} from "../pricing";

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
 *
 * The pricing panel is interactive on purpose: dragging the slider lets the
 * operator *watch* the effective per-array rate fall and the volume bands
 * light up as they scale, so the discount is felt, not buried in fine print.
 * All math comes from ../pricing (a mirror of api/pricing.py and the live
 * Stripe graduated price), so this preview matches the real invoice.
 */
export const SO_ARRAY_ESTIMATE_KEY = "so_array_estimate";

// Back-compat key kept so older sessions don't blow up on read.
export const SO_CLIENTS_DRAFT_KEY = "so_clients_draft";

export interface ClientDraftEntry {
  name: string;
  contact_email?: string;
  arrays?: { name: string; nepool_gis_id?: string }[];
}

const SETUP_FEE = 250;

const QUICK_PICKS = [5, 25, 50, 100, 250, 500];

// Slider covers all four tiers with headroom; the number input + quick picks
// handle counts beyond this for the rare very-large operator.
const SLIDER_MAX = 300;

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

function dollars(cents: number): string {
  // Whole-dollar pricing throughout; show cents only if a band ever needs it.
  return cents % 100 === 0
    ? `$${cents / 100}`
    : `$${(cents / 100).toFixed(2)}`;
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

  const safeCount = Math.max(1, count);
  const monthly = monthlyDollars(safeCount);
  const blendedCents = blendedUnitCents(safeCount);
  const saved = savingsCents(safeCount);
  const pctOff = discountPct(safeCount);
  const showVolume = hasVolumeDiscount(safeCount);
  const bands = tierBreakdown(safeCount);
  const sliderValue = Math.min(safeCount, SLIDER_MAX);
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
      // Cloud-Capture fork (dark-shipped): with the flag on, offer store-with-us
      // vs extension. Off (every real signup today) → the unchanged extension flow.
      navigate(cloudCaptureUiEnabled() ? "/connect" : "/extension");
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
          A ballpark is fine — drag the slider to see your price. No payment
          today; your 14-day free trial begins right now. You&apos;ll add your
          real clients and arrays in the dashboard, and the extension
          auto-populates most of it when you log into your utility portal. We
          reconcile your subscription quantity automatically as the real count
          comes in.
        </p>

        {/* ── Live price hero ─────────────────────────────────────────── */}
        <div className="mt-8 rounded-2xl border border-primary-200 bg-gradient-to-b from-primary-50 to-white p-6">
          <div className="flex items-end justify-between gap-4">
            <div>
              <div className="text-xs font-medium uppercase tracking-wide text-primary-700">
                Your monthly price
              </div>
              <div className="mt-1 flex items-baseline gap-1">
                <span className="text-5xl font-semibold tracking-tight text-zinc-900 tabular-nums">
                  ${monthly}
                </span>
                <span className="text-lg text-zinc-500">/mo</span>
              </div>
              <div className="mt-1 text-sm text-zinc-500 tabular-nums">
                {dollars(blendedCents)}/array average
                <span className="text-zinc-400">
                  {" "}
                  · {safeCount} {safeCount === 1 ? "array" : "arrays"}
                </span>
              </div>
            </div>

            {/* Savings badge — only once a discount is actually earned. */}
            {showVolume && saved > 0 && (
              <div className="shrink-0 rounded-xl bg-primary-600 px-3 py-2 text-right text-white shadow-sm">
                <div className="text-[11px] font-medium uppercase tracking-wide text-primary-100">
                  You save
                </div>
                <div className="text-lg font-semibold tabular-nums">
                  {dollars(saved)}/mo
                </div>
                <div className="text-[11px] text-primary-100 tabular-nums">
                  {pctOff}% off
                </div>
              </div>
            )}
          </div>

          {/* Slider */}
          <div className="mt-6">
            <input
              type="range"
              min={1}
              max={SLIDER_MAX}
              step={1}
              value={sliderValue}
              onChange={(e) => setCount(parseInt(e.target.value, 10))}
              aria-label="Number of arrays"
              className="w-full accent-primary-600"
            />
            <div className="mt-1 flex justify-between text-[11px] text-zinc-400 tabular-nums">
              <span>1</span>
              <span>50</span>
              <span>100</span>
              <span>150</span>
              <span>{SLIDER_MAX}+</span>
            </div>
          </div>

          {/* Quick picks + precise entry */}
          <div className="mt-5 flex flex-wrap items-center gap-2">
            {QUICK_PICKS.map((n) => {
              const selected = count === n;
              return (
                <button
                  key={n}
                  type="button"
                  onClick={() => setCount(n)}
                  className={`rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 ${
                    selected
                      ? "border-primary-500 bg-primary-100 text-primary-800"
                      : "border-zinc-200 bg-white text-zinc-700 hover:border-zinc-300 hover:bg-zinc-50"
                  }`}
                >
                  {n}
                </button>
              );
            })}
            <div className="ml-auto flex items-center gap-2">
              <label htmlFor="array-count" className="sr-only">
                Exact number of arrays
              </label>
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
                className="w-24 rounded-lg border border-zinc-300 px-3 py-1.5 text-right text-sm font-semibold text-zinc-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500/30"
              />
              <span className="text-sm text-zinc-500">
                {count === 1 ? "array" : "arrays"}
              </span>
            </div>
          </div>
        </div>

        {/* ── Tier ladder — fills band-by-band as the count grows ─────── */}
        <div className="mt-5 rounded-2xl border border-zinc-200 bg-white p-5">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium uppercase tracking-wide text-zinc-500">
              How volume pricing works
            </span>
            <span className="text-[11px] text-zinc-400">
              Each band is priced on its own arrays
            </span>
          </div>

          <div className="mt-3 space-y-2">
            {bands.map((b) => {
              const pctFull =
                b.capacity === Infinity
                  ? b.filled > 0
                    ? 100
                    : 0
                  : Math.round((b.filled / b.capacity) * 100);
              return (
                <div
                  key={b.label}
                  className={`rounded-xl border px-3 py-2.5 transition-colors ${
                    b.active
                      ? "border-primary-200 bg-primary-50/60"
                      : "border-zinc-100 bg-zinc-50/60"
                  }`}
                >
                  <div className="flex items-center justify-between text-sm">
                    <div className="flex items-center gap-2">
                      <span
                        className={`font-medium tabular-nums ${
                          b.active ? "text-zinc-900" : "text-zinc-400"
                        }`}
                      >
                        Arrays {b.label}
                      </span>
                      {b.offPct > 0 && (
                        <span
                          className={`rounded-full px-1.5 py-0.5 text-[10px] font-semibold ${
                            b.active
                              ? "bg-primary-600 text-white"
                              : "bg-zinc-200 text-zinc-500"
                          }`}
                        >
                          {b.offPct}% off
                        </span>
                      )}
                    </div>
                    <div
                      className={`tabular-nums ${
                        b.active ? "text-zinc-700" : "text-zinc-400"
                      }`}
                    >
                      {dollars(b.unitCents)}/array
                      {b.filled > 0 && (
                        <span className="ml-2 font-semibold text-zinc-900">
                          {b.filled} × = {dollars(b.subtotalCents)}
                        </span>
                      )}
                    </div>
                  </div>
                  {/* Fill bar (skipped for the open-ended final band) */}
                  {b.capacity !== Infinity && (
                    <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-zinc-200">
                      <div
                        className="h-full rounded-full bg-primary-500 transition-all duration-200"
                        style={{ width: `${pctFull}%` }}
                      />
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          {!showVolume && (
            <p className="mt-3 text-xs text-zinc-500">
              You&apos;re billed a flat ${FULL_UNIT_CENTS / 100}/array up to 50.
              Past 50 arrays, volume discounts kick in automatically — drag the
              slider past 50 to see them.
            </p>
          )}
        </div>

        {/* ── Trial-end summary ──────────────────────────────────────── */}
        <div className="mt-5 rounded-2xl border border-zinc-200 bg-white p-5 text-sm">
          <div className="flex items-center justify-between text-zinc-700">
            <span>One-time setup (charged at trial end)</span>
            <span className="tabular-nums">${SETUP_FEE}</span>
          </div>
          <div className="mt-2 flex items-center justify-between text-zinc-700">
            <span>
              Monthly{showVolume ? " (volume-discounted)" : ""} ·{" "}
              {safeCount} {safeCount === 1 ? "array" : "arrays"}
            </span>
            <span className="tabular-nums">${monthly}/mo</span>
          </div>
          <div className="mt-3 flex items-center justify-between border-t border-zinc-200 pt-3 font-semibold text-zinc-900">
            <span>Due at trial end</span>
            <span className="tabular-nums">${SETUP_FEE + monthly}</span>
          </div>
          <p className="mt-3 text-xs text-zinc-500">
            Your 14-day free trial starts right now — no payment today. We&apos;ll
            email you a few days before it ends so you can add a card. If you
            have zero arrays at trial end, a one-array minimum ($
            {FULL_UNIT_CENTS / 100}) keeps your subscription active.
          </p>
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
          Not sure yet? Pick the closest estimate — your subscription quantity
          reconciles automatically once your real arrays are set up in the
          dashboard. No payment today, and you won&apos;t be charged later
          without notice. You&apos;ll add your card from the dashboard when
          you&apos;re ready; it&apos;s stored securely by Stripe — we never see
          it.
        </p>
      </Card>
    </ScreenLayout>
  );
}
