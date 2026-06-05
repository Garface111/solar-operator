import { useEffect, useState } from "react";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { RevealNumber } from "../ui/RevealNumber";
import { Spinner } from "../ui/Spinner";
import { EditableField } from "../ui/EditableField";
import { useToast } from "../ui/Toast";
import {
  type Account,
  type BillingSummary,
  type CaptureEntry,
  type NextInvoice,
  updateAccountEmail,
  getBillingPortalUrl,
  getBillingSummary,
  getRecentCaptures,
  getNextInvoice,
} from "../lib/api";

/** Next scheduled send date based on report_frequency.
 *  quarterly: Jan 1/Apr 1/Jul 1/Oct 1 at 09:00 UTC
 *  monthly:   1st of next month at 09:00 UTC
 *  weekly:    next Monday at 09:00 UTC */
function nextReportDate(freq: string | null): Date {
  const now = new Date();
  const utcNow = now.getTime();
  if (freq === "monthly") {
    const d = now.getUTCMonth() === 11
      ? new Date(Date.UTC(now.getUTCFullYear() + 1, 0, 1, 9, 0, 0))
      : new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1, 9, 0, 0));
    return d;
  }
  if (freq === "weekly") {
    const day = now.getUTCDay(); // 0=Sun, 1=Mon
    const daysUntilMon = day === 0 ? 1 : 8 - day;
    const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + daysUntilMon, 9, 0, 0));
    return d;
  }
  // default: quarterly
  const year = now.getUTCFullYear();
  const candidates = [0, 3, 6, 9].map((m) => new Date(Date.UTC(year, m, 1, 9, 0, 0)));
  candidates.push(new Date(Date.UTC(year + 1, 0, 1, 9, 0, 0)));
  return candidates.find((d) => d.getTime() > utcNow)!;
}

/** Human-readable "X ago", e.g. "2h ago", "3 days ago". */
function timeAgo(past: Date): string {
  const diffMs = Date.now() - past.getTime();
  if (diffMs < 60_000) return "just now";
  const mins = Math.floor(diffMs / 60_000);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(diffMs / 3_600_000);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(diffMs / 86_400_000);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

/** Human-readable relative time, e.g. "in 3h 12m" or "in 4 days". */
function relativeTime(target: Date, overdueLabel = "soon"): string {
  const diffMs = target.getTime() - Date.now();
  if (diffMs <= 0) return overdueLabel;
  const mins = Math.floor(diffMs / 60_000);
  if (mins < 60) return `in ${mins}m`;
  const hrs = Math.floor(diffMs / 3_600_000);
  if (hrs < 24) {
    const rem = Math.floor((diffMs % 3_600_000) / 60_000);
    return rem > 0 ? `in ${hrs}h ${rem}m` : `in ${hrs}h`;
  }
  const days = Math.ceil(diffMs / 86_400_000);
  return `in ${days} day${days === 1 ? "" : "s"}`;
}

function fmtMoney(cents: number, currency: string): string {
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: currency.toUpperCase(),
      minimumFractionDigits: cents % 100 === 0 ? 0 : 2,
    }).format(cents / 100);
  } catch {
    return `$${(cents / 100).toFixed(2)}`;
  }
}


const STATUS_STYLES: Record<string, string> = {
  active:   "bg-primary-100 text-primary-700",
  trialing: "bg-primary-50 text-primary-600 border border-primary-100",
  comped:   "bg-wood-100 text-wood-600 border border-wood-border",
  past_due: "bg-amber-100 text-amber-800",
  canceled: "bg-zinc-200 text-zinc-600",
  pending:  "bg-zinc-100 text-zinc-500",
};

/** Human-readable label for a subscription status value. DB values stay unchanged. */
function statusLabel(status: string): string {
  if (status === "comped") return "Complimentary";
  if (status === "past_due") return "Past due";
  return status.replace(/_/g, " ");
}

/** One-line tooltip explaining each badge, shown on hover. */
const STATUS_TOOLTIP: Record<string, string> = {
  active: "Subscription active — billing is current",
  trialing: "Trial period — no charge yet",
  comped: "Complimentary access — no charge",
  past_due: "Payment failed — update your card to keep access",
  canceled: "Subscription canceled",
  pending: "Subscription pending",
};

function StatusBadge({ account }: { account: Account }) {
  const status = account.subscription_status || (account.active ? "active" : "inactive");
  const cls = STATUS_STYLES[status] ?? "bg-zinc-100 text-zinc-600";
  const tooltip = STATUS_TOOLTIP[status];
  return (
    <span
      title={tooltip}
      className={`inline-flex cursor-default items-center rounded-full px-2.5 py-0.5 text-xs font-medium capitalize ${cls}`}
    >
      {statusLabel(status)}
    </span>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-4 py-2.5">
      <span className="text-sm text-zinc-500">{label}</span>
      <div className="text-right text-sm font-medium text-zinc-800">
        {children}
      </div>
    </div>
  );
}

interface Props {
  account: Account;
  onAccountChange: (patch: Partial<Account>) => void;
}

export function AccountSummaryCard({ account, onAccountChange }: Props) {
  const toast = useToast();
  const [openingPortal, setOpeningPortal] = useState(false);
  const [billing, setBilling] = useState<BillingSummary | null>(null);
  const [captures, setCaptures] = useState<CaptureEntry[] | null>(null);
  const [nextInvoice, setNextInvoice] = useState<NextInvoice | null>(null);

  useEffect(() => {
    let cancelled = false;
    function fetchBilling() {
      getBillingSummary()
        .then((b) => {
          if (!cancelled) setBilling(b);
        })
        .catch(() => {
          /* non-fatal — the billing strip just stays hidden */
        });
    }
    fetchBilling();
    window.addEventListener("so:arrays-changed", fetchBilling);

    getRecentCaptures(5)
      .then((c) => { if (!cancelled) setCaptures(c); })
      .catch(() => {});

    getNextInvoice()
      .then((n) => { if (!cancelled) setNextInvoice(n); })
      .catch(() => {});

    return () => {
      cancelled = true;
      window.removeEventListener("so:arrays-changed", fetchBilling);
    };
  }, []);

  async function saveEmail(next: string) {
    if (!next) throw new Error("Email can't be empty");
    const email = await updateAccountEmail(next);
    onAccountChange({ email });
    toast.success("Email updated");
  }

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

  return (
    <Card>
      <div className="flex items-start justify-between">
        <div>
          <h2 className="text-lg font-semibold tracking-tight text-zinc-900">
            {account.name || "Your account"}
          </h2>
          <p className="mt-0.5 text-sm text-zinc-500">
            Solar Operator
          </p>
        </div>
        <StatusBadge account={account} />
      </div>

      <div className="mt-4 divide-y divide-zinc-100 border-t border-zinc-100">
        <Field label="Email">
          <EditableField
            value={account.email}
            onSave={saveEmail}
            label="email"
            type="email"
            placeholder="you@example.com"
          />
        </Field>
        <Field label="Report cadence">
          <span className="capitalize">
            {account.report_frequency ?? "—"}
          </span>
        </Field>
        <Field label="Clients">
          <span title="Reporting clients you manage — each gets their own workbook.">
            <RevealNumber value={account.clients_count} delayMs={0} />
          </span>
        </Field>
        <Field label="Utility accounts">
          <span title="Utility account numbers detected by the extension. Each array typically has 1–3 accounts.">
            <RevealNumber value={account.accounts_count} delayMs={120} />
          </span>
        </Field>
        <Field label="Bills on file">
          <span title="Individual monthly bills pulled from those accounts. A full 6-quarter report needs ~18 bills per array.">
            <RevealNumber value={account.bills_count} delayMs={240} />
          </span>
        </Field>
        {account.extension_heartbeat_at && (
          <Field label="Last capture">
            <span>
              {new Date(account.extension_heartbeat_at).toLocaleDateString()}{" "}
              <span className="font-normal text-zinc-400">
                ({timeAgo(new Date(account.extension_heartbeat_at))})
              </span>
            </span>
          </Field>
        )}
        {account.last_delivery_at && (
          <Field label="Most recent delivery">
            <span>
              {new Date(account.last_delivery_at).toLocaleDateString()}{" "}
              <span className="font-normal text-zinc-400">
                ({timeAgo(new Date(account.last_delivery_at!))})
              </span>
            </span>
          </Field>
        )}
      </div>

      {billing && (
        <div className="mt-4 rounded-xl border border-cream-border bg-cream px-4 py-3">
          <div className="text-[11px] font-medium uppercase tracking-wide text-zinc-500">
            Billing
          </div>
          {billing.billable_arrays > 0 ? (
            <p className="mt-1 text-sm text-zinc-700">
              <span className="font-semibold text-zinc-900">
                {billing.billable_arrays}
              </span>{" "}
              billable {billing.billable_arrays === 1 ? "array" : "arrays"} ·{" "}
              {fmtMoney(billing.price_cents, billing.currency)} ×{" "}
              {billing.billable_arrays} ={" "}
              <span className="font-semibold text-zinc-900">
                {fmtMoney(billing.total_cents, billing.currency)}/mo
              </span>
            </p>
          ) : nextInvoice?.amount_cents && nextInvoice.amount_cents > 0 ? (
            // No arrays on file yet but Stripe is billing — operator used the
            // estimate path at checkout. Make this explicit so they're not surprised.
            <p className="mt-1 text-sm text-amber-800">
              Billing is currently based on your initial estimate — no arrays on
              file yet.{" "}
              <a href="/accounts/clients" className="font-medium underline underline-offset-2 hover:text-amber-900">
                Add your arrays
              </a>{" "}
              and your subscription will sync to the real count automatically.
            </p>
          ) : (
            <p className="mt-1 text-sm text-zinc-700">
              No billable arrays yet — you&apos;ll be billed{" "}
              {fmtMoney(billing.price_cents, billing.currency)}/array per month as
              arrays are added.
            </p>
          )}
          {/* Removed "Next charge: $X on <date>" — Stripe's upcoming_invoice
              endpoint was returning confusing values (zero amounts during the
              estimate-path window, stale period_ends after the first cycle).
              The headline above (billable arrays × price = monthly total) is
              the source of truth; the auto-sync note below covers the rest. */}
          <p className="mt-1.5 text-xs text-zinc-500">
            Arrays count toward your bill as soon as they&apos;re added — whether by manual entry or by utility capture. We sync with Stripe automatically; you&apos;ll see the change on your next statement.
          </p>
          <p className="mt-1 text-xs text-zinc-400">
            Updates automatically as you add or remove arrays.{" "}
            <a
              href="/accounts/clients"
              className="underline-offset-2 hover:text-zinc-600 hover:underline"
            >
              See your arrays →
            </a>
          </p>
        </div>
      )}

      {account.subscription_status === "past_due" && (
        <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3">
          <p className="text-sm font-medium text-amber-900">
            Payment failed — please update your card.
          </p>
          <p className="mt-0.5 text-xs text-amber-800">
            Reports will continue while we retry. Click &ldquo;Manage billing&rdquo; below to update your payment method.
          </p>
        </div>
      )}

      {/* Vein F: warn when extension is active but no bills have been captured */}
      {account.extension_heartbeat_at && account.bills_count === 0 &&
       account.accounts_count > 0 && (
        <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3">
          <p className="text-sm font-medium text-amber-900">
            Extension is active but no bills captured yet.
          </p>
          <p className="mt-0.5 text-xs text-amber-800">
            Make sure you&apos;re signed into your utility portal (GMP, VEC) with an account that has community
            solar. If your account isn&apos;t showing community solar billing,{" "}
            <a
              href="mailto:admin@solaroperator.org"
              className="underline underline-offset-2 hover:text-amber-700"
            >
              contact support
            </a>
            .
          </p>
        </div>
      )}

      {/* Recent captures activity feed — aggregated by array (deduplicated) */}
      {captures !== null && captures.length > 0 && (() => {
        // Deduplicate: keep only the most recent capture per unique array name.
        const seen = new Map<string, CaptureEntry>();
        for (const c of captures) {
          const key = `${c.client_name}|${c.array_name}`;
          if (!seen.has(key) || (c.pulled_at && (!seen.get(key)!.pulled_at || c.pulled_at > seen.get(key)!.pulled_at!))) {
            seen.set(key, c);
          }
        }
        const deduped = Array.from(seen.values());
        const sortedAts = deduped.map((c) => c.pulled_at).filter(Boolean).sort() as string[];
        const latestAt = sortedAts.length > 0 ? sortedAts[sortedAts.length - 1] : undefined;
        const arrayNames = deduped.map((c) => c.array_name).join(", ");
        return (
          <div className="mt-4 rounded-xl border border-cream-border bg-cream px-4 py-3">
            <div className="text-[11px] font-medium uppercase tracking-wide text-zinc-500">
              Recent activity
            </div>
            <p className="mt-1.5 text-xs text-zinc-700">
              {latestAt ? (
                <>
                  <span className="font-medium">{timeAgo(new Date(latestAt))}</span>
                  {" — collected data from "}
                  <span className="font-medium">{deduped.length} {deduped.length === 1 ? "array" : "arrays"}</span>
                  <span className="text-zinc-400"> ({arrayNames})</span>
                </>
              ) : (
                <>
                  Collected data from{" "}
                  <span className="font-medium">{deduped.length} {deduped.length === 1 ? "array" : "arrays"}</span>
                  <span className="text-zinc-400"> ({arrayNames})</span>
                </>
              )}
            </p>
          </div>
        );
      })()}

      {/* What happens next — forward-looking timeline so new operators know
          the system is running without them having to wonder. */}
      <div className="mt-4 rounded-xl border border-cream-border bg-cream px-4 py-3">
        <div className="text-[11px] font-medium uppercase tracking-wide text-zinc-500">
          What happens next
        </div>
        <ul className="mt-2 space-y-1.5 text-sm text-zinc-700">
          <li>
            <span className="font-medium">Next utility data pull:</span>{" "}
            {account.last_pull_at ? (
              <>
                {relativeTime(
                  new Date(
                    new Date(account.last_pull_at).getTime() + 6 * 60 * 60 * 1000,
                  ),
                  "soon",
                )}{" "}
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
            {(() => {
              const d = nextReportDate(account.report_frequency);
              return (
                <>
                  {d.toLocaleDateString(undefined, {
                    month: "long",
                    day: "numeric",
                    year: "numeric",
                  })}{" "}
                  <span className="text-xs text-zinc-400">
                    ({relativeTime(d)})
                  </span>
                </>
              );
            })()}
          </li>
        </ul>
      </div>

      <div className="mt-5 flex justify-end">
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
    </Card>
  );
}
