import { useEffect, useState } from "react";
import { Button } from "../../ui/Button";
import { useToast } from "../../ui/Toast";
import {
  type BillingSubscription,
  type FlatArray,
  createManualSubscription,
  listAllArrays,
  listBillingSubscriptions,
} from "../../lib/api";

// ─── Add-customer card ───────────────────────────────────────────────────────
// The MANUAL customer-input path (no xlsx required). Paul types a customer in
// — name, which array, their allocation %, email, cadence, delivery + send mode
// — and the backend creates a workbook-less subscription whose invoices are
// computed as allocation_pct × the array's period generation.

type Cadence = "monthly" | "quarterly";
type Delivery = "approval" | "auto";
type Send = "to_me" | "to_client" | "to_both";

const FIELD =
  "h-9 w-full rounded-lg border border-cream-border bg-white px-3 text-sm text-zinc-700 placeholder:text-zinc-400 focus:border-zinc-300 focus:outline-none focus:ring-1 focus:ring-zinc-200";
const LABEL = "mb-1 block text-xs font-medium text-zinc-500";

export function AddCustomerCard({ onCreated }: { onCreated?: () => void }) {
  const toast = useToast();

  const [open, setOpen] = useState(false);
  const [arrays, setArrays] = useState<FlatArray[] | null>(null);
  const [subs, setSubs] = useState<BillingSubscription[]>([]);
  const [saving, setSaving] = useState(false);

  // Form state
  const [name, setName] = useState("");
  const [arrayId, setArrayId] = useState<string>("");
  const [pct, setPct] = useState("");
  const [email, setEmail] = useState("");
  const [cadence, setCadence] = useState<Cadence>("monthly");
  const [delivery, setDelivery] = useState<Delivery>("approval");
  const [send, setSend] = useState<Send>("to_me");

  function loadSubs() {
    listBillingSubscriptions()
      .then(setSubs)
      .catch(() => {
        /* non-fatal — the list is informational */
      });
  }

  useEffect(() => {
    loadSubs();
  }, []);

  useEffect(() => {
    if (open && arrays === null) {
      listAllArrays()
        .then(setArrays)
        .catch(() => setArrays([]));
    }
  }, [open, arrays]);

  function resetForm() {
    setName("");
    setArrayId("");
    setPct("");
    setEmail("");
    setCadence("monthly");
    setDelivery("approval");
    setSend("to_me");
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const trimmedName = name.trim();
    if (!trimmedName) {
      toast.error("Enter a customer name");
      return;
    }
    if (!arrayId) {
      toast.error("Pick an array");
      return;
    }
    const pctNum = Number(pct);
    if (!Number.isFinite(pctNum) || pctNum <= 0 || pctNum > 100) {
      toast.error("Allocation must be a number between 0 and 100");
      return;
    }
    if ((send === "to_client" || send === "to_both") && !email.trim()) {
      toast.error("A customer email is required to send to the client");
      return;
    }

    setSaving(true);
    try {
      await createManualSubscription({
        customer_name: trimmedName,
        array_id: Number(arrayId),
        allocation_pct: pctNum / 100, // percent → fraction (0..1)
        client_email: email.trim() || null,
        cadence,
        delivery_mode: delivery,
        send_mode: send,
      });
      toast.success(`Added ${trimmedName}`);
      resetForm();
      setOpen(false);
      loadSubs();
      onCreated?.();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't add customer");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="rounded-2xl border border-cream-border bg-cream shadow-sm">
      {/* Header */}
      <div className="flex items-center justify-between gap-3 px-5 py-4">
        <div>
          <p className="text-sm font-semibold text-zinc-800">Billing customers</p>
          <p className="mt-0.5 text-xs text-zinc-400">
            Add a customer to invoice for their share of an array — no
            spreadsheet required.
          </p>
        </div>
        <Button
          variant={open ? "secondary" : "primary"}
          onClick={() => setOpen((v) => !v)}
        >
          {open ? "Cancel" : "Add customer"}
        </Button>
      </div>

      {/* Existing customers (informational) */}
      {subs.length > 0 && (
        <div className="border-t border-cream-border px-5 py-3">
          <ul className="space-y-1.5">
            {subs.map((s) => (
              <li
                key={s.id}
                className="flex items-center justify-between gap-3 text-xs text-zinc-600"
                data-testid="billing-sub-row"
              >
                <span className="truncate font-medium text-zinc-700">
                  {s.customer_name}
                </span>
                <span className="shrink-0 text-zinc-400">
                  {s.allocation_pct != null
                    ? `${(s.allocation_pct * 100).toFixed(0)}%`
                    : s.source_filename
                      ? "workbook"
                      : "—"}{" "}
                  · {s.cadence} ·{" "}
                  {s.delivery_mode === "auto" ? "auto-send" : "approval"}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Add form */}
      {open && (
        <form
          onSubmit={submit}
          className="space-y-4 border-t border-cream-border px-5 py-4"
          data-testid="add-customer-form"
        >
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <label className={LABEL} htmlFor="ac-name">
                Customer name
              </label>
              <input
                id="ac-name"
                className={FIELD}
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Paul Bozuwa"
                autoComplete="off"
              />
            </div>

            <div>
              <label className={LABEL} htmlFor="ac-array">
                Array
              </label>
              <select
                id="ac-array"
                className={FIELD}
                value={arrayId}
                onChange={(e) => setArrayId(e.target.value)}
              >
                <option value="">
                  {arrays === null ? "Loading arrays…" : "Select an array…"}
                </option>
                {(arrays ?? []).map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name} ({a.client_name})
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className={LABEL} htmlFor="ac-pct">
                Allocation %
              </label>
              <input
                id="ac-pct"
                className={FIELD}
                type="number"
                min={0}
                max={100}
                step="any"
                value={pct}
                onChange={(e) => setPct(e.target.value)}
                placeholder="25"
              />
            </div>

            <div>
              <label className={LABEL} htmlFor="ac-email">
                Customer email
              </label>
              <input
                id="ac-email"
                className={FIELD}
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="paul@example.com"
                autoComplete="off"
              />
            </div>

            <div>
              <label className={LABEL} htmlFor="ac-cadence">
                Cadence
              </label>
              <select
                id="ac-cadence"
                className={FIELD}
                value={cadence}
                onChange={(e) => setCadence(e.target.value as Cadence)}
              >
                <option value="monthly">Monthly</option>
                <option value="quarterly">Quarterly</option>
              </select>
            </div>

            <div>
              <label className={LABEL} htmlFor="ac-delivery">
                Delivery mode
              </label>
              <select
                id="ac-delivery"
                className={FIELD}
                value={delivery}
                onChange={(e) => setDelivery(e.target.value as Delivery)}
              >
                <option value="approval">
                  Approval inbox (review before send)
                </option>
                <option value="auto">Auto-send each period</option>
              </select>
            </div>

            <div>
              <label className={LABEL} htmlFor="ac-send">
                Send to
              </label>
              <select
                id="ac-send"
                className={FIELD}
                value={send}
                onChange={(e) => setSend(e.target.value as Send)}
              >
                <option value="to_me">Me (operator)</option>
                <option value="to_client">The customer</option>
                <option value="to_both">Both</option>
              </select>
            </div>
          </div>

          <div className="flex items-center justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setOpen(false)}
              disabled={saving}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={saving}>
              {saving ? "Adding…" : "Add customer"}
            </Button>
          </div>
        </form>
      )}
    </div>
  );
}
