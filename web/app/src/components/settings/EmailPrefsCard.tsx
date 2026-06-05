import { useState } from "react";
import { Card } from "../../ui/Card";
import { Toggle } from "../../ui/Toggle";
import { useToast } from "../../ui/Toast";
import { type Account, updateCcOnReports, updateAccountFrequency } from "../../lib/api";

const CADENCE_OPTIONS = [
  { value: "quarterly", label: "Quarterly" },
  { value: "monthly", label: "Monthly" },
] as const;

interface Props {
  account: Account;
  onAccountChange: (patch: Partial<Account>) => void;
}

export function EmailPrefsCard({ account, onAccountChange }: Props) {
  const toast = useToast();
  const [savingCc, setSavingCc] = useState(false);
  const [savingFreq, setSavingFreq] = useState(false);

  async function toggleCc(next: boolean) {
    setSavingCc(true);
    try {
      const val = await updateCcOnReports(next);
      onAccountChange({ cc_on_reports: val });
      toast.success(val ? "You'll receive a copy of every report" : "CC copy disabled");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't update preference");
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
      toast.error(err instanceof Error ? err.message : "Couldn't update cadence");
    } finally {
      setSavingFreq(false);
    }
  }

  const currentFreq = account.report_frequency ?? "quarterly";

  return (
    <Card>
      <h2 className="text-lg font-semibold tracking-tight text-zinc-900">Email preferences</h2>
      <p className="mt-0.5 text-sm text-zinc-500">Control how and when reports go out.</p>

      <div className="mt-5 space-y-6">
        <div className="flex items-start justify-between gap-6">
          <div>
            <p className="text-sm font-medium text-zinc-800">CC me on every report</p>
            <p className="mt-0.5 text-xs leading-relaxed text-zinc-400">
              You'll get a copy of each report email as it goes out — useful for records or QA.
            </p>
          </div>
          <Toggle
            checked={account.cc_on_reports}
            onChange={toggleCc}
            disabled={savingCc}
          />
        </div>

        <div>
          <p className="text-sm font-medium text-zinc-800">Report cadence</p>
          <p className="mt-0.5 text-xs text-zinc-400">How often reports are generated and sent.</p>
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
                      ? "bg-white text-zinc-900 shadow-sm"
                      : "text-zinc-500 hover:text-zinc-800",
                  ].join(" ")}
                >
                  {opt.label}
                </button>
              );
            })}
          </div>
        </div>
      </div>
    </Card>
  );
}
