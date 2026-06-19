import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

type ToastKind = "error" | "warning" | "success" | "info";

interface ToastItem {
  id: number;
  message: string;
  kind: ToastKind;
}

interface ToastApi {
  /** Show a toast. Errors are the common case for network failures. */
  show: (message: string, kind?: ToastKind) => void;
  error: (message: string) => void;
  warning: (message: string) => void;
  success: (message: string) => void;
  /** Remove all currently-shown toasts. Used when bouncing to the login screen
   *  so a sticky (dismiss-only) error toast doesn't linger over the new view. */
  clear: () => void;
}

const ToastContext = createContext<ToastApi | null>(null);

/** Auto-dismiss window for success/info toasts. Errors are dismiss-only so they
 *  don't vanish before the operator can read them or act on them. */
const TOAST_MS = 5000;

const KIND_STYLES: Record<ToastKind, string> = {
  error: "border-red-200 bg-red-50 text-red-800",
  warning: "border-amber-300 bg-amber-50 text-amber-900",
  success: "border-primary-200 bg-primary-50 text-primary-800",
  info: "border-zinc-200 bg-white text-zinc-800",
};

const KIND_ICON: Record<ToastKind, string> = {
  error: "⚠",
  warning: "⚠",
  success: "✓",
  info: "ℹ",
};

/** Wrap the app so any screen can raise a top-right auto-dismissing toast. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => {
    setToasts((ts) => ts.filter((t) => t.id !== id));
  }, []);

  const show = useCallback((message: string, kind: ToastKind = "error") => {
    const id = nextId.current++;
    setToasts((ts) => [...ts, { id, message, kind }]);
  }, []);

  const clear = useCallback(() => setToasts([]), []);

  const api: ToastApi = {
    show,
    error: useCallback((m: string) => show(m, "error"), [show]),
    warning: useCallback((m: string) => show(m, "warning"), [show]),
    success: useCallback((m: string) => show(m, "success"), [show]),
    clear,
  };

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div
        className="pointer-events-none fixed right-4 top-4 z-50 flex w-[calc(100%-2rem)] max-w-sm flex-col gap-2"
        role="region"
        aria-label="Notifications"
        aria-live="polite"
      >
        {toasts.map((t) => (
          <ToastCard key={t.id} toast={t} onDismiss={() => dismiss(t.id)} />
        ))}
      </div>
    </ToastContext.Provider>
  );
}

function ToastCard({
  toast,
  onDismiss,
}: {
  toast: ToastItem;
  onDismiss: () => void;
}) {
  useEffect(() => {
    // Error and warning toasts are dismiss-only — don't auto-dismiss so the
    // operator can read the message or act on it without a race against the timer.
    if (toast.kind === "error" || toast.kind === "warning") return;
    const id = window.setTimeout(onDismiss, TOAST_MS);
    return () => window.clearTimeout(id);
  }, [onDismiss, toast.kind]);

  return (
    <div
      role="alert"
      className={[
        "pointer-events-auto flex items-start gap-3 rounded-xl border px-4 py-3 text-sm shadow-lg",
        "animate-[toast-in_150ms_ease-out]",
        KIND_STYLES[toast.kind],
      ].join(" ")}
    >
      <span aria-hidden className="mt-0.5 shrink-0 font-semibold">
        {KIND_ICON[toast.kind]}
      </span>
      <span className="flex-1 leading-snug">{toast.message}</span>
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss notification"
        className="shrink-0 rounded opacity-60 transition-opacity hover:opacity-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-1"
      >
        ✕
      </button>
    </div>
  );
}

/** Access the toast API from any screen. Must be inside <ToastProvider>. */
export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within <ToastProvider>");
  return ctx;
}
