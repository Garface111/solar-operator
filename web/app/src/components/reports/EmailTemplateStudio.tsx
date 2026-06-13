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

// Populate-only chips shown in the sticky banner above the chat input.
// Clicking sets the textarea content; the operator reviews before sending.
const BANNER_CHIPS = [
  "Draft a friendly Q3 summary",
  "Make this warmer",
  "Add a thank-you line",
];

const TOKEN_CHIPS = [
  "{{greeting}}",
  "{{client_first_name}}",
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

type SaveStatus = "saved" | "saving" | "saved-moment" | "error";

export function EmailTemplateStudio({ open, onClose }: Props) {
  const toast = useToast();

  const [templateData, setTemplateData] = useState<EmailTemplateData | null>(null);
  const [loadingTemplate, setLoadingTemplate] = useState(false);

  // Editable drafts
  const [subjectDraft, setSubjectDraft] = useState("");
  const [bodyDraft, setBodyDraft] = useState("");
  const [signoffDraft, setSignoffDraft] = useState("");
  // Tracks whether the user has edited anything this session (for the C4 CTA).
  const [hasUserEdited, setHasUserEdited] = useState(false);

  // Autosave status indicator
  const [saveStatus, setSaveStatus] = useState<SaveStatus>("saved");

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
  const [chatOpen, setChatOpen] = useState(false);
  // First-visit hint: show expanded tip until operator opens AI or dismisses it.
  const [hintShown, setHintShown] = useState(false);

  // Action states
  const [testing, setTesting] = useState(false);
  const [resetting, setResetting] = useState(false);

  // Debounce timer for body edits → preview
  const previewDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Autosave machinery
  const autosaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const savedMomentTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Latest draft values — read by the timer callback so it always sees fresh state.
  const draftRef = useRef({ subject: "", body: "", signoff: "" });
  // Which fields have unsaved changes.
  const dirtyRef = useRef({ subject: false, body: false, signoff: false });
  // Monotonically increasing counter — latest-only pattern avoids stale responses
  // flashing "Saved" over a still-pending change.
  const saveCounterRef = useRef(0);

  const subjectInputRef = useRef<HTMLInputElement>(null);
  const bodyTextareaRef = useRef<HTMLTextAreaElement>(null);
  // Which field a token chip click should target. Updated on focus.
  // Defaults to "subject" so first-click behavior matches the old single-field flow.
  const [tokenTarget, setTokenTarget] = useState<"subject" | "body">("subject");
  const chatBottomRef = useRef<HTMLDivElement>(null);
  const chatInputRef = useRef<HTMLTextAreaElement>(null);

  // Load template on open
  useEffect(() => {
    if (!open) return;
    setLoadingTemplate(true);
    setMessages([]);
    setAiGenerated(false);
    setHintShown(!!sessionStorage.getItem("so:studio:ai-hint-shown"));
    getEmailTemplate()
      .then((data) => {
        setTemplateData(data);
        setSubjectDraft(data.subject_template);
        setBodyDraft(data.body_template);
        setSignoffDraft(data.signoff);
        draftRef.current = {
          subject: data.subject_template,
          body: data.body_template,
          signoff: data.signoff,
        };
        dirtyRef.current = { subject: false, body: false, signoff: false };
        setHasUserEdited(false);
        setSaveStatus("saved");
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

  // Flush any pending autosave when the dialog closes or the component unmounts.
  // Covers Escape-key close (which skips onBlur) and route-change unmount.
  useEffect(() => {
    if (!open) return;
    return () => {
      if (autosaveTimerRef.current) {
        clearTimeout(autosaveTimerRef.current);
        autosaveTimerRef.current = null;
      }
      if (savedMomentTimerRef.current) {
        clearTimeout(savedMomentTimerRef.current);
        savedMomentTimerRef.current = null;
      }
      const { subject, body, signoff } = dirtyRef.current;
      if (subject || body || signoff) {
        const draft = draftRef.current;
        const promises: Promise<unknown>[] = [];
        if (subject || body) {
          promises.push(
            saveEmailTemplate({
              subject_template: draft.subject || null,
              body_template: draft.body || null,
            }),
          );
        }
        if (signoff) {
          promises.push(saveEmailSignoff(draft.signoff || null));
        }
        void Promise.all(promises);
      }
    };
  }, [open]);

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
    } catch (err) {
      // Surface the failure so the operator knows something's wrong instead
      // of staring at the placeholder "Preview will appear here" forever.
      console.error("Preview render failed:", err);
      toast.error(
        err instanceof Error
          ? `Preview render failed: ${err.message}`
          : "Preview render failed — check your template syntax.",
      );
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

  // ── Autosave ──────────────────────────────────────────────────────────────

  async function doSave() {
    const counter = ++saveCounterRef.current;
    // Snapshot + clear dirty bits atomically so keystrokes during the network
    // call re-dirty the ref and get picked up by the next save.
    const dirty = { ...dirtyRef.current };
    dirtyRef.current = { subject: false, body: false, signoff: false };
    const draft = { ...draftRef.current };

    if (!dirty.subject && !dirty.body && !dirty.signoff) return;

    setSaveStatus("saving");

    try {
      const promises: Promise<unknown>[] = [];
      if (dirty.subject || dirty.body) {
        promises.push(
          saveEmailTemplate({
            subject_template: draft.subject || null,
            body_template: draft.body || null,
          }),
        );
      }
      if (dirty.signoff) {
        promises.push(saveEmailSignoff(draft.signoff || null));
      }
      await Promise.all(promises);

      if (counter === saveCounterRef.current) {
        if (savedMomentTimerRef.current) clearTimeout(savedMomentTimerRef.current);
        setSaveStatus("saved-moment");
        savedMomentTimerRef.current = setTimeout(() => {
          setSaveStatus((s) => (s === "saved-moment" ? "saved" : s));
        }, 2000);
      }
    } catch (err) {
      // Restore dirty bits so retry has something to send.
      dirtyRef.current.subject = dirtyRef.current.subject || dirty.subject;
      dirtyRef.current.body = dirtyRef.current.body || dirty.body;
      dirtyRef.current.signoff = dirtyRef.current.signoff || dirty.signoff;

      if (counter === saveCounterRef.current) {
        setSaveStatus("error");
        toast.error(err instanceof Error ? err.message : "Save failed");
      }
    }
  }

  function scheduleSave() {
    if (autosaveTimerRef.current) clearTimeout(autosaveTimerRef.current);
    autosaveTimerRef.current = setTimeout(() => void doSave(), 800);
  }

  function flushSave() {
    if (autosaveTimerRef.current) {
      clearTimeout(autosaveTimerRef.current);
      autosaveTimerRef.current = null;
    }
    void doSave();
  }

  function handleRetry() {
    void doSave();
  }

  // ─────────────────────────────────────────────────────────────────────────

  function handleOpenChat() {
    if (!hintShown) {
      sessionStorage.setItem("so:studio:ai-hint-shown", "1");
      setHintShown(true);
    }
    setChatOpen(true);
    if (messages.length === 0) {
      setTimeout(() => chatInputRef.current?.focus(), 0);
    }
  }

  function dismissHint() {
    sessionStorage.setItem("so:studio:ai-hint-shown", "1");
    setHintShown(true);
  }

  function populateChatInput(text: string) {
    setChatInput(text);
    setTimeout(() => chatInputRef.current?.focus(), 0);
  }

  async function handleChatSubmit(instruction?: string) {
    const text = (instruction ?? chatInput).trim();
    if (!text || chatLoading) return;

    const userMsg: ChatMessage = { role: "user", content: text };
    const newMessages = [...messages, userMsg];
    setMessages(newMessages);
    setChatInput("");
    // Collapse the auto-grown textarea back to one row.
    if (chatInputRef.current) chatInputRef.current.style.height = "auto";
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
      draftRef.current.body = newBody;
      draftRef.current.subject = newSubject;
      dirtyRef.current.body = true;
      dirtyRef.current.subject = true;
      setHasUserEdited(true);
      setAiGenerated(true);
      scheduleSave();

      await refreshPreview(newSubject, newBody, signoffDraft);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "AI request failed");
    } finally {
      setChatLoading(false);
    }
  }

  function insertToken(token: string) {
    if (tokenTarget === "body") {
      const ta = bodyTextareaRef.current;
      if (!ta) {
        const next = bodyDraft + token;
        setBodyDraft(next);
        draftRef.current.body = next;
        dirtyRef.current.body = true;
        setHasUserEdited(true);
        schedulePreviewRefresh(subjectDraft, next, signoffDraft);
        scheduleSave();
        return;
      }
      const start = ta.selectionStart ?? bodyDraft.length;
      const end = ta.selectionEnd ?? bodyDraft.length;
      const next = bodyDraft.slice(0, start) + token + bodyDraft.slice(end);
      setBodyDraft(next);
      draftRef.current.body = next;
      dirtyRef.current.body = true;
      setHasUserEdited(true);
      schedulePreviewRefresh(subjectDraft, next, signoffDraft);
      scheduleSave();
      setTimeout(() => {
        ta.setSelectionRange(start + token.length, start + token.length);
        ta.focus();
      }, 0);
      return;
    }
    const input = subjectInputRef.current;
    if (!input) {
      const next = subjectDraft + token;
      setSubjectDraft(next);
      draftRef.current.subject = next;
      dirtyRef.current.subject = true;
      setHasUserEdited(true);
      scheduleSave();
      return;
    }
    const start = input.selectionStart ?? subjectDraft.length;
    const end = input.selectionEnd ?? subjectDraft.length;
    const next =
      subjectDraft.slice(0, start) + token + subjectDraft.slice(end);
    setSubjectDraft(next);
    draftRef.current.subject = next;
    dirtyRef.current.subject = true;
    setHasUserEdited(true);
    scheduleSave();
    setTimeout(() => {
      input.setSelectionRange(start + token.length, start + token.length);
      input.focus();
    }, 0);
  }

  async function handleResetSignoff() {
    if (!templateData) return;
    const defaultSignoff = templateData.signoff;
    setSignoffDraft(defaultSignoff);
    draftRef.current.signoff = defaultSignoff;
    dirtyRef.current.signoff = true;
    schedulePreviewRefresh(subjectDraft, bodyDraft, defaultSignoff);
    flushSave();
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
      // Cancel any pending autosave before resetting
      if (autosaveTimerRef.current) {
        clearTimeout(autosaveTimerRef.current);
        autosaveTimerRef.current = null;
      }
      await resetEmailTemplate();
      const data = await getEmailTemplate();
      setTemplateData(data);
      setSubjectDraft(data.subject_template);
      setBodyDraft(data.body_template);
      setSignoffDraft(data.signoff);
      draftRef.current = {
        subject: data.subject_template,
        body: data.body_template,
        signoff: data.signoff,
      };
      dirtyRef.current = { subject: false, body: false, signoff: false };
      setHasUserEdited(false);
      setSaveStatus("saved");
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
    !hasUserEdited;

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
        <div className="relative flex min-h-0 flex-1 overflow-hidden">
          {/* ── Editor + preview (full width — chat is now floating) ── */}
          <div className="flex flex-1 flex-col bg-[#faf8f5] overflow-hidden">
            {/* Preview header */}
            <div className="border-b border-cream-border bg-[#faf8f5] px-5 py-3">
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

            {/* Two-column body: PREVIEW left (58%), EDITOR right (42%) */}
            <div className="flex flex-1 min-h-0 overflow-hidden">
              {/* ── LEFT: live email preview (what your client sees) ── */}
              <div className="w-[58%] overflow-y-auto p-5 border-r border-cream-border">
                {/* Email preview box — styled like a real inbox message */}
                <div className="rounded-xl border border-zinc-200 bg-white shadow-sm overflow-hidden sticky top-0">
                {/* Inbox toolbar — visual fidelity cue that this is an email */}
                <div className="flex items-center gap-2 border-b border-zinc-100 bg-zinc-50 px-4 py-2">
                  <div className="flex gap-1.5">
                    <span className="h-2.5 w-2.5 rounded-full bg-red-300" />
                    <span className="h-2.5 w-2.5 rounded-full bg-amber-300" />
                    <span className="h-2.5 w-2.5 rounded-full bg-emerald-300" />
                  </div>
                  <span className="ml-2 text-[10px] uppercase tracking-wider text-zinc-400">
                    Inbox preview
                  </span>
                </div>

                {/* Subject line — top of message like in a mail client */}
                <div className="border-b border-zinc-100 px-5 pt-4 pb-3">
                  {previewLoading ? (
                    <div className="h-5 w-3/4 animate-pulse rounded bg-zinc-100" />
                  ) : (
                    <h3 className="text-lg font-semibold tracking-tight text-zinc-900">
                      {previewSubject || "(subject will appear here)"}
                    </h3>
                  )}
                </div>

                {/* From / To header — avatar + names like Gmail */}
                {templateData && (
                  <div className="flex items-start gap-3 border-b border-zinc-100 px-5 py-3">
                    <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-primary-100 text-sm font-semibold text-primary-700">
                      {initialsOf(templateData.from_email ?? "SO")}
                    </div>
                    <div className="min-w-0 flex-1 space-y-0.5">
                      <p className="truncate text-sm text-zinc-900">
                        <span className="font-medium">
                          {templateData.from_email ?? "admin@solaroperator.org"}
                        </span>
                        <span className="ml-1.5 text-xs text-zinc-400">
                          via NEPOOL Operator
                        </span>
                      </p>
                      <p className="truncate text-xs text-zinc-500">
                        to{" "}
                        <span className="text-zinc-700">
                          {templateData.sample_client_email ?? sampleClient ?? "your client"}
                        </span>
                        {sampleClient && (
                          <span className="ml-1 text-zinc-400">· sample client</span>
                        )}
                      </p>
                    </div>
                    <span className="shrink-0 text-[11px] text-zinc-400">
                      just now
                    </span>
                  </div>
                )}

                {/* Body — rendered HTML email content */}
                <div className="bg-white px-6 py-6">
                  {previewLoading ? (
                    <div className="space-y-2.5">
                      <div className="h-3 w-full animate-pulse rounded bg-zinc-100" />
                      <div className="h-3 w-5/6 animate-pulse rounded bg-zinc-100" />
                      <div className="h-3 w-4/6 animate-pulse rounded bg-zinc-100" />
                      <div className="h-3 w-3/4 animate-pulse rounded bg-zinc-100 mt-4" />
                    </div>
                  ) : previewBody ? (
                    <div
                      className="text-[15px] leading-[1.65] text-zinc-800 font-serif [&_a]:text-primary-600 [&_a]:underline [&_p]:mb-4 [&_p:last-child]:mb-0 [&_strong]:font-semibold [&_em]:italic"
                      style={{ fontFamily: "Georgia, 'Times New Roman', ui-serif, serif" }}
                      dangerouslySetInnerHTML={{ __html: previewBody }}
                    />
                  ) : (
                    <p className="text-sm text-zinc-400">Preview will appear here.</p>
                  )}
                </div>

                {/* Attachment chip — the GMCS workbook that actually goes out */}
                <div className="border-t border-zinc-100 bg-zinc-50 px-5 py-3">
                  <div className="inline-flex items-center gap-2 rounded-lg border border-zinc-200 bg-white px-3 py-1.5 text-xs">
                    <svg viewBox="0 0 16 16" width="14" height="14" className="text-emerald-600" fill="currentColor" aria-hidden>
                      <path d="M3 1.5A1.5 1.5 0 0 1 4.5 0h5.379a1.5 1.5 0 0 1 1.06.44l2.122 2.12A1.5 1.5 0 0 1 13.5 3.62V14.5A1.5 1.5 0 0 1 12 16H4.5A1.5 1.5 0 0 1 3 14.5v-13Z" opacity="0.2" />
                      <path d="M9.5 0v3a1 1 0 0 0 1 1h3" stroke="currentColor" strokeWidth="0.8" fill="none" />
                      <text x="8" y="11.5" textAnchor="middle" fontSize="3.5" fontWeight="bold" fill="currentColor">XLSX</text>
                    </svg>
                    <span className="font-medium text-zinc-700">
                      {sampleClient ? `${sampleClient.split(" ")[0]}-Q-Report.xlsx` : "Q-Report.xlsx"}
                    </span>
                    <span className="text-zinc-400">· NEPOOL-format workbook</span>
                  </div>
                </div>
                </div> {/* /preview card */}
              </div> {/* /LEFT column */}

              {/* ── RIGHT: editor (subject + body + signoff) ── */}
              <div className="w-[42%] overflow-y-auto p-5 space-y-4">
                {/* Token chips for subject */}
                <div>
                  <p className="mb-1.5 text-[11px] font-medium text-zinc-400">
                    Insert token into {tokenTarget === "body" ? "body" : "subject"} — click a field, then a chip
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
                      draftRef.current.subject = e.target.value;
                      dirtyRef.current.subject = true;
                      setHasUserEdited(true);
                      scheduleSave();
                    }}
                    onFocus={() => setTokenTarget("subject")}
                    onBlur={() => {
                      void refreshPreview();
                      flushSave();
                    }}
                    className="w-full rounded-xl border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-900 focus:border-primary-400 focus:outline-none focus:ring-1 focus:ring-primary-400/30"
                  />
                </div>

              {/* Body editor — editable HTML textarea */}
              <div>
                <label className="mb-1 block text-[11px] font-medium text-zinc-500">
                  Body (HTML)
                </label>
                <textarea
                  ref={bodyTextareaRef}
                  value={bodyDraft}
                  onChange={(e) => {
                    setBodyDraft(e.target.value);
                    draftRef.current.body = e.target.value;
                    dirtyRef.current.body = true;
                    setHasUserEdited(true);
                    schedulePreviewRefresh(subjectDraft, e.target.value, signoffDraft);
                    scheduleSave();
                  }}
                  onFocus={() => setTokenTarget("body")}
                  onBlur={() => flushSave()}
                  rows={8}
                  className="w-full rounded-xl border border-zinc-200 bg-white px-3 py-2 font-mono text-xs text-zinc-800 focus:border-primary-400 focus:outline-none focus:ring-1 focus:ring-primary-400/30"
                />
                <p className="mt-1 text-[11px] text-zinc-400">
                  HTML allowed.{" "}
                  <span className="font-mono">{"{{greeting}}"}</span>{" "}
                  <span className="text-zinc-300">(auto-picks Hi/Dear based on client name)</span>,{" "}
                  <span className="font-mono">{"{{client_first_name}}"}</span>{" "}
                  <span className="text-zinc-300">(e.g. Bruce)</span>,{" "}
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
                        draftRef.current.signoff = chip.value;
                        dirtyRef.current.signoff = true;
                        setHasUserEdited(true);
                        schedulePreviewRefresh(subjectDraft, bodyDraft, chip.value);
                        scheduleSave();
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
                    draftRef.current.signoff = e.target.value;
                    dirtyRef.current.signoff = true;
                    setHasUserEdited(true);
                    schedulePreviewRefresh(subjectDraft, bodyDraft, e.target.value);
                    scheduleSave();
                  }}
                  onBlur={() => flushSave()}
                  rows={4}
                  placeholder="Paste your sign-off here…"
                  className="w-full rounded-xl border border-zinc-200 bg-zinc-50 px-3 py-2 text-sm text-zinc-800 placeholder:text-zinc-400 focus:border-primary-400 focus:outline-none focus:ring-1 focus:ring-primary-400/30"
                />

                {/* Signoff name tip */}
                <p className="text-[11px] text-zinc-400">
                  Tip — the{" "}
                  <span className="font-mono">{"{{tenant_name}}"}</span>{" "}
                  variable in your sign-off uses your{" "}
                  <strong className="font-medium text-zinc-500">Sign-as name</strong>{" "}
                  from Master Account if set, otherwise your account name. Update it
                  under <strong className="font-medium text-zinc-500">Master Account → Sign-as name</strong>.
                </p>

                <button
                  type="button"
                  onClick={() => void handleResetSignoff()}
                  className="text-[11px] font-medium text-zinc-400 underline underline-offset-2 hover:text-zinc-600"
                >
                  Reset to default sign-off
                </button>
              </div>

              {/* ── C4: "Looks great" CTA ── shown only when all defaults, nothing dirty */}
              {isAllDefault && (
                <div className="rounded-xl border-2 border-emerald-200 bg-emerald-50 px-5 py-4 text-center">
                  <p className="text-sm font-semibold text-emerald-600 mb-3">
                    Looks good as-is — use the default for all my client emails
                  </p>
                  <Button
                    onClick={() => {
                      toast.success(
                        "Using the default template — your clients will get the standard email.",
                      );
                      onClose();
                    }}
                    className="bg-emerald-600 hover:bg-emerald-600 text-xs"
                  >
                    Looks great, use this
                  </Button>
                </div>
              )}
              </div> {/* /RIGHT column */}
            </div> {/* /two-column body */}

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
                <SaveStatusIndicator status={saveStatus} onRetry={handleRetry} />
              </div>
            )}
          </div>

          {/* ── Floating AI assistant — middle-RIGHT bubble / panel ──
              Vertically centered on the right edge so it never sits on top
              of the right column's "Send myself a test" / "Save sign-off" /
              "Save as my default" buttons at the bottom (Ford's explicit
              no-overlap rule, June 5). */}
          {!chatOpen && (
            <div className="absolute right-5 top-1/2 -translate-y-1/2 z-20 flex flex-col items-end gap-2">
              {/* First-visit hint card — dismissed on open or explicit ✕ */}
              {!hintShown && (
                <div className="w-60 rounded-xl border border-primary-200 bg-primary-50 px-4 py-2.5 shadow-md">
                  <div className="flex items-start gap-2">
                    <span className="flex-1 text-xs font-medium text-primary-800 leading-snug">
                      Tip — let AI tune this
                      {sampleClient ? (
                        <> for <strong className="font-semibold">{sampleClient}</strong></>
                      ) : null}{" "}
                      before you send
                    </span>
                    <button
                      type="button"
                      onClick={dismissHint}
                      aria-label="Dismiss AI tip"
                      className="mt-0.5 shrink-0 text-primary-300 hover:text-primary-600 transition-colors"
                    >
                      <svg viewBox="0 0 16 16" width="10" height="10" aria-hidden>
                        <path d="M3 3 L13 13 M13 3 L3 13" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                      </svg>
                    </button>
                  </div>
                </div>
              )}

              {/* The pill — larger padding, larger text, ring glow, bobbing arrow */}
              <button
                type="button"
                onClick={handleOpenChat}
                className="flex items-center gap-2.5 rounded-full bg-primary-600 px-5 py-3 text-base font-medium text-white shadow-lg ring-2 ring-primary-200 hover:bg-primary-700 transition-colors"
                aria-label="Open AI assistant"
              >
                <span className="text-lg leading-none">✦</span>
                <span>Ask AI</span>
                {messages.length > 0 && (
                  <span className="rounded-full bg-white/25 px-1.5 py-0.5 text-[10px]">
                    {messages.length}
                  </span>
                )}
              </button>
            </div>
          )}
          {chatOpen && (
            <div className="absolute right-5 top-1/2 -translate-y-1/2 z-20 flex h-[440px] w-[340px] flex-col rounded-2xl border border-zinc-200 bg-white shadow-2xl overflow-hidden">
              {/* Floating header */}
              <div className="flex items-center justify-between border-b border-zinc-100 bg-zinc-50 px-3 py-2">
                <p className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
                  AI assistant
                </p>
                <button
                  type="button"
                  onClick={() => setChatOpen(false)}
                  aria-label="Close AI assistant"
                  className="flex h-6 w-6 items-center justify-center rounded-full text-zinc-400 hover:bg-zinc-100 hover:text-zinc-700"
                >
                  <svg viewBox="0 0 16 16" width="11" height="11" aria-hidden>
                    <path d="M3 3 L13 13 M13 3 L3 13" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" />
                  </svg>
                </button>
              </div>

              {/* Messages */}
              <div className="flex-1 overflow-y-auto space-y-2 p-3">
                {messages.length === 0 && (
                  <p className="text-[11px] text-zinc-400">
                    Describe how you'd like to customize below ↓
                  </p>
                )}
                {messages.map((m, i) => (
                  <div
                    key={i}
                    className={[
                      "max-w-[88%] rounded-xl px-2.5 py-1.5 text-xs",
                      m.role === "user"
                        ? "ml-auto bg-zinc-100 text-zinc-900"
                        : "mr-auto border border-zinc-200 bg-white text-zinc-800 shadow-sm",
                    ].join(" ")}
                  >
                    {m.content}
                  </div>
                ))}
                {chatLoading && (
                  <div className="mr-auto flex items-center gap-1.5 rounded-xl border border-zinc-200 bg-white px-2.5 py-1.5 text-[11px] text-zinc-500 shadow-sm">
                    <Spinner className="h-3 w-3" />
                    Drafting…
                  </div>
                )}
                <div ref={chatBottomRef} />
              </div>

              {/* ── Ask AI sticky banner — pinned above chat input ── */}
              <div className="border-t border-primary-100 bg-gradient-to-b from-primary-50/70 to-primary-50 px-3 py-2.5">
                {messages.length === 0 ? (
                  <>
                    <p className="mb-2 text-[11px] font-semibold text-primary-700">
                      ✦ Ask AI to draft something — try:
                    </p>
                    <div className="flex flex-wrap gap-1">
                      {BANNER_CHIPS.map((chip) => (
                        <button
                          key={chip}
                          type="button"
                          disabled={chatLoading}
                          onClick={() => populateChatInput(chip)}
                          className="rounded-full border border-primary-200 bg-white px-2.5 py-1 text-[10px] font-medium text-primary-700 hover:border-primary-400 hover:bg-primary-50 disabled:opacity-50 transition-colors"
                        >
                          {chip}
                        </button>
                      ))}
                    </div>
                  </>
                ) : (
                  <div className="flex items-center justify-center gap-1 text-[11px] font-medium text-primary-600">
                    <span>Ask AI</span>
                  </div>
                )}
              </div>

              {/* Chat input */}
              <div className="border-t border-zinc-100 p-2">
                <div className="flex items-end gap-1.5">
                  <textarea
                    ref={chatInputRef}
                    value={chatInput}
                    onChange={(e) => {
                      setChatInput(e.target.value);
                      // Auto-grow: reset height then size to scrollHeight, capped.
                      const ta = e.currentTarget;
                      ta.style.height = "auto";
                      ta.style.height = `${Math.min(ta.scrollHeight, 160)}px`;
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault();
                        void handleChatSubmit();
                      }
                    }}
                    placeholder="Make it warmer…"
                    disabled={chatLoading}
                    rows={1}
                    className={[
                      "flex-1 resize-none rounded-lg border bg-zinc-50 px-2.5 py-1.5 text-xs placeholder:text-zinc-400 focus:border-primary-400 focus:outline-none focus:ring-1 focus:ring-primary-400/30 disabled:opacity-60 leading-5 max-h-40 overflow-y-auto",
                      messages.length === 0
                        ? "border-primary-300 shadow-[0_0_0_3px_rgba(46,107,58,0.12)]"
                        : "border-zinc-200",
                    ].join(" ")}
                  />
                  <Button
                    onClick={() => void handleChatSubmit()}
                    disabled={!chatInput.trim() || chatLoading}
                    className="h-7 px-2 text-[11px]"
                  >
                    Send
                  </Button>
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SaveStatusIndicator({
  status,
  onRetry,
}: {
  status: SaveStatus;
  onRetry: () => void;
}) {
  if (status === "saving") {
    return (
      <div className="flex items-center gap-1.5 text-xs text-zinc-400">
        <Spinner className="h-3 w-3" />
        <span>Saving…</span>
      </div>
    );
  }
  if (status === "saved-moment") {
    return <span className="text-xs text-zinc-400">Saved a moment ago</span>;
  }
  if (status === "error") {
    return (
      <div className="flex items-center gap-1.5 text-xs">
        <span className="text-red-600">Couldn't save —</span>
        <button
          type="button"
          onClick={onRetry}
          className="text-red-600 underline underline-offset-2 hover:text-red-700"
        >
          retry
        </button>
      </div>
    );
  }
  // "saved" (default) — unobtrusive
  return <span className="text-xs text-zinc-400">Saved</span>;
}

/** Initials from an email address or name — for the inbox preview avatar.
 *  "ford@example.com" → "FO". "Bruce Genereaux" → "BG". Falls back to "SO". */
function initialsOf(input: string): string {
  const s = (input || "").trim();
  if (!s) return "SO";
  const local = s.includes("@") ? s.split("@")[0] : s;
  const tokens = local.split(/[._\s-]+/).filter(Boolean);
  if (tokens.length >= 2) {
    return (tokens[0][0] + tokens[1][0]).toUpperCase();
  }
  return local.slice(0, 2).toUpperCase();
}
