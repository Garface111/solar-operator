import { useEffect, useRef, useState } from "react";
import type { Account } from "../lib/api";

// ── Gating ──────────────────────────────────────────────────────────────────
// The Mind button is a private dogfood feature during this rollout. It renders
// ONLY for the operators listed here. Keep this list tiny and explicit; broaden
// later once we're ready to ship widely. Gating is by email because the client
// auth context (Account.email) is always populated, whereas Ford's tenant_id
// isn't reliably pinned in client config yet.
export const MIND_BUTTON_ALLOWED_EMAILS = ["ford.genereaux@gmail.com"];

// ── Backend wiring ──────────────────────────────────────────────────────────
// Configurable so Ford can dogfood against his own dev Mind. Falls back to the
// local dev port when VITE_MIND_BASE is unset. See web/app/README.md.
const MIND_BASE = import.meta.env.VITE_MIND_BASE ?? "http://localhost:8001";

const SESSION_KEY = "mind-session-id";

interface ChatMessage {
  role: "user" | "mind";
  text: string;
}

/** Stable per-browser session id. Generated once, reused forever. */
function getSessionId(): string {
  try {
    let id = window.localStorage.getItem(SESSION_KEY);
    if (!id) {
      id = crypto.randomUUID();
      window.localStorage.setItem(SESSION_KEY, id);
    }
    return id;
  } catch {
    // Private mode / storage disabled — fall back to an ephemeral id.
    return crypto.randomUUID();
  }
}

interface Props {
  account: Account | null;
}

/**
 * Floating "Talk to OCICBB" button + slide-in chat panel.
 *
 * Renders nothing unless the current operator is allow-listed (see
 * MIND_BUTTON_ALLOWED_EMAILS). When open, posts to `${MIND_BASE}/v1/chat` and
 * streams the SSE response, accumulating `delta` events and finalizing on
 * `done`.
 */
export function MindButton({ account }: Props) {
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [thinking, setThinking] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);
  const sessionId = useRef<string | null>(null);

  // Auto-scroll the message log to the bottom as content streams in.
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, thinking]);

  // Close on Escape while the panel is open.
  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  // Beta: show to everyone. Gating removed Jun 6'26 ("we are still beta testing").
  const allowed = !!account?.email;
  if (!allowed) return null;

  async function send() {
    const text = input.trim();
    if (!text || thinking) return;
    if (!sessionId.current) sessionId.current = getSessionId();

    setInput("");
    setMessages((m) => [...m, { role: "user", text }]);
    setThinking(true);

    // Reserve a Mind message slot we'll fill as deltas arrive.
    let mindIndex = -1;
    const appendDelta = (chunk: string) => {
      setMessages((m) => {
        const next = [...m];
        if (mindIndex < 0) {
          mindIndex = next.length;
          next.push({ role: "mind", text: chunk });
        } else {
          next[mindIndex] = {
            role: "mind",
            text: next[mindIndex].text + chunk,
          };
        }
        return next;
      });
    };

    try {
      const res = await fetch(`${MIND_BASE}/v1/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project: "solar-operator",
          session_id: sessionId.current,
          page: window.location.pathname,
          page_path: window.location.pathname,  // belt+suspenders: backend uses `page`
          message: text,
          // Identity — the Mind is on our team and should know who it's talking
          // to. The host product already has the auth context; pass it through
          // instead of forcing the Mind to address an anonymized "operator-since-N"
          // when we know perfectly well it's Bruce running GMCS. Ford Jun 7'26:
          // "the agent needs access to the person who is using its account so
          // they are on the same page and can introduce themselves. IT'S OUR SYSTEM."
          operator_name: account?.send_from_name || account?.name || "",
          operator_email: account?.email || "",
          tenant_name: account?.name || "",
          tenant_id: account?.tenant_id || "",
        }),
      });
      if (!res.ok || !res.body) {
        throw new Error(`Mind responded ${res.status}`);
      }

      // SSE over fetch: read the stream, split on newlines, parse `data:` lines.
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let done = false;

      while (!done) {
        const { value, done: streamDone } = await reader.read();
        if (streamDone) break;
        buffer += decoder.decode(value, { stream: true });

        // Process complete lines; keep the trailing partial in the buffer.
        let nl: number;
        while ((nl = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, nl).trim();
          buffer = buffer.slice(nl + 1);
          if (!line.startsWith("data:")) continue;
          const payload = line.slice(5).trim();
          if (!payload) continue;

          let evt: { type?: string; text?: string };
          try {
            evt = JSON.parse(payload);
          } catch {
            continue; // ignore malformed frames
          }

          if (evt.type === "delta" && evt.text) {
            // First real text arrived — drop the thinking indicator.
            setThinking(false);
            appendDelta(evt.text);
          } else if (evt.type === "done") {
            done = true;
            break;
          }
          // `meta` and any unknown types are ignored.
        }
      }
    } catch {
      // Stay in character if the Mind is unreachable.
      if (mindIndex < 0) {
        setMessages((m) => [
          ...m,
          {
            role: "mind",
            text: "I'm briefly quiet — couldn't reach the Mind just now. Try again in a moment.",
          },
        ]);
      }
    } finally {
      setThinking(false);
    }
  }

  return (
    <>
      {/* Floating launcher */}
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label="Talk to OCICBB"
        title="Talk to OCICBB"
        className="fixed bottom-6 right-6 z-40 flex h-12 w-12 items-center justify-center rounded-full bg-primary-500 text-white shadow-lg ring-1 ring-black/5 transition-all duration-150 hover:-translate-y-0.5 hover:shadow-xl hover:ring-2 hover:ring-wood-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-wood-300"
      >
        <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden fill="currentColor">
          {/* Four-point sparkle */}
          <path d="M12 2l1.6 6.4L20 10l-6.4 1.6L12 18l-1.6-6.4L4 10l6.4-1.6L12 2z" />
        </svg>
      </button>

      {open && (
        <div className="fixed inset-0 z-50" role="dialog" aria-modal="true" aria-label="OCICBB chat">
          {/* Backdrop */}
          <div
            className="absolute inset-0 bg-zinc-900/30"
            onMouseDown={() => setOpen(false)}
          />

          {/* Side panel */}
          <div className="absolute inset-y-0 right-0 flex w-full max-w-full flex-col border-l border-cream-border bg-white shadow-2xl sm:w-[420px]">
            {/* Header */}
            <div className="flex shrink-0 items-start justify-between gap-3 border-b border-cream-border bg-cream px-5 py-4">
              <div>
                <h2 className="font-serif text-lg leading-tight text-zinc-900">
                  Of Course It Could Be Better
                </h2>
                <p className="text-xs font-medium uppercase tracking-wide text-wood-500">
                  OCICBB
                </p>
              </div>
              <button
                type="button"
                onClick={() => setOpen(false)}
                aria-label="Close chat"
                className="-mr-1 flex h-8 w-8 items-center justify-center rounded-full text-zinc-400 transition-colors hover:bg-zinc-100 hover:text-zinc-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
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

            {/* Message log */}
            <div ref={logRef} className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-4">
              {messages.length === 0 && !thinking && (
                <p className="text-sm text-zinc-400">
                  Ask the Mind anything about this page or your operation.
                </p>
              )}
              {messages.map((m, i) =>
                m.role === "user" ? (
                  <div key={i} className="flex justify-end">
                    <div className="max-w-[85%] whitespace-pre-wrap rounded-xl rounded-br-sm bg-primary-500 px-3.5 py-2 text-sm text-white">
                      {m.text}
                    </div>
                  </div>
                ) : (
                  <div key={i} className="border-t border-wood-300 pt-3">
                    <div className="whitespace-pre-wrap text-sm leading-relaxed text-zinc-700">
                      {m.text}
                    </div>
                  </div>
                ),
              )}
              {thinking && (
                <div className="border-t border-wood-300 pt-3" aria-label="Mind is thinking">
                  <div className="flex items-center gap-1.5">
                    {[0, 1, 2].map((i) => (
                      <span
                        key={i}
                        className="h-2 w-2 animate-pulse rounded-full bg-wood-400"
                        style={{ animationDelay: `${i * 0.2}s` }}
                      />
                    ))}
                  </div>
                </div>
              )}
            </div>

            {/* Composer */}
            <div className="shrink-0 border-t border-cream-border bg-cream px-4 py-3">
              <div className="flex items-end gap-2">
                <textarea
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      void send();
                    }
                  }}
                  rows={2}
                  placeholder="Talk to OCICBB…"
                  className="min-h-0 flex-1 resize-none rounded-xl border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 placeholder:text-zinc-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500/30"
                />
                <button
                  type="button"
                  onClick={() => void send()}
                  disabled={!input.trim() || thinking}
                  aria-label="Send message"
                  className="inline-flex h-10 shrink-0 items-center justify-center rounded-xl bg-primary-500 px-4 text-sm font-medium text-white transition-colors hover:bg-primary-600 active:bg-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  Send
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
