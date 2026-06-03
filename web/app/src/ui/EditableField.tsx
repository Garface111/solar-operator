import { useEffect, useRef, useState } from "react";
import { Spinner } from "./Spinner";

interface EditableFieldProps {
  value: string | null | undefined;
  /** Called when the user commits a changed value (blur or Enter). */
  onSave: (next: string) => Promise<void> | void;
  label?: string;
  placeholder?: string;
  type?: "text" | "email" | "number";
  /** Shown (muted) when value is empty and the field isn't focused. */
  emptyText?: string;
  /** Disable editing (renders read-only text). */
  readOnly?: boolean;
  className?: string;
  inputClassName?: string;
}

/**
 * Click-to-edit inline field. Renders as plain text until clicked; then becomes
 * an input that saves on blur or Enter and reverts on Escape. Saving is async:
 * a spinner shows while the onSave promise is in flight, and the field rolls
 * back to the previous value if onSave throws.
 */
export function EditableField({
  value,
  onSave,
  label,
  placeholder,
  type = "text",
  emptyText = "—",
  readOnly = false,
  className = "",
  inputClassName = "",
}: EditableFieldProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value ?? "");
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // Keep the draft in sync when the upstream value changes and we're idle.
  useEffect(() => {
    if (!editing) setDraft(value ?? "");
  }, [value, editing]);

  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  async function commit() {
    const next = draft.trim();
    const current = (value ?? "").trim();
    setEditing(false);
    if (next === current) {
      setDraft(value ?? "");
      return;
    }
    setSaving(true);
    try {
      await onSave(next);
    } catch {
      // Roll back to the last known-good value on failure (caller toasts).
      setDraft(value ?? "");
    } finally {
      setSaving(false);
    }
  }

  if (readOnly) {
    return (
      <span className={["text-sm text-zinc-700", className].join(" ")}>
        {value || <span className="text-zinc-400">{emptyText}</span>}
      </span>
    );
  }

  if (!editing) {
    return (
      <button
        type="button"
        onClick={() => setEditing(true)}
        aria-label={label ? `Edit ${label}` : "Edit field"}
        className={[
          "group inline-flex items-center gap-1.5 rounded-md px-1.5 py-1 text-left text-sm",
          "transition-colors duration-150 hover:bg-zinc-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40",
          className,
        ].join(" ")}
      >
        <span className={value ? "text-zinc-800" : "text-zinc-400"}>
          {value || emptyText}
        </span>
        {saving ? (
          <Spinner className="h-3 w-3 text-zinc-400" />
        ) : (
          <span
            aria-hidden
            className="text-xs text-zinc-300 opacity-0 transition-opacity group-hover:opacity-100"
          >
            ✎
          </span>
        )}
      </button>
    );
  }

  return (
    <input
      ref={inputRef}
      type={type}
      value={draft}
      placeholder={placeholder}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          (e.target as HTMLInputElement).blur();
        } else if (e.key === "Escape") {
          setDraft(value ?? "");
          setEditing(false);
        }
      }}
      className={[
        "w-full rounded-md border border-primary-400 bg-white px-2 py-1 text-sm",
        "focus:outline-none focus:ring-2 focus:ring-primary-500/40",
        inputClassName,
      ].join(" ")}
    />
  );
}
