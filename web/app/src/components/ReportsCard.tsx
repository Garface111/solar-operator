import { useState } from "react";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Modal } from "../ui/Modal";
import { Spinner } from "../ui/Spinner";
import { Toggle } from "../ui/Toggle";
import { useToast } from "../ui/Toast";
import {
  type Account,
  type SendResult,
  sendReportNow,
  updateAccountFrequency,
  updateCcOnReports,
} from "../lib/api";

const FREQUENCIES = [
  { value: "weekly", label: "Weekly" },
  { value: "monthly", label: "Monthly" },
  { value: "quarterly", label: "Quarterly" },
] as const;

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
 *  week / month / quarter. A friendly estimate — the scheduler fires on calendar
 *  boundaries, but this answers "roughly when does my next one go?". */
function nextSendDate(freq: string, lastDeliveryIso: string | null): Date {
  if (lastDeliveryIso) {
    const d = new Date(lastDeliveryIso);
    if (freq === "weekly") d.setDate(d.getDate() + 7);
    else if (freq === "monthly") d.setMonth(d.getMonth() + 1);
    else d.setMonth(d.getMonth() + 3); // quarterly
    return d;
  }
  const now = new Date();
  if (freq === "weekly") {
    const d = new Date(now);
    d.setDate(d.getDate() + ((7 - d.getDay()) % 7)); // end of week (Sunday)
    return d;
  }
  if (freq === "monthly") {
    return new Date(now.getFullYear(), now.getMonth() + 1, 0); // last day of month
  }
  const endMonth = Math.floor(now.getMonth() / 3) * 3 + 3; // quarter end
  return new Date(now.getFullYear(), endMonth, 0);
}

interface Props {
  account: Account;
  onAccountChange: (patch: Partial<Account>) => void;
}

export function ReportsCard({ account, onAccountChange }: Props) {
  const toast = useToast();
  const [savingFreq, setSavingFreq] = useState(false);
  const [savingCc, setSavingCc] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [sending, setSending] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);

  async function selectFrequency(next: string) {
    if (next === account.report_frequency || savingFreq) return;
    const prev = account.report_frequency;
    // Optimistic — snap the control immediately, revert if the save fails.
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

  async function toggleCc(next: boolean) {
    if (savingCc) return;
    const prev = account.cc_on_reports;
    // Optimistic — flip immediately, revert if the save fails.
    onAccountChange({ cc_on_reports: next });
    setSavingCc(true);
    try {
      const value = await updateCcOnReports(next);
      onAccountChange({ cc_on_reports: value });
      toast.success(
        value
          ? "You'll get a copy of every report"
          : "Stopped copying you on reports",
      );
    } catch (err) {
      onAccountChange({ cc_on_reports: prev });
      toast.error(
        err instanceof Error ? err.message : "Couldn't update that setting",
      );
    } finally {
      setSavingCc(false);
    }
  }

  async function doSend() {
    setSending(true);
    try {
      const res = await sendReportNow();
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
        // Everything failed — show why, per client.
        toast.error(`Report failed for all ${total}. ${describeFailures(failures)}`);
      } else {
        // Partial — be specific about who didn't get it and why.
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

  return (
    <Card>
      <h2 className="text-lg font-semibold tracking-tight text-zinc-900">
        Automatic reports
      </h2>
      <p className="mt-2 text-sm leading-relaxed text-zinc-600">
        Solar Operator generates NEPOOL-GIS quarterly generation workbooks for
        each of your clients and emails them automatically. Each workbook has
        one sheet per array, covering the last 6 complete quarters of GMP bill
        data, with REC counts (floor of MWh) per month and the standard NEPOOL
        footnote.
      </p>

      {/* Frequency selector — segmented control */}
      <div className="mt-6">
        <span className="text-sm font-medium text-zinc-700">Schedule</span>
        <div
          role="radiogroup"
          aria-label="Report schedule"
          className="mt-2 inline-flex rounded-xl border border-zinc-200 bg-zinc-50 p-1"
        >
          {FREQUENCIES.map((f) => {
            const selected = account.report_frequency === f.value;
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
            nextSendDate(
              account.report_frequency ?? "quarterly",
              account.last_delivery_at,
            ).toISOString(),
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

      {/* Copy me on every report */}
      <div className="mt-6 border-t border-zinc-100 pt-5">
        <Toggle
          id="cc-on-reports"
          checked={account.cc_on_reports}
          disabled={savingCc}
          onChange={toggleCc}
          label="Send me a copy of every report"
        />
        <p className="ml-14 mt-1.5 text-xs leading-relaxed text-zinc-400">
          You&apos;ll receive an identical email to whatever each client gets,
          every time a report goes out. Useful for keeping records or QA.
        </p>
        {account.send_mode === "to_me" && (
          <p className="ml-14 mt-1 text-xs text-amber-700">
            Note: when send mode is &ldquo;To me only,&rdquo; clients are never
            emailed — this copy goes to you alongside the forwarding copy.
          </p>
        )}
      </div>

      {/* Send now */}
      <div className="mt-6">
        <Button onClick={() => setConfirmOpen(true)}>Send a report now</Button>
        <p className="mt-2 text-xs text-zinc-400">
          Manual sends don&apos;t change your schedule — your next automatic
          report will still go out on the cadence above.
        </p>
      </div>

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
        )}
      </div>

      <Modal
        open={confirmOpen}
        onClose={() => {
          if (!sending) setConfirmOpen(false);
        }}
        title="Send a report now?"
        footer={
          <>
            <Button
              variant="secondary"
              onClick={() => setConfirmOpen(false)}
              disabled={sending}
            >
              Cancel
            </Button>
            <Button onClick={doSend} disabled={sending}>
              {sending ? (
                <>
                  <Spinner />
                  Sending…
                </>
              ) : (
                "Send report"
              )}
            </Button>
          </>
        }
      >
        This will email this quarter&apos;s workbook to all your clients.
        Continue?
      </Modal>
    </Card>
  );
}
