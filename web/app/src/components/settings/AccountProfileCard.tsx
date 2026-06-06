import { useState } from "react";
import { EditableField } from "../../ui/EditableField";
import { Input } from "../../ui/Input";
import { Button } from "../../ui/Button";
import { Spinner } from "../../ui/Spinner";
import { useToast } from "../../ui/Toast";
import { type Account, updateAccountEmail, setPassword } from "../../lib/api";
import { timeAgo } from "./utils";

const STATUS_STYLES: Record<string, string> = {
  active:   "bg-primary-100 text-primary-700",
  trialing: "bg-primary-50 text-primary-600 border border-primary-100",
  comped:   "bg-wood-100 text-wood-600 border border-wood-border",
  past_due: "bg-amber-100 text-amber-800",
  canceled: "bg-zinc-200 text-zinc-600",
  pending:  "bg-zinc-100 text-zinc-500",
};

const STATUS_TOOLTIP: Record<string, string> = {
  active: "Subscription active — billing is current",
  trialing: "Trial period — no charge yet",
  comped: "Complimentary access — no charge",
  past_due: "Payment failed — update your card to keep access",
  canceled: "Subscription canceled",
  pending: "Subscription pending",
};

function statusLabel(status: string): string {
  if (status === "comped") return "Complimentary";
  if (status === "past_due") return "Past due";
  return status.replace(/_/g, " ");
}

function StatusBadge({ account }: { account: Account }) {
  const status = account.subscription_status || (account.active ? "active" : "inactive");
  const cls = STATUS_STYLES[status] ?? "bg-zinc-100 text-zinc-600";
  return (
    <span
      title={STATUS_TOOLTIP[status]}
      className={`inline-flex cursor-default items-center rounded-full px-2.5 py-0.5 text-xs font-medium capitalize ${cls}`}
    >
      {statusLabel(status)}
    </span>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 py-2.5">
      <span className="text-sm text-zinc-500">{label}</span>
      <div className="text-right text-sm font-medium text-zinc-800">{children}</div>
    </div>
  );
}

function SecuritySection({
  hasPassword,
  onPasswordSet,
}: {
  hasPassword: boolean;
  onPasswordSet: () => void;
}) {
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [currentPw, setCurrentPw] = useState("");
  const [newPw, setNewPw] = useState("");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!newPw || saving) return;
    setSaving(true);
    try {
      await setPassword(newPw, hasPassword ? currentPw : undefined);
      toast.success(hasPassword ? "Password updated." : "Password set.");
      setOpen(false);
      setCurrentPw("");
      setNewPw("");
      onPasswordSet();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't save password.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      {!open ? (
        <div className="flex items-center justify-between gap-4 py-1">
          <span className="text-sm text-zinc-500">
            {hasPassword
              ? "Password is set."
              : "Your account uses email sign-in links."}
          </span>
          <button
            type="button"
            onClick={() => setOpen(true)}
            className="shrink-0 rounded text-sm font-medium text-primary-600 hover:text-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
          >
            {hasPassword ? "Change password" : "Set password"}
          </button>
        </div>
      ) : (
        <form onSubmit={handleSubmit} className="flex flex-col gap-3">
          {hasPassword && (
            <Input
              id="sec-current-pw"
              label="Current password"
              type="password"
              autoComplete="current-password"
              value={currentPw}
              onChange={(e) => setCurrentPw(e.target.value)}
              autoFocus
            />
          )}
          <Input
            id="sec-new-pw"
            label="New password"
            type="password"
            autoComplete="new-password"
            value={newPw}
            onChange={(e) => setNewPw(e.target.value)}
            autoFocus={!hasPassword}
          />
          <p className="text-xs text-zinc-400">
            Min 10 characters, include a letter and a digit.
          </p>
          <div className="flex gap-2">
            <Button
              type="submit"
              disabled={saving || !newPw || (hasPassword && !currentPw)}
              className="flex-1"
            >
              {saving ? <Spinner className="h-4 w-4" /> : "Save"}
            </Button>
            <Button
              type="button"
              variant="secondary"
              onClick={() => {
                setOpen(false);
                setCurrentPw("");
                setNewPw("");
              }}
            >
              Cancel
            </Button>
          </div>
        </form>
      )}
    </>
  );
}

interface Props {
  account: Account;
  onAccountChange: (patch: Partial<Account>) => void;
}

export function AccountProfileCard({ account, onAccountChange }: Props) {
  const toast = useToast();

  async function saveEmail(next: string) {
    if (!next) throw new Error("Email can't be empty");
    const email = await updateAccountEmail(next);
    onAccountChange({ email });
    toast.success("Email updated");
  }

  return (
    <div>
      <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-zinc-400">
        Operator profile
      </h2>

      <div className="rounded-2xl border border-cream-border bg-cream shadow-sm">
        {/* Header: account name + status badge */}
        <div className="flex items-start justify-between gap-3 px-5 py-4">
          <div>
            <p className="text-sm font-medium text-zinc-800">
              {account.name || "Your account"}
            </p>
            <p className="mt-0.5 text-xs text-zinc-400">Solar Operator</p>
          </div>
          <StatusBadge account={account} />
        </div>

        {/* Data rows */}
        <div className="border-t border-cream-border px-5 py-1">
          <div className="divide-y divide-zinc-100">
            <Row label="Name">
              <span className="text-zinc-700">
                {account.name || <span className="text-zinc-400">—</span>}
              </span>
            </Row>
            <Row label="Email">
              <EditableField
                value={account.email}
                onSave={saveEmail}
                label="email"
                type="email"
                placeholder="you@example.com"
              />
            </Row>
            <Row label="Clients">
              <span title="Reporting clients you manage — each gets their own workbook.">
                {account.clients_count}
              </span>
            </Row>
            <Row label="Utility accounts">
              <span title="Utility account numbers detected by the extension.">
                {account.accounts_count}
              </span>
            </Row>
            <Row label="Bills on file">
              <span title="Individual monthly bills pulled from utility accounts.">
                {account.bills_count}
              </span>
            </Row>
            {account.last_delivery_at && (
              <Row label="Last delivery">
                <span>
                  {new Date(account.last_delivery_at).toLocaleDateString()}{" "}
                  <span className="font-normal text-zinc-400">
                    ({timeAgo(new Date(account.last_delivery_at))})
                  </span>
                </span>
              </Row>
            )}
          </div>
        </div>

        {/* Security section */}
        <div className="border-t border-cream-border px-5 py-4">
          <p className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
            Sign-in &amp; security
          </p>
          <SecuritySection
            hasPassword={account.has_password}
            onPasswordSet={() => onAccountChange({ has_password: true })}
          />
        </div>
      </div>
    </div>
  );
}
