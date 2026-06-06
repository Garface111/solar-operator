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
  /** Applied to the raw input value on every keystroke before updating draft. */
  transform?: (value: string) => string;
  /**
   * Commit-time validator. If `valid` is false the save is blocked and
   * `reason` is shown as inline red text below the input.
   */
  validate?: (value: string) => { valid: boolean; reason?: string };
  maxLength?: number;
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
  transform,
  validate,
  maxLength,
}: EditableFieldProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
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

    if (validate) {
      const result = validate(next);
      if (!result.valid) {
        setError(result.reason ?? "Invalid value");
        inputRef.current?.focus();
        return;
      }
    }

    setEditing(false);
    setError("");
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
            className="text-xs text-zinc-300 transition-opacity group-hover:text-zinc-500"
          >
            ✎
          </span>
        )}
      </button>
    );
  }

  return (
    <div className="min-w-0">
      <input
        ref={inputRef}
        type={type}
        value={draft}
        placeholder={placeholder}
        maxLength={maxLength}
        onChange={(e) => {
          const v = transform ? transform(e.target.value) : e.target.value;
          setDraft(v);
          if (error) setError("");
        }}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            (e.target as HTMLInputElement).blur();
          } else if (e.key === "Escape") {
            setDraft(value ?? "");
            setEditing(false);
            setError("");
          }
        }}
        className={[
          "w-full rounded-md border bg-white px-2 py-1 text-sm",
          "focus:outline-none focus:ring-2 focus:ring-primary-500/40",
          error ? "border-red-400 focus:ring-red-400/40" : "border-primary-400",
          inputClassName,
        ].join(" ")}
      />
      {error && (
        <p className="mt-0.5 text-[11px] text-red-600">{error}</p>
      )}
    </div>
  );
}
