import { useEffect, useState } from "react";
import { Card } from "../../ui/Card";
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

  const nextReport = nextReportDate(account.report_frequency);
  const nextPullAt = account.last_pull_at
    ? new Date(new Date(account.last_pull_at).getTime() + 6 * 60 * 60 * 1000)
    : null;

  return (
    <Card>
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold tracking-tight text-zinc-900">Plan &amp; billing</h2>
          <p className="mt-0.5 text-sm text-zinc-500">
            {account.plan ? (
              <span className="capitalize">{account.plan} plan</span>
            ) : (
              "Your subscription and upcoming reports."
            )}
          </p>
        </div>
      </div>

      {account.subscription_status === "past_due" && (
        <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3">
          <p className="text-sm font-medium text-amber-900">
            Payment failed — please update your card.
          </p>
          <p className="mt-0.5 text-xs text-amber-800">
            Reports will continue while we retry. Click &quot;Manage billing&quot; below to update
            your payment method.
          </p>
        </div>
      )}

      {billing && (
        <div className="mt-4 rounded-xl border border-cream-border bg-cream px-4 py-3">
          <div className="text-[11px] font-medium uppercase tracking-wide text-zinc-500">
            Billing
          </div>
          {billing.billable_arrays > 0 ? (
            <p className="mt-1 text-sm text-zinc-700">
              <span className="font-semibold text-zinc-900">{billing.billable_arrays}</span>{" "}
              billable {billing.billable_arrays === 1 ? "array" : "arrays"} ·{" "}
              {fmtMoney(billing.price_cents, billing.currency)} ×{" "}
              {billing.billable_arrays} ={" "}
              <span className="font-semibold text-zinc-900">
                {fmtMoney(billing.total_cents, billing.currency)}/mo
              </span>
            </p>
          ) : nextInvoice?.amount_cents && nextInvoice.amount_cents > 0 ? (
            <p className="mt-1 text-sm text-amber-800">
              Billing is currently based on your initial estimate — no arrays on file yet.{" "}
              <a
                href="/accounts/clients"
                className="font-medium underline underline-offset-2 hover:text-amber-900"
              >
                Add your arrays
              </a>{" "}
              and your subscription will sync to the real count automatically.
            </p>
          ) : (
            <p className="mt-1 text-sm text-zinc-700">
              No billable arrays yet — you&apos;ll be billed{" "}
              {fmtMoney(billing.price_cents, billing.currency)}/array per month as arrays are
              added.
            </p>
          )}
          <p className="mt-1.5 text-xs text-zinc-500">
            Arrays count toward your bill as soon as they&apos;re added. We sync with Stripe
            automatically; you&apos;ll see the change on your next statement.
          </p>
        </div>
      )}

      <div className="mt-4 rounded-xl border border-cream-border bg-cream px-4 py-3">
        <div className="text-[11px] font-medium uppercase tracking-wide text-zinc-500">
          What happens next
        </div>
        <ul className="mt-2 space-y-1.5 text-sm text-zinc-700">
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
            <span className="text-xs text-zinc-400">({relativeTime(nextReport)})</span>
          </li>
        </ul>
      </div>

      <div className="mt-5 flex justify-end">
        <Button variant="secondary" onClick={openBillingPortal} disabled={openingPortal}>
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
    </Card>
  );
}
