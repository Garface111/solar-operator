import { useState } from "react";
import { Toggle } from "../../ui/Toggle";
import { Button } from "../../ui/Button";
import { useToast } from "../../ui/Toast";
import {
  type Account,
  updateCcOnReports,
  updateAccountFrequency,
} from "../../lib/api";

const CADENCE_OPTIONS = [
  { value: "quarterly", label: "Quarterly" },
  { value: "monthly", label: "Monthly" },
] as const;

interface Props {
  account: Account;
  onAccountChange: (patch: Partial<Account>) => void;
  onOpenStudio: () => void;
}

function stripHtml(html: string): string {
  return html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

export function AutoReportsSettingsCard({
  account,
  onAccountChange,
  onOpenStudio,
}: Props) {
  const toast = useToast();
  const [savingCc, setSavingCc] = useState(false);
  const [savingFreq, setSavingFreq] = useState(false);

  async function toggleCc(next: boolean) {
    setSavingCc(true);
    try {
      const val = await updateCcOnReports(next);
      onAccountChange({ cc_on_reports: val });
      toast.success(
        val ? "You'll receive a copy of every report" : "CC copy disabled",
      );
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Couldn't update preference",
      );
    } finally {
      setSavingCc(false);
    }
  }

  async function setFrequency(freq: string) {
    if (freq === account.report_frequency) return;
    setSavingFreq(true);
    try {
      const val = await updateAccountFrequency(freq);
      onAccountChange({ report_frequency: val });
      toast.success(`Report cadence set to ${val}`);
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Couldn't update cadence",
      );
    } finally {
      setSavingFreq(false);
    }
  }

  const currentFreq = account.report_frequency ?? "quarterly";

  const fromName = account.send_from_name || account.name || "Solar Operator";
  const fromEmail = account.send_from_email || "admin@solaroperator.org";
  const subject =
    account.email_subject_template ?? account.default_email_subject;
  const bodyPreview = stripHtml(
    account.email_body_template ?? account.default_email_body,
  );

  return (
    <div>
      <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-zinc-400">
        Delivery settings
      </h2>

      <div className="rounded-xl border border-cream-border bg-cream shadow-sm">
        {/* Region A: Cadence */}
        <div className="px-5 py-4">
          <p className="text-sm font-medium text-zinc-800">Report cadence</p>
          <p className="mt-0.5 text-xs text-zinc-400">
            How often reports are generated and sent.
          </p>
          <div
            role="radiogroup"
            aria-label="Report cadence"
            className="mt-3 flex w-fit rounded-xl border border-zinc-200 bg-zinc-50 p-1"
          >
            {CADENCE_OPTIONS.map((opt) => {
              const selected = currentFreq === opt.value;
              return (
                <button
                  key={opt.value}
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  disabled={savingFreq}
                  onClick={() => setFrequency(opt.value)}
                  className={[
                    "rounded-lg px-4 py-1.5 text-sm font-medium transition-colors",
                    "focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40",
                    "disabled:cursor-not-allowed disabled:opacity-50",
                    selected
                      ? "bg-primary-500 text-white shadow-sm"
                      : "text-zinc-500 hover:text-zinc-800",
                  ].join(" ")}
                >
                  {opt.label}
                </button>
              );
            })}
          </div>
        </div>

        {/* Region B: CC me toggle */}
        <div className="border-t border-cream-border px-5 py-4">
          <div className="flex items-center justify-between gap-4">
            <div>
              <p className="text-sm font-medium text-zinc-800">
                CC me on every report
              </p>
              <p className="mt-0.5 text-xs text-zinc-400">
                Get a copy of each report email as it goes out — useful for
                records or QA.
              </p>
            </div>
            <Toggle
              checked={account.cc_on_reports}
              onChange={toggleCc}
              disabled={savingCc}
            />
          </div>
        </div>

        {/* Region C: Email template preview — the centerpiece CTA */}
        <div className="border-t border-cream-border px-5 py-5">
          <p className="mb-3 text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
            Email template
          </p>
          <div className="rounded-xl border border-primary-200 bg-primary-50/50 p-4">
            <div className="space-y-1.5">
              <div className="flex items-baseline gap-2 text-xs">
                <span className="w-14 shrink-0 font-semibold uppercase tracking-wide text-zinc-400">
                  From
                </span>
                <span className="text-zinc-700">{`${fromName} <${fromEmail}>`}</span>
              </div>
              <div className="flex items-baseline gap-2 text-xs">
                <span className="w-14 shrink-0 font-semibold uppercase tracking-wide text-zinc-400">
                  Subject
                </span>
                <span className="font-medium text-zinc-800">{subject}</span>
              </div>
              <div className="border-t border-primary-100 pt-2.5">
                <p className="line-clamp-2 text-xs leading-relaxed text-zinc-500">
                  {bodyPreview}
                </p>
              </div>
            </div>
            <div className="mt-4">
              <Button onClick={onOpenStudio} className="w-full py-3">
                Customize email template
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
