import { useEffect, useState } from "react";
import { Button } from "../../ui/Button";
import { Spinner } from "../../ui/Spinner";
import { useToast } from "../../ui/Toast";
import {
  type Account,
  type BillingSummary,
  type NextInvoice,
  getBillingSummary,
  getNextInvoice,
  getBillingPortalUrl,
  addPaymentMethod,
} from "../../lib/api";
import { relativeTime, nextReportDate, fmtMoney } from "./utils";

interface Props {
  account: Account;
}

export function PlanBillingCard({ account }: Props) {
  const toast = useToast();
  const [billing, setBilling] = useState<BillingSummary | null>(null);
  const [nextInvoice, setNextInvoice] = useState<NextInvoice | null>(null);
  const [openingPortal, setOpeningPortal] = useState(false);
  const [addingCard, setAddingCard] = useState(false);

  useEffect(() => {
    let cancelled = false;

    function fetchBilling() {
      getBillingSummary()
        .then((b) => { if (!cancelled) setBilling(b); })
        .catch(() => {});
    }

    fetchBilling();
    window.addEventListener("so:arrays-changed", fetchBilling);

    getNextInvoice()
      .then((n) => { if (!cancelled) setNextInvoice(n); })
      .catch(() => {});

    return () => {
      cancelled = true;
      window.removeEventListener("so:arrays-changed", fetchBilling);
    };
  }, []);

  async function openBillingPortal() {
    setOpeningPortal(true);
    try {
      const url = await getBillingPortalUrl();
      window.location.href = url;
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Couldn't open the billing portal",
      );
      setOpeningPortal(false);
    }
  }

  async function startAddCard() {
    setAddingCard(true);
    try {
      await addPaymentMethod(); // redirects to Stripe Checkout (setup mode)
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Couldn't open the add-card page",
      );
      setAddingCard(false);
    }
  }

  // No-upfront-payment: a live trial can have no card yet, and a trial that
  // ended card-less is paused (read-only). billing.has_payment_method is the
  // source of truth; fall back to false while billing is still loading.
  const hasCard = billing?.has_payment_method ?? false;
  const isPaused = account.subscription_status === "paused_no_card";

  const nextReport = nextReportDate(account.report_frequency);
  // Pulls run every 6h, so the next one is last_pull_at + 6h. Defensive clamp:
  // if a malformed/future-skewed last_pull_at would push the countdown beyond
  // the 6h cadence (the timezone bug that once showed "in 11h, every 6 hours"),
  // cap it at now + 6h so the display can never contradict its own cadence.
  const PULL_INTERVAL_MS = 6 * 60 * 60 * 1000;
  const nextPullAt = account.last_pull_at
    ? new Date(
        Math.min(
          new Date(account.last_pull_at).getTime() + PULL_INTERVAL_MS,
          Date.now() + PULL_INTERVAL_MS,
        ),
      )
    : null;

  return (
    <div>
      <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-zinc-400">
        Plan &amp; billing
      </h2>

      <div className="rounded-2xl border border-cream-border bg-cream shadow-sm">
        {/* Header */}
        <div className="px-5 py-4">
          <p className="text-sm font-medium text-zinc-800">
            {account.plan ? (
              <span className="capitalize">{account.plan} plan</span>
            ) : (
              "Subscription &amp; upcoming reports"
            )}
          </p>
          {account.subscription_status === "past_due" && (
            <div className="mt-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3">
              <p className="text-sm font-medium text-amber-900">
                Payment failed — please update your card.
              </p>
              <p className="mt-0.5 text-xs text-amber-800">
                Reports will continue while we retry. Click &quot;Manage billing&quot; below
                to update your payment method.
              </p>
            </div>
          )}

          {/* Paused (trial ended, no card) — amber banner with a primary CTA. */}
          {isPaused && (
            <div className="mt-3 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3">
              <p className="text-sm font-semibold text-amber-900">
                Trial ended. Add a card to resume reports.
              </p>
              <p className="mt-0.5 text-xs text-amber-800">
                Your account is read-only until a card is on file. We&apos;ve held all
                your data — nothing is deleted.
              </p>
              <button
                type="button"
                onClick={startAddCard}
                disabled={addingCard}
                className="mt-3 inline-flex items-center gap-2 rounded-xl bg-amber-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-amber-700 disabled:opacity-60"
              >
                {addingCard ? (
                  <>
                    <Spinner />
                    Opening…
                  </>
                ) : (
                  "Add card →"
                )}
              </button>
            </div>
          )}

          {/* Live (trialing/active) but no card yet — gentle add-payment CTA. */}
          {!isPaused && billing && !hasCard && (
            <div className="mt-3 rounded-xl border border-primary-200 bg-primary-50 px-4 py-3">
              <p className="text-sm font-medium text-primary-900">
                No payment method on file yet.
              </p>
              <p className="mt-0.5 text-xs text-primary-800">
                Add a card so your reports keep flowing when your trial ends.
              </p>
              <button
                type="button"
                onClick={startAddCard}
                disabled={addingCard}
                className="mt-3 inline-flex items-center gap-2 rounded-xl bg-primary-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-primary-700 disabled:opacity-60"
              >
                {addingCard ? (
                  <>
                    <Spinner />
                    Opening…
                  </>
                ) : (
                  "Add payment method →"
                )}
              </button>
            </div>
          )}
        </div>

        {/* Billing summary */}
        {billing && (
          <div className="border-t border-cream-border px-5 py-4">
            <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
              Billing
            </p>
            {billing.billable_arrays > 0 ? (
              billing.full_unit_cents && billing.price_cents < billing.full_unit_cents ? (
                <p className="text-sm text-zinc-700">
                  <span className="font-semibold text-zinc-900">
                    {billing.billable_arrays}
                  </span>{" "}
                  billable{" "}
                  {billing.billable_arrays === 1 ? "array" : "arrays"} ·{" "}
                  volume-discounted to{" "}
                  {fmtMoney(billing.price_cents, billing.currency)}/array ={" "}
                  <span className="font-semibold text-zinc-900">
                    {fmtMoney(billing.total_cents, billing.currency)}/mo
                  </span>
                </p>
              ) : (
                <p className="text-sm text-zinc-700">
                  <span className="font-semibold text-zinc-900">
                    {billing.billable_arrays}
                  </span>{" "}
                  billable{" "}
                  {billing.billable_arrays === 1 ? "array" : "arrays"} ·{" "}
                  {fmtMoney(billing.price_cents, billing.currency)} ×{" "}
                  {billing.billable_arrays} ={" "}
                  <span className="font-semibold text-zinc-900">
                    {fmtMoney(billing.total_cents, billing.currency)}/mo
                  </span>
                </p>
              )
            ) : nextInvoice?.amount_cents && nextInvoice.amount_cents > 0 ? (
              <p className="text-sm text-amber-800">
                Billing is currently based on your initial estimate — no arrays on
                file yet.{" "}
                <a
                  href="/accounts/clients"
                  className="font-medium underline underline-offset-2 hover:text-amber-900"
                >
                  Add your arrays
                </a>{" "}
                and your subscription will sync to the real count automatically.
              </p>
            ) : (
              <p className="text-sm text-zinc-700">
                No billable arrays yet — you&apos;ll be billed{" "}
                {fmtMoney(billing.price_cents, billing.currency)}/array per month as
                arrays are added.
              </p>
            )}
            <p className="mt-1.5 text-xs text-zinc-500">
              Arrays count toward your bill as soon as they&apos;re added. We sync with
              Stripe automatically; you&apos;ll see the change on your next statement.
            </p>
          </div>
        )}

        {/* What happens next */}
        <div className="border-t border-cream-border px-5 py-4">
          <p className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
            What happens next
          </p>
          <ul className="space-y-1.5 text-sm text-zinc-700">
            <li>
              <span className="font-medium">Next utility data pull:</span>{" "}
              {nextPullAt ? (
                <>
                  {relativeTime(nextPullAt, "soon")}{" "}
                  <span className="text-xs text-zinc-400">(every 6 hours)</span>
                </>
              ) : (
                <span className="text-zinc-400">
                  soon — the extension will begin pulling automatically
                </span>
              )}
            </li>
            <li>
              <span className="font-medium">
                Next {account.report_frequency ?? "quarterly"} report:
              </span>{" "}
              {nextReport.toLocaleDateString(undefined, {
                month: "long",
                day: "numeric",
                year: "numeric",
              })}{" "}
              <span className="text-xs text-zinc-400">
                ({relativeTime(nextReport)})
              </span>
            </li>
          </ul>
        </div>

        {/* Billing portal CTA — only meaningful once a card is on file. The
            Stripe billing portal requires a customer with a payment method, so
            we hide it until then and surface the add-card CTA above instead. */}
        {hasCard && (
          <div className="border-t border-cream-border px-5 py-4">
            <Button
              variant="secondary"
              onClick={openBillingPortal}
              disabled={openingPortal}
            >
              {openingPortal ? (
                <>
                  <Spinner />
                  Opening…
                </>
              ) : (
                "Manage billing →"
              )}
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
