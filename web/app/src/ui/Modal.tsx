import { useEffect, type ReactNode } from "react";

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  /** Footer actions (buttons). Rendered right-aligned. */
  footer?: ReactNode;
  /** Hide the top-right ✕ close button (e.g. when the modal must be
   *  confirmed/denied explicitly). Defaults to false. */
  hideCloseButton?: boolean;
}

/** Centered dialog with a dimmed backdrop. Closes on Escape, backdrop
 *  click, or the ✕ button in the top-right corner. */
export function Modal({ open, onClose, title, children, footer, hideCloseButton }: ModalProps) {
  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-zinc-900/40 px-4 py-8"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        // Flex column + capped height so a long body scrolls *inside* the
        // dialog instead of growing it past the viewport (which would push the
        // footer buttons off-screen). py-8 backdrop = 4rem of vertical inset.
        className="relative flex max-h-[calc(100vh-4rem)] w-full max-w-md flex-col rounded-xl border border-zinc-200 bg-white shadow-xl"
      >
        {!hideCloseButton && (
          <button
            type="button"
            onClick={onClose}
            aria-label="Close dialog"
            className="absolute right-3 top-3 z-10 flex h-8 w-8 items-center justify-center rounded-full text-zinc-400 transition-colors hover:bg-zinc-100 hover:text-zinc-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
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
        )}
        <h2 className="shrink-0 px-6 pt-6 pr-12 text-lg font-semibold tracking-tight text-zinc-900">
          {title}
        </h2>
        <div className="mt-4 min-h-0 flex-1 overflow-y-auto px-6 text-sm text-zinc-600">{children}</div>
        {footer && (
          <div className="mt-4 flex shrink-0 justify-end gap-2 border-t border-zinc-100 px-6 pb-6 pt-4">{footer}</div>
        )}
        {!footer && <div className="pb-6" />}
      </div>
    </div>
  );
}
