import { useState } from "react";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";
import { Modal } from "../ui/Modal";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import {
  type Account,
  type EmailPreview,
  type EmailSettingsInput,
  previewEmail,
  updateEmailSettings,
} from "../lib/api";

const SEND_MODES = [
  { value: "to_client", label: "To my clients" },
  { value: "to_me", label: "To me only (I forward)" },
] as const;

const MERGE_HELP = "Use {{client_name}}, {{tenant_name}}, {{quarter}}";

interface Props {
  account: Account;
  onAccountChange: (patch: Partial<Account>) => void;
}

export function EmailCustomizationCard({ account, onAccountChange }: Props) {
  const toast = useToast();
  // The form mirrors the stored template fields; "" means "use the default".
  const [fromEmail, setFromEmail] = useState(account.send_from_email ?? "");
  const [fromName, setFromName] = useState(account.send_from_name ?? "");
  const [subject, setSubject] = useState(account.email_subject_template ?? "");
  const [body, setBody] = useState(account.email_body_template ?? "");
  const [sendMode, setSendMode] = useState(account.send_mode || "to_client");

  const [saving, setSaving] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [preview, setPreview] = useState<EmailPreview | null>(null);

  function currentInput(): EmailSettingsInput {
    return {
      send_from_email: fromEmail,
      send_from_name: fromName,
      email_subject_template: subject,
      email_body_template: body,
      send_mode: sendMode,
    };
  }

  async function persist(
    input: EmailSettingsInput,
    successMsg: string,
  ): Promise<boolean> {
    setSaving(true);
    try {
      const saved = await updateEmailSettings(input);
      // Reflect server-normalized values (blank → null) back into the form + account.
      setFromEmail(saved.send_from_email ?? "");
      setFromName(saved.send_from_name ?? "");
      setSubject(saved.email_subject_template ?? "");
      setBody(saved.email_body_template ?? "");
      setSendMode(saved.send_mode || "to_client");
      onAccountChange({
        send_from_email: saved.send_from_email,
        send_from_name: saved.send_from_name,
        email_subject_template: saved.email_subject_template,
        email_body_template: saved.email_body_template,
        send_mode: saved.send_mode || "to_client",
      });
      toast.success(successMsg);
      return true;
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Couldn't save your email settings",
      );
      return false;
    } finally {
      setSaving(false);
    }
  }

  async function doPreview() {
    setPreviewing(true);
    try {
      const p = await previewEmail(currentInput());
      setPreview(p);
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Couldn't render the preview",
      );
    } finally {
      setPreviewing(false);
    }
  }

  async function saveFromModal() {
    const ok = await persist(currentInput(), "Email settings saved");
    if (ok) setPreview(null);
  }

  async function resetToDefaults() {
    if (saving) return;
    // Clear the four template fields (blank → null server-side). Keep send_mode.
    setSubject("");
    setBody("");
    setFromEmail("");
    setFromName("");
    await persist(
      {
        send_from_email: "",
        send_from_name: "",
        email_subject_template: "",
        email_body_template: "",
        send_mode: sendMode,
      },
      "Reset to the default template",
    );
  }

  return (
    <Card>
      <h2 className="text-lg font-semibold tracking-tight text-zinc-900">
        Your reports, your voice
      </h2>
      <p className="mt-2 text-sm leading-relaxed text-zinc-600">
        Customize how reports go out to your clients. Preview before saving.
      </p>

      <div className="mt-6 space-y-5">
        <Input
          id="send-from-email"
          label="Send from email"
          type="email"
          placeholder="admin@solaroperator.org"
          value={fromEmail}
          onChange={(e) => setFromEmail(e.target.value)}
        />
        <p className="-mt-3 text-xs text-zinc-400">
          Leave blank to send from the Solar Operator address. Custom domains
          must be verified, or we fall back to the default automatically.
        </p>

        <Input
          id="send-from-name"
          label="Send from name"
          placeholder={account.name ?? "Your company name"}
          value={fromName}
          onChange={(e) => setFromName(e.target.value)}
        />

        <div>
          <Input
            id="email-subject"
            label="Subject line"
            placeholder={account.default_email_subject}
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
          />
          <p className="mt-1.5 text-xs text-zinc-400">{MERGE_HELP}</p>
        </div>

        <div>
          <label
            htmlFor="email-body"
            className="mb-1.5 block text-sm font-medium text-zinc-700"
          >
            Email body
          </label>
          <textarea
            id="email-body"
            rows={10}
            placeholder={account.default_email_body}
            value={body}
            onChange={(e) => setBody(e.target.value)}
            className="w-full rounded-xl border border-zinc-300 bg-white px-3.5 py-2.5 font-mono text-xs leading-relaxed text-zinc-800 placeholder:text-zinc-400 transition-colors focus:border-transparent focus:outline-none focus:ring-2 focus:ring-primary-500/40"
          />
          <p className="mt-1.5 text-xs text-zinc-400">
            HTML supported. {MERGE_HELP}, plus {"{{period_start}}"},{" "}
            {"{{period_end}}"}, {"{{arrays_count}}"}, {"{{dashboard_url}}"}.
          </p>
        </div>

        {/* Send mode — segmented control */}
        <div>
          <span className="text-sm font-medium text-zinc-700">Send mode</span>
          <div
            role="radiogroup"
            aria-label="Send mode"
            className="mt-2 flex flex-wrap rounded-xl border border-zinc-200 bg-zinc-50 p-1"
          >
            {SEND_MODES.map((m) => {
              const selected = sendMode === m.value;
              return (
                <button
                  key={m.value}
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  onClick={() => setSendMode(m.value)}
                  className={[
                    "rounded-lg px-4 py-1.5 text-sm font-medium transition-colors",
                    "focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40",
                    selected
                      ? "bg-white text-zinc-900 shadow-sm"
                      : "text-zinc-500 hover:text-zinc-800",
                  ].join(" ")}
                >
                  {m.label}
                </button>
              );
            })}
          </div>
          <p className="mt-2 text-xs leading-relaxed text-zinc-400">
            {sendMode === "to_me"
              ? "Reports come to your inbox (with the client's name in the subject and body) so you can review and forward them yourself."
              : "Reports go straight to each client's contact email."}
          </p>
        </div>
      </div>

      {/* Actions */}
      <div className="mt-6 flex flex-wrap items-center gap-3">
        <Button
          variant="secondary"
          onClick={doPreview}
          disabled={previewing || saving}
        >
          {previewing ? (
            <>
              <Spinner />
              Rendering…
            </>
          ) : (
            "Preview"
          )}
        </Button>
        <Button onClick={() => persist(currentInput(), "Email settings saved")} disabled={saving}>
          {saving ? (
            <>
              <Spinner />
              Saving…
            </>
          ) : (
            "Save"
          )}
        </Button>
        <button
          type="button"
          onClick={resetToDefaults}
          disabled={saving}
          className="text-sm font-medium text-zinc-500 underline-offset-2 hover:text-zinc-800 hover:underline focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
        >
          Reset to defaults
        </button>
      </div>

      {/* Preview modal */}
      <Modal
        open={preview !== null}
        onClose={() => {
          if (!saving) setPreview(null);
        }}
        title="Email preview"
        footer={
          <>
            <Button
              variant="secondary"
              onClick={() => setPreview(null)}
              disabled={saving}
            >
              Keep editing
            </Button>
            <Button onClick={saveFromModal} disabled={saving}>
              {saving ? (
                <>
                  <Spinner />
                  Saving…
                </>
              ) : (
                "Looks good — save"
              )}
            </Button>
          </>
        }
      >
        {preview && (
          <div className="space-y-3">
            <div className="rounded-lg bg-zinc-50 p-3 text-xs text-zinc-600">
              <div>
                <span className="font-semibold text-zinc-700">From:</span>{" "}
                {preview.from}
              </div>
              <div className="mt-1">
                <span className="font-semibold text-zinc-700">To:</span>{" "}
                {preview.to}
              </div>
              <div className="mt-1">
                <span className="font-semibold text-zinc-700">Subject:</span>{" "}
                {preview.subject}
              </div>
            </div>
            <div
              className="max-h-80 overflow-auto rounded-lg border border-zinc-200 bg-white p-4 text-sm leading-relaxed text-zinc-800"
              // Preview only — content is the tenant's own template rendered server-side.
              dangerouslySetInnerHTML={{ __html: preview.html }}
            />
            <p className="text-xs text-zinc-400">
              Sample uses {"{{client_name}}"} = Bruce Genereaux,{" "}
              {"{{quarter}}"} = 2026 Q2, {"{{arrays_count}}"} = 3.
            </p>
          </div>
        )}
      </Modal>
    </Card>
  );
}
