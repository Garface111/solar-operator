import { useState, useEffect } from "react";
import { Toggle } from "../../ui/Toggle";
import { Button } from "../../ui/Button";
import { useToast } from "../../ui/Toast";
import {
  type Account,
  type EmailTemplatePreviewResult,
  updateCcOnReports,
  updateAccountFrequency,
  previewEmailTemplate,
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

export function AutoReportsSettingsCard({
  account,
  onAccountChange,
  onOpenStudio,
}: Props) {
  const toast = useToast();
  const [savingCc, setSavingCc] = useState(false);
  const [savingFreq, setSavingFreq] = useState(false);

  const [preview, setPreview] = useState<EmailTemplatePreviewResult | null>(null);
  const [previewLoading, setPreviewLoading] = useState(true);
  const [previewError, setPreviewError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setPreviewLoading(true);
    setPreviewError(null);
    previewEmailTemplate({
      subject_template: account.email_subject_template ?? null,
      body_template: account.email_body_template ?? null,
    })
      .then((res) => {
        if (!cancelled) {
          setPreview(res);
          setPreviewLoading(false);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setPreviewError(err instanceof Error ? err.message : "Couldn't render preview");
          setPreviewLoading(false);
        }
      });
    return () => { cancelled = true; };
  }, [account.email_subject_template, account.email_body_template]);

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
          <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
            Email template
          </p>
          <p className="mb-3 text-[11px] uppercase tracking-wide text-zinc-400">
            ✦ Sample preview · what your clients actually receive
          </p>
          <div className="rounded-xl border border-primary-200 bg-primary-50/50 p-4">
            {/* Mini email card */}
            <div className="rounded-lg border border-zinc-200 bg-white shadow-sm">
              {/* Email headers */}
              <div className="space-y-1.5 px-4 py-3">
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
                  <span className="font-medium text-zinc-800">
                    {previewLoading
                      ? <span className="inline-block h-3 w-48 animate-pulse rounded bg-zinc-200" />
                      : previewError
                      ? <span className="text-zinc-400">{account.email_subject_template ?? account.default_email_subject}</span>
                      : preview?.subject_rendered}
                  </span>
                </div>
              </div>
              {/* Divider */}
              <div className="border-t border-zinc-100" />
              {/* Email body */}
              <div className="max-h-72 overflow-y-auto px-4 py-3">
                {previewLoading ? (
                  <div className="flex items-center justify-center py-8">
                    <svg
                      className="h-5 w-5 animate-spin text-zinc-300"
                      fill="none"
                      viewBox="0 0 24 24"
                    >
                      <circle
                        className="opacity-25"
                        cx="12"
                        cy="12"
                        r="10"
                        stroke="currentColor"
                        strokeWidth="4"
                      />
                      <path
                        className="opacity-75"
                        fill="currentColor"
                        d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
                      />
                    </svg>
                  </div>
                ) : previewError ? (
                  <p className="text-xs text-red-400">
                    Couldn't render preview. The template will still send
                    correctly.{" "}
                    <button
                      type="button"
                      className="underline hover:text-red-500"
                      onClick={() => {
                        setPreviewLoading(true);
                        setPreviewError(null);
                        previewEmailTemplate({
                          subject_template: account.email_subject_template ?? null,
                          body_template: account.email_body_template ?? null,
                        })
                          .then((res) => {
                            setPreview(res);
                            setPreviewLoading(false);
                          })
                          .catch((err) => {
                            setPreviewError(
                              err instanceof Error ? err.message : "Couldn't render preview",
                            );
                            setPreviewLoading(false);
                          });
                      }}
                    >
                      Try again
                    </button>
                  </p>
                ) : (
                  <div
                    className="prose prose-sm max-w-none text-xs leading-relaxed text-zinc-700"
                    dangerouslySetInnerHTML={{ __html: preview?.body_rendered ?? "" }}
                  />
                )}
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
