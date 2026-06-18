import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { ScreenLayout } from "../ui/ScreenLayout";
import { useToast } from "../ui/Toast";
import { useDashboardContext } from "./DashboardLayout";
import {
  type BillingSubscription,
  type FlatArray,
  type ReportDraft,
  type SubscriptionPreview,
  approveDraft,
  attachGmpInvoice,
  createManualSubscription,
  downloadInvoiceWorkbook,
  generateDraft,
  getSubscriptionPreview,
  listAllArrays,
  listBillingSubscriptions,
  listDrafts,
  patchDraft,
  patchSubscription,
} from "../lib/api";

// ═══════════════════════════════════════════════════════════════════════════
// Array Operator — Reports · "The Billing Run"
// Redesign of the Reports tab around the per-period customer-billing run (see
// sketches/reports-redesign/004-hybrid). One hero card for the current run, a
// per-customer run table with inline allocation math, an inline "Add a
// customer" row, a "Manage customers" mode, a right-hand Review drawer, and a
// durable History section. Nothing ever auto-sends — every path ends at a
// human "Approve & send".
// ═══════════════════════════════════════════════════════════════════════════

// Blended net-metering credit rate used only for DISPLAY math when a draft
// hasn't been computed yet; once a draft exists the effective rate is derived
// from the authoritative amount ÷ kWh the backend returns.
const RATE_FALLBACK = 0.1485;

// ─── number formatting ──────────────────────────────────────────────────────
function fmtMoney(n: number): string {
  return n.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}
function fmtKwh(n: number): string {
  return n.toLocaleString("en-US", { maximumFractionDigits: 0 });
}

type ChipStatus = "draft" | "needs" | "ready" | "sent";

const CHIP_LABEL: Record<ChipStatus, string> = {
  draft: "Draft",
  needs: "Needs GMP PDF",
  ready: "Ready",
  sent: "Sent",
};

const CHIP_CLASS: Record<ChipStatus, string> = {
  draft: "bg-blue-50 text-blue-700",
  needs: "bg-amber-50 text-amber-700",
  ready: "bg-primary-50 text-primary-800",
  sent: "bg-zinc-100 text-zinc-600",
};

const CHIP_DOT: Record<ChipStatus, string> = {
  draft: "bg-blue-600",
  needs: "bg-amber-500",
  ready: "bg-primary-700",
  sent: "bg-emerald-500",
};

// ─── derived helpers ────────────────────────────────────────────────────────

/** The status chip for a customer row, derived from its (optional) draft. */
function chipFor(sub: BillingSubscription, draft: ReportDraft | undefined): ChipStatus {
  if (draft?.status === "sent") return "sent";
  if (draft?.status === "pending") return draft.has_gmp_pdf ? "ready" : "needs";
  if (sub.last_sent_at) return "sent";
  return "draft";
}

/** Effective $/kWh rate for a row — authoritative from the draft when present. */
function rateFor(draft: ReportDraft | undefined): number {
  if (draft && draft.customer_kwh && draft.amount_usd != null && draft.customer_kwh > 0) {
    return draft.amount_usd / draft.customer_kwh;
  }
  return RATE_FALLBACK;
}

function pctOf(sub: BillingSubscription): number {
  return sub.allocation_pct != null ? sub.allocation_pct * 100 : 0;
}

function currentPeriodLabel(): string {
  return new Date().toLocaleString("en-US", { month: "long", year: "numeric" });
}

/** Format an ISO date (YYYY-MM-DD) as a clean "Month YYYY". Parsed in UTC so a
 *  period-end like 2026-05-19 doesn't slip into April in negative time zones. */
function monthYearFromISO(iso: string): string | null {
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return null;
  return dt.toLocaleDateString("en-US", {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  });
}

/** True when a period label is already human-friendly (e.g. "May 2026") rather
 *  than a raw ISO range like "2026-04-17 → 2026-05-19". */
function isHumanPeriodLabel(label: string): boolean {
  return /[A-Za-z]{3,}/.test(label) && !label.includes("→");
}

/** Normalize a draft's stored period_label to a clean "Month YYYY". Raw ISO
 *  ranges are collapsed to the month of their END date; already-friendly labels
 *  pass through untouched. */
function cleanPeriodLabel(raw: string | null | undefined): string | null {
  if (!raw) return null;
  if (isHumanPeriodLabel(raw)) return raw;
  const parts = raw.split("→").map((s) => s.trim());
  const end = parts[parts.length - 1];
  return monthYearFromISO(end) ?? (isHumanPeriodLabel(raw) ? raw : null);
}

/** Does this subscription's allocation % live in the column, or in a parsed
 *  workbook map? Workbook-sourced rows carry the % inside parsed_map, so the
 *  column reads null/0 — we must NOT render a "gen × 0% =" contradiction. */
function isWorkbookSub(
  sub: BillingSubscription,
  preview: SubscriptionPreview | undefined,
): boolean {
  if (preview && (preview.source === "schema" || preview.source === "llm")) {
    return true;
  }
  return !!sub.source_filename;
}

// ═══════════════════════════════════════════════════════════════════════════
// Editable allocation-% pill
// ═══════════════════════════════════════════════════════════════════════════
function PctPill({
  sub,
  editable,
  onCommit,
}: {
  sub: BillingSubscription;
  editable: boolean;
  onCommit: (pct: number) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(String(Math.round(pctOf(sub))));
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    setValue(String(Math.round(pctOf(sub))));
  }, [sub]);

  useEffect(() => {
    if (editing) inputRef.current?.select();
  }, [editing]);

  function commit() {
    const v = Math.max(1, Math.min(100, parseInt(value, 10) || Math.round(pctOf(sub))));
    setEditing(false);
    if (v !== Math.round(pctOf(sub))) onCommit(v);
    else setValue(String(v));
  }

  if (!editable) {
    return (
      <span className="font-semibold text-zinc-700 tabular-nums">
        {Math.round(pctOf(sub))}%
      </span>
    );
  }

  if (editing) {
    return (
      <span className="inline-flex items-center gap-0.5 rounded-md border border-primary-300 bg-white px-1 shadow-[0_0_0_3px_rgba(52,211,153,0.18)]">
        <input
          ref={inputRef}
          type="number"
          min={1}
          max={100}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
            if (e.key === "Escape") {
              setValue(String(Math.round(pctOf(sub))));
              setEditing(false);
            }
          }}
          className="w-9 border-none p-0 text-right font-semibold tabular-nums outline-none"
        />
        <span className="font-semibold">%</span>
      </span>
    );
  }

  return (
    <button
      type="button"
      onClick={() => setEditing(true)}
      title="Click to edit allocation"
      className="inline-flex items-center gap-0.5 rounded-md border border-transparent px-1 font-semibold text-zinc-700 tabular-nums hover:border-cream-border hover:bg-white"
    >
      {Math.round(pctOf(sub))}%
    </button>
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// Inline "Add a customer" row → form
// ═══════════════════════════════════════════════════════════════════════════
function AddCustomerRow({
  arrays,
  open,
  onOpen,
  onClose,
  onCreated,
}: {
  arrays: FlatArray[] | null;
  open: boolean;
  onOpen: () => void;
  onClose: () => void;
  onCreated: () => void;
}) {
  const toast = useToast();
  const [name, setName] = useState("");
  const [arrayId, setArrayId] = useState("");
  const [pct, setPct] = useState("50");
  const [email, setEmail] = useState("");
  const [saving, setSaving] = useState(false);

  const FIELD =
    "h-9 w-full rounded-lg border border-cream-border bg-white px-3 text-sm text-zinc-700 placeholder:text-zinc-400 focus:border-primary-300 focus:outline-none focus:ring-2 focus:ring-primary-500/20";
  const LABEL =
    "mb-1 block text-[11px] font-semibold uppercase tracking-wide text-zinc-400";

  function reset() {
    setName("");
    setArrayId("");
    setPct("50");
    setEmail("");
  }

  async function submit() {
    const trimmed = name.trim();
    if (!trimmed) {
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
    setSaving(true);
    try {
      await createManualSubscription({
        customer_name: trimmed,
        array_id: Number(arrayId),
        allocation_pct: pctNum / 100,
        client_email: email.trim() || null,
        cadence: "monthly",
        delivery_mode: "approval",
        send_mode: email.trim() ? "to_client" : "to_me",
      });
      toast.success(`${trimmed} added · ${Math.round(pctNum)}% allocation`);
      reset();
      onClose();
      onCreated();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't add customer");
    } finally {
      setSaving(false);
    }
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={onOpen}
        data-testid="add-customer-row"
        className="flex w-full items-center gap-2 border-t border-dashed border-cream-border bg-[#fcfdfc] px-4 py-3 text-left text-sm font-semibold text-primary-800 hover:bg-primary-50"
      >
        <span className="grid h-5 w-5 place-items-center rounded-md bg-primary-50 font-bold text-primary-800">
          +
        </span>
        Add a customer
      </button>
    );
  }

  return (
    <div
      className="border-t border-cream-border bg-[#fbfdfc] px-4 py-4"
      data-testid="add-customer-form"
    >
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-[1.4fr_1.2fr_0.7fr_1.4fr]">
        <div>
          <label className={LABEL}>Customer name</label>
          <input
            className={FIELD}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Maple Lane Farm"
            autoComplete="off"
          />
        </div>
        <div>
          <label className={LABEL}>Array</label>
          <select
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
          <label className={LABEL}>Allocation %</label>
          <input
            className={FIELD}
            type="number"
            min={1}
            max={100}
            value={pct}
            onChange={(e) => setPct(e.target.value)}
          />
        </div>
        <div>
          <label className={LABEL}>Email</label>
          <input
            className={FIELD}
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="billing@customer.com"
            autoComplete="off"
          />
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <Button onClick={submit} disabled={saving} className="px-4 py-2 text-sm">
          {saving ? "Adding…" : "Add customer"}
        </Button>
        <Button
          variant="secondary"
          onClick={onClose}
          disabled={saving}
          className="px-4 py-2 text-sm"
        >
          Cancel
        </Button>
        <span className="text-xs text-zinc-400">
          Used to split this array's generation. Remainder goes to the landowner.
        </span>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// Review drawer (right slide-over)
// ═══════════════════════════════════════════════════════════════════════════
function ReviewDrawer({
  sub,
  draft,
  arrayName,
  periodLabel,
  onClose,
  onSent,
  onDraftChange,
}: {
  sub: BillingSubscription;
  draft: ReportDraft;
  arrayName: string;
  periodLabel: string;
  onClose: () => void;
  onSent: () => void;
  onDraftChange: (d: ReportDraft) => void;
}) {
  const toast = useToast();
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [sending, setSending] = useState(false);
  const [attaching, setAttaching] = useState(false);
  const [downloading, setDownloading] = useState(false);

  // A subscription is workbook-sourced when it carries an uploaded format
  // (source_filename). Manual (typed-in) customers have allocation_pct set and
  // no workbook, so they have no own-format invoice to download.
  const hasOwnFormat = Boolean(sub.source_filename);

  const rate = rateFor(draft);
  const pct = draft.allocation_pct != null ? draft.allocation_pct * 100 : pctOf(sub);
  const arrayKwh = draft.array_total_kwh ?? 0;
  const shareKwh = draft.customer_kwh ?? (arrayKwh * pct) / 100;
  const amount = draft.amount_usd ?? shareKwh * rate;

  const [to, setTo] = useState(sub.client_email ?? "");
  const [subject, setSubject] = useState(
    `Your ${periodLabel} solar invoice — ${sub.customer_name}`,
  );
  const [message, setMessage] = useState(
    draft.note ??
      `Hi,\n\nYour solar production for ${periodLabel} is in. ${arrayName} generated ${fmtKwh(
        arrayKwh,
      )} kWh; your ${Math.round(pct)}% allocation comes to ${fmtKwh(
        shareKwh,
      )} kWh, for a total of $${fmtMoney(amount)}.\n\nYour invoice and the GMP utility statement are attached. Let me know if you have any questions.\n\nThanks`,
  );

  async function handleAttach(file: File) {
    setAttaching(true);
    try {
      const updated = await attachGmpInvoice(draft.id, file);
      onDraftChange(updated);
      toast.success("GMP statement attached");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't attach PDF");
    } finally {
      setAttaching(false);
    }
  }

  async function handleDownloadWorkbook() {
    setDownloading(true);
    try {
      await downloadInvoiceWorkbook(sub.id, sub.customer_name);
    } catch (err) {
      // The backend returns 422 for manual subs (no uploaded format); fall
      // back gracefully with a clear message rather than a raw error.
      const msg = err instanceof Error ? err.message : "";
      if (/422/.test(msg)) {
        toast.error(
          "This customer has no uploaded format — using the standard invoice",
        );
      } else {
        toast.error(msg || "Couldn't download the invoice");
      }
    } finally {
      setDownloading(false);
    }
  }

  async function send() {
    if (!draft.has_gmp_pdf) {
      toast.error("Attach the GMP PDF before sending");
      return;
    }
    setSending(true);
    try {
      // Persist any edited message as the draft note (best-effort) before send.
      try {
        await patchDraft(draft.id, { note: message });
      } catch {
        /* note save is non-fatal */
      }
      await approveDraft(draft.id);
      toast.success(`Invoice sent to ${sub.customer_name}`);
      onSent();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't send invoice");
    } finally {
      setSending(false);
    }
  }

  const FIELD =
    "w-full rounded-lg border border-cream-border bg-white px-3 py-2 text-sm text-zinc-800 focus:border-primary-300 focus:outline-none focus:ring-2 focus:ring-primary-500/20";
  const LABEL =
    "mb-1.5 mt-4 block text-[11px] font-semibold uppercase tracking-wide text-zinc-400";

  return (
    <>
      <div
        className="fixed inset-0 z-40 bg-zinc-900/40"
        onClick={onClose}
        aria-hidden
      />
      <aside
        className="fixed right-0 top-0 z-50 flex h-full w-[460px] max-w-[94vw] flex-col bg-white shadow-2xl"
        role="dialog"
        aria-label="Review invoice"
        data-testid="review-drawer"
      >
        {/* Head */}
        <div className="flex items-start gap-3 border-b border-cream-border px-5 py-4">
          <div className="min-w-0">
            <h3 className="truncate text-base font-semibold text-zinc-900">
              {sub.customer_name}
            </h3>
            <p className="text-xs text-zinc-400">
              {arrayName} · {periodLabel} billing run
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="ml-auto text-2xl leading-none text-zinc-400 hover:text-zinc-600"
            aria-label="Close"
          >
            ×
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-5 py-4">
          {/* Calc card */}
          <div className="rounded-xl border border-primary-200 bg-primary-50 px-4 py-3.5">
            {[
              ["Array generation (best available)", `${fmtKwh(arrayKwh)} kWh`],
              ["Customer allocation", `${Math.round(pct)}%`],
              ["Customer share", `${fmtKwh(shareKwh)} kWh`],
              ["Credit rate", `$${rate.toFixed(4)} / kWh`],
            ].map(([label, val]) => (
              <div
                key={label}
                className="flex justify-between py-0.5 text-sm tabular-nums text-primary-900"
              >
                <span className="text-primary-800">{label}</span>
                <span>{val}</span>
              </div>
            ))}
            <div className="mt-1.5 flex justify-between border-t border-dashed border-primary-300 pt-2 text-[15px] font-bold tabular-nums text-primary-900">
              <span>Invoice total</span>
              <span>${fmtMoney(amount)}</span>
            </div>
          </div>

          {/* Attachments */}
          <div className="mt-4 text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
            Attachments
          </div>
          <div className="mt-2 space-y-2">
            <div className="flex items-center gap-2.5 rounded-lg border border-cream-border px-3 py-2.5 text-sm">
              <span className="grid h-8 w-7 place-items-center rounded bg-red-100 text-[9px] font-bold text-red-700">
                PDF
              </span>
              <div>
                <div className="font-semibold text-zinc-800">
                  {draft.invoice_number
                    ? `Invoice-${draft.invoice_number}.pdf`
                    : "Customer invoice.pdf"}
                </div>
                <div className="text-xs text-zinc-400">Customer invoice</div>
              </div>
            </div>
            <div className="flex items-center gap-2.5 rounded-lg border border-cream-border px-3 py-2.5 text-sm">
              <span className="grid h-8 w-7 place-items-center rounded bg-red-100 text-[9px] font-bold text-red-700">
                PDF
              </span>
              <div className="min-w-0">
                <div className="truncate font-semibold text-zinc-800">
                  {draft.has_gmp_pdf
                    ? draft.gmp_filename ?? "GMP-statement.pdf"
                    : "No GMP statement yet"}
                </div>
                <div className="text-xs text-zinc-400">GMP utility statement</div>
              </div>
              <div className="ml-auto">
                <input
                  ref={fileRef}
                  type="file"
                  accept="application/pdf"
                  className="hidden"
                  onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) void handleAttach(f);
                    e.target.value = "";
                  }}
                />
                <Button
                  variant="secondary"
                  onClick={() => fileRef.current?.click()}
                  disabled={attaching}
                  className="px-3 py-1.5 text-xs"
                >
                  {attaching
                    ? "Attaching…"
                    : draft.has_gmp_pdf
                      ? "Replace"
                      : "Attach GMP PDF"}
                </Button>
              </div>
            </div>
          </div>

          {/* Download the customer's own-format populated invoice. Only shown
              for workbook-sourced subscriptions — manual (typed-in) customers
              have no uploaded format to populate. */}
          {hasOwnFormat && (
            <button
              type="button"
              onClick={() => void handleDownloadWorkbook()}
              disabled={downloading}
              className="mt-3 inline-flex items-center gap-2 text-sm font-semibold text-primary-700 hover:text-primary-800 disabled:opacity-60"
            >
              <span className="grid h-5 w-5 place-items-center rounded bg-primary-50 text-xs font-bold text-primary-800">
                ↓
              </span>
              {downloading
                ? "Preparing…"
                : "Download invoice (.xlsx, your format)"}
            </button>
          )}

          {/* Email fields */}
          <label className={LABEL}>To</label>
          <input
            className={FIELD}
            type="text"
            value={to}
            onChange={(e) => setTo(e.target.value)}
            placeholder="billing@customer.com"
          />
          <label className={LABEL}>Subject</label>
          <input
            className={FIELD}
            type="text"
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
          />
          <label className={LABEL}>Message</label>
          <textarea
            className={`${FIELD} min-h-[150px] leading-relaxed`}
            value={message}
            onChange={(e) => setMessage(e.target.value)}
          />
        </div>

        {/* Foot */}
        <div className="flex items-center gap-3 border-t border-cream-border px-5 py-3.5">
          <Button
            onClick={send}
            disabled={sending || !draft.has_gmp_pdf}
            className="px-4 py-2 text-sm"
          >
            {sending
              ? "Sending…"
              : draft.has_gmp_pdf
                ? "Approve & send"
                : "Attach GMP PDF first"}
          </Button>
          <Button
            variant="secondary"
            onClick={onClose}
            disabled={sending}
            className="px-4 py-2 text-sm"
          >
            Cancel
          </Button>
          <span className="text-xs text-zinc-400">Nothing sends automatically.</span>
        </div>
      </aside>
    </>
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// History section
// ═══════════════════════════════════════════════════════════════════════════
interface HistoryPeriod {
  key: string;
  label: string;
  sentOn: string | null;
  total: number;
  invoices: {
    id: number;
    name: string;
    kwh: number;
    pct: number;
    rate: number;
    amount: number;
  }[];
}

function buildHistory(
  sentDrafts: ReportDraft[],
): HistoryPeriod[] {
  const groups = new Map<string, HistoryPeriod>();
  for (const d of sentDrafts) {
    const label = d.period_label ?? "Earlier";
    const key = label;
    const rate = rateFor(d);
    const kwh = d.customer_kwh ?? 0;
    const amount = d.amount_usd ?? kwh * rate;
    const pct = d.allocation_pct != null ? d.allocation_pct * 100 : 0;
    let g = groups.get(key);
    if (!g) {
      g = { key, label, sentOn: d.sent_at, total: 0, invoices: [] };
      groups.set(key, g);
    }
    g.total += amount;
    g.invoices.push({ id: d.id, name: d.customer_name, kwh, pct, rate, amount });
    if (d.sent_at && (!g.sentOn || d.sent_at > g.sentOn)) g.sentOn = d.sent_at;
  }
  return Array.from(groups.values()).sort((a, b) =>
    (b.sentOn ?? "").localeCompare(a.sentOn ?? ""),
  );
}

function HistorySection({ periods }: { periods: HistoryPeriod[] }) {
  const [open, setOpen] = useState<Set<string>>(new Set());
  function toggle(k: string) {
    const next = new Set(open);
    if (next.has(k)) next.delete(k);
    else next.add(k);
    setOpen(next);
  }

  return (
    <div>
      <h3 className="text-sm font-bold text-zinc-900">History</h3>
      <p className="mb-3 mt-0.5 text-xs text-zinc-400">
        Every completed run is saved as a durable record. Expand a period to audit
        its sent invoices.
      </p>
      {periods.length === 0 ? (
        <div className="rounded-xl border border-cream-border bg-cream p-6 text-center text-sm text-zinc-400">
          No invoices have been sent yet. Your sent runs will be archived here.
        </div>
      ) : (
        <div className="overflow-hidden rounded-xl border border-cream-border bg-white shadow-sm">
          {periods.map((p) => {
            const isOpen = open.has(p.key);
            return (
              <div
                key={p.key}
                className="border-b border-cream-border last:border-b-0"
              >
                <button
                  type="button"
                  onClick={() => toggle(p.key)}
                  className="flex w-full items-center gap-3.5 px-4 py-3.5 text-left hover:bg-zinc-50"
                >
                  <span
                    className={`text-xs text-zinc-400 transition-transform ${
                      isOpen ? "rotate-90" : ""
                    }`}
                  >
                    ▶
                  </span>
                  <span className="font-semibold text-zinc-800">{p.label}</span>
                  <span className="rounded-full bg-primary-50 px-2 py-0.5 text-[11px] font-semibold text-primary-800">
                    {p.invoices.length} sent
                  </span>
                  {p.sentOn && (
                    <span className="text-xs text-zinc-400">
                      · sent {new Date(p.sentOn).toLocaleDateString()}
                    </span>
                  )}
                  <span className="ml-auto font-bold tabular-nums text-zinc-800">
                    ${fmtMoney(p.total)}
                  </span>
                </button>
                {isOpen && (
                  <div className="px-4 pb-4 pl-11">
                    {p.invoices.map((inv) => (
                      <div
                        key={inv.id}
                        className="flex flex-wrap items-center gap-3 border-b border-dashed border-cream-border py-2.5 text-sm last:border-b-0"
                      >
                        <span className="font-semibold text-zinc-800">
                          {inv.name}
                        </span>
                        <span className="text-xs tabular-nums text-zinc-400">
                          {fmtKwh(inv.kwh)} kWh × {Math.round(inv.pct)}% × $
                          {inv.rate.toFixed(4)}
                        </span>
                        <span className="ml-auto font-semibold tabular-nums text-zinc-800">
                          ${fmtMoney(inv.amount)}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// Main component
// ═══════════════════════════════════════════════════════════════════════════
export default function ReportsTab() {
  const { account, failed, retryLoad } = useDashboardContext();
  const toast = useToast();

  const [subs, setSubs] = useState<BillingSubscription[]>([]);
  const [pendingDrafts, setPendingDrafts] = useState<ReportDraft[]>([]);
  const [sentDrafts, setSentDrafts] = useState<ReportDraft[]>([]);
  const [previews, setPreviews] = useState<Map<number, SubscriptionPreview>>(
    new Map(),
  );
  const [arrays, setArrays] = useState<FlatArray[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [manageMode, setManageMode] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [batchRunning, setBatchRunning] = useState(false);

  // Drawer: the subscription + its (freshly generated) draft under review.
  const [reviewSub, setReviewSub] = useState<BillingSubscription | null>(null);
  const [reviewDraft, setReviewDraft] = useState<ReportDraft | null>(null);
  const [reviewLoadingId, setReviewLoadingId] = useState<number | null>(null);

  const loadData = useCallback(() => {
    setLoading(true);
    setLoadError(null);
    Promise.all([
      listBillingSubscriptions(),
      listDrafts("pending"),
      listDrafts("sent"),
    ])
      .then(([rows, pend, sent]) => {
        setSubs(rows);
        setPendingDrafts(pend);
        setSentDrafts(sent);
        // Eagerly compute each visible sub's billing math so every run-table
        // row shows real numbers (gen × % = kWh × rate = $) immediately —
        // never a "Review to compute" placeholder. Failures are swallowed
        // per-row so one bad workbook can't blank the whole table.
        void Promise.all(
          rows.map((s) =>
            getSubscriptionPreview(s.id)
              .then((p) => [s.id, p] as const)
              .catch(() => null),
          ),
        ).then((results) => {
          const next = new Map<number, SubscriptionPreview>();
          for (const r of results) if (r) next.set(r[0], r[1]);
          setPreviews(next);
        });
      })
      .catch((err) =>
        setLoadError(
          err instanceof Error ? err.message : "Couldn't load the billing run",
        ),
      )
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  // Lazy-load arrays when the add form or manage mode first needs them.
  useEffect(() => {
    if ((addOpen || manageMode) && arrays === null) {
      listAllArrays()
        .then(setArrays)
        .catch(() => setArrays([]));
    }
  }, [addOpen, manageMode, arrays]);

  // Draft lookup keyed by subscription id (latest pending draft wins).
  const draftBySub = useMemo(() => {
    const m = new Map<number, ReportDraft>();
    for (const d of pendingDrafts) m.set(d.subscription_id, d);
    return m;
  }, [pendingDrafts]);

  const arrayName = useCallback(
    (sub: BillingSubscription): string => {
      const a = (arrays ?? []).find((x) => x.id === sub.array_id);
      if (a) return a.name;
      return sub.source_filename ? "Workbook customer" : "—";
    },
    [arrays],
  );

  const periodLabel = useMemo(() => {
    // Prefer a draft's label (cleaned to "Month YYYY"); else derive the month
    // from any preview's period-end date; else fall back to the current month.
    for (const d of pendingDrafts) {
      const clean = cleanPeriodLabel(d.period_label);
      if (clean) return clean;
    }
    for (const p of previews.values()) {
      if (p.period_end) {
        const clean = monthYearFromISO(p.period_end);
        if (clean) return clean;
      }
    }
    return currentPeriodLabel();
  }, [pendingDrafts, previews]);

  // Period total: sum of computed amounts — authoritative draft amount when
  // present, else the eager preview amount, so the hero total reflects every
  // row's real math even before any draft is generated.
  const periodTotal = useMemo(() => {
    return subs.reduce((sum, s) => {
      const d = draftBySub.get(s.id);
      if (d?.amount_usd != null) return sum + d.amount_usd;
      const p = previews.get(s.id);
      if (p?.has_data && p.amount_usd != null) return sum + p.amount_usd;
      return sum;
    }, 0);
  }, [subs, draftBySub, previews]);

  const sentCount = useMemo(
    () => subs.filter((s) => chipFor(s, draftBySub.get(s.id)) === "sent").length,
    [subs, draftBySub],
  );
  const reviewableCount = subs.length - sentCount;
  const readyCount = useMemo(
    () => subs.filter((s) => chipFor(s, draftBySub.get(s.id)) === "ready").length,
    [subs, draftBySub],
  );

  // ── Inline % edit commit ───────────────────────────────────────────────────
  async function commitPct(sub: BillingSubscription, pct: number) {
    try {
      const updated = await patchSubscription(sub.id, {
        allocation_pct: pct / 100,
      });
      setSubs((prev) => prev.map((s) => (s.id === sub.id ? updated : s)));
      toast.success(`${sub.customer_name.split(" ")[0]} set to ${pct}%`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't update allocation");
    }
  }

  // ── Open review drawer (generate/fetch the draft first) ─────────────────────
  async function openReview(sub: BillingSubscription) {
    setReviewLoadingId(sub.id);
    try {
      let draft = draftBySub.get(sub.id);
      if (!draft) draft = await generateDraft(sub.id);
      setReviewSub(sub);
      setReviewDraft(draft);
      // Keep the run table in sync with any freshly generated draft.
      setPendingDrafts((prev) => {
        const others = prev.filter((d) => d.subscription_id !== sub.id);
        return draft ? [...others, draft] : others;
      });
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't open the draft");
    } finally {
      setReviewLoadingId(null);
    }
  }

  function onDrawerDraftChange(d: ReportDraft) {
    setReviewDraft(d);
    setPendingDrafts((prev) =>
      prev.map((x) => (x.id === d.id ? d : x)).concat(
        prev.some((x) => x.id === d.id) ? [] : [d],
      ),
    );
  }

  function closeDrawer() {
    setReviewSub(null);
    setReviewDraft(null);
  }

  function onDrawerSent() {
    closeDrawer();
    loadData();
  }

  // ── Batch "Review & send all" ───────────────────────────────────────────────
  async function batchSend() {
    const ready = subs
      .map((s) => draftBySub.get(s.id))
      .filter(
        (d): d is ReportDraft =>
          !!d && d.status === "pending" && d.has_gmp_pdf,
      );
    if (ready.length === 0) {
      toast.error("No invoices are Ready — review and attach GMP PDFs first");
      return;
    }
    setBatchRunning(true);
    let ok = 0;
    for (const d of ready) {
      try {
        await approveDraft(d.id);
        ok += 1;
      } catch {
        /* keep going; surface the count below */
      }
    }
    setBatchRunning(false);
    if (ok > 0) toast.success(`${ok} invoice(s) sent in one pass`);
    if (ok < ready.length)
      toast.error(`${ready.length - ok} invoice(s) didn't send — retry them`);
    loadData();
  }

  const history = useMemo(() => buildHistory(sentDrafts), [sentDrafts]);

  // ── Account loading guard ───────────────────────────────────────────────────
  if (account === null) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 py-24 text-zinc-400">
        {failed ? (
          <>
            <p className="text-sm">Couldn't load your account.</p>
            <Button variant="secondary" onClick={retryLoad}>
              Retry
            </Button>
          </>
        ) : (
          <Spinner className="h-6 w-6" />
        )}
      </div>
    );
  }

  const sendDisabled = manageMode;

  return (
    <ScreenLayout>
      {/* ── HERO: current billing run ─────────────────────────────────────── */}
      <section className="overflow-hidden rounded-xl border border-cream-border bg-white shadow-sm">
        <div className="flex flex-wrap items-start gap-4 border-b border-cream-border bg-gradient-to-b from-[#f6fdfa] to-white px-5 py-4">
          <div className="min-w-0">
            <div className="text-[11px] font-bold uppercase tracking-wide text-primary-800">
              Current billing run
            </div>
            <h2 className="mt-0.5 text-2xl font-bold text-zinc-900">
              {periodLabel}
            </h2>
            <div className="mt-1 text-sm text-zinc-500">
              GMP generation posted ·{" "}
              {reviewableCount} invoice{reviewableCount === 1 ? "" : "s"} to review
              &amp; send
            </div>
          </div>
          <div className="ml-auto text-right">
            <div className="mb-2 text-sm text-zinc-500">
              Period total
              <b className="block text-xl font-bold tabular-nums text-zinc-900">
                ${fmtMoney(periodTotal)}
              </b>
            </div>
            <Button
              onClick={batchSend}
              disabled={sendDisabled || batchRunning || readyCount === 0}
              className={`px-4 py-2 text-sm ${sendDisabled ? "opacity-40" : ""}`}
            >
              {batchRunning
                ? "Sending…"
                : `Review & send all ${reviewableCount} →`}
            </Button>
            <div className="mt-2">
              <Button
                variant="secondary"
                onClick={() => {
                  setManageMode((v) => {
                    const next = !v;
                    setAddOpen(next);
                    return next;
                  });
                }}
                className="px-3 py-1.5 text-xs"
              >
                {manageMode ? "Done managing" : "Manage customers"}
              </Button>
            </div>
          </div>
        </div>

        {/* Manage banner */}
        {manageMode && (
          <div className="border-b border-blue-100 bg-blue-50 px-5 py-2.5 text-xs font-medium text-blue-800">
            Manage mode — edit each customer's allocation %, or add a customer.
            Sending is paused.
          </div>
        )}

        {/* Progress line */}
        <div className="flex items-center gap-3 border-b border-cream-border px-5 py-3">
          <div className="h-2 flex-1 overflow-hidden rounded-full bg-[#eef2f1]">
            <span
              className="block h-full bg-gradient-to-r from-primary-700 to-primary-400 transition-all"
              style={{
                width: `${subs.length ? (sentCount / subs.length) * 100 : 0}%`,
              }}
            />
          </div>
          <div className="whitespace-nowrap text-xs text-zinc-500 tabular-nums">
            {sentCount} of {subs.length} sent
          </div>
        </div>

        {/* ── RUN TABLE ───────────────────────────────────────────────────── */}
        {loading ? (
          <div className="space-y-3 p-5" aria-busy>
            {Array.from({ length: 3 }).map((_, i) => (
              <div
                key={i}
                className="h-12 animate-pulse rounded-lg bg-zinc-100"
              />
            ))}
          </div>
        ) : loadError ? (
          <div className="flex flex-col items-center gap-3 p-8 text-center">
            <p className="text-sm text-zinc-500">{loadError}</p>
            <Button variant="secondary" onClick={loadData}>
              Retry
            </Button>
          </div>
        ) : subs.length === 0 ? (
          <div className="flex flex-col items-center gap-4 px-5 py-12 text-center">
            <p className="text-sm text-zinc-400">
              No billing customers yet. Add your first customer to start a run.
            </p>
            <button
              type="button"
              onClick={() => setAddOpen(true)}
              data-testid="add-first-customer"
              className="inline-flex items-center gap-2 rounded-lg bg-primary-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-primary-700"
            >
              <span className="text-base font-bold leading-none">+</span>
              Add your first customer
            </button>
          </div>
        ) : (
          <table className="w-full border-collapse">
            <thead>
              <tr>
                {["Customer", "Generation × allocation", "Customer share", "Status", ""].map(
                  (h, i) => (
                    <th
                      key={i}
                      className="border-b border-cream-border px-4 py-3 text-left text-[11px] font-semibold uppercase tracking-wide text-zinc-400"
                    >
                      {h}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody>
              {subs.map((sub) => {
                const draft = draftBySub.get(sub.id);
                const preview = previews.get(sub.id);
                const chip = chipFor(sub, draft);
                const pct = pctOf(sub);
                const isWorkbook = isWorkbookSub(sub, preview);

                // Prefer the authoritative draft, else the eager preview. Each
                // source already carries array total, customer share, $ and the
                // effective rate — never re-derive a fabricated number.
                const rate =
                  draft && draft.customer_kwh && draft.amount_usd != null
                    ? rateFor(draft)
                    : preview?.rate ?? rateFor(draft);
                const arrayKwh =
                  draft?.array_total_kwh ?? preview?.array_total_kwh ?? null;
                const shareKwh =
                  draft?.customer_kwh ?? preview?.customer_kwh ?? null;
                const amount =
                  draft?.amount_usd ?? preview?.amount_usd ?? null;
                // The effective % to display: a draft's stored % wins; else the
                // column %. For workbook subs the column is null/0 so we never
                // render a contradictory "× 0%".
                const draftPct =
                  draft?.allocation_pct != null
                    ? draft.allocation_pct * 100
                    : null;
                const effectivePct = draftPct ?? pct;
                // Real, displayable math requires generation AND a share.
                const hasMath =
                  arrayKwh != null &&
                  arrayKwh > 0 &&
                  shareKwh != null &&
                  amount != null;
                // A preview that came back but has no generation yet.
                const noData =
                  !hasMath &&
                  preview != null &&
                  !preview.has_data &&
                  draft?.array_total_kwh == null;
                const arr = arrayName(sub);
                const showArr = arr && arr !== "—" && arr !== "Workbook customer";
                return (
                  <tr
                    key={sub.id}
                    className="hover:bg-[#fafefb]"
                    data-testid="run-row"
                  >
                    {/* Customer */}
                    <td className="border-b border-[#f1f3f2] px-4 py-3.5 align-middle">
                      <div className="max-w-[15rem] truncate font-semibold text-zinc-900">
                        {sub.customer_name}
                      </div>
                      <div className="flex min-w-0 items-center gap-1 truncate text-xs text-zinc-400">
                        {showArr && (
                          <span className="truncate">{arr} ·</span>
                        )}
                        {isWorkbook && pct === 0 ? (
                          // Workbook % lives in parsed_map, not the column — show
                          // a clean label instead of a misleading "0% allocation".
                          <span>Workbook allocation</span>
                        ) : (
                          <>
                            <PctPill
                              sub={sub}
                              editable={manageMode}
                              onCommit={(p) => commitPct(sub, p)}
                            />{" "}
                            allocation
                          </>
                        )}
                      </div>
                    </td>
                    {/* Math */}
                    <td className="border-b border-[#f1f3f2] px-4 py-3.5 align-middle text-xs tabular-nums text-zinc-500">
                      {hasMath ? (
                        isWorkbook ? (
                          // Workbook subs carry the % inside parsed_map, not the
                          // column — show the derived share without a fake "× %".
                          <>
                            <b className="text-zinc-800">
                              {fmtKwh(shareKwh)} kWh
                            </b>{" "}
                            of {fmtKwh(arrayKwh)} kWh
                            <br />
                            <span className="text-[11px]">
                              Workbook allocation · {fmtKwh(shareKwh)} kWh × $
                              {rate.toFixed(4)}
                            </span>
                          </>
                        ) : (
                          <>
                            {fmtKwh(arrayKwh)} kWh × {Math.round(effectivePct)}% ={" "}
                            <b className="text-zinc-800">{fmtKwh(shareKwh)} kWh</b>
                            <br />
                            <span className="text-[11px]">
                              {fmtKwh(shareKwh)} kWh × ${rate.toFixed(4)}
                            </span>
                          </>
                        )
                      ) : noData ? (
                        <span className="text-zinc-400">No generation data yet</span>
                      ) : (
                        <span className="text-zinc-400">Computing share…</span>
                      )}
                    </td>
                    {/* Customer share $ */}
                    <td className="border-b border-[#f1f3f2] px-4 py-3.5 align-middle">
                      <span className="text-[15px] font-bold tabular-nums text-zinc-900">
                        {amount != null ? `$${fmtMoney(amount)}` : "—"}
                      </span>
                    </td>
                    {/* Status chip */}
                    <td className="border-b border-[#f1f3f2] px-4 py-3.5 align-middle">
                      <span
                        className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold ${
                          CHIP_CLASS[chip]
                        } ${sendDisabled ? "opacity-40" : ""}`}
                      >
                        <span
                          className={`h-1.5 w-1.5 rounded-full ${CHIP_DOT[chip]}`}
                        />
                        {CHIP_LABEL[chip]}
                      </span>
                    </td>
                    {/* Action */}
                    <td className="border-b border-[#f1f3f2] px-4 py-3.5 text-right align-middle">
                      {chip === "sent" ? (
                        <span className="text-xs text-zinc-400">Sent ✓</span>
                      ) : (
                        <Button
                          variant="secondary"
                          onClick={() => openReview(sub)}
                          disabled={sendDisabled || reviewLoadingId === sub.id}
                          className={`px-3 py-1.5 text-xs ${
                            sendDisabled ? "opacity-40" : ""
                          }`}
                        >
                          {reviewLoadingId === sub.id
                            ? "Opening…"
                            : chip === "needs"
                              ? "Attach GMP PDF"
                              : "Review"}
                        </Button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}

        {/* ── Add a customer (inline) ─────────────────────────────────────── */}
        {/* When there are zero customers the prominent empty-state CTA above
            opens this same form, so we only render the dashed collapsed row
            when there are existing customers (or the form is already open). */}
        {!loading && !loadError && (subs.length > 0 || addOpen) && (
          <AddCustomerRow
            arrays={arrays}
            open={addOpen}
            onOpen={() => setAddOpen(true)}
            onClose={() => setAddOpen(false)}
            onCreated={loadData}
          />
        )}
      </section>

      {/* ── HISTORY ──────────────────────────────────────────────────────── */}
      {!loading && !loadError && <HistorySection periods={history} />}

      {/* ── REVIEW DRAWER ────────────────────────────────────────────────── */}
      {reviewSub && reviewDraft && (
        <ReviewDrawer
          sub={reviewSub}
          draft={reviewDraft}
          arrayName={arrayName(reviewSub)}
          periodLabel={periodLabel}
          onClose={closeDrawer}
          onSent={onDrawerSent}
          onDraftChange={onDrawerDraftChange}
        />
      )}
    </ScreenLayout>
  );
}
