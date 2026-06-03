import { useState } from "react";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { EditableField } from "../ui/EditableField";
import { useToast } from "../ui/Toast";
import {
  type Account,
  updateAccountEmail,
  updateAccountFrequency,
  getBillingPortalUrl,
} from "../lib/api";

const FREQUENCIES = ["weekly", "monthly", "quarterly"] as const;

const STATUS_STYLES: Record<string, string> = {
  active: "bg-primary-100 text-primary-700",
  trialing: "bg-blue-100 text-blue-700",
  comped: "bg-violet-100 text-violet-700",
  past_due: "bg-amber-100 text-amber-800",
  canceled: "bg-zinc-200 text-zinc-600",
  pending: "bg-zinc-100 text-zinc-500",
};

function StatusBadge({ account }: { account: Account }) {
  const status = account.subscription_status || (account.active ? "active" : "inactive");
  const cls = STATUS_STYLES[status] ?? "bg-zinc-100 text-zinc-600";
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium capitalize ${cls}`}
    >
      {status.replace("_", " ")}
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

  async function saveEmail(next: string) {
    if (!next) throw new Error("Email can't be empty");
    const email = await updateAccountEmail(next);
    onAccountChange({ email });
    toast.success("Email updated");
  }

  async function saveFrequency(next: string) {
    const frequency = await updateAccountFrequency(next);
    onAccountChange({ report_frequency: frequency });
    toast.success("Report cadence updated");
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
            {account.plan ? `${account.plan} plan` : "Solar Operator"}
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
          <select
            value={account.report_frequency ?? ""}
            onChange={(e) => saveFrequency(e.target.value)}
            className="rounded-lg border border-zinc-300 bg-white px-2 py-1 text-sm capitalize focus:outline-none focus:ring-2 focus:ring-primary-500/40"
          >
            {!account.report_frequency && <option value="">—</option>}
            {FREQUENCIES.map((f) => (
              <option key={f} value={f} className="capitalize">
                {f}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Utility accounts">{account.accounts_count}</Field>
        <Field label="Bills on file">{account.bills_count}</Field>
        {account.last_delivery_at && (
          <Field label="Last report sent">
            {new Date(account.last_delivery_at).toLocaleDateString()}
          </Field>
        )}
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
