import { useEffect, useRef, useState } from "react";
import { Button } from "../../ui/Button";
import { Spinner } from "../../ui/Spinner";
import { useToast } from "../../ui/Toast";
import {
  type ChatMessage,
  type EmailTemplateData,
  getEmailTemplate,
  previewEmailTemplate,
  saveEmailTemplate,
  saveEmailSignoff,
  resetEmailTemplate,
  testSendEmailTemplate,
  chatEmailTemplate,
} from "../../lib/api";

interface Props {
  open: boolean;
  onClose: () => void;
}

const STARTER_CHIPS = [
  "Make it warmer and more personal",
  "Shorter — just the headline numbers",
  "Add a 'questions? reply here' line",
  "Emphasize NEPOOL compliance for their auditor",
];

const TOKEN_CHIPS = [
  "{{client_name}}",
  "{{quarter}}",
  "{{period_start}}",
  "{{period_end}}",
  "{{tenant_name}}",
];

const SIGNOFF_STARTER_CHIPS: { label: string; value: string }[] = [
  {
    label: "Just my name",
    value: "<p>Thank you,<br>{{tenant_name}}</p>",
  },
  {
    label: "Name + email",
    value: "<p>Thank you,<br>{{tenant_name}}<br>{{tenant_email}}</p>",
  },
  {
    label: "Full signature",
    value:
      "<p>Thank you,<br><strong>{{tenant_name}}</strong><br>Solar consultant<br>{{tenant_email}}</p>",
  },
];

export function EmailTemplateStudio({ open, onClose }: Props) {
  const toast = useToast();

  const [templateData, setTemplateData] = useState<EmailTemplateData | null>(null);
  const [loadingTemplate, setLoadingTemplate] = useState(false);

  // Editable drafts
  const [subjectDraft, setSubjectDraft] = useState("");
  const [bodyDraft, setBodyDraft] = useState("");
  const [signoffDraft, setSignoffDraft] = useState("");
  const [isDirty, setIsDirty] = useState(false);
  const [signoffDirty, setSignoffDirty] = useState(false);

  // Preview state
  const [previewSubject, setPreviewSubject] = useState("");
  const [previewBody, setPreviewBody] = useState("");
  const [sampleClient, setSampleClient] = useState("");
  const [previewLoading, setPreviewLoading] = useState(false);
  const [aiGenerated, setAiGenerated] = useState(false);

  // Chat state
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [chatInput, setChatInput] = useState("");
  const [chatLoading, setChatLoading] = useState(false);

  // Action states
  const [saving, setSaving] = useState(false);
  const [savingSignoff, setSavingSignoff] = useState(false);
  const [testing, setTesting] = useState(false);
  const [resetting, setResetting] = useState(false);

  // Debounce timer for body edits → preview
  const previewDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const subjectInputRef = useRef<HTMLInputElement>(null);
  const chatBottomRef = useRef<HTMLDivElement>(null);

  // Load template on open
  useEffect(() => {
    if (!open) return;
    setLoadingTemplate(true);
    setMessages([]);
    setAiGenerated(false);
    getEmailTemplate()
      .then((data) => {
        setTemplateData(data);
        setSubjectDraft(data.subject_template);
        setBodyDraft(data.body_template);
        setSignoffDraft(data.signoff);
        setIsDirty(false);
        setSignoffDirty(false);
        return refreshPreview(data.subject_template, data.body_template, data.signoff);
      })
      .catch(() => toast.error("Couldn't load email template"))
      .finally(() => setLoadingTemplate(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Close on Escape
  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // Scroll chat to bottom on new messages
  useEffect(() => {
    chatBottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function refreshPreview(subject?: string, body?: string, signoff?: string) {
    setPreviewLoading(true);
    try {
      const result = await previewEmailTemplate({
        subject_template: subject ?? subjectDraft,
        body_template: body ?? bodyDraft,
        signoff: signoff ?? signoffDraft,
      });
      setPreviewSubject(result.subject_rendered);
      setPreviewBody(result.body_rendered);
      setSampleClient(result.sample_client);
    } catch {
      // Silently fail — preview is non-critical
    } finally {
      setPreviewLoading(false);
    }
  }

  function schedulePreviewRefresh(subject?: string, body?: string, signoff?: string) {
    if (previewDebounceRef.current) clearTimeout(previewDebounceRef.current);
    previewDebounceRef.current = setTimeout(
      () => void refreshPreview(subject, body, signoff),
      300,
    );
  }

  async function handleChatSubmit(instruction?: string) {
    const text = (instruction ?? chatInput).trim();
    if (!text || chatLoading) return;

    const userMsg: ChatMessage = { role: "user", content: text };
    const newMessages = [...messages, userMsg];
    setMessages(newMessages);
    setChatInput("");
    setChatLoading(true);

    try {
      const result = await chatEmailTemplate({
        messages: newMessages,
        current_body: bodyDraft,
        current_subject: subjectDraft,
      });

      const assistantMsg: ChatMessage = {
        role: "assistant",
        content: result.assistant_reply,
      };
      setMessages([...newMessages, assistantMsg]);

      const newBody = result.proposed_body || bodyDraft;
      const newSubject =
        result.proposed_subject != null ? result.proposed_subject : subjectDraft;

      setBodyDraft(newBody);
      setSubjectDraft(newSubject);
      setIsDirty(true);
      setAiGenerated(true);

      await refreshPreview(newSubject, newBody, signoffDraft);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "AI request failed");
    } finally {
      setChatLoading(false);
    }
  }

  function insertToken(token: string) {
    const input = subjectInputRef.current;
    if (!input) {
      setSubjectDraft((s) => s + token);
      setIsDirty(true);
      return;
    }
    const start = input.selectionStart ?? subjectDraft.length;
    const end = input.selectionEnd ?? subjectDraft.length;
    const next =
      subjectDraft.slice(0, start) + token + subjectDraft.slice(end);
    setSubjectDraft(next);
    setIsDirty(true);
    setTimeout(() => {
      input.setSelectionRange(start + token.length, start + token.length);
      input.focus();
    }, 0);
  }

  async function handleSave() {
    setSaving(true);
    try {
      await saveEmailTemplate({
        subject_template: subjectDraft || null,
        body_template: bodyDraft || null,
      });
      setIsDirty(false);
      toast.success("Email template saved as your default.");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  async function handleSaveSignoff() {
    setSavingSignoff(true);
    try {
      await saveEmailSignoff(signoffDraft || null);
      setSignoffDirty(false);
      toast.success("Sign-off saved.");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSavingSignoff(false);
    }
  }

  async function handleResetSignoff() {
    if (!templateData) return;
    setSignoffDraft(templateData.signoff);
    setSignoffDirty(false);
    await refreshPreview(subjectDraft, bodyDraft, templateData.signoff);
  }

  async function handleTestSend() {
    setTesting(true);
    try {
      const r = await testSendEmailTemplate({
        subject_template: subjectDraft || null,
        body_template: bodyDraft || null,
        signoff: signoffDraft || null,
      });
      toast.success(`Test email sent to ${r.sent_to}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Test send failed");
    } finally {
      setTesting(false);
    }
  }

  async function handleReset() {
    setResetting(true);
    try {
      await resetEmailTemplate();
      const data = await getEmailTemplate();
      setTemplateData(data);
      setSubjectDraft(data.subject_template);
      setBodyDraft(data.body_template);
      setSignoffDraft(data.signoff);
      setIsDirty(false);
      setSignoffDirty(false);
      setAiGenerated(false);
      await refreshPreview(data.subject_template, data.body_template, data.signoff);
      toast.success("Template reset to system default.");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Reset failed");
    } finally {
      setResetting(false);
    }
  }

  if (!open) return null;

  const noClientEmail = templateData && !templateData.has_client_with_email;

  // C4: "looks great" CTA is shown when all three are still default and operator hasn't
  // made any in-session edits.
  const isAllDefault =
    templateData != null &&
    templateData.is_default_subject &&
    templateData.is_default_body &&
    templateData.is_default_signoff &&
    !isDirty &&
    !signoffDirty;

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-[#faf8f5]">
      {/* ── Header ── */}
      <div className="flex h-14 shrink-0 items-center justify-between border-b border-cream-border bg-white px-6 shadow-sm">
        <div className="flex items-center gap-2">
          <span className="text-base font-semibold text-zinc-900">
            Customize report email
          </span>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close template studio"
          className="flex h-8 w-8 items-center justify-center rounded-full text-zinc-400 transition-colors hover:bg-zinc-100 hover:text-zinc-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
        >
          <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden>
            <path
              d="M3 3 L13 13 M13 3 L3 13"
              stroke="currentColor"
              strokeWidth="1.75"
              strokeLinecap="round"
            />
          </svg>
        </button>
      </div>

      {/* ── Body ── */}
      {loadingTemplate ? (
        <div className="flex flex-1 items-center justify-center">
          <Spinner className="h-6 w-6 text-zinc-400" />
        </div>
      ) : noClientEmail ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 text-center">
          <p className="text-2xl">✉️</p>
          <p className="text-sm font-medium text-zinc-700">
            Add a client with an email first
          </p>
          <p className="max-w-xs text-xs text-zinc-500">
            The template studio previews your email with real client data. Add a
            client email address in the Clients tab, then come back here.
          </p>
          <button
            type="button"
            onClick={onClose}
            className="mt-2 text-xs font-medium text-primary-600 underline underline-offset-2 hover:text-primary-700"
          >
            Close
          </button>
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 overflow-hidden">
          {/* ── Left: AI chat (40%) ── */}
          <div className="flex w-[40%] flex-col border-r border-cream-border bg-white">
            <div className="border-b border-zinc-100 px-5 py-3">
              <p className="text-xs font-semibold uppercase tracking-wide text-zinc-400">
                AI Assistant
              </p>
            </div>

            {/* Messages */}
            <div className="flex-1 overflow-y-auto space-y-3 p-4">
              {messages.length === 0 && (
                <p className="text-xs text-zinc-400">
                  Describe how you'd like to customize your email template.
                </p>
              )}
              {messages.map((m, i) => (
                <div
                  key={i}
                  className={[
                    "max-w-[88%] rounded-xl px-3 py-2 text-sm",
                    m.role === "user"
                      ? "ml-auto bg-zinc-100 text-zinc-900"
                      : "mr-auto border border-zinc-200 bg-white text-zinc-800 shadow-sm",
                  ].join(" ")}
                >
                  {m.content}
                </div>
              ))}
              {chatLoading && (
                <div className="mr-auto flex items-center gap-1.5 rounded-xl border border-zinc-200 bg-white px-3 py-2 text-xs text-zinc-500 shadow-sm">
                  <Spinner className="h-3 w-3" />
                  Drafting…
                </div>
              )}
              <div ref={chatBottomRef} />
            </div>

            {/* Starter chips — only before first message */}
            {messages.length === 0 && (
              <div className="px-4 pb-2">
                <p className="mb-2 text-[11px] font-medium text-zinc-400">
                  Quick starts
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {STARTER_CHIPS.map((chip) => (
                    <button
                      key={chip}
                      type="button"
                      disabled={chatLoading}
                      onClick={() => handleChatSubmit(chip)}
                      className="rounded-full border border-zinc-200 bg-zinc-50 px-2.5 py-1 text-[11px] font-medium text-zinc-600 hover:border-primary-300 hover:bg-primary-50 hover:text-primary-700 disabled:opacity-50"
                    >
                      {chip}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {/* Chat input */}
            <div className="border-t border-zinc-100 p-3">
              <div className="flex gap-2">
                <input
                  type="text"
                  value={chatInput}
                  onChange={(e) => setChatInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      void handleChatSubmit();
                    }
                  }}
                  placeholder="Make it warmer…"
                  disabled={chatLoading}
                  className="flex-1 rounded-xl border border-zinc-200 bg-zinc-50 px-3 py-2 text-sm placeholder:text-zinc-400 focus:border-primary-400 focus:outline-none focus:ring-1 focus:ring-primary-400/30 disabled:opacity-60"
                />
                <Button
                  onClick={() => void handleChatSubmit()}
                  disabled={!chatInput.trim() || chatLoading}
                  className="h-9 px-3 text-xs"
                >
                  Send
                </Button>
              </div>
            </div>
          </div>

          {/* ── Right: editor + preview (60%) ── */}
          <div className="flex w-[60%] flex-col bg-[#faf8f5] overflow-y-auto">
            {/* Preview header */}
            <div className="border-b border-cream-border bg-[#faf8f5] px-5 py-3 sticky top-0 z-10">
              <div className="flex items-center justify-between">
                <p className="text-xs font-semibold uppercase tracking-wide text-zinc-400">
                  Preview
                  {sampleClient && (
                    <span className="ml-2 font-normal normal-case text-zinc-400">
                      — {sampleClient}
                    </span>
                  )}
                  {aiGenerated && (
                    <span className="ml-2 text-primary-600">AI draft</span>
                  )}
                </p>
                <button
                  type="button"
                  onClick={handleReset}
                  disabled={resetting}
                  className="text-[11px] font-medium text-zinc-400 underline underline-offset-2 hover:text-zinc-600 disabled:opacity-50"
                >
                  Reset to default
                </button>
              </div>
            </div>

            <div className="flex-1 p-5 space-y-4">
              {/* Token chips for subject */}
              <div>
                <p className="mb-1.5 text-[11px] font-medium text-zinc-400">
                  Insert token into subject
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {TOKEN_CHIPS.map((t) => (
                    <button
                      key={t}
                      type="button"
                      onClick={() => insertToken(t)}
                      className="rounded-full border border-zinc-200 bg-white px-2.5 py-0.5 font-mono text-[11px] text-zinc-500 hover:border-primary-300 hover:bg-primary-50 hover:text-primary-700"
                    >
                      {t}
                    </button>
                  ))}
                </div>
              </div>

              {/* Subject line */}
              <div>
                <label className="mb-1 block text-[11px] font-medium text-zinc-500">
                  Subject line
                </label>
                <input
                  ref={subjectInputRef}
                  type="text"
                  value={subjectDraft}
                  onChange={(e) => {
                    setSubjectDraft(e.target.value);
                    setIsDirty(true);
                  }}
                  onBlur={() => void refreshPreview()}
                  className="w-full rounded-xl border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-900 focus:border-primary-400 focus:outline-none focus:ring-1 focus:ring-primary-400/30"
                />
              </div>

              {/* Email preview box */}
              <div className="rounded-xl border border-zinc-200 bg-white shadow-sm overflow-hidden">
                {/* From → To row */}
                {templateData && (
                  <div className="border-b border-zinc-100 px-4 py-2.5 bg-zinc-50 space-y-0.5">
                    <p className="text-[11px] text-zinc-500">
                      <span className="font-medium text-zinc-600">From:</span>{" "}
                      {templateData.from_email ?? "admin@solaroperator.org"}
                      <span className="ml-1 text-zinc-400">(replies go here)</span>
                    </p>
                    <p className="text-[11px] text-zinc-500">
                      <span className="font-medium text-zinc-600">To:</span>{" "}
                      {templateData.sample_client_email ?? sampleClient}
                      {sampleClient && (
                        <span className="ml-1 text-zinc-400">(sample client)</span>
                      )}
                    </p>
                  </div>
                )}
                {/* Subject header */}
                <div className="border-b border-zinc-100 px-4 py-3">
                  <p className="text-[11px] font-medium uppercase tracking-wide text-zinc-400">
                    Subject
                  </p>
                  {previewLoading ? (
                    <div className="mt-1 h-4 w-64 animate-pulse rounded bg-zinc-100" />
                  ) : (
                    <p className="mt-0.5 text-sm font-medium text-zinc-900">
                      {previewSubject || "(preview will appear here)"}
                    </p>
                  )}
                </div>
                {/* Body */}
                <div className="px-4 py-4">
                  {previewLoading ? (
                    <div className="space-y-2">
                      <div className="h-3 w-full animate-pulse rounded bg-zinc-100" />
                      <div className="h-3 w-5/6 animate-pulse rounded bg-zinc-100" />
                      <div className="h-3 w-4/6 animate-pulse rounded bg-zinc-100" />
                    </div>
                  ) : previewBody ? (
                    <div
                      className="text-sm leading-relaxed text-zinc-800 [&_a]:text-primary-600 [&_a]:underline [&_p]:mb-3 [&_p:last-child]:mb-0"
                      dangerouslySetInnerHTML={{ __html: previewBody }}
                    />
                  ) : (
                    <p className="text-sm text-zinc-400">Preview will appear here.</p>
                  )}
                </div>
              </div>

              {/* Body editor — editable HTML textarea */}
              <div>
                <label className="mb-1 block text-[11px] font-medium text-zinc-500">
                  Body (HTML)
                </label>
                <textarea
                  value={bodyDraft}
                  onChange={(e) => {
                    setBodyDraft(e.target.value);
                    setIsDirty(true);
                    schedulePreviewRefresh(subjectDraft, e.target.value, signoffDraft);
                  }}
                  rows={8}
                  className="w-full rounded-xl border border-zinc-200 bg-white px-3 py-2 font-mono text-xs text-zinc-800 focus:border-primary-400 focus:outline-none focus:ring-1 focus:ring-primary-400/30"
                />
                <p className="mt-1 text-[11px] text-zinc-400">
                  HTML allowed. Tokens like{" "}
                  <span className="font-mono">{"{{client_name}}"}</span>,{" "}
                  <span className="font-mono">{"{{quarter}}"}</span>,{" "}
                  <span className="font-mono">{"{{signoff}}"}</span> are inserted automatically.
                </p>
              </div>

              {/* ── Sign-off section (C3) ── */}
              <div className="rounded-xl border border-zinc-200 bg-white p-4 shadow-sm space-y-3">
                <div>
                  <p className="text-sm font-semibold text-zinc-800">Sign-off</p>
                  <p className="text-[11px] text-zinc-500 mt-0.5">
                    Appears at the bottom of every report email. Paste your name, title,
                    phone, and anything else here.
                  </p>
                </div>

                {/* Starter chips */}
                <div className="flex flex-wrap gap-1.5">
                  {SIGNOFF_STARTER_CHIPS.map((chip) => (
                    <button
                      key={chip.label}
                      type="button"
                      onClick={() => {
                        setSignoffDraft(chip.value);
                        setSignoffDirty(true);
                        schedulePreviewRefresh(subjectDraft, bodyDraft, chip.value);
                      }}
                      className="rounded-full border border-zinc-200 bg-zinc-50 px-2.5 py-1 text-[11px] font-medium text-zinc-600 hover:border-primary-300 hover:bg-primary-50 hover:text-primary-700"
                    >
                      {chip.label}
                    </button>
                  ))}
                </div>

                {/* Signoff textarea */}
                <textarea
                  value={signoffDraft}
                  onChange={(e) => {
                    setSignoffDraft(e.target.value);
                    setSignoffDirty(true);
                    schedulePreviewRefresh(subjectDraft, bodyDraft, e.target.value);
                  }}
                  rows={4}
                  placeholder="Paste your sign-off here…"
                  className="w-full rounded-xl border border-zinc-200 bg-zinc-50 px-3 py-2 text-sm text-zinc-800 placeholder:text-zinc-400 focus:border-primary-400 focus:outline-none focus:ring-1 focus:ring-primary-400/30"
                />

                {/* Signoff action buttons */}
                <div className="flex items-center gap-2">
                  <Button
                    onClick={handleSaveSignoff}
                    disabled={savingSignoff}
                    className="text-xs"
                  >
                    {savingSignoff ? (
                      <>
                        <Spinner />
                        Saving…
                      </>
                    ) : (
                      "Save sign-off"
                    )}
                  </Button>
                  <button
                    type="button"
                    onClick={() => void handleResetSignoff()}
                    className="text-[11px] font-medium text-zinc-400 underline underline-offset-2 hover:text-zinc-600"
                  >
                    Reset to default sign-off
                  </button>
                </div>
              </div>

              {/* ── C4: "Looks great" CTA ── shown only when all defaults, nothing dirty */}
              {isAllDefault && (
                <div className="rounded-xl border-2 border-emerald-200 bg-emerald-50 px-5 py-4 text-center">
                  <p className="text-sm font-semibold text-emerald-800 mb-3">
                    Looks good as-is — use the default for all my client emails
                  </p>
                  <Button
                    onClick={() => {
                      toast.success(
                        "Using the default template — your clients will get the standard email.",
                      );
                      onClose();
                    }}
                    className="bg-emerald-600 hover:bg-emerald-700 text-xs"
                  >
                    Looks great, use this
                  </Button>
                </div>
              )}
            </div>

            {/* Action bar — shown when anything is customized */}
            {!isAllDefault && (
              <div className="border-t border-cream-border bg-white px-5 py-3 flex items-center justify-between gap-3 sticky bottom-0">
                <Button
                  variant="secondary"
                  onClick={handleTestSend}
                  disabled={testing}
                  className="text-xs"
                >
                  {testing ? (
                    <>
                      <Spinner />
                      Sending…
                    </>
                  ) : (
                    "Send myself a test"
                  )}
                </Button>
                <div className="flex items-center gap-2">
                  {isDirty && (
                    <span className="text-[11px] text-zinc-400">Unsaved draft</span>
                  )}
                  <Button
                    onClick={handleSave}
                    disabled={saving}
                    className="text-xs"
                  >
                    {saving ? (
                      <>
                        <Spinner />
                        Saving…
                      </>
                    ) : (
                      "Save as my default"
                    )}
                  </Button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
