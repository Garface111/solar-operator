import { useState, useEffect } from "react";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Modal } from "../ui/Modal";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import {
  type Account,
  type SendResult,
  type ClientRow,
  listClients,
  sendReportNow,
  sendSampleReport,
  updateAccountFrequency,
} from "../lib/api";

const FREQUENCIES = [
  { value: "monthly", label: "Monthly" },
  { value: "quarterly", label: "Quarterly" },
] as const;

const SAMPLE_WORKBOOK_URL =
  "https://web-production-49c83.up.railway.app/onboarding/sample.xlsx";

/** Compact, human "<name> (<reason>)" list for a failed-send toast. */
function describeFailures(failures: SendResult[]): string {
  return failures
    .map((f) => {
      const name = f.client_name?.trim() || "a client";
      return f.reason ? `${name} (${f.reason})` : name;
    })
    .join("; ");
}

function humanDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

/** Estimate the next automatic send. If a report has gone out before, it's the
 *  last delivery plus one cadence interval; otherwise the end of the current
 *  month / quarter. */
function nextSendDate(freq: string, lastDeliveryIso: string | null): Date {
  if (lastDeliveryIso) {
    const d = new Date(lastDeliveryIso);
    if (freq === "monthly") d.setMonth(d.getMonth() + 1);
    else d.setMonth(d.getMonth() + 3); // quarterly
    return d;
  }
  const now = new Date();
  if (freq === "monthly") {
    return new Date(now.getFullYear(), now.getMonth() + 1, 0); // last day of month
  }
  const endMonth = Math.floor(now.getMonth() / 3) * 3 + 3; // quarter end
  return new Date(now.getFullYear(), endMonth, 0);
}

/** Coerce legacy "weekly" rows to "monthly" for display only. */
function displayFrequency(freq: string | null): string {
  if (!freq || freq === "weekly") return "monthly";
  return freq;
}

interface Props {
  account: Account;
  onAccountChange: (patch: Partial<Account>) => void;
}

export function ReportsCard({ account, onAccountChange }: Props) {
  const toast = useToast();
  const [savingFreq, setSavingFreq] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [sending, setSending] = useState(false);
  const [sendingSample, setSendingSample] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  // Per-client checkbox state, loaded when the modal opens.
  const [clientList, setClientList] = useState<ClientRow[] | null>(null);
  const [loadingClients, setLoadingClients] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());

  // Lazy-load clients on first modal open so we never block the dashboard.
  useEffect(() => {
    if (!confirmOpen || clientList !== null) return;
    setLoadingClients(true);
    listClients()
      .then((rows) => {
        const active = rows.filter((c) => c.active);
        setClientList(active);
        // Default: every active client checked.
        setSelectedIds(new Set(active.map((c) => c.id)));
      })
      .catch((err) => {
        toast.error(err instanceof Error ? err.message : "Couldn't load clients");
        setClientList([]);
      })
      .finally(() => setLoadingClients(false));
  }, [confirmOpen, clientList, toast]);

  const freq = displayFrequency(account.report_frequency);

  async function selectFrequency(next: string) {
    if (next === freq || savingFreq) return;
    const prev = account.report_frequency;
    onAccountChange({ report_frequency: next });
    setSavingFreq(true);
    try {
      const frequency = await updateAccountFrequency(next);
      onAccountChange({ report_frequency: frequency });
      toast.success(`Reports now send ${frequency}`);
    } catch (err) {
      onAccountChange({ report_frequency: prev });
      toast.error(
        err instanceof Error ? err.message : "Couldn't update the schedule",
      );
    } finally {
      setSavingFreq(false);
    }
  }

  async function doSend() {
    setSending(true);
    try {
      const ids = Array.from(selectedIds);
      // If everything is selected, omit client_ids so the backend uses its
      // "all clients" code path. If a subset is selected, send those ids.
      const allSelected =
        clientList !== null && ids.length === clientList.length;
      const res = await sendReportNow(allSelected ? undefined : ids);
      setConfirmOpen(false);

      const total = res.client_count;
      const ok = res.delivered;
      const failures = res.results.filter((r) => !r.ok);

      if (total === 0) {
        toast.error(
          "No active clients to send to — add a client first.",
        );
      } else if (failures.length === 0) {
        toast.success(
          ok === 1
            ? "Report sent to 1 client."
            : `Report sent to ${ok} clients.`,
        );
      } else if (ok === 0) {
        toast.error(`Report failed for all ${total}. ${describeFailures(failures)}`);
      } else {
        toast.error(
          `Sent to ${ok} of ${total}. ${failures.length} failed: ${describeFailures(failures)}`,
        );
      }
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Couldn't send the report",
      );
    } finally {
      setSending(false);
    }
  }

  async function doSendSample() {
    setSendingSample(true);
    try {
      const res = await sendSampleReport();
      toast.success(`Sample sent to ${res.sent_to}. Check your inbox.`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Couldn't send sample";
      if (msg.includes("Add a client")) {
        toast.error(
          "Add a client and at least one array first — then come back to preview the email.",
        );
      } else {
        toast.error(msg);
      }
    } finally {
      setSendingSample(false);
    }
  }

  return (
    <Card>
      <h2 className="text-lg font-semibold tracking-tight text-zinc-900">
        Automatic reports
      </h2>
      <p className="mt-2 text-sm leading-relaxed text-zinc-600">
        Solar Operator generates NEPOOL-GIS quarterly generation workbooks for
        each of your clients and emails them automatically. Each workbook has
        one sheet per array, covering the last 6 complete quarters of utility
        bill data, with REC counts (floor of MWh) per month and the standard
        NEPOOL footnote.
      </p>

      {/* Frequency selector — segmented control */}
      <div className="mt-6">
        <span className="block text-sm font-medium text-zinc-700">Schedule</span>
        <div
          role="radiogroup"
          aria-label="Report schedule"
          className="mt-3 inline-flex rounded-xl border border-zinc-200 bg-zinc-50 p-1"
        >
          {FREQUENCIES.map((f) => {
            const selected = freq === f.value;
            return (
              <button
                key={f.value}
                type="button"
                role="radio"
                aria-checked={selected}
                disabled={savingFreq}
                onClick={() => selectFrequency(f.value)}
                className={[
                  "rounded-lg px-4 py-1.5 text-sm font-medium transition-colors",
                  "focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40",
                  "disabled:cursor-not-allowed",
                  selected
                    ? "bg-white text-zinc-900 shadow-sm"
                    : "text-zinc-500 hover:text-zinc-800",
                ].join(" ")}
              >
                {f.label}
              </button>
            );
          })}
        </div>
      </div>

      {/* Next + last delivery */}
      <p className="mt-4 text-sm text-zinc-700">
        Next automatic report:{" "}
        <span className="font-medium text-zinc-900">
          {humanDate(
            nextSendDate(freq, account.last_delivery_at).toISOString(),
          )}
        </span>
      </p>
      <p className="mt-1 text-xs text-zinc-400">
        Changes take effect immediately — your next scheduled send will use the
        new cadence.
      </p>
      <p className="mt-2 text-sm text-zinc-500">
        {account.last_delivery_at
          ? `Last sent: ${humanDate(account.last_delivery_at)}`
          : "No reports sent yet"}
      </p>

      {/* What it looks like — collapsible */}
      <div className="mt-6 border-t border-zinc-100 pt-4">
        <button
          type="button"
          onClick={() => setDetailsOpen((o) => !o)}
          aria-expanded={detailsOpen}
          className="flex w-full items-center justify-between text-left text-sm font-medium text-zinc-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
        >
          <span>What it looks like</span>
          <span
            aria-hidden
            className={`text-zinc-400 transition-transform ${
              detailsOpen ? "rotate-180" : ""
            }`}
          >
            ▾
          </span>
        </button>
        {detailsOpen && (
          <>
            <ul className="mt-3 space-y-1.5 text-sm text-zinc-500">
              <li>
                • One workbook per client (each client&apos;s arrays get their own
                sheet inside)
              </li>
              <li>• Sheet title = &ldquo;&lt;Array Name&gt; (&lt;NEPOOL-GIS ID&gt;)&rdquo;</li>
              <li>• Rolling 6 quarters of monthly MWh + REC counts</li>
              <li>• Standard NEPOOL footnote in row 31</li>
              <li>
                • Delivered to the client&apos;s contact email (+ CCs if
                configured)
              </li>
            </ul>
            <p className="mt-3">
              <a
                href={SAMPLE_WORKBOOK_URL}
                download
                className="text-sm text-primary-600 underline underline-offset-2 hover:text-primary-800"
              >
                Download example workbook (.xlsx)
              </a>
            </p>
          </>
        )}
      </div>

      {/* Sample + Send now */}
      <div className="mt-6 flex flex-wrap items-center gap-3">
        <Button
          variant="secondary"
          onClick={doSendSample}
          disabled={sendingSample}
        >
          {sendingSample ? (
            <>
              <Spinner />
              Sending…
            </>
          ) : (
            "Send me a sample"
          )}
        </Button>
        <Button onClick={() => setConfirmOpen(true)}>Send a report now</Button>
      </div>
      <p className="mt-2 text-xs text-zinc-400">
        &ldquo;Send me a sample&rdquo; sends one workbook to your own inbox only — no client is contacted.
      </p>

      <Modal
        open={confirmOpen}
        onClose={() => {
          if (!sending) setConfirmOpen(false);
        }}
        title="Send a report now"
        footer={
          <>
            <Button
              variant="secondary"
              onClick={() => setConfirmOpen(false)}
              disabled={sending}
            >
              Cancel
            </Button>
            <Button
              onClick={doSend}
              disabled={sending || selectedIds.size === 0}
            >
              {sending ? (
                <>
                  <Spinner />
                  Sending…
                </>
              ) : selectedIds.size === 0 ? (
                "Pick at least one client"
              ) : clientList && selectedIds.size === clientList.length ? (
                `Send to all ${clientList.length}`
              ) : (
                `Send to ${selectedIds.size} client${selectedIds.size === 1 ? "" : "s"}`
              )}
            </Button>
          </>
        }
      >
        {loadingClients && clientList === null ? (
          <div className="flex items-center gap-2 text-sm text-zinc-500">
            <Spinner />
            Loading your clients…
          </div>
        ) : clientList === null || clientList.length === 0 ? (
          <p className="text-sm text-zinc-600">
            No active clients yet. Add a client first.
          </p>
        ) : (
          <div className="space-y-3">
            <p className="text-sm text-zinc-600">
              Pick which clients to send to. Each picked client gets this
              quarter&apos;s workbook by email.
            </p>
            <div className="flex items-center justify-between border-b border-zinc-100 pb-2 text-xs">
              <button
                type="button"
                onClick={() => setSelectedIds(new Set(clientList.map((c) => c.id)))}
                className="font-medium text-primary-600 hover:text-primary-700"
              >
                Select all
              </button>
              <button
                type="button"
                onClick={() => setSelectedIds(new Set())}
                className="font-medium text-zinc-500 hover:text-zinc-700"
              >
                Select none
              </button>
            </div>
            <ul className="max-h-64 space-y-1 overflow-y-auto pr-1">
              {clientList.map((c) => {
                const checked = selectedIds.has(c.id);
                return (
                  <li key={c.id}>
                    <label className="flex cursor-pointer items-center gap-2.5 rounded-lg px-2 py-1.5 hover:bg-zinc-50">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={(e) => {
                          setSelectedIds((prev) => {
                            const next = new Set(prev);
                            if (e.target.checked) next.add(c.id);
                            else next.delete(c.id);
                            return next;
                          });
                        }}
                        className="h-4 w-4 accent-primary-600"
                      />
                      <span className="text-sm text-zinc-800">{c.name}</span>
                      {!c.contact_email && (
                        <span className="ml-auto text-[11px] text-amber-600">
                          no contact email
                        </span>
                      )}
                    </label>
                  </li>
                );
              })}
            </ul>
          </div>
        )}
      </Modal>
    </Card>
  );
}
