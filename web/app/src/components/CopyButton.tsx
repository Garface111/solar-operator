import { useState } from "react";

interface CopyButtonProps {
  value: string;
  /** Label for the default (idle) state. */
  label?: string;
  className?: string;
}

/** Copies `value` to the clipboard and flashes a confirmation. */
export function CopyButton({ value, label = "Copy", className = "" }: CopyButtonProps) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      // Fallback for older/insecure contexts.
      const ta = document.createElement("textarea");
      ta.value = value;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
      } catch {
        /* give up silently */
      }
      document.body.removeChild(ta);
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  }

  return (
    <button
      type="button"
      onClick={copy}
      className={[
        "inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-xs font-medium text-zinc-700",
        "transition-colors hover:bg-zinc-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-1",
        className,
      ].join(" ")}
    >
      {copied ? (
        <>
          <span aria-hidden className="text-primary-600">
            ✓
          </span>
          Copied
        </>
      ) : (
        <>
          <span aria-hidden>⧉</span>
          {label}
        </>
      )}
    </button>
  );
}
